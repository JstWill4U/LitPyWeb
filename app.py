import sys
import email.utils
import mimetypes
import functools
import threading
import weakref
import json
import pickle
import hashlib
import io
import tempfile
import cgi
from datetime import date as datedate
from datetime import datetime, timedelta
from urllib.parse import urljoin, SplitResult, urlencode, quote, unquote

from .router import Router, Route, RouteError, RouteBuildError, RouteSyntaxError
from .request_response import Request, Response, LocalRequest, LocalResponse


class LitPyWebException(Exception):
    """ A base class for exceptions used by LitPyWeb. """
    pass

class RouteSyntaxError(RouteError):
    """ The route parser found something not supported by this router. """


class RouteBuildError(RouteError):
    """ The route could not be built. """


def _re_flatten(p):
    """ Turn all capturing groups in a regular expression pattern into
        non-capturing groups. """
    if '(' not in p:
        return p
    return re.sub(r'(\\*)(\(\?P<[^>]+>|\((?!\?))', lambda m: m.group(0) if
                  len(m.group(1)) % 2 else m.group(1) + '(?:', p)

class LitPyWeb:
    """ Each LitPyWeb object represents a single, distinct web application and
        consists of routes, callbacks, plugins, resources and configuration.
        Instances are callable WSGI applications.

        :param catchall: If true (default), handle all exceptions. Turn off to
                         let debugging middleware handle exceptions.
    """

    @lazy_attribute
    def _global_config(cls):
        cfg = ConfigDict()
        cfg.meta_set('catchall', 'validate', bool)
        return cfg

    def __init__(self, **kwargs):
        #: A :class:`ConfigDict` for app specific configuration.
        self.config = self._global_config._make_overlay()
        self.config._add_change_listener(
            functools.partial(self.trigger_hook, 'config'))

        self.config.update({
            "catchall": True
        })

        if kwargs.get('catchall') is False:
            depr(0, 13, "LitPyWeb(catchall) keyword argument.",
                        "The 'catchall' setting is now part of the app "
                        "configuration. Fix: `app.config['catchall'] = False`")
            self.config['catchall'] = False
        if kwargs.get('autojson') is False:
            depr(0, 13, "LitPyWeb(autojson) keyword argument.",
                 "The 'autojson' setting is now part of the app "
                 "configuration. Fix: `app.config['json.enable'] = False`")
            self.config['json.enable'] = False

        self._mounts = []

        #: A :class:`ResourceManager` for application files
        self.resources = ResourceManager()

        self.routes = []  # List of installed :class:`Route` instances.
        self.router = Router()  # Maps requests to :class:`Route` instances.
        self.error_handler = {}

        # Core plugins
        self.plugins = []  # List of installed plugins.
        self.install(JSONPlugin())
        self.install(TemplatePlugin())

    #: If true, most exceptions are caught and returned as :exc:`HTTPError`
    catchall = DictProperty('config', 'catchall')

    __hook_names = 'before_request', 'after_request', 'app_reset', 'config'
    __hook_reversed = {'after_request'}

    @cached_property
    def _hooks(self):
        return dict((name, []) for name in self.__hook_names)

    def add_hook(self, name, func):
        """ Attach a callback to a hook. Three hooks are currently implemented:

            before_request
                Executed once before each request. The request context is
                available, but no routing has happened yet.
            after_request
                Executed once after each request regardless of its outcome.
            app_reset
                Called whenever :meth:`LitPyWeb.reset` is called.
        """
        if name in self.__hook_reversed:
            self._hooks[name].insert(0, func)
        else:
            self._hooks[name].append(func)

    def remove_hook(self, name, func):
        """ Remove a callback from a hook. """
        if name in self._hooks and func in self._hooks[name]:
            self._hooks[name].remove(func)
            return True

    def trigger_hook(self, __name, *args, **kwargs):
        """ Trigger a hook and return a list of results. """
        return [hook(*args, **kwargs) for hook in self._hooks[__name][:]]

    def hook(self, name):
        """ Return a decorator that attaches a callback to a hook. See
            :meth:`add_hook` for details."""

        def decorator(func):
            self.add_hook(name, func)
            return func

        return decorator

    def _mount_wsgi(self, prefix, app, **options):
        segments = [p for p in prefix.split('/') if p]
        if not segments:
            raise ValueError('WSGI applications cannot be mounted to "/".')
        path_depth = len(segments)

        def mountpoint_wrapper():
            try:
                request.path_shift(path_depth)
                rs = HTTPResponse([])

                def start_response(status, headerlist, exc_info=None):
                    if exc_info:
                        _raise(*exc_info)
                    status = _wsgi_recode(status)
                    headerlist = [(k, _wsgi_recode(v))
                                    for (k, v) in headerlist]
                    rs.status = status
                    for name, value in headerlist:
                        rs.add_header(name, value)
                    return rs.body.append

                body = app(request.environ, start_response)
                rs.body = itertools.chain(rs.body, body) if rs.body else body
                return rs
            finally:
                request.path_shift(-path_depth)

        options.setdefault('skip', True)
        options.setdefault('method', 'PROXY')
        options.setdefault('mountpoint', {'prefix': prefix, 'target': app})
        options['callback'] = mountpoint_wrapper

        self.route('/%s/<:re:.*>' % '/'.join(segments), **options)
        if not prefix.endswith('/'):
            self.route('/' + '/'.join(segments), **options)

    def _mount_app(self, prefix, app, **options):
        if app in self._mounts or '_mount.app' in app.config:
            depr(0, 13, "Application mounted multiple times. Falling back to WSGI mount.",
                 "Clone application before mounting to a different location.")
            return self._mount_wsgi(prefix, app, **options)

        if options:
            depr(0, 13, "Unsupported mount options. Falling back to WSGI mount.",
                 "Do not specify any route options when mounting LitPyWeb application.")
            return self._mount_wsgi(prefix, app, **options)

        if not prefix.endswith("/"):
            depr(0, 13, "Prefix must end in '/'. Falling back to WSGI mount.",
                 "Consider adding an explicit redirect from '/prefix' to '/prefix/' in the parent application.")
            return self._mount_wsgi(prefix, app, **options)

        self._mounts.append(app)
        app.config['_mount.prefix'] = prefix
        app.config['_mount.app'] = self
        for route in app.routes:
            route.rule = prefix + route.rule.lstrip('/')
            self.add_route(route)

    def mount(self, prefix, app, **options):
        """ Mount an application (:class:`LitPyWeb` or plain WSGI) to a specific
            URL prefix. Example::

                parent_app.mount('/prefix/', child_app)

            :param prefix: path prefix or `mount-point`.
            :param app: an instance of :class:`LitPyWeb` or a WSGI application.

            Plugins from the parent application are not applied to the routes
            of the mounted child application. If you need plugins in the child
            application, install them separately.

            While it is possible to use path wildcards within the prefix path
            (:class:`LitPyWeb` childs only), it is highly discouraged.

            The prefix path must end with a slash. If you want to access the
            root of the child application via `/prefix` in addition to
            `/prefix/`, consider adding a route with a 307 redirect to the
            parent application.
        """

        if not prefix.startswith('/'):
            raise ValueError("Prefix must start with '/'")

        if isinstance(app, LitPyWeb):
            return self._mount_app(prefix, app, **options)
        else:
            return self._mount_wsgi(prefix, app, **options)

    def merge(self, routes):
        """ Merge the routes of another :class:`LitPyWeb` application or a list of
            :class:`Route` objects into this application. The routes keep their
            'owner', meaning that the :data:`Route.app` attribute is not
            changed. """
        if isinstance(routes, LitPyWeb):
            routes = routes.routes
        for route in routes:
            self.add_route(route)

    def install(self, plugin):
        """ Add a plugin to the list of plugins and prepare it for being
            applied to all routes of this application. A plugin may be a simple
            decorator or an object that implements the :class:`Plugin` API.
        """
        if hasattr(plugin, 'setup'): plugin.setup(self)
        if not callable(plugin) and not hasattr(plugin, 'apply'):
            raise TypeError("Plugins must be callable or implement .apply()")
        self.plugins.append(plugin)
        self.reset()
        return plugin

    def uninstall(self, plugin):
        """ Uninstall plugins. Pass an instance to remove a specific plugin, a type
            object to remove all plugins that match that type, a string to remove
            all plugins with a matching ``name`` attribute or ``True`` to remove all
            plugins. Return the list of removed plugins. """
        removed, remove = [], plugin
        for i, plugin in list(enumerate(self.plugins))[::-1]:
            if remove is True or remove is plugin or remove is type(plugin) \
            or getattr(plugin, 'name', True) == remove:
                removed.append(plugin)
                del self.plugins[i]
                if hasattr(plugin, 'close'): plugin.close()
        if removed: self.reset()
        return removed

    def reset(self, route=None):
        """ Reset all routes (force plugins to be re-applied) and clear all
            caches. If an ID or route object is given, only that specific route
            is affected. """
        if route is None: routes = self.routes
        elif isinstance(route, Route): routes = [route]
        else: routes = [self.routes[route]]
        for route in routes:
            route.reset()
        if DEBUG:
            for route in routes:
                route.prepare()
        self.trigger_hook('app_reset')

    def close(self):
        """ Close the application and all installed plugins. """
        for plugin in self.plugins:
            if hasattr(plugin, 'close'): plugin.close()

    def run(self, **kwargs):
        """ Calls :func:`run` with the same parameters. """
        run(self, **kwargs)

    def match(self, environ):
        """ Search for a matching route and return a (:class:`Route`, urlargs)
            tuple. The second value is a dictionary with parameters extracted
            from the URL. Raise :exc:`HTTPError` (404/405) on a non-match."""
        return self.router.match(environ)

    def get_url(self, routename, **kargs):
        """ Return a string that matches a named route """
        scriptname = request.environ.get('SCRIPT_NAME', '').strip('/') + '/'
        location = self.router.build(routename, **kargs).lstrip('/')
        return urljoin(urljoin('/', scriptname), location)

    def add_route(self, route):
        """ Add a route object, but do not change the :data:`Route.app`
            attribute."""
        self.routes.append(route)
        self.router.add(route.rule, route.method, route, name=route.name)
        if DEBUG: route.prepare()

    def route(self,
              path=None,
              method='GET',
              callback=None,
              name=None,
              apply=None,
              skip=None, **config):
        """ A decorator to bind a function to a request URL. Example::

                @app.route('/hello/<name>')
                def hello(name):
                    return 'Hello %s' % name

            The ``<name>`` part is a wildcard. See :class:`Router` for syntax
            details.

            :param path: Request path or a list of paths to listen to. If no
              path is specified, it is automatically generated from the
              signature of the function.
            :param method: HTTP method (`GET`, `POST`, `PUT`, ...) or a list of
              methods to listen to. (default: `GET`)
            :param callback: An optional shortcut to avoid the decorator
              syntax. ``route(..., callback=func)`` equals ``route(...)(func)``
            :param name: The name for this route. (default: None)
            :param apply: A decorator or plugin or a list of plugins. These are
              applied to the route callback in addition to installed plugins.
            :param skip: A list of plugins, plugin classes or names. Matching
              plugins are not installed to this route. ``True`` skips all.

            Any additional keyword arguments are stored as route-specific
            configuration and passed to plugins (see :meth:`Plugin.apply`).
        """
        if callable(path): path, callback = None, path
        plugins = makelist(apply)
        skiplist = makelist(skip)

        def decorator(callback):
            if isinstance(callback, str): callback = load(callback)
            for rule in makelist(path) or yieldroutes(callback):
                for verb in makelist(method):
                    verb = verb.upper()
                    route = Route(self, rule, verb, callback,
                                  name=name,
                                  plugins=plugins,
                                  skiplist=skiplist, **config)
                    self.add_route(route)
            return callback

        return decorator(callback) if callback else decorator

    def get(self, path=None, method='GET', **options):
        """ Equals :meth:`route`. """
        return self.route(path, method, **options)

    def post(self, path=None, method='POST', **options):
        """ Equals :meth:`route` with a ``POST`` method parameter. """
        return self.route(path, method, **options)

    def put(self, path=None, method='PUT', **options):
        """ Equals :meth:`route` with a ``PUT`` method parameter. """
        return self.route(path, method, **options)

    def delete(self, path=None, method='DELETE', **options):
        """ Equals :meth:`route` with a ``DELETE`` method parameter. """
        return self.route(path, method, **options)

    def patch(self, path=None, method='PATCH', **options):
        """ Equals :meth:`route` with a ``PATCH`` method parameter. """
        return self.route(path, method, **options)

    def error(self, code=500, callback=None):
        """ Register an output handler for a HTTP error code. Can
            be used as a decorator or called directly ::

                def error_handler_500(error):
                    return 'error_handler_500'

                app.error(code=500, callback=error_handler_500)

                @app.error(404)
                def error_handler_404(error):
                    return 'error_handler_404'

        """

        def decorator(callback):
            if isinstance(callback, str): callback = load(callback)
            self.error_handler[int(code)] = callback
            return callback

        return decorator(callback) if callback else decorator

    def default_error_handler(self, res):
        return tob(template(ERROR_PAGE_TEMPLATE, e=res, template_settings=dict(name='__ERROR_PAGE_TEMPLATE')))

    def _handle(self, environ):
        path = environ['LitPyWeb.raw_path'] = environ['PATH_INFO']
        environ['PATH_INFO'] = _wsgi_recode(path)

        environ['LitPyWeb.app'] = self
        request.bind(environ)
        response.bind()
        out = None

        try:
            try:
                self.trigger_hook('before_request')
                route, args = self.router.match(environ)
                environ['route.handle'] = route
                environ['LitPyWeb.route'] = route
                environ['route.url_args'] = args
                out = route.call(**args)
            except HTTPResponse as E:
                out = E
            finally:
                if isinstance(out, HTTPResponse):
                    out.apply(response)
                try:
                    self.trigger_hook('after_request')
                except HTTPResponse as E:
                    out = E
                    out.apply(response)
        except (KeyboardInterrupt, SystemExit, MemoryError):
            raise
        except Exception as E:
            _try_close(out)
            if not self.catchall: raise
            stacktrace = format_exc()
            environ['wsgi.errors'].write(stacktrace)
            environ['wsgi.errors'].flush()
            environ['LitPyWeb.exc_info'] = sys.exc_info()
            out = HTTPError(500, "Internal Server Error", E, stacktrace)
            out.apply(response)

        return out

    def _cast(self, out, peek=None):
        """ Try to convert the parameter into something WSGI compatible and set
        correct HTTP headers when possible.
        Support: False, bytes/bytearray, str, dict, HTTPResponse, HTTPError, file-like,
        iterable of bytes/bytearray or str instances.
        """

        # Empty output is done here
        if not out:
            if 'Content-Length' not in response:
                response['Content-Length'] = 0
            return []
        # Join lists of byte or unicode strings. Mixed lists are NOT supported
        if isinstance(out, (tuple, list))\
        and isinstance(out[0], (bytes, str)):
            out = out[0][0:0].join(out)  # b'abc'[0:0] -> b''
        # Encode unicode strings
        if isinstance(out, str):
            out = out.encode(response.charset)
        # Byte Strings are just returned
        if isinstance(out, bytes):
            if 'Content-Length' not in response:
                response['Content-Length'] = len(out)
            return [out]
        # HTTPError or HTTPException (recursive, because they may wrap anything)
        # TODO: Handle these explicitly in handle() or make them iterable.
        if isinstance(out, HTTPError):
            out.apply(response)
            out = self.error_handler.get(out.status_code,
                                         self.default_error_handler)(out)
            return self._cast(out)
        if isinstance(out, HTTPResponse):
            out.apply(response)
            return self._cast(out.body)

        # File-like objects.
        if hasattr(out, 'read'):
            if 'wsgi.file_wrapper' in request.environ:
                return request.environ['wsgi.file_wrapper'](out)
            elif hasattr(out, 'close') or not hasattr(out, '__iter__'):
                return WSGIFileWrapper(out)

        # Handle Iterables. We peek into them to detect their inner type.
        try:
            iout = iter(out)
            first = next(iout)
            while not first:
                first = next(iout)
        except StopIteration:
            _try_close(out)
            return self._cast('')
        except HTTPResponse as E:
            first = E
        except (KeyboardInterrupt, SystemExit, MemoryError):
            raise
        except Exception as error:
            _try_close(out)
            if not self.catchall: raise
            first = HTTPError(500, 'Unhandled exception', error, format_exc())

        # These are the inner types allowed in iterator or generator objects.
        if isinstance(first, HTTPResponse):
            return self._cast(first)
        elif isinstance(first, bytes):
            new_iter = itertools.chain([first], iout)
        elif isinstance(first, str):
            encoder = lambda x: x.encode(response.charset)
            new_iter = map(encoder, itertools.chain([first], iout))
        else:
            _try_close(out)
            msg = 'Unsupported response type: %s' % type(first)
            return self._cast(HTTPError(500, msg))
        if hasattr(out, 'close'):
            new_iter = _closeiter(new_iter, out.close)
        return new_iter

    def wsgi(self, environ, start_response):
        """ The LitPyWeb WSGI-interface. """
        out = None
        try:
            out = self._cast(self._handle(environ))
            # rfc2616 section 4.3
            if response._status_code in (100, 101, 204, 304)\
            or environ['REQUEST_METHOD'] == 'HEAD':
                if hasattr(out, 'close'): out.close()
                out = []
            exc_info = environ.get('LitPyWeb.exc_info')
            if exc_info is not None:
                del environ['LitPyWeb.exc_info']
            start_response(response._wsgi_status_line(), response.headerlist, exc_info)
            return out
        except (KeyboardInterrupt, SystemExit, MemoryError):
            raise
        except Exception as E:
            _try_close(out)
            if not self.catchall: raise
            err = '<h1>Critical error while processing request: %s</h1>' \
                  % html_escape(environ.get('PATH_INFO', '/'))
            if DEBUG:
                err += '<h2>Error:</h2>\n<pre>\n%s\n</pre>\n' \
                       '<h2>Traceback:</h2>\n<pre>\n%s\n</pre>\n' \
                       % (html_escape(repr(E)), html_escape(format_exc()))
            environ['wsgi.errors'].write(err)
            environ['wsgi.errors'].flush()
            headers = [('Content-Type', 'text/html; charset=UTF-8')]
            start_response('500 INTERNAL SERVER ERROR', headers, sys.exc_info())
            return [tob(err)]

    def __call__(self, environ, start_response):
        """ Each instance of :class:'LitPyWeb' is a WSGI application. """
        return self.wsgi(environ, start_response)

    def __enter__(self):
        """ Use this application as default for all module-level shortcuts. """
        default_app.push(self)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        default_app.pop()

    def __setattr__(self, name, value):
        if name in self.__dict__:
            raise AttributeError("Attribute %s already defined. Plugin conflict?" % name)
        object.__setattr__(self, name, value)

class _ImportRedirect:
    def __init__(self, name, impmask):
        """ Create a virtual package that redirects imports (see PEP 302). """
        self.name = name
        self.impmask = impmask
        self.module = sys.modules.setdefault(name, new_module(name))
        self.module.__dict__.update({
            '__file__': __file__,
            '__path__': [],
            '__all__': [],
            '__loader__': self
        })
        sys.meta_path.append(self)

    def find_spec(self, fullname, path, target=None):
        if '.' not in fullname: return
        if fullname.rsplit('.', 1)[0] != self.name: return
        from importlib.util import spec_from_loader
        return spec_from_loader(fullname, self)

    def find_module(self, fullname, path=None):
        if '.' not in fullname: return
        if fullname.rsplit('.', 1)[0] != self.name: return
        return self

    def create_module(self, spec):
        return self.load_module(spec.name)

    def exec_module(self, module):
        pass # This probably breaks importlib.reload() :/

    def load_module(self, fullname):
        if fullname in sys.modules: return sys.modules[fullname]
        modname = fullname.rsplit('.', 1)[1]
        realname = self.impmask % modname
        __import__(realname)
        module = sys.modules[fullname] = sys.modules[realname]
        setattr(self.module, modname, module)
        module.__loader__ = self
        return module

###############################################################################
# Common Utilities #############################################################
###############################################################################


class MultiDict(DictMixin):
    """ This dict stores multiple values per key, but behaves exactly like a
        normal dict in that it returns only the newest value for any given key.
        There are special methods available to access the full list of values.
    """

    def __init__(self, *a, **k):
        self.dict = dict((k, [v]) for (k, v) in dict(*a, **k).items())

    def __len__(self):
        return len(self.dict)

    def __iter__(self):
        return iter(self.dict)

    def __contains__(self, key):
        return key in self.dict

    def __delitem__(self, key):
        del self.dict[key]

    def __getitem__(self, key):
        return self.dict[key][-1]

    def __setitem__(self, key, value):
        self.append(key, value)

    def keys(self):
        return self.dict.keys()

    def values(self):
        return (v[-1] for v in self.dict.values())

    def items(self):
        return ((k, v[-1]) for k, v in self.dict.items())

    def allitems(self):
        return ((k, v) for k, vl in self.dict.items() for v in vl)

    iterkeys = keys
    itervalues = values
    iteritems = items
    iterallitems = allitems

    def get(self, key, default=None, index=-1, type=None):
        """ Return the most recent value for a key.

            :param default: The default value to be returned if the key is not
                   present or the type conversion fails.
            :param index: An index for the list of available values.
            :param type: If defined, this callable is used to cast the value
                    into a specific type. Exception are suppressed and result in
                    the default value to be returned.
        """
        try:
            val = self.dict[key][index]
            return type(val) if type else val
        except Exception:
            pass
        return default

    def append(self, key, value):
        """ Add a new value to the list of values for this key. """
        self.dict.setdefault(key, []).append(value)

    def replace(self, key, value):
        """ Replace the list of values with a single value. """
        self.dict[key] = [value]

    def getall(self, key):
        """ Return a (possibly empty) list of values for a key. """
        return self.dict.get(key) or []

    #: Aliases for WTForms to mimic other multi-dict APIs (Django)
    getone = get
    getlist = getall


class FormsDict(MultiDict):
    """ This :class:`MultiDict` subclass is used to store request form data.
        Additionally to the normal dict-like item access methods, this container
        also supports attribute-like access to its values. Missing attributes
        default to an empty string.

        .. versionchanged:: 0.14
            All keys and values are now decoded as utf8 by default, item and
            attribute access will return the same string.
    """

    def decode(self, encoding=None):
        """ (deprecated) Starting with 0.13 all keys and values are already
            correctly decoded. """
        copy = FormsDict()
        for key, value in self.allitems():
            copy[key] = value
        return copy

    def getunicode(self, name, default=None, encoding=None):
        """ (deprecated) Return the value as a unicode string, or the default. """
        return self.get(name, default)

    def __getattr__(self, name, default=str()):
        # Without this guard, pickle generates a cryptic TypeError:
        if name.startswith('__') and name.endswith('__'):
            return super(FormsDict, self).__getattr__(name)
        return self.get(name, default=default)

class HeaderDict(MultiDict):
    """ A case-insensitive version of :class:`MultiDict` that defaults to
        replace the old value instead of appending it. """

    def __init__(self, *a, **ka):
        self.dict = {}
        if a or ka: self.update(*a, **ka)

    def __contains__(self, key):
        return _hkey(key) in self.dict

    def __delitem__(self, key):
        del self.dict[_hkey(key)]

    def __getitem__(self, key):
        return self.dict[_hkey(key)][-1]

    def __setitem__(self, key, value):
        self.dict[_hkey(key)] = [_hval(value)]

    def append(self, key, value):
        self.dict.setdefault(_hkey(key), []).append(_hval(value))

    def replace(self, key, value):
        self.dict[_hkey(key)] = [_hval(value)]

    def getall(self, key):
        return self.dict.get(_hkey(key)) or []

    def get(self, key, default=None, index=-1):
        return MultiDict.get(self, _hkey(key), default, index)

    def filter(self, names):
        for name in (_hkey(n) for n in names):
            if name in self.dict:
                del self.dict[name]


class WSGIHeaderDict(DictMixin):
    """ This dict-like class wraps a WSGI environ dict and provides convenient
        access to HTTP_* fields. Header names are case-insensitive and titled by default.
    """
    #: List of keys that do not have a ``HTTP_`` prefix.
    cgikeys = ('CONTENT_TYPE', 'CONTENT_LENGTH')

    def __init__(self, environ):
        self.environ = environ

    def _ekey(self, key):
        """ Translate header field name to CGI/WSGI environ key. """
        key = key.replace('-', '_').upper()
        if key in self.cgikeys:
            return key
        return 'HTTP_' + key

    def raw(self, key, default=None):
        """ Return the header value as is (not utf8-translated). """
        return self.environ.get(self._ekey(key), default)

    def __getitem__(self, key):
        return _wsgi_recode(self.environ[self._ekey(key)])

    def __setitem__(self, key, value):
        raise TypeError("%s is read-only." % self.__class__)

    def __delitem__(self, key):
        raise TypeError("%s is read-only." % self.__class__)

    def __iter__(self):
        for key in self.environ:
            if key[:5] == 'HTTP_':
                yield _hkey(key[5:])
            elif key in self.cgikeys:
                yield _hkey(key)

    def keys(self):
        return [x for x in self]

    def __len__(self):
        return len(self.keys())

    def __contains__(self, key):
        return self._ekey(key) in self.environ

_UNSET = object()

class ConfigDict(dict):
    """ A dict-like configuration storage with additional support for
        namespaces, validators, meta-data and overlays.

        This dict-like class is heavily optimized for read access.
        Read-only methods and item access should be as fast as a native dict.
    """

    __slots__ = ('_meta', '_change_listener', '_overlays', '_virtual_keys', '_source', '__weakref__')

    def __init__(self):
        self._meta = {}
        self._change_listener = []
        #: Weak references of overlays that need to be kept in sync.
        self._overlays = []
        #: Config that is the source for this overlay.
        self._source = None
        #: Keys of values copied from the source (values we do not own)
        self._virtual_keys = set()

    def load_module(self, name, squash=True):
        """Load values from a Python module.

           Import a python module by name and add all upper-case module-level
           variables to this config dict.

           :param name: Module name to import and load.
           :param squash: If true (default), nested dicts are assumed to
              represent namespaces and flattened (see :meth:`load_dict`).
        """
        config_obj = load(name)
        obj = {key: getattr(config_obj, key)
               for key in dir(config_obj) if key.isupper()}

        if squash:
            self.load_dict(obj)
        else:
            self.update(obj)
        return self

    def load_config(self, filename, **options):
        """ Load values from ``*.ini`` style config files using configparser.

            INI style sections (e.g. ``[section]``) are used as namespace for
            all keys within that section. Both section and key names may contain
            dots as namespace separators and are converted to lower-case.

            The special sections ``[LitPyWeb]`` and ``[ROOT]`` refer to the root
            namespace and the ``[DEFAULT]`` section defines default values for all
            other sections.

            :param filename: The path of a config file, or a list of paths.
            :param options: All keyword parameters are passed to the underlying
                :class:`python:configparser.ConfigParser` constructor call.

        """
        options.setdefault('allow_no_value', True)
        options.setdefault('interpolation', configparser.ExtendedInterpolation())
        conf = configparser.ConfigParser(**options)
        conf.read(filename)
        for section in conf.sections():
            for key in conf.options(section):
                value = conf.get(section, key)
                if section not in ('LitPyWeb', 'ROOT'):
                    key = section + '.' + key
                self[key.lower()] = value
        return self

    def load_dict(self, source, namespace=''):
        """ Load values from a dictionary structure. Nesting can be used to
            represent namespaces.

            >>> c = ConfigDict()
            >>> c.load_dict({'some': {'namespace': {'key': 'value'} } })
            {'some.namespace.key': 'value'}
        """
        for key, value in source.items():
            if isinstance(key, str):
                nskey = (namespace + '.' + key).strip('.')
                if isinstance(value, dict):
                    self.load_dict(value, namespace=nskey)
                else:
                    self[nskey] = value
            else:
                raise TypeError('Key has type %r (not a string)' % type(key))
        return self

    def update(self, *a, **ka):
        """ If the first parameter is a string, all keys are prefixed with this
            namespace. Apart from that it works just as the usual dict.update().

            >>> c = ConfigDict()
            >>> c.update('some.namespace', key='value')
        """
        prefix = ''
        if a and isinstance(a[0], str):
            prefix = a[0].strip('.') + '.'
            a = a[1:]
        for key, value in dict(*a, **ka).items():
            self[prefix + key] = value

    def setdefault(self, key, value=None):
        if key not in self:
            self[key] = value
        return self[key]

    def __setitem__(self, key, value):
        if not isinstance(key, str):
            raise TypeError('Key has type %r (not a string)' % type(key))

        self._virtual_keys.discard(key)

        value = self.meta_get(key, 'filter', lambda x: x)(value)
        if key in self and self[key] is value:
            return

        self._on_change(key, value)
        dict.__setitem__(self, key, value)

        for overlay in self._iter_overlays():
            overlay._set_virtual(key, value)

    def __delitem__(self, key):
        if key not in self:
            raise KeyError(key)
        if key in self._virtual_keys:
            raise KeyError("Virtual keys cannot be deleted: %s" % key)

        if self._source and key in self._source:
            # Not virtual, but present in source -> Restore virtual value
            dict.__delitem__(self, key)
            self._set_virtual(key, self._source[key])
        else:  # not virtual, not present in source. This is OUR value
            self._on_change(key, None)
            dict.__delitem__(self, key)
            for overlay in self._iter_overlays():
                overlay._delete_virtual(key)

    def _set_virtual(self, key, value):
        """ Recursively set or update virtual keys. """
        if key in self and key not in self._virtual_keys:
            return  # Do nothing for non-virtual keys.

        self._virtual_keys.add(key)
        if key in self and self[key] is not value:
            self._on_change(key, value)
        dict.__setitem__(self, key, value)
        for overlay in self._iter_overlays():
            overlay._set_virtual(key, value)

    def _delete_virtual(self, key):
        """ Recursively delete virtual entry. """
        if key not in self._virtual_keys:
            return  # Do nothing for non-virtual keys.

        if key in self:
            self._on_change(key, None)
        dict.__delitem__(self, key)
        self._virtual_keys.discard(key)
        for overlay in self._iter_overlays():
            overlay._delete_virtual(key)

    def _on_change(self, key, value):
        for cb in self._change_listener:
            if cb(self, key, value):
                return True

    def _add_change_listener(self, func):
        self._change_listener.append(func)
        return func

    def meta_get(self, key, metafield, default=None):
        """ Return the value of a meta field for a key. """
        return self._meta.get(key, {}).get(metafield, default)

    def meta_set(self, key, metafield, value):
        """ Set the meta field for a key to a new value.
        
            Meta-fields are shared between all members of an overlay tree.
        """
        self._meta.setdefault(key, {})[metafield] = value

    def meta_list(self, key):
        """ Return an iterable of meta field names defined for a key. """
        return self._meta.get(key, {}).keys()

    def _define(self, key, default=_UNSET, help=_UNSET, validate=_UNSET):
        """ (Unstable) Shortcut for plugins to define own config parameters. """
        if default is not _UNSET:
            self.setdefault(key, default)
        if help is not _UNSET:
            self.meta_set(key, 'help', help)
        if validate is not _UNSET:
            self.meta_set(key, 'validate', validate)

    def _iter_overlays(self):
        for ref in self._overlays:
            overlay = ref()
            if overlay is not None:
                yield overlay

    def _make_overlay(self):
        """ (Unstable) Create a new overlay that acts like a chained map: Values
            missing in the overlay are copied from the source map. Both maps
            share the same meta entries.

            Entries that were copied from the source are called 'virtual'. You
            can not delete virtual keys, but overwrite them, which turns them
            into non-virtual entries. Setting keys on an overlay never affects
            its source, but may affect any number of child overlays.

            Other than collections.ChainMap or most other implementations, this
            approach does not resolve missing keys on demand, but instead
            actively copies all values from the source to the overlay and keeps
            track of virtual and non-virtual keys internally. This removes any
            lookup-overhead. Read-access is as fast as a build-in dict for both
            virtual and non-virtual keys.

            Changes are propagated recursively and depth-first. A failing
            on-change handler in an overlay stops the propagation of virtual
            values and may result in an partly updated tree. Take extra care
            here and make sure that on-change handlers never fail.

            Used by Route.config
        """
        # Cleanup dead references
        self._overlays[:] = [ref for ref in self._overlays if ref() is not None]

        overlay = ConfigDict()
        overlay._meta = self._meta
        overlay._source = self
        self._overlays.append(weakref.ref(overlay))
        for key in self:
            overlay._set_virtual(key, self[key])
        return overlay


class ResourceManager:
    """ This class manages a list of search paths and helps to find and open
        application-bound resources (files).

        :param base: default value for :meth:`add_path` calls.
        :param opener: callable used to open resources.
        :param cachemode: controls which lookups are cached. One of 'all',
                         'found' or 'none'.
    """

    def __init__(self, base='./', opener=open, cachemode='all'):
        self.opener = opener
        self.base = base
        self.cachemode = cachemode

        #: A list of search paths. See :meth:`add_path` for details.
        self.path = []
        #: A cache for resolved paths. ``res.cache.clear()`` clears the cache.
        self.cache = {}

    def add_path(self, path, base=None, index=None, create=False):
        """ Add a new path to the list of search paths. Return False if the
            path does not exist.

            :param path: The new search path. Relative paths are turned into
                an absolute and normalized form. If the path looks like a file
                (not ending in `/`), the filename is stripped off.
            :param base: Path used to absolutize relative search paths.
                Defaults to :attr:`base` which defaults to ``os.getcwd()``.
            :param index: Position within the list of search paths. Defaults
                to last index (appends to the list).

            The `base` parameter makes it easy to reference files installed
            along with a python module or package::

                res.add_path('./resources/', __file__)
        """
        base = os.path.abspath(os.path.dirname(base or self.base))
        path = os.path.abspath(os.path.join(base, os.path.dirname(path)))
        path += os.sep
        if path in self.path:
            self.path.remove(path)
        if create and not os.path.isdir(path):
            os.makedirs(path)
        if index is None:
            self.path.append(path)
        else:
            self.path.insert(index, path)
        self.cache.clear()
        return os.path.exists(path)

    def __iter__(self):
        """ Iterate over all existing files in all registered paths. """
        search = self.path[:]
        while search:
            path = search.pop()
            if not os.path.isdir(path): continue
            for name in os.listdir(path):
                full = os.path.join(path, name)
                if os.path.isdir(full): search.append(full)
                else: yield full

    def lookup(self, name):
        """ Search for a resource and return an absolute file path, or `None`.

            The :attr:`path` list is searched in order. The first match is
            returned. Symlinks are followed. The result is cached to speed up
            future lookups. """
        if name not in self.cache or DEBUG:
            for path in self.path:
                fpath = os.path.join(path, name)
                if os.path.isfile(fpath):
                    if self.cachemode in ('all', 'found'):
                        self.cache[name] = fpath
                    return fpath
            if self.cachemode == 'all':
                self.cache[name] = None
        return self.cache[name]

    def open(self, name, mode='r', *args, **kwargs):
        """ Find a resource and return a file object, or raise IOError. """
        fname = self.lookup(name)
        if not fname: raise IOError("Resource %r not found." % name)
        return self.opener(fname, mode=mode, *args, **kwargs)
    
class AppStack(list):
    """ A stack-like list. Calling it returns the head of the stack. """

    def __call__(self):
        """ Return the current default application. """
        return self.default

    def push(self, value=None):
        """ Add a new :class:`LitPyWeb` instance to the stack """
        if not isinstance(value, LitPyWeb):
            value = LitPyWeb()
        self.append(value)
        return value
    new_app = push

    @property
    def default(self):
        try:
            return self[-1]
        except IndexError:
            return self.push()


class WSGIFileWrapper:
    def __init__(self, fp, buffer_size=1024 * 64):
        self.fp, self.buffer_size = fp, buffer_size
        for attr in 'fileno', 'close', 'read', 'readlines', 'tell', 'seek':
            if hasattr(fp, attr): setattr(self, attr, getattr(fp, attr))

    def __iter__(self):
        buff, read = self.buffer_size, self.read
        part = read(buff)
        while part:
            yield part
            part = read(buff)


class _closeiter:
    """ This only exists to be able to attach a .close method to iterators that
        do not support attribute assignment (most of itertools). """

    def __init__(self, iterator, close=None):
        self.iterator = iterator
        self.close_callbacks = makelist(close)

    def __iter__(self):
        return iter(self.iterator)

    def close(self):
        for func in self.close_callbacks:
            func()

class FileUpload:
    def __init__(self, fileobj, name, filename, headers=None):
        """ Wrapper for a single file uploaded via ``multipart/form-data``. """
        #: Open file(-like) object (BytesIO buffer or temporary file)
        self.file = fileobj
        #: Name of the upload form field
        self.name = name
        #: Raw filename as sent by the client (may contain unsafe characters)
        self.raw_filename = filename
        #: A :class:`HeaderDict` with additional headers (e.g. content-type)
        self.headers = HeaderDict(headers) if headers else HeaderDict()

    content_type = HeaderProperty('Content-Type')
    content_length = HeaderProperty('Content-Length', reader=int, default=-1)

    def get_header(self, name, default=None):
        """ Return the value of a header within the multipart part. """
        return self.headers.get(name, default)

    @cached_property
    def filename(self):
        """ Name of the file on the client file system, but normalized to ensure
            file system compatibility. An empty filename is returned as 'empty'.

            Only ASCII letters, digits, dashes, underscores and dots are
            allowed in the final filename. Accents are removed, if possible.
            Whitespace is replaced by a single dash. Leading or tailing dots
            or dashes are removed. The filename is limited to 255 characters.
        """
        fname = self.raw_filename
        fname = normalize('NFKD', fname)
        fname = fname.encode('ASCII', 'ignore').decode('ASCII')
        fname = os.path.basename(fname.replace('\\', os.path.sep))
        fname = re.sub(r'[^a-zA-Z0-9-_.\s]', '', fname).strip()
        fname = re.sub(r'[-\s]+', '-', fname).strip('.-')
        return fname[:255] or 'empty'

    def _copy_file(self, fp, chunk_size=2 ** 16):
        read, write, offset = self.file.read, fp.write, self.file.tell()
        while 1:
            buf = read(chunk_size)
            if not buf: break
            write(buf)
        self.file.seek(offset)

    def save(self, destination, overwrite=False, chunk_size=2 ** 16):
        """ Save file to disk or copy its content to an open file(-like) object.
            If *destination* is a directory, :attr:`filename` is added to the
            path. Existing files are not overwritten by default (IOError).

            :param destination: File path, directory or file(-like) object.
            :param overwrite: If True, replace existing files. (default: False)
            :param chunk_size: Bytes to read at a time. (default: 64kb)
        """
        if isinstance(destination, str):  # Except file-likes here
            if os.path.isdir(destination):
                destination = os.path.join(destination, self.filename)
            if not overwrite and os.path.exists(destination):
                raise IOError('File exists.')
            with open(destination, 'wb') as fp:
                self._copy_file(fp, chunk_size)
        else:
            self._copy_file(destination, chunk_size)

class MultipartError(HTTPError):
    def __init__(self, msg):
        HTTPError.__init__(self, 400, "MultipartError: " + msg)


class _MultipartParser:
    def __init__(
        self,
        stream,
        boundary,
        content_length=-1,
        disk_limit=2 ** 30,
        mem_limit=2 ** 20,
        memfile_limit=2 ** 18,
        buffer_size=2 ** 16,
        charset="latin1",
    ):
        self.stream = stream
        self.boundary = boundary
        self.content_length = content_length
        self.disk_limit = disk_limit
        self.memfile_limit = memfile_limit
        self.mem_limit = min(mem_limit, self.disk_limit)
        self.buffer_size = min(buffer_size, self.mem_limit)
        self.charset = charset

        if not boundary:
            raise MultipartError("No boundary.")

        if self.buffer_size - 6 < len(boundary):  # "--boundary--\r\n"
            raise MultipartError("Boundary does not fit into buffer_size.")

    def _lineiter(self):
        """ Iterate over a binary file-like object (crlf terminated) line by
            line. Each line is returned as a (line, crlf) tuple. Lines larger
            than buffer_size are split into chunks where all but the last chunk
            has an empty string instead of crlf. Maximum chunk size is twice the
            buffer size.
        """

        read = self.stream.read
        maxread, maxbuf = self.content_length, self.buffer_size
        partial = b""  # Contains the last (partial) line

        while True:
            chunk = read(maxbuf if maxread < 0 else min(maxbuf, maxread))
            maxread -= len(chunk)
            if not chunk:
                if partial:
                    yield partial, b''
                break

            if partial:
                chunk = partial + chunk

            scanpos = 0
            while True:
                i = chunk.find(b'\r\n', scanpos)
                if i >= 0:
                    yield chunk[scanpos:i], b'\r\n'
                    scanpos = i + 2
                else: # CRLF not found
                    partial = chunk[scanpos:] if scanpos else chunk
                    break

            if len(partial) > maxbuf:
                yield partial[:-1], b""
                partial = partial[-1:]

    def parse(self):
        """ Return a MultiPart iterator. Can only be called once. """

        lines, line = self._lineiter(), ""
        separator = b"--" + tob(self.boundary)
        terminator = separator + b"--"
        mem_used, disk_used = 0, 0  # Track used resources to prevent DoS
        is_tail = False  # True if the last line was incomplete (cutted)

        # Consume first boundary. Ignore any preamble, as required by RFC
        # 2046, section 5.1.1.
        for line, nl in lines:
            if line in (separator, terminator):
                break
        else:
            raise MultipartError("Stream does not contain boundary")

        # First line is termainating boundary -> empty multipart stream
        if line == terminator:
            for _ in lines:
                raise MultipartError("Found data after empty multipart stream")
            return

        part_options = {
            "buffer_size": self.buffer_size,
            "memfile_limit": self.memfile_limit,
            "charset": self.charset,
        }
        part = _MultipartPart(**part_options)

        for line, nl in lines:
            if not is_tail and (line == separator or line == terminator):
                part.finish()
                if part.is_buffered():
                    mem_used += part.size
                else:
                    disk_used += part.size
                yield part
                if line == terminator:
                    break
                part = _MultipartPart(**part_options)
            else:
                is_tail = not nl  # The next line continues this one
                try:
                    part.feed(line, nl)
                    if part.is_buffered():
                        if part.size + mem_used > self.mem_limit:
                            raise MultipartError("Memory limit reached.")
                    elif part.size + disk_used > self.disk_limit:
                        raise MultipartError("Disk limit reached.")
                except MultipartError:
                    part.close()
                    raise
        else:
            part.close()

        if line != terminator:
            raise MultipartError("Unexpected end of multipart stream.")


class _MultipartPart:
    def __init__(self, buffer_size=2 ** 16, memfile_limit=2 ** 18, charset="latin1"):
        self.headerlist = []
        self.headers = None
        self.file = False
        self.size = 0
        self._buf = b""
        self.disposition = None
        self.name = None
        self.filename = None
        self.content_type = None
        self.charset = charset
        self.memfile_limit = memfile_limit
        self.buffer_size = buffer_size

    def feed(self, line, nl=""):
        if self.file:
            return self.write_body(line, nl)
        return self.write_header(line, nl)

    def write_header(self, line, nl):
        line = str(line, self.charset)

        if not nl:
            raise MultipartError("Unexpected end of line in header.")

        if not line.strip():  # blank line -> end of header segment
            self.finish_header()
        elif line[0] in " \t" and self.headerlist:
            name, value = self.headerlist.pop()
            self.headerlist.append((name, value + line.strip()))
        else:
            if ":" not in line:
                raise MultipartError("Syntax error in header: No colon.")

            name, value = line.split(":", 1)
            self.headerlist.append((name.strip(), value.strip()))

    def write_body(self, line, nl):
        if not line and not nl:
            return  # This does not even flush the buffer

        self.size += len(line) + len(self._buf)
        self.file.write(self._buf + line)
        self._buf = nl

        if self.content_length > 0 and self.size > self.content_length:
            raise MultipartError("Size of body exceeds Content-Length header.")

        if self.size > self.memfile_limit and isinstance(self.file, BytesIO):
            self.file, old = NamedTemporaryFile(mode="w+b"), self.file
            old.seek(0)

            copied, maxcopy, chunksize = 0, self.size, self.buffer_size
            read, write = old.read, self.file.write
            while copied < maxcopy:
                chunk = read(min(chunksize, maxcopy - copied))
                write(chunk)
                copied += len(chunk)

    def finish_header(self):
        self.file = BytesIO()
        self.headers = HeaderDict(self.headerlist)
        content_disposition = self.headers.get("Content-Disposition")
        content_type = self.headers.get("Content-Type")

        if not content_disposition:
            raise MultipartError("Content-Disposition header is missing.")

        self.disposition, self.options = _parse_http_header(content_disposition)[0]
        self.name = self.options.get("name")
        if "filename" in self.options:
            self.filename = self.options.get("filename")
            if self.filename[1:3] == ":\\" or self.filename[:2] == "\\\\":
                self.filename = self.filename.split("\\")[-1] # ie6 bug

        self.content_type, options = _parse_http_header(content_type)[0] if content_type else (None, {})
        self.charset = options.get("charset") or self.charset

        self.content_length = int(self.headers.get("Content-Length", "-1"))

    def finish(self):
        if not self.file:
            raise MultipartError("Incomplete part: Header section not closed.")
        self.file.seek(0)

    def is_buffered(self):
        """ Return true if the data is fully buffered in memory."""
        return isinstance(self.file, BytesIO)

    @property
    def value(self):
        """ Data decoded with the specified charset """
        return str(self.raw, self.charset)

    @property
    def raw(self):
        """ Data without decoding """
        pos = self.file.tell()
        self.file.seek(0)

        try:
            return self.file.read()
        finally:
            self.file.seek(pos)

    def close(self):
        if self.file:
            self.file.close()
            self.file = False
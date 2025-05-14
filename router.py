import re
import itertools
import threading
import weakref
import mimetypes
from datetime import date as datedate
from datetime import datetime, timedelta
from tempfile import NamedTemporaryFile
from io import BytesIO
import hashlib
import email.utils

from .utils import tob, touni, json_dumps, json_lds, _lscmp, _wsgi_recode
from .exceptions import LitPyWebException, RouteBuildError, RouteSyntaxError


class RouteError(LitPyWebException):
    """ This is a base class for all routing related exceptions """


class RouterUnknownModeError(RouteError):
    pass


class Router:
    """ A Router is an ordered collection of route->target pairs. It is used to
        efficiently match WSGI requests against a number of routes and return
        the first target that satisfies the request. The target may be anything,
        usually a string, ID or callable object. A route consists of a path-rule
        and a HTTP method.

        The path-rule is either a static path (e.g. `/contact`) or a dynamic
        path that contains wildcards (e.g. `/wiki/<page>`). The wildcard syntax
        and details on the matching order are described in docs:`routing`.
    """

    default_pattern = '[^/]+'
    default_filter = 're'

    #: The current CPython regexp implementation does not allow more
    #: than 99 matching groups per regular expression.
    _MAX_GROUPS_PER_PATTERN = 99

    def __init__(self, strict=False):
        self.rules = []  # All rules in order
        self._groups = {}  # index of regexes to find them in dyna_routes
        self.builder = {}  # Data structure for the url builder
        self.static = {}  # Search structure for static routes
        self.dyna_routes = {}
        self.dyna_regexes = {}  # Search structure for dynamic routes
        #: If true, static routes are no longer checked first.
        self.strict_order = strict
        self.filters = {
            're': lambda conf: (_re_flatten(conf or self.default_pattern),
                                None, None),
            'int': lambda conf: (r'-?\d+', int, lambda x: str(int(x))),
            'float': lambda conf: (r'-?[\d.]+', float, lambda x: str(float(x))),
            'path': lambda conf: (r'.+?', None, None)
        }

    def add_filter(self, name, func):
        """ Add a filter. The provided function is called with the configuration
        string as parameter and must return a (regexp, to_python, to_url) tuple.
        The first element is a string, the last two are callables or None. """
        self.filters[name] = func

    rule_syntax = re.compile('(\\\\*)'
        '(?:(?::([a-zA-Z_][a-zA-Z_0-9]*)?()(?:#(.*?)#)?)'
          '|(?:<([a-zA-Z_][a-zA-Z_0-9]*)?(?::([a-zA-Z_]*)'
            '(?::((?:\\\\.|[^\\\\>])+)?)?)?>))')

    def _itertokens(self, rule):
        offset, prefix = 0, ''
        for match in self.rule_syntax.finditer(rule):
            prefix += rule[offset:match.start()]
            g = match.groups()
            if g[2] is not None:
                depr(0, 13, "Use of old route syntax.",
                            "Use <name> instead of :name in routes.",
                            stacklevel=4)
            if len(g[0]) % 2:  # Escaped wildcard
                prefix += match.group(0)[len(g[0]):]
                offset = match.end()
                continue
            if prefix:
                yield prefix, None, None
            name, mode, conf = g[4:7] if g[2] is None else g[1:4]
            yield name, mode or 'default', conf or None
            offset, prefix = match.end(), ''
        if offset <= len(rule) or prefix:
            yield prefix + rule[offset:], None, None

    def add(self, rule, method, target, name=None):
        """ Add a new rule or replace the target for an existing rule. """
        anons = 0  # Number of anonymous wildcards found
        keys = []  # Names of keys
        pattern = ''  # Regular expression pattern with named groups
        filters = []  # Lists of wildcard input filters
        builder = []  # Data structure for the URL builder
        is_static = True

        for key, mode, conf in self._itertokens(rule):
            if mode:
                is_static = False
                if mode == 'default': mode = self.default_filter
                mask, in_filter, out_filter = self.filters[mode](conf)
                if not key:
                    pattern += '(?:%s)' % mask
                    key = 'anon%d' % anons
                    anons += 1
                else:
                    pattern += '(?P<%s>%s)' % (key, mask)
                    keys.append(key)
                if in_filter: filters.append((key, in_filter))
                builder.append((key, out_filter or str))
            elif key:
                pattern += re.escape(key)
                builder.append((None, key))

        self.builder[rule] = builder
        if name: self.builder[name] = builder

        if is_static and not self.strict_order:
            self.static.setdefault(method, {})
            self.static[method][self.build(rule)] = (target, None)
            return

        try:
            re_pattern = re.compile('^(%s)$' % pattern)
            re_match = re_pattern.match
        except re.error as e:
            raise RouteSyntaxError("Could not add Route: %s (%s)" % (rule, e))

        if filters:

            def getargs(path):
                url_args = re_match(path).groupdict()
                for name, wildcard_filter in filters:
                    try:
                        url_args[name] = wildcard_filter(url_args[name])
                    except ValueError:
                        raise HTTPError(400, 'Path has wrong format.')
                return url_args
        elif re_pattern.groupindex:

            def getargs(path):
                return re_match(path).groupdict()
        else:
            getargs = None

        flatpat = _re_flatten(pattern)
        whole_rule = (rule, flatpat, target, getargs)

        if (flatpat, method) in self._groups:
            if DEBUG:
                msg = 'Route <%s %s> overwrites a previously defined route'
                warnings.warn(msg % (method, rule), RuntimeWarning, stacklevel=3)
            self.dyna_routes[method][
                self._groups[flatpat, method]] = whole_rule
        else:
            self.dyna_routes.setdefault(method, []).append(whole_rule)
            self._groups[flatpat, method] = len(self.dyna_routes[method]) - 1

        self._compile(method)

    def _compile(self, method):
        all_rules = self.dyna_routes[method]
        comborules = self.dyna_regexes[method] = []
        maxgroups = self._MAX_GROUPS_PER_PATTERN
        for x in range(0, len(all_rules), maxgroups):
            some = all_rules[x:x + maxgroups]
            combined = (flatpat for (_, flatpat, _, _) in some)
            combined = '|'.join('(^%s$)' % flatpat for flatpat in combined)
            combined = re.compile(combined).match
            rules = [(target, getargs) for (_, _, target, getargs) in some]
            comborules.append((combined, rules))

    def build(self, _name, *anons, **query):
        """ Build an URL by filling the wildcards in a rule. """
        builder = self.builder.get(_name)
        if not builder:
            raise RouteBuildError("No route with that name.", _name)
        try:
            for i, value in enumerate(anons):
                query['anon%d' % i] = value
            url = ''.join([f(query.pop(n)) if n else f for (n, f) in builder])
            return url if not query else url + '?' + urlencode(query, doseq=True)
        except KeyError as E:
            raise RouteBuildError('Missing URL argument: %r' % E.args[0])

    def match(self, environ):
        """ Return a (target, url_args) tuple or raise HTTPError(400/404/405). """
        verb = environ['REQUEST_METHOD'].upper()
        path = environ['PATH_INFO'] or '/'

        methods = ('PROXY', 'HEAD', 'GET', 'ANY') if verb == 'HEAD' else ('PROXY', verb, 'ANY')

        for method in methods:
            if method in self.static and path in self.static[method]:
                target, getargs = self.static[method][path]
                return target, getargs(path) if getargs else {}
            elif method in self.dyna_regexes:
                for combined, rules in self.dyna_regexes[method]:
                    match = combined(path)
                    if match:
                        target, getargs = rules[match.lastindex - 1]
                        return target, getargs(path) if getargs else {}

        # No matching route found. Collect alternative methods for 405 response
        allowed = set([])
        nocheck = set(methods)
        for method in set(self.static) - nocheck:
            if path in self.static[method]:
                allowed.add(method)
        for method in set(self.dyna_regexes) - allowed - nocheck:
            for combined, rules in self.dyna_regexes[method]:
                match = combined(path)
                if match:
                    allowed.add(method)
        if allowed:
            allow_header = ",".join(sorted(allowed))
            raise HTTPError(405, "Method not allowed.", Allow=allow_header)

        # No matching route and no alternative method found. We give up
        raise HTTPError(404, "Not found: " + repr(path))


class Route:
    """ This class wraps a route callback along with route specific metadata and
        configuration and applies Plugins on demand. It is also responsible for
        turning an URL path rule into a regular expression usable by the Router.
    """

    def __init__(self, app, rule, method, callback,
                 name=None,
                 plugins=None,
                 skiplist=None, **config):
        #: The application this route is installed to.
        self.app = app
        #: The path-rule string (e.g. ``/wiki/<page>``).
        self.rule = rule
        #: The HTTP method as a string (e.g. ``GET``).
        self.method = method
        #: The original callback with no plugins applied. Useful for introspection.
        self.callback = callback
        #: The name of the route (if specified) or ``None``.
        self.name = name or None
        #: A list of route-specific plugins (see :meth:`LitPyWeb.route`).
        self.plugins = plugins or []
        #: A list of plugins to not apply to this route (see :meth:`LitPyWeb.route`).
        self.skiplist = skiplist or []
        #: Additional keyword arguments passed to the :meth:`LitPyWeb.route`
        #: decorator are stored in this dictionary. Used for route-specific
        #: plugin configuration and meta-data.
        self.config = app.config._make_overlay()
        self.config.load_dict(config)

    @cached_property
    def call(self):
        """ The route callback with all plugins applied. This property is
            created on demand and then cached to speed up subsequent requests."""
        return self._make_callback()

    def reset(self):
        """ Forget any cached values. The next time :attr:`call` is accessed,
            all plugins are re-applied. """
        self.__dict__.pop('call', None)

    def prepare(self):
        """ Do all on-demand work immediately (useful for debugging)."""
        self.call

    def all_plugins(self):
        """ Yield all Plugins affecting this route. """
        unique = set()
        for p in reversed(self.app.plugins + self.plugins):
            if True in self.skiplist: break
            name = getattr(p, 'name', False)
            if name and (name in self.skiplist or name in unique): continue
            if p in self.skiplist or type(p) in self.skiplist: continue
            if name: unique.add(name)
            yield p

    def _make_callback(self):
        callback = self.callback
        for plugin in self.all_plugins():
            if hasattr(plugin, 'apply'):
                callback = plugin.apply(callback, self)
            else:
                callback = plugin(callback)
            if callback is not self.callback:
                update_wrapper(callback, self.callback)
        return callback

    def get_undecorated_callback(self):
        """ Return the callback. If the callback is a decorated function, try to
            recover the original function. """
        func = self.callback
        func = getattr(func, '__func__', func)
        while hasattr(func, '__closure__') and getattr(func, '__closure__'):
            attributes = getattr(func, '__closure__')
            func = attributes[0].cell_contents

            # in case of decorators with multiple arguments
            if not isinstance(func, FunctionType):
                # pick first FunctionType instance from multiple arguments
                func = filter(lambda x: isinstance(x, FunctionType),
                              map(lambda x: x.cell_contents, attributes))
                func = list(func)[0]  # py3 support
        return func

    def get_callback_args(self):
        """ Return a list of argument names the callback (most likely) accepts
            as keyword arguments. If the callback is a decorated function, try
            to recover the original function before inspection. """
        sig = inspect.signature(self.get_undecorated_callback())
        return [p.name for p in sig.parameters.values() if p.kind in (
            p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY
        )]

    def get_config(self, key, default=None):
        """ Lookup a config field and return its value, first checking the
            route.config, then route.app.config."""
        depr(0, 13, "Route.get_config() is deprecated.",
                    "The Route.config property already includes values from the"
                    " application config for missing keys. Access it directly.")
        return self.config.get(key, default)

    def __repr__(self):
        cb = self.get_undecorated_callback()
        return '<%s %s -> %s:%s>' % (self.method, self.rule, cb.__module__, cb.__name__)
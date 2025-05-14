import sys
import os
import time
import tempfile
import mimetypes
from datetime import date as datedate
from datetime import datetime, timedelta
from urllib.parse import urljoin, SplitResult, quote
from io import BytesIO
import hashlib
import email.utils
import functools
import threading
import weakref
import json
import pickle

from .router import Router, Route, RouteError, RouteBuildError, RouteSyntaxError
from .request_response import Request, Response, LocalRequest, LocalResponse
from .app import LitPyWebLitPyWeb, LitPyWebException, HTTPError, HTTPResponse
from .main import *


class ServerAdapter:
    quiet = False

    def __init__(self, host='127.0.0.1', port=8080, **options):
        self.options = options
        self.host = host
        self.port = int(port)

    def run(self, handler):  # pragma: no cover
        pass

    @property
    def _listen_url(self):
        if self.host.startswith("unix:"):
            return self.host
        elif ':' in self.host:
            return "http://[%s]:%d/" % (self.host, self.port)
        else:
            return "http://%s:%d/" % (self.host, self.port)

    def __repr__(self):
        args = ', '.join('%s=%r' % kv for kv in self.options.items())
        return "%s(%s)" % (self.__class__.__name__, args)


class CGIServer(ServerAdapter):
    quiet = True

    def run(self, handler):  # pragma: no cover
        from wsgiref.handlers import CGIHandler

        def fixed_environ(environ, start_response):
            environ.setdefault('PATH_INFO', '')
            return handler(environ, start_response)

        CGIHandler().run(fixed_environ)


class FlupFCGIServer(ServerAdapter):
    def run(self, handler):  # pragma: no cover
        import flup.server.fcgi
        self.options.setdefault('bindAddress', (self.host, self.port))
        flup.server.fcgi.WSGIServer(handler, **self.options).run()


class WSGIRefServer(ServerAdapter):
    def run(self, app):  # pragma: no cover
        from wsgiref.simple_server import make_server
        from wsgiref.simple_server import WSGIRequestHandler, WSGIServer
        import socket

        class FixedHandler(WSGIRequestHandler):
            def log_message(other, format, *args):
                if not self.quiet:
                    return WSGIRequestHandler.log_message(other, format, *args)

        handler_cls = self.options.get('handler_class', FixedHandler)
        server_cls = self.options.get('server_class', WSGIServer)

        if ':' in self.host:  # Fix wsgiref for IPv6 addresses.
            if getattr(server_cls, 'address_family') == socket.AF_INET:

                class server_cls(server_cls):
                    address_family = socket.AF_INET6

        self.srv = make_server(self.host, self.port, app, server_cls,
                               handler_cls)
        self.port = self.srv.server_port  # update port actual port (0 means random)
        try:
            self.srv.serve_forever()
        except KeyboardInterrupt:
            self.srv.server_close()  # Prevent ResourceWarning: unclosed socket
            raise


class CherryPyServer(ServerAdapter):
    def run(self, handler):  # pragma: no cover
        depr(0, 13, "The wsgi server part of cherrypy was split into a new "
                    "project called 'cheroot'.", "Use the 'cheroot' server "
                    "adapter instead of cherrypy.")
        from cherrypy import wsgiserver  # This will fail for CherryPy >= 9

        self.options['bind_addr'] = (self.host, self.port)
        self.options['wsgi_app'] = handler

        certfile = self.options.get('certfile')
        if certfile:
            del self.options['certfile']
        keyfile = self.options.get('keyfile')
        if keyfile:
            del self.options['keyfile']

        server = wsgiserver.CherryPyWSGIServer(**self.options)
        if certfile and keyfile:
            server.ssl_certificate = certfile
        if keyfile:
            server.ssl_private_key = keyfile

        try:
            server.start()
        finally:
            server.stop()


class CherootServer(ServerAdapter):
    def run(self, handler):  # pragma: no cover
        from cheroot import wsgi
        from cheroot.ssl import builtin
        self.options['bind_addr'] = (self.host, self.port)
        self.options['wsgi_app'] = handler
        certfile = self.options.pop('certfile', None)
        keyfile = self.options.pop('keyfile', None)
        chainfile = self.options.pop('chainfile', None)
        server = wsgi.Server(**self.options)
        if certfile and keyfile:
            server.ssl_adapter = builtin.BuiltinSSLAdapter(
                    certfile, keyfile, chainfile)
        try:
            server.start()
        finally:
            server.stop()


class WaitressServer(ServerAdapter):
    def run(self, handler):
        from waitress import serve
        serve(handler, host=self.host, port=self.port, _quiet=self.quiet, **self.options)


class PasteServer(ServerAdapter):
    def run(self, handler):  # pragma: no cover
        from paste import httpserver
        from paste.translogger import TransLogger
        handler = TransLogger(handler, setup_console_handler=(not self.quiet))
        httpserver.serve(handler,
                         host=self.host,
                         port=str(self.port), **self.options)


class MeinheldServer(ServerAdapter):
    def run(self, handler):
        from meinheld import server
        server.listen((self.host, self.port))
        server.run(handler)


class FapwsServer(ServerAdapter):
    """ Extremely fast webserver using libev. See https://github.com/william-os4y/fapws3 """

    def run(self, handler):  # pragma: no cover
        depr(0, 13, "fapws3 is not tested or supported and will be removed.")
        import fapws._evwsgi as evwsgi
        from fapws import base, config
        port = self.port
        if float(config.SERVER_IDENT[-2:]) > 0.4:
            port = str(port)
        evwsgi.start(self.host, port)
        # fapws3 never releases the GIL. Complain upstream. I tried. No luck.
        if 'LitPyWeb_CHILD' in os.environ and not self.quiet:
            _stderr("WARNING: Auto-reloading does not work with Fapws3.")
            _stderr("         (Fapws3 breaks python thread support)")
        evwsgi.wsgi_cb(('', handler))
        evwsgi.run()


class TornadoServer(ServerAdapter):
    """ The super hyped asynchronous server by facebook. Untested. """

    def run(self, handler):  # pragma: no cover
        import tornado.wsgi, tornado.httpserver, tornado.ioloop
        container = tornado.wsgi.WSGIContainer(handler)
        server = tornado.httpserver.HTTPServer(container)
        server.listen(port=self.port, address=self.host)
        tornado.ioloop.IOLoop.instance().start()


class AppEngineServer(ServerAdapter):
    """ Adapter for Google App Engine. """
    quiet = True

    def run(self, handler):
        depr(0, 13, "AppEngineServer no longer required",
             "Configure your application directly in your app.yaml")
        from google.appengine.ext.webapp import util
        # A main() function in the handler script enables 'App Caching'.
        # Lets makes sure it is there. This _really_ improves performance.
        module = sys.modules.get('__main__')
        if module and not hasattr(module, 'main'):
            module.main = lambda: util.run_wsgi_app(handler)
        util.run_wsgi_app(handler)


class TwistedServer(ServerAdapter):
    """ Untested. """

    def run(self, handler):  # pragma: no cover
        from twisted.web import server, wsgi
        from twisted.python.threadpool import ThreadPool
        from twisted.internet import reactor
        thread_pool = ThreadPool()
        thread_pool.start()
        reactor.addSystemEventTrigger('after', 'shutdown', thread_pool.stop)
        factory = server.Site(wsgi.WSGIResource(reactor, thread_pool, handler))
        reactor.listenTCP(self.port, factory)
        if not reactor.running:
            reactor.run()


class DieselServer(ServerAdapter):
    """ Untested. """

    def run(self, handler):  # pragma: no cover
        depr(0, 13, "Diesel is not tested or supported and will be removed.")
        from diesel.protocols.wsgi import WSGIApplication
        app = WSGIApplication(handler, port=self.port)
        app.run()


class GeventServer(ServerAdapter):
    """ Untested. Options:

        * See gevent.wsgi.WSGIServer() documentation for more options.
    """

    def run(self, handler):
        from gevent import pywsgi, local
        if not isinstance(threading.local(), local.local):
            msg = "LitPyWeb requires gevent.monkey.patch_all() (before import)"
            raise RuntimeError(msg)
        self.options['log'] = sys.stderr if not self.quiet else None
        address = (self.host, self.port)
        server = pywsgi.WSGIServer(address, handler, **self.options)
        if 'LitPyWeb_CHILD' in os.environ:
            import signal
            signal.signal(signal.SIGINT, lambda s, f: server.stop())
        server.serve_forever()


class GunicornServer(ServerAdapter):
    """ Untested. See http://gunicorn.org/configure.html for options. """

    def run(self, handler):
        from gunicorn.app.base import BaseApplication

        if self.host.startswith("unix:"):
            config = {'bind': self.host}
        else:
            config = {'bind': "%s:%d" % (self.host, self.port)}

        config.update(self.options)

        class GunicornApplication(BaseApplication):
            def load_config(self):
                for key, value in config.items():
                    self.cfg.set(key, value)

            def load(self):
                return handler

        GunicornApplication().run()


class EventletServer(ServerAdapter):
    """ Untested. Options:

        * `backlog` adjust the eventlet backlog parameter which is the maximum
          number of queued connections. Should be at least 1; the maximum
          value is system-dependent.
        * `family`: (default is 2) socket family, optional. See socket
          documentation for available families.
    """

    def run(self, handler):
        from eventlet import wsgi, listen, patcher
        if not patcher.is_monkey_patched(os):
            msg = "LitPyWeb requires eventlet.monkey_patch() (before import)"
            raise RuntimeError(msg)
        socket_args = {}
        for arg in ('backlog', 'family'):
            try:
                socket_args[arg] = self.options.pop(arg)
            except KeyError:
                pass
        address = (self.host, self.port)
        try:
            wsgi.server(listen(address, **socket_args), handler,
                        log_output=(not self.quiet))
        except TypeError:
            # Fallback, if we have old version of eventlet
            wsgi.server(listen(address), handler)


class BjoernServer(ServerAdapter):
    """ Fast server written in C: https://github.com/jonashaag/bjoern """

    def run(self, handler):
        from bjoern import run
        run(handler, self.host, self.port)

class AsyncioServerAdapter(ServerAdapter):
    """ Extend ServerAdapter for adding custom event loop """
    def get_event_loop(self):
        pass

class AiohttpServer(AsyncioServerAdapter):
    """ Asynchronous HTTP client/server framework for asyncio
        https://pypi.python.org/pypi/aiohttp/
        https://pypi.org/project/aiohttp-wsgi/
    """

    def get_event_loop(self):
        import asyncio
        return asyncio.new_event_loop()

    def run(self, handler):
        import asyncio
        from aiohttp_wsgi.wsgi import serve
        self.loop = self.get_event_loop()
        asyncio.set_event_loop(self.loop)

        if 'LitPyWeb_CHILD' in os.environ:
            import signal
            signal.signal(signal.SIGINT, lambda s, f: self.loop.stop())

        serve(handler, host=self.host, port=self.port)


class AiohttpUVLoopServer(AiohttpServer):
    """uvloop
       https://github.com/MagicStack/uvloop
    """
    def get_event_loop(self):
        import uvloop
        return uvloop.new_event_loop()

class AutoServer(ServerAdapter):
    """ Untested. """
    adapters = [WaitressServer, PasteServer, TwistedServer, CherryPyServer,
                CherootServer, WSGIRefServer]

    def run(self, handler):
        for sa in self.adapters:
            try:
                return sa(self.host, self.port, **self.options).run(handler)
            except ImportError:
                pass

server_names = {
    'cgi': CGIServer,
    'flup': FlupFCGIServer,
    'wsgiref': WSGIRefServer,
    'waitress': WaitressServer,
    'cherrypy': CherryPyServer,
    'cheroot': CherootServer,
    'paste': PasteServer,
    'fapws3': FapwsServer,
    'tornado': TornadoServer,
    'gae': AppEngineServer,
    'twisted': TwistedServer,
    'diesel': DieselServer,
    'meinheld': MeinheldServer,
    'gunicorn': GunicornServer,
    'eventlet': EventletServer,
    'gevent': GeventServer,
    'bjoern': BjoernServer,
    'aiohttp': AiohttpServer,
    'uvloop': AiohttpUVLoopServer,
    'auto': AutoServer,
}

###############################################################################
# Application Control ##########################################################
###############################################################################


def load(target, **namespace):
    """ Import a module or fetch an object from a module.

        * ``package.module`` returns `module` as a module object.
        * ``pack.mod:name`` returns the module variable `name` from `pack.mod`.
        * ``pack.mod:func()`` calls `pack.mod.func()` and returns the result.

        The last form accepts not only function calls, but any type of
        expression. Keyword arguments passed to this function are available as
        local variables. Example: ``import_string('re:compile(x)', x='[a-z]')``
    """
    module, target = target.split(":", 1) if ':' in target else (target, None)
    if module not in sys.modules: __import__(module)
    if not target: return sys.modules[module]
    if target.isalnum(): return getattr(sys.modules[module], target)
    package_name = module.split('.')[0]
    namespace[package_name] = sys.modules[package_name]
    return eval('%s.%s' % (module, target), namespace)


def load_app(target):
    """ Load a LitPyWeb application from a module and make sure that the import
        does not affect the current default application, but returns a separate
        application object. See :func:`load` for the target parameter. """
    global NORUN
    NORUN, nr_old = True, NORUN
    tmp = default_app.push()  # Create a new "default application"
    try:
        rv = load(target)  # Import the target module
        return rv if callable(rv) else tmp
    finally:
        default_app.remove(tmp)  # Remove the temporary added default application
        NORUN = nr_old


_debug = debug

def run(app=None,
        server='wsgiref',
        host='127.0.0.1',
        port=8080,
        interval=1,
        reloader=False,
        quiet=False,
        plugins=None,
        debug=None,
        config=None, **kargs):
    """ Start a server instance. This method blocks until the server terminates.

        :param app: WSGI application or target string supported by
               :func:`load_app`. (default: :func:`default_app`)
        :param server: Server adapter to use. See :data:`server_names` keys
               for valid names or pass a :class:`ServerAdapter` subclass.
               (default: `wsgiref`)
        :param host: Server address to bind to. Pass ``0.0.0.0`` to listens on
               all interfaces including the external one. (default: 127.0.0.1)
        :param port: Server port to bind to. Values below 1024 require root
               privileges. (default: 8080)
        :param reloader: Start auto-reloading server? (default: False)
        :param interval: Auto-reloader interval in seconds (default: 1)
        :param quiet: Suppress output to stdout and stderr? (default: False)
        :param options: Options passed to the server adapter.
     """
    if NORUN: return
    if reloader and not os.environ.get('LitPyWeb_CHILD'):
        import subprocess
        fd, lockfile = tempfile.mkstemp(prefix='LitPyWeb.', suffix='.lock')
        environ = os.environ.copy()
        environ['LitPyWeb_CHILD'] = 'true'
        environ['LitPyWeb_LOCKFILE'] = lockfile
        args = [sys.executable] + sys.argv
        # If a package was loaded with `python -m`, then `sys.argv` needs to be
        # restored to the original value, or imports might break. See #1336
        if getattr(sys.modules.get('__main__'), '__package__', None):
            args[1:1] = ["-m", sys.modules['__main__'].__package__]

        try:
            os.close(fd)  # We never write to this file
            while os.path.exists(lockfile):
                p = subprocess.Popen(args, env=environ)
                while p.poll() is None:
                    os.utime(lockfile, None)  # Tell child we are still alive
                    time.sleep(interval)
                if p.returncode == 3:  # Child wants to be restarted
                    continue
                sys.exit(p.returncode)
        except KeyboardInterrupt:
            pass
        finally:
            if os.path.exists(lockfile):
                os.unlink(lockfile)
        return

    try:
        if debug is not None: _debug(debug)
        app = app or default_app()
        if isinstance(app, str):
            app = load_app(app)
        if not callable(app):
            raise ValueError("Application is not callable: %r" % app)

        for plugin in plugins or []:
            if isinstance(plugin, str):
                plugin = load(plugin)
            app.install(plugin)

        if config:
            app.config.update(config)

        if server in server_names:
            server = server_names.get(server)
        if isinstance(server, str):
            server = load(server)
        if isinstance(server, type):
            server = server(host=host, port=port, **kargs)
        if not isinstance(server, ServerAdapter):
            raise ValueError("Unknown or unsupported server: %r" % server)

        server.quiet = server.quiet or quiet
        if not server.quiet:
            _stderr("LitPyWeb v%s server starting up (using %s)..." %
                    (__version__, repr(server)))
            _stderr("Listening on %s" % server._listen_url)
            _stderr("Hit Ctrl-C to quit.\n")

        if reloader:
            lockfile = os.environ.get('LitPyWeb_LOCKFILE')
            bgcheck = FileCheckerThread(lockfile, interval)
            with bgcheck:
                server.run(app)
            if bgcheck.status == 'reload':
                sys.exit(3)
        else:
            server.run(app)
    except KeyboardInterrupt:
        pass
    except (SystemExit, MemoryError):
        raise
    except:
        if not reloader: raise
        if not getattr(server, 'quiet', quiet):
            print_exc()
        time.sleep(interval)
        sys.exit(3)


class FileCheckerThread(threading.Thread):
    """ Interrupt main-thread as soon as a changed module file is detected,
        the lockfile gets deleted or gets too old. """

    def __init__(self, lockfile, interval):
        threading.Thread.__init__(self)
        self.daemon = True
        self.lockfile, self.interval = lockfile, interval
        #: Is one of 'reload', 'error' or 'exit'
        self.status = None

    def run(self):
        exists = os.path.exists
        mtime = lambda p: os.stat(p).st_mtime
        files = {}

        for module in list(sys.modules.values()):
            path = getattr(module, '__file__', '') or ''
            if path[-4:] in ('.pyo', '.pyc'): path = path[:-1]
            if path and exists(path): files[path] = mtime(path)

        while not self.status:
            if not exists(self.lockfile)\
            or mtime(self.lockfile) < time.time() - self.interval - 5:
                self.status = 'error'
                thread.interrupt_main()
            for path, lmtime in list(files.items()):
                if not exists(path) or mtime(path) > lmtime:
                    self.status = 'reload'
                    thread.interrupt_main()
                    break
            time.sleep(self.interval)

    def __enter__(self):
        self.start()

    def __exit__(self, exc_type, *_):
        if not self.status: self.status = 'exit'  # silent exit
        self.join()
        return exc_type is not None and issubclass(exc_type, KeyboardInterrupt)


class PluginError(LitPyWebException):
    pass


class JSONPlugin:
    name = 'json'
    api = 2

    def __init__(self, json_dumps=json_dumps):
        self.json_dumps = json_dumps

    def setup(self, app):
        app.config._define('json.enable', default=True, validate=bool,
                          help="Enable or disable automatic dict->json filter.")
        app.config._define('json.ascii', default=False, validate=bool,
                          help="Use only 7-bit ASCII characters in output.")
        app.config._define('json.indent', default=True, validate=bool,
                          help="Add whitespace to make json more readable.")
        app.config._define('json.dump_func', default=None,
                          help="If defined, use this function to transform"
                               " dict into json. The other options no longer"
                               " apply.")

    def apply(self, callback, route):
        dumps = self.json_dumps
        if not self.json_dumps: return callback

        @functools.wraps(callback)
        def wrapper(*a, **ka):
            try:
                rv = callback(*a, **ka)
            except HTTPResponse as resp:
                rv = resp

            if isinstance(rv, dict):
                # Attempt to serialize, raises exception on failure
                json_response = dumps(rv)
                # Set content type only if serialization successful
                response.content_type = 'application/json'
                return json_response
            elif isinstance(rv, HTTPResponse) and isinstance(rv.body, dict):
                rv.body = dumps(rv.body)
                rv.content_type = 'application/json'
            return rv

        return wrapper


class TemplatePlugin:
    """ This plugin applies the :func:`view` decorator to all routes with a
        `template` config parameter. If the parameter is a tuple, the second
        element must be a dict with additional options (e.g. `template_engine`)
        or default variables for the template. """
    name = 'template'
    api = 2

    def setup(self, app):
        app.tpl = self

    def apply(self, callback, route):
        conf = route.config.get('template')
        if isinstance(conf, (tuple, list)) and len(conf) == 2:
            return view(conf[0], **conf[1])(callback)
        elif isinstance(conf, str):
            return view(conf)(callback)
        else:
            return callback
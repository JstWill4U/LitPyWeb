from ..utils.cli import depr, _stderr
import sys, threading, os

###############################################################################
# Server Adapter ###############################################################
###############################################################################

class ServerAdapter:
    """ 所有服务器适配器的基类，定义统一接口与配置方式 """
    quiet = False

    def __init__(self, host='127.0.0.1', port=8080, **options):
        self.options = options
        self.host = host
        self.port = int(port)

    def run(self):  # 子类需重写该方法
        pass

    @property
    def _listen_url(self):
        """ 返回监听地址的字符串形式，支持 IPv6 或 Unix 套接字 """
        if self.host.startswith("unix:"):
            return self.host
        elif ':' in self.host:
            return "http://[%s]:%d/" % (self.host, self.port)
        else:
            return "http://%s:%d/" % (self.host, self.port)

    def __repr__(self):
        """ 返回适配器对象的字符串表示形式 """
        args = ', '.join('%s=%r' % kv for kv in self.options.items())
        return "%s(%s)" % (self.__class__.__name__, args)


class CGIServer(ServerAdapter):
    """ 基于标准库 wsgiref 的 CGI 服务器，适用于传统部署环境 """
    quiet = True

    def run(self, handler):
        from wsgiref.handlers import CGIHandler

        def fixed_environ(environ, start_response):
            environ.setdefault('PATH_INFO', '')
            return handler(environ, start_response)

        CGIHandler().run(fixed_environ)


class FlupFCGIServer(ServerAdapter):
    """ 使用 flup 库提供 FastCGI 支持的适配器 """
    def run(self, handler):
        import flup.server.fcgi
        self.options.setdefault('bindAddress', (self.host, self.port))
        flup.server.fcgi.WSGIServer(handler, **self.options).run()


class WSGIRefServer(ServerAdapter):
    """ Python 标准库中的 WSGI 测试服务器，适合开发调试 """
    def run(self, app):
        from wsgiref.simple_server import make_server
        from wsgiref.simple_server import WSGIRequestHandler, WSGIServer
        import socket

        class FixedHandler(WSGIRequestHandler):
            def log_message(other, format, *args):
                if not self.quiet:
                    return WSGIRequestHandler.log_message(other, format, *args)

        handler_cls = self.options.get('handler_class', FixedHandler)
        server_cls = self.options.get('server_class', WSGIServer)

        if ':' in self.host:# IPv6 支持补丁
            if getattr(server_cls, 'address_family') == socket.AF_INET:

                class server_cls(server_cls):
                    address_family = socket.AF_INET6

        self.srv = make_server(self.host, self.port, app, server_cls,
                               handler_cls)
        self.port = self.srv.server_port  # 若为 0 则更新为实际端口
        try:
            self.srv.serve_forever()
        except KeyboardInterrupt:
            self.srv.server_close()
            raise


class CherryPyServer(ServerAdapter):
    """ 旧版 CherryPy 内置的 WSGI 服务器（>=v9 已移除）"""
    def run(self, handler):
        depr(0, 13, "The wsgi server part of cherrypy was split into a new "
                    "project called 'cheroot'.", "Use the 'cheroot' server "
                    "adapter instead of cherrypy.")
        from cherrypy import wsgiserver # This will fail for CherryPy >= 9

        self.options['bind_addr'] = (self.host, self.port)
        self.options['wsgi_app'] = handler

        certfile = self.options.get('certfile')
        if certfile:
            del self.options['certfile']
        keyfile = self.options.get('keyfile')
        if keyfile:
            del self.options['keyfile']

        server = wsgiserver.CherryPyWSGIServer(**self.options)
        if certfile:
            server.ssl_certificate = certfile
        if keyfile:
            server.ssl_private_key = keyfile

        try:
            server.start()
        finally:
            server.stop()


class CherootServer(ServerAdapter):
    """ Cheroot 是 CherryPy 拆分出的专门 WSGI 服务组件，支持 SSL """
    def run(self, handler):
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
    """ Pyramid 推荐的生产级服务器：waitress """
    def run(self, handler):
        from waitress import serve
        serve(handler, host=self.host, port=self.port, _quiet=self.quiet, **self.options)


class PasteServer(ServerAdapter):
    """ 使用 Paste 框架提供的 HTTP 服务，支持日志记录。"""
    def run(self, handler):  # pragma: no cover
        from paste import httpserver
        from paste.translogger import TransLogger
        handler = TransLogger(handler, setup_console_handler=(not self.quiet))
        httpserver.serve(handler,
                         host=self.host,
                         port=str(self.port), **self.options)


class MeinheldServer(ServerAdapter):
    """ 高性能的 C 编写的 WSGI 服务器 Meinheld。"""
    def run(self, handler):
        from meinheld import server
        server.listen((self.host, self.port))
        server.run(handler)


class FapwsServer(ServerAdapter):
    """ 超高性能 Web 服务器，基于 libev"""

    def run(self, handler):  # pragma: no cover
        depr(0, 13, "fapws3 is not maintained and support will be dropped.")
        import fapws._evwsgi as evwsgi
        from fapws import base, config
        port = self.port
        if float(config.SERVER_IDENT[-2:]) > 0.4:
            # fapws3 silently changed its API in 0.5
            port = str(port)
        evwsgi.start(self.host, port)
        # fapws3 never releases the GIL. Complain upstream. I tried. No luck.
        if 'LitPyWeb_CHILD' in os.environ and not self.quiet:
            _stderr("WARNING: Auto-reloading does not work with Fapws3.")
            _stderr("         (Fapws3 breaks python thread support)")
        evwsgi.set_base_module(base)

        def app(environ, start_response):
            environ['wsgi.multiprocess'] = False
            return handler(environ, start_response)

        evwsgi.wsgi_cb(('', app))
        evwsgi.run()


class TornadoServer(ServerAdapter):
    """ Tornado 异步框架服务器，适用于高并发场景。 """

    def run(self, handler):  # pragma: no cover
        import tornado.wsgi, tornado.httpserver, tornado.ioloop
        container = tornado.wsgi.WSGIContainer(handler)
        server = tornado.httpserver.HTTPServer(container)
        server.listen(port=self.port, address=self.host)
        tornado.ioloop.IOLoop.instance().start()


class AppEngineServer(ServerAdapter):
    """ Google App Engine 专用适配器 """
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
    """ Twisted 框架的 WSGI 适配器。 """

    def run(self, handler):
        from twisted.web import server, wsgi
        from twisted.python.threadpool import ThreadPool
        from twisted.internet import reactor
        thread_pool = ThreadPool()
        thread_pool.start()
        reactor.addSystemEventTrigger('after', 'shutdown', thread_pool.stop)
        factory = server.Site(wsgi.WSGIResource(reactor, thread_pool, handler))
        reactor.listenTCP(self.port, factory, interface=self.host)
        if not reactor.running:
            reactor.run()


class DieselServer(ServerAdapter):
    """ Diesel 异步框架支持 """

    def run(self, handler):
        depr(0, 13, "Diesel is not tested or supported and will be removed.")
        from diesel.protocols.wsgi import WSGIApplication
        app = WSGIApplication(handler, port=self.port)
        app.run()


class GeventServer(ServerAdapter):
    """ Gevent 异步框架支持，支持协程式并发。
    """

    def run(self, handler):
        from gevent import pywsgi, local
        if not isinstance(threading.local(), local.local):
            msg = "LitPyWeb requires gevent.monkey.patch_all() (before import)"
            raise RuntimeError(msg)
        if self.quiet:
            self.options['log'] = None
        address = (self.host, self.port)
        server = pywsgi.WSGIServer(address, handler, **self.options)
        if 'LitPyWeb_CHILD' in os.environ:
            import signal
            signal.signal(signal.SIGINT, lambda s, f: server.stop())
        server.serve_forever()


class GunicornServer(ServerAdapter):
    """ Gunicorn 多进程 WSGI 服务器，适用于生产环境。. """

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
    """ Eventlet 异步服务器。需 monkey_patch 支持协程式并发。
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
    """ 超高性能 C 实现的 WSGI 服务器 """

    def run(self, handler):
        from bjoern import run
        run(handler, self.host, self.port, reuse_port=True)

class AsyncioServerAdapter(ServerAdapter):
    """ 基于 asyncio 的服务器基类，可重写 get_event_loop 实现自定义循环。 """
    def get_event_loop(self):
        pass

class AiohttpServer(AsyncioServerAdapter):
    """ aiohttp + aiohttp-wsgi 支持异步 HTTP 服务
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
    """aiohttp + uvloop 组合，极致性能
    """
    def get_event_loop(self):
        import uvloop
        return uvloop.new_event_loop()

class AutoServer(ServerAdapter):
    """ 自动选择可用服务器的适配器（默认优先级顺序） """
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
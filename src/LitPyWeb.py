import functools, itertools, sys
from ..utils.cli import lazy_attribute, ConfigDict, default_app, depr, tob, DictProperty, cached_property, request, _raise, DEBUG, makelist, response
from ..utils.utilities import ResourceManager, _try_close, WSGIFileWrapper, _closeiter
from .routing import Router, Route
from .plugin import JSONPlugin, TemplatePlugin
from .wsgi import HTTPResponse, HTTPError
from .server import _wsgi_recode
from .control import run, load
from urllib.parse import urljoin
from utils.core_utils import yieldroutes, html_escape
from ..utils.cli import ERROR_PAGE_TEMPLATE
from .template import template
from traceback import format_exc


###############################################################################
# 应用对象 ###########################################################
###############################################################################


class LitPyWeb:
    """ 每个 LitPyWeb 实例代表一个独立的 Web 应用，包含路由、回调函数、
        插件、资源和配置。每个实例本质上是一个可调用的 WSGI 应用。

        :param catchall: 若为 True（默认），则捕获所有异常。设置为 False
                         可交由调试中间件处理异常。
    """

    @lazy_attribute
    def _global_config(cls):
        # 创建全局配置对象，并设置 'catchall' 的验证类型为布尔型
        cfg = ConfigDict()
        cfg.meta_set('catchall', 'validate', bool)
        return cfg

    def __init__(self, **kwargs):
        # 应用级配置，使用 ConfigDict 实现
        self.config = self._global_config._make_overlay()
        self.config._add_change_listener(
            functools.partial(self.trigger_hook, 'config'))

        self.config.update({
            "catchall": True
        })

        # 兼容旧参数写法，推荐方式是直接修改 config
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

        self._mounts = [] # 已挂载的子应用

        # 应用文件的资源管理器
        self.resources = ResourceManager()

        self.routes = []  # 已注册的路由列表（Route 实例）
        self.router = Router()  # 路由映射器
        self.error_handler = {} # 错误处理函数

        # 核心插件列表
        self.plugins = []  # 已安装插件列表
        self.install(JSONPlugin())
        self.install(TemplatePlugin())

    # 是否捕获异常的配置项
    catchall = DictProperty('config', 'catchall')

    # 支持的钩子类型
    __hook_names = 'before_request', 'after_request', 'app_reset', 'config'
    __hook_reversed = {'after_request'}

    @cached_property
    def _hooks(self):
        # 初始化钩子字典
        return dict((name, []) for name in self.__hook_names)

    def add_hook(self, name, func):
        """ 添加钩子函数。当前支持以下钩子类型：

            before_request
                每个请求开始前执行一次，尚未进行路由匹配。
            after_request
                每个请求完成后执行一次，无论结果如何。
            app_reset
                调用 reset() 方法时执行。
        """
        if name in self.__hook_reversed:
            self._hooks[name].insert(0, func)
        else:
            self._hooks[name].append(func)

    def remove_hook(self, name, func):
        """ 从钩子中移除回调函数。 """
        if name in self._hooks and func in self._hooks[name]:
            self._hooks[name].remove(func)
            return True

    def trigger_hook(self, __name, *args, **kwargs):
        """ 触发指定钩子，返回所有钩子函数的执行结果列表。 """
        return [hook(*args, **kwargs) for hook in self._hooks[__name][:]]

    def hook(self, name):
        """ 返回一个装饰器，用于注册钩子函数，等价于调用 add_hook 方法。"""

        def decorator(func):
            self.add_hook(name, func)
            return func

        return decorator

    def _mount_wsgi(self, prefix, app, **options):
        # 挂载一个标准的 WSGI 应用到指定路径前缀
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
        # 挂载 LitPyWeb 实例作为子应用
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
        """ 挂载一个应用（LitPyWeb 或标准 WSGI 应用）到指定的 URL 路径前缀。
            示例:

                parent_app.mount('/prefix/', child_app)

            :param prefix: 路径前缀（必须以 '/' 开头且以 '/' 结尾）
            :param app: LitPyWeb 实例或任意 WSGI 应用

            父应用中的插件不会自动应用到子应用中。
            若子应用需要插件支持，请手动安装。
        """

        if not prefix.startswith('/'):
            raise ValueError("Prefix must start with '/'")

        if isinstance(app, LitPyWeb):
            return self._mount_app(prefix, app, **options)
        else:
            return self._mount_wsgi(prefix, app, **options)

    def merge(self, routes):
        """ 合并其他 LitPyWeb 实例的路由或指定的 Route 列表。
            注意：Route 的 app 属性不会改变。 """
        if isinstance(routes, LitPyWeb):
            routes = routes.routes
        for route in routes:
            self.add_route(route)

    def install(self, plugin):
        """ 安装插件并应用到所有路由。
            插件可以是函数装饰器或实现 Plugin 接口的对象。
        """
        if hasattr(plugin, 'setup'): plugin.setup(self)
        if not callable(plugin) and not hasattr(plugin, 'apply'):
            raise TypeError("Plugins must be callable or implement .apply()")
        self.plugins.append(plugin)
        self.reset()
        return plugin

    def uninstall(self, plugin):
        """ 卸载插件。
            可传入插件实例、插件类型、插件名字符串，或传入 True 卸载全部插件。
            返回被卸载插件的列表。 """
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
        """ 重置所有路由（强制重新应用插件）并清除所有缓存。
            如果提供路由 ID 或 Route 对象，仅重置指定路由。 """
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
        """ 关闭应用，并关闭所有已安装的插件。 """
        for plugin in self.plugins:
            if hasattr(plugin, 'close'): plugin.close()

    def run(self, **kwargs):
        """ 启动当前应用，相当于调用外部的 run(self, **kwargs)。 """
        run(self, **kwargs)

    def match(self, environ):
        """ 在当前路由表中查找匹配项。
            返回 (Route对象, URL参数字典) 二元组。
            如果没有匹配项，则抛出 HTTPError（404/405）。"""
        return self.router.match(environ)

    def get_url(self, routename, **kargs):
        """ 返回指定名称路由的 URL 字符串。 """
        scriptname = request.environ.get('SCRIPT_NAME', '').strip('/') + '/'
        location = self.router.build(routename, **kargs).lstrip('/')
        return urljoin(urljoin('/', scriptname), location)

    def add_route(self, route):
        """ 添加一个路由对象，但不修改其 app 属性。"""
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
        """ 将函数绑定到请求路径，可作为装饰器使用。
            示例::

                @app.route('/hello/<name>')
                def hello(name):
                    return 'Hello %s' % name

            :param path: 请求路径，可包含通配符。
            :param method: 支持的 HTTP 方法，如 GET、POST 等。
            :param callback: 回调函数（可选）。
            :param name: 路由名称（可选）。
            :param apply: 附加插件或装饰器。
            :param skip: 要跳过的插件列表。
            :param config: 其他路由配置参数。
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
        """ 等价于 route(path, method='GET', ...)。 """
        return self.route(path, method, **options)

    def post(self, path=None, method='POST', **options):
        """ 等价于 route(path, method='POST', ...)。 """
        return self.route(path, method, **options)

    def put(self, path=None, method='PUT', **options):
        """ 等价于 route(path, method='PUT', ...)。 """
        return self.route(path, method, **options)

    def delete(self, path=None, method='DELETE', **options):
        """ 等价于 route(path, method='DELETE', ...)。 """
        return self.route(path, method, **options)

    def patch(self, path=None, method='PATCH', **options):
        """ 等价于 route(path, method='PATCH', ...)。 """
        return self.route(path, method, **options)

    def error(self, code=500, callback=None):
        """ 注册指定 HTTP 错误码的响应处理函数。
            可用作装饰器或直接调用。
            示例::

                @app.error(404)
                def not_found(err): return 'Not Found'
        """

        def decorator(callback):
            if isinstance(callback, str): callback = load(callback)
            self.error_handler[int(code)] = callback
            return callback

        return decorator(callback) if callback else decorator

    def default_error_handler(self, res):
        """ 默认错误处理函数，返回错误页面 HTML。"""
        return tob(template(ERROR_PAGE_TEMPLATE, e=res, template_settings=dict(name='__ERROR_PAGE_TEMPLATE')))

    def _handle(self, environ):
        """ 处理一次请求，包括路由匹配、回调执行、异常处理及钩子调用。"""
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
        """ 将返回值转换为 WSGI 可识别格式，并设置适当的响应头。
            支持类型包括：bool、bytes、str、dict、HTTPResponse、文件对象、可迭代对象等。
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
        """ LitPyWeb 的 WSGI 接口。 """
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
        """ LitPyWeb 实例本身是一个 WSGI 应用，可直接调用。 """
        return self.wsgi(environ, start_response)

    def __enter__(self):
        """ 将当前应用设为模块级别默认应用（用于上下文管理）。 """
        default_app.push(self)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """ 退出上下文管理器，恢复默认应用。"""
        default_app.pop()

    def __setattr__(self, name, value):
        """ 属性设置拦截：防止重复定义，避免插件冲突。"""
        if name in self.__dict__:
            raise AttributeError("Attribute %s already defined. Plugin conflict?" % name)
        object.__setattr__(self, name, value)
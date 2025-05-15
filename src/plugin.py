from ..utils.exceptions import LitPyWebException
import functools, sys
from .wsgi import HTTPResponse
from ..utils.cli import response
from .template import view
from types import ModuleType as new_module

try:
    from ujson import dumps as json_dumps, loads as json_lds
except ImportError:
    from json import dumps as json_dumps, loads as json_lds

###############################################################################
# 插件系统 ######################################################################
###############################################################################


class PluginError(LitPyWebException):
    """ 插件相关错误类，继承自 LitPyWebException """
    pass


class JSONPlugin:
    """ JSON 插件：将返回值为字典的响应自动转为 JSON 格式。 """
    name = 'json'
    api = 2

    def __init__(self, json_dumps=json_dumps):
        self.json_dumps = json_dumps

    def setup(self, app):
        """ 在应用配置中注册 json 相关选项 """
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

    def apply(self, callback):
        """ 为路由回调函数应用 JSON 格式化功能 """
        dumps = self.json_dumps
        if not self.json_dumps: return callback

        @functools.wraps(callback)
        def wrapper(*a, **ka):
            try:
                rv = callback(*a, **ka)
            except HTTPResponse as resp:
                rv = resp

            if isinstance(rv, dict):
                # 序列化为 JSON（失败则抛异常）
                json_response = dumps(rv)
                # 设置响应头 content-type 为 application/json
                response.content_type = 'application/json'
                return json_response
            elif isinstance(rv, HTTPResponse) and isinstance(rv.body, dict):
                rv.body = dumps(rv.body)
                rv.content_type = 'application/json'
            return rv

        return wrapper


class TemplatePlugin:
    """ 模板插件：为带有 `template` 配置参数的路由自动应用 view 装饰器。
        若 template 参数是元组，则第二项应为模板引擎或默认变量等额外参数。 """
    name = 'template'
    api = 2

    def setup(self, app):
        """ 在应用中挂载模板插件自身 """
        app.tpl = self

    def apply(self, callback, route):
        """ 为带有 template 配置的路由应用 view 装饰器 """
        conf = route.config.get('template')
        if isinstance(conf, (tuple, list)) and len(conf) == 2:
            return view(conf[0], **conf[1])(callback)
        elif isinstance(conf, str):
            return view(conf)(callback)
        else:
            return callback


#: 虽不是插件，但属于插件 API 组件之一。
class _ImportRedirect:
    """ 虚拟模块导入重定向器，支持 PEP 302 格式的模块别名加载机制 """
    def __init__(self, name, impmask):
        """ 创建一个虚拟包，将导入请求重定向到指定模式模块上。 """
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

    def find_spec(self, fullname):
        """ PEP 451 导入查找器实现 """
        if '.' not in fullname: return
        if fullname.rsplit('.', 1)[0] != self.name: return
        from importlib.util import spec_from_loader
        return spec_from_loader(fullname, self)

    def find_module(self, fullname):
        """ 兼容早期 Python 版本的导入查找器接口 """
        if '.' not in fullname: return
        if fullname.rsplit('.', 1)[0] != self.name: return
        return self

    def create_module(self, spec):
        """ 创建模块实例（新语法）"""
        return self.load_module(spec.name)

    def exec_module(self):
        """ 模块执行阶段（此处未实现，可能导致 importlib.reload() 无效）"""
        pass

    def load_module(self, fullname):
        """ 加载并返回模块，同时缓存映射关系 """
        if fullname in sys.modules: return sys.modules[fullname]
        modname = fullname.rsplit('.', 1)[1]
        realname = self.impmask % modname
        __import__(realname)
        module = sys.modules[fullname] = sys.modules[realname]
        setattr(self.module, modname, module)
        module.__loader__ = self
        return module
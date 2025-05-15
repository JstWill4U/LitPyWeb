from ..utils.exceptions import LitPyWebException
import re, warnings, inspect
from ..utils.cli import depr, DEBUG, cached_property, update_wrapper
from .wsgi import HTTPError
from urllib.parse import urlencode
from types import FunctionType

###############################################################################
# 路由系统 ######################################################################
###############################################################################


class RouteError(LitPyWebException):
    """ 路由相关异常的基类 """

class RouterUnknownModeError(RouteError):
    """ 未知的路由匹配模式错误 """
    pass


class RouteSyntaxError(RouteError):
    """ 路由解析器发现了不被支持的语法 """


class RouteBuildError(RouteError):
    """ 路由构建失败 """


def _re_flatten(p):
    """ 将正则表达式中所有捕获组转换为非捕获组 """
    if '(' not in p:
        return p
    return re.sub(r'(\\*)(\(\?P<[^>]+>|\((?!\?))', lambda m: m.group(0) if
                  len(m.group(1)) % 2 else m.group(1) + '(?:', p)


class Router:
    """ Router 是一个有序的 路由-目标 配对集合。
        它用于高效匹配 WSGI 请求，找到第一个满足条件的目标。
        目标可以是任意对象（常为函数、字符串等）。
        支持静态路径（如 /home）和动态路径（如 /user/<name>）。
    """

    default_pattern = '[^/]+'
    default_filter = 're'

    _MAX_GROUPS_PER_PATTERN = 99

    def __init__(self, strict=False):
        self.rules = []
        self._groups = {}
        self.builder = {}
        self.static = {}
        self.dyna_routes = {}
        self.dyna_regexes = {}
        self.strict_order = strict
        self.filters = {
            're': lambda conf: (_re_flatten(conf or self.default_pattern),
                                None, None),
            'int': lambda conf: (r'-?\d+', int, lambda x: str(int(x))),
            'float': lambda conf: (r'-?[\d.]+', float, lambda x: str(float(x))),
            'path': lambda conf: (r'.+?', None, None)
        }

    def add_filter(self, name, func):
        """ 添加自定义的过滤器。
            过滤器函数接收配置字符串作为参数，并返回一个三元组：
            (正则表达式，反序列化函数，序列化函数) """
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
            name, filtr, conf = g[4:7] if g[2] is None else g[1:4]
            yield name, filtr or 'default', conf or None
            offset, prefix = match.end(), ''
        if offset <= len(rule) or prefix:
            yield prefix + rule[offset:], None, None

    def add(self, rule, method, target, name=None):
        """ 添加一个路由规则。将路径表达式编译为正则表达式，并建立构建器和参数提取器。 """
        anons = 0
        keys = []
        pattern = ''
        filters = []
        builder = []
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
        """ 根据路由名称和参数构建 URL，可用于在程序中动态生成链接。"""
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
        """ 根据请求环境匹配静态或动态路由。
            返回 (目标对象, 路径参数)。
            若无匹配则抛出 404 或 405 异常。 """
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

        raise HTTPError(404, "Not found: " + repr(path))


class Route:
    """ Route 表示单个路由规则及其元信息和插件机制。
        它负责将路径规则转换为正则表达式，并根据需要应用插件。
    """

    def __init__(self, app, rule, method, callback,
                 name=None,
                 plugins=None,
                 skiplist=None, **config):
        self.app = app
        self.rule = rule
        self.method = method
        self.callback = callback
        self.name = name or None
        self.plugins = plugins or []
        self.skiplist = skiplist or []
        self.config = app.config._make_overlay()
        self.config.load_dict(config)

    @cached_property
    def call(self):
        """ 应用所有插件后的最终回调函数。调用时自动缓存，提升效率。"""
        return self._make_callback()

    def reset(self):
        """ 清除缓存的 call 值，触发插件重新绑定 """
        self.__dict__.pop('call', None)

    def prepare(self):
        """ 提前准备插件绑定（调试时使用）"""
        self.call

    def all_plugins(self):
        """ 返回该路由实际生效的所有插件（过滤 skiplist） """
        unique = set()
        for p in reversed(self.app.plugins + self.plugins):
            if True in self.skiplist: break
            name = getattr(p, 'name', False)
            if name and (name in self.skiplist or name in unique): continue
            if p in self.skiplist or type(p) in self.skiplist: continue
            if name: unique.add(name)
            yield p

    def _make_callback(self):
        """ 应用插件包装后的回调函数生成器 """
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
        """ 尝试还原被装饰器包裹的原始函数 """
        func = self.callback
        func = getattr(func, '__func__', func)
        while hasattr(func, '__closure__') and getattr(func, '__closure__'):
            attributes = getattr(func, '__closure__')
            func = attributes[0].cell_contents

            if not isinstance(func, FunctionType):
                func = filter(lambda x: isinstance(x, FunctionType),
                              map(lambda x: x.cell_contents, attributes))
                func = list(func)[0]
        return func

    def get_callback_args(self):
        """ 推断回调函数接受的关键字参数列表 """
        sig = inspect.signature(self.get_undecorated_callback())
        return [p.name for p in sig.parameters.values() if p.kind in (
            p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY
        )]

    def get_config(self, key, default=None):
        """ 读取配置字段（已过时）"""
        depr(0, 13, "Route.get_config() is deprecated.",
                    "The Route.config property already includes values from the"
                    " application config for missing keys. Access it directly.")
        return self.config.get(key, default)

    def __repr__(self):
        """ 返回格式化的路由信息字符串 """
        cb = self.get_undecorated_callback()
        return '<%s %s -> %s:%s>' % (self.method, self.rule, cb.__module__, cb.__name__)
from..utils.exceptions import LitPyWebException
import os, threading, functools, re
from ..utils.cli import depr, DEBUG, touni, cached_property, TEMPLATES, TEMPLATE_PATH
from ..utils.core_utils import html_escape
from collections.abc import MutableMapping as DictMixin
from .application import abort

###############################################################################
# 模板适配器 ############################################################
###############################################################################


class TemplateError(LitPyWebException):
    pass


class BaseTemplate:
    """ 模板适配器基类，定义所有模板引擎需要实现的统一接口。 """
    extensions = ['tpl', 'html', 'thtml', 'stpl']
    settings = {}  # 存储模板解析器设置
    defaults = {}  # 模板渲染时使用的默认变量

    def __init__(self,
                 source=None,
                 name=None,
                 lookup=None,
                 encoding='utf8', **settings):
        """ 初始化模板实例。
        
        参数说明：
        - source：直接提供的模板源码（字符串或文件对象）；
        - name：模板名称（用于文件查找）；
        - lookup：查找模板的目录列表；
        - encoding：读取文件或源码的编码；
        - settings：模板引擎专用设置。

        如果未指定 source，则会根据 name 从 lookup 中查找模板文件。
        """
        self.name = name
        self.source = source.read() if hasattr(source, 'read') else source
        self.filename = source.filename if hasattr(source, 'filename') else None
        self.lookup = [os.path.abspath(x) for x in lookup] if lookup else []
        self.encoding = encoding
        self.settings = self.settings.copy()  # Copy from class variable
        self.settings.update(settings)  # Apply
        if not self.source and self.name:
            self.filename = self.search(self.name, self.lookup)
            if not self.filename:
                raise TemplateError('Template %s not found.' % repr(name))
        if not self.source and not self.filename:
            raise TemplateError('No template specified.')
        self.prepare(**self.settings)

    @classmethod
    def search(cls, name, lookup=None):
        """ 在 lookup 路径中查找模板文件，支持自动补全扩展名。 """
        if not lookup:
            raise depr(0, 12, "Empty template lookup path.", "Configure a template lookup path.")

        if os.path.isabs(name):
            raise depr(0, 12, "Use of absolute path for template name.",
                       "Refer to templates with names or paths relative to the lookup path.")

        for spath in lookup:
            spath = os.path.abspath(spath) + os.sep
            fname = os.path.abspath(os.path.join(spath, name))
            if not fname.startswith(spath): continue
            if os.path.isfile(fname): return fname
            for ext in cls.extensions:
                if os.path.isfile('%s.%s' % (fname, ext)):
                    return '%s.%s' % (fname, ext)

    @classmethod
    def global_config(cls, key, *args):
        """ 读取或设置全局模板引擎配置（类级别） """
        if args:
            cls.settings = cls.settings.copy()  # Make settings local to class
            cls.settings[key] = args[0]
        else:
            return cls.settings[key]

    def prepare(self):
        """ 由子类实现的模板编译、缓存等预处理操作。
        """
        raise NotImplementedError

    def render(self):
        """  由子类实现的模板渲染方法。返回渲染结果字符串。
        """
        raise NotImplementedError


class MakoTemplate(BaseTemplate):
    """ 使用 Mako 模板引擎 """
    def prepare(self, **options):
        from mako.template import Template
        from mako.lookup import TemplateLookup
        options.update({'input_encoding': self.encoding})
        options.setdefault('format_exceptions', bool(DEBUG))
        lookup = TemplateLookup(directories=self.lookup, **options)
        if self.source:
            self.tpl = Template(self.source, lookup=lookup, **options)
        else:
            self.tpl = Template(uri=self.name,
                                filename=self.filename,
                                lookup=lookup, **options)

    def render(self, *args, **kwargs):
        for dictarg in args:
            kwargs.update(dictarg)
        _defaults = self.defaults.copy()
        _defaults.update(kwargs)
        return self.tpl.render(**_defaults)


class CheetahTemplate(BaseTemplate):
    """ 使用 Cheetah 模板引擎（线程隔离的变量绑定） """
    def prepare(self, **options):
        from Cheetah.Template import Template
        self.context = threading.local()
        self.context.vars = {}
        options['searchList'] = [self.context.vars]
        if self.source:
            self.tpl = Template(source=self.source, **options)
        else:
            self.tpl = Template(file=self.filename, **options)

    def render(self, *args, **kwargs):
        for dictarg in args:
            kwargs.update(dictarg)
        self.context.vars.update(self.defaults)
        self.context.vars.update(kwargs)
        out = str(self.tpl)
        self.context.vars.clear()
        return out


class Jinja2Template(BaseTemplate):
    """ 使用 Jinja2 模板引擎 """
    def prepare(self, filters=None, tests=None, globals={}, **kwargs):
        from jinja2 import Environment, FunctionLoader
        self.env = Environment(loader=FunctionLoader(self.loader), **kwargs)
        if filters: self.env.filters.update(filters)
        if tests: self.env.tests.update(tests)
        if globals: self.env.globals.update(globals)
        if self.source:
            self.tpl = self.env.from_string(self.source)
        else:
            self.tpl = self.env.get_template(self.name)

    def render(self, *args, **kwargs):
        for dictarg in args:
            kwargs.update(dictarg)
        _defaults = self.defaults.copy()
        _defaults.update(kwargs)
        return self.tpl.render(**_defaults)

    def loader(self, name):
        if name == self.filename:
            fname = name
        else:
            fname = self.search(name, self.lookup)
        if not fname: return
        with open(fname, "rb") as f:
            return (f.read().decode(self.encoding), fname, lambda: False)


class SimpleTemplate(BaseTemplate):
    """ 内置简易模板引擎，支持 include、rebase、表达式插值等 """
    def prepare(self,
                escape_func=html_escape,
                noescape=False,
                syntax=None, **ka):
        self.cache = {}
        enc = self.encoding
        self._str = lambda x: touni(x, enc)
        self._escape = lambda x: escape_func(touni(x, enc))
        self.syntax = syntax
        if noescape:
            self._str, self._escape = self._escape, self._str

    @cached_property
    def co(self):
        return compile(self.code, self.filename or '<string>', 'exec')

    @cached_property
    def code(self):
        source = self.source
        if not source:
            with open(self.filename, 'rb') as f:
                source = f.read()
        try:
            source, encoding = touni(source), 'utf8'
        except UnicodeError:
            raise depr(0, 11, 'Unsupported template encodings.', 'Use utf-8 for templates.')
        parser = StplParser(source, encoding=encoding, syntax=self.syntax)
        code = parser.translate()
        self.encoding = parser.encoding
        return code

    def _rebase(self, _env, _name=None, **kwargs):
        _env['_rebase'] = (_name, kwargs)

    def _include(self, _env, _name=None, **kwargs):
        env = _env.copy()
        env.update(kwargs)
        if _name not in self.cache:
            self.cache[_name] = self.__class__(name=_name, lookup=self.lookup, syntax=self.syntax)
        return self.cache[_name].execute(env['_stdout'], env)

    def execute(self, _stdout, kwargs):
        env = self.defaults.copy()
        env.update(kwargs)
        env.update({
            '_stdout': _stdout,
            '_printlist': _stdout.extend,
            'include': functools.partial(self._include, env),
            'rebase': functools.partial(self._rebase, env),
            '_rebase': None,
            '_str': self._str,
            '_escape': self._escape,
            'get': env.get,
            'setdefault': env.setdefault,
            'defined': env.__contains__
        })
        exec(self.co, env)
        if env.get('_rebase'):
            subtpl, rargs = env.pop('_rebase')
            rargs['base'] = ''.join(_stdout)
            del _stdout[:]
            return self._include(env, subtpl, **rargs)
        return env

    def render(self, *args, **kwargs):
        env = {}
        stdout = []
        for dictarg in args:
            env.update(dictarg)
        env.update(kwargs)
        self.execute(stdout, env)
        return ''.join(stdout)


class StplSyntaxError(TemplateError):
    """ 简单模板语言 STPL 的语法错误类 """
    pass


class StplParser:
    """ STPL 模板的解析器，将模板字符串转换为可执行的 Python 代码 """
    _re_cache = {}  # 编译后的正则表达式缓存

    # Python 字面量和多行字符串匹配
    _re_tok = r'''(
        [urbURB]*
        (?:  ''(?!')
            |""(?!")
            |'{6}
            |"{6}
            |'(?:[^\\']|\\.)+?'
            |"(?:[^\\"]|\\.)+?"
            |'{3}(?:[^\\]|\\.|\n)+?'{3}
            |"{3}(?:[^\\]|\\.|\n)+?"{3}
        )
    )'''

    _re_inl = _re_tok.replace(r'|\n', '')  # We re-use this string pattern later

    _re_tok += r'''
        # 注释、括号、控制结构、end、模板结束符、换行等
        |(\#.*)

        |([\[\{\(])
        |([\]\}\)])

        |^([\ \t]*(?:if|for|while|with|try|def|class)\b)
        |^([\ \t]*(?:elif|else|except|finally)\b)

        |((?:^|;)[\ \t]*end[\ \t]*(?=(?:%(block_close)s[\ \t]*)?\r?$|;|\#))

        |(%(block_close)s[\ \t]*(?=\r?$))

        |(\r?\n)
    '''

    _re_split = r'''(?m)^[ \t]*(\\?)((%(line_start)s)|(%(block_start)s))'''
    _re_inl = r'''%%(inline_start)s((?:%s|[^'"\n])*?)%%(inline_end)s''' % _re_inl
    _re_tok = '(?mx)' + _re_tok
    _re_inl = '(?mx)' + _re_inl


    default_syntax = '<% %> % {{ }}'

    def __init__(self, source, syntax=None, encoding='utf8'):
        self.source, self.encoding = touni(source, encoding), encoding
        self.set_syntax(syntax or self.default_syntax)
        self.code_buffer, self.text_buffer = [], []
        self.lineno, self.offset = 1, 0
        self.indent, self.indent_mod = 0, 0
        self.paren_depth = 0

    def get_syntax(self):
        return self._syntax

    def set_syntax(self, syntax):
        """ 设置模板语法：默认 '<% %> % {{ }}' """
        self._syntax = syntax
        self._tokens = syntax.split()
        if syntax not in self._re_cache:
            names = 'block_start block_close line_start inline_start inline_end'
            etokens = map(re.escape, self._tokens)
            pattern_vars = dict(zip(names.split(), etokens))
            patterns = (self._re_split, self._re_tok, self._re_inl)
            patterns = [re.compile(p % pattern_vars) for p in patterns]
            self._re_cache[syntax] = patterns
        self.re_split, self.re_tok, self.re_inl = self._re_cache[syntax]

    syntax = property(get_syntax, set_syntax)

    def translate(self):
        """ 将模板转换为可执行的 Python 源代码 """
        if self.offset: raise RuntimeError('Parser is a one time instance.')
        while True:
            m = self.re_split.search(self.source, pos=self.offset)
            if m:
                text = self.source[self.offset:m.start()]
                self.text_buffer.append(text)
                self.offset = m.end()
                if m.group(1):  # Escape syntax
                    line, sep, _ = self.source[self.offset:].partition('\n')
                    self.text_buffer.append(self.source[m.start():m.start(1)] +
                                            m.group(2) + line + sep)
                    self.offset += len(line + sep)
                    continue
                self.flush_text()
                self.offset += self.read_code(self.source[self.offset:],
                                              multiline=bool(m.group(4)))
            else:
                break
        self.text_buffer.append(self.source[self.offset:])
        self.flush_text()
        return ''.join(self.code_buffer)

    def read_code(self, pysource, multiline):
        code_line, comment = '', ''
        offset = 0
        while True:
            m = self.re_tok.search(pysource, pos=offset)
            if not m:
                code_line += pysource[offset:]
                offset = len(pysource)
                self.write_code(code_line.strip(), comment)
                break
            code_line += pysource[offset:m.start()]
            offset = m.end()
            _str, _com, _po, _pc, _blk1, _blk2, _end, _cend, _nl = m.groups()
            if self.paren_depth > 0 and (_blk1 or _blk2):  # a if b else c
                code_line += _blk1 or _blk2
                continue
            if _str:  # Python string
                code_line += _str
            elif _com:  # Python comment (up to EOL)
                comment = _com
                if multiline and _com.strip().endswith(self._tokens[1]):
                    multiline = False  # Allow end-of-block in comments
            elif _po:  # open parenthesis
                self.paren_depth += 1
                code_line += _po
            elif _pc:  # close parenthesis
                if self.paren_depth > 0:
                    # we could check for matching parentheses here, but it's
                    # easier to leave that to python - just check counts
                    self.paren_depth -= 1
                code_line += _pc
            elif _blk1:  # Start-block keyword (if/for/while/def/try/...)
                code_line = _blk1
                self.indent += 1
                self.indent_mod -= 1
            elif _blk2:  # Continue-block keyword (else/elif/except/...)
                code_line = _blk2
                self.indent_mod -= 1
            elif _cend:  # The end-code-block template token (usually '%>')
                if multiline: multiline = False
                else: code_line += _cend
            elif _end:
                self.indent -= 1
                self.indent_mod += 1
            else:  # \n
                self.write_code(code_line.strip(), comment)
                self.lineno += 1
                code_line, comment, self.indent_mod = '', '', 0
                if not multiline:
                    break

        return offset

    def flush_text(self):
        """ 将收集到的纯文本缓冲区转换为 _printlist 输出语句 """
        text = ''.join(self.text_buffer)
        del self.text_buffer[:]
        if not text: return
        parts, pos, nl = [], 0, '\\\n' + '  ' * self.indent
        for m in self.re_inl.finditer(text):
            prefix, pos = text[pos:m.start()], m.end()
            if prefix:
                parts.append(nl.join(map(repr, prefix.splitlines(True))))
            if prefix.endswith('\n'): parts[-1] += nl
            parts.append(self.process_inline(m.group(1).strip()))
        if pos < len(text):
            prefix = text[pos:]
            lines = prefix.splitlines(True)
            if lines[-1].endswith('\\\\\n'): lines[-1] = lines[-1][:-3]
            elif lines[-1].endswith('\\\\\r\n'): lines[-1] = lines[-1][:-4]
            parts.append(nl.join(map(repr, lines)))
        code = '_printlist((%s,))' % ', '.join(parts)
        self.lineno += code.count('\n') + 1
        self.write_code(code)

    @staticmethod
    def process_inline(chunk):
        if chunk[0] == '!': return '_str(%s)' % chunk[1:]
        return '_escape(%s)' % chunk

    def write_code(self, line, comment=''):
        code = '  ' * (self.indent + self.indent_mod)
        code += line.lstrip() + comment + '\n'
        self.code_buffer.append(code)


def template(*args, **kwargs):
    """ 模板渲染入口函数（支持字符串/名称/Template对象） """
    tpl = args[0] if args else None
    for dictarg in args[1:]:
        kwargs.update(dictarg)
    adapter = kwargs.pop('template_adapter', SimpleTemplate)
    lookup = kwargs.pop('template_lookup', TEMPLATE_PATH)
    tplid = (id(lookup), tpl)
    if tplid not in TEMPLATES or DEBUG:
        settings = kwargs.pop('template_settings', {})
        if isinstance(tpl, adapter):
            TEMPLATES[tplid] = tpl
            if settings: TEMPLATES[tplid].prepare(**settings)
        elif "\n" in tpl or "{" in tpl or "%" in tpl or '$' in tpl:
            TEMPLATES[tplid] = adapter(source=tpl, lookup=lookup, **settings)
        else:
            TEMPLATES[tplid] = adapter(name=tpl, lookup=lookup, **settings)
    if not TEMPLATES[tplid]:
        abort(500, 'Template (%s) not found' % tpl)
    return TEMPLATES[tplid].render(kwargs)


mako_template = functools.partial(template, template_adapter=MakoTemplate)
cheetah_template = functools.partial(template,
                                     template_adapter=CheetahTemplate)
jinja2_template = functools.partial(template, template_adapter=Jinja2Template)


def view(tpl_name, **defaults):
    """ 装饰器：自动将返回 dict 的 handler 渲染为模板视图 """

    def decorator(func):

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            if isinstance(result, (dict, DictMixin)):
                tplvars = defaults.copy()
                tplvars.update(result)
                return template(tpl_name, **tplvars)
            elif result is None:
                return template(tpl_name, **defaults)
            return result

        return wrapper

    return decorator


mako_view = functools.partial(view, template_adapter=MakoTemplate)
cheetah_view = functools.partial(view, template_adapter=CheetahTemplate)
jinja2_view = functools.partial(view, template_adapter=Jinja2Template)
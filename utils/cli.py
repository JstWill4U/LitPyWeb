import sys

__author__ = 'Setsuna丶'
__version__ = '1.0-dev'

def _cli_parse(args):
    """
    命令行参数解析函数。
    用于将命令行参数解析为配置选项和 WSGI 应用入口点。
    """
    from argparse import ArgumentParser

    parser = ArgumentParser(prog=args[0], usage="%(prog)s [options] package.module:app")
    opt = parser.add_argument
    opt("--version", action="store_true", help="show version number.")
    opt("-b", "--bind", metavar="ADDRESS", help="bind socket to ADDRESS.")
    opt("-s", "--server", default='wsgiref', help="use SERVER as backend.")
    opt("-p", "--plugin", action="append", help="install additional plugin/s.")
    opt("-c", "--conf", action="append", metavar="FILE",
        help="load config values from FILE.")
    opt("-C", "--param", action="append", metavar="NAME=VALUE",
        help="override config values.")
    opt("--debug", action="store_true", help="start server in debug mode.")
    opt("--reload", action="store_true", help="auto-reload on file changes.")
    opt('app', help='WSGI app entry point.', nargs='?')

    cli_args = parser.parse_args(args[1:])

    return cli_args, parser


def _cli_patch(cli_args):
    """
    根据命令行参数启用协程补丁（如 gevent、eventlet）。
    """
    parsed_args, _ = _cli_parse(cli_args)
    opts = parsed_args
    if opts.server:
        if opts.server.startswith('gevent'):
            import gevent.monkey
            gevent.monkey.patch_all()
        elif opts.server.startswith('eventlet'):
            import eventlet
            eventlet.monkey_patch()


if __name__ == '__main__':
    _cli_patch(sys.argv)

import base64, calendar, email.utils, functools, hmac, itertools, \
    mimetypes, os, re, tempfile, threading, time, warnings, weakref, hashlib

from types import FunctionType
from datetime import date as datedate, datetime, timedelta
from tempfile import NamedTemporaryFile
from traceback import format_exc, print_exc
from unicodedata import normalize

try:
    from ujson import dumps as json_dumps, loads as json_lds
except ImportError:
    from json import dumps as json_dumps, loads as json_lds

py = sys.version_info

import http.client as httplib
import _thread as thread
from urllib.parse import urljoin, SplitResult as UrlSplitResult
from urllib.parse import urlencode, quote as urlquote, unquote as urlunquote
from http.cookies import SimpleCookie, Morsel, CookieError
from collections.abc import MutableMapping as DictMixin
from types import ModuleType as new_module
import pickle
from io import BytesIO
import configparser
from datetime import timezone
UTC = timezone.utc
import inspect

json_loads = lambda s: json_lds(touni(s))
callable = lambda x: hasattr(x, '__call__')

def _wsgi_recode(src):
    """ 将 PEP-3333 的 latin1 编码字符串转换为 utf-8，并处理 surrogateescape。 """
    if src.isascii():
        return src
    return src.encode('latin1').decode('utf8', 'surrogateescape')


def _raise(*a):
    """ 模拟 Python 3 的 raise exc.with_traceback(tb)。"""
    raise a[0](a[1]).with_traceback(a[2])

def tob(s, enc='utf8'):
    """ 将字符串转换为字节串。None -> b''。"""
    if isinstance(s, str):
        return s.encode(enc)
    return b'' if s is None else bytes(s)


def touni(s, enc='utf8', err='strict'):
    """ 将字节串转换为字符串。None -> ""。"""
    if isinstance(s, (bytes, bytearray)):
        return str(s, enc, err)
    return "" if s is None else str(s)


def _stderr(*args):
    """ 安全地向标准错误输出，避免某些环境下打印异常（如 mod_wsgi）。"""
    try:
        print(*args, file=sys.stderr)
    except (IOError, AttributeError):
        pass

def update_wrapper(wrapper, wrapped, *a, **ka):
    """ 修复 functools 在实例方法上的 wrapper 错误。"""
    try:
        functools.update_wrapper(wrapper, wrapped, *a, **ka)
    except AttributeError:
        pass

def depr(major, minor, cause, fix, stacklevel=3):
    """ 输出弃用警告，若 DEBUG 模式为 strict 则抛出异常。"""
    text = "Warning: Use of deprecated feature or API. (Deprecated in LitPyWeb-%d.%d)\n"\
           "Cause: %s\n"\
           "Fix: %s\n" % (major, minor, cause, fix)
    if DEBUG == 'strict':
        raise DeprecationWarning(text)
    warnings.warn(text, DeprecationWarning, stacklevel=stacklevel)
    return DeprecationWarning(text)


def makelist(data):
    """ 将任意容器类型转换为列表，None -> []。"""
    if isinstance(data, (tuple, list, set, dict)):
        return list(data)
    elif data:
        return [data]
    else:
        return []


class DictProperty:
    """ 可将某个对象属性映射为其内部 dict 类型属性的一个键值。 """

    def __init__(self, attr, key=None, read_only=False):
        self.attr, self.key, self.read_only = attr, key, read_only

    def __call__(self, func):
        functools.update_wrapper(self, func, updated=[])
        self.getter, self.key = func, self.key or func.__name__
        return self

    def __get__(self, obj, cls):
        if obj is None: return self
        key, storage = self.key, getattr(obj, self.attr)
        if key not in storage: storage[key] = self.getter(obj)
        return storage[key]

    def __set__(self, obj, value):
        if self.read_only: raise AttributeError("Read-Only property.")
        getattr(obj, self.attr)[self.key] = value

    def __delete__(self, obj):
        if self.read_only: raise AttributeError("Read-Only property.")
        del getattr(obj, self.attr)[self.key]


class cached_property:
    """ 只计算一次的属性，第一次访问后将其替换为普通属性。
        删除该属性将会在下次访问时重新计算。 """

    def __init__(self, func):
        update_wrapper(self, func)
        self.func = func

    def __get__(self, obj, cls):
        if obj is None: return self
        value = obj.__dict__[self.func.__name__] = self.func(obj)
        return value


class lazy_attribute:
    """ 加载类属性：首次访问时计算并缓存到类对象上。 """

    def __init__(self, func):
        functools.update_wrapper(self, func, updated=[])
        self.getter = func

    def __get__(self, obj, cls):
        value = self.getter(cls)
        setattr(cls, self.__name__, value)
        return value
    
TEMPLATE_PATH = ['./', './views/']
TEMPLATES = {}
DEBUG = False
NORUN = False

# HTTP 状态码 -> 原因短语映射表
HTTP_CODES = httplib.responses.copy()
HTTP_CODES[418] = "I'm a teapot"  # RFC 2324
HTTP_CODES[428] = "Precondition Required"
HTTP_CODES[429] = "Too Many Requests"
HTTP_CODES[431] = "Request Header Fields Too Large"
HTTP_CODES[451] = "Unavailable For Legal Reasons" # RFC 7725
HTTP_CODES[511] = "Network Authentication Required"
_HTTP_STATUS_LINES = dict((k, '%d %s' % (k, v))
                          for (k, v) in HTTP_CODES.items())

# 错误页面模板（用于显示异常时的 HTML）
ERROR_PAGE_TEMPLATE = """
%%try:
    %%from %s import DEBUG, request
    <!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">
    <html>
        <head>
            <title>Error: {{e.status}}</title>
            <style type="text/css">
              html {background-color: #eee; font-family: sans-serif;}
              body {background-color: #fff; border: 1px solid #ddd;
                    padding: 15px; margin: 15px;}
              pre {background-color: #eee; border: 1px solid #ddd; padding: 5px;}
            </style>
        </head>
        <body>
            <h1>Error: {{e.status}}</h1>
            <p>Sorry, the requested URL <tt>{{repr(request.url)}}</tt>
               caused an error:</p>
            <pre>{{e.body}}</pre>
            %%if DEBUG and e.exception:
              <h2>Exception:</h2>
              %%try:
                %%exc = repr(e.exception)
              %%except:
                %%exc = '<unprintable %%s object>' %% type(e.exception).__name__
              %%end
              <pre>{{exc}}</pre>
            %%end
            %%if DEBUG and e.traceback:
              <h2>Traceback:</h2>
              <pre>{{e.traceback}}</pre>
            %%end
        </body>
    </html>
%%except ImportError:
    <b>ImportError:</b> Could not generate the error page. Please add LitPyWeb to
    the import path.
%%end
""" % __name__

# 当前线程的请求对象（LocalRequest 会自动映射到当前线程的请求上下文）
from ..src.wsgi import LocalRequest
request = LocalRequest()

# 当前线程的响应对象（用于设置响应体和状态码）
from ..src.wsgi import LocalResponse
response = LocalResponse()

# 通用线程本地命名空间（可用于存储线程变量）
local = threading.local()

# 应用栈：支持多应用部署
from .utilities import AppStack
apps = app = default_app = AppStack()

# 虚拟模块重定向机制
from ..src.plugin import _ImportRedirect
from .utilities import ConfigDict
from ..src.control import run
ext = _ImportRedirect('LitPyWeb.ext' if __name__ == '__main__' else
                      __name__ + ".ext", 'LitPyWeb_%s').module


def _main(argv):
    args, parser = _cli_parse(argv)

    def _cli_error(cli_msg):
        parser.print_help()
        _stderr('\nError: %s\n' % cli_msg)
        sys.exit(1)

    if args.version:
        print(__version__)
        sys.exit(0)
    if not args.app:
        _cli_error("No application entry point specified.")

    sys.path.insert(0, '.')
    sys.modules.setdefault('LitPyWeb', sys.modules['__main__'])

    host, port = (args.bind or 'localhost'), 8080
    if ':' in host and host.rfind(']') < host.rfind(':'):
        host, port = host.rsplit(':', 1)
    host = host.strip('[]')

    config = ConfigDict()

    for cfile in args.conf or []:
        try:
            if cfile.endswith('.json'):
                with open(cfile, 'rb') as fp:
                    config.load_dict(json_loads(fp.read()))
            else:
                config.load_config(cfile)
        except configparser.Error as parse_error:
            _cli_error(parse_error)
        except IOError:
            _cli_error("Unable to read config file %r" % cfile)
        except (UnicodeError, TypeError, ValueError) as error:
            _cli_error("Unable to parse config file %r: %s" % (cfile, error))

    for cval in args.param or []:
        if '=' in cval:
            config.update((cval.split('=', 1),))
        else:
            config[cval] = True

    run(args.app,
        host=host,
        port=int(port),
        server=args.server,
        reloader=args.reload,
        plugins=args.plugin,
        debug=args.debug,
        config=config)


def main():
    _main(sys.argv)


if __name__ == '__main__':
    main()

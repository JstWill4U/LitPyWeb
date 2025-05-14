#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LitPyWeb is a fast and simple micro-framework for small web applications. It
offers request dispatching (Routes) with URL parameter support, templates,
a built-in HTTP Server and adapters for many third party WSGI/HTTP-server and
template engines - all in a single file and with no dependencies other than the
Python Standard Library.

Homepage and documentation: http://LitPyWebpy.org/

Copyright (c) 2009-2025, Marcel Hellkamp.
License: MIT (see LICENSE for details)
"""

import sys

__author__ = 'Marcel Hellkamp'
__version__ = '0.14-dev'
__license__ = 'MIT'

###############################################################################
# Command-line interface ######################################################
###############################################################################
# INFO: Some server adapters need to monkey-patch std-lib modules before they
# are imported. This is why some of the command-line handling is done here, but
# the actual call to _main() is at the end of the file.


def _cli_parse(args):  # pragma: no coverage
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


def _cli_patch(cli_args):  # pragma: no coverage
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

###############################################################################
# Imports and Helpers used everywhere else #####################################
###############################################################################

import base64, calendar, email.utils, functools, hmac, itertools, \
    mimetypes, os, re, tempfile, threading, time, warnings, weakref, hashlib

from types import FunctionType
from datetime import date as datedate, datetime, timedelta
from .request_response import Request, Response, LocalRequest, LocalResponse
from .app import AppStack, _ImportRedirect, ConfigDict
from .server_adapter import run
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
    """ Translate a PEP-3333 latin1-string to utf8+surrogateescape """
    if src.isascii():
        return src
    return src.encode('latin1').decode('utf8', 'surrogateescape')


def _raise(*a):
    raise a[0](a[1]).with_traceback(a[2])


# Some helpers for string/byte handling
def tob(s, enc='utf8'):
    if isinstance(s, str):
        return s.encode(enc)
    return b'' if s is None else bytes(s)


def touni(s, enc='utf8', err='strict'):
    if isinstance(s, (bytes, bytearray)):
        return str(s, enc, err)
    return "" if s is None else str(s)


def _stderr(*args):
    try:
        print(*args, file=sys.stderr)
    except (IOError, AttributeError):
        pass # Some environments do not allow printing (mod_wsgi)


# A bug in functools causes it to break if the wrapper is an instance method
def update_wrapper(wrapper, wrapped, *a, **ka):
    try:
        functools.update_wrapper(wrapper, wrapped, *a, **ka)
    except AttributeError:
        pass

# These helpers are used at module level and need to be defined first.
# And yes, I know PEP-8, but sometimes a lower-case classname makes more sense.


def depr(major, minor, cause, fix, stacklevel=3):
    text = "Warning: Use of deprecated feature or API. (Deprecated in LitPyWeb-%d.%d)\n"\
           "Cause: %s\n"\
           "Fix: %s\n" % (major, minor, cause, fix)
    if DEBUG == 'strict':
        raise DeprecationWarning(text)
    warnings.warn(text, DeprecationWarning, stacklevel=stacklevel)
    return DeprecationWarning(text)


def makelist(data):  # This is just too handy
    if isinstance(data, (tuple, list, set, dict)):
        return list(data)
    elif data:
        return [data]
    else:
        return []


class DictProperty:
    """ Property that maps to a key in a local dict-like attribute. """

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
    """ A property that is only computed once per instance and then replaces
        itself with an ordinary attribute. Deleting the attribute resets the
        property. """

    def __init__(self, func):
        update_wrapper(self, func)
        self.func = func

    def __get__(self, obj, cls):
        if obj is None: return self
        value = obj.__dict__[self.func.__name__] = self.func(obj)
        return value


class lazy_attribute:
    """ A property that caches itself to the class object. """

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
NORUN = False  # If set, run() does nothing. Used by load_app()

#: A dict to map HTTP status codes (e.g. 404) to phrases (e.g. 'Not Found')
HTTP_CODES = httplib.responses.copy()
HTTP_CODES[418] = "I'm a teapot"  # RFC 2324
HTTP_CODES[428] = "Precondition Required"
HTTP_CODES[429] = "Too Many Requests"
HTTP_CODES[431] = "Request Header Fields Too Large"
HTTP_CODES[451] = "Unavailable For Legal Reasons" # RFC 7725
HTTP_CODES[511] = "Network Authentication Required"
_HTTP_STATUS_LINES = dict((k, '%d %s' % (k, v))
                          for (k, v) in HTTP_CODES.items())

#: The default template used for error pages. Override with @error()
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

#: A thread-safe instance of :class:`LocalRequest`. If accessed from within a
#: request callback, this instance always refers to the *current* request
#: (even on a multi-threaded server).
request = LocalRequest()

#: A thread-safe instance of :class:`LocalResponse`. It is used to change the
#: HTTP response for the *current* request.
response = LocalResponse()

#: A thread-safe namespace. Not used by LitPyWeb.
local = threading.local()

# Initialize app stack (create first empty LitPyWeb app now deferred until needed)
# BC: 0.6.4 and needed for run()
apps = app = default_app = AppStack()

#: A virtual package that redirects import statements.
#: Example: ``import LitPyWeb.ext.sqlite`` actually imports `LitPyWeb_sqlite`.
ext = _ImportRedirect('LitPyWeb.ext' if __name__ == '__main__' else
                      __name__ + ".ext", 'LitPyWeb_%s').module


def _main(argv):  # pragma: no coverage
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


if __name__ == '__main__':  # pragma: no coverage
    main()
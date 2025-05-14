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

from .utils import tob, touni, json_dumps, json_lds, _lscmp, _wsgi_recode
from .exceptions import LitPyWebException, HTTPError, HTTPResponse, HeaderProperty
from .router import RouterUnknownModeError, RouteBuildError, RouteSyntaxError


class BaseRequest:
    """ A wrapper for WSGI environment dictionaries that adds a lot of
        convenient access methods and properties. Most of them are read-only.

        Adding new attributes to a request actually adds them to the environ
        dictionary (as 'LitPyWeb.request.ext.<name>'). This is the recommended
        way to store and access request-specific data.
    """

    __slots__ = ('environ', )

    #: Maximum size of memory buffer for :attr:`body` in bytes.
    MEMFILE_MAX = 102400

    def __init__(self, environ=None):
        """ Wrap a WSGI environ dictionary. """
        #: The wrapped WSGI environ dictionary. This is the only real attribute.
        #: All other attributes actually are read-only properties.
        self.environ = {} if environ is None else environ
        self.environ['LitPyWeb.request'] = self

    @DictProperty('environ', 'LitPyWeb.app', read_only=True)
    def app(self):
        """ LitPyWeb application handling this request. """
        raise RuntimeError('This request is not connected to an application.')

    @DictProperty('environ', 'LitPyWeb.route', read_only=True)
    def route(self):
        """ The LitPyWeb :class:`Route` object that matches this request. """
        raise RuntimeError('This request is not connected to a route.')

    @DictProperty('environ', 'route.url_args', read_only=True)
    def url_args(self):
        """ The arguments extracted from the URL. """
        raise RuntimeError('This request is not connected to a route.')

    @property
    def path(self):
        """ The value of ``PATH_INFO`` with exactly one prefixed slash (to fix
            broken clients and avoid the "empty path" edge case). """
        return '/' + self.environ.get('PATH_INFO', '').lstrip('/')

    @property
    def method(self):
        """ The ``REQUEST_METHOD`` value as an uppercase string. """
        return self.environ.get('REQUEST_METHOD', 'GET').upper()

    @DictProperty('environ', 'LitPyWeb.request.headers', read_only=True)
    def headers(self):
        """ A :class:`WSGIHeaderDict` that provides case-insensitive access to
            HTTP request headers. """
        return WSGIHeaderDict(self.environ)

    def get_header(self, name, default=None):
        """ Return the value of a request header, or a given default value. """
        return self.headers.get(name, default)

    @DictProperty('environ', 'LitPyWeb.request.cookies', read_only=True)
    def cookies(self):
        """ Cookies parsed into a :class:`FormsDict`. Signed cookies are NOT
            decoded. Use :meth:`get_cookie` if you expect signed cookies. """
        cookie_header = _wsgi_recode(self.environ.get('HTTP_COOKIE', ''))
        cookies = SimpleCookie(cookie_header).values()
        return FormsDict((c.key, c.value) for c in cookies)

    def get_cookie(self, key, default=None, secret=None, digestmod=hashlib.sha256):
        """ Return the content of a cookie. To read a `Signed Cookie`, the
            `secret` must match the one used to create the cookie (see
            :meth:`Response.set_cookie <BaseResponse.set_cookie>`). If anything goes wrong (missing
            cookie or wrong signature), return a default value. """
        value = self.cookies.get(key)
        if secret:
            # See BaseResponse.set_cookie for details on signed cookies.
            if value and value.startswith('!') and '?' in value:
                sig, msg = map(tob, value[1:].split('?', 1))
                hash = hmac.new(tob(secret), msg, digestmod=digestmod).digest()
                if _lscmp(sig, base64.b64encode(hash)):
                    dst = pickle.loads(base64.b64decode(msg))
                    if dst and dst[0] == key:
                        return dst[1]
            return default
        return value or default

    @DictProperty('environ', 'LitPyWeb.request.query', read_only=True)
    def query(self):
        """ The :attr:`query_string` parsed into a :class:`FormsDict`. These
            values are sometimes called "URL arguments" or "GET parameters", but
            not to be confused with "URL wildcards" as they are provided by the
            :class:`Router`. """
        get = self.environ['LitPyWeb.get'] = FormsDict()
        pairs = _parse_qsl(self.environ.get('QUERY_STRING', ''), 'utf8')
        for key, value in pairs:
            get[key] = value
        return get

    @DictProperty('environ', 'LitPyWeb.request.forms', read_only=True)
    def forms(self):
        """ Form values parsed from an `url-encoded` or `multipart/form-data`
            encoded POST or PUT request body. The result is returned as a
            :class:`FormsDict`. All keys and values are strings. File uploads
            are stored separately in :attr:`files`. """
        forms = FormsDict()
        for name, item in self.POST.allitems():
            if not isinstance(item, FileUpload):
                forms[name] = item
        return forms

    @DictProperty('environ', 'LitPyWeb.request.params', read_only=True)
    def params(self):
        """ A :class:`FormsDict` with the combined values of :attr:`query` and
            :attr:`forms`. File uploads are stored in :attr:`files`. """
        params = FormsDict()
        for key, value in self.query.allitems():
            params[key] = value
        for key, value in self.forms.allitems():
            params[key] = value
        return params

    @DictProperty('environ', 'LitPyWeb.request.files', read_only=True)
    def files(self):
        """ File uploads parsed from `multipart/form-data` encoded POST or PUT
            request body. The values are instances of :class:`FileUpload`.

        """
        files = FormsDict()
        for name, item in self.POST.allitems():
            if isinstance(item, FileUpload):
                files[name] = item
        return files

    @DictProperty('environ', 'LitPyWeb.request.json', read_only=True)
    def json(self):
        """ If the ``Content-Type`` header is ``application/json`` or
            ``application/json-rpc``, this property holds the parsed content
            of the request body. Only requests smaller than :attr:`MEMFILE_MAX`
            are processed to avoid memory exhaustion.
            Invalid JSON raises a 400 error response.
        """
        ctype = self.environ.get('CONTENT_TYPE', '').lower().split(';')[0]
        if ctype in ('application/json', 'application/json-rpc'):
            b = self._get_body_string(self.MEMFILE_MAX)
            if not b:
                return None
            try:
                return json_loads(b)
            except (ValueError, TypeError) as err:
                raise HTTPError(400, 'Invalid JSON', exception=err)
        return None

    def _iter_body(self, read, bufsize):
        maxread = max(0, self.content_length)
        while maxread:
            part = read(min(maxread, bufsize))
            if not part: break
            yield part
            maxread -= len(part)

    @staticmethod
    def _iter_chunked(read, bufsize):
        err = HTTPError(400, 'Error while parsing chunked transfer body.')
        rn, sem, bs = b'\r\n', b';', b''
        while True:
            header = read(1)
            while header[-2:] != rn:
                c = read(1)
                header += c
                if not c: raise err
                if len(header) > bufsize: raise err
            size, _, _ = header.partition(sem)
            try:
                maxread = int(size.strip(), 16)
            except ValueError:
                raise err
            if maxread == 0: break
            buff = bs
            while maxread > 0:
                if not buff:
                    buff = read(min(maxread, bufsize))
                part, buff = buff[:maxread], buff[maxread:]
                if not part: raise err
                yield part
                maxread -= len(part)
            if read(2) != rn:
                raise err

    @DictProperty('environ', 'LitPyWeb.request.body', read_only=True)
    def _body(self):
        try:
            read_func = self.environ['wsgi.input'].read
        except KeyError:
            self.environ['wsgi.input'] = BytesIO()
            return self.environ['wsgi.input']
        body_iter = self._iter_chunked if self.chunked else self._iter_body
        body, body_size, is_temp_file = BytesIO(), 0, False
        for part in body_iter(read_func, self.MEMFILE_MAX):
            body.write(part)
            body_size += len(part)
            if not is_temp_file and body_size > self.MEMFILE_MAX:
                body, tmp = NamedTemporaryFile(mode='w+b'), body
                body.write(tmp.getvalue())
                del tmp
                is_temp_file = True
        self.environ['wsgi.input'] = body
        body.seek(0)
        return body

    def _get_body_string(self, maxread):
        """ Read body into a string. Raise HTTPError(413) on requests that are
            too large. """
        if self.content_length > maxread:
            raise HTTPError(413, 'Request entity too large')
        data = self.body.read(maxread + 1)
        if len(data) > maxread:
            raise HTTPError(413, 'Request entity too large')
        return data

    @property
    def body(self):
        """ The HTTP request body as a seek-able file-like object. Depending on
            :attr:`MEMFILE_MAX`, this is either a temporary file or a
            :class:`io.BytesIO` instance. Accessing this property for the first
            time reads and replaces the ``wsgi.input`` environ variable.
            Subsequent accesses just do a `seek(0)` on the file object. """
        self._body.seek(0)
        return self._body

    @property
    def chunked(self):
        """ True if Chunked transfer encoding was. """
        return 'chunked' in self.environ.get(
            'HTTP_TRANSFER_ENCODING', '').lower()

    #: An alias for :attr:`query`.
    GET = query

    @DictProperty('environ', 'LitPyWeb.request.post', read_only=True)
    def POST(self):
        """ The values of :attr:`forms` and :attr:`files` combined into a single
            :class:`FormsDict`. Values are either strings (form values) or
            instances of :class:`FileUpload`.
        """
        post = FormsDict()
        content_type = self.environ.get('CONTENT_TYPE', '')
        content_type, options = _parse_http_header(content_type)[0]
        # We default to application/x-www-form-urlencoded for everything that
        # is not multipart and take the fast path (also: 3.1 workaround)
        if not content_type.startswith('multipart/'):
            body = self._get_body_string(self.MEMFILE_MAX).decode('utf8', 'surrogateescape')
            for key, value in _parse_qsl(body, 'utf8'):
                post[key] = value
            return post

        charset = options.get("charset", "utf8")
        boundary = options.get("boundary")
        if not boundary:
            raise MultipartError("Invalid content type header, missing boundary")
        parser = _MultipartParser(self.body, boundary, self.content_length,
            mem_limit=self.MEMFILE_MAX, memfile_limit=self.MEMFILE_MAX,
            charset=charset)

        for part in parser.parse():
            if not part.filename and part.is_buffered():
                post[part.name] = part.value
            else:
                post[part.name] = FileUpload(part.file, part.name,
                                            part.filename, part.headerlist)

        return post

    @property
    def url(self):
        """ The full request URI including hostname and scheme. If your app
            lives behind a reverse proxy or load balancer and you get confusing
            results, make sure that the ``X-Forwarded-Host`` header is set
            correctly. """
        return self.urlparts.geturl()

    @DictProperty('environ', 'LitPyWeb.request.urlparts', read_only=True)
    def urlparts(self):
        """ The :attr:`url` string as an :class:`urlparse.SplitResult` tuple.
            The tuple contains (scheme, host, path, query_string and fragment),
            but the fragment is always empty because it is not visible to the
            server. """
        env = self.environ
        http = env.get('HTTP_X_FORWARDED_PROTO') \
             or env.get('wsgi.url_scheme', 'http')
        host = env.get('HTTP_X_FORWARDED_HOST') or env.get('HTTP_HOST')
        if not host:
            # HTTP 1.1 requires a Host-header. This is for HTTP/1.0 clients.
            host = env.get('SERVER_NAME', '127.0.0.1')
            port = env.get('SERVER_PORT')
            if port and port != ('80' if http == 'http' else '443'):
                host += ':' + port
        path = urlquote(self.fullpath)
        return UrlSplitResult(http, host, path, env.get('QUERY_STRING'), '')

    @property
    def fullpath(self):
        """ Request path including :attr:`script_name` (if present). """
        return urljoin(self.script_name, self.path.lstrip('/'))

    @property
    def query_string(self):
        """ The raw :attr:`query` part of the URL (everything in between ``?``
            and ``#``) as a string. """
        return self.environ.get('QUERY_STRING', '')

    @property
    def script_name(self):
        """ The initial portion of the URL's `path` that was removed by a higher
            level (server or routing middleware) before the application was
            called. This script path is returned with leading and tailing
            slashes. """
        script_name = self.environ.get('SCRIPT_NAME', '').strip('/')
        return '/' + script_name + '/' if script_name else '/'

    def path_shift(self, shift=1):
        """ Shift path segments from :attr:`path` to :attr:`script_name` and
            vice versa.

           :param shift: The number of path segments to shift. May be negative
                         to change the shift direction. (default: 1)
        """
        script, path = path_shift(self.environ.get('SCRIPT_NAME', '/'), self.path, shift)
        self['SCRIPT_NAME'], self['PATH_INFO'] = script, path

    @property
    def content_length(self):
        """ The request body length as an integer. The client is responsible to
            set this header. Otherwise, the real length of the body is unknown
            and -1 is returned. In this case, :attr:`body` will be empty. """
        return int(self.environ.get('CONTENT_LENGTH') or -1)

    @property
    def content_type(self):
        """ The Content-Type header as a lowercase-string (default: empty). """
        return self.environ.get('CONTENT_TYPE', '').lower()

    @property
    def is_xhr(self):
        """ True if the request was triggered by a XMLHttpRequest. This only
            works with JavaScript libraries that support the `X-Requested-With`
            header (most of the popular libraries do). """
        requested_with = self.environ.get('HTTP_X_REQUESTED_WITH', '')
        return requested_with.lower() == 'xmlhttprequest'

    @property
    def is_ajax(self):
        """ Alias for :attr:`is_xhr`. "Ajax" is not the right term. """
        return self.is_xhr

    @property
    def auth(self):
        """ HTTP authentication data as a (user, password) tuple. This
            implementation currently supports basic (not digest) authentication
            only. If the authentication happened at a higher level (e.g. in the
            front web-server or a middleware), the password field is None, but
            the user field is looked up from the ``REMOTE_USER`` environ
            variable. On any errors, None is returned. """
        basic = parse_auth(self.environ.get('HTTP_AUTHORIZATION', ''))
        if basic: return basic
        ruser = self.environ.get('REMOTE_USER')
        if ruser: return (ruser, None)
        return None

    @property
    def remote_route(self):
        """ A list of all IPs that were involved in this request, starting with
            the client IP and followed by zero or more proxies. This does only
            work if all proxies support the ```X-Forwarded-For`` header. Note
            that this information can be forged by malicious clients. """
        proxy = self.environ.get('HTTP_X_FORWARDED_FOR')
        if proxy: return [ip.strip() for ip in proxy.split(',')]
        remote = self.environ.get('REMOTE_ADDR')
        return [remote] if remote else []

    @property
    def remote_addr(self):
        """ The client IP as a string. Note that this information can be forged
            by malicious clients. """
        route = self.remote_route
        return route[0] if route else None

    def copy(self):
        """ Return a new :class:`Request` with a shallow :attr:`environ` copy. """
        return Request(self.environ.copy())

    def get(self, key, default=None):
        return self.environ.get(key, default)

    def __getitem__(self, key):
        return self.environ[key]

    def __delitem__(self, key):
        self[key] = ""
        del (self.environ[key])

    def __iter__(self):
        return iter(self.environ)

    def __len__(self):
        return len(self.environ)

    def keys(self):
        return self.environ.keys()

    def __setitem__(self, key, value):
        """ Change an environ value and clear all caches that depend on it. """

        if self.environ.get('LitPyWeb.request.readonly'):
            raise KeyError('The environ dictionary is read-only.')

        self.environ[key] = value
        todelete = ()

        if key == 'wsgi.input':
            todelete = ('body', 'forms', 'files', 'params', 'post', 'json')
        elif key == 'QUERY_STRING':
            todelete = ('query', 'params')
        elif key.startswith('HTTP_'):
            todelete = ('headers', 'cookies')

        for key in todelete:
            self.environ.pop('LitPyWeb.request.' + key, None)

    def __repr__(self):
        return '<%s: %s %s>' % (self.__class__.__name__, self.method, self.url)

    def __getattr__(self, name):
        """ Search in self.environ for additional user defined attributes. """
        try:
            var = self.environ['LitPyWeb.request.ext.%s' % name]
            return var.__get__(self) if hasattr(var, '__get__') else var
        except KeyError:
            raise AttributeError('Attribute %r not defined.' % name)

    def __setattr__(self, name, value):
        """ Define new attributes that are local to the bound request environment. """
        if name == 'environ': return object.__setattr__(self, name, value)
        key = 'LitPyWeb.request.ext.%s' % name
        if hasattr(self, name):
            raise AttributeError("Attribute already defined: %s" % name)
        self.environ[key] = value

    def __delattr__(self, name):
        try:
            del self.environ['LitPyWeb.request.ext.%s' % name]
        except KeyError:
            raise AttributeError("Attribute not defined: %s" % name)

def _hkey(key):
    key = touni(key)
    if '\n' in key or '\r' in key or '\0' in key:
        raise ValueError("Header names must not contain control characters: %r" % key)
    return key.title().replace('_', '-')


def _hval(value):
    value = touni(value)
    if '\n' in value or '\r' in value or '\0' in value:
        raise ValueError("Header value must not contain control characters: %r" % value)
    return value


class HeaderProperty:
    def __init__(self, name, reader=None, writer=None, default=''):
        self.name, self.default = name, default
        self.reader, self.writer = reader, writer
        self.__doc__ = 'Current value of the %r header.' % name.title()

    def __get__(self, obj, _):
        if obj is None: return self
        value = obj.get_header(self.name, self.default)
        return self.reader(value) if self.reader else value

    def __set__(self, obj, value):
        obj[self.name] = self.writer(value) if self.writer else value

    def __delete__(self, obj):
        del obj[self.name]


class BaseResponse:
    """ Storage class for a response body as well as headers and cookies.

        This class does support dict-like case-insensitive item-access to
        headers, but is NOT a dict. Most notably, iterating over a response
        yields parts of the body and not the headers.
    """

    default_status = 200
    default_content_type = 'text/html; charset=UTF-8'

    # Header denylist for specific response codes
    # (rfc2616 section 10.2.3 and 10.3.5)
    bad_headers = {
        204: frozenset(('Content-Type', 'Content-Length')),
        304: frozenset(('Allow', 'Content-Encoding', 'Content-Language',
                  'Content-Length', 'Content-Range', 'Content-Type',
                  'Content-Md5', 'Last-Modified'))
    }

    def __init__(self, body='', status=None, headers=None, **more_headers):
        """ Create a new response object.

        :param body: The response body as one of the supported types.
        :param status: Either an HTTP status code (e.g. 200) or a status line
                       including the reason phrase (e.g. '200 OK').
        :param headers: A dictionary or a list of name-value pairs.

        Additional keyword arguments are added to the list of headers.
        Underscores in the header name are replaced with dashes.
        """
        self._cookies = None
        self._headers = {}
        self.body = body
        self.status = status or self.default_status
        if headers:
            if isinstance(headers, dict):
                headers = headers.items()
            for name, value in headers:
                self.add_header(name, value)
        if more_headers:
            for name, value in more_headers.items():
                self.add_header(name, value)

    def copy(self, cls=None):
        """ Returns a copy of self. """
        cls = cls or BaseResponse
        assert issubclass(cls, BaseResponse)
        copy = cls()
        copy.status = self.status
        copy._headers = dict((k, v[:]) for (k, v) in self._headers.items())
        if self._cookies:
            cookies = copy._cookies = SimpleCookie()
            for k,v in self._cookies.items():
                cookies[k] = v.value
                cookies[k].update(v) # also copy cookie attributes
        return copy

    def __iter__(self):
        return iter(self.body)

    def close(self):
        if hasattr(self.body, 'close'):
            self.body.close()

    @property
    def status_line(self):
        """ The HTTP status line as a string (e.g. ``404 Not Found``)."""
        return self._status_line

    @property
    def status_code(self):
        """ The HTTP status code as an integer (e.g. 404)."""
        return self._status_code

    def _set_status(self, status):
        if isinstance(status, int):
            code, status = status, _HTTP_STATUS_LINES.get(status)
        elif ' ' in status:
            if '\n' in status or '\r' in status or '\0' in status:
                raise ValueError('Status line must not include control chars.')
            status = status.strip()
            code = int(status.split()[0])
        else:
            raise ValueError('String status line without a reason phrase.')
        if not 100 <= code <= 999:
            raise ValueError('Status code out of range.')
        self._status_code = code
        self._status_line = str(status or ('%d Unknown' % code))

    def _get_status(self):
        return self._status_line

    status = property(
        _get_status, _set_status, None,
        ''' A writeable property to change the HTTP response status. It accepts
            either a numeric code (100-999) or a string with a custom reason
            phrase (e.g. "404 Brain not found"). Both :data:`status_line` and
            :data:`status_code` are updated accordingly. The return value is
            always a status string. ''')
    del _get_status, _set_status

    @property
    def headers(self):
        """ An instance of :class:`HeaderDict`, a case-insensitive dict-like
            view on the response headers. """
        hdict = HeaderDict()
        hdict.dict = self._headers
        return hdict

    def __contains__(self, name):
        return _hkey(name) in self._headers

    def __delitem__(self, name):
        del self._headers[_hkey(name)]

    def __getitem__(self, name):
        return self._headers[_hkey(name)][-1]

    def __setitem__(self, name, value):
        self._headers[_hkey(name)] = [_hval(value)]

    def get_header(self, name, default=None):
        """ Return the value of a previously defined header. If there is no
            header with that name, return a default value. """
        return self._headers.get(_hkey(name), [default])[-1]

    def set_header(self, name, value):
        """ Create a new response header, replacing any previously defined
            headers with the same name. """
        self._headers[_hkey(name)] = [_hval(value)]

    def add_header(self, name, value):
        """ Add an additional response header, not removing duplicates. """
        self._headers.setdefault(_hkey(name), []).append(_hval(value))

    def iter_headers(self):
        """ Yield (header, value) tuples, skipping headers that are not
            allowed with the current response status code. """
        return self.headerlist

    def _wsgi_status_line(self):
        """ WSGI conform status line (latin1-encodeable) """
        return self._status_line.encode('utf8', 'surrogateescape').decode('latin1')

    @property
    def headerlist(self):
        """ WSGI conform list of (header, value) tuples. """
        out = []
        headers = list(self._headers.items())
        if 'Content-Type' not in self._headers:
            headers.append(('Content-Type', [self.default_content_type]))
        if self._status_code in self.bad_headers:
            bad_headers = self.bad_headers[self._status_code]
            headers = [h for h in headers if h[0] not in bad_headers]
        out += [(name, val) for (name, vals) in headers for val in vals]
        if self._cookies:
            for c in self._cookies.values():
                out.append(('Set-Cookie', _hval(c.OutputString())))
        out = [(k, v.encode('utf8', 'surrogateescape').decode('latin1')) for (k, v) in out]
        return out

    content_type = HeaderProperty('Content-Type')
    content_length = HeaderProperty('Content-Length', reader=int, default=-1)
    expires = HeaderProperty(
        'Expires',
        reader=lambda x: datetime.fromtimestamp(parse_date(x), UTC),
        writer=lambda x: http_date(x))

    @property
    def charset(self, default='UTF-8'):
        """ Return the charset specified in the content-type header (default: utf8). """
        if 'charset=' in self.content_type:
            return self.content_type.split('charset=')[-1].split(';')[0].strip()
        return default

    def set_cookie(self, name, value, secret=None, digestmod=hashlib.sha256, **options):
        """ Create a new cookie or replace an old one. If the `secret` parameter is
            set, create a `Signed Cookie` (described below).

            :param name: the name of the cookie.
            :param value: the value of the cookie.
            :param secret: a signature key required for signed cookies.

            Additionally, this method accepts all RFC 2109 attributes that are
            supported by :class:`cookie.Morsel`, including:

            :param maxage: maximum age in seconds. (default: None)
            :param expires: a datetime object or UNIX timestamp. (default: None)
            :param domain: the domain that is allowed to read the cookie.
              (default: current domain)
            :param path: limits the cookie to a given path (default: current path)
            :param secure: limit the cookie to HTTPS connections (default: off).
            :param httponly: prevents client-side javascript to read this cookie
              (default: off, requires Python 2.6 or newer).
            :param samesite: Control or disable third-party use for this cookie.
              Possible values: `lax`, `strict` or `none` (default).

            If neither `expires` nor `maxage` is set (default), the cookie will
            expire at the end of the browser session (as soon as the browser
            window is closed).

            Signed cookies may store any pickle-able object and are
            cryptographically signed to prevent manipulation. Keep in mind that
            cookies are limited to 4kb in most browsers.

            Warning: Pickle is a potentially dangerous format. If an attacker
            gains access to the secret key, he could forge cookies that execute
            code on server side if unpickled. Using pickle is discouraged and
            support for it will be removed in later versions of LitPyWeb.

            Warning: Signed cookies are not encrypted (the client can still see
            the content) and not copy-protected (the client can restore an old
            cookie). The main intention is to make pickling and unpickling
            save, not to store secret information at client side.
        """
        if not self._cookies:
            self._cookies = SimpleCookie()

        # Monkey-patch Cookie lib to support 'SameSite' parameter
        # https://tools.ietf.org/html/draft-west-first-party-cookies-07#section-4.1
        if py < (3, 8, 0):
            Morsel._reserved.setdefault('samesite', 'SameSite')

        if secret:
            if not isinstance(value, str):
                depr(0, 13, "Pickling of arbitrary objects into cookies is "
                            "deprecated.", "Only store strings in cookies. "
                            "JSON strings are fine, too.")
            encoded = base64.b64encode(pickle.dumps([name, value], -1))
            sig = base64.b64encode(hmac.new(tob(secret), encoded,
                                            digestmod=digestmod).digest())
            value = touni(b'!' + sig + b'?' + encoded)
        elif not isinstance(value, str):
            raise TypeError('Secret key required for non-string cookies.')

        # Cookie size plus options must not exceed 4kb.
        if len(name) + len(value) > 3800:
            raise ValueError('Content does not fit into a cookie.')

        self._cookies[name] = value

        for key, value in options.items():
            if key in ('max_age', 'maxage'): # 'maxage' variant added in 0.13
                key = 'max-age'
                if isinstance(value, timedelta):
                    value = value.seconds + value.days * 24 * 3600
            if key == 'expires':
                value = http_date(value)
            if key in ('same_site', 'samesite'): # 'samesite' variant added in 0.13
                key, value = 'samesite', (value or "none").lower()
                if value not in ('lax', 'strict', 'none'):
                    raise CookieError("Invalid value for SameSite")
            if key in ('secure', 'httponly') and not value:
                continue
            self._cookies[name][key] = value

    def delete_cookie(self, key, **kwargs):
        """ Delete a cookie. Be sure to use the same `domain` and `path`
            settings as used to create the cookie. """
        kwargs['max_age'] = -1
        kwargs['expires'] = 0
        self.set_cookie(key, '', **kwargs)

    def __repr__(self):
        out = ''
        for name, value in self.headerlist:
            out += '%s: %s\n' % (name.title(), value.strip())
        return out


def _local_property():
    ls = threading.local()

    def fget(_):
        try:
            return ls.var
        except AttributeError:
            raise RuntimeError("Request context not initialized.")

    def fset(_, value):
        ls.var = value

    def fdel(_):
        del ls.var

    return property(fget, fset, fdel, 'Thread-local property')


class LocalRequest(BaseRequest):
    """ A thread-local subclass of :class:`BaseRequest` with a different
        set of attributes for each thread. There is usually only one global
    instance of this class (:data:`request`). If accessed during a
        request/response cycle, this instance always refers to the *current*
        request (even on a multithreaded server). """
    bind = BaseRequest.__init__
    environ = _local_property()


class LocalResponse(BaseResponse):
    """ A thread-local subclass of :class:`BaseResponse` with a different
        set of attributes for each thread. There is usually only one global
        instance of this class (:data:`response`). Its attributes are used
        to build the HTTP response at the end of the request/response cycle.
    """
    bind = BaseResponse.__init__
    _status_line = _local_property()
    _status_code = _local_property()
    _cookies = _local_property()
    _headers = _local_property()
    body = _local_property()

Request = BaseRequest
Response = BaseResponse


class HTTPResponse(Response, LitPyWebException):
    """ A subclass of :class:`Response` that can be raised or returned from request
        handlers to short-curcuit request processing and override changes made to the
        global :data:`request` object. This bypasses error handlers, even if the status
        code indicates an error. Return or raise :class:`HTTPError` to trigger error
        handlers.
    """

    def __init__(self, body='', status=None, headers=None, **more_headers):
        super(HTTPResponse, self).__init__(body, status, headers, **more_headers)

    def apply(self, other):
        """ Copy the state of this response to a different :class:`Response` object. """
        other._status_code = self._status_code
        other._status_line = self._status_line
        other._headers = self._headers
        other._cookies = self._cookies
        other.body = self.body


class HTTPError(HTTPResponse):
    """ A subclass of :class:`HTTPResponse` that triggers error handlers. """

    default_status = 500

    def __init__(self,
                 status=None,
                 body=None,
                 exception=None,
                 traceback=None, **more_headers):
        self.exception = exception
        self.traceback = traceback
        super(HTTPError, self).__init__(body, status, **more_headers)
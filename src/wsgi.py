from ..utils.cli import DictProperty, _wsgi_recode, tob, hmac, json_loads, touni, _HTTP_STATUS_LINES, depr
from ..utils.utilities import WSGIHeaderDict, FormsDict, FileUpload
from http.cookies import SimpleCookie, Morsel, CookieError
import hashlib, base64, pickle, datetime, sys, threading
from ..utils.core_utils import _lscmp, _parse_qsl, path_shift, parse_auth, parse_date
from io import BytesIO
from tempfile import NamedTemporaryFile
from .server import _parse_http_header, http_date
from .multipart import MultipartError, _MultipartParser
from urllib.parse import urljoin, SplitResult as UrlSplitResult
from urllib.parse import quote as urlquote
from ..utils.utilities import HeaderDict
from datetime import timezone, timedelta
UTC = timezone.utc
from ..utils.exceptions import LitPyWebException

py = sys.version_info

###############################################################################
# HTTP和WSGI工具 ##########################################################
###############################################################################


class BaseRequest:
    """ WSGI 环境字典的包装类，提供了大量便捷的访问方法和属性，大多数是只读的。

        向请求对象添加新属性实际上是向 environ 字典中添加以 'LitPyWeb.request.ext.<name>' 为前缀的键值对。
        这是推荐的用于存储和访问请求特定数据的方法。
    """

    __slots__ = ('environ', )

    MEMFILE_MAX = 102400

    def __init__(self, environ=None):
        """ 初始化函数：封装一个 WSGI 的 environ 字典。 """
        self.environ = {} if environ is None else environ
        self.environ['LitPyWeb.request'] = self

    @DictProperty('environ', 'LitPyWeb.app', read_only=True)
    def app(self):
        """ 处理该请求的 LitPyWeb 应用程序对象。 """
        raise RuntimeError('This request is not connected to an application.')

    @DictProperty('environ', 'LitPyWeb.route', read_only=True)
    def route(self):
        """ 与当前请求匹配的 LitPyWeb 路由对象（Route）。 """
        raise RuntimeError('This request is not connected to a route.')

    @DictProperty('environ', 'route.url_args', read_only=True)
    def url_args(self):
        """ 从 URL 中提取的参数。 """
        raise RuntimeError('This request is not connected to a route.')

    @property
    def path(self):
        """ 获取 PATH_INFO，并确保以单个斜杠开头，用于修复一些客户端不规范行为和避免路径为空的特殊情况。 """
        return '/' + self.environ.get('PATH_INFO', '').lstrip('/')

    @property
    def method(self):
        """ 获取请求方法（如 GET、POST），返回大写字符串形式。 """
        return self.environ.get('REQUEST_METHOD', 'GET').upper()

    @DictProperty('environ', 'LitPyWeb.request.headers', read_only=True)
    def headers(self):
        """ 提供对 HTTP 请求头不区分大小写的访问接口，返回 WSGIHeaderDict 对象。 """
        return WSGIHeaderDict(self.environ)

    def get_header(self, name, default=None):
        """ 获取指定名称的请求头值，如果不存在则返回默认值。 """
        return self.headers.get(name, default)

    @DictProperty('environ', 'LitPyWeb.request.cookies', read_only=True)
    def cookies(self):
        """ 解析的 Cookies，结果为 FormsDict。
            注意：签名 Cookie 不会被解码，如需获取签名 Cookie，请使用 get_cookie 方法。 """
        cookie_header = _wsgi_recode(self.environ.get('HTTP_COOKIE', ''))
        cookies = SimpleCookie(cookie_header).values()
        return FormsDict((c.key, c.value) for c in cookies)

    def get_cookie(self, key, default=None, secret=None, digestmod=hashlib.sha256):
        """ 获取 Cookie 内容。如果需要读取签名 Cookie，必须提供 secret（与设置时相同）。
            如果 Cookie 不存在或签名错误，则返回默认值。 """
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
        """ 解析 query_string 中的 GET 参数，结果为 FormsDict。
            这些参数有时也称为 URL 参数，但不要与 Router 中的 URL 通配符混淆。"""
        get = self.environ['LitPyWeb.get'] = FormsDict()
        pairs = _parse_qsl(self.environ.get('QUERY_STRING', ''), 'utf8')
        for key, value in pairs:
            get[key] = value
        return get

    @DictProperty('environ', 'LitPyWeb.request.forms', read_only=True)
    def forms(self):
        """ 解析 POST 或 PUT 请求中的表单字段，支持 url-encoded 与 multipart/form-data 格式。
            返回 FormsDict，仅包含字符串键值对，上传文件请查看 files 属性。 """
        forms = FormsDict()
        for name, item in self.POST.allitems():
            if not isinstance(item, FileUpload):
                forms[name] = item
        return forms

    @DictProperty('environ', 'LitPyWeb.request.params', read_only=True)
    def params(self):
        """ 返回合并后的 GET 参数（query）与表单参数（forms），不包含文件。结果为 FormsDict。 """
        params = FormsDict()
        for key, value in self.query.allitems():
            params[key] = value
        for key, value in self.forms.allitems():
            params[key] = value
        return params

    @DictProperty('environ', 'LitPyWeb.request.files', read_only=True)
    def files(self):
        """ 解析 multipart/form-data 中的上传文件，结果为 FormsDict，其中的值为 FileUpload 实例。
        """
        files = FormsDict()
        for name, item in self.POST.allitems():
            if isinstance(item, FileUpload):
                files[name] = item
        return files

    @DictProperty('environ', 'LitPyWeb.request.json', read_only=True)
    def json(self):
        """ 如果请求头中的 Content-Type 为 application/json 或 application/json-rpc，
            则该属性包含解析后的 JSON 内容（前提是请求体大小小于 MEMFILE_MAX）。
            如果 JSON 无效，则抛出 400 错误。
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
        # 正常模式下按块读取请求体内容
        maxread = max(0, self.content_length)
        while maxread:
            part = read(min(maxread, bufsize))
            if not part: break
            yield part
            maxread -= len(part)

    @staticmethod
    def _iter_chunked(read, bufsize):
        # 使用 chunked 编码的请求体解析逻辑（RFC 7230）
        # 每个块包含一个十六进制长度和对应内容
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
        # 读取请求体内容，如果内容较大将使用临时文件缓存
        # 如果使用 chunked 传输编码，则调用 _iter_chunked
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
        """ 将请求体读取为字符串。如果请求体过大，抛出 HTTPError(413)。 """
        if self.content_length > maxread:
            raise HTTPError(413, 'Request entity too large')
        data = self.body.read(maxread + 1)
        if len(data) > maxread:
            raise HTTPError(413, 'Request entity too large')
        return data

    @property
    def body(self):
        """ 请求体作为一个可 seek 的类似文件对象。
        根据 MEMFILE_MAX 的值，可能是临时文件或 BytesIO 实例。
        第一次访问时会读取并替换 environ['wsgi.input']，后续访问会自动 seek(0)。 """
        self._body.seek(0)
        return self._body

    @property
    def chunked(self):
        """ 如果使用了 Chunked Transfer-Encoding，则为 True。 """
        return 'chunked' in self.environ.get(
            'HTTP_TRANSFER_ENCODING', '').lower()

    GET = query

    @DictProperty('environ', 'LitPyWeb.request.post', read_only=True)
    def POST(self):
        """ 表单字段与上传文件的组合体，返回 FormsDict。
        表单值为字符串，文件值为 FileUpload 实例。
        """
        post = FormsDict()
        content_type = self.environ.get('CONTENT_TYPE', '')
        content_type, options = _parse_http_header(content_type)[0]
        # 对非 multipart 类型默认使用 application/x-www-form-urlencoded，进行快速解析
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
        """ 包含协议和主机名的完整请求 URI。如果应用部署在反向代理或负载均衡器后，
        请确保设置了 X-Forwarded-Host 头。 """
        return self.urlparts.geturl()

    @DictProperty('environ', 'LitPyWeb.request.urlparts', read_only=True)
    def urlparts(self):
        """ 返回 url 属性作为 urlparse.SplitResult 结果，包含 scheme、host、path、query_string，fragment 总为空。 """
        env = self.environ
        http = env.get('HTTP_X_FORWARDED_PROTO') \
             or env.get('wsgi.url_scheme', 'http')
        host = env.get('HTTP_X_FORWARDED_HOST') or env.get('HTTP_HOST')
        if not host:
            # HTTP/1.1 要求 Host 头，此处为兼容 HTTP/1.0 客户端
            host = env.get('SERVER_NAME', '127.0.0.1')
            port = env.get('SERVER_PORT')
            if port and port != ('80' if http == 'http' else '443'):
                host += ':' + port
        path = urlquote(self.fullpath)
        return UrlSplitResult(http, host, path, env.get('QUERY_STRING'), '')

    @property
    def fullpath(self):
        """ 包含 script_name 的完整请求路径。 """
        return urljoin(self.script_name, self.path.lstrip('/'))

    @property
    def query_string(self):
        """ 原始查询字符串（? 与 # 之间部分）。 """
        return self.environ.get('QUERY_STRING', '')

    @property
    def script_name(self):
        """ 被上层服务（如反向代理）移除的 URL 路径前缀，返回以 / 开头和结尾的路径。 """
        script_name = self.environ.get('SCRIPT_NAME', '').strip('/')
        return '/' + script_name + '/' if script_name else '/'

    def path_shift(self, shift=1):
        """ 在 script_name 和 path 之间移动路径段。

        :param shift: 要移动的路径段数量，负数表示反向移动（默认为 1）
        """
        script, path = path_shift(self.environ.get('SCRIPT_NAME', '/'), self.path, shift)
        self['SCRIPT_NAME'], self['PATH_INFO'] = script, path

    @property
    def content_length(self):
        """ 请求体长度（由客户端提供的 Content-Length 头决定）。
        若未提供则返回 -1，body 属性将为空。 """
        return int(self.environ.get('CONTENT_LENGTH') or -1)

    @property
    def content_type(self):
        """ Content-Type 请求头的小写字符串，默认为空字符串。 """
        return self.environ.get('CONTENT_TYPE', '').lower()

    @property
    def is_xhr(self):
        """ 如果请求通过 XMLHttpRequest 触发，则为 True（依赖 X-Requested-With 头）。 """
        requested_with = self.environ.get('HTTP_X_REQUESTED_WITH', '')
        return requested_with.lower() == 'xmlhttprequest'

    @property
    def is_ajax(self):
        """ is_xhr 的别名，虽然 “Ajax” 并不是最准确的术语。 """
        return self.is_xhr

    @property
    def auth(self):
        """ HTTP 认证信息，返回 (用户名, 密码) 元组。
        当前仅支持 Basic Auth；如果认证由上层处理（如 nginx），则密码为 None，用户名从 REMOTE_USER 中获取。
        出错则返回 None。 """
        basic = parse_auth(self.environ.get('HTTP_AUTHORIZATION', ''))
        if basic: return basic
        ruser = self.environ.get('REMOTE_USER')
        if ruser: return (ruser, None)
        return None

    @property
    def remote_route(self):
        """ 发起请求的所有 IP 列表，包含客户端及代理 IP，依赖 X-Forwarded-For 头。注意该信息可能被伪造。 """
        proxy = self.environ.get('HTTP_X_FORWARDED_FOR')
        if proxy: return [ip.strip() for ip in proxy.split(',')]
        remote = self.environ.get('REMOTE_ADDR')
        return [remote] if remote else []

    @property
    def remote_addr(self):
        """ 客户端 IP 地址字符串（可能被伪造）。 """
        route = self.remote_route
        return route[0] if route else None

    def copy(self):
        """ 返回当前请求的浅拷贝，environ 将被复制。 """
        return Request(self.environ.copy())

    def get(self, key, default=None):
        """ 从 environ 中获取指定键值。"""
        return self.environ.get(key, default)

    def __getitem__(self, key):
        """ 获取 environ 中指定键的值。"""
        return self.environ[key]

    def __delitem__(self, key):
        """ 删除指定键值对，同时设置为空字符串防止异常。"""
        self[key] = ""
        del (self.environ[key])

    def __iter__(self):
        """ 遍历 environ 字典。"""
        return iter(self.environ)

    def __len__(self):
        """ 返回 environ 中的键值对数量。"""
        return len(self.environ)

    def keys(self):
        """ 返回 environ 的所有键。"""
        return self.environ.keys()

    def __setitem__(self, key, value):
        """ 修改 environ 中的键值，并清除依赖该键的缓存。 """

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
        """ 返回请求对象的简洁字符串表示，如 <BaseRequest: GET /index>。"""
        return '<%s: %s %s>' % (self.__class__.__name__, self.method, self.url)

    def __getattr__(self, name):
        """ 从 environ 中查找用户自定义扩展属性。 """
        try:
            var = self.environ['LitPyWeb.request.ext.%s' % name]
            return var.__get__(self) if hasattr(var, '__get__') else var
        except KeyError:
            raise AttributeError('Attribute %r not defined.' % name)

    def __setattr__(self, name, value):
        """ 为请求对象设置扩展属性，会存入 environ 的扩展空间。 """
        if name == 'environ': return object.__setattr__(self, name, value)
        key = 'LitPyWeb.request.ext.%s' % name
        if hasattr(self, name):
            raise AttributeError("Attribute already defined: %s" % name)
        self.environ[key] = value

    def __delattr__(self, name):
        """ 删除请求扩展属性。"""
        try:
            del self.environ['LitPyWeb.request.ext.%s' % name]
        except KeyError:
            raise AttributeError("Attribute not defined: %s" % name)


def _hkey(key):
    """ 标准化 HTTP 头字段名，转换为标题格式并去除控制字符。"""
    key = touni(key)
    if '\n' in key or '\r' in key or '\0' in key:
        raise ValueError("Header names must not contain control characters: %r" % key)
    return key.title().replace('_', '-')


def _hval(value):
    """ 标准化 HTTP 头字段值，转换为 unicode 并去除控制字符。"""
    value = touni(value)
    if '\n' in value or '\r' in value or '\0' in value:
        raise ValueError("Header value must not contain control characters: %r" % value)
    return value


class HeaderProperty:
    """ 用于通过属性访问响应头字段的描述符类。"""
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
    """ HTTP 响应对象，封装响应体、头部和 cookies。

        支持类字典访问响应头（不区分大小写），但并非真正的 dict。
        迭代该对象会返回响应体的内容而非头部。
    """

    default_status = 200
    default_content_type = 'text/html; charset=UTF-8'

    # 某些状态码禁止设置的响应头（来自 RFC 2616）
    bad_headers = {
        204: frozenset(('Content-Type', 'Content-Length')),
        304: frozenset(('Allow', 'Content-Encoding', 'Content-Language',
                  'Content-Length', 'Content-Range', 'Content-Type',
                  'Content-Md5', 'Last-Modified'))
    }

    def __init__(self, body='', status=None, headers=None, **more_headers):
        """ 创建新的响应对象。

        :param body: 响应体内容，可以是字符串或可迭代对象。
        :param status: 整数状态码（如 200）或状态行字符串（如 '200 OK'）。
        :param headers: 头部字典或 (name, value) 列表。
        :param more_headers: 额外的头部参数，自动将下划线替换为中划线。
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
        """ 返回当前响应的副本。"""
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
        """ 支持对响应体的迭代（如 WSGI 中间件使用）。"""
        return iter(self.body)

    def close(self):
        """ 关闭响应体（如果支持 close 方法）。"""
        if hasattr(self.body, 'close'):
            self.body.close()

    @property
    def status_line(self):
        """ 返回状态行字符串，例如 '404 Not Found'。"""
        return self._status_line

    @property
    def status_code(self):
        """ 返回状态码整数值，例如 404。"""
        return self._status_code

    def _set_status(self, status):
        """ 内部方法：设置状态码和状态行。"""
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
        ''' 可读写属性，用于设置 HTTP 响应状态。
            可为整数（如 404）或字符串（如 '404 Not Found'）。
            status_line 与 status_code 都会自动更新。
            返回值始终为字符串形式的状态行。 ''')
    del _get_status, _set_status

    @property
    def headers(self):
        """ 返回 HeaderDict（不区分大小写）封装的响应头。 """
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
        """ 获取某个响应头的值，如果不存在则返回默认值。 """
        return self._headers.get(_hkey(name), [default])[-1]

    def set_header(self, name, value):
        """ 设置响应头，如果已有同名头部则替换。 """
        self._headers[_hkey(name)] = [_hval(value)]

    def add_header(self, name, value):
        """ 添加响应头，不去重，允许多个同名头部存在。 """
        self._headers.setdefault(_hkey(name), []).append(_hval(value))

    def iter_headers(self):
        """ 生成 (header, value) 对，用于输出，但会跳过当前状态码禁止的头部字段。 """
        return self.headerlist

    def _wsgi_status_line(self):
        """ 返回符合 WSGI 标准的状态行，编码为 latin1。 """
        return self._status_line.encode('utf8', 'surrogateescape').decode('latin1')

    @property
    def headerlist(self):
        """ 返回 WSGI 规范格式的 (header, value) 列表。 """
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
        """ 返回 Content-Type 中声明的字符编码，默认值为 UTF-8。 """
        if 'charset=' in self.content_type:
            return self.content_type.split('charset=')[-1].split(';')[0].strip()
        return default

    def set_cookie(self, name, value, secret=None, digestmod=hashlib.sha256, **options):
        """ 设置新的 Cookie，支持签名 Cookie。
        - name: Cookie 名称
        - value: Cookie 值
        - secret: 如果设置则创建签名 Cookie
        - 额外参数支持 maxage, expires, domain, path, secure, httponly, samesite 等

        警告：
        - Pickle 可能不安全，如泄漏 secret 可被伪造；
        - 签名 Cookie 并未加密，仅防止篡改，不建议存储敏感信息。
        """
        if not self._cookies:
            self._cookies = SimpleCookie()

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

        if len(name) + len(value) > 3800:
            raise ValueError('Content does not fit into a cookie.')

        self._cookies[name] = value

        for key, value in options.items():
            if key in ('max_age', 'maxage'):
                key = 'max-age'
                if isinstance(value, timedelta):
                    value = value.seconds + value.days * 24 * 3600
            if key == 'expires':
                value = http_date(value)
            if key in ('same_site', 'samesite'):
                key, value = 'samesite', (value or "none").lower()
                if value not in ('lax', 'strict', 'none'):
                    raise CookieError("Invalid value for SameSite")
            if key in ('secure', 'httponly') and not value:
                continue
            self._cookies[name][key] = value

    def delete_cookie(self, key, **kwargs):
        """ 删除指定 Cookie，需设置与原 Cookie 相同的 path 和 domain 才能成功。 """
        kwargs['max_age'] = -1
        kwargs['expires'] = 0
        self.set_cookie(key, '', **kwargs)

    def __repr__(self):
        """ 打印响应对象的 header 列表。"""
        out = ''
        for name, value in self.headerlist:
            out += '%s: %s\n' % (name.title(), value.strip())
        return out


def _local_property():
    """ 返回线程局部变量的属性描述符，用于多线程隔离。"""
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
    """ 线程本地的请求对象，每个线程独立拥有一份属性副本。
        一般通过全局 request 实例访问。"""
    bind = BaseRequest.__init__
    environ = _local_property()


class LocalResponse(BaseResponse):
    """ 线程本地的响应对象，每个线程独立拥有一份属性副本。
        一般通过全局 response 实例访问，用于构建最终响应。
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
    """ 可抛出的 Response 子类，用于提前中断请求处理流程。
        会跳过错误处理器（即使状态码为错误）。
        若希望触发错误处理器，请使用 HTTPError。
    """

    def __init__(self, body='', status=None, headers=None, **more_headers):
        super(HTTPResponse, self).__init__(body, status, headers, **more_headers)

    def apply(self, other):
        """ 将当前响应状态复制到另一个 Response 对象。 """
        other._status_code = self._status_code
        other._status_line = self._status_line
        other._headers = self._headers
        other._cookies = self._cookies
        other.body = self.body


class HTTPError(HTTPResponse):
    """ 表示触发错误处理器的特殊响应。 """

    default_status = 500

    def __init__(self,
                 status=None,
                 body=None,
                 exception=None,
                 traceback=None, **more_headers):
        self.exception = exception
        self.traceback = traceback
        super(HTTPError, self).__init__(body, status, **more_headers)
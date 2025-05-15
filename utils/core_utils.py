import warnings, email.utils, calendar, base64, hashlib, hmac, pickle, inspect, functools
from .cli import tob, touni, depr, request, app
from urllib.parse import urlencode, quote as urlquote, unquote as urlunquote
from ..src.wsgi import HTTPError
from ..src.LitPyWeb import LitPyWeb

def debug(mode=True):
    """ 设置调试模式。
    当前仅支持一个调试级别：True/False。"""
    global DEBUG
    if mode: warnings.simplefilter('default')
    DEBUG = bool(mode)

def parse_date(ims):
    """ 解析 rfc1123、rfc850 和 asctime 格式的时间字符串，返回 UTC 时间戳（秒）。 """
    try:
        ts = email.utils.parsedate_tz(ims)
        return calendar.timegm(ts[:8] + (0, )) - (ts[9] or 0)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def parse_auth(header):
    """ 解析 HTTP Basic Auth 头部，返回 (用户名, 密码) 元组。"""
    try:
        method, data = header.split(None, 1)
        if method.lower() == 'basic':
            user, pwd = touni(base64.b64decode(tob(data))).split(':', 1)
            return user, pwd
    except (KeyError, ValueError):
        return None


def parse_range_header(header, maxlen=0):
    """ 解析 Range 头部字段，返回不超过 maxlen 的字节范围生成器（start, end）。
        end 为非包含型索引（Python 风格）。"""
    if not header or header[:6] != 'bytes=': return
    ranges = [r.split('-', 1) for r in header[6:].split(',') if '-' in r]
    for start, end in ranges:
        try:
            if not start:  # bytes=-100    -> last 100 bytes
                start, end = max(0, maxlen - int(end)), maxlen
            elif not end:  # bytes=100-    -> all but the first 99 bytes
                start, end = int(start), maxlen
            else:  # bytes=100-200 -> bytes 100-200 (inclusive)
                start, end = int(start), min(int(end) + 1, maxlen)
            if 0 <= start < end <= maxlen:
                yield start, end
        except ValueError:
            pass

def _parse_qsl(qs, encoding="utf8"):
    """ 解析查询字符串，返回键值对列表。"""
    r = []
    for pair in qs.split('&'):
        if not pair: continue
        nv = pair.split('=', 1)
        if len(nv) != 2: nv.append('')
        key = urlunquote(nv[0].replace('+', ' '), encoding)
        value = urlunquote(nv[1].replace('+', ' '), encoding)
        r.append((key, value))
    return r

def _lscmp(a, b):
    """ 安全比较两个字符串，避免时间侧信道攻击。 """
    return not sum(0 if x == y else 1
                   for x, y in zip(a, b)) and len(a) == len(b)


def cookie_encode(data, key, digestmod=None):
    """ 码并签名可序列化对象，返回字节串。 """
    depr(0, 13, "cookie_encode() will be removed soon.",
                "Do not use this API directly.")
    digestmod = digestmod or hashlib.sha256
    msg = base64.b64encode(pickle.dumps(data, -1))
    sig = base64.b64encode(hmac.new(tob(key), msg, digestmod=digestmod).digest())
    return b'!' + sig + b'?' + msg

def cookie_decode(data, key, digestmod=None):
    """ 验证并解码已编码的 Cookie 字符串，返回对象或 None。"""
    depr(0, 13, "cookie_decode() will be removed soon.",
                "Do not use this API directly.")
    data = tob(data)
    if cookie_is_encoded(data):
        sig, msg = data.split(b'?', 1)
        digestmod = digestmod or hashlib.sha256
        hashed = hmac.new(tob(key), msg, digestmod=digestmod).digest()
        if _lscmp(sig[1:], base64.b64encode(hashed)):
            return pickle.loads(base64.b64decode(msg))
    return None

def cookie_is_encoded(data):
    """ 判断数据是否为编码过的 Cookie 格式。"""
    depr(0, 13, "cookie_is_encoded() will be removed soon.",
                "Do not use this API directly.")
    return bool(data.startswith(b'!') and b'?' in data)

def html_escape(string):
    """ 转义 HTML 特殊字符：& < > ' "。"""
    return string.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')\
                 .replace('"', '&quot;').replace("'", '&#039;')

def html_quote(string):
    """ 适用于 HTML 属性值的转义版本，带引号。"""
    return '"%s"' % html_escape(string).replace('\n', '&#10;')\
                    .replace('\r', '&#13;').replace('\t', '&#9;')

def yieldroutes(func):
    """ 根据函数签名自动生成匹配的路由路径字符串。
        支持可选参数，会生成多个候选路径。
    """
    path = '/' + func.__name__.replace('__', '/').lstrip('/')
    sig = inspect.signature(func, follow_wrapped=False)
    for p in sig.parameters.values():
        if p.kind == p.POSITIONAL_ONLY:
            raise ValueError("Invalid signature for yieldroutes: %s" % sig)
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
            if p.default != p.empty:
                yield path  # Yield path without this (optional) parameter.
            path += "/<%s>" % p.name
    yield path

def path_shift(script_name, path_info, shift=1):
    """ 在 SCRIPT_NAME 和 PATH_INFO 之间移动路径片段。
        :param shift: >0 表示向后移动（从 PATH_INFO 移入 SCRIPT_NAME）；
                      <0 表示向前移动。
        :return: 新的 script_name 和 path_info
    """
    if shift == 0: return script_name, path_info
    pathlist = path_info.strip('/').split('/')
    scriptlist = script_name.strip('/').split('/')
    if pathlist and pathlist[0] == '': pathlist = []
    if scriptlist and scriptlist[0] == '': scriptlist = []
    if 0 < shift <= len(pathlist):
        moved = pathlist[:shift]
        scriptlist = scriptlist + moved
        pathlist = pathlist[shift:]
    elif 0 > shift >= -len(scriptlist):
        moved = scriptlist[shift:]
        pathlist = moved + pathlist
        scriptlist = scriptlist[:shift]
    else:
        empty = 'SCRIPT_NAME' if shift < 0 else 'PATH_INFO'
        raise AssertionError("Cannot shift. Nothing left from %s" % empty)
    new_script_name = '/' + '/'.join(scriptlist)
    new_path_info = '/' + '/'.join(pathlist)
    if path_info.endswith('/') and pathlist: new_path_info += '/'
    return new_script_name, new_path_info


def auth_basic(check, realm="private", text="Access denied"):
    """ 使用 HTTP Basic Auth 验证访问的装饰器。
        :param check: 回调函数，接受 (username, password)。
        :param realm: 显示在认证提示中的保护域。 """

    def decorator(func):

        @functools.wraps(func)
        def wrapper(*a, **ka):
            user, password = request.auth or (None, None)
            if user is None or not check(user, password):
                err = HTTPError(401, text)
                err.add_header('WWW-Authenticate', 'Basic realm="%s"' % realm)
                return err
            return func(*a, **ka)

        return wrapper

    return decorator

def make_default_app_wrapper(name):
    """ 为默认应用程序包装一个代理函数，用于调用其方法。 """

    @functools.wraps(getattr(LitPyWeb, name))
    def wrapper(*a, **ka):
        return getattr(app(), name)(*a, **ka)

    return wrapper


route     = make_default_app_wrapper('route')
get       = make_default_app_wrapper('get')
post      = make_default_app_wrapper('post')
put       = make_default_app_wrapper('put')
delete    = make_default_app_wrapper('delete')
patch     = make_default_app_wrapper('patch')
error     = make_default_app_wrapper('error')
mount     = make_default_app_wrapper('mount')
hook      = make_default_app_wrapper('hook')
install   = make_default_app_wrapper('install')
uninstall = make_default_app_wrapper('uninstall')
url       = make_default_app_wrapper('get_url')

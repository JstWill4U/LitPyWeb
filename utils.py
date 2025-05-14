def abort(code=500, text='Unknown Error.'):
    """ Aborts execution and causes a HTTP error. """
    raise HTTPError(code, text)


def redirect(url, code=None):
    """ Aborts execution and causes a 303 or 302 redirect, depending on
        the HTTP protocol version. """
    if not code:
        code = 303 if request.get('SERVER_PROTOCOL') == "HTTP/1.1" else 302
    res = response.copy(cls=HTTPResponse)
    res.status = code
    res.body = ""
    res.set_header('Location', urljoin(request.url, url))
    raise res


def _rangeiter(fp, offset, limit, bufsize=1024 * 1024):
    """ Yield chunks from a range in a file. """
    fp.seek(offset)
    while limit > 0:
        part = fp.read(min(limit, bufsize))
        if not part:
            break
        limit -= len(part)
        yield part


def static_file(filename, root,
                mimetype=True,
                download=False,
                charset='UTF-8',
                etag=None,
                headers=None):
    """ Open a file in a safe way and return an instance of :exc:`HTTPResponse`
        that can be sent back to the client.

        :param filename: Name or path of the file to send, relative to ``root``.
        :param root: Root path for file lookups. Should be an absolute directory
            path.
        :param mimetype: Provide the content-type header (default: guess from
            file extension)
        :param download: If True, ask the browser to open a `Save as...` dialog
            instead of opening the file with the associated program. You can
            specify a custom filename as a string. If not specified, the
            original filename is used (default: False).
        :param charset: The charset for files with a ``text/*`` mime-type.
            (default: UTF-8)
        :param etag: Provide a pre-computed ETag header. If set to ``False``,
            ETag handling is disabled. (default: auto-generate ETag header)
        :param headers: Additional headers dict to add to the response.

        While checking user input is always a good idea, this function provides
        additional protection against malicious ``filename`` parameters from
        breaking out of the ``root`` directory and leaking sensitive information
        to an attacker.

        Read-protected files or files outside of the ``root`` directory are
        answered with ``403 Access Denied``. Missing files result in a
        ``404 Not Found`` response. Conditional requests (``If-Modified-Since``,
        ``If-None-Match``) are answered with ``304 Not Modified`` whenever
        possible. ``HEAD`` and ``Range`` requests (used by download managers to
        check or continue partial downloads) are also handled automatically.
    """

    root = os.path.join(os.path.abspath(root), '')
    filename = os.path.abspath(os.path.join(root, filename.strip('/\\')))
    headers = headers.copy() if headers else {}
    getenv = request.environ.get

    if not filename.startswith(root):
        return HTTPError(403, "Access denied.")
    if not os.path.exists(filename) or not os.path.isfile(filename):
        return HTTPError(404, "File does not exist.")
    if not os.access(filename, os.R_OK):
        return HTTPError(403, "You do not have permission to access this file.")

    if mimetype is True:
        name = download if isinstance(download, str) else filename
        mimetype, encoding = mimetypes.guess_type(name)
        if encoding == 'gzip':
            mimetype = 'application/gzip'
        elif encoding: # e.g. bzip2 -> application/x-bzip2
            mimetype = 'application/x-' + encoding

    if charset and mimetype and 'charset=' not in mimetype \
        and (mimetype[:5] == 'text/' or mimetype == 'application/javascript'):
        mimetype += '; charset=%s' % charset

    if mimetype:
        headers['Content-Type'] = mimetype

    if download is True:
        download = os.path.basename(filename)

    if download:
        download = download.replace('"','')
        headers['Content-Disposition'] = 'attachment; filename="%s"' % download

    stats = os.stat(filename)
    headers['Content-Length'] = clen = stats.st_size
    headers['Last-Modified'] = email.utils.formatdate(stats.st_mtime, usegmt=True)
    headers['Date'] = email.utils.formatdate(time.time(), usegmt=True)

    if etag is None:
        etag = '%d:%d:%d:%d:%s' % (stats.st_dev, stats.st_ino, stats.st_mtime,
                                   clen, filename)
        etag = hashlib.sha1(tob(etag)).hexdigest()

    if etag:
        headers['ETag'] = etag
        check = getenv('HTTP_IF_NONE_MATCH')
        if check and check == etag:
            return HTTPResponse(status=304, **headers)

    ims = getenv('HTTP_IF_MODIFIED_SINCE')
    if ims:
        ims = parse_date(ims.split(";")[0].strip())
        if ims is not None and ims >= int(stats.st_mtime):
            return HTTPResponse(status=304, **headers)

    body = '' if request.method == 'HEAD' else open(filename, 'rb')

    headers["Accept-Ranges"] = "bytes"
    range_header = getenv('HTTP_RANGE')
    if range_header:
        ranges = list(parse_range_header(range_header, clen))
        if not ranges:
            return HTTPError(416, "Requested Range Not Satisfiable")
        offset, end = ranges[0]
        rlen = end - offset
        headers["Content-Range"] = "bytes %d-%d/%d" % (offset, end - 1, clen)
        headers["Content-Length"] = str(rlen)
        if body: body = _closeiter(_rangeiter(body, offset, rlen), body.close)
        return HTTPResponse(body, status=206, **headers)
    return HTTPResponse(body, **headers)

###############################################################################
# HTTP Utilities and MISC (TODO) ###############################################
###############################################################################


def debug(mode=True):
    """ Change the debug level.
    There is only one debug level supported at the moment."""
    global DEBUG
    if mode: warnings.simplefilter('default')
    DEBUG = bool(mode)


def http_date(value):
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        # aware datetime.datetime is converted to UTC time
        # naive datetime.datetime is treated as UTC time
        value = value.utctimetuple()
    elif isinstance(value, datedate):
        # datetime.date is naive, and is treated as UTC time
        value = value.timetuple()
    if not isinstance(value, (int, float)):
        # convert struct_time in UTC to UNIX timestamp
        value = calendar.timegm(value)
    return email.utils.formatdate(value, usegmt=True)


def parse_date(ims):
    """ Parse rfc1123, rfc850 and asctime timestamps and return UTC epoch. """
    try:
        ts = email.utils.parsedate_tz(ims)
        return calendar.timegm(ts[:8] + (0, )) - (ts[9] or 0)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def parse_auth(header):
    """ Parse rfc2617 HTTP authentication header string (basic) and return (user,pass) tuple or None"""
    try:
        method, data = header.split(None, 1)
        if method.lower() == 'basic':
            user, pwd = touni(base64.b64decode(tob(data))).split(':', 1)
            return user, pwd
    except (KeyError, ValueError):
        return None


def parse_range_header(header, maxlen=0):
    """ Yield (start, end) ranges parsed from a HTTP Range header. Skip
        unsatisfiable ranges. The end index is non-inclusive."""
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


#: Header tokenizer used by _parse_http_header()
_hsplit = re.compile('(?:(?:"((?:[^"\\\\]|\\\\.)*)")|([^;,=]+))([;,=]?)').findall

def _parse_http_header(h):
    """ Parses a typical multi-valued and parametrised HTTP header (e.g. Accept headers) and returns a list of values
        and parameters. For non-standard or broken input, this implementation may return partial results.
    :param h: A header string (e.g. ``text/html,text/plain;q=0.9,*/*;q=0.8``)
    :return: List of (value, params) tuples. The second element is a (possibly empty) dict.
    """
    values = []
    if '"' not in h:  # INFO: Fast path without regexp (~2x faster)
        for value in h.split(','):
            parts = value.split(';')
            values.append((parts[0].strip(), {}))
            for attr in parts[1:]:
                name, value = attr.split('=', 1)
                values[-1][1][name.strip().lower()] = value.strip()
    else:
        lop, key, attrs = ',', None, {}
        for quoted, plain, tok in _hsplit(h):
            value = plain.strip() if plain else quoted.replace('\\"', '"')
            if lop == ',':
                attrs = {}
                values.append((value, attrs))
            elif lop == ';':
                if tok == '=':
                    key = value
                else:
                    attrs[value.strip().lower()] = ''
            elif lop == '=' and key:
                attrs[key.strip().lower()] = value
                key = None
            lop = tok
    return values


def _parse_qsl(qs, encoding="utf8"):
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
    """ Compares two strings in a cryptographically safe way:
        Runtime is not affected by length of common prefix. """
    return not sum(0 if x == y else 1
                   for x, y in zip(a, b)) and len(a) == len(b)


def cookie_encode(data, key, digestmod=None):
    """ Encode and sign a pickle-able object. Return a (byte) string """
    depr(0, 13, "cookie_encode() will be removed soon.",
                "Do not use this API directly.")
    digestmod = digestmod or hashlib.sha256
    msg = base64.b64encode(pickle.dumps(data, -1))
    sig = base64.b64encode(hmac.new(tob(key), msg, digestmod=digestmod).digest())
    return b'!' + sig + b'?' + msg


def cookie_decode(data, key, digestmod=None):
    """ Verify and decode an encoded string. Return an object or None."""
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
    """ Return True if the argument looks like a encoded cookie."""
    depr(0, 13, "cookie_is_encoded() will be removed soon.",
                "Do not use this API directly.")
    return bool(data.startswith(b'!') and b'?' in data)


def html_escape(string):
    """ Escape HTML special characters ``&<>`` and quotes ``'"``. """
    return string.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')\
                 .replace('"', '&quot;').replace("'", '&#039;')


def html_quote(string):
    """ Escape and quote a string to be used as an HTTP attribute."""
    return '"%s"' % html_escape(string).replace('\n', '&#10;')\
                    .replace('\r', '&#13;').replace('\t', '&#9;')


def yieldroutes(func):
    """ Return a generator for routes that match the signature (name, args)
    of the func parameter. This may yield more than one route if the function
    takes optional keyword arguments. The output is best described by example::

        a()         -> '/a'
        b(x, y)     -> '/b/<x>/<y>'
        c(x, y=5)   -> '/c/<x>' and '/c/<x>/<y>'
        d(x=5, y=6) -> '/d' and '/d/<x>' and '/d/<x>/<y>'
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
    """ Shift path fragments from PATH_INFO to SCRIPT_NAME and vice versa.

        :return: The modified paths.
        :param script_name: The SCRIPT_NAME path.
        :param script_name: The PATH_INFO path.
        :param shift: The number of path fragments to shift. May be negative to
          change the shift direction. (default: 1)
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
    """ Callback decorator to require HTTP auth (basic).
        TODO: Add route(check_auth=...) parameter. """

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

# Shortcuts for common LitPyWeb methods.
# They all refer to the current default application.

def _try_close(obj):
    """ Call obj.close() if present and ignore exceptions """
    try:
        if hasattr(obj, 'close'):
            obj.close()
    except Exception:
        pass


def make_default_app_wrapper(name):
    """ Return a callable that relays calls to the current default app. """

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


###############################################################################
# Multipart Handling ###########################################################
###############################################################################
# cgi.FieldStorage was deprecated in Python 3.11 and removed in 3.13
# This implementation is based on https://github.com/defnull/multipart/
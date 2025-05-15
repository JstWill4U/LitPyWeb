from .wsgi import HTTPError, HTTPResponse
from ..utils.cli import response, request, tob
from ..utils.core_utils import parse_date, parse_range_header
from urllib.parse import urljoin
from ..utils.utilities import _closeiter
import os, mimetypes, email.utils, time, hashlib

###############################################################################
# 应用辅助函数 ###########################################################
###############################################################################

def abort(code=500, text='Unknown Error.'):
    """ 终止执行并抛出一个 HTTP 错误。 """
    raise HTTPError(code, text)


def redirect(url, code=None):
    """ 重定向请求，抛出一个 303 或 302 重定向错误，取决于 HTTP 协议版本。 """
    if not code:
        code = 303 if request.get('SERVER_PROTOCOL') == "HTTP/1.1" else 302
    res = response.copy(cls=HTTPResponse)
    res.status = code
    res.body = ""
    res.set_header('Location', urljoin(request.url, url))
    raise res


def _rangeiter(fp, offset, limit, bufsize=1024 * 1024):
    """ 从文件中按范围（offset ~ offset+limit）逐块读取数据并返回迭代器。 """
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
    """ 以安全的方式打开一个文件，并返回一个 HTTPResponse 对象用于发送给客户端。

        :param filename: 要发送的文件名称或路径，相对于 `root`。
        :param root: 文件查找的根目录，应为绝对路径。
        :param mimetype: 是否自动添加 Content-Type 头（默认：根据文件扩展名猜测）。
        :param download: 若为 True，则提示浏览器保存文件而非直接打开。也可传入自定义文件名字符串。
        :param charset: 用于 `text/*` 类型文件的字符集（默认：UTF-8）。
        :param etag: 提供预生成的 ETag，如果设为 False 则禁用 ETag（默认：自动生成）。
        :param headers: 附加的响应头字典。

        本函数提供了额外的安全机制，防止恶意路径突破 root 限制。

        - 无读取权限或路径非法时返回 403；
        - 文件不存在返回 404；
        - 支持条件请求（If-Modified-Since、If-None-Match）并自动返回 304；
        - 支持 HEAD 请求与 Range 请求（部分下载）。
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
        elif encoding: # 如 bzip2 -> application/x-bzip2
            mimetype = 'application/x-' + encoding

    # 自动添加 charset 到 text/* 或 JavaScript 类型
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
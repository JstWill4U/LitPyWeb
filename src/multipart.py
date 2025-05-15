from .wsgi import HTTPError
from ..utils.cli import tob
from io import BytesIO
from tempfile import NamedTemporaryFile
from ..utils.utilities import HeaderDict
from .server import _parse_http_header

###############################################################################
# Multipart 处理模块 ###########################################################
###############################################################################


class MultipartError(HTTPError):
    """ 多部分表单处理异常类，继承自 HTTPError，状态码为 400 """
    def __init__(self, msg):
        HTTPError.__init__(self, 400, "MultipartError: " + msg)


class _MultipartParser:
    """ 多部分表单解析器，仅可使用一次。 """
    def __init__(
        self,
        stream,
        boundary,
        content_length=-1,
        disk_limit=2 ** 30,
        mem_limit=2 ** 20,
        memfile_limit=2 ** 18,
        buffer_size=2 ** 16,
        charset="latin1",
    ):
        self.stream = stream
        self.boundary = boundary
        self.content_length = content_length
        self.disk_limit = disk_limit
        self.memfile_limit = memfile_limit
        self.mem_limit = min(mem_limit, self.disk_limit)
        self.buffer_size = min(buffer_size, self.mem_limit)
        self.charset = charset

        if not boundary:
            raise MultipartError("No boundary.")

        if self.buffer_size - 6 < len(boundary):  # "--boundary--\r\n"
            raise MultipartError("Boundary does not fit into buffer_size.")

    def _lineiter(self):
        """ 按行读取二进制流（以 \r\n 结尾），每次返回 (line, crlf) 元组。
            如果行超过 buffer_size，会被拆分为多个块。
        """

        read = self.stream.read
        maxread, maxbuf = self.content_length, self.buffer_size
        partial = b""  # Contains the last (partial) line

        while True:
            chunk = read(maxbuf if maxread < 0 else min(maxbuf, maxread))
            maxread -= len(chunk)
            if not chunk:
                if partial:
                    yield partial, b''
                break

            if partial:
                chunk = partial + chunk

            scanpos = 0
            while True:
                i = chunk.find(b'\r\n', scanpos)
                if i >= 0:
                    yield chunk[scanpos:i], b'\r\n'
                    scanpos = i + 2
                else: # CRLF not found
                    partial = chunk[scanpos:] if scanpos else chunk
                    break

            if len(partial) > maxbuf:
                yield partial[:-1], b""
                partial = partial[-1:]

    def parse(self):
        """ 返回一个 Multipart 迭代器（生成器），只能调用一次。 """

        lines, line = self._lineiter(), ""
        separator = b"--" + tob(self.boundary)
        terminator = separator + b"--"
        mem_used, disk_used = 0, 0  # 限制资源消耗，防止 DoS
        is_tail = False  # 标记上一行是否是非完整的续行

        # 跳过前导内容，直到第一个分隔符
        for line, nl in lines:
            if line in (separator, terminator):
                break
        else:
            raise MultipartError("Stream does not contain boundary")

        # 若第一个分隔符即为终止符，表示空的 multipart 数据
        if line == terminator:
            for _ in lines:
                raise MultipartError("Found data after empty multipart stream")
            return

        part_options = {
            "buffer_size": self.buffer_size,
            "memfile_limit": self.memfile_limit,
            "charset": self.charset,
        }
        part = _MultipartPart(**part_options)

        for line, nl in lines:
            if not is_tail and (line == separator or line == terminator):
                part.finish()
                if part.is_buffered():
                    mem_used += part.size
                else:
                    disk_used += part.size
                yield part
                if line == terminator:
                    break
                part = _MultipartPart(**part_options)
            else:
                is_tail = not nl  # The next line continues this one
                try:
                    part.feed(line, nl)
                    if part.is_buffered():
                        if part.size + mem_used > self.mem_limit:
                            raise MultipartError("Memory limit reached.")
                    elif part.size + disk_used > self.disk_limit:
                        raise MultipartError("Disk limit reached.")
                except MultipartError:
                    part.close()
                    raise
        else:
            part.close()

        if line != terminator:
            raise MultipartError("Unexpected end of multipart stream.")


class _MultipartPart:
    """ 表示一个 multipart 部分，包括其头信息和内容体 """
    def __init__(self, buffer_size=2 ** 16, memfile_limit=2 ** 18, charset="latin1"):
        self.headerlist = []
        self.headers = None
        self.file = False
        self.size = 0
        self._buf = b""
        self.disposition = None
        self.name = None
        self.filename = None
        self.content_type = None
        self.charset = charset
        self.memfile_limit = memfile_limit
        self.buffer_size = buffer_size

    def feed(self, line, nl=""):
        """ 接收数据输入并处理 """
        if self.file:
            return self.write_body(line, nl)
        return self.write_header(line, nl)

    def write_header(self, line, nl):
        """ 处理头部字段，若遇到空行则结束头部解析 """
        line = str(line, self.charset)

        if not nl:
            raise MultipartError("Unexpected end of line in header.")

        if not line.strip():  # blank line -> end of header segment
            self.finish_header()
        elif line[0] in " \t" and self.headerlist:
            name, value = self.headerlist.pop()
            self.headerlist.append((name, value + line.strip()))
        else:
            if ":" not in line:
                raise MultipartError("Syntax error in header: No colon.")

            name, value = line.split(":", 1)
            self.headerlist.append((name.strip(), value.strip()))

    def write_body(self, line, nl):
        """ 写入 body 内容（支持缓冲和溢出）"""
        if not line and not nl:
            return  # This does not even flush the buffer

        self.size += len(line) + len(self._buf)
        self.file.write(self._buf + line)
        self._buf = nl

        if self.content_length > 0 and self.size > self.content_length:
            raise MultipartError("Size of body exceeds Content-Length header.")

        if self.size > self.memfile_limit and isinstance(self.file, BytesIO):
            self.file, old = NamedTemporaryFile(mode="w+b"), self.file
            old.seek(0)

            copied, maxcopy, chunksize = 0, self.size, self.buffer_size
            read, write = old.read, self.file.write
            while copied < maxcopy:
                chunk = read(min(chunksize, maxcopy - copied))
                write(chunk)
                copied += len(chunk)

    def finish_header(self):
        """ 完成头部解析，初始化 file、headers、字段提取 """
        self.file = BytesIO()
        self.headers = HeaderDict(self.headerlist)
        content_disposition = self.headers.get("Content-Disposition")
        content_type = self.headers.get("Content-Type")

        if not content_disposition:
            raise MultipartError("Content-Disposition header is missing.")

        self.disposition, self.options = _parse_http_header(content_disposition)[0]
        self.name = self.options.get("name")
        if "filename" in self.options:
            self.filename = self.options.get("filename")
            if self.filename[1:3] == ":\\" or self.filename[:2] == "\\\\":
                self.filename = self.filename.split("\\")[-1] # ie6 bug

        self.content_type, options = _parse_http_header(content_type)[0] if content_type else (None, {})
        self.charset = options.get("charset") or self.charset

        self.content_length = int(self.headers.get("Content-Length", "-1"))

    def finish(self):
        """ 完成 body 部分，重置文件指针 """
        if not self.file:
            raise MultipartError("Incomplete part: Header section not closed.")
        self.file.seek(0)

    def is_buffered(self):
        """ 判断当前数据是否完全保存在内存中 """
        return isinstance(self.file, BytesIO)

    @property
    def value(self):
        """ 获取解码后的数据值 """
        """ Data decoded with the specified charset """
        return str(self.raw, self.charset)

    @property
    def raw(self):
        """ 获取未解码的原始数据 """
        """ Data without decoding """
        pos = self.file.tell()
        self.file.seek(0)

        try:
            return self.file.read()
        finally:
            self.file.seek(pos)

    def close(self):
        """ 关闭资源 """
        if self.file:
            self.file.close()
            self.file = False
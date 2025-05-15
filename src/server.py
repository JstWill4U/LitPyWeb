import re, email.utils, calendar
from datetime import date as datedate, datetime

def _wsgi_recode(src):
    """ 将 PEP-3333 规定的 latin1 编码字符串转换为 utf-8 编码字符串，并使用 surrogateescape 错误处理策略。 """
    if src.isascii():
        return src
    return src.encode('latin1').decode('utf8', 'surrogateescape')

def http_date(value):
    """ 将 datetime/date/时间戳/struct_time 转换为符合 HTTP 规范的日期字符串（RFC 1123 格式）。
    
    :param value: 支持 datetime、date、struct_time 或时间戳等多种格式。
    :return: 格式为 'Sun, 06 Nov 1994 08:49:37 GMT' 的字符串。
    """
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

# 正则表达式用于解析带参数的 HTTP header 项（如 Accept、Content-Type）
_hsplit = re.compile('(?:(?:"((?:[^"\\\\]|\\\\.)*)")|([^;,=]+))([;,=]?)').findall

def _parse_http_header(h):
    """ 解析典型的 HTTP 头字段值（如 Accept、Content-Type 等），支持参数与多个值。
    
    示例：
        输入: 'text/html,text/plain;q=0.9,*/*;q=0.8'
        输出: [('text/html', {}), ('text/plain', {'q': '0.9'}), ('*/*', {'q': '0.8'})]

    :param h: 头字段原始字符串
    :return: (值, 参数字典) 的列表
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
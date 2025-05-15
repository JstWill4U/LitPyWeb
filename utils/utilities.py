from collections.abc import MutableMapping as DictMixin
from ..src.wsgi import _hkey, _hval
from .cli import _wsgi_recode, makelist, DEBUG, cached_property, normalize
from ..src.control import load
import configparser, weakref, os, re
from ..src.LitPyWeb import LitPyWeb
from ..src.wsgi import HeaderProperty

###############################################################################
# 通用实体 #############################################################
###############################################################################


class MultiDict(DictMixin):
    """ 多值字典：支持一个键对应多个值，但默认行为与普通字典相同（返回最后一个值）。
        提供了额外方法访问所有值列表。
    """

    def __init__(self, *a, **k):
        self.dict = dict((k, [v]) for (k, v) in dict(*a, **k).items())

    def __len__(self):
        return len(self.dict)

    def __iter__(self):
        return iter(self.dict)

    def __contains__(self, key):
        return key in self.dict

    def __delitem__(self, key):
        del self.dict[key]

    def __getitem__(self, key):
        return self.dict[key][-1]

    def __setitem__(self, key, value):
        self.append(key, value)

    def keys(self):
        return self.dict.keys()

    def values(self):
        return (v[-1] for v in self.dict.values())

    def items(self):
        return ((k, v[-1]) for k, v in self.dict.items())

    def allitems(self):
        return ((k, v) for k, vl in self.dict.items() for v in vl)

    iterkeys = keys
    itervalues = values
    iteritems = items
    iterallitems = allitems

    def get(self, key, default=None, index=-1, type=None):
        """ 获取某个键的某个值。
        :param default: 如果键不存在或类型转换失败则返回
        :param index: 获取第几个值（默认最后一个）
        :param type: 类型转换函数，失败则返回默认值
        """
        try:
            val = self.dict[key][index]
            return type(val) if type else val
        except Exception:
            pass
        return default

    def append(self, key, value):
        """ 向该键追加一个新值。 """
        self.dict.setdefault(key, []).append(value)

    def replace(self, key, value):
        """ 用单个值替换该键的所有值。 """
        self.dict[key] = [value]

    def getall(self, key):
        """ 获取该键的所有值（列表）。 """
        return self.dict.get(key) or []

    # 为 WTForms 提供的别名
    getone = get
    getlist = getall


class FormsDict(MultiDict):
    """ 表单数据的多值字典封装，支持属性方式访问键值。
        缺失属性返回空字符串。

        注意：从 0.14 起，所有键值对都以 UTF-8 解码为字符串。
    """

    def decode(self):
        """ 已弃用：从 0.13 起，所有键值已自动解码为字符串。 """
        copy = FormsDict()
        for key, value in self.allitems():
            copy[key] = value
        return copy

    def getunicode(self, name, default=None):
        """ 已弃用：返回指定键的字符串值。"""
        return self.get(name, default)

    def __getattr__(self, name, default=str()):
        # 允许通过属性方式访问键（如 form.username）。
        if name.startswith('__') and name.endswith('__'):
            return super(FormsDict, self).__getattr__(name)
        return self.get(name, default=default)

class HeaderDict(MultiDict):
    """ 不区分大小写的 HTTP 头部字典。
        默认在设置新值时替换旧值，而非追加。 """

    def __init__(self, *a, **ka):
        self.dict = {}
        if a or ka: self.update(*a, **ka)

    def __contains__(self, key):
        return _hkey(key) in self.dict

    def __delitem__(self, key):
        del self.dict[_hkey(key)]

    def __getitem__(self, key):
        return self.dict[_hkey(key)][-1]

    def __setitem__(self, key, value):
        self.dict[_hkey(key)] = [_hval(value)]

    def append(self, key, value):
        self.dict.setdefault(_hkey(key), []).append(_hval(value))

    def replace(self, key, value):
        self.dict[_hkey(key)] = [_hval(value)]

    def getall(self, key):
        return self.dict.get(_hkey(key)) or []

    def get(self, key, default=None, index=-1):
        return MultiDict.get(self, _hkey(key), default, index)

    def filter(self, names):
        for name in (_hkey(n) for n in names):
            if name in self.dict:
                del self.dict[name]


class WSGIHeaderDict(DictMixin):
    """ WSGI 环境字典的 HTTP_* 字段封装器，支持大小写无关访问。
    """
    cgikeys = ('CONTENT_TYPE', 'CONTENT_LENGTH')

    def __init__(self, environ):
        self.environ = environ

    def _ekey(self, key):
        """ Translate header field name to CGI/WSGI environ key. """
        key = key.replace('-', '_').upper()
        if key in self.cgikeys:
            return key
        return 'HTTP_' + key

    def raw(self, key, default=None):
        """ Return the header value as is (not utf8-translated). """
        return self.environ.get(self._ekey(key), default)

    def __getitem__(self, key):
        return _wsgi_recode(self.environ[self._ekey(key)])

    def __setitem__(self, key, value):
        raise TypeError("%s is read-only." % self.__class__)

    def __delitem__(self, key):
        raise TypeError("%s is read-only." % self.__class__)

    def __iter__(self):
        for key in self.environ:
            if key[:5] == 'HTTP_':
                yield _hkey(key[5:])
            elif key in self.cgikeys:
                yield _hkey(key)

    def keys(self):
        return [x for x in self]

    def __len__(self):
        return len(self.keys())

    def __contains__(self, key):
        return self._ekey(key) in self.environ

_UNSET = object()

class ConfigDict(dict):
    """ 多功能配置容器，支持如下特性：
    - 类字典操作；
    - 命名空间键（如 'db.timeout'）；
    - 配置覆盖层（overlay）；
    - 配置项元信息存储（meta，如说明文字、验证函数等）；
    - 快速读取优化（读性能接近内建 dict）；
    """

    __slots__ = ('_meta', '_change_listener', '_overlays', '_virtual_keys', '_source', '__weakref__')

    def __init__(self):
        self._meta = {} 
        # 存储 key 的元数据，如 help、validate、filter 等
        self._change_listener = []
        # 配置变更监听器
        self._overlays = []
        # 子 overlay 的弱引用列表
        self._source = None
        # overlay 的来源（父配置）
        self._virtual_keys = set()
        # 从父配置继承的 key 集合（虚拟）

    def load_module(self, name, squash=True):
        """从模块加载配置（导入模块后将所有大写变量作为配置项导入）。

        :param name: 模块名
        :param squash: 如果为 True，则嵌套 dict 会被展开为命名空间形式（默认 True）
        """
        config_obj = load(name)
        obj = {key: getattr(config_obj, key)
               for key in dir(config_obj) if key.isupper()}

        if squash:
            self.load_dict(obj)
        else:
            self.update(obj)
        return self

    def load_config(self, filename, **options):
        """ 从 INI 配置文件中加载配置。

        支持使用 section 做命名空间（例如 [db] -> db.timeout）。
        特殊段名：
        - [LitPyWeb] 或 [ROOT] 表示根命名空间
        - [DEFAULT] 表示默认值

        :param filename: 文件路径或路径列表
        :param options: 传递给 configparser.ConfigParser 的参数

        """
        options.setdefault('allow_no_value', True)
        options.setdefault('interpolation', configparser.ExtendedInterpolation())
        conf = configparser.ConfigParser(**options)
        conf.read(filename)
        for section in conf.sections():
            for key in conf.options(section):
                value = conf.get(section, key)
                if section not in ('LitPyWeb', 'ROOT'):
                    key = section + '.' + key
                self[key.lower()] = value
        return self

    def load_dict(self, source, namespace=''):
        """ 从嵌套 dict 加载配置项，并使用命名空间展平。

        >>> c = ConfigDict()
        >>> c.load_dict({'server': {'timeout': 5}})
        -> {'server.timeout': 5}
        """
        for key, value in source.items():
            if isinstance(key, str):
                nskey = (namespace + '.' + key).strip('.')
                if isinstance(value, dict):
                    self.load_dict(value, namespace=nskey)
                else:
                    self[nskey] = value
            else:
                raise TypeError('Key has type %r (not a string)' % type(key))
        return self

    def update(self, *a, **ka):
        """ 与 dict.update 相似，但如果第一个参数是字符串，则作为命名空间前缀。

        >>> c.update('db', host='localhost', port=3306)
        -> {'db.host': ..., 'db.port': ...}
        """
        prefix = ''
        if a and isinstance(a[0], str):
            prefix = a[0].strip('.') + '.'
            a = a[1:]
        for key, value in dict(*a, **ka).items():
            self[prefix + key] = value

    def setdefault(self, key, value=None):
        if key not in self:
            self[key] = value
        return self[key]

    def __setitem__(self, key, value):
        if not isinstance(key, str):
            raise TypeError('Key has type %r (not a string)' % type(key))

        self._virtual_keys.discard(key)

        value = self.meta_get(key, 'filter', lambda x: x)(value)
        if key in self and self[key] is value:
            return

        self._on_change(key, value)
        dict.__setitem__(self, key, value)

        for overlay in self._iter_overlays():
            overlay._set_virtual(key, value)

    def __delitem__(self, key):
        if key not in self:
            raise KeyError(key)
        if key in self._virtual_keys:
            raise KeyError("Virtual keys cannot be deleted: %s" % key)

        if self._source and key in self._source:
            # 重置为虚拟值
            dict.__delitem__(self, key)
            self._set_virtual(key, self._source[key])
        else:
            self._on_change(key, None)
            dict.__delitem__(self, key)
            for overlay in self._iter_overlays():
                overlay._delete_virtual(key)

    def _set_virtual(self, key, value):
        """ 设置虚拟键（从父配置继承）。 """
        if key in self and key not in self._virtual_keys:
            return  # 本地已定义该键，跳过

        self._virtual_keys.add(key)
        if key in self and self[key] is not value:
            self._on_change(key, value)
        dict.__setitem__(self, key, value)
        for overlay in self._iter_overlays():
            overlay._set_virtual(key, value)

    def _delete_virtual(self, key):
        """ 删除虚拟键。"""
        if key not in self._virtual_keys:
            return

        if key in self:
            self._on_change(key, None)
        dict.__delitem__(self, key)
        self._virtual_keys.discard(key)
        for overlay in self._iter_overlays():
            overlay._delete_virtual(key)

    def _on_change(self, key, value):
        """ 调用注册的变更监听器。"""
        for cb in self._change_listener:
            if cb(self, key, value):
                return True

    def _add_change_listener(self, func):
        """ 添加变更监听器函数。"""
        self._change_listener.append(func)
        return func

    def meta_get(self, key, metafield, default=None):
        """ 获取 key 对应的 meta 属性值。"""
        return self._meta.get(key, {}).get(metafield, default)

    def meta_set(self, key, metafield, value):
        """ 设置 key 的 meta 属性值。支持共享元信息。"""
        self._meta.setdefault(key, {})[metafield] = value

    def meta_list(self, key):
        """ 返回该 key 拥有的所有 meta 字段名称。 """
        return self._meta.get(key, {}).keys()

    def _define(self, key, default=_UNSET, help=_UNSET, validate=_UNSET):
        """ 用于插件快速定义配置项（非稳定 API）。 """
        if default is not _UNSET:
            self.setdefault(key, default)
        if help is not _UNSET:
            self.meta_set(key, 'help', help)
        if validate is not _UNSET:
            self.meta_set(key, 'validate', validate)

    def _iter_overlays(self):
        """ 生成所有存活的子 overlay 引用。"""
        for ref in self._overlays:
            overlay = ref()
            if overlay is not None:
                yield overlay

    def _make_overlay(self):
        """ 创建一个 overlay（覆盖层配置字典），用于在不影响原配置的情况下覆盖或扩展配置。

        特性：
        - overlay 读取原配置内容作为初始值；
        - overlay 设置键值不会影响原始配置；
        - 支持链式更新传播；
        - 读写性能与 dict 相当。

        用于：每个路由使用独立的 overlay 来设置参数。
        """
        # Cleanup dead references
        self._overlays[:] = [ref for ref in self._overlays if ref() is not None]

        overlay = ConfigDict()
        overlay._meta = self._meta
        overlay._source = self
        self._overlays.append(weakref.ref(overlay))
        for key in self:
            overlay._set_virtual(key, self[key])
        return overlay

class AppStack(list):
    """ 应用栈：一个栈结构的列表。调用时返回当前默认应用实例。 """

    def __call__(self):
        """ 返回当前默认的应用实例。 """
        return self.default

    def push(self, value=None):
        """ 向栈中推入一个新的 LitPyWeb 应用实例。 """
        if not isinstance(value, LitPyWeb):
            value = LitPyWeb()
        self.append(value)
        return value
    new_app = push

    @property
    def default(self):
        try:
            return self[-1]
        except IndexError:
            return self.push()


class WSGIFileWrapper:
    """ 对文件对象进行 WSGI 兼容封装，支持迭代读取。"""
    def __init__(self, fp, buffer_size=1024 * 64):
        self.fp, self.buffer_size = fp, buffer_size
        for attr in 'fileno', 'close', 'read', 'readlines', 'tell', 'seek':
            if hasattr(fp, attr): setattr(self, attr, getattr(fp, attr))

    def __iter__(self):
        buff, read = self.buffer_size, self.read
        part = read(buff)
        while part:
            yield part
            part = read(buff)


class _closeiter:
    """ 为不支持属性赋值的迭代器（如 itertools）封装 `.close()` 回调支持。 """

    def __init__(self, iterator, close=None):
        self.iterator = iterator
        self.close_callbacks = makelist(close)

    def __iter__(self):
        return iter(self.iterator)

    def close(self):
        for func in self.close_callbacks:
            func()


def _try_close(obj):
    """ 安全地关闭对象（如果有 `.close()` 方法）。 """
    try:
        if hasattr(obj, 'close'):
            obj.close()
    except Exception:
        pass


class ResourceManager:
    """ 管理资源路径、打开资源文件、缓存文件查找结果。

    :param base: 添加路径的默认基础路径。
    :param opener: 打开文件使用的函数（默认是内建 open）。
    :param cachemode: 缓存模式，可选值 'all', 'found', 'none'。
    """

    def __init__(self, base='./', opener=open, cachemode='all'):
        self.opener = opener
        self.base = base
        self.cachemode = cachemode

        #: A list of search paths. See :meth:`add_path` for details.
        self.path = []
        #: A cache for resolved paths. ``res.cache.clear()`` clears the cache.
        self.cache = {}

    def add_path(self, path, base=None, index=None, create=False):
        """ 添加资源搜索路径。

        :param path: 路径（相对路径将转换为绝对路径并标准化）。
        :param base: 作为相对路径基准的路径，默认使用 self.base。
        :param index: 插入到路径列表的指定位置（默认添加到末尾）。
        :param create: 如果路径不存在则自动创建（默认 False）。
        :return: 是否添加成功（路径是否存在）。
        """
        base = os.path.abspath(os.path.dirname(base or self.base))
        path = os.path.abspath(os.path.join(base, os.path.dirname(path)))
        path += os.sep
        if path in self.path:
            self.path.remove(path)
        if create and not os.path.isdir(path):
            os.makedirs(path)
        if index is None:
            self.path.append(path)
        else:
            self.path.insert(index, path)
        self.cache.clear()
        return os.path.exists(path)

    def __iter__(self):
        """ 遍历所有注册路径中的文件（递归）。 """
        search = self.path[:]
        while search:
            path = search.pop()
            if not os.path.isdir(path): continue
            for name in os.listdir(path):
                full = os.path.join(path, name)
                if os.path.isdir(full): search.append(full)
                else: yield full

    def lookup(self, name):
        """ 查找资源文件，返回其绝对路径或 None。
        查找结果会缓存，加快后续访问。

        :param name: 文件名
        :return: 绝对路径或 None """
        if name not in self.cache or DEBUG:
            for path in self.path:
                fpath = os.path.join(path, name)
                if os.path.isfile(fpath):
                    if self.cachemode in ('all', 'found'):
                        self.cache[name] = fpath
                    return fpath
            if self.cachemode == 'all':
                self.cache[name] = None
        return self.cache[name]

    def open(self, name, mode='r', *args, **kwargs):
        """ 查找并打开资源文件。

        :param name: 资源名称
        :param mode: 打开模式（如 'r', 'rb'）
        :return: 文件对象或抛出 IOError """
        fname = self.lookup(name)
        if not fname: raise IOError("Resource %r not found." % name)
        return self.opener(fname, mode=mode, *args, **kwargs)


class FileUpload:
    """ 表单上传文件的包装器，支持文件属性、头部访问和保存操作。"""

    def __init__(self, fileobj, name, filename, headers=None):
        self.file = fileobj
        # 文件对象（BytesIO 或临时文件）
        self.name = name
        # 表单字段名称
        self.raw_filename = filename
        # 原始上传文件名（可能不安全）
        self.headers = HeaderDict(headers) if headers else HeaderDict()

    content_type = HeaderProperty('Content-Type')
    content_length = HeaderProperty('Content-Length', reader=int, default=-1)

    def get_header(self, name, default=None):
        """ 获取该文件段中的某个 HTTP 头部字段。 """
        return self.headers.get(name, default)

    @cached_property
    def filename(self):
        """ 获取安全文件名（清洗非法字符、标准化、限制长度）。
        """
        fname = self.raw_filename
        fname = normalize('NFKD', fname)
        fname = fname.encode('ASCII', 'ignore').decode('ASCII')
        fname = os.path.basename(fname.replace('\\', os.path.sep))
        fname = re.sub(r'[^a-zA-Z0-9-_.\s]', '', fname).strip()
        fname = re.sub(r'[-\s]+', '-', fname).strip('.-')
        return fname[:255] or 'empty'

    def _copy_file(self, fp, chunk_size=2 ** 16):
        read, write, offset = self.file.read, fp.write, self.file.tell()
        while 1:
            buf = read(chunk_size)
            if not buf: break
            write(buf)
        self.file.seek(offset)

    def save(self, destination, overwrite=False, chunk_size=2 ** 16):
        """ 保存文件到磁盘或复制到文件指针对象。

        :param destination: 路径（字符串）或文件对象
        :param overwrite: 是否允许覆盖文件
        :param chunk_size: 每次读写的数据量（默认 64KB）
        """
        if isinstance(destination, str):  # Except file-likes here
            if os.path.isdir(destination):
                destination = os.path.join(destination, self.filename)
            if not overwrite and os.path.exists(destination):
                raise IOError('File exists.')
            with open(destination, 'wb') as fp:
                self._copy_file(fp, chunk_size)
        else:
            self._copy_file(destination, chunk_size)
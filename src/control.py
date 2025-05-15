import sys, os, tempfile, time, threading
import _thread as thread
from ..utils.cli import default_app, _stderr, __version__
from ..utils.core_utils import debug
from .server_adapter import server_names, ServerAdapter
from traceback import print_exc

###############################################################################
# Application Control ##########################################################
###############################################################################


def load(target, **namespace):
    """ 导入模块或从模块中获取对象。

        * ``package.module`` 返回模块对象。
        * ``pack.mod:name`` 返回模块中名为 `name` 的对象。
        * ``pack.mod:func()`` 执行函数并返回结果。

        最后一种形式支持任意表达式。通过关键字参数可传入局部变量。
        示例：``import_string('re:compile(x)', x='[a-z]')``
    """
    module, target = target.split(":", 1) if ':' in target else (target, None)
    if module not in sys.modules: __import__(module)
    if not target: return sys.modules[module]
    if target.isalnum(): return getattr(sys.modules[module], target)
    package_name = module.split('.')[0]
    namespace[package_name] = sys.modules[package_name]
    return eval('%s.%s' % (module, target), namespace)


def load_app(target):
    """ 从模块中加载一个 LitPyWeb 应用，并确保导入过程不会影响当前默认应用。
        返回独立的应用对象。详见 `load()` 的参数说明。 """
    global NORUN
    NORUN, nr_old = True, NORUN
    tmp = default_app.push()  # Create a new "default application"
    try:
        rv = load(target)  # Import the target module
        return rv if callable(rv) else tmp
    finally:
        default_app.remove(tmp)  # Remove the temporary added default application
        NORUN = nr_old


_debug = debug


def run(app=None,
        server='wsgiref',
        host='127.0.0.1',
        port=8080,
        interval=1,
        reloader=False,
        quiet=False,
        plugins=None,
        debug=None,
        config=None, **kargs):
    """ 启动服务器实例。该方法会阻塞直到服务器终止。

        :param app: WSGI 应用对象或支持的目标字符串（默认为 default_app）。
        :param server: 使用的服务器适配器。支持字符串名或 ServerAdapter 子类。
        :param host: 绑定的服务器地址，如使用 0.0.0.0 则监听所有网卡。
        :param port: 绑定的端口号，低于 1024 的端口需管理员权限。
        :param reloader: 是否开启自动重载（默认 False）。
        :param interval: 自动重载检测间隔（秒）。
        :param quiet: 是否静默运行，不输出标准日志（默认 False）。
        :param config: 应用配置字典。
        :param plugins: 要安装的插件列表。
        :param kargs: 传递给服务器适配器的其他参数。
     """
    if NORUN: return
    if reloader and not os.environ.get('LitPyWeb_CHILD'):
        import subprocess
        fd, lockfile = tempfile.mkstemp(prefix='LitPyWeb.', suffix='.lock')
        environ = os.environ.copy()
        environ['LitPyWeb_CHILD'] = 'true'
        environ['LitPyWeb_LOCKFILE'] = lockfile
        args = [sys.executable] + sys.argv
        # 如果通过 `python -m` 加载包，则需还原 sys.argv，避免导入出错
        if getattr(sys.modules.get('__main__'), '__package__', None):
            args[1:1] = ["-m", sys.modules['__main__'].__package__]

        try:
            os.close(fd)
            while os.path.exists(lockfile):
                p = subprocess.Popen(args, env=environ)
                while p.poll() is None:
                    os.utime(lockfile, None)  # 向子进程表明主进程仍存活
                    time.sleep(interval)
                if p.returncode == 3:  # 子进程请求重启
                    continue
                sys.exit(p.returncode)
        except KeyboardInterrupt:
            pass
        finally:
            if os.path.exists(lockfile):
                os.unlink(lockfile)
        return

    try:
        if debug is not None: _debug(debug)
        app = app or default_app()
        if isinstance(app, str):
            app = load_app(app)
        if not callable(app):
            raise ValueError("Application is not callable: %r" % app)

        for plugin in plugins or []:
            if isinstance(plugin, str):
                plugin = load(plugin)
            app.install(plugin)

        if config:
            app.config.update(config)

        if server in server_names:
            server = server_names.get(server)
        if isinstance(server, str):
            server = load(server)
        if isinstance(server, type):
            server = server(host=host, port=port, **kargs)
        if not isinstance(server, ServerAdapter):
            raise ValueError("Unknown or unsupported server: %r" % server)

        server.quiet = server.quiet or quiet
        if not server.quiet:
            _stderr("LitPyWeb v%s server starting up (using %s)..." %
                    (__version__, repr(server)))
            _stderr("Listening on %s" % server._listen_url)
            _stderr("Hit Ctrl-C to quit.\n")

        if reloader:
            lockfile = os.environ.get('LitPyWeb_LOCKFILE')
            bgcheck = FileCheckerThread(lockfile, interval)
            with bgcheck:
                server.run(app)
            if bgcheck.status == 'reload':
                sys.exit(3)
        else:
            server.run(app)
    except KeyboardInterrupt:
        pass
    except (SystemExit, MemoryError):
        raise
    except:
        if not reloader: raise
        if not getattr(server, 'quiet', quiet):
            print_exc()
        time.sleep(interval)
        sys.exit(3)


class FileCheckerThread(threading.Thread):
    """ 文件变动检测线程。 
        该线程在检测到以下任一情况时会中断主线程：
        - 任一模块文件发生变更；
        - 锁文件被删除或超时。
    """

    def __init__(self, lockfile, interval):
        threading.Thread.__init__(self)
        self.daemon = True
        self.lockfile, self.interval = lockfile, interval
        #: 线程状态：可为 'reload', 'error', 'exit'
        self.status = None

    def run(self):
        exists = os.path.exists
        mtime = lambda p: os.stat(p).st_mtime
        files = {}

        for module in list(sys.modules.values()):
            path = getattr(module, '__file__', '') or ''
            if path[-4:] in ('.pyo', '.pyc'): path = path[:-1]
            if path and exists(path): files[path] = mtime(path)

        while not self.status:
            if not exists(self.lockfile)\
            or mtime(self.lockfile) < time.time() - self.interval - 5:
                self.status = 'error'
                thread.interrupt_main()
            for path, lmtime in list(files.items()):
                if not exists(path) or mtime(path) > lmtime:
                    self.status = 'reload'
                    thread.interrupt_main()
                    break
            time.sleep(self.interval)

    def __enter__(self):
        self.start()

    def __exit__(self, exc_type, *_):
        if not self.status: self.status = 'exit'  # 正常退出
        self.join()
        return exc_type is not None and issubclass(exc_type, KeyboardInterrupt)
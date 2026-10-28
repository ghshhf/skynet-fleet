"""单实例：第二次双击不该开出第二个窗口，而应该把已经在跑的那个叫出来。

## 为什么不是"再开一个也无所谓"

这个程序起的不是一次性的窗口，而是**一个常驻的服务**：它占端口、持有配对码、
连着手机、跑着心跳。开第二份的后果不是"多一个窗口"，是两套服务抢同一个端口、
两台机器各自认为自己在管编队、审批弹窗出现在你看不见的那个窗口里。

## 两道锁，各管一件事

* **互斥体（Windows）/ 端口占用（其他平台）**：回答"我是不是唯一的那一个"。
  进程哪怕是崩的，互斥体也随进程句柄自动释放——不会留下"僵尸锁"。
* **UDP 回环口**：回答"把那个已经在跑的窗口叫出来"。
  只绑 `127.0.0.1`，报文是固定暗号，收到就回 ACK 并触发回调。
  没有它，用户双击后只会看到一个"已经在运行"的提示框，然后**找不到那个窗口**
  ——窗口可能被关到托盘里、被最小化、被拖到屏幕外。那比直接不给启动更难受。

为什么不走 HTTP 唤起：本地服务的令牌是随机的一次性令牌，**故意不落盘**
（见 app.py 顶部的说明）。为了唤起窗口而把凭据写到盘上，是拿安全换方便。

## 降级

唤起收不到（端口被别的程序占了、上一个实例卡死）时，`poke()` 返回 False，
调用方据此说人话："已经有一个在跑，但我叫不醒它，去任务管理器看看。"
——不静默、不装成功。
"""
import ctypes
import hashlib
import os
import socket
import sys
import threading
import zlib

PORT_BASE = 47100
PORT_SPAN = 80
POKE = b"FLEET-GUI-SHOW"
ACK = b"FLEET-GUI-OK"
MUTEX_PREFIX = "Local\\DeviceFleetGui-"


def _default_key():
    """同一份代码目录 = 同一个应用。装在两个目录就当两个应用，互不干扰。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.normcase(os.path.abspath(root))


def port_of(key):
    """暗号端口由 key 算出：同一份安装永远同一个口，不用配置文件。"""
    return PORT_BASE + (zlib.crc32(key.encode("utf-8")) % PORT_SPAN)


class SingleInstance:
    def __init__(self, key=None):
        self.key = key or _default_key()
        self.port = port_of(self.key)
        self._mutex = None
        self._sock = None
        self._thread = None
        self._on_show = None
        self._running = threading.Event()
        self.last_error = ""

    # ---------- 拿锁 ----------

    def acquire(self):
        """True = 我是唯一实例，继续启动；False = 已经有一个在跑。"""
        if sys.platform == "win32":
            got = self._acquire_mutex()
            # 互斥体句柄拿不到（受限环境）时退回端口占用法，仍然只有一个能过。
            return got if got is not None else self._acquire_socket()

    def _acquire_mutex(self):
        """返回 True/False；环境不支持时返回 None 交给调用方降级。"""
        try:
            k32 = ctypes.windll.kernel32
        except Exception as e:                       # 非 Windows 或受限
            self.last_error = "%s: %s" % (type(e).__name__, e)
            return None
        name = MUTEX_PREFIX + hashlib.sha1(
            self.key.encode("utf-8")).hexdigest()[:16]
        h = k32.CreateMutexW(None, True, name)
        if not h:
            self.last_error = "CreateMutexW 失败"
            return None
        # ERROR_ALREADY_EXISTS = 183：名字已存在 = 已经有一个在跑。
        if k32.GetLastError() == 183:
            k32.CloseHandle(h)
            return False
        self._mutex = h          # 故意不关：进程活着锁就在，进程死了系统自动放
        return True

    def _acquire_socket(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("127.0.0.1", self.port))
        except OSError as e:
            s.close()
            self.last_error = "端口 %d 被占用（%s）" % (self.port, e)
            return False
        self._sock = s
        return True

    # ---------- 被叫醒那一侧 ----------

    def listen(self, on_show):
        """后台等唤起请求；收到就回 ACK 并调 on_show()。

        返回 True 表示真的在听。返回 False 不是致命错误（互斥体已经保证了唯一），
        只是"这次唤起可能叫不醒我"，要如实告诉主人。
        """
        self._on_show = on_show
        if self._sock is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.bind(("127.0.0.1", self.port))
            except OSError as e:
                s.close()
                self.last_error = "唤起端口 %d 起不来（%s）" % (self.port, e)
                return False
            self._sock = s
        self._sock.settimeout(0.4)
        self._running.set()

        def loop():
            while self._running.is_set():
                try:
                    data, addr = self._sock.recvfrom(64)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if data.strip() != POKE:
                    continue
                try:
                    self._sock.sendto(ACK, addr)
                except OSError:
                    pass
                cb = self._on_show
                if cb is not None:
                    try:
                        cb()
                    except Exception:
                        pass

        self._thread = threading.Thread(target=loop, name="fleet-poke", daemon=True)
        self._thread.start()
        return True

    # ---------- 叫醒别人那一侧 ----------

    def poke(self, timeout=1.2, tries=3):
        """把已经在跑的那个叫出来。True = 它回话了。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout / max(1, tries))
        try:
            for _ in range(tries):
                try:
                    s.sendto(POKE, ("127.0.0.1", self.port))
                except OSError:
                    continue
                try:
                    data, _addr = s.recvfrom(16)
                except socket.timeout:
                    continue
                except OSError:
                    return False
                if data.strip() == ACK:
                    return True
            return False
        finally:
            s.close()

    # ---------- 收尾 ----------

    def release(self):
        self._running.clear()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._mutex is not None:
            try:
                ctypes.windll.kernel32.CloseHandle(self._mutex)
            except Exception:
                pass
            self._mutex = None


def probe_running(key=None):
    """给别的模块问一句"现在有实例在跑吗"，不产生副作用（不唤起窗口）。"""
    inst = SingleInstance(key)
    if sys.platform == "win32":
        got = inst._acquire_mutex()
        if got is True:
            inst.release()
            return False
        if got is False:
            return True
    return inst._acquire_socket() is False

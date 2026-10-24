"""天网·反向隧道（手机主动出站反连家里的常开节点）。

为什么是「反向」：手机几乎永远在 NAT / 蜂窝 CGNAT 后面，外面主动连不进来；
但手机**出站**永远通。所以让手机主动拨家里，隧道由内向外建——这就是你说的
「逆向」，也是打破「方寸之间」的唯一稳法（不依赖你家有没有公网 IP）。

设计纪律（与项目铁律一致）：
  · **隧道只做传输，手机本地执行**。手机收到 CALL，在自己的本地 registry 跑能力
    （android_phone / acquire / creative 全复用），只把 DeviceResult 回传。
    → 已建的 L2–L5、红外、场景宏**一个不用重写**就能远程用。
  · **鉴权复用 device_auth 配对**（6 位码 → sha256 secret，电脑重启不变）。
    只认自己配对的设备；错密钥/未知 id 一律 403，不区分（防枚举）。
  · **加密交给入口层**：relay 本身明文跑在内层，由 cloudflared（TLS 终止）或
    Tailscale（wireguard）提供传输加密。绝不把明文 adb / 凭据直接裸奔公网——
    那等于全网开放 root 壳（cleanyourtv/REMOTE.md 的血训）。
  · 入口可插拔：relay 只绑 host:port（默认 127.0.0.1）。想让外面连，用
    cloudflared 打洞 / frp / 路由器端口转发 / Tailscale 把那个端口暴露出去皆可。
  · 断线自动重连；会话状态变更回调，供编队自动增删远程设备。

复用：common.py 的长度前缀 JSON 帧（带每连接写锁）+ AUTH/REGISTER 语义。
"""
import json
import os
import socket
import struct
import sys
import threading
import time

# ---- 复用 common 帧协议（仓库根目录有一份，保证可 import） ----
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
import common  # noqa: E402

from .device_auth import DeviceVault  # noqa: E402

# ---- 本层新增消息类型（在 common 基础语义上扩展） ----
CALL = "call"                 # home -> phone:  {id, cap, args, device}
CALL_RESULT = "call_result"   # phone -> home:  {id, ok, code, data?, err?, ...}
DENY = "deny"                 # 通用拒绝（auth 失败等）

RELAY_STATE_PATH = os.path.join(
    os.path.expanduser("~/.fleet"), "remote_relay.json")


def _ensure_path():
    d = os.path.dirname(RELAY_STATE_PATH)
    if d:
        os.makedirs(d, exist_ok=True)


def save_relay_state(host, port, token=""):
    try:
        _ensure_path()
        with open(RELAY_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"host": host, "port": port, "token": token or ""}, f)
    except OSError:
        pass


def load_relay_state():
    try:
        with open(RELAY_STATE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("port"):
            return d
    except (OSError, ValueError):
        pass
    return None


class RelayServer:
    """家里常开节点：手机拨进来，登记能力，承接 CALL 转发。

    executor 不在本端——本端只持有「会话 → 连接」的映射，把 CALL 发给对应手机，
    手机本地执行后回 CALL_RESULT。这样天网所有手机能力天然远程可用。
    """

    def __init__(self, host="127.0.0.1", port=0, vault=None, token=""):
        self.host = host
        self.port = port
        self.token = token or ""
        self.vault = vault or DeviceVault()     # 复用配对库做鉴权
        self._srv = None
        self._thread = None
        self._stop = threading.Event()
        self._lk = threading.Lock()
        # device_id -> {conn, caps:set, last_seen, alive}
        self._sessions = {}
        self._pending = {}                       # call id -> future(dict)
        self._call_seq = 0
        self._on_change = None                  # 会话增删回调（编队同步用）

    # ---------------- 生命周期 ----------------

    def start(self, host=None, port=None):
        if host is not None:
            self.host = host
        if port is not None:
            self.port = port
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self._srv.listen(64)
        self.port = self._srv.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        save_relay_state(self.host, self.port, self.token)
        return self.port

    @property
    def running(self):
        return self._srv is not None

    def stop(self):
        self._stop.set()
        try:
            if self._srv:
                self._srv.close()
        except Exception:
            pass

    def set_on_change(self, cb):
        """会话增删时回调（编队用它自动增删 RemotePhone 设备）。"""
        self._on_change = cb

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_session, args=(conn,),
                            daemon=True).start()

    # ---------------- 会话处理 ----------------

    def _handle_session(self, conn):
        # 可选的入口 token（cloudflared/frp 暴露到公网时挡误连/扫描）
        did = ""                      # finally 里要用，先置空防未绑定
        registered = False           # 只有真正注册成功才允许在 finally 清理会话
        try:
            conn.settimeout(30.0)
            # 1) AUTH（手机持配对 secret 拨入）
            msg = common.recv_msg(conn)
            if not msg or msg.get("t") != common.AUTH:
                common.send_msg(conn, {"t": DENY, "reason": "期望 AUTH"})
                return
            did = msg.get("device_id", "")
            sec = msg.get("secret", "")
            # token 模式：公网暴露时再加一层弱共享密钥
            if self.token and msg.get("token") != self.token:
                common.send_msg(conn, {"t": common.AUTH_FAIL,
                                       "reason": "token 不匹配"})
                return
            pub = self.vault.verify(did, sec)
            if pub is None:
                # 不区分「id 不存在 / 密钥错」：对外都 403（防枚举）
                common.send_msg(conn, {"t": common.AUTH_FAIL,
                                       "reason": "认证失败"})
                return
            common.send_msg(conn, {"t": common.AUTH_OK, "device_id": did})

            # 2) REGISTER（手机上报能力）
            reg = common.recv_msg(conn)
            if not reg or reg.get("t") != common.REGISTER:
                return
            caps = set(reg.get("capabilities") or [])
            with self._lk:
                self._sessions[did] = {"conn": conn, "caps": caps,
                                       "last_seen": time.time(), "alive": True}
                registered = True
            if self._on_change:
                try:
                    self._on_change()
                except Exception:
                    pass

            # 3) 主循环：收 CALL_RESULT，匹配 pending future
            buf_id = 0
            while True:
                m = common.recv_msg(conn)
                if not m:
                    break
                if m.get("t") == CALL_RESULT:
                    cid = m.get("id")
                    with self._lk:
                        fut = self._pending.pop(cid, None)
                    if fut is not None:
                        fut["data"] = m
                        fut["event"].set()
                elif m.get("t") == common.HEARTBEAT:
                    with self._lk:
                        if did in self._sessions:
                            self._sessions[did]["last_seen"] = time.time()
        except Exception as e:
            print(f"[remote-link] 会话主循环异常：{type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            if registered:
                with self._lk:
                    self._sessions.pop(did, None)
                if self._on_change:
                    try:
                        self._on_change()
                    except Exception:
                        pass

    # ---------------- 出站调用 ----------------

    def request(self, device_id, cap, args=None, timeout=60.0):
        """向某台已拨入的手机发 CALL，阻塞等 CALL_RESULT。

        返回 dict（DeviceResult 的字典形态）。超时/未连接返回带 code=unavailable 的 dict。
        """
        args = dict(args or {})
        with self._lk:
            sess = self._sessions.get(device_id)
            if sess is None or not sess.get("alive"):
                return {"ok": False, "code": "unavailable",
                        "cap": cap, "device": device_id,
                        "err": "手机未拨入或已掉线"}
            conn = sess["conn"]
            self._call_seq += 1
            cid = "c%d" % self._call_seq
            fut = {"event": threading.Event(), "data": None}
            self._pending[cid] = fut
        common.send_msg(conn, {"t": CALL, "id": cid, "cap": cap,
                               "args": args, "device": device_id})
        if not fut["event"].wait(timeout):
            with self._lk:
                self._pending.pop(cid, None)
            return {"ok": False, "code": "timeout", "cap": cap,
                    "device": device_id, "err": "手机 %ds 未回执" % int(timeout)}
        return fut["data"] or {"ok": False, "code": "error", "cap": cap,
                               "device": device_id, "err": "空回执"}

    # ---------------- 状态 ----------------

    def status(self):
        """返回已连手机清单（含能力、最后心跳）。"""
        with self._lk:
            rows = []
            for did, s in self._sessions.items():
                if s.get("alive"):
                    rows.append({"device_id": did,
                                 "caps": sorted(s.get("caps") or []),
                                 "last_seen": s.get("last_seen", 0)})
            return {"relay": self.host + ":" + str(self.port),
                    "online": len(rows), "devices": rows}

    def has(self, device_id):
        with self._lk:
            return self._sessions.get(device_id, {}).get("alive", False)

    def device_caps(self, device_id):
        with self._lk:
            s = self._sessions.get(device_id)
            return sorted(s.get("caps") or []) if s else []


# ---------------------------------------------------------------- 单例

_relay = None


def get_relay(host=None, port=None, token=None, enabled=True):
    """单例。enabled=False 时返回 None（关闭远程层）。"""
    global _relay
    if not enabled:
        return None
    if _relay is None:
        saved = None
        if host is None and port is None and token is None:
            saved = load_relay_state()
        _host = host or (saved.get("host") if saved else None) or "127.0.0.1"
        _port = port if port is not None else ((saved.get("port") if saved else 0) or 0)
        _token = token if token is not None else ((saved.get("token") if saved else "") or "")
        _relay = RelayServer(host=_host, port=_port, token=_token)
        _relay.start()
    else:
        if host is not None:
            _relay.host = host
        if token is not None:
            _relay.token = token
    return _relay


# ---------------------------------------------------------------- 手机端 client

class PhoneClient:
    """手机侧反连 client：拨家里 → AUTH → REGISTER → 处理 CALL → 本地执行 → 回传。

    executor(cap, args) -> DeviceResult（或 dict）。真实部署里 executor 就是
    手机自己的 registry.call；自测里可以塞假执行器。
    caps_provider() -> 能力名列表（通常从本地 registry 取）。
    """

    def __init__(self, host, port, device_id, secret, executor, caps_provider,
                 token="", reconnect=True, hb_secs=30):
        self.host = host
        self.port = int(port)
        self.device_id = device_id
        self.secret = secret
        self.token = token or ""
        self.executor = executor
        self.caps_provider = caps_provider
        self.reconnect = reconnect
        self.hb_secs = hb_secs
        self._stop = threading.Event()
        self._conn = None
        self._thread = None

    def start(self):
        """后台常驻（断线自动重连）。要前台阻塞用 run()。"""
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        return self

    def run(self):
        """后台常驻（断线自动重连）。要前台阻塞用 run()。"""
        fails = 0
        while not self._stop.is_set():
            try:
                self._connect_once()
                fails = 0  # 走到这里说明连接曾被建立并随后断开，重置计数
            except Exception as e:
                if self._stop.is_set():
                    break
                fails += 1
                print(f"[remote-link] 反连 {self.host}:{self.port} 第 {fails} 次失败："
                      f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
            if self._stop.is_set():
                break
            self._stop.wait(3)     # 重连退避；stop 时立即唤醒（不再死等 3 秒）

    def stop(self):
        self._stop.set()
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass

    def _connect_once(self):
        conn = socket.create_connection((self.host, self.port), timeout=10)
        conn.settimeout(30.0)
        self._conn = conn
        # AUTH
        common.send_msg(conn, {"t": common.AUTH, "device_id": self.device_id,
                               "secret": self.secret, "token": self.token})
        r = common.recv_msg(conn)
        if not r or r.get("t") != common.AUTH_OK:
            reason = (r or {}).get("reason", "未知")
            conn.close()
            raise RuntimeError("反连认证失败: %s" % reason)
        # REGISTER
        caps = list(self.caps_provider() or [])
        common.send_msg(conn, {"t": common.REGISTER,
                               "device_id": self.device_id,
                               "capabilities": caps})
        # 主循环：收 CALL → 本地执行 → 回 CALL_RESULT
        while not self._stop.is_set():
            m = common.recv_msg(conn)
            if not m:
                break
            if m.get("t") == CALL:
                self._serve(conn, m)
            elif m.get("t") == common.HEARTBEAT:
                continue
        conn.close()

    def _serve(self, conn, m):
        cap = m.get("cap", "")
        args = m.get("args") or {}
        cid = m.get("id", "")
        try:
            out = self.executor(cap, args)
        except Exception as e:          # 执行异常绝不能让连接断
            out = {"ok": False, "code": "error", "cap": cap,
                   "device": self.device_id, "err": "%s: %s" % (type(e).__name__, e)}
        if hasattr(out, "as_dict"):
            out = out.as_dict()
        if not isinstance(out, dict):
            out = {"ok": True, "code": "success", "cap": cap,
                   "device": self.device_id, "data": {"value": out}}
        out.setdefault("id", cid)
        out.setdefault("t", CALL_RESULT)
        common.send_msg(conn, out)


# ---------------------------------------------------------------- 自测（环回，不依赖外网/手机）

def _selftest():
    tmpd = None
    import tempfile
    tmpd = tempfile.mkdtemp(prefix="relay_")
    from .device_auth import DeviceVault
    vault = DeviceVault(path=os.path.join(tmpd, "auth.json"), name="Home-PC")
    code = vault.new_code()
    claim = vault.claim(code, "红米", "android")
    assert claim["ok"], claim
    did, sec = claim["device_id"], claim["secret"]

    relay = RelayServer(host="127.0.0.1", port=0, vault=vault)
    relay.start()

    # 假执行器：只认 probe.echo，且参数 x 会回显
    def fake_exec(cap, args):
        if cap == "probe.echo":
            return {"ok": True, "code": "success", "cap": cap,
                    "device": did, "data": {"x": args.get("x")}}
        return {"ok": False, "code": "unsupported", "cap": cap,
                "device": did, "err": "手机不支持 %s" % cap}

    def caps():
        return ["probe.echo", "phone.see", "ir.command"]

    client = PhoneClient(relay.host, relay.port, did, sec, fake_exec, caps,
                         reconnect=False)
    # 后台常驻：_connect_once 会在主循环阻塞等 CALL，故走 start() 另起线程
    client.start()

    ok = fail = 0

    def chk(cond, what):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print("  [X] %s" % what)

    # 等手机 REGISTER 完成（最多 5s）
    for _ in range(50):
        if relay.has(did):
            break
        time.sleep(0.1)

    # ① 正常 CALL 往返
    r = relay.request(did, "probe.echo", {"x": 42}, timeout=5)
    chk(r.get("ok") and r.get("data", {}).get("x") == 42, "CALL 往返成功: %r" % r)
    # ② 未连接设备 → unavailable
    r2 = relay.request("dev-none", "probe.echo", {}, timeout=2)
    chk(r2.get("code") == "unavailable", "未知设备 unavailable: %r" % r2)
    # ③ 不支持的能力 → unsupported（手机本地回的）
    r3 = relay.request(did, "no.such.cap", {}, timeout=5)
    chk(r3.get("code") == "unsupported", "不支持能力 unsupported: %r" % r3)
    # ④ 错误密钥拨入被拒（另起一个 client 用错 secret）
    try:
        bad = PhoneClient(relay.host, relay.port, did, sec + "x", fake_exec, caps,
                          reconnect=False)
        bad._connect_once()
        chk(False, "错误密钥居然过了认证")
    except RuntimeError as e:
        chk("认证失败" in str(e), "错误密钥被拒: %r" % e)
    # ⑤ 未知 id 拨入被拒
    try:
        bad2 = PhoneClient(relay.host, relay.port, "dev-intruder", "fw_xxx",
                           fake_exec, caps, reconnect=False)
        bad2._connect_once()
        chk(False, "未知 id 居然过了认证")
    except RuntimeError as e:
        chk("认证失败" in str(e), "未知 id 被拒: %r" % e)
    # ⑥ status 报告在线设备
    st = relay.status()
    chk(st["online"] == 1 and st["devices"][0]["device_id"] == did,
        "status 报告在线: %r" % st)
    chk("phone.see" in st["devices"][0]["caps"], "能力已上报")

    relay.stop()
    print("\nremote_link 自测：%d 通过，%d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())

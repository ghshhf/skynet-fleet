"""天网 · 对等中继端点（relay_pair）：把 wsrelay 的 /pair 接成隧道认的形状。

## 它补的是哪个洞

`tools/cloudnode/wsrelay.py` 原先只会一件事：把 WS 变成「到某个 host:port 的
TCP 隧道」，本质是**出口代理** —— 客户端说要连谁，服务端替它连。

隧道层要的是另一件事：**两台机器各自接进来，由中继转发彼此的字节**。
谁都不指定目标，目标是「另一个也连进来的人」。

语义不同，所以 wsrelay 里新开了 `/pair` 一条独立路径；本文件负责把那条路径
包成 `Tunnel` 认的形状（`send` / `recv` / `close`）。

## 为什么中继要放在隧道层的「第一个」而不是「最后一个」

这是 DCUtR 的核心：**永远不要等最优路径**。

  直连成功 → 秒连；打洞要 3~8 秒，双对称可能永远打不通
  中继先通 → 无论打洞成不成，业务都已经在跑了

顺序反了（先打洞、失败再中继），用户看到的就是「转圈三十秒然后失败」。
所以中继不是兜底方案，它是**起点**；直连是后来的升级项。

## 房子号（room）为什么就是会话 id

一个值办两件事：
  · 配对钥匙 —— 两端连同一个房间才会被中继凑到一起
  · 打洞时刻的种子 —— `tunnel.punch_at(sid)` 两端各算一次结果相同，信令省一轮

如果另起一个房间号，就要在信令里多发一个字段，还要保证两端不会用错。
用同一个值，这类错配从结构上就不存在。

## 诚实边界

  · **不做自动重连**。中继断了就是断了，`send` 返回 0，上层重启会话。
    自动重连看着贴心，实际会让「对端已经没在等」的房间被反复占用，
    并且掩盖真实的断线原因。宁可如实断、让上层重来。
  · **不自己加密**。本层只搬字节，信任与鉴权在 pairtls / device_auth。
  · secret 为空时**不连接**：服务端会 403，但更重要的是别让调用方
    以为「配好了」。在这里直接不可用，比去撞一次 403 更清楚。
"""
import os
import sys

try:                                  # 被包导入时（正常路径）
    from .. import tunnel as tun
except ImportError:                   # 直接 python fleet/relay_pair.py 跑自测时
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from fleet import tunnel as tun


def resolve_endpoint(url="", secret=""):
    """把配置收敛成 (pair_url, secret)。任一缺失就返回 (None, 原因)。"""
    url = (url or os.environ.get("FLEET_WSRELAY") or "").strip()
    secret = (secret or os.environ.get("FLEET_WSRELAY_SECRET") or "").strip()

    if not url:
        return None, ("没有中继地址（FLEET_WSRELAY 未设置）。"
                      "隧道的契约是「无论打洞成不成都能用」，没有中继就不该开；"
                      "先用 tun.plan 看策略，或部署 tools/cloudnode/wsrelay.py")
    if not secret:
        return None, ("中继地址有，但 WSRELAY_SECRET 没配。"
                      "开放的无鉴权中继等于给全网一个免费跳板，服务端会 403；"
                      "这里提前拦住，免得调用方以为配好了")

    base = url.rstrip("/")
    pair_url = base if base.endswith("/pair") else base + "/pair"
    return {"url": pair_url, "secret": secret}, ""


class PairRelay:
    """对等中继端点。形状对齐 `Tunnel` 需要的 send / recv / close。"""

    def __init__(self, pair_url, room, secret, timeout=None, sock=None):
        self.url = pair_url
        self.room = room
        self.err = ""
        self.sock = sock if sock is not None else self._connect(secret, timeout)

    def _connect(self, secret, timeout):
        from tools.cloudnode import wsrelay as wsr
        kw = {"timeout": timeout} if timeout else {}
        return wsr.connect_pair(self.url, self.room, secret, **kw)

    # ---- 形状
    def send(self, data):
        """返回发出的字节数；发不出去返回 0（**不抛**）。

        为什么不抛：隧道层在 `Tunnel.send` 里已经区分了「直连失败 → 回落中继」
        和「中继也失败 → 如实报错」。这里抛异常会让它多一层 try/except，
        而且异常类型不统一。返回 0 是最省事、语义最准的信号。
        """
        if self.sock is None:
            return 0
        try:
            n = self.sock.send(data)
            if not n:
                self.err = self.sock.err or "中继已断开"
            return n or 0
        except Exception as e:
            self.err = f"{type(e).__name__}: {e}"
            return 0

    def recv(self, n=65536):
        if self.sock is None:
            return b""
        return self.sock.recv(n)

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def status(self):
        return {"url": self.url, "room": self.room,
                "alive": self.sock is not None and not getattr(self.sock, "closed", True),
                "err": self.err}


def make_factory(url="", secret=""):
    """给 `Tunnel(relay_factory=...)` 用的工厂。配置不全时返回 None。"""
    cfg, why = resolve_endpoint(url, secret)
    if cfg is None:
        return None, why

    def _make(sid):
        return PairRelay(cfg["url"], sid, cfg["secret"])

    return _make, ""


def punch_session_id(url="", secret=""):
    """房间号就是从会话 id 来的，这里只是把「同一个值办两件事」写在明面上。"""
    return tun.new_session_id()


# ---------------------------------------------------------------- 自测
def _selftest():
    ok = fail = 0

    def chk(cond, msg):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ✓", msg)
        else:
            fail += 1
            print("  ✗", msg, flush=True)

    print("【配置收敛：缺一样就如实说缺哪样】")
    c, why = resolve_endpoint("", "")
    chk(c is None and "中继地址" in why, "没地址 → 说清楚缺地址")
    c, why = resolve_endpoint("wss://x.onrender.com", "")
    chk(c is None and "SECRET" in why, "没 secret → 说清楚缺 secret，不放行")
    c, _ = resolve_endpoint("wss://x.onrender.com", "s")
    chk(c["url"] == "wss://x.onrender.com/pair", "补 /pair 后缀")
    c, _ = resolve_endpoint("wss://x.onrender.com/pair", "s")
    chk(c["url"] == "wss://x.onrender.com/pair", "已有 /pair 不重复拼")
    c, _ = resolve_endpoint("wss://x.onrender.com/pair/", "s")
    chk(c["url"] == "wss://x.onrender.com/pair", "尾斜杠被吃掉")

    print("【环境变量兜底】")
    os.environ["FLEET_WSRELAY"] = "wss://env.example.com"
    os.environ["FLEET_WSRELAY_SECRET"] = "envsecret"
    c, _ = resolve_endpoint("", "")
    chk(c and c["url"] == "wss://env.example.com/pair", "从环境变量读地址")
    chk(c["secret"] == "envsecret", "从环境变量读 secret")
    f, why = make_factory()
    chk(callable(f) and why == "", "配置齐了给出工厂")
    del os.environ["FLEET_WSRELAY"]
    f, why = make_factory()
    chk(f is None and why, "配置撤了工厂变 None（不假装可用）")
    del os.environ["FLEET_WSRELAY_SECRET"]

    print("【端点：断了自己知道，不抛异常】")
    pr = PairRelay("ws://x/pair", "room", "s", sock=_FakeSock())
    chk(pr.send(b"abc") == 3, "send 返回字节数")
    chk(pr.status()["alive"] is True, "活着时 status.alive=True")
    pr.sock._dead = True
    chk(pr.send(b"abc") == 0, "断开后 send 返回 0（不是抛异常）")
    chk(pr.err, "断开原因记在 err 里")
    pr.close()
    chk(pr.send(b"x") == 0, "close 之后 send 仍是 0")

    print(f"\n自测结果：{ok} 过 / {fail} 败")
    return 0 if fail == 0 else 1


class _FakeSock:
    def __init__(self):
        self.closed = False
        self.err = ""
        self._dead = False

    def send(self, data):
        if self._dead:
            self.closed = True
            return 0
        return len(data)

    def recv(self, n=65536):
        return b""

    def close(self):
        self.closed = True


if __name__ == "__main__":
    raise SystemExit(_selftest())

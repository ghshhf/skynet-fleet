"""编队节点：跑着 node_agent 的那些机器（另一台 PC / 树莓派 / 云主机）。

**D1 已通**：控制面（commander）加了对外协议（`common.CLIENT*`），非 commander
进程（桌面 GUI 等）可以经 `fleet.ctrl_client` 让它对本节点执行命令 / 推文件 /
取文件。所以 `shell.exec` / `file.push` / `file.pull` **恢复申报**——
但只在「控制面端口通 + FLEET_PASS 在环境里」两个条件都满足时申报：
声明了却调不通比缺功能更糟（界面会长出点了没反应的假按钮）。

在线判定用「最后一次心跳距今多久」，并把这个依据写进原因里：
历史文件里没有「在线」字段，冒充在线会让策略层以为编队还在（见 root registry.py）。
"""
import json
import os
import socket
import time

from ..core import BaseDevice, DeviceResult as R
from ..transports import get as get_transport
from ..skynet_relay import get_relay

NODES_PATH = os.environ.get("FLEET_NODES", os.path.join(
    os.path.expanduser("~"), ".fleet", "nodes.json"))
FRESH = 90.0          # 心跳 10s，90s 没消息就算掉线（与看门狗同量级）


def load_nodes(path=None):
    p = path or NODES_PATH
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    nodes = d.get("nodes") if isinstance(d, dict) else None
    return nodes if isinstance(nodes, dict) else {}


class FleetNode(BaseDevice):
    kind = "fleet_node"
    via = "control"

    def __init__(self, name, rec=None, **kw):
        super().__init__(name if name.startswith("node:") else f"node:{name}",
                         via="control", **kw)
        self.rec = dict(rec or {})

    def probe(self):
        ctl = get_transport("control")
        ok, reason = ctl.available() if ctl else (False, "没有 control 通道")
        if not ok:
            return False, reason
        seen = float(self.rec.get("last_seen") or 0)
        age = time.time() - seen if seen else None
        if age is None:
            return False, "历史里没有心跳时间（这机器从没连上过）"
        if age > FRESH:
            return False, (f"控制面在跑，但这台 {age:.0f}s 没心跳"
                           f"（> {FRESH:.0f}s 判掉线；据最后一条心跳推的，未经控制面确认）")
        return True, ""

    def caps(self):
        caps = list(self.rec.get("capabilities") or [])
        # 本机若跑着 SkyNet relay，主动申报 skynet.relay 能力，
        # 供 commander 路由 relay_fetch 到本节点。
        try:
            relay = get_relay()
            if relay.running and "skynet.relay" not in caps:
                caps.append("skynet.relay")
        except Exception:
            pass
        # D1 真代发（总纲 P1-9）：控制面已有对外协议（common.CLIENT*），
        # 桌面 GUI 这类非 commander 进程可以经控制面客户端让节点执行命令。
        # 🔴 只在**真能用**时才申报：控制面端口通 + 客户端密码在环境里，
        # 两个条件缺一个就不申报——否则界面又会长出点了没反应的假按钮
        # （这正是当初按总纲 §四-3 摘掉它们的原因）。
        try:
            from .. import ctrl_client as CC
            if CC.available() and CC.password_from_env():
                for c in ("shell.exec", "file.push", "file.pull"):
                    if c not in caps:
                        caps.append(c)
        except Exception:
            pass
        return tuple(caps)

    # ---- D1：经控制面客户端把动作真正发到本节点 ----
    def _ctl(self, cap):
        from .. import ctrl_client as CC
        if not CC.password_from_env():
            return None, R.error(
                cap, "没有控制面密码：设 FLEET_PASS 环境变量后重启（控制面只认密码，"
                     "不认『本机免认证』——那样任何本机进程都能指挥整个编队）",
                device=self.name, via=self.via)
        if not CC.available():
            return None, R.error(
                cap, "控制面未运行（连不上 %s:%s）—— 先起 commander，"
                     "远程能力全部不可用" % (CC.default_host(), CC.default_port()),
                device=self.name, via=self.via)
        return CC.ControlClient(), None

    def invoke(self, cap, args=None):
        args = dict(args or {})
        node = self.name.split(":", 1)[1] if ":" in self.name else self.name

        # shell.exec：在节点上执行命令（节点侧白名单把关，这里不重复实现策略）
        if cap == "shell.exec":
            cmd = (args.get("cmd") or "").strip()
            target = (args.get("node") or node).strip()
            if not cmd:
                return R.error(cap, "缺少 cmd 参数", device=self.name, via=self.via)
            ctl, err = self._ctl(cap)
            # 🔴 必须写 `is not None`，不能写 `if err:` —— DeviceResult 定义了
            # `__bool__ = ok`，而错误结果的 ok 是 False ⇒ `if err:` 对**错误结果**
            # 判假。写成 `if err:` 就会带着 ctl=None 继续往后走，
            # 最后抛成「'NoneType' object has no attribute 'exec'」这种看不懂的错。
            if err is not None:
                return err
            try:
                d = ctl.exec(target, cmd, timeout=float(args.get("timeout") or 60))
            except Exception as e:
                return self._ctl_fail(cap, e)
            rc = d.get("rc")
            return R.success(cap, self.name, data={
                "ok": rc in (0, "0", None), "node": target, "rc": rc,
                "out": d.get("out") or "", "err": d.get("err") or ""},
                via=self.via)

        # file.push：把 base64 内容写到节点（本地文件由调用方读成 b64）
        if cap == "file.push":
            target = (args.get("node") or node).strip()
            path = (args.get("path") or "").strip()
            b64 = args.get("data") or ""
            if not path:
                return R.error(cap, "缺少 path 参数", device=self.name, via=self.via)
            if not b64:
                return R.error(cap, "缺少 data（base64 内容）——不猜、不写空文件",
                               device=self.name, via=self.via)
            ctl, err = self._ctl(cap)
            # 🔴 必须写 `is not None`，不能写 `if err:` —— DeviceResult 定义了
            # `__bool__ = ok`，而错误结果的 ok 是 False ⇒ `if err:` 对**错误结果**
            # 判假。写成 `if err:` 就会带着 ctl=None 继续往后走，
            # 最后抛成「'NoneType' object has no attribute 'exec'」这种看不懂的错。
            if err is not None:
                return err
            try:
                d = ctl.push(target, path, b64,
                             timeout=float(args.get("timeout") or 120))
            except Exception as e:
                return self._ctl_fail(cap, e)
            return R.success(cap, self.name, data={
                "ok": bool(d.get("ok", True)), "node": target, "path": path,
                "bytes": d.get("bytes")}, via=self.via)

        # file.pull：从节点读文件（base64）
        if cap == "file.pull":
            target = (args.get("node") or node).strip()
            path = (args.get("path") or "").strip()
            if not path:
                return R.error(cap, "缺少 path 参数", device=self.name, via=self.via)
            ctl, err = self._ctl(cap)
            # 🔴 必须写 `is not None`，不能写 `if err:` —— DeviceResult 定义了
            # `__bool__ = ok`，而错误结果的 ok 是 False ⇒ `if err:` 对**错误结果**
            # 判假。写成 `if err:` 就会带着 ctl=None 继续往后走，
            # 最后抛成「'NoneType' object has no attribute 'exec'」这种看不懂的错。
            if err is not None:
                return err
            try:
                d = ctl.pull(target, path, timeout=float(args.get("timeout") or 120))
            except Exception as e:
                return self._ctl_fail(cap, e)
            return R.success(cap, self.name, data={
                "ok": True, "node": target, "path": path,
                "data": d.get("data") or "", "bytes": d.get("bytes")},
                via=self.via)

        # skynet.relay：返回本机 relay 运行状态（只读，不限网）
        if cap == "skynet.relay":
            try:
                relay = get_relay()
                if relay.running:
                    return R.success(
                        cap, self.name,
                        data={"ok": True, "host": relay.host, "port": relay.port,
                              "url": f"http://{relay.host}:{relay.port}"},
                        via=self.via)
                return R.success(
                    cap, self.name,
                    data={"ok": False, "host": relay.host, "port": relay.port,
                          "err": "relay 未启动（节点未以 --relay-port 启动）"},
                    via=self.via)
            except Exception as e:
                return R.error(cap, f"读取 relay 状态失败：{type(e).__name__}: {e}",
                               device=self.name, via=self.via)

        # skynet.relay.fetch：经本机（或可达）节点的 SkyNet relay 代取 URL。
        # 与 caps() 同一数据源：relay 是否真在跑以 get_relay() 为准，
        # 不再查 self.rec（带外启动 relay 时 rec 里可能没有 skynet.relay，
        # 那样 caps() 会谎报「有」而 invoke 会拒，自相矛盾）。
        if cap == "skynet.relay.fetch":
            args = dict(args or {})
            url = args.get("url", "")
            if not url:
                return R.error(cap, "缺少 url 参数", device=self.name, via=self.via)
            try:
                relay = get_relay()
            except Exception as e:
                return R.error(cap, f"读取 relay 状态失败：{type(e).__name__}: {e}",
                               device=self.name, via=self.via)
            if not relay.running:
                return R.error(
                    cap, f"节点 {self.name} 的 relay 未启动（未以 --relay-port 启动，"
                    f"或 get_relay() 还没 start）",
                    device=self.name, via=self.via)
            rp = relay.port
            try:
                s = socket.create_connection(("127.0.0.1", int(rp)), timeout=20)
            except OSError as e:
                return R.error(
                    cap, f"连不上 {self.name} 的 relay 端口 {rp}：{e}"
                    f"（relay 默认只绑 127.0.0.1，跨机需经 adb reverse / SSH 隧道）",
                    device=self.name, via=self.via)
            rid = f"rf-{self.name}-{int(time.time() * 1000)}"
            try:
                import json as _json
                _req = {"type": "task", "id": rid, "kind": "fetch", "url": url}
                if relay.token:          # --lan 模式下 relay 设了 token，本地调用也要带
                    _req["token"] = relay.token
                s.sendall(_json.dumps(_req).encode("utf-8") + b"\n")
                data = b""
                while b"\n" not in data:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            finally:
                s.close()
            if not data:
                return R.error(cap, f"relay 无回执（{self.name} 可能已掉线）",
                               device=self.name, via=self.via)
            try:
                msg = _json.loads(data.split(b"\n", 1)[0].decode("utf-8", "ignore"))
            except Exception as e:
                return R.error(cap, f"relay 回执解析失败：{type(e).__name__}: {e}",
                               device=self.name, via=self.via)
            if not msg.get("ok"):
                return R.error(cap, f"取页失败：{msg.get('error', '未知原因')}",
                               device=self.name, via=self.via)
            text = msg.get("text", "") or ""
            status = msg.get("status", 0)
            return R.success(
                cap, self.name,
                data={"ok": True, "status": status, "text": text,
                      "chars": len(text), "via": f"relay:{self.name}:{rp}"},
                via=self.via)

        return R.unsupported(
            cap, f"{self.name}（{self.kind}）没有这项能力",
            device=self.name, via=self.via)

    def _ctl_fail(self, cap, e):
        """把控制面客户端的异常折成「两种失败」，别糊成一句「失败」。

        · offline=True：节点不在线 ⇒ 该做的是叫它上线，不是重试命令；
        · 其他：通道/认证/命令本身的问题 ⇒ 看原因。
        """
        offline = bool(getattr(e, "offline", False))
        if offline:
            return R.error(cap, "节点不在线：%s（控制面在跑，是这台机器没连上）" % e,
                           device=self.name, via=self.via)
        return R.error(cap, "代发失败：%s" % e, device=self.name, via=self.via)

    def snapshot(self):
        d = super().snapshot()
        d["meta"].setdefault("platform", self.rec.get("platform") or "")
        d["last_seen"] = self.rec.get("last_seen")
        try:
            relay = get_relay()
            if relay.running:
                d["relay_port"] = relay.port
                d["relay_host"] = relay.host
        except Exception:
            pass
        return d

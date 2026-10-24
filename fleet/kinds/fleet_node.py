"""编队节点：跑着 node_agent 的那些机器（另一台 PC / 树莓派 / 云主机）。

D0 只做「认得它、说得出它现在什么样」。真调用要等 D1：控制面目前只有 REPL
（`nodes` / `exec` 是人在终端里敲的操作），没有对外的远程代发口。所以这里的
invoke 一律如实回答 `unavailable` 并说清缺什么——假装修好了比缺功能更糟。

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
        # 总纲 §四-3：不再硬编码申报 shell.exec/file.push/file.pull。
        # 这三个能力此前只在 caps() 里"空登记"、invoke 必失败——声明了却调不通
        # 比缺功能更糟（界面会长出点了没反应的假按钮）。rec 没上报就是空。
        return tuple(caps)

    def invoke(self, cap, args=None):
        # 跨机真代发：原先在 caps() 空登记、此处必失败（总纲 C 档「调必失败」）。
        # 按总纲 §四-3「或诚实摘掉登记，不留空壳」——明确说出未实现，
        # 而不是让它看起来像"能力存在但这次没成"。
        if cap in ("shell.exec", "file.push", "file.pull"):
            return R.unsupported(
                cap,
                f"{cap} 暂不提供：跨机真代发未实现（控制面只有 REPL，"
                f"没有对外代发口），登记已按总纲摘除，不留空壳",
                device=self.name, via=self.via)

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

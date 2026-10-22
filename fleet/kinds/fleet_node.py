"""编队节点：跑着 node_agent 的那些机器（另一台 PC / 树莓派 / 云主机）。

D0 只做「认得它、说得出它现在什么样」。真调用要等 D1：控制面目前只有 REPL
（`nodes` / `exec` 是人在终端里敲的操作），没有对外的远程代发口。所以这里的
invoke 一律如实回答 `unavailable` 并说清缺什么——假装修好了比缺功能更糟。

在线判定用「最后一次心跳距今多久」，并把这个依据写进原因里：
历史文件里没有「在线」字段，冒充在线会让策略层以为编队还在（见 root registry.py）。
"""
import json
import os
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
        if not caps:
            return ("shell.exec", "file.push", "file.pull")
        return tuple(caps)

    def invoke(self, cap, args=None):
        return R.unavailable(
            cap, "控制面没有对外代发口（D0 只登记；D1 接 MCP 时补），暂不能远程执行",
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

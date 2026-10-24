"""远程手机设备：把「拨入家里的手机」暴露成编队里一台普通设备。

RemotePhone 不持有任何执行逻辑——它只是 relay 会话的一个门面。invoke 把 CALL 经
反向隧道发到手机，手机在本地跑（android_phone / acquire / creative 全复用），
只把 DeviceResult 回传。对上层来说，调 `phone.see` 和调本地手机**完全同构**，
区别仅在 via="reverse-tunnel"。

诚实边界（沿用项目铁律）：
  · relay 只认配对设备（device_auth），错密钥一律 403；
  · 隧道加密由入口层（cloudflared TLS / Tailscale wireguard）提供，relay 本身明文；
  · RemotePhone 不新增任何能力——它能做什么，取决于手机 REGISTER 时上报了哪些。
"""
from ..core import BaseDevice, result as R
from .. import remote_link as RL
from ..remote_link import get_relay


def _wrap(d, default_cap=""):
    """把手机回传的 dict 还原成 DeviceResult（字段与 DeviceResult 对齐）。"""
    if not isinstance(d, dict):
        return R.DeviceResult.error(default_cap, "手机回传了非字典结果: %r" % type(d),
                                    via="reverse-tunnel")
    code = d.get("code", "error")
    cap = d.get("cap") or default_cap
    device = d.get("device", "")
    data = d.get("data") or {}
    err = d.get("err") or ""
    via = d.get("via") or "reverse-tunnel"
    side = bool(d.get("side_effect"))
    if code == R.OK:
        return R.DeviceResult.success(cap, device, data=data,
                                      side_effect=side, via=via)
    if code == R.UNAVAILABLE:
        return R.DeviceResult.unavailable(cap, err, device=device, data=data, via=via)
    if code == R.UNSUPPORTED:
        return R.DeviceResult.unsupported(cap, err, device=device, via=via)
    if code == R.DENIED:
        return R.DeviceResult.denied(cap, err, device=device, via=via, side_effect=side)
    if code == R.TIMEOUT:
        return R.DeviceResult.timeout(cap, err, device=device, via=via)
    return R.DeviceResult.error(cap, err, device=device, via=via)


class RemotePhone(BaseDevice):
    kind = "remote_phone"
    via = "reverse-tunnel"

    def __init__(self, device_id, via="reverse-tunnel"):
        self.device_id = device_id
        super().__init__("phone:" + device_id, via=via)

    def _relay(self):
        return get_relay(enabled=True)

    def probe(self):
        r = self._relay()
        if r is None:
            return False, "中继未启动"
        if r.has(self.device_id):
            return True, ""
        return False, "手机未拨入 / 已掉线"

    def caps(self):
        r = self._relay()
        if r is None:
            return []
        return list(r.device_caps(self.device_id))

    def invoke(self, cap, args=None):
        r = self._relay()
        if r is None:
            return R.DeviceResult.unavailable(cap, "中继未启动（FLEET_RELAY_ENABLED=1 才开）",
                                              device=self.name, via=self.via)
        out = r.request(self.device_id, cap, args or {}, timeout=90.0)
        return _wrap(out, default_cap=cap)


class RelayNode(BaseDevice):
    """家里常开的中继节点自身（用于编队查看「谁在线 / 中继状态」）。"""

    kind = "relay"
    via = "local"

    def __init__(self, name="relay:home", via="local"):
        super().__init__(name, via=via)
        self._caps = ("relay.status",)

    def probe(self):
        r = get_relay(enabled=True)
        if r is None:
            return False, "中继未启动"
        return True, ""

    def caps(self):
        return list(self._caps)

    def invoke(self, cap, args=None):
        if cap == "relay.status":
            r = get_relay(enabled=True)
            if r is None:
                return R.DeviceResult.unavailable(cap, "中继未启动", device=self.name)
            st = r.status()
            return R.DeviceResult.success(cap, self.name, data=st, via=self.via)
        return super().invoke(cap, args)


def sync_remote_devices(reg, relay=None):
    """把 relay 当前会话里的手机，同步成编队里的 RemotePhone 设备。

    手机拨入/掉线时由 relay.set_on_change 触发。已存在的同名设备原地保留
    （不重置其 seen 计数），新拨入的加进来，掉线的移除。
    """
    relay = relay or get_relay(enabled=True)
    if relay is None:
        return
    with reg._lk:
        online = {("phone:" + d["device_id"]) for d in relay.status()["devices"]}
        # 移除已掉线的远程手机
        for dev in list(reg._devices.values()):
            if dev.kind == "remote_phone" and dev.name not in online:
                reg._devices.pop(dev.name, None)
        # 加进新拨入的
        for name in online:
            if name not in reg._devices:
                did = name.split(":", 1)[1]
                reg._devices[name] = RemotePhone(did)

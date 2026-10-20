"""一台设备 = 一个 adapter。

契约只有三个方法，故意少：

    probe()   现在能不能用（返回 (bool, 原因)）——不可用也要说清为什么
    caps()    这台设备声明支持哪些能力（cap 名，domain.verb）
    invoke()  真去干（返回 DeviceResult）

为什么 probe 与 invoke 分开：注册表要在「不产生副作用」的前提下回答「这台能用吗」。
把可用性判断塞进 invoke（try 一下再说），会让终端刷新状态栏变成一连串真动作。

为什么 invoke 默认返回 unsupported 而不是抛异常：一台只读设备（摄像头、路由）只想
实现只读能力，不该为「不能调台」写一堆 NotImplementedError。默认答「我没有这项」，
上层才能决定换设备。
"""
from abc import ABC, abstractmethod

from . import result as R


class BaseDevice(ABC):
    #: 设备大类（kinds/ 下每个文件一个）：windows_pc / android_phone / lan_scanner / fleet_node
    kind = "device"
    #: 默认通道名（真连接由 transports/ 提供，这里只做标注，用于「经什么达成的」）
    via = "local"

    def __init__(self, name, via="", meta=None):
        if not name:
            raise ValueError("设备必须有名字（cap 结果里的 device 字段靠它定位）")
        self.name = name
        self.via = via or self.via
        self.meta = dict(meta or {})

    # ---------------- 子类必须实现 ----------------

    @abstractmethod
    def probe(self):
        """返回 (可用: bool, 原因: str)。可用时原因可为空；不可用时必须说清。"""

    @abstractmethod
    def caps(self):
        """返回本设备支持的能力名序列。"""

    # ---------------- 子类按需覆盖 ----------------

    def invoke(self, cap, args=None):
        return R.DeviceResult.unsupported(
            cap, f"{self.name}（{self.kind}）没有这项能力", device=self.name, via=self.via)

    # ---------------- 公共 ----------------

    def supports(self, cap):
        return cap in set(self.caps())

    def snapshot(self):
        """给注册表/终端看的一张静态名片（绝不包含凭据）。"""
        return {"name": self.name, "kind": self.kind, "via": self.via,
                "caps": list(self.caps()), "meta": dict(self.meta)}

    def availability(self):
        """把 probe 的 (bool, reason) 包成信封，让调用方只处理一种形状。"""
        try:
            ok, reason = self.probe()
        except Exception as e:                       # 探针自己炸了也是一种「不可用」
            return R.DeviceResult.error("device.probe", f"探测出错: {e}",
                                        device=self.name, via=self.via)
        if ok:
            return R.DeviceResult.success("device.probe", self.name, via=self.via)
        return R.DeviceResult.unavailable("device.probe", reason or "不可用（未给原因）",
                                          device=self.name, via=self.via)

    def __repr__(self):
        return f"<{self.kind} {self.name} via={self.via}>"

"""统一结果信封：所有 transport / kind / capability 共用同一种「回答」。

为什么需要信封：现在各模块各返各的——有的 `{"ok": False, "err": "..."}`，有的
返回 tuple，有的直接 print。上层（规则/她/模型）只看到「失败了」，看不到
「为什么失败」，所以写不出「重试 / 换设备 / 叫人 / 算了」这种决策。

`code` 就是那根分界线：

    ok           成了
    unavailable  现在做不到（桥不在线 / adb 没设备 / 端口不通）→ 可重试、可换设备
    denied       有本事做，但授权不许（闸门 / 分号角色）        → 叫人
    timeout      发出去了，没回音                              → 可重试一次
    unsupported  这台设备根本没有这项能力                      → 换设备
    error        做了，出错了                                  → 报错

三条铁律（写进构造器，不靠调用方自觉）：
  · ok=True 必配 code="ok"；
  · ok=False 必须带原因（err 不许空）——「不可用必须给原因」；
  · code 只许上面六个值（拼错立刻炸，不许悄悄退化成 error）。
"""
from dataclasses import dataclass, field

OK = "ok"
UNAVAILABLE = "unavailable"
DENIED = "denied"
TIMEOUT = "timeout"
ERROR = "error"
UNSUPPORTED = "unsupported"

CODES = (OK, UNAVAILABLE, DENIED, TIMEOUT, ERROR, UNSUPPORTED)

# 哪些 code 值得上层再试一次（规则引擎与编排器都读这张表，别各写一份）
RETRYABLE = (UNAVAILABLE, TIMEOUT)

_FIELDS = ("ok", "cap", "device", "data", "err", "code", "ms", "side_effect", "via")


@dataclass
class DeviceResult:
    """一次能力调用的结果。字段固定，多出来的载荷一律进 data。"""

    # 类上也挂一份：调用方写 DeviceResult.UNAVAILABLE 比 import 五个常量顺手，
    # 也减少「手打字符串拼错」的机会（拼错在构造器里就炸）。
    OK = OK
    UNAVAILABLE = UNAVAILABLE
    DENIED = DENIED
    TIMEOUT = TIMEOUT
    ERROR = ERROR
    UNSUPPORTED = UNSUPPORTED

    cap: str
    device: str = ""
    ok: bool = False
    code: str = ERROR
    data: dict = field(default_factory=dict)
    err: str = ""
    ms: float = 0.0
    side_effect: bool = False
    via: str = ""

    def __post_init__(self):
        if self.code not in CODES:
            raise ValueError(f"未知 code: {self.code!r}（只许 {CODES}）")
        if self.ok and self.code != OK:
            raise ValueError(f"ok=True 必须配 code='ok'，得到 {self.code!r}")
        if not self.ok and self.code == OK:
            raise ValueError("ok=False 不许配 code='ok'")
        if not self.ok and not (self.err or "").strip():
            raise ValueError(f"失败必须给原因：cap={self.cap} code={self.code}")
        if self.data is None:
            self.data = {}

    # ---------------- 构造器（动词用全拼，避免和字段 ok 撞名） ----------------

    @classmethod
    def success(cls, cap, device="", data=None, ms=0.0, side_effect=False, via=""):
        return cls(cap=cap, device=device, ok=True, code=OK, data=data or {},
                   ms=ms, side_effect=side_effect, via=via)

    @classmethod
    def unavailable(cls, cap, reason, device="", data=None, ms=0.0, via=""):
        return cls(cap=cap, device=device, ok=False, code=UNAVAILABLE, data=data or {},
                   err=reason, ms=ms, via=via)

    @classmethod
    def denied(cls, cap, reason, device="", ms=0.0, side_effect=True, via=""):
        return cls(cap=cap, device=device, ok=False, code=DENIED, err=reason,
                   ms=ms, side_effect=side_effect, via=via)

    @classmethod
    def timeout(cls, cap, reason="超时未回", device="", ms=0.0, via=""):
        return cls(cap=cap, device=device, ok=False, code=TIMEOUT, err=reason,
                   ms=ms, via=via)

    @classmethod
    def unsupported(cls, cap, reason, device="", ms=0.0, via=""):
        return cls(cap=cap, device=device, ok=False, code=UNSUPPORTED, err=reason,
                   ms=ms, via=via)

    @classmethod
    def error(cls, cap, reason, device="", ms=0.0, via=""):
        return cls(cap=cap, device=device, ok=False, code=ERROR, err=reason,
                   ms=ms, via=via)

    # ---------------- 与其他模块的 dict 习惯兼容 ----------------

    def as_dict(self):
        return {k: getattr(self, k) for k in _FIELDS}

    def __getitem__(self, key):
        if key not in _FIELDS:
            raise KeyError(key)
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key) if key in _FIELDS else default

    def keys(self):
        return list(_FIELDS)

    def items(self):
        return self.as_dict().items()

    def __contains__(self, key):
        return key in _FIELDS

    def __bool__(self):
        return bool(self.ok)

    @property
    def retryable(self):
        return self.code in RETRYABLE

    # 给终端/日志用的一行摘要（不塞长载荷）
    def brief(self):
        if self.ok:
            return f"{self.cap}@{self.device or '-'} ok {self.ms:.0f}ms"
        return f"{self.cap}@{self.device or '-'} {self.code}: {self.err}"


# ---------------------------------------------------------------- 自测

def _selftest():
    r = DeviceResult.success("pc.state", "pc:local", {"cpu": 12}, ms=8.4, via="local")
    assert r.ok and r["ok"] is True and r.get("code") == OK
    assert r.as_dict()["data"]["cpu"] == 12 and "ok" in r and bool(r) is True

    # ① 失败必须带原因
    try:
        DeviceResult(cap="x", ok=False, code=ERROR, err="  ")
        raise AssertionError("空原因居然通过了")
    except ValueError:
        pass

    # ② ok 与 code 不许矛盾
    for bad in (dict(ok=True, code=UNAVAILABLE, err="x"),
                dict(ok=False, code=OK, err="x")):
        try:
            DeviceResult(cap="x", **bad)
            raise AssertionError(f"矛盾组合通过了: {bad}")
        except ValueError:
            pass

    # ③ 拼错 code 立刻炸
    try:
        DeviceResult(cap="x", ok=False, code="unavailble", err="x")
        raise AssertionError("拼错的 code 居然通过")
    except ValueError:
        pass

    # ④ 五个失败态各自可构造，且都能给出「要不要重试」
    assert DeviceResult.unavailable("c", "桥不在线").retryable is True
    assert DeviceResult.timeout("c").retryable is True
    assert DeviceResult.denied("c", "分号无权发令").retryable is False
    assert DeviceResult.unsupported("c", "这台没有").retryable is False
    assert DeviceResult.error("c", "炸了").retryable is False

    # ⑤ brief 不会把 data 抖出来（终端状态栏每帧都调它）
    big = DeviceResult.success("pc.state", "pc:local", {"big": "x" * 500})
    assert len(big.brief()) < 80, big.brief()

    print("result 自测：5 组全过")


if __name__ == "__main__":
    _selftest()

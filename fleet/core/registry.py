"""注册表：唯一真相。

一个能力要能被「人 / 她 / 模型」发现并调用，必须在这里有一条记录：
名字、参数 schema、有没有副作用、能不能用、归属哪台设备。没登记的，上层看不见。

三条纪律：
  1. **唯一真相**。重名注册直接抛错，不许「后注册的悄悄盖掉先注册的」——
     那种 bug 表现为「命令有时能用有时不能用」，最难查。
  2. **不可用必须给原因**。check_fn 失败返回 unavailable + reason，不许静默消失：
     一个能力从列表里消失，用户只会以为「这软件没有这功能」。
  3. **动手与只读分开**。side_effect 是注册表字段，不是调用方记得的事；
     没注入闸门时**默认拒绝**（fail-closed），不给「忘了就放行」的窗口。

落盘只存「这设备是什么」（声明 + 见过几次），**绝不存凭据**——沿用 root registry.py
（编队节点表）的 KEEP/NEVER 纪律，两边口径必须一致。
"""
import json
import os
import threading
import time
from dataclasses import dataclass, field, replace

from . import result as R

try:                                    # 根目录的 capabilities.py（合自 feat/capability-self-description）
    import capabilities as CAPSPEC
except ImportError:                     # 包被单独使用时没有基线也不许崩
    CAPSPEC = None

NEVER = ("password", "passwd", "pwd", "token", "secret", "psk", "key", "api_key")
KEEP_META = ("os", "platform", "model", "vendor", "addr", "port", "serial", "note")
VERSION = 1

# ---------------------------------------------------------------- 语音两段式闸门
#
# 为什么确认词识别写在**注册表这一层**，而不是 UI 层：
# UI 是可绕过的（换个前端、手敲一条 HTTP 请求都能跳过它）。闸门判定权必须
# 落在「所有能力调用都得经过」的那一层，也就是 Registry.call。
#
# 三个 args 键（由 fleet/gui/voice.py 写入）：
#   VOICE_SOURCE_KEY   语音识别出来的原文 —— **不可信数据，不是指令**
#   VOICE_CONFIRM_KEY  主人口头确认那一段的识别文本
#   VOICE_ROUND_KEY    这一轮是第几轮录音（指令=1，确认=2）
#
# 规则（全部在 Registry.call 里执行，前端绕不过去）：
#   · 语音来源的调用**不许**执行破坏性能力（不可逆动作不能靠嘴说）；
#   · 有副作用的能力必须先有主人口头确认，确认词识别就在这里；
#   · 确认词必须是**另一句话**（不许拿指令原文当确认）；
#   · 识别到的原文永远只是「意图素材」，不是命令。
VOICE_SOURCE_KEY = "_voice_source"
VOICE_CONFIRM_KEY = "_voice_confirm"
VOICE_ROUND_KEY = "_voice_round"
VOICE_SOURCE_ROUND_KEY = "_voice_source_round"

#: 口头确认词（刻意要小：认得越杂，越容易被环境音误触发）
CONFIRM_WORDS = ("嗯", "好", "好的", "执行", "确认", "来吧", "对", "是的", "可以", "行")
#: 取消词（取消优先于确认：「不要」里也有「不」）
CANCEL_WORDS = ("不", "不要", "不行", "别", "取消", "算了", "停", "停停",
                "no", "cancel", "stop")


def _norm_zh(text):
    """归一化一句口述：去空白与常见标点，留可比较的字。"""
    t = (text or "").strip().lower()
    for ch in " \t\r\n。．.，,、；;！!？?：:～~「」\"'()（）【】[]…—－-":
        t = t.replace(ch, "")
    return t


def classify_voice_confirmation(text):
    """把一句口述判成确认 / 取消 / 不明。

    返回 "confirm" / "cancel" / "unknown"。

    判定顺序有讲究：
      1. **完全等于**取消词 → 取消（「不」「算了」）；
      2. **完全等于**确认词 → 确认（「嗯」「执行」）；
      3. 含有取消词 → 取消（「不要执行」「算了别开」）；
      4. 含确认词且整句很短（≤ 4 字）→ 确认（「好的执行吧」）；
      5. 其余 → 不明（必须重新问，绝不默认放行）。

    第 4 条为什么要卡长度：STT 会把「嗯嗯嗯大概就这样」这种废话也吐出来，
    里面含「嗯」。若不卡长度，含糊的话会被当成确认 —— 那就等于「默认放行」，
    恰好是这道闸门要防的那件事。
    """
    t = _norm_zh(text)
    if not t:
        return "unknown"
    if t in CANCEL_WORDS:
        return "cancel"
    if t in CONFIRM_WORDS:
        return "confirm"
    for w in CANCEL_WORDS:
        if w in t:
            return "cancel"
    for w in CONFIRM_WORDS:
        if w in t:
            return "confirm" if len(t) <= 4 else "unknown"
    return "unknown"


@dataclass(frozen=True)
class Param:
    name: str
    type: str = "str"          # str / int / bool / json
    required: bool = False
    doc: str = ""

    def as_dict(self):
        return {"name": self.name, "type": self.type,
                "required": self.required, "doc": self.doc}


@dataclass(frozen=True)
class Capability:
    """一条能力记录。name 一律 domain.verb（screen.read / input.tap / pc.state）。"""

    name: str
    title: str
    params: tuple = ()
    side_effect: bool = False      # 会改变外部世界（点一下、发一条、写一个文件）
    destructive: bool = False      # 不可逆（删、格式化、发射）——闸门要双确认
    check: object = None           # check_fn(registry, device) -> (bool, reason)
    note: str = ""
    approval: str = "auto"         # 审批档（学 Cherry 的 builtinToolPolicyRegistry）：
                                    #   auto —— 默认，按现有闸门走
                                    #   ask  —— 即便非副作用也要人确认（闸门 level=2）
                                    #   deny —— 永远拒绝（fail-closed，数据文件改不了）

    def __post_init__(self):
        """把 params 一律归一成元组。

        `params=(Param(...))` 少写一个逗号，得到的就是一个 Param 而不是元组——
        这个坑阴在：写的时候看不出来，登记也不报错，要等别处 `for p in c.params`
        才当场炸，而堆栈指向的是被炸的那个文件，不是写错的这一行。
        所以归一放在构造器里，写错的人自己立刻拿到正确的东西。
        """
        p = self.params
        if p is None:
            p = ()
        elif isinstance(p, Param):
            p = (p,)
        else:
            p = tuple(p)
        object.__setattr__(self, "params", p)

    def as_dict(self):
        return {"name": self.name, "title": self.title,
                "params": [p.as_dict() for p in self.params],
                "side_effect": self.side_effect, "destructive": self.destructive,
                "note": self.note,
                "approval": self.approval,
                "domain": self.name.split(".", 1)[0]}


@dataclass
class _ProbeCache:
    at: float = 0.0
    ok: bool = False
    reason: str = ""
    extra: dict = field(default_factory=dict)


class Registry:
    def __init__(self, path="", gate=None, ttl=5.0):
        self.path = path
        self.gate = gate                    # gate(cap, device, args, level) -> bool | (bool, reason)
        self.ttl = ttl
        self._caps = {}
        self._devices = {}
        self._seen = {}
        self._probe = {}
        self._lk = threading.RLock()

    # ---------------- 登记 ----------------

    def register(self, cap):
        """登记一条能力。

        没写参数 schema 的能力，自动从根目录 `capabilities.py` 的基线继承——
        「一条能力要有说明书」是注册表的事，不该指望每个写设备种类的人记得填。
        显式声明的永远压过继承的（catalog 是唯一真相，基线只是兜底）。
        """
        with self._lk:
            old = self._caps.get(cap.name)
            if old is not None and old != cap:
                raise ValueError(f"能力重复注册: {cap.name}（{old.title} / {cap.title}）")
            if not cap.params and CAPSPEC is not None:
                inh = tuple(Param(**p) for p in CAPSPEC.params_of(cap.name))
                if inh:
                    cap = replace(cap, params=inh)
            self._caps[cap.name] = cap
        return cap

    def register_all(self, caps):
        for c in caps:
            self.register(c)

    def add(self, device):
        with self._lk:
            if device.name in self._devices:
                raise ValueError(f"设备重名: {device.name}")
            self._devices[device.name] = device
            rec = self._seen.get(device.name) or {}
            now = time.time()
            rec.update({"kind": device.kind, "via": device.via,
                        "caps": list(device.caps()),
                        "first_seen": rec.get("first_seen") or now,
                        "last_seen": now,
                        "count": int(rec.get("count") or 0) + 1})
            meta = {k: v for k, v in device.meta.items()
                    if k in KEEP_META and not any(n in k.lower() for n in NEVER)}
            rec["meta"] = meta
            self._seen[device.name] = rec
        return device

    # ---------------- 查询 ----------------

    def capability(self, name):
        with self._lk:
            return self._caps.get(name)

    def capabilities(self):
        with self._lk:
            return [self._caps[k] for k in sorted(self._caps)]

    def cap_names(self):
        return [c.name for c in self.capabilities()]

    def device(self, name):
        with self._lk:
            return self._devices.get(name)

    def devices(self):
        with self._lk:
            return [self._devices[k] for k in sorted(self._devices)]

    def owners(self, cap):
        """声明支持这项能力的设备（不管现在可不可用）。"""
        return [d for d in self.devices() if d.supports(cap)]

    # ---------------- 可用性 ----------------

    def probe(self, device, ttl=None):
        """带缓存的 probe。终端状态栏每次刷新都会问，不能每次都真去敲 adb。"""
        ttl = self.ttl if ttl is None else ttl
        now = time.time()
        with self._lk:
            c = self._probe.get(device.name)
            if c and ttl > 0 and (now - c.at) < ttl:
                return c.ok, c.reason
        try:
            ok, reason = device.probe()
        except Exception as e:
            ok, reason = False, f"探测出错: {e}"
        with self._lk:
            self._probe[device.name] = _ProbeCache(at=time.time(), ok=bool(ok),
                                                   reason=reason or "")
        return bool(ok), (reason or "")

    def invalidate(self, name=""):
        with self._lk:
            if name:
                self._probe.pop(name, None)
            else:
                self._probe.clear()

    def check(self, cap_name, device_name=""):
        """综合判定：能力存在 → 有设备声明它能做 → 设备可用 → check_fn 通过。"""
        cap = self.capability(cap_name)
        if cap is None:
            return False, f"注册表里没有这项能力: {cap_name}", None
        devs = self.owners(cap_name)
        if not devs:
            return False, f"没有任何设备声明能做 {cap_name}", None

        if device_name:
            dev = self.device(device_name)
            if dev is None:
                return False, f"没有这台设备: {device_name}", None
            if not dev.supports(cap_name):
                return False, f"{dev.name} 不支持 {cap_name}", dev
            devs = [dev]

        reasons = []
        for dev in devs:
            ok, reason = self.probe(dev)
            if not ok:
                reasons.append(f"{dev.name}: {reason or '不可用'}")
                continue
            if cap.check is not None:
                try:
                    res = cap.check(self, dev)
                except Exception as e:
                    reasons.append(f"{dev.name}: 检查出错 {e}")
                    continue
                ok_c, reason_c = (res if isinstance(res, tuple) else (bool(res), ""))
                if not ok_c:
                    reasons.append(f"{dev.name}: {reason_c or '检查未通过'}")
                    continue
            return True, "", dev
        return False, "；".join(reasons) or "没有可用设备", None

    def availability(self, ttl=None):
        """每台设备一行：能干什么、现在能不能用、不能用为什么。"""
        rows = []
        for dev in self.devices():
            ok, reason = self.probe(dev, ttl=ttl)
            rows.append({"name": dev.name, "kind": dev.kind, "via": dev.via,
                         "ok": ok, "reason": reason, "caps": list(dev.caps())})
        return rows

    def stats(self):
        rows = self.availability()
        return {"devices": len(rows),
                "online": sum(1 for r in rows if r["ok"]),
                "caps": len(self.capabilities())}

    def _failure(self, cap_name, reason, device_name=""):
        """把「为什么不成」映射成正确的 code。

        这一步不能省：规则引擎读 code 决定「换设备 / 重试 / 叫人」。
        「这台设备没有这项能力」（unsupported → 换设备）与「设备在但现在连不上」
        （unavailable → 重试或等人开机）是两种完全不同的处置，混成一种就等于没分类。
        """
        owners = self.owners(cap_name)
        if not owners:
            return R.DeviceResult.unsupported(cap_name, reason, device=device_name)
        if device_name:
            dev = self.device(device_name)
            if dev is not None and not dev.supports(cap_name):
                return R.DeviceResult.unsupported(cap_name, reason, device=device_name)
        return R.DeviceResult.unavailable(cap_name, reason, device=device_name)

    # ---------------- 语音两段式闸门 ----------------

    def _voice_approval(self, cap, args):
        """语音路径的审批判定。返回 None = 放行；返回 str = 拒绝理由。

        为什么判定写在注册表里：闸门是**所有能力调用的必经之路**，
        放在 UI 层的话，换个前端或手敲 HTTP 就能绕过——那这道闸门等于没有。
        """
        # ① 破坏性能力：语音路径一律不许。嘴说的话不能启动不可逆动作。
        if cap.destructive:
            return ("语音路径不执行破坏性能力（%s 是不可逆的）："
                    "这类动作请在界面上点" % cap.name)
        # ② 必须有主人口头确认
        confirm_text = args.get(VOICE_CONFIRM_KEY)
        if confirm_text is None:
            return ("语音触发的动作需要主人口头确认："
                    "先复述意图，等主人说「嗯/好/执行/确认」再执行"
                    "（取消说「不/取消/算了」）")
        verdict = classify_voice_confirmation(confirm_text)
        if verdict == "cancel":
            return "主人说了取消，这次不执行"
        if verdict != "confirm":
            return ("没听清主人的确认（听到的是「%s」）："
                    "需要一句「嗯/好/执行/确认」，或「不/取消/算了」"
                    % str(confirm_text)[:40])
        # ③ 确认必须是**另一句话**：不许拿指令原文当确认
        src = str(args.get(VOICE_SOURCE_KEY) or "").strip()
        if src and _norm_zh(src) == _norm_zh(str(confirm_text)):
            return ("确认内容与指令内容相同（都是「%s」）："
                    "确认必须是另一句话，不许拿指令本身顶替" % src[:40])
        # ④ 确认轮次必须晚于指令轮次（先说指令，再确认）
        s_round = args.get(VOICE_SOURCE_ROUND_KEY)
        c_round = args.get(VOICE_ROUND_KEY)
        try:
            if s_round is not None and c_round is not None and int(c_round) <= int(s_round):
                return ("确认必须来自另一轮录音（指令第 %s 轮，确认第 %s 轮）"
                        % (s_round, c_round))
        except (TypeError, ValueError):
            return "语音轮次参数不合法，确认判定不了"
        return None

    # ---------------- 调用 ----------------

    def call(self, cap_name, device="", args=None, gate=None):
        args = dict(args or {})
        cap = self.capability(cap_name)
        if cap is None:
            return R.DeviceResult.unsupported(cap_name, f"注册表里没有这项能力: {cap_name}")

        # ---- 判定次序（2026-10-23 定，两条门禁同时钉住这个顺序）----
        # ① 结构：能力在不在 / 有没有设备声明 / 指定设备在不在且支不支持。
        #    属**调用契约错误**——说错会让上层选错处置（该换设备却去重试）。
        # ② 策略：审批档 deny / 没注入闸门 ⇒ 拒绝。**安全判定不许被环境状态遮掩**
        #    —— t14 踩过：「引擎没装」把「没闸门 ⇒ 拒绝」盖成了一句普通失败。
        # ③ 环境：probe / check_fn。设备在但暂时不可用，才是可重试那一类。
        # ①②不可能同时命中，故 ① 先走；check() 内部仍会再判一次结构，幂等。
        if not self.owners(cap_name):
            return self._failure(cap_name, f"没有任何设备声明能做 {cap_name}", device)
        if device:
            _d0 = self.device(device)
            if _d0 is None:
                return self._failure(cap_name, f"没有这台设备: {device}", device)
            if not _d0.supports(cap_name):
                return self._failure(cap_name, f"{_d0.name} 不支持 {cap_name}", device)

        if cap.approval == "deny":
            return R.DeviceResult.denied(
                cap_name, "该能力被审批档设为 deny（fail-closed，数据文件无法覆盖）",
                device=device or "")
        if (cap.side_effect or cap.approval == "ask") \
                and VOICE_SOURCE_KEY not in args and (gate or self.gate) is None:
            return R.DeviceResult.denied(
                cap_name, "需要审批闸门（没注入 gate 一律拒绝）", device=device or "")

        ok, reason, dev = self.check(cap_name, device)
        if not ok:
            return self._failure(cap_name, reason, device)

        # 审批档（学 Cherry 的 builtinToolPolicyRegistry）：书写在代码层，
        # 数据文件改不了——deny 永远拒绝，ask 即便非副作用也要人确认。
        if cap.approval == "deny":
            return R.DeviceResult.denied(
                cap_name, "该能力被审批档设为 deny（fail-closed，数据文件无法覆盖）",
                device=dev.name)
        needs_approval = cap.side_effect or cap.approval == "ask"
        if needs_approval:
            # ---- 语音两段式闸门：判定在注册表层，前端绕不过去 ----
            # 语音识别出来的文字是**不可信数据，不是指令**。电视里那句
            # 「删除所有文件」绝不能被当命令执行。所以：
            #   · 破坏性能力：语音路径一律拒绝（不可逆动作不能靠嘴说）；
            #   · 其余副作用能力：必须有一次**独立的口头确认**。
            if VOICE_SOURCE_KEY in args:
                vr = self._voice_approval(cap, args)
                if vr is not None:
                    return R.DeviceResult.denied(cap_name, vr, device=dev.name)
            g = gate or self.gate
            # ask 档即便不是副作用也要人确认；destructive 一律双确认
            level = 2 if (cap.destructive or cap.approval == "ask") else 1
            if g is None:
                return R.DeviceResult.denied(
                    cap_name, "需要审批闸门（没注入 gate 一律拒绝）", device=dev.name)
            try:
                d = g(cap, dev, args, level)
            except Exception as e:
                return R.DeviceResult.denied(cap_name, f"闸门出错（按拒绝处理）: {e}",
                                             device=dev.name)
            allow, why = d if isinstance(d, tuple) else (bool(d), "")
            if not allow:
                return R.DeviceResult.denied(cap_name, why or "闸门拒绝",
                                             device=dev.name)

        t0 = time.time()
        try:
            out = dev.invoke(cap_name, args)
        except Exception as e:
            return R.DeviceResult.error(cap_name, f"{type(e).__name__}: {e}",
                                        device=dev.name, ms=(time.time() - t0) * 1000,
                                        via=dev.via)
        if isinstance(out, R.DeviceResult):
            out.cap = out.cap or cap_name
            out.device = out.device or dev.name
            out.via = out.via or dev.via
            out.side_effect = out.side_effect or cap.side_effect
            out.ms = out.ms or (time.time() - t0) * 1000
            return out
        if isinstance(out, dict):                       # 老模块的 dict 习惯
            ok = bool(out.get("ok")) if "ok" in out else True
            if ok:
                return R.DeviceResult.success(cap_name, dev.name, data=out,
                                              ms=(time.time() - t0) * 1000,
                                              side_effect=cap.side_effect, via=dev.via)
            return R.DeviceResult.error(cap_name, str(out.get("err") or "失败"),
                                        device=dev.name, ms=(time.time() - t0) * 1000,
                                        via=dev.via)
        if out is None:
            return R.DeviceResult.error(cap_name, "设备返回了空结果（adapter 忘了 return）",
                                        device=dev.name, ms=(time.time() - t0) * 1000,
                                        via=dev.via)
        return R.DeviceResult.success(cap_name, dev.name, data={"value": out},
                                      ms=(time.time() - t0) * 1000,
                                      side_effect=cap.side_effect, via=dev.via)

    # ---------------- 落盘（只存「是什么」） ----------------

    def save(self):
        if not self.path:
            return ""
        payload = {"version": VERSION, "at": time.time(), "devices": {}}
        for name, rec in self._seen.items():
            payload["devices"][name] = {
                "kind": rec.get("kind"), "via": rec.get("via"),
                "caps": rec.get("caps") or [], "meta": rec.get("meta") or {},
                "first_seen": rec.get("first_seen"), "last_seen": rec.get("last_seen"),
                "count": rec.get("count") or 0,
            }
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            return self.path
        except OSError:
            return ""

    def load(self):
        """读历史（谁来过）。**不把历史当在线**——与节点表同一条纪律。"""
        if not self.path or not os.path.isfile(self.path):
            return 0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return 0
        devs = d.get("devices") if isinstance(d, dict) else None
        if not isinstance(devs, dict):
            return 0
        with self._lk:
            self._seen = {k: v for k, v in devs.items() if isinstance(v, dict)}
        return len(self._seen)

    def history(self):
        with self._lk:
            return {k: dict(v) for k, v in self._seen.items()}


# ---------------------------------------------------------------- 自测

def _selftest():
    from .device import BaseDevice

    class Fake(BaseDevice):
        kind = "fake"

        def __init__(self, name, up=True, caps=(("probe.echo",),), via="fake"):
            self._up = up
            super().__init__(name, via=via)
            self._caps = list(caps)

        def probe(self):
            return (self._up, "" if self._up else "假设备不在线")

        def caps(self):
            return self._caps

        def invoke(self, cap, args=None):
            if args and args.get("boom"):
                raise RuntimeError("炸了")
            if args and args.get("dict"):
                return {"ok": False, "err": "老格式失败"}
            return R.DeviceResult.success(cap, self.name, {"got": (args or {}).get("x", 0)},
                                          via=self.via)

    reg = Registry(path="", ttl=0)
    reg.register(Capability("probe.echo", "回声", params=(Param("x", "int"),)))
    reg.register(Capability("probe.write", "写一下", side_effect=True))
    reg.add(Fake("f:up", True, caps=("probe.echo", "probe.write", "probe.burn",
                                     "probe.locked", "probe.askme")))
    reg.add(Fake("f:down", False, caps=("probe.echo",)))

    # ① 正常调用 + 指定设备
    r = reg.call("probe.echo", "f:up", {"x": 7})
    assert r.ok and r.data["got"] == 7 and r.via == "fake", r

    # ② 不指定设备时自动挑在线的那台（f:down 排前面也不该被挑中）
    r = reg.call("probe.echo")
    assert r.ok and r.device == "f:up", r

    # ③ 掉线设备必须给出原因，不许静默变成「没这能力」
    reg2 = Registry(path="", ttl=0)
    reg2.register(Capability("probe.echo", "回声"))
    reg2.add(Fake("only:down", False, caps=("probe.echo",)))
    r = reg2.call("probe.echo")
    assert r.code == R.UNAVAILABLE and "不在线" in r.err, r
    ok, reason, dev = reg2.check("probe.echo")
    assert not ok and "不在线" in reason and dev is None

    # ④ 没登记的能力是 unsupported，不是「不可用」
    assert reg.call("nope.nope").code == R.UNSUPPORTED
    # ⑤ 指定了不存在的设备 → unavailable（不是「设备没有这能力」，是「找不到这台」）
    r = reg.call("probe.write", "no:such")
    assert r.code == R.UNAVAILABLE and "没有这台设备" in r.err, r
    # ⑤b 设备存在但不支持 → unsupported（上层该换设备）
    r = reg.call("probe.echo", "f:down")
    assert r.code == R.UNAVAILABLE, r          # f:down 声明支持但离线
    r = reg.call("probe.echo", "f:up")
    assert r.ok, r

    # ⑥ 动手能力没闸门 → 默认拒绝（fail-closed）
    r = reg.call("probe.write", "f:up")
    assert r.code == R.DENIED, r

    # ⑦ 闸门放行/拒绝，且拒绝要带理由
    seen = {}
    def gate(cap, dev, args, level):
        seen["level"] = level
        return (args.get("yes") is True, "主人说不")
    assert reg.call("probe.write", "f:up", {"yes": True}, gate=gate).ok
    r = reg.call("probe.write", "f:up", {}, gate=gate)
    assert r.code == R.DENIED and r.err == "主人说不" and seen["level"] == 1, r

    # ⑧ 破坏性能力 → 闸门收到 level=2（交给闸门做双确认）
    reg.register(Capability("probe.burn", "烧掉", side_effect=True, destructive=True))
    reg.call("probe.burn", "f:up", {"yes": True}, gate=gate)
    assert seen["level"] == 2, seen

    # ⑧b 审批档 deny：永远拒绝（fail-closed），数据文件盖不了
    reg.register(Capability("probe.locked", "锁死", approval="deny"))
    r = reg.call("probe.locked", "f:up", {}, gate=gate)
    assert r.code == R.DENIED, r
    # ⑧c 审批档 ask：即便非副作用也要闸门确认；闸门放行才过
    reg.register(Capability("probe.askme", "要确认", approval="ask"))
    r = reg.call("probe.askme", "f:up", {}, gate=gate)
    assert r.code == R.DENIED and seen["level"] == 2, r
    assert reg.call("probe.askme", "f:up", {"yes": True}, gate=gate).ok

    # ⑨ adapter 抛异常 → error，不传染
    r = reg.call("probe.echo", "f:up", {"boom": 1})
    assert r.code == R.ERROR and "RuntimeError" in r.err, r
    # ⑩ 老 dict 格式照样能转
    r = reg.call("probe.echo", "f:up", {"dict": 1})
    assert r.code == R.ERROR and r.err == "老格式失败", r

    # ⑪ 重名注册/重名设备立即炸
    for fn in (lambda: reg.register(Capability("probe.echo", "另一个回声")),
               lambda: reg.add(Fake("f:up"))):
        try:
            fn()
            raise AssertionError("重名居然通过了")
        except ValueError:
            pass

    # ⑫ 落盘不含凭据；重载不把历史当在线
    import tempfile
    p = os.path.join(tempfile.gettempdir(), f"fleet_dev_st_{os.getpid()}.json")
    for f in (p, p + ".tmp"):
        if os.path.exists(f):
            os.remove(f)
    reg3 = Registry(path=p, ttl=0)
    reg3.register(Capability("probe.echo", "回声"))
    d = Fake("f:secret", True, caps=("probe.echo",))
    d.meta = {"platform": "win", "password": "hunter2", "token": "tok-abc", "model": "X1"}
    reg3.add(d)
    reg3.save()
    raw = open(p, encoding="utf-8").read()
    assert "hunter2" not in raw and "tok-abc" not in raw, raw
    assert "win" in raw and "X1" in raw
    reg4 = Registry(path=p, ttl=0)
    assert reg4.load() == 1 and reg4.history()["f:secret"]["kind"] == "fake"
    assert reg4.devices() == [], "重载后不许把历史当在线设备"

    # ⑬ 缓存：TTL 内不重复 probe
    calls = {"n": 0}
    class Count(Fake):
        def probe(self):
            calls["n"] += 1
            return True, ""
    reg5 = Registry(path="", ttl=60)
    reg5.register(Capability("probe.echo", "回声"))
    reg5.add(Count("c1", True, caps=("probe.echo",)))
    for _ in range(5):
        reg5.probe(reg5.device("c1"))
    assert calls["n"] == 1, calls
    reg5.invalidate()
    reg5.probe(reg5.device("c1"))
    assert calls["n"] == 2, calls

    # ⑭ stats 给状态栏用
    st = reg.stats()
    assert st["devices"] == 2 and st["online"] == 1 and st["caps"] == 5, st

    # ⑮ 没声明参数的能力，自动从根目录 capabilities 基线继承
    if CAPSPEC is not None:
        reg6 = Registry(path="", ttl=0)
        reg6.register(Capability("shell.exec", "在节点上执行命令", side_effect=True))
        c = reg6.capability("shell.exec")
        assert [p.name for p in c.params] == ["cmd"] and c.params[0].required, c
        # 显式声明永远压过继承（catalog 是唯一真相）
        reg6.register(Capability("file.pull", "取文件", params=(Param("node", "str"),)))
        assert [p.name for p in reg6.capability("file.pull").params] == ["node"]
        assert not CAPSPEC.known("nope.nope") and not reg6.capability("nope.nope")
    else:
        print("  （无根目录 capabilities 基线，跳过继承断言）")

    for f in (p, p + ".tmp"):
        if os.path.exists(f):
            os.remove(f)
    print("registry 自测：15 组全过（含能力说明书基线继承）")


if __name__ == "__main__":
    _selftest()

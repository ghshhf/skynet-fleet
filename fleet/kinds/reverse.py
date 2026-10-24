# -*- coding: utf-8 -*-
"""逆向拆解设备：把「不暴露的数据」变成能力。

和 Osint 的关系：Osint 读的是**已经公开**的情报（证书透明度、注册信息）；
Reverse 拆的是**不拆就没有**的数据（脉冲编码、广播结构、固件里的硬编码）。
一个读表面，一个拆里面——都是只读，都不发攻击性报文。
"""
from .. import bit_reverse as BR
from .. import proto_reverse as PR
from .. import reverse as RV
from .. import re_llm
from ..core import BaseDevice, DeviceResult as R


class Reverse(BaseDevice):
    kind = "re"
    via = "local"

    def __init__(self, name="re:bench", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("note", "只拆你自己捕获/拥有的数据；LLM 输出标假设")

    def probe(self):
        ok, why = re_llm.available()
        if not ok:
            # 没有 LLM 只是没有「假设层」，确定性拆解照样能跑——如实说清
            return True, "拆解可用；LLM 假设层不可用：%s" % why
        return True, ""

    def caps(self):
        return ("re.ir", "re.ble", "re.binary", "re.proto",
                "re.bits", "re.bits_extract", "re.ask")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "re.ir":
            cap_data = args.get("capture")
            if cap_data is None:
                return R.error(cap, "缺参数 capture（微秒 list / Flipper 文本）",
                               device=self.name, via=self.via)
            m = RV.ir_report(cap_data, llm=bool(args.get("llm", True)))
        elif cap == "re.ble":
            hexs = (args.get("hex") or args.get("data") or "").strip()
            if not hexs:
                return R.error(cap, "缺参数 hex（BLE 广播原文）",
                               device=self.name, via=self.via)
            m = RV.ble_report(hexs, llm=bool(args.get("llm", True)))
        elif cap == "re.binary":
            path = (args.get("path") or "").strip()
            if not path:
                return R.error(cap, "缺参数 path（固件/APK/可执行文件）",
                               device=self.name, via=self.via)
            m = RV.binary_report(path)
            if m.get("ok") and args.get("llm", True):
                m["hypothesis"] = RV.llm_hypothesize("binary", m)
        elif cap == "re.proto":
            samples = args.get("samples")
            if not samples:
                return R.error(cap, "缺参数 samples（多帧 hex 列表；一段长流也行）",
                               device=self.name, via=self.via)
            m = PR.proto_report(samples)
            if m.get("ok") and args.get("llm", True):
                m["hypothesis"] = RV.llm_hypothesize("proto", m)
        elif cap == "re.bits":
            samples = args.get("samples")
            if not samples:
                return R.error(cap, "缺参数 samples（多帧 hex 列表）",
                               device=self.name, via=self.via)
            m = BR.bit_report(samples, order=(args.get("order") or None))
        elif cap == "re.bits_extract":
            samples = args.get("samples")
            if not samples:
                return R.error(cap, "缺参数 samples（多帧 hex 列表）",
                               device=self.name, via=self.via)
            if args.get("bit_len") is None:
                return R.error(cap, "缺参数 bit_len（位宽）",
                               device=self.name, via=self.via)
            m = BR.bit_extract(samples, int(args.get("bit_offset") or 0),
                               int(args["bit_len"]),
                               order=(args.get("order") or "msb"))
        elif cap == "re.ask":
            q = (args.get("question") or "").strip()
            if not q:
                return R.error(cap, "缺参数 question",
                               device=self.name, via=self.via)
            try:
                txt, used = re_llm.ask(q, system=RV._RE_SYS)
                m = {"ok": True, "question": q, "answer": txt, "model": used,
                     "label": "模型回答（可能错，关键结论要交叉验证）"}
            except RuntimeError as e:
                return R.unavailable(cap, str(e)[:160],
                                     device=self.name, via=self.via)
        else:
            return R.unsupported(cap, "re 设备没有这个能力",
                                 device=self.name, via=self.via)
        if not m.get("ok"):
            return R.error(cap, m.get("err") or "拆解失败",
                           device=self.name, via=self.via)
        return R.success(cap, self.name, via=self.via, data=m)

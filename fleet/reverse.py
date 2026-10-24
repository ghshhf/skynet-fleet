# -*- coding: utf-8 -*-
"""逆向/拆解引擎：把「拿不到的数据」拆出来。

定位（为什么值得单独一层）
------------------------
信息获取层（L2-L6）拿的都是**已经暴露**的数据：扫描得到 SSID、拍照得到画面、
crt.sh 吐子域。但还有一类数据是**不暴露、必须拆**才有的：
  - 一串红外脉冲里到底编码了什么（别家遥控器的码）
  - 一段 BLE 广播字节里设备自称什么、厂商是谁
  - 一个固件/APK 里硬编码了哪些 URL、密钥、版本横幅
这就是用户说的「你必须拆解啊，否则根本得不到一些相关数据」。

两段式（和 webtool.py 同一条纪律）
--------------------------------
1. **确定性拆解**：脉冲聚类、AD 结构遍历、strings 分类、码库对照——纯计算，
   答案唯一，不劳模型，也**不允许模型编**。
2. **LLM 假设层**（re_llm）：模型只读第 1 段产出的事实 JSON，输出人话假设。
   假设必须标「假设」，事实必须标「事实」——模型读不懂时会说读不懂，
   而不是把猜测说成结论（诚实原则）。

入口（都返回 dict，ok/err + facts，与 osint.py 同约定）：
  ir_report(capture)     红外捕获 → 协议判定 → addr/cmd → 码库对照
  ble_report(hex)        BLE 广播原文 → AD 结构 → 设备画像
  binary_report(path)    二进制/固件/APK → strings 分类 + 文件魔数
  llm_hypothesize(kind, facts)  上面任一 facts → LLM 假设

支持的真实捕获格式：
  - Flipper Zero `.ir`（parsed 与 raw 两种）、`.sub` 的 RAW_Data
  - ConsumerIrManager 裸 pattern（微秒数组，交替 on/off）
  - BLE HCI 广播原文 hex（hcidump / WireShark 里抄的那种）
"""
import json
import os
import re
import struct

import infrared as IR
import ir_codes as IC

# ================================================================ 红外：脉冲 → 协议

def _cluster(values, tol=0.25):
    """把一堆微秒值按相近程度聚成类。返回 [(center, count)]，按 center 升序。

    不做花哨聚类：先用最小值当种子，线性扫一遍把彼此差 <tol 的并一类。
    遥控脉冲时长高度量子化（都是某个 unit 的整数倍），这招够用也更好解释。
    """
    vals = sorted(v for v in values if v > 0)
    if not vals:
        return []
    groups = []
    for v in vals:
        for g in groups:
            if abs(v - g["sum"] / g["n"]) <= tol * (g["sum"] / g["n"]):
                g["sum"] += v
                g["n"] += 1
                break
        else:
            groups.append({"sum": v, "n": 1})
    return sorted(((g["sum"] / g["n"], g["n"]) for g in groups),
                  key=lambda x: x[0])


def _near(a, b, tol=0.22):
    return b * (1 - tol) <= a <= b * (1 + tol)


def decode_pulses(pairs):
    """微秒脉冲序列（交替 on/off，on 开头）→ 协议判定。

    返回 {"ok", "protocol", "addr", "cmd", "bits", "unit_us", "confidence",
          "histogram", "note"}
    protocol ∈ nec / nec_ext / samsung / sirc / rc5_like / pwm / unknown
    confidence: high=反码校验通过或时序完全吻合规范；low=只是像，别当结论。
    """
    seq = [int(round(x)) for x in pairs if int(x) > 0]
    if len(seq) < 6:
        return {"ok": False, "err": "脉冲太少（<6 段），拆不了",
                "protocol": "unknown"}
    hist = _cluster(seq)
    on = [seq[i] for i in range(0, len(seq), 2)]
    off = [seq[i] for i in range(1, len(seq), 2)]
    lead_m, lead_s = (seq[0], seq[1]) if len(seq) >= 2 else (0, 0)
    unit = min(hist)[0] if hist else 0
    out = {"ok": True, "unit_us": round(unit, 1),
           "histogram": [{"us": round(c, 1), "n": n} for c, n in hist],
           "addr": None, "cmd": None, "bits": None,
           "protocol": "unknown", "confidence": "low"}

    # ---- NEC / Samsung 家族：lead + 每 bit 两个 mark/space，unit≈560 ----
    # 两家 lead 不同（NEC 9000/4500，Samsung 4500/4500），但 bit 结构一样，
    # 所以先用 unit+lead 认家族，再靠反码结构分具体是谁——
    # 判据是数据本身，不是 lead 时长（lead 会因遥控器而异，反码不会）。
    if _near(unit, 560, 0.25) and (_near(lead_m, 9000, 0.2)
                                  or _near(lead_m, 4500, 0.2)):
        bits = []
        i = 2
        ok_parse = True
        while i + 1 < len(seq):
            m, s = seq[i], seq[i + 1]
            if not _near(m, 560, 0.3):
                ok_parse = False
                break
            if _near(s, 1690, 0.3):
                bits.append(1)
            elif _near(s, 560, 0.3):
                bits.append(0)
            else:
                ok_parse = False
                break
            i += 2
        if ok_parse and bits:
            out["bits"] = "".join(str(b) for b in bits)
            n = len(bits)
            addr = cmd = None
            if n >= 32:
                def _byte(bs):
                    return sum(b << i for i, b in enumerate(bs[:8]))
                a1, a2, c1, c2 = (_byte(bits[0:8]), _byte(bits[8:16]),
                                  _byte(bits[16:24]), _byte(bits[24:32]))
                if (a1 ^ a2) == 0xFF and (c1 ^ c2) == 0xFF:
                    addr, cmd = a1, c1
                    out["protocol"] = "nec"
                    out["confidence"] = "high"   # 反码校验过 = 几乎不可能是巧合
                elif (a1 ^ a2) == 0x00:
                    addr, cmd = a1, c1
                    out["protocol"] = "samsung"
                    out["confidence"] = "high"   # 地址发两遍不取反 = 三星特征
            if addr is None and n >= 16:
                # NEC 扩展（16 位地址）兜底：低 16 位当地址、后 8 位命令
                def _byte(bs):
                    return sum(b << i for i, b in enumerate(bs[:8]))
                addr = _byte(bits[0:8]) | (_byte(bits[8:16]) << 8)
                cmd = _byte(bits[16:24]) if n >= 24 else None
                out["protocol"] = "nec_ext"
                out["confidence"] = "low"
            out["addr"] = addr
            out["cmd"] = cmd
            out["note"] = ("lead %.0f/%.0fµs、unit %.0fµs、%d bit（LSB 在前）"
                           % (lead_m, lead_s, unit, n))

    # ---- Sony SIRC：lead 2400/600，PWM（1=1200 通，0=600 通），40kHz ----
    elif _near(lead_m, 2400, 0.2) and _near(unit, 600, 0.25):
        bits = []
        i = 2
        ok_parse = True
        while i + 1 < len(seq):
            m, s = seq[i], seq[i + 1]
            if _near(m, 1200, 0.3):
                bits.append(1)
            elif _near(m, 600, 0.3):
                bits.append(0)
            else:
                ok_parse = False
                break
            if not _near(s, 600, 0.3):
                ok_parse = False
                break
            i += 2
        if ok_parse and bits:
            # SIRC：7 bit 命令在前、5 bit 设备号在后（与 infrared.sony 一致）
            cmd = sum(b << i for i, b in enumerate(bits[:7]))
            addr = sum(b << i for i, b in enumerate(bits[7:12]))
            out.update({"protocol": "sirc", "confidence": "high",
                        "addr": addr, "cmd": cmd,
                        "bits": "".join(map(str, bits)),
                        "note": "SIRC：7bit 命令 + 5bit 设备号，脉宽调制"})

    # ---- RC5 类：曼彻斯特，全程等宽半位（≈889µs），无 lead ----
    elif hist and _near(hist[0][0], 889, 0.15) and _near(hist[-1][0], 889, 0.15):
        n_half = len(seq)
        out.update({"protocol": "rc5_like", "confidence": "low",
                    "bits": "?" * (n_half // 2),
                    "note": ("全程等宽 %.0fµs（%d 个半位）像 RC5 曼彻斯特编码；"
                             " RC5 每比特是「电平翻转」不是「有无」，"
                             "裸 on/off 序列判不了电平，只能到「像」这一层"
                             % (hist[0][0], n_half))})

    # ---- 通用 PWM（两种脉宽 → 占空比编码）----
    elif len(hist) >= 2:
        w1, w2 = hist[0][0], hist[-1][0]
        out.update({"protocol": "pwm", "confidence": "low",
                    "note": ("两种脉宽 %.0f/%.0fµs（占空比 %.2f），"
                             "像 OOK-PWM；够不上已知协议，别硬套"
                             % (w1, w2, w1 / (w1 + w2) if w1 + w2 else 0))})
    else:
        out["note"] = "只有一种脉宽，判不了协议"
    return out


# ================================================================ 真实捕获格式

def parse_flipper_ir(text):
    """Flipper Zero `.ir` 文件 → dict（parsed / raw 两种 type）。"""
    d = {"ok": True, "src": "flipper.ir", "name": "", "type": "",
         "protocol": "", "address": None, "command": None,
         "frequency": None, "duty_cycle": None, "raw": None}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k == "name":
            d["name"] = v
        elif k == "type":
            d["type"] = v
        elif k == "protocol":
            d["protocol"] = v.lower()
        elif k == "address":
            d["address"] = int(v.split()[0], 16) if v.split() else None
        elif k == "command":
            d["command"] = int(v.split()[0], 16) if v.split() else None
        elif k == "frequency":
            d["frequency"] = int(float(v)) if v else None
        elif k == "duty_cycle":
            d["duty_cycle"] = float(v) if v else None
        elif k == "data":
            d["raw"] = [int(x) for x in v.split() if x.lstrip("-").isdigit()]
    if d["type"] == "raw" and not d["raw"]:
        d["ok"] = False
        d["err"] = "raw 类型但没有 data 行"
    elif d["type"] == "parsed" and d["address"] is None:
        d["ok"] = False
        d["err"] = "parsed 类型但没有 address/command"
    return d


# Flipper .sub 的原始行两种写法都见过：`RAW_Data:`（irdb 系）和 `data:`（Flipper 系）
_SUB_RAW = re.compile(r"(?:RAW_Data|data):\s*((?:-?\d+\s*)+)")


def parse_flipper_sub(text):
    """Flipper `.sub` 抓包 → on/off 微秒序列（RAW_Data 行，负数是 off）。"""
    m = _SUB_RAW.search(text or "")
    if not m:
        return {"ok": False, "err": "没有 RAW_Data 行", "src": "flipper.sub"}
    seq = [int(x) for x in m.group(1).split()]
    return {"ok": True, "src": "flipper.sub", "pairs": [abs(x) for x in seq]}


def _as_pairs(capture):
    """capture 统一成 on/off 微秒序列。支持：list / dict(带 pairs) / flipper 文本。"""
    if isinstance(capture, dict):
        if capture.get("pairs"):
            return capture["pairs"], capture.get("src", "dict")
        return None, capture.get("err", "没有 pairs")
    if isinstance(capture, (list, tuple)):
        return [int(x) for x in capture], "raw-list"
    if isinstance(capture, str):
        t = capture.strip()
        if t.upper().startswith("RAW_DATA") or "RAW_Data:" in t:
            r = parse_flipper_sub(t)
            return (r.get("pairs") if r.get("ok") else None), r.get("err", "")
        if ":" in t and ("protocol" in t.lower() or "name" in t.lower()):
            f = parse_flipper_ir(t)
            if f.get("ok"):
                if f.get("raw"):
                    return f["raw"], "flipper.ir(raw)"
                return None, "parsed 格式（无脉冲序列，直接用 address/command）"
            return None, f.get("err", "")
        # 兜底：当成空格分隔数字
        try:
            return [int(x) for x in t.replace(",", " ").split()], "str-list"
        except ValueError:
            return None, "capture 认不出来（要 list / flipper 文本 / 数字串）"
    return None, "capture 类型不支持"


# ================================================================ 红外：码库对照

def codebook_crossref(proto, addr, cmd):
    """拿拆出来的 (proto, addr, cmd) 对照码库：是已知键还是新码？

    返回 {"known": bool, "hits": [...], "proposal": {...}}
    新码 → proposal 给出可直接并进 ir_codes/ 的 JSON 片段（人工确认后才入库，
    不自动写——码库是给人用的，编错的码比没有码更糟）。
    """
    hits = []
    for meta in (IC.available_brands() or []):
        brand, dev = meta.get("brand"), meta.get("device")
        if not (brand and dev):
            continue
        cb = IC.load_codebook(brand, dev) or {}
        if (cb.get("protocol") or "").lower() != (proto or "").lower():
            continue
        for key, rec in (cb.get("keys") or {}).items():
            if rec.get("addr") == addr and rec.get("cmd") == cmd:
                hits.append({"brand": brand, "device": dev, "key": key})
    out = {"known": bool(hits), "hits": hits[:8]}
    if not hits and addr is not None and cmd is not None:
        out["proposal"] = {"addr": addr, "cmd": cmd,
                           "protocol": (proto or "").lower(),
                           "note": "新码：确认对应按键后，把这条并进 ir_codes/<品牌>/<设备>.json"}
    return out


# ================================================================ BLE 广播拆解

_AD_TYPES = {
    0x01: "flags", 0x02: "uuid16_partial", 0x03: "uuid16",
    0x04: "uuid32_partial", 0x05: "uuid32",
    0x06: "uuid128_partial", 0x07: "uuid128",
    0x08: "name_short", 0x09: "name", 0x0A: "tx_power",
    0x16: "service_data_16", 0x20: "service_data_32", 0x21: "service_data_128",
    0xFF: "manufacturer",
}
# 常见厂商 ID（蓝牙 SIG 分配表里那几个天天出现的）
_COMPANIES = {
    0x004C: "Apple", 0x0006: "Microsoft", 0x0075: "Samsung",
    0x00E0: "Google", 0x038F: "Xiaomi", 0x0157: "Xiaomi",
    0x0499: "Ruuvi", 0x0059: "Nordic", 0x0001: "Nokia",
}


def _hex_to_bytes(s):
    t = re.sub(r"[^0-9a-fA-F]", "", s or "")
    if len(t) % 2:
        t = t[:-1]
    return bytes.fromhex(t)


def parse_ble_adv(hex_str):
    """BLE 广播原文 hex → AD 结构拆解。

    这就是「不拆就没有」的那类数据：裸字节里设备自称什么、带哪些服务 UUID、
    厂商数据里藏了什么（iBeacon 的 UUID/major/minor 也在这层）。
    """
    try:
        b = _hex_to_bytes(hex_str)
    except ValueError:
        return {"ok": False, "err": "不是合法 hex"}
    if not b:
        return {"ok": False, "err": "空"}

    out = {"ok": True, "ad": [], "flags": None, "name": None,
           "uuids": [], "manufacturer": None, "service_data": [],
           "tx_power": None, "ibeacon": None, "raw_len": len(b)}
    i = 0
    while i < len(b):
        ln = b[i]
        if ln == 0:
            break
        if i + 1 + ln > len(b):
            out["ad"].append({"type_hex": "??", "err": "长度越界，后面截断"})
            break
        t = b[i + 1]
        v = b[i + 2:i + 1 + ln]
        name = _AD_TYPES.get(t, "type_0x%02X" % t)
        rec = {"type_hex": "0x%02X" % t, "type": name, "value_hex": v.hex()}
        if t == 0x01:
            out["flags"] = v.hex()
            rec["flags"] = {"le_limited_discoverable": bool(v and v[0] & 0x01),
                            "le_general_discoverable": bool(v and v[0] & 0x02),
                            "br_edr_not_supported": bool(v and v[0] & 0x04)}
        elif t in (0x08, 0x09):
            try:
                out["name"] = v.decode("utf-8")
                rec["name"] = out["name"]
            except UnicodeDecodeError:
                rec["name"] = "<非 UTF-8>"
        elif t in (0x02, 0x03, 0x04, 0x05, 0x06, 0x07):
            sz = {0x02: 2, 0x03: 2, 0x04: 4, 0x05: 4, 0x06: 16, 0x07: 16}[t]
            for j in range(0, len(v) - sz + 1, sz):
                if sz == 2:
                    u = "%04X" % struct.unpack("<H", v[j:j + 2])[0]
                elif sz == 4:
                    u = "%08X" % struct.unpack("<I", v[j:j + 4])[0]
                else:
                    u = v[j:j + 16].hex().upper()
                out["uuids"].append(u)
        elif t == 0x0A:
            out["tx_power"] = struct.unpack("<b", v[:1])[0] if v else None
        elif t in (0x16, 0x20, 0x21):
            sd = {"uuid": None, "data": v[2:].hex() if len(v) > 2 else ""}
            if t == 0x16 and len(v) >= 2:
                sd["uuid"] = "%04X" % struct.unpack("<H", v[:2])[0]
            out["service_data"].append(sd)
        elif t == 0xFF and len(v) >= 2:
            cid = struct.unpack("<H", v[:2])[0]
            data = v[2:]
            out["manufacturer"] = {
                "company_id": "0x%04X" % cid,
                "company": _COMPANIES.get(cid, "未登记/未知"),
                "data_hex": data.hex(), "data_len": len(data)}
            # iBeacon：Apple(0x4C) + 子类型 0x02 + 长度 0x15
            if cid == 0x004C and len(data) >= 23 and data[0] == 0x02 and data[1] == 0x15:
                try:
                    out["ibeacon"] = {
                        "uuid": str(data[2:18]).upper() if False else data[2:18].hex().upper(),
                        "major": struct.unpack(">H", data[18:20])[0],
                        "minor": struct.unpack(">H", data[20:22])[0],
                        "measured_power": struct.unpack("<b", data[22:23])[0]}
                except struct.error:
                    pass
        out["ad"].append(rec)
        i += 1 + ln
    if not out["ad"]:
        out["ok"] = False
        out["err"] = "一个完整 AD 结构都没解析出来（字节可能不是广播原文）"
    return out


# ================================================================ 二进制 strings

_RE_URL = re.compile(rb"(https?|ftp)://[^\s\"'<>]{4,120}")
_RE_IP = re.compile(rb"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
                    rb"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b")
_RE_EMAIL = re.compile(rb"[A-Za-z0-9._%+-]{2,64}@[A-Za-z0-9.-]{2,64}\.[A-Za-z]{2,12}")
_RE_HEX64 = re.compile(rb"\b[0-9a-fA-F]{64}\b")
_RE_HEX32 = re.compile(rb"\b[0-9a-fA-F]{32}\b")
_RE_VER = re.compile(rb"\b\d+\.\d+(?:\.\d+){0,2}(?:[-.][A-Za-z0-9]+)?\b")
_RE_PATH = re.compile(rb"(?:/[A-Za-z0-9._-]+){2,}")
_RE_B64 = re.compile(rb"\b[A-Za-z0-9+/]{24,}={0,2}\b")

_MAGIC = [
    (b"\x7fELF", "ELF 可执行/库"),
    (b"MZ", "PE 可执行（Windows）"),
    (b"PK\x03\x04", "zip（APK/jar 也是这个）"),
    (b"dex\n", "Dalvik dex"),
    (b"\xde\xad\xbe\xef", "ARM 固件镜像"),
    (b"uboot", "U-Boot 头"),
]


def binary_strings(data, min_len=6, top=8):
    """抽可打印字符串并按类型分类。返回 {counts, 分类明细}。"""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    runs = re.findall(rb"[\x20-\x7e]{%d,}" % min_len, data)
    cats = {"url": [], "ip": [], "email": [], "hex64": [], "hex32": [],
            "version": [], "path": [], "base64ish": []}
    for s in runs:
        if _RE_URL.search(s):
            cats["url"].append(s[:100])
        elif _RE_EMAIL.search(s):
            cats["email"].append(s[:80])
        elif _RE_IP.search(s):
            cats["ip"].append(s[:60])
        elif _RE_HEX64.search(s):
            cats["hex64"].append(s[:70])
        elif _RE_HEX32.search(s):
            cats["hex32"].append(s[:40])
        elif _RE_VER.search(s):
            cats["version"].append(s[:50])
        elif _RE_PATH.search(s):
            cats["path"].append(s[:80])
        elif _RE_B64.search(s) and len(s) >= 24:
            cats["base64ish"].append(s[:60])
    def _s(x):
        return x.decode("utf-8", "replace")

    return {k: [_s(x) for x in v[:top]] for k, v in cats.items()}, len(runs)


def binary_report(path, max_bytes=8 * 1024 * 1024):
    """文件 → strings 分类 + 魔数 + 大小。只读，绝不上传原文。"""
    if not os.path.isfile(path):
        return {"ok": False, "err": "文件不存在：%s" % path}
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(max_bytes)
    magic = next((m for sig, m in _MAGIC if head.startswith(sig)), None)
    cats, n_runs = binary_strings(head)
    return {"ok": True, "path": path, "size": size, "magic": magic,
            "strings_total": n_runs, "strings": cats,
            "note": ("只看前 %d 字节；分类是正则猜的，"
                     "hex32/hex64 是「像」不「是」" % min(size, max_bytes))}


# ================================================================ 汇总入口

def ir_report(capture, llm=False):
    """红外捕获 → 拆解全流程。capture：list/flipper 文本/dict。"""
    pairs, src = _as_pairs(capture)
    facts = {"ok": False, "src": src}
    if pairs:
        d = decode_pulses(pairs)
        facts.update(d)
        if d.get("ok") and d.get("addr") is not None:
            facts["codebook"] = codebook_crossref(
                d.get("protocol"), d.get("addr"), d.get("cmd"))
    else:
        facts["err"] = src or "拿不到脉冲序列"
        # flipper parsed 格式：直接就是答案，不用拆脉冲
        if isinstance(capture, str):
            f = parse_flipper_ir(capture)
            if f.get("ok") and f.get("address") is not None:
                facts = {"ok": True, "src": "flipper.ir(parsed)",
                         "protocol": f["protocol"], "addr": f["address"],
                         "cmd": f["command"], "confidence": "high",
                         "note": "parsed 格式自带地址/命令，无需反推",
                         "codebook": codebook_crossref(
                             f["protocol"], f["address"], f["command"])}
    if llm and facts.get("ok"):
        facts["hypothesis"] = llm_hypothesize("ir", facts)
    return facts


def ble_report(hex_str, llm=False):
    facts = parse_ble_adv(hex_str)
    if llm and facts.get("ok"):
        facts["hypothesis"] = llm_hypothesize("ble", facts)
    return facts


_RE_SYS = ("你是无线电/嵌入式逆向工程师。只根据给定的事实 JSON 说话："
           "能推出什么就说什么，推不出就说「事实不足，别硬猜」。"
           "先说结论再说依据；结论里区分「事实」与「推断」，推断必须标『推断』。"
           "200 字以内。")


def llm_hypothesize(kind, facts):
    """事实 → LLM 假设。断网/无额度时返回带原因的降级说明（不假装有假设）。"""
    from . import re_llm
    ok, why = re_llm.available()
    if not ok:
        return {"ok": False, "err": "LLM 不可用：%s" % why}
    prompt = ("下面是%s拆解得到的事实 JSON。请给出：①这是什么（协议/设备/内容）"
              "②依据（引用 JSON 里的哪些字段）③还缺什么数据才能进一步确认。\n%s"
              % ({"ir": "红外脉冲", "ble": "BLE 广播", "binary": "二进制文件"
                  }.get(kind, kind),
                 json.dumps(facts, ensure_ascii=False)[:2600]))
    try:
        txt, used = re_llm.ask(prompt, system=_RE_SYS)
        return {"ok": True, "model": used, "text": txt,
                "label": "假设（模型读事实后的推断，不是测量结论）"}
    except RuntimeError as e:
        return {"ok": False, "err": str(e)[:160]}


# ================================================================ 自测
def _selftest():
    ok = fail = 0

    def chk(cond, what):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print("  [X] %s" % what)

    # ① NEC 往返：用红外编码器造一串，拆回 addr/cmd（4/8 = 小米电源）
    freq, pat = IR.nec(0x04, 0x08)
    d = decode_pulses(pat)
    chk(d["ok"] and d["protocol"] == "nec" and d["addr"] == 4
        and d["cmd"] == 8 and d["confidence"] == "high",
        "NEC 编→拆往返（addr=4 cmd=8，反码校验 high）")

    # ② Samsung：addr 发两遍
    _f, p2 = IR.samsung(0x07, 0x02)
    d2 = decode_pulses(p2)
    chk(d2["protocol"] == "samsung" and d2["addr"] == 7 and d2["cmd"] == 2,
        "Samsung 判定（addr=7 cmd=2）")

    # ③ SIRC
    _f, p3 = IR.sony(0x1A, 0x46)
    d3 = decode_pulses(p3)
    chk(d3["protocol"] == "sirc" and d3["addr"] == 0x1A and d3["cmd"] == 0x46,
        "SIRC 判定（addr=0x1A cmd=0x46）")

    # ④ NEC 扩展 16 位地址
    _f, p4 = IR.nec_ext(0x1234, 0x56)
    d4 = decode_pulses(p4)
    chk(d4["protocol"] == "nec_ext" and d4["addr"] == 0x1234 and d4["cmd"] == 0x56,
        "NEC 扩展（16 位地址 0x1234）")

    # ⑤ 未知 PWM：两种脉宽，不许硬套协议
    d5 = decode_pulses([300, 300] * 3 + [900, 300] * 3)
    chk(d5["protocol"] == "pwm" and d5["confidence"] == "low",
        "未知 PWM 只报 pwm/low，不冒充 NEC")

    # ⑥ 码库对照：4/8 必须是已知键（小米电视电源）
    x = codebook_crossref("nec", 4, 8)
    chk(x["known"] and any(h["brand"] == "小米" and h["key"] == "power"
                           for h in x["hits"]),
        "码库对照命中 小米/电视/power")
    # ⑦ 新码 → proposal（不自动入库）
    x2 = codebook_crossref("nec", 0xAB, 0xCD)
    chk((not x2["known"]) and x2.get("proposal", {}).get("addr") == 0xAB,
        "新码给 proposal 并标『人工确认后入库』")

    # ⑧ Flipper parsed 文本
    f = parse_flipper_ir("name: Power\ntype: parsed\nprotocol: NEC\n"
                         "address: 04 00 00 00\ncommand: 08 00 00 00\n"
                         "frequency: 38000\nduty_cycle: 0.330000\n")
    chk(f["ok"] and f["protocol"] == "nec" and f["address"] == 4
        and f["command"] == 8 and f["frequency"] == 38000,
        "Flipper .ir parsed 解析")
    r8 = ir_report("name: Power\ntype: parsed\nprotocol: NEC\n"
                   "address: 04 00 00 00\ncommand: 08 00 00 00\n")
    chk(r8["ok"] and r8["codebook"]["known"], "parsed 直通码库对照")

    # ⑨ Flipper .sub RAW_Data
    sub = ("Filetype: IR signals file\nVersion: 1\n#\nname: test\n"
           "type: raw\nfrequency: 38000\nduty_cycle: 0.33\n"
           "data: 9000 -4500 560 -560 560 -1690 560 -560\n")
    r9 = parse_flipper_sub(sub)
    chk(r9["ok"] and r9["pairs"][:2] == [9000, 4500], ".sub RAW_Data 取绝对值序列")

    # ⑩ BLE：手搓一条广播（flags + 名字 + Apple 厂商数据）
    # AD 长度字节含 type 自身：iBeacon 厂商段 = type(1)+cid(2)+数据(23) = 26 = 0x1A
    adv = (bytes([0x02, 0x01, 0x06])
           + bytes([0x05, 0x09]) + b"Pods"
           + bytes([0x1A, 0xFF, 0x4C, 0x00, 0x02, 0x15])
           + bytes(range(16))          # iBeacon UUID 16 字节
           + bytes([0x00, 0x01, 0x00, 0x02, 0xC5]))
    b = parse_ble_adv(adv.hex())
    chk(b["ok"] and b["name"] == "Pods" and b["flags"] == "06"
        and b["manufacturer"]["company"] == "Apple",
        "BLE 拆解：flags/名字/Apple 厂商")
    chk(b["ibeacon"] and b["ibeacon"]["major"] == 1 and b["ibeacon"]["minor"] == 2,
        "iBeacon UUID/major/minor 拆出")

    # ⑪ BLE 非广播字节 → 诚实报错（长度 0 = 一个结构都没有）
    b2 = parse_ble_adv("00")
    chk((not b2["ok"]) and b2.get("err"), "垃圾字节诚实报错")

    # ⑫ 二进制 strings 分类
    blob = (b"\x00\x00GET https://api.example.com/v1/status HTTP/1.1\x00"
            b"user@example.com\x00192.168.1.42\x00"
            b"\x00" + b"a1b2c3d4" * 8 + b"\x00"          # 独立的 64 位 hex
            b"v2.14.3-beta\x00/usr/local/bin/tool\x00"
            b"\x7fELF\x02\x01\x01\x00")
    cats, n = binary_strings(blob)
    chk(any("api.example.com" in u for u in cats["url"]), "strings：URL")
    chk(any("user@example.com" in e for e in cats["email"]), "strings：邮箱")
    chk(any("192.168.1.42" in i for i in cats["ip"]), "strings：IPv4")
    chk(len(cats["hex64"]) >= 1, "strings：64 位 hex（像密钥）")
    chk(any("2.14.3" in v for v in cats["version"]), "strings：版本号")
    chk(any("usr/local/bin" in p for p in cats["path"]), "strings：路径")

    # ⑬ ELF 魔数
    rep = binary_report(__file__ if os.path.isfile(__file__) else ".")
    # （__file__ 是 .py，测不了魔数；单独造个 ELF 头文件测）
    import tempfile
    tmp = tempfile.mkdtemp()
    elf = os.path.join(tmp, "fw.bin")
    with open(elf, "wb") as fh:
        fh.write(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8 + b"http://iot.vendor.io/ota")
    rep = binary_report(elf)
    chk(rep["ok"] and rep["magic"] == "ELF 可执行/库"
        and any("iot.vendor.io" in u for u in rep["strings"]["url"]),
        "binary_report：ELF 魔数 + 固件里的 OTA 地址")

    # ⑭ 空输入不崩
    chk(decode_pulses([1, 2, 3])["ok"] is False, "脉冲太短诚实报错")

    print("reverse 自测：%d 过 %d 挂" % (ok, fail))
    return fail == 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv or not sys.argv[1:]:
        sys.exit(0 if _selftest() else 1)
    # 命令行速用：python -m fleet.reverse ir "name: ..." / ble <hex> / binary <path>
    op = sys.argv[1]
    if op == "ir":
        print(json.dumps(ir_report(" ".join(sys.argv[2:])), ensure_ascii=False,
                         indent=1))
    elif op == "ble":
        print(json.dumps(ble_report(" ".join(sys.argv[2:])), ensure_ascii=False,
                         indent=1))
    elif op == "binary":
        print(json.dumps(binary_report(sys.argv[2]), ensure_ascii=False,
                         indent=1))
    else:
        print("用法：python -m fleet.reverse ir|ble|binary …")

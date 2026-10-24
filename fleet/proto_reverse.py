# -*- coding: utf-8 -*-
"""未知二进制协议拆解：从「一串字节」推到「字段 + 校验算法 + 帧长」。

为什么单独一层：`fleet/reverse.py` 里已有的都是**认格式**（红外 NEC/Sony、
BLE 广播头、文件魔数）。认格式的本事上限就是「我见过」——遇到私有协议就只剩
「输出十六进制给人看」。协议逆向真正的分界不是认得多少个协议，而是
**没见过的时候能不能从数据本身把结构推出来**。

方法论三条，缺一条结论就变成猜：

  ① **多帧**。单帧推不出任何字段语义——常量/变量/校验都要对比才有意义。
     本模块的 `proto_report` 只收样本列表；给一帧时会明确说「只有一帧，
     只能列字节，不能判字段」。
  ② **列统计**。把 N 帧按字节位置对齐成表，按**列**判行为
     （常量 / 递增 / 固定步长 / 枚举 / 双值 / 长度 / 随机），再合并相邻同类成字段。
  ③ **校验必须全帧吻合**。1 字节校验单帧撞上的概率约 1/256，
     只验一帧等于没验 —— 本模块只报「所有帧都对上」的算法，
     并把「几帧支持」一起报出来。

诚实边界（都会在输出里写明，不装懂）：
  · 只做**字节对齐**分析。位域（bitfield）不拆，结论里会提示「可能有位域未拆」；
  · 变长帧先按长度**分组**，跨长度对齐不猜；
  · 校验命中 ≠ 协议懂了。只说明「这几个位置的字节与该算法一致」。
    等价形式（8bit sum 与 neg-sum 在某些数据下同解、crc16 的 BE/LE 两种摆放）
    会同时列出，由人判断，不替人拍板；
  · 取到的样本如果全都一样（测试时常犯），会判「无变化 ⇒ 拆不出字段结构」，
    而不是硬编几个字段交差。

纯本地、纯只读：不联网、不改文件、不发任何报文。
"""
import math
import re

# ================================================================ 校验算法

def crc8(data, poly=0x07, init=0x00):
    """CRC-8/SMBUS 风格（poly 0x07，MSB first）。"""
    c = init & 0xFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ poly) & 0xFF if (c & 0x80) else ((c << 1) & 0xFF)
    return c


def crc8_maxim(data, init=0x00):
    """CRC-8/MAXIM-DOW（反射 poly 0x8C）——很多传感器用这个。"""
    c = init & 0xFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0x8C if (c & 1) else (c >> 1)
    return c


def crc16_ccitt(data, init=0xFFFF):
    """CRC-16/CCITT-FALSE（poly 0x1021，init 0xFFFF，不反射）。"""
    c = init & 0xFFFF
    for b in data:
        c ^= (b << 8)
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if (c & 0x8000) else ((c << 1) & 0xFFFF)
    return c


def crc16_xmodem(data, init=0x0000):
    return crc16_ccitt(data, init)


def crc16_modbus(data, init=0xFFFF):
    """CRC-16/MODBUS（反射 poly 0xA001）。"""
    c = init & 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if (c & 1) else (c >> 1)
    return c


def crc32(data):
    """CRC-32/ISO-HDLC（zip/以太网那个）。"""
    c = 0xFFFFFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xEDB88320 if (c & 1) else (c >> 1)
    return c ^ 0xFFFFFFFF


def sum8(data):
    return sum(data) & 0xFF


def sum8_neg(data):
    """反码和：(−sum) & 0xFF —— 很多老设备用「加起来等于 0」的自校验。"""
    return (-sum(data)) & 0xFF


def xor8(data):
    c = 0
    for b in data:
        c ^= b
    return c


def sum16(data):
    return sum(data) & 0xFFFF


# name → (函数, 输出宽度字节)
ALGOS = {
    "crc8/0x07":        (crc8, 1),
    "crc8/maxim":       (crc8_maxim, 1),
    "sum8":             (sum8, 1),
    "sum8_neg":         (sum8_neg, 1),
    "xor8":             (xor8, 1),
    "crc16/ccitt":      (crc16_ccitt, 2),
    "crc16/xmodem":     (crc16_xmodem, 2),
    "crc16/modbus":     (crc16_modbus, 2),
    "sum16":            (sum16, 2),
    "crc32":            (crc32, 4),
}

# 分析规模上限：再多就放弃统计精度换响应速度，并如实说「已采样」
MAX_FRAMES = 96
MAX_LEN = 256
# 帧数 × 帧长 的预算：控制校验暴力的成本（超了降帧数，不降结论诚实度）
FRAME_BUDGET = 6000


# ============================================================ 增量校验（性能关键）

# 暴力版是 O(帧数 × 位置 × 算法 × 帧长)：64 字节帧、100 帧就要上千万次位运算，
# 长帧直接卡死。但**所有经典校验都是增量的**（CRC 寄存器本身就是状态机，
# sum/xor 是累加器）⇒ 每个算法对每帧只扫一遍，就能同时得到**所有前缀**的校验值。
# 复杂度降到 O(算法 × 帧数 × 帧长)，快两个数量级，且结果与暴力版逐位等价。
_INCR = {}


def _inc(name, init, step, final=None):
    _INCR[name] = (init, step, final or (lambda s: s))


def _step_sum8(s, b):
    return (s + b) & 0xFF


def _step_xor8(s, b):
    return s ^ b


def _step_crc8(s, b):
    s ^= b
    for _ in range(8):
        s = ((s << 1) ^ 0x07) & 0xFF if (s & 0x80) else ((s << 1) & 0xFF)
    return s


def _step_crc8_maxim(s, b):
    s ^= b
    for _ in range(8):
        s = (s >> 1) ^ 0x8C if (s & 1) else (s >> 1)
    return s


def _step_crc16_ccitt(s, b):
    s ^= (b << 8)
    for _ in range(8):
        s = ((s << 1) ^ 0x1021) & 0xFFFF if (s & 0x8000) else ((s << 1) & 0xFFFF)
    return s


def _step_crc16_modbus(s, b):
    s ^= b
    for _ in range(8):
        s = (s >> 1) ^ 0xA001 if (s & 1) else (s >> 1)
    return s


def _step_sum16(s, b):
    return (s + b) & 0xFFFF


def _step_crc32(s, b):
    s ^= b
    for _ in range(8):
        s = (s >> 1) ^ 0xEDB88320 if (s & 1) else (s >> 1)
    return s


_inc("crc8/0x07", 0x00, _step_crc8)
_inc("crc8/maxim", 0x00, _step_crc8_maxim)
_inc("sum8", 0x00, _step_sum8)
_inc("sum8_neg", 0x00, _step_sum8, lambda s: (-s) & 0xFF)
_inc("xor8", 0x00, _step_xor8)
_inc("crc16/ccitt", 0xFFFF, _step_crc16_ccitt)
_inc("crc16/xmodem", 0x0000, _step_crc16_ccitt)
_inc("crc16/modbus", 0xFFFF, _step_crc16_modbus)
_inc("sum16", 0x00, _step_sum16)
_inc("crc32", 0xFFFFFFFF, _step_crc32, lambda s: s ^ 0xFFFFFFFF)


# ================================================================ 样本规整

_RE_HEX = re.compile(r"(?:0x)?([0-9a-fA-F]{2})")


def to_bytes(x):
    """hex 字符串（可带空格/0x/冒号/横线）/ bytes / bytearray / int 列表 → bytes。"""
    if isinstance(x, (bytes, bytearray)):
        return bytes(x)
    if isinstance(x, (list, tuple)):
        return bytes(b & 0xFF for b in x)
    if isinstance(x, str):
        s = x.strip()
        # 先试「连续 hex」，再退化到逐组匹配（容忍 "AA BB:CC-11" 这类写法）
        compact = re.sub(r"[\s:\-,]", "", s)
        if len(compact) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]*", compact):
            return bytes.fromhex(compact)
        return bytes(int(g, 16) for g in _RE_HEX.findall(s))
    raise ValueError("样本类型不支持：%r" % (type(x).__name__,))


def _norm_samples(samples):
    """→ (frames, meta)。frames 是 bytes 列表。"""
    if isinstance(samples, (bytes, bytearray, str)):
        samples = [samples]
    seq = list(samples)          # 先落地：generator 只能走一次
    frames = []
    bad = 0
    for s in seq:
        try:
            b = to_bytes(s)
        except Exception:
            bad += 1
            continue
        if b:
            frames.append(b)
    return frames, {"input": len(seq), "usable": len(frames), "unparsable": bad}


# ================================================================ 帧头猜测

def guess_head(frames, max_head=8):
    """最长公共前缀（太短不算：1 字节相同毫无信息量）。返回 (head, len)。"""
    if len(frames) < 2:
        return b"", 0
    ln = min(len(f) for f in frames)
    n = 0
    while n < min(ln, max_head) and len({f[n] for f in frames}) == 1:
        n += 1
    return frames[0][:n], n


def guess_period(stream, max_head=8, min_repeats=3):
    """单段长流 → 猜帧长（周期）。

    做法：把流的开头当帧头候选，找它在流里再次出现的位置，
    要求**所有间隔完全相同**（数据会变但帧头不会）。
    返回候选列表，按（头长降序, 周期升序）—— 头越长越具体。
    """
    n = len(stream)
    out = []
    for h in range(2, min(max_head, n // 2) + 1):
        head = stream[:h]
        poses = [i for i in range(0, n - h + 1) if stream[i:i + h] == head]
        if len(poses) < min_repeats:
            continue
        gaps = [poses[i + 1] - poses[i] for i in range(len(poses) - 1)]
        if len(set(gaps)) == 1 and gaps[0] >= h:
            out.append({"head": head.hex(), "head_len": h,
                        "period": gaps[0], "repeats": len(poses)})
    out.sort(key=lambda d: (-d["head_len"], d["period"]))
    return out


# ================================================================ 列统计

def column_stats(frames):
    """按字节位置对齐成表，逐列判行为。

    kind 取值（按判定优先级）：
      const    该列所有帧同值
      length   该列值恰好等于「帧总长」或「从该列起到帧尾的剩余长度」
      counter  逐帧 +1（mod 256）
      step     逐帧固定步长（≠1）
      twin     只出现两个值（开关/标志位候选）
      enum     值域很小但不是逐帧规律（命令码候选）
      var      其余（高变化率）
    """
    n = len(frames)
    if not n:
        return []
    ln = min(len(f) for f in frames)
    lens = [len(f) for f in frames]
    same_len = len(set(lens)) == 1
    cols = []
    for i in range(ln):
        vals = [f[i] for f in frames]
        uniq = sorted(set(vals))
        kind, note = "var", ""
        if len(uniq) == 1:
            kind = "const"
        elif same_len and all(v == lens[0] for v in vals):
            kind, note = "length", "= 帧总长"
        elif same_len and all(lens[0] - i == v for v in vals):
            kind, note = "length", "= 从本列起到帧尾的字节数"
        else:
            deltas = {(vals[k + 1] - vals[k]) % 256 for k in range(len(vals) - 1)}
            if len(deltas) == 1:
                d = deltas.pop()
                if d == 1:
                    kind = "counter"
                elif d not in (0,):
                    kind, note = "step", "步长 %d" % d
        if kind == "var":
            if len(uniq) == 2:
                kind = "twin"
            elif len(uniq) <= max(2, n // 4) and len(uniq) < n:
                kind = "enum"
        cols.append({"offset": i, "kind": kind, "uniq": len(uniq),
                     "values": uniq[:6], "note": note})
    return cols


_FIELD_LABEL = {
    "const": "常量", "length": "长度", "counter": "递增计数",
    "step": "固定步长", "twin": "双值", "enum": "小值域", "var": "变化",
}


def split_fields(cols):
    """相邻同类列合并成字段（同 kind 且同「值指纹」才算一个字段）。"""
    if not cols:
        return []
    out, cur = [], None
    for c in cols:
        key = (c["kind"], c["note"], tuple(c["values"][:1]) if c["kind"] == "const" else ())
        if cur and cur["_key"] == key:
            cur["length"] += 1
            cur["vals"].append(c["values"][0])
        else:
            if cur:
                out.append(cur)
            cur = {"offset": c["offset"], "length": 1, "kind": c["kind"],
                   "note": c["note"], "_key": key, "vals": [c["values"][0]],
                   "uniq": c["uniq"]}
    if cur:
        out.append(cur)
    for f in out:
        f.pop("_key", None)
        f["label"] = _FIELD_LABEL.get(f["kind"], f["kind"])
        vs = f.pop("vals")
        if f["kind"] == "const":
            f["value_hex"] = bytes([vs[0]] * f["length"]).hex()
        else:
            f["examples"] = vs[:6]
    return out


# ================================================================ 校验识别

def checksum_hunt(frames, max_pos=None, want=None):
    """找「哪几个字节是前面数据的校验」。

    对每个候选位置 pos、每个算法，要求**所有帧**都吻合（不是多数）：
    8bit 算法单帧蒙对概率 1/256，只用一帧验等于没验。

    走增量表：每个算法对每帧扫一遍，得到 pos=0..n 全部前缀的校验状态，
    再逐 pos 与「帧里那几个字节」比。等价于暴力版，快两个数量级。

    返回命中列表，每条带 pos / algo / width / endian / span / frames_ok。
    """
    if len(frames) < 2:
        return []
    ln = min(len(f) for f in frames)
    lim = min(ln, max_pos or MAX_LEN)
    if lim < 3:
        return []
    hits = []
    for name, (init, step, final) in _INCR.items():
        if want and name not in want:
            continue
        width = ALGOS[name][1]
        if lim <= width:
            continue
        # states[k][pos] = 第 k 帧喂完前 pos 个字节后的状态
        states = []
        for f in frames:
            s = init
            row = [s]
            for b in f[:lim]:
                s = step(s, b)
                row.append(s)
            states.append(row)
        for pos in range(1, lim - width + 1):
            # 1 字节校验没有字节序之分：报一条就够，报两条是自我重复
            for endian in (("big",) if width == 1 else ("big", "little")):
                ok = True
                for k, f in enumerate(frames):
                    if final(states[k][pos]) != int.from_bytes(f[pos:pos + width], endian):
                        ok = False
                        break
                if ok:
                    hits.append({"pos": pos, "algo": name, "width": width,
                                 "endian": endian, "span": [0, pos],
                                 "frames_ok": len(frames)})
    hits.sort(key=lambda h: (-h["width"], h["pos"], h["algo"]))
    return hits


def _carve_checksums(fields, hits):
    """把校验命中的字节从「变化」字段里切出来，独立成一个字段。

    为什么必须切：4 字节载荷 + 1 字节 sum8 会被列统计合并成一个 5 字节
    「变化」字段 —— 那等于**没拆**，报告看着有字段其实含糊。
    只切完全落在字段内部、彼此不重叠的命中（按宽度降序先到先得）。
    """
    if not hits or not fields:
        return fields
    used = []
    for h in hits:
        s, e = h["pos"], h["pos"] + h["width"]
        if any(not (e <= a or s >= b) for a, b in used):
            continue
        used.append((s, e))
    if not used:
        return fields
    used.sort()
    out = []
    for f in fields:
        fs, fe = f["offset"], f["offset"] + f["length"]
        cuts = [(s, e) for (s, e) in used if s >= fs and e <= fe and s + (e - s) <= fe]
        if not cuts or f["kind"] in ("const", "length", "counter", "step"):
            out.append(f)
            continue
        cur = fs
        for s, e in cuts:
            if s > cur:
                out.append(_subfield(f, cur, s - cur, None))
            hit = next(x for x in hits if x["pos"] == s and x["pos"] + x["width"] == e)
            out.append(_subfield(f, s, e - s, hit))
            cur = e
        if cur < fe:
            out.append(_subfield(f, cur, fe - cur, None))
    return out


def _subfield(f, off, length, hit):
    d = {"offset": off, "length": length, "kind": f["kind"],
         "label": f.get("label"), "note": f.get("note", "")}
    if hit:
        d["kind"] = "checksum"
        d["label"] = "校验"
        d["checksum"] = "%s(%d字节,%s)" % (hit["algo"], hit["width"], hit["endian"])
    elif f.get("value_hex"):
        d["value_hex"] = f["value_hex"]
    elif f.get("examples"):
        # 子段的样例值在切分后不再准确，宁可不给也不给错的
        d["note"] = (d["note"] + "；" if d["note"] else "") + "从多字节字段切出，样例值已省略"
    return d


# ================================================================ 主入口

def proto_report(samples, llm_facts=False, hunt_checksum=True):
    """多帧样本 → 结构报告。

    samples：hex 字符串 / bytes / 列表（一帧一个元素）。也接受单段长流
    （含帧头重复时用 guess_period 自动切帧）。
    """
    frames, meta = _norm_samples(samples)
    if not frames:
        return {"ok": False, "err": "没有可用样本（解析后为空）"}

    # 单段长流 → 先尝试自动切帧
    auto_split = None
    if len(frames) == 1 and len(frames[0]) >= 12:
        cands = guess_period(frames[0], min_repeats=3)
        if cands:
            p = cands[0]["period"]
            hl = cands[0]["head_len"]
            stream = frames[0]
            cut = [stream[i:i + p] for i in range(hl, len(stream) - p + 1, p)]
            if len(cut) >= 2:
                # 把第一帧接回去（从 0 开始切）
                cut = [stream[i:i + p] for i in range(0, len(stream) - p + 1, p)]
                auto_split = {"period": p, "head": cands[0]["head"],
                              "head_len": hl, "frames": len(cut),
                              "candidates": cands[:4]}
                frames = cut

    notes = []
    if len(frames) < 2:
        notes.append("只有一帧：只能列字节，**不能**判字段语义（常量/变量/校验都要对比）")

    # 长度分组：变长帧不跨长度对齐
    lens = sorted({len(f) for f in frames})
    groups = {}
    for f in frames:
        groups.setdefault(len(f), []).append(f)
    report = {"ok": True, "frames": len(frames), "lengths": lens,
              "meta": meta, "auto_split": auto_split, "notes": notes}

    # 主分析用「样本数最多的那个长度组」
    main_len = max(groups, key=lambda k: (len(groups[k]), k))
    main = groups[main_len]
    if len(lens) > 1:
        notes.append("样本含 %d 种长度（%s），主分析只对 %d 字节那一组做；"
                     "跨长度对齐不猜" % (len(lens), lens[:6], main_len))
    if len(main) > MAX_FRAMES:
        notes.append("该组 %d 帧超过上限 %d，已等距采样（统计结论仍是全组的近似）"
                     % (len(main), MAX_FRAMES))
        step = max(1, len(main) // MAX_FRAMES)
        main = main[::step]

    # 全样本都一样 ⇒ 拆不出结构，别硬编字段（必须在采样前判，否则可能采到不同的两帧）
    all_same = len({bytes(f) for f in main}) == 1
    if all_same:
        notes.append("该组 %d 帧**完全相同**：没有变化就没有对比，"
                     "拆不出字段语义（只标了帧头）。要判字段请给变化的样本" % len(main))

    # 规模控制：帧数 × 帧长 决定校验暴力的成本，超预算就等距降帧（并在 notes 里说）
    cap = max(8, min(MAX_FRAMES, FRAME_BUDGET // max(1, main_len)))
    if len(main) > cap:
        notes.append("该组 %d 帧 × %d 字节超出分析预算，已等距采样到 %d 帧"
                     "（统计结论是全组的近似，不是逐帧全查）"
                     % (len(main), main_len, cap))
        step_i = max(1, len(main) // cap)
        main = main[::step_i][:cap]

    head, hlen = guess_head(main)
    cols = column_stats(main)
    fields = split_fields(cols)

    cs_hits = []
    if hunt_checksum and len(main) >= 2 and not all_same:
        cs_hits = checksum_hunt(main)
        fields = _carve_checksums(fields, cs_hits)

    entropy = _entropy(main[0]) if main else 0.0
    if entropy > 7.2:
        notes.append("首帧熵 %.2f bit/byte 偏高：这段可能是**压缩或加密**的载荷，"
                     "字段分析和校验识别对它都没意义" % entropy)

    report.update({
        "head": {"hex": head.hex() if head else "", "len": hlen},
        "analyzed_len": main_len,
        "analyzed_frames": len(main),
        "cols": cols,
        "fields": fields,
        "checksum_hits": cs_hits,
        "first_frame_entropy": round(entropy, 3),
        "caveats": _caveats(fields, cs_hits, hlen, all_same, main_len),
    })
    return report


def _entropy(data):
    if not data:
        return 0.0
    freq = {}
    for b in data:
        freq[b] = freq.get(b, 0) + 1
    n = float(len(data))
    return -sum((c / n) * math.log(c / n, 2) for c in freq.values())


def _caveats(fields, hits, hlen, all_same, total_len=0):
    """把「这个报告没说到什么」写清楚 —— 不写等于让人以为已拆完。"""
    out = []
    if not all_same:
        out.append("只做**字节对齐**分析：若字段是位域（1 字节里塞 3 个标志），"
                   "这里看到的会是「变化」，拆不出来")
        out.append("字段边界是「相邻同类列合并」推出来的，语义（哪个是命令码）"
                   "仍需人结合设备行为确认")
        if not hits:
            out.append("没有找到全帧吻合的校验算法：可能校验跨帧、带 key、"
                       "或在未采集的字节里；也可能本来就没有校验")
        else:
            same = {}
            for h in hits:
                same.setdefault((h["pos"], h["width"]), []).append(h["algo"])
            for (pos, w), algs in same.items():
                if len(algs) > 1:
                    out.append("位置 %d 有 %d 个算法同时吻合（%s）："
                               "在当前样本下它们等价，**不能**据此断定是哪一个"
                               % (pos, len(algs), "/".join(algs)))
        # 常量列的值恰好等于帧长 ⇒ 很可能是长度字段，只是样本同长分不出来
        for f in fields:
            if f["kind"] == "const" and f["length"] == 1 and total_len:
                try:
                    v = int(f["value_hex"], 16)
                except (ValueError, TypeError):
                    continue
                if v == total_len:
                    out.append("列 %d 的常量 0x%02x 恰好等于**帧总长**：很可能是长度字段，"
                               "但本次样本所有帧同长，与真常量无法区分"
                               "（给一组变长帧就能定论）" % (f["offset"], v))
                elif v == total_len - f["offset"]:
                    out.append("列 %d 的常量 0x%02x 恰好等于**从本列到帧尾的字节数**："
                               "很可能是剩余长度字段（同样需要变长样本才能确证）"
                               % (f["offset"], v))
    if hlen >= 2:
        out.append("前 %d 字节在所有帧里相同，按「帧头」处理；"
                   "注意公共前缀会顺带吃进长度/类型字段（帧长不变时很常见）" % hlen)
    return out

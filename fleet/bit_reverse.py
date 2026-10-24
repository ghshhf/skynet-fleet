# -*- coding: utf-8 -*-
"""位层协议拆解：字段不落在字节边界上时怎么办。

为什么单独一层
--------------
`proto_reverse` 的字节层分析假设「字段从字节边界开始、长度是整字节数」。
这条假设在软件协议里通常成立，但**硬件协议经常不成立** —— 传感器报文、
SPI/I2C 交互、无线遥控常常把 `4 位版本 + 6 位长度 + 2 位命令` 塞进同一个字节。
对这类数据，字节层看到的每一列都是「变化」，结论只剩「全是载荷」，等于没拆。

位层的方法与字节层同构，但**边界来源不同**：

  · 字节层靠「相邻列同类」合并字段；
  · 位层靠「常量位 vs 变化位」切分 —— 单个 bit 判不出 counter
    （要看整个位段的值），但常量位是天然的字段分隔符。
    所以先按常量/变化切段，再对每个变化段**整体**判语义。

因此位层的强项是「帧头/版本/标志位固定、载荷变化」这类结构；
相邻两个字段都变化时会合成一段，那种情况需要报文长度先验，本模块不猜（会说明）。

诚实边界（都写进输出的 notes / caveats）：
  · 位序 msb/lsb 按「哪种解释常量位更多」选，样本不足时可能选错；
  · 位层没比字节层多得到东西时会明说「用字节层就够」，不假装有发现；
  · 全相同样本的常量位占比 100% 不构成任何证据。

纯本地、纯只读。
"""
import math

from .proto_reverse import _norm_samples

_BIT_LABEL = {"const": "常量", "counter": "递增计数", "twin": "双值",
              "enum": "小值域", "var": "变化"}


def _to_bits(frame, order="msb"):
    """bytes → 0/1 列表。msb = 每字节高位在前（串行线/UART 常见），lsb 反之。"""
    out = []
    if order == "msb":
        for b in frame:
            out.extend((b >> s) & 1 for s in range(7, -1, -1))
    else:
        for b in frame:
            out.extend((b >> s) & 1 for s in range(0, 8))
    return out


def _bits_value(bits):
    """位列表 → 整数。列表第一项是**最高位**（顺序已由 _to_bits 决定）。"""
    v = 0
    for b in bits:
        v = (v << 1) | b
    return v


def _reg(seqs, s, e):
    """位段 [s,e) 的「可解释性」得分 0..1。

    这是递归切分的评分函数，取值依据是「这段能不能被一个简单模型说清」：
      常量          1.00  完全可预测
      等差（counter）0.90  逐帧步长恒定
      值域窄（enum） 0.60  候选命令码/标志
      其余           1 - uniq/min(帧数, 理论值域)，值域越满越像随机载荷
    纯随机数据在各宽度下都趋近 0 —— 这是防止「切出一堆假字段」的关键：
    没有这个性质，递归切分对随机数据也能切出「结构」。
    """
    width = e - s
    vals = [_bits_value(sq[s:e]) for sq in seqs]
    n = len(vals)
    uniq = len(set(vals))
    if uniq == 1:
        return 1.0
    deltas = {(vals[k + 1] - vals[k]) for k in range(n - 1)}
    if len(deltas) == 1:
        return 0.9
    if uniq <= max(2, n // 4) and uniq < (1 << width):
        return 0.6
    return max(0.0, 1.0 - uniq / float(min(n, 1 << width)))


def _find_cuts(seqs, s, e, depth=0, max_depth=4):
    """递归找位段内部的字段边界。

    为什么需要：位层的第一刀只按「常量位」切。如果两个相邻字段**都是变化的**
    （例如 4 位序号紧跟 2 位命令），它们会被并成一段 —— 此时位置全在,
    但结构没了。做法是在段内逐点试切，要求「切开后两边的平均可解释性
    明显高于整体」（门槛 0.15）。这是启发式，不是证明，所以结论里标了 caveat。
    """
    base = _reg(seqs, s, e)
    if base >= 0.85 or e - s <= 1 or depth >= max_depth:
        return [(s, e)]
    whole_uniq = len({_bits_value(sq[s:e]) for sq in seqs})
    best_cut, best_gain = None, 0.0
    for cut in range(s + 1, e):
        lv = {_bits_value(sq[s:cut]) for sq in seqs}
        rv = {_bits_value(sq[cut:e]) for sq in seqs}
        # 撤销「切在数值高位」的假边界：一边恒定、另一边值域**一点没缩小**
        # ⇒ 恒定那侧只是零/一填充，不是独立字段。
        # 实例：命令码 7/8/9 的二进制高 6 位恒 0，直接切会切出「6 位常量 0」
        # 这个假字段，还把原本字节对齐的协议判成非对齐（2026-10-25 实测）。
        # 真协议里的常量字段早在第一刀（按常量位切分）就切走了，不会走到这里，
        # 所以这条豁免不会吃掉真实的常量字段。
        if len(lv) == 1 and len(rv) == whole_uniq:
            continue
        if len(rv) == 1 and len(lv) == whole_uniq:
            continue
        gain = (_reg(seqs, s, cut) + _reg(seqs, cut, e)) / 2.0 - base
        if gain > best_gain:
            best_cut, best_gain = cut, gain
    if best_cut is None or best_gain < 0.15:
        return [(s, e)]
    return (_find_cuts(seqs, s, best_cut, depth + 1, max_depth) +
            _find_cuts(seqs, best_cut, e, depth + 1, max_depth))


def bit_fields(frames, order="msb", refine=True):
    """位层字段：先按「常量位/变化位」切段；refine=True 时再递归细分变化段。

    refine=False 用于「没找到结构」的场合：那时候递归切出来的碎片没有意义，
    只会让人以为拆出了东西。
    """
    if not frames:
        return []
    seqs = [_to_bits(f, order) for f in frames]
    ln = min(len(s) for s in seqs)
    if ln <= 0:
        return []
    flags = [len({s[i] for s in seqs}) == 1 for i in range(ln)]
    segs, start = [], 0
    for i in range(1, ln + 1):
        if i == ln or flags[i] != flags[start]:
            segs.append((start, i, flags[start]))
            start = i
    # 变化段内部再切（常量段本身就是字段，不用切）
    spans = []
    for s, e, is_const in segs:
        if is_const or not refine:
            spans.append((s, e))
        else:
            spans.extend(_find_cuts(seqs, s, e))
    out = []
    for s, e in spans:
        width = e - s
        vals = [_bits_value(seq[s:e]) for seq in seqs]
        rec = {"bit_offset": s, "bit_len": width,
               "byte_offset": s // 8 if s % 8 == 0 else None}
        if len(set(vals)) == 1:
            rec.update(kind="const", label=_BIT_LABEL["const"], value=vals[0],
                       bits="".join(str(x) for x in seqs[0][s:e]))
        else:
            uniq = sorted(set(vals))
            deltas = {(vals[k + 1] - vals[k]) for k in range(len(vals) - 1)}
            # 值域是否被填满：4 位字段只有 16 个可能值，样本一多必然填满 ——
            # 填满就不是「候选命令码」，是「看起来像枚举的载荷」。
            # 漏了这个判断，纯随机数据会被切成一堆 enum 字段（2026-10-25 实测过拟合）。
            span = 1 << width
            if len(uniq) == 2 and span > 2:
                # 1 位字段（span==2）出现两种值是**必然**，不构成「双值标志」的证据
                kind = "twin"
            elif len(deltas) == 1 and 1 in deltas:
                kind = "counter"
            elif len(uniq) <= max(2, len(vals) // 4) and len(uniq) < span:
                kind = "enum"
            else:
                kind = "var"
            rec.update(kind=kind, label=_BIT_LABEL[kind], uniq=len(uniq),
                       values=uniq[:8])
        out.append(rec)
    return out


def _bit_score(fields):
    """一种位序解释得好不好：常量位越多越好；同等常量下字段越少越整。"""
    const_bits = sum(f["bit_len"] for f in fields if f["kind"] == "const")
    return (const_bits, -len(fields))


def bit_report(samples, order=None):
    """位层拆解报告。order=None 时自动比较 msb/lsb 哪个解释得更好。"""
    frames, meta = _norm_samples(samples)
    if not frames:
        return {"ok": False, "err": "没有可用样本（解析后为空）"}
    ln = min(len(f) for f in frames)
    if ln == 0:
        return {"ok": False, "err": "样本里有空帧"}
    frames = [f[:ln] for f in frames]

    cands = {}
    for o in (("msb", "lsb") if order is None else (order,)):
        fs = bit_fields(frames, o)
        cands[o] = {"fields": fs, "score": _bit_score(fs)}
    if order is None:
        best = max(cands, key=lambda k: (cands[k]["score"], k == "msb"))
    else:
        best = order
    fields = cands[best]["fields"]

    const_bits = sum(f["bit_len"] for f in fields if f["kind"] == "const")
    total_bits = ln * 8
    all_same = len({bytes(f) for f in frames}) == 1

    # 「找到结构」必须靠**强证据**，不能靠「切出来 >= 2 段」：
    # 递归切分对随机数据也能切出碎片，把碎片当结构就是假绿。
    strong = [f for f in fields
              if f["kind"] in ("const", "counter", "twin")]
    strong_enum = [f for f in fields
                   if f["kind"] == "enum"
                   and f.get("uniq", 99) <= max(2, (1 << f["bit_len"]) // 2)]
    found = bool(strong or strong_enum) and not all_same

    if not found:
        # 没结构时退回「只按常量/变化分两段」，不展示递归碎片
        fields = bit_fields(frames, best, refine=False)
        const_bits = sum(f["bit_len"] for f in fields if f["kind"] == "const")

    byte_aligned = _byte_layer_ok(frames)
    boundaries_on_bytes = all(
        f["bit_offset"] % 8 == 0 and
        (f["bit_len"] % 8 == 0 or f["bit_offset"] + f["bit_len"] == total_bits)
        for f in fields)
    var_segs = [f for f in fields if f["kind"] == "var"]

    notes = []
    if not found:
        if all_same:
            notes.append("所有样本完全相同：常量位占比再高也不构成结构证据")
        else:
            notes.append("位层没拆出结构：整帧都是变化位。可能样本太少、数据高熵"
                         "（压缩/加密），也可能确实没有固定位域")
    if found and byte_aligned:
        notes.append("字节层已经能拆出结构（有常量字节）：这类数据用 re.proto 的字节层"
                     "分析就够，位层不会多告诉你什么")
    if found and not byte_aligned:
        notes.append("**字节层拆不出结构，位层能**：这是位打包（硬件协议）的特征，"
                     "字段跨字节边界，只做字节对齐分析必然看不出东西")
    if var_segs and const_bits:
        biggest = max(var_segs, key=lambda f: f["bit_len"])
        notes.append("有 %d 个变化位段（最大 %d 位 @ bit %d）：变化段内部若还藏着更细的"
                     "定长字段，仅靠参数统计分不出来，需要更多帧或已知报文长度"
                     % (len(var_segs), biggest["bit_len"], biggest["bit_offset"]))

    return {"ok": True, "frames": len(frames), "frame_bytes": ln,
            "total_bits": total_bits, "bit_order": best,
            "fields": fields, "const_bits": const_bits,
            "const_ratio": round(const_bits / float(total_bits), 3),
            "byte_aligned": byte_aligned,
            "boundaries_on_bytes": boundaries_on_bytes,
            "found": found,
            "try_orders": {k: v["score"] for k, v in cands.items()},
            "meta": meta, "notes": notes,
            "caveats": [
                "位层只按「常量位切段」找边界：相邻两个字段都变化时会被当成一段，"
                "那种情况需要报文长度先验才能再切，本模块不猜",
                "位序 msb/lsb 是按「哪种解释常量位更多」选的，样本不足时可能选错",
            ]}


def bit_extract(samples, bit_offset, bit_len, order="msb"):
    """按给定位段把每帧的值抽出来 —— 一旦推出命令字段，用它列全部取值。

    这是「从读懂到可复现」的那一步：知道某个位段是命令码、并且拿到了它的
    值域，才谈得上照着实现。
    """
    frames, meta = _norm_samples(samples)
    if not frames:
        return {"ok": False, "err": "没有可用样本"}
    out = []
    for f in frames:
        bits = _to_bits(f, order)
        seg = bits[bit_offset:bit_offset + bit_len]
        if len(seg) < bit_len:
            continue
        v = _bits_value(seg)
        out.append({"bits": "".join(str(x) for x in seg), "value": v,
                    "hex": "%0*x" % ((bit_len + 3) // 4, v)})
    if not out:
        return {"ok": False, "err": "位段 %d+%d 超出帧长（每帧 %d 位）"
                                     % (bit_offset, bit_len,
                                        min(len(f) for f in frames) * 8)}
    vals = sorted({o["value"] for o in out})
    return {"ok": True, "bit_offset": bit_offset, "bit_len": bit_len, "order": order,
            "frames": len(out), "distinct": len(vals), "value_set": vals[:32],
            "rows": out[:24], "note": "值域 %d 种；rows 只列前 24 帧" % len(vals)}


def _byte_layer_ok(frames):
    """字节层能不能自己拆出结构（判据：有没有常量字节）。

    为什么用这个而不是「边界是否都在字节上」：位层按常量位切分，
    **天然**会产生跨字节边界（`AA55` + 序号高 3 位恒 0 会连成一段 19 位常量），
    所以「边界在字节上」这个条件对位层几乎永远不成立，拿它判「字节对齐」
    只会把所有协议都说成位打包。

    真正要回答的问题是**「还要不要用位层」**。而字节层能拆出常量字节，
    就说明固定头/固定字段已经看见了 ⇒ 位层多余。
    """
    from .proto_reverse import column_stats, split_fields
    try:
        fields = split_fields(column_stats(frames))
    except Exception:
        return False
    return any(f["kind"] == "const" for f in fields)


def _shannon(bits):
    if not bits:
        return 0.0
    p = sum(bits) / float(len(bits))
    if p in (0.0, 1.0):
        return 0.0
    return -p * math.log(p, 2) - (1 - p) * math.log(1 - p, 2)

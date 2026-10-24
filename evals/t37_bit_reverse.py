#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T37 逆向深化第二层：位打包协议拆解（字段不落在字节边界上）。

为什么单列一层门禁：字节层分析的前提假设是「字段从字节边界开始」。
这个假设在软件协议里基本成立，在**硬件协议**里经常不成立 ——
传感器/SPI/I2C/无线遥控会把 4 位版本 + 6 位长度 + 2 位命令塞进同一个字节。
对这类数据，字节层每一列都是「变化」，结论只剩「全是载荷」，等于没拆。
所以位层不是「字节层的补充」，它是另一类数据的**唯一**可行路径。

用已知真值的假协议验，四条硬判据：

  ① 真值逐位对上
     bit0-3=版本(1100 固定) | bit4-7=序号 | bit8-9=命令 | bit10-15=值
     这四个字段全部**跨字节边界**，字节层必然拆不出，位层必须逐个还原。

  ② 递归切分必须真的有增益
     第一刀只按常量位切，[4:16] 会是一个大段。递归细分要能把
     「4 位序号 + 2 位命令 + 6 位值」再拆开；门禁同时验 refine=False
     与 refine=True 的差别，证明这一步不是装饰。

  ③ 随机数据零假阳性（最关键）
     递归切分有个天然风险：它对**任何**数据都能切出碎片，把碎片当结构就是假绿。
     所以三种规模（64×2、96×8、40×4）的纯随机数据都必须 found=False，
     并且要退回「一个整段」而不是一堆碎片。

  ④ 该说「不用拆」就要说
     全部字段都落在字节边界上时，必须明说「位层没比字节层多得到东西」——
     不然每个字节对齐的协议都会「拆出位域」，看着很厉害其实是废话。

全离线：不联网、不读外部数据、不碰真设备。
"""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fleet import bit_reverse as B     # noqa: E402
from fleet import proto_reverse as P   # noqa: E402

FAIL = []


def check(ok, msg):
    print(("  [ok] " if ok else "  [X ] ") + msg, flush=True)
    if not ok:
        FAIL.append(msg)


def field_at(fields, bit_offset):
    for f in fields:
        if f["bit_offset"] == bit_offset:
            return f
    return None


# ================================================================ 假协议

def packed(n=16):
    """真值：4 位版本 1100 | 4 位序号 | 2 位命令(3种) | 6 位值。

    16 位刚好 2 字节，四个字段**都跨字节边界**：
      版本占 byte0 高 4 位；序号占 byte0 低 4 位（跨界）；
      命令占 byte1 高 2 位；值占 byte1 低 6 位。
    所以字节层怎么看都是「byte0 递增、byte1 变化」。
    """
    out = []
    for i in range(n):
        x = (0b1100 << 12) | ((i % 16) << 8) | ((i % 3) << 6) | ((i * 37) & 0x3F)
        out.append(x.to_bytes(2, "big"))
    return out


def byte_aligned_proto(n=20):
    """对照组：字段全部落在字节边界的普通协议（AA55|seq|cmd|payload[3]）。"""
    out = []
    for i in range(n):
        out.append(bytes([0xAA, 0x55, i, [7, 8, 9][i % 3]]) +
                   bytes([(i * 5 + k) & 0xFF for k in range(3)]))
    return out


# ================================================================ ① 真值逐位对上

def test_truth():
    print("\n① 位打包真协议：四个跨字节字段必须逐个还原", flush=True)
    frames = packed()
    # 先证明字节层确实拆不出（否则这个门禁测的不是位层的价值）
    br = P.proto_report(frames)
    byte_kinds = [f["kind"] for f in br["fields"]]
    check("const" not in byte_kinds,
          "字节层确实拆不出常量/结构（实得 %r）—— 这样位层才有意义" % (byte_kinds,))

    r = B.bit_report(frames)
    check(r.get("ok"), "位层报告生成")
    check(r["found"] is True, "判定「找到了位域结构」")
    check(r["byte_aligned"] is False, "判定「存在非字节对齐位域」")
    check(r["bit_order"] == "msb", "位序选 msb（实得 %r）" % r["bit_order"])
    check(r["const_bits"] == 4, "常量位恰好 4 个（实得 %r）" % r["const_bits"])

    f0 = field_at(r["fields"], 0)
    check(f0 is not None and f0["kind"] == "const" and f0["bit_len"] == 4,
          "bit0 宽 4 判常量（实得 kind=%r len=%r）" % ((f0 or {}).get("kind"),
                                                    (f0 or {}).get("bit_len")))
    check(f0 is not None and f0.get("bits") == "1100",
          "常量位内容还原为 1100（实得 %r）" % (f0 or {}).get("bits"))

    f4 = field_at(r["fields"], 4)
    check(f4 is not None and f4["kind"] == "counter" and f4["bit_len"] == 4,
          "bit4 宽 4 判递增计数（实得 kind=%r len=%r）" % ((f4 or {}).get("kind"),
                                                        (f4 or {}).get("bit_len")))

    f8 = field_at(r["fields"], 8)
    check(f8 is not None and f8["kind"] == "enum" and f8["bit_len"] == 2,
          "bit8 宽 2 判小值域（实得 kind=%r len=%r）" % ((f8 or {}).get("kind"),
                                                  (f8 or {}).get("bit_len")))
    check(f8 is not None and f8.get("uniq") == 3,
          "命令位段恰好 3 种取值（实得 %r）" % (f8 or {}).get("uniq"))

    f10 = field_at(r["fields"], 10)
    check(f10 is not None and f10["kind"] == "var" and f10["bit_len"] == 6,
          "bit10 宽 6 判变化载荷（实得 kind=%r len=%r）" % ((f10 or {}).get("kind"),
                                                     (f10 or {}).get("bit_len")))

    total = sum(f["bit_len"] for f in r["fields"])
    check(total == 16, "四个位段拼起来正好覆盖 16 位（实得 %d）" % total)


# ================================================================ ② 递归切分有增益

def test_refine_gain():
    print("\n② 递归切分：refine=False 与 True 必须真的不同", flush=True)
    frames = packed()
    raw = B.bit_fields(frames, "msb", refine=False)
    fine = B.bit_fields(frames, "msb", refine=True)
    check(len(raw) == 2, "不细分时只有 2 段（常量 + 一大段变化，实得 %d）" % len(raw))
    check(len(fine) == 4, "细分后 4 段（实得 %d）" % len(fine))
    check(len(fine) > len(raw), "细分确实增加了字段数（不是装饰）")
    widths = [f["bit_len"] for f in fine]
    check(widths == [4, 4, 2, 6], "位宽序列为 4/4/2/6（实得 %r）" % (widths,))
    # 细分出来的中间两段必须带语义，不能只是切碎
    kinds = [f["kind"] for f in fine]
    check("counter" in kinds and "enum" in kinds,
          "细分出的段带语义（实得 %r）" % (kinds,))


# ================================================================ ③ 随机零假阳性

def test_no_false_positive():
    print("\n③ 三种规模的纯随机：都必须 found=False", flush=True)
    for seed, n, ln in ((3, 64, 2), (7, 96, 8), (11, 40, 4)):
        rnd = random.Random(seed)
        frames = [bytes(rnd.randrange(256) for _ in range(ln)) for _ in range(n)]
        r = B.bit_report(frames)
        check(r["found"] is False,
              "随机 %d帧×%d字节 ⇒ found=False（实得 %r，字段 %d 个）"
              % (n, ln, r["found"], len(r["fields"])))
        check(len(r["fields"]) == 1,
              "  退回「一整段」而不是一堆碎片（实得 %d 段）" % len(r["fields"]))
        if r["fields"]:
            check(r["fields"][0]["kind"] == "var",
                  "  该段判为变化（实得 %r）" % r["fields"][0]["kind"])


def test_all_same():
    print("\n③b 全相同样本：常量位再多也不算结构", flush=True)
    r = B.bit_report([bytes.fromhex("c0a5")] * 20)
    check(r["found"] is False, "全相同 ⇒ found=False（实得 %r）" % r["found"])
    check(any("完全相同" in n for n in r["notes"]),
          "notes 明说「完全相同不构成证据」")


# ================================================================ ④ 该说「不用拆」要说

def test_byte_aligned_hint():
    print("\n④ 字节对齐协议：必须明说「用字节层就够」", flush=True)
    r = B.bit_report(byte_aligned_proto())
    check(r["found"] is True, "字节对齐协议也应能看出结构（实得 %r）" % r["found"])
    check(r["byte_aligned"] is True, "判定为字节对齐（实得 %r）" % r["byte_aligned"])
    check(any("字节层" in n for n in r["notes"]),
          "notes 明说「位层没比字节层多得到东西」")


# ================================================================ ⑤ 位段提取

def test_extract():
    print("\n⑤ 位段取值：命令位段的值域要能列出来", flush=True)
    frames = [f.hex() for f in packed()]
    e = B.bit_extract(frames, 8, 2)
    check(e.get("ok"), "提取成功")
    check(e["distinct"] == 3, "bit8+2 有 3 种取值（实得 %r）" % e["distinct"])
    check(e["value_set"] == [0, 1, 2], "值域为 [0,1,2]（实得 %r）" % (e["value_set"],))
    check(all(len(r["bits"]) == 2 for r in e["rows"]), "每行都是 2 位")
    check([r["value"] for r in e["rows"][:4]] == [0, 1, 2, 0],
          "前四帧命令值 0,1,2,0（实得 %r）" % [r["value"] for r in e["rows"][:4]])

    # 越界要明确失败，不许返回空 list 当成功
    bad = B.bit_extract(frames, 200, 4)
    check(bad.get("ok") is False and "超出" in (bad.get("err") or ""),
          "位段越界 ⇒ 明确报错（实得 %r）" % (bad.get("err"),))

    # 版本位段：值域只有一种
    e2 = B.bit_extract(frames, 0, 4)
    check(e2["distinct"] == 1 and e2["value_set"] == [12],
          "bit0+4 只有一种值 12(0b1100)（实得 %r）" % (e2["value_set"],))


# ================================================================ ⑥ 位序自动选择

def test_order():
    print("\n⑥ 位序：lsb 打包的数据不该被当成 msb", flush=True)
    # 造一份 lsb 视角更整齐的数据：每字节低 4 位是常量 1111
    frames = []
    for i in range(16):
        frames.append(bytes([0b0000_1111, (i * 3) & 0xF0 | 0b0000_1111]))
    r_msb = B.bit_report(frames, order="msb")
    r_lsb = B.bit_report(frames, order="lsb")
    check(r_lsb["const_bits"] >= r_msb["const_bits"],
          "lsb 解释的常量位不少于 msb（lsb=%d, msb=%d）"
          % (r_lsb["const_bits"], r_msb["const_bits"]))
    auto = B.bit_report(frames)
    check(auto["bit_order"] in ("msb", "lsb"), "自动选择返回了明确位序")
    best_const = max(r_msb["const_bits"], r_lsb["const_bits"])
    check(auto["const_bits"] == best_const,
          "自动选择拿到两种里更好的那个（实得 %d，最好 %d）"
          % (auto["const_bits"], best_const))
    check(set(auto["try_orders"]) == {"msb", "lsb"},
          "报告里附了两种位序的评分（实得 %r）" % (auto["try_orders"],))


# ================================================================ ⑦ 能力登记

def test_registered():
    print("\n⑦ 能力必须可查（catalog + 工具表）", flush=True)
    from fleet import catalog
    names = [c.name for c in catalog.CAPABILITIES]
    for n in ("re.bits", "re.bits_extract"):
        check(n in names, "catalog 里有 %s" % n)
    reg = catalog.build(with_nodes=False)
    caps = reg.device("re:bench").caps()
    for n in ("re.bits", "re.bits_extract"):
        check(n in caps, "re:bench 声明了 %s" % n)
    import fleet.agent_tools as A
    ts = {t.name: t for t in A.TOOLS}
    for n in ("re_bits", "re_bits_extract"):
        check(n in ts, "工具表里下放了 %s" % n)
    if "re_bits" in ts:
        txt = ts["re_bits"].render({"ok": False, "err": "样本没解析出来"}, "x")
        check("失败" in txt, "渲染器对失败有话说（%r）" % txt[:30])
    if "re_bits_extract" in ts:
        txt = ts["re_bits_extract"].render(
            {"ok": True, "data": {"bit_offset": 8, "bit_len": 2, "order": "msb",
                                  "frames": 16, "distinct": 3,
                                  "value_set": [0, 1, 2],
                                  "rows": [{"hex": "0", "value": 0}]}}, "x")
        check("0, 1, 2" in txt, "渲染器列出值域（%r）" % txt[:60])


def main():
    print("=" * 62, flush=True)
    print("t37_bit_reverse: 位打包协议拆解（非字节对齐）", flush=True)
    print("=" * 62, flush=True)
    test_truth()
    test_refine_gain()
    test_no_false_positive()
    test_all_same()
    test_byte_aligned_hint()
    test_extract()
    test_order()
    test_registered()

    if FAIL:
        print("\nt37_bit_reverse: FAIL (%d 项)" % len(FAIL), flush=True)
        for m in FAIL:
            print("  - " + m, flush=True)
        return 1
    print("\nt37_bit_reverse: PASS（真值逐位对上 / 递归有增益 / 随机零假阳性）",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

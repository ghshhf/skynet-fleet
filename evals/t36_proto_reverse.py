#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T36 逆向深化第一层：未知二进制协议拆解（总纲 §八「逆向」第一条台阶）。

这一层钉的不是「认得多少个协议」，而是**没见过的时候能不能从数据本身推出结构**。
所以门禁用**已知真值的假协议**验 —— 拿真设备碰运气验不出「推得对不对」，
只能验「这次碰巧对了没」。

  ① 三条假协议逐字段对真值
     A: AA55 | 长度 | 序号 | 命令 | 载荷[5] | CRC16-CCITT(BE)
     B: 7E | 序号 | 随机载荷[4] | sum8(全部前面字节)
     C: 帧头 | xor8 | 载荷 —— 校验在**中间**（不是帧尾，考的是「位置要能搜出来」）

  ② 假阳性必须为 0（最重要的一条）
     96 帧纯随机数据，**一个校验算法都不许命中**。
     1 字节校验单帧蒙对率 1/256 ⇒「只验一帧」的实现会在这里大面积露出来；
     「验多数帧」的实现也会。判据只有「全帧吻合」才配叫命中。

  ③ 该说的不说等于骗人
     只有一帧 / 全部帧相同 ⇒ 必须**明确声明判不出字段语义**，
     不许硬编几个字段交差（假绿比失败更坏）。

  ④ 性能优化不许改结果
     校验搜索走的是增量状态机（快两个数量级），必须与**独立写的暴力版**
     逐位等价 —— 门禁里自带暴力参考实现，刻意不复用被测代码。

  ⑤ 探活不许自己抛
     models.json 是裸 list（某些工具直接落 list）时 re_llm.available() 不能崩。
     2026-10-25 真事故：这里一崩，re.* 四个能力全被判 unavailable，
     而根因只是文件形状，从报错里完全看不出来。

全离线：不联网、不读外部数据、不碰真设备。
"""
import json
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fleet import proto_reverse as P     # noqa: E402

FAIL = []


def check(ok, msg):
    print(("  [ok] " if ok else "  [X ] ") + msg, flush=True)
    if not ok:
        FAIL.append(msg)


def find_field(fields, offset):
    for f in fields:
        if f["offset"] == offset:
            return f
    return None


def kind_at(fields, offset):
    f = find_field(fields, offset)
    return f["kind"] if f else None


# ================================================================ ① 三条假协议

def proto_a(n=40):
    """AA55 | len=12 | seq | cmd | payload[5] | CRC16-CCITT(BE)。"""
    out = []
    for i in range(n):
        body = bytes([0xAA, 0x55, 12, i, [1, 2, 3][i % 3]]) + \
            bytes([(i * 7 + k) & 0xFF for k in range(5)])
        out.append(body + P.crc16_ccitt(body).to_bytes(2, "big"))
    return out


def proto_b(n=30, seed=11):
    """7E | seq | 随机载荷[4] | sum8(前面全部)。"""
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        body = bytes([0x7E, i]) + bytes(rnd.randrange(256) for _ in range(4))
        out.append(body + bytes([P.sum8(body)]))
    return out


def proto_c(n=24, seed=5):
    """帧头 | xor8(帧头起) | 载荷[3] —— 校验夹在中间。"""
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        head = bytes([0xD5, i & 0xFF])
        ck = P.xor8(head)
        body = head + bytes([ck]) + bytes(rnd.randrange(256) for _ in range(3))
        out.append(body)
    return out


def test_a():
    print("\n① A: AA55|len|seq|cmd|payload|CRC16-CCITT(BE)", flush=True)
    r = P.proto_report(proto_a())
    check(r.get("ok"), "报告生成")
    check(r["head"]["hex"].startswith("aa55"),
          "认出帧头 aa55（实得 %r）" % r["head"]["hex"])
    check(r["lengths"] == [12], "帧长识别为 12（实得 %r）" % (r["lengths"],))
    check(kind_at(r["fields"], 0) == "const", "偏移 0 判常量")
    check(kind_at(r["fields"], 3) == "counter", "偏移 3（序号）判递增计数（实得 %r）"
          % kind_at(r["fields"], 3))
    check(kind_at(r["fields"], 4) == "enum", "偏移 4（命令）判小值域（实得 %r）"
          % kind_at(r["fields"], 4))
    hits = [h for h in r["checksum_hits"] if h["pos"] == 10]
    check(bool(hits), "在偏移 10 找到校验（实得位置 %r）"
          % [h["pos"] for h in r["checksum_hits"]][:5])
    if hits:
        check(hits[0]["algo"] == "crc16/ccitt",
              "算法判为 crc16/ccitt（实得 %r）" % hits[0]["algo"])
        check(hits[0]["endian"] == "big", "字节序判为 big（实得 %r）" % hits[0]["endian"])
        check(hits[0]["width"] == 2, "宽度 2 字节")
        check(hits[0]["frames_ok"] == 40, "40 帧全部吻合，不是「多数吻合」")
    cf = find_field(r["fields"], 10)
    check(cf is not None and cf["kind"] == "checksum",
          "校验字节从「变化」里切出来成独立字段（实得 %r）"
          % (cf or {}).get("kind"))
    # 长度字段的歧义必须说出来（本样本帧长不变，数据上分不出常量和长度）
    check(any("长度字段" in c for c in r["caveats"]),
          "caveats 明确提示「偏移 2 可能是长度字段但无法确证」")


def test_b():
    print("\n① B: 7E|seq|载荷[4]|sum8（帧尾）", flush=True)
    r = P.proto_report(proto_b())
    check(kind_at(r["fields"], 0) == "const", "偏移 0（7E）判常量")
    check(kind_at(r["fields"], 1) == "counter", "偏移 1 判递增计数（实得 %r）"
          % kind_at(r["fields"], 1))
    pos6 = [h for h in r["checksum_hits"] if h["pos"] == 6]
    check(bool(pos6), "在偏移 6（帧尾第 7 字节）找到校验（实得 %r）"
          % [h["pos"] for h in r["checksum_hits"]][:5])
    if pos6:
        check(pos6[0]["algo"] == "sum8", "算法判为 sum8（实得 %r）" % pos6[0]["algo"])
    dup = [h for h in r["checksum_hits"] if h["pos"] == 6]
    check(len(dup) == 1, "1 字节校验不重复报 big/little（实得 %d 条）" % len(dup))


def test_c():
    print("\n① C: 帧头|xor8|载荷（校验在中间）", flush=True)
    r = P.proto_report(proto_c())
    pos2 = [h for h in r["checksum_hits"] if h["pos"] == 2]
    check(bool(pos2), "校验在**中间**也能搜出来（偏移 2，实得 %r）"
          % [h["pos"] for h in r["checksum_hits"]][:6])
    if pos2:
        check(pos2[0]["algo"] == "xor8", "算法判为 xor8（实得 %r）" % pos2[0]["algo"])


# ================================================================ ② 假阳性

def test_no_false_positive():
    print("\n② 96 帧纯随机：一个校验都不许命中", flush=True)
    rnd = random.Random(20261025)
    frames = [bytes(rnd.randrange(256) for _ in range(24)) for _ in range(96)]
    r = P.proto_report(frames)
    check(len(r["checksum_hits"]) == 0,
          "随机数据 0 命中（实得 %d 条：%r）"
          % (len(r["checksum_hits"]),
             [(h["pos"], h["algo"]) for h in r["checksum_hits"][:3]]))
    check(any("没有找到" in c for c in r["caveats"]),
          "没找到校验时如实说「没有找到」，不硬凑")
    # 帧头也不许硬编：随机数据不该有 2 字节以上公共前缀
    check(r["head"]["len"] <= 1, "随机数据不该编出多字节帧头（实得 %d）" % r["head"]["len"])


# ================================================================ ③ 该说的不说等于骗人

def test_honesty():
    print("\n③ 单帧 / 全相同：必须声明判不出", flush=True)
    one = P.proto_report([bytes.fromhex("aa550c0105aabbccddee1234")])
    check(one.get("ok"), "单帧仍能出报告（列字节）")
    check(any("只有一帧" in n for n in one["notes"]),
          "单帧明确声明「不能判字段语义」")
    check(len(one["checksum_hits"]) == 0, "单帧不报校验（1/256 蒙对率不能算命中）")

    same = P.proto_report([bytes.fromhex("aa550c0105aabbccddee1234")] * 40)
    check(any("完全相同" in n for n in same["notes"]),
          "全相同样本明确声明「拆不出字段语义」")
    check(len(same["checksum_hits"]) == 0, "全相同样本不硬报校验")


# ================================================================ ④ 增量与暴力等价

def _bruteforce_prefix(fn, frame):
    """独立实现：暴力算每个前缀的校验值。"""
    return [fn(frame[:p]) for p in range(len(frame) + 1)]


def test_incremental_equiv():
    print("\n④ 增量状态机 vs 暴力版：必须逐位等价", flush=True)
    frames = proto_a(6) + proto_b(4) + proto_c(3)
    bad = []
    for name, (fn, width) in P.ALGOS.items():
        init, step, final = P._INCR[name]
        for f in frames:
            force = _bruteforce_prefix(fn, f)
            s = init
            got = [final(s)]
            for b in f:
                s = step(s, b)
                got.append(final(s))
            if got != force:
                bad.append((name, f.hex()[:16],
                            [i for i in range(len(got)) if got[i] != force[i]][:3]))
    check(not bad, "10 个算法全部等价（不等价的：%r）" % (bad[:3],))


# ================================================================ ⑤ 探活不许自己抛

def test_probe_never_raises():
    print("\n⑤ re_llm 探活：models.json 是裸 list 时不许崩", flush=True)
    from fleet import re_llm
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                      encoding="utf-8")
    tmp.write(json.dumps([
        {"name": "★裸list端点", "url": "http://127.0.0.1:1/v1", "id": "m1"},
        {"name": "已下线端点", "url": "http://127.0.0.1:2/v1", "id": "m2"},
    ]))
    tmp.close()
    old = re_llm.MODELS_JSON
    re_llm.MODELS_JSON = tmp.name
    try:
        ok, why = re_llm.available()
        check(ok is True, "裸 list 也能读出端点（ok=%r, why=%r）" % (ok, why))
        check("裸list端点" in why, "读到的确实是 list 里的那条（%r）" % (why,))
        ents = re_llm._live_entries()
        check(len(ents) == 1, "「已下线」那条被跳过（实得 %d 条）" % len(ents))
    except Exception as e:
        check(False, "探活抛异常了：%s: %s" % (type(e).__name__, e))
    finally:
        re_llm.MODELS_JSON = old
        os.unlink(tmp.name)

    # 垃圾内容也不许抛
    tmp2 = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                       encoding="utf-8")
    tmp2.write('{"weird": 1}')
    tmp2.close()
    re_llm.MODELS_JSON = tmp2.name
    try:
        ok, why = re_llm.available()
        check(ok is False and why, "无端点时返回 (False, 原因) 而不是抛")
    except Exception as e:
        check(False, "非法形状抛异常了：%s" % e)
    finally:
        re_llm.MODELS_JSON = old
        os.unlink(tmp2.name)


# ================================================================ ⑥ 单流切帧

def test_auto_split():
    print("\n⑥ 单段长流：自动切帧", flush=True)
    frames = []
    for i in range(12):
        b = bytes([0xAA, 0x55, 9, i, 0x01]) + bytes([(i * 3 + k) & 0xFF for k in range(2)])
        frames.append(b + P.crc16_ccitt(b).to_bytes(2, "big"))
    stream = b"".join(frames)                       # 一整段，不告诉它帧长
    r = P.proto_report(stream)
    sp = r.get("auto_split")
    check(bool(sp), "触发了自动切帧")
    if sp:
        check(sp["period"] == 9, "切出的帧长 9（实得 %r）" % sp["period"])
        check(sp["frames"] == 12, "切出 12 帧（实得 %r）" % sp["frames"])
        hits = [h for h in r["checksum_hits"] if h["pos"] == 7]
        check(bool(hits), "切帧后仍能在偏移 7 找到校验（实得 %r）"
              % [h["pos"] for h in r["checksum_hits"]][:5])


# ================================================================ ⑦ 变长帧分组

def test_var_len():
    print("\n⑦ 变长帧：按长度分组，不跨长度硬对齐", flush=True)
    frames = proto_a(10) + [f[:8] for f in proto_a(10)]
    r = P.proto_report(frames)
    check(len(r["lengths"]) == 2, "识别出两种长度（实得 %r）" % (r["lengths"],))
    check(any("种长度" in n for n in r["notes"]),
          "明确声明「跨长度对齐不猜」")
    check(r["analyzed_len"] == 12, "主分析取样本多的那一组（12 字节）")


# ================================================================ ⑧ 能力登记

def test_registered():
    print("\n⑧ 能力必须可查（catalog + 工具表）", flush=True)
    from fleet import catalog
    names = [c.name for c in catalog.CAPABILITIES]
    check("re.proto" in names, "catalog 里有 re.proto")
    reg = catalog.build(with_nodes=False)
    d = reg.device("re:bench")
    check("re.proto" in d.caps(), "re:bench 声明了 re.proto")
    import fleet.agent_tools as A
    ts = {t.name: t for t in A.TOOLS}
    check("re_proto" in ts, "工具表里下放了 re_proto")
    if "re_proto" in ts:
        txt = ts["re_proto"].render({"ok": False, "err": "样本没解析出来"}, "x")
        check("失败" in txt, "渲染器对失败有话说（%r）" % txt[:40])


def main():
    print("=" * 62, flush=True)
    print("t36_proto_reverse: 未知协议拆解（字节对齐 / 字段 / 校验）", flush=True)
    print("=" * 62, flush=True)
    test_a()
    test_b()
    test_c()
    test_no_false_positive()
    test_honesty()
    test_incremental_equiv()
    test_probe_never_raises()
    test_auto_split()
    test_var_len()
    test_registered()

    if FAIL:
        print("\nt36_proto_reverse: FAIL (%d 项)" % len(FAIL), flush=True)
        for m in FAIL:
            print("  - " + m, flush=True)
        return 1
    print("\nt36_proto_reverse: PASS（真值逐字段对上 / 随机零假阳性 / 增量≡暴力）",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

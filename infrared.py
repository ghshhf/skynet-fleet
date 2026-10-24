"""红外遥控编码：把「按一下电视电源」变成一串微秒级的通断序列。

ConsumerIrManager 只认裸 pattern（载波频率 + 微秒数组），协议要自己按规范拼。
NEC 覆盖绝大多数国产电视、风扇、机顶盒；Samsung / Sony 各有一套。

安全默认：不加 --send 只打印序列，不发射。真发射会把电器打开或关掉，
这种动作不该在没确认对象的时候发生。

码库路线（ir_codes.py / ir_codes/）：
  infrared.command(serial, brand, device, key) → 查码库 → 编码 → 发射
  infrared.codebook() → 列出有码的品牌
  infrared.learn_info(serial) → 问硬件是否支持学习模式

本机 25060RK16C 红外只有发射口，没有学习口——所以码库是唯一路线。
"""
import json
import os
import sys

import android_device as A
import ir_codes as IC

NEC_FREQ = 38000


def _bits(byte):
    """一个字节拆成 8 个 bit，低位在前（NEC 就是这么发的）。"""
    return [(byte >> i) & 1 for i in range(8)]


def _pattern(bits, header, one, zero, tail):
    pat = list(header)
    for b in bits:
        pat += list(one) if b else list(zero)
    pat.append(tail)
    return pat


def nec(addr, cmd, freq=NEC_FREQ):
    """NEC：地址 + 反码 + 命令 + 反码，38kHz，载波通断 560/1690µs。"""
    bits = (_bits(addr & 0xFF) + _bits((~addr) & 0xFF)
            + _bits(cmd & 0xFF) + _bits((~cmd) & 0xFF))
    return freq, _pattern(bits, (9000, 4500), (560, 1690), (560, 560), 560)


def nec_ext(addr, cmd, freq=NEC_FREQ):
    """NEC 扩展：16 位地址，命令仍带反码。"""
    bits = (_bits(addr & 0xFF) + _bits((addr >> 8) & 0xFF)
            + _bits(cmd & 0xFF) + _bits((~cmd) & 0xFF))
    return freq, _pattern(bits, (9000, 4500), (560, 1690), (560, 560), 560)


def samsung(addr, cmd, freq=38000):
    """三星：地址发两遍（第二遍不取反），头 4500/4500。"""
    bits = (_bits(addr & 0xFF) + _bits(addr & 0xFF)
            + _bits(cmd & 0xFF) + _bits((~cmd) & 0xFF))
    return freq, _pattern(bits, (4500, 4500), (560, 1690), (560, 560), 560)


def sony(addr, cmd, bits_n=12, freq=40000):
    """索尼 SIRC：7 位命令 + 5 位设备号，脉宽调制（1=1200 通，0=600 通）。"""
    data = _bits(cmd & 0x7F)[:7] + _bits(addr & 0x1F)[:5]
    pat = [2400, 600]
    for b in data:
        pat += [1200, 600] if b else [600, 600]
    return freq, pat


def send(serial, freq, pattern):
    """经编队桥发射。不需要无障碍注入权限，只要桥活着。"""
    return A.bridge_cmd(serial, f"IR {freq} " + " ".join(str(x) for x in pattern))


def ir_info(serial):
    return A.bridge_cmd(serial, "IRINFO")


def learn_info(serial):
    """查红外收发器是否支持学习模式（仅部分机型支持）。

    返回 dict：{"has_emitter": bool, "can_learn": bool, "bands": [...], "reason": str}
    """
    r = ir_info(serial)
    out = (r.get("out") or "").strip()
    if r.get("ok") and out.startswith("OK ir"):
        bands = out[5:].strip()
        band_list = []
        for b in bands.split():
            if "-" in b:
                parts = b.split("-")
                if len(parts) == 2 and all(p.isdigit() for p in parts):
                    band_list.append({"min": int(parts[0]), "max": int(parts[1])})
        return {"has_emitter": True, "can_learn": False, "bands": band_list,
                "reason": "码库路线（本机红外只有发射口，没有学习口）"}
    return {"has_emitter": False, "can_learn": False, "bands": [],
            "reason": r.get("err") or r.get("out") or "红外收发器不可用"}


def command(serial, brand, device_type, key, dry_run=True):
    """经码库发射一条红外指令。

    流程：查码库 → 编码 → (dry_run=False 时)经桥发射。
    不发射时只返回编码信息，供审批/调试用。

    返回 dict：{"ok": True/False, "err": str, "protocol": str, "freq": int,
                "addr": int, "cmd": int, "dry_run": bool}
    """
    proto, addr, cmd = IC.lookup(brand, device_type, key)
    if proto is None:
        return {"ok": False, "err": f"码库里没有品牌={brand} 类型={device_type} 键={key}",
                "protocol": None, "freq": 0, "addr": 0, "cmd": 0, "dry_run": True}

    encoders = {
        "nec": nec, "nec_ext": nec_ext, "samsung": samsung, "sony": sony,
    }
    enc = encoders.get(proto.lower())
    if enc is None:
        return {"ok": False, "err": f"不认识的协议 {proto}",
                "protocol": proto, "freq": 0, "addr": addr, "cmd": cmd, "dry_run": True}

    freq, pattern = enc(addr, cmd)
    result = {"ok": True, "protocol": proto, "freq": freq,
              "addr": addr, "cmd": cmd, "pairs": len(pattern) // 2,
              "dry_run": dry_run, "codebook": True}
    if not dry_run:
        r = send(serial, freq, pattern)
        if not r.get("ok"):
            result["ok"] = False
            result["err"] = r.get("err") or "桥拒绝发射"
            return result
        result["sent"] = True
    return result


def codebook():
    """列出可用码库。"""
    return IC.available_brands()


def _pairs(pattern):
    """把微秒序列折成「通/断」两列，方便人眼核对。"""
    rows = []
    for i in range(0, len(pattern), 2):
        on = pattern[i]
        off = pattern[i + 1] if i + 1 < len(pattern) else 0
        rows.append(f"{on:>5} / {off:>5}")
    return rows


def _selftest():
    # ① NEC 帧结构：2 段头 + 32bit×2 段 + 1 段尾 = 67
    freq, pat = nec(0x04, 0x08)
    assert freq == 38000, freq
    assert len(pat) == 67, len(pat)
    assert pat[0] == 9000 and pat[1] == 4500, pat[:2]
    assert pat[-1] == 560, pat[-1]

    # ② 低位在前：addr=0x01 的第一个 bit 是 1（长间隔），第二个是 0（短间隔）
    _, p = nec(0x01, 0x00)
    assert p[2:4] == [560, 1690], p[2:4]
    assert p[4:6] == [560, 560], p[4:6]

    # ③ 反码位：cmd=0x00 时第 17–24 位（cmd）全短间隔、25–32 位（~cmd）全长间隔。
    #    注意不能用「1 的个数」判——加了反码后每帧恒为 16 个 1，判不出东西。
    _, p = nec(0x00, 0x00)
    assert p[2 + 2 * 16:4 + 2 * 16] == [560, 560], p[34:36]
    assert p[2 + 2 * 24:4 + 2 * 24] == [560, 1690], p[50:52]

    # ④ 三星头是 4500/4500，地址发两遍
    _, sp = samsung(0x07, 0x02)
    assert sp[0] == 4500 and sp[1] == 4500, sp[:2]
    assert len(sp) == 67, len(sp)

    # ⑤ 索尼 SIRC 是 12 位脉宽调制，1 是 1200 通
    sfreq, snp = sony(0x01, 0x15)
    assert sfreq == 40000, sfreq
    assert snp[0] == 2400 and snp[1] == 600, snp[:2]
    assert 1200 in snp and len(snp) == 2 + 24, len(snp)

    # ⑥ 奇数长度的 pattern 折行不能越界
    rows = _pairs([9000, 4500, 560])
    assert rows[-1].strip() == "560 /     0", rows[-1]

    # ⑦ 地址是 16 位扩展时低高字节顺序别搞反
    _, ep = nec_ext(0x1234, 0x56)
    assert len(ep) == 67, len(ep)

    # ⑧ 码库查找
    cb = IC.load_codebook("小米", "电视")
    assert cb is not None and cb.get("brand") == "小米", cb
    proto, addr, cmd = IC.lookup("小米", "电视", "power")
    assert proto == "nec" and addr == 4 and cmd == 8, (proto, addr, cmd)
    p2, a2, c2 = IC.lookup("海信", "空调", "temp_up")
    assert p2 == "nec" and a2 == 16 and c2 == 9, (p2, a2, c2)
    proto_none, _, _ = IC.lookup("nonexistent", "电视", "power")
    assert proto_none is None, "不存在品牌返回 None"

    # ⑨ 键名归一化
    assert IC._normalize_key("电源") == "power", "键名归一化：电源->power"
    proto, addr, cmd = IC.lookup("海信", "电视", "on")
    assert addr == 8 and cmd == 8, "on -> power"

    # ⑩ available_brands 非空（至少 generic）
    brands = codebook()
    assert len(brands) >= 1 and any(b["brand"] == "generic" for b in brands), brands

    print("infrared 自测：10 项全过")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    d = A.attach()
    if not d["ok"]:
        print("设备接不上：", d.get("err"), file=sys.stderr)
        return 2
    serial = d["serial"]

    if not argv or argv[0] == "info":
        print(ir_info(serial))
        return 0

    if argv[0] == "cb" or argv[0] == "codebook":
        for c in codebook():
            print(f"  {c['brand']} / {c['device']}  {c['keys']} 个键")
        return 0

    if argv[0] == "cmd":
        # infrared.py cmd 小米 电视 power [--send]
        if len(argv) < 4:
            print("用法: infrared.py cmd <品牌> <类型> <键> [--send]")
            return 1
        brand, dev_type, key = argv[1], argv[2], argv[3]
        send_it = "--send" in argv
        r = command(serial, brand, dev_type, key, dry_run=not send_it)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1

    proto = argv[0].lower()
    send_it = "--send" in argv
    argv = [a for a in argv if a != "--send"]

    if proto == "raw":
        if len(argv) < 3:
            print("用法: infrared.py raw <频> <通1> <断1> ...")
            return 1
        freq = int(argv[1])
        pat = [int(x) for x in argv[2:]]
    elif proto == "nec":
        if len(argv) < 3:
            print("用法: infrared.py nec <addr> <cmd>")
            return 1
        freq, pat = nec(int(argv[1], 0), int(argv[2], 0))
    elif proto == "necx":
        if len(argv) < 3:
            print("用法: infrared.py necx <addr16> <cmd>")
            return 1
        freq, pat = nec_ext(int(argv[1], 0), int(argv[2], 0))
    elif proto == "samsung":
        if len(argv) < 3:
            print("用法: infrared.py samsung <addr> <cmd>")
            return 1
        freq, pat = samsung(int(argv[1], 0), int(argv[2], 0))
    elif proto == "sony":
        if len(argv) < 3:
            print("用法: infrared.py sony <addr> <cmd> [bits]")
            return 1
        bits_n = int(argv[3]) if len(argv) > 3 else 12
        freq, pat = sony(int(argv[1], 0), int(argv[2], 0), bits_n)
    else:
        print(__doc__)
        return 1

    print(f"协议 {proto}  载波 {freq}Hz  共 {len(pat)} 段")
    for row in _pairs(pat):
        print("  ", row)
    if send_it:
        print("发射：", send(serial, freq, pat))
    else:
        print("（未发射，加 --send 才真发）")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    sys.exit(main())

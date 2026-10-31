#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T52 可达层：连得上就在网里，但**连得上不等于能下令**。

这次的升级把天网从「先入网才有网」改成「只要能连就已在网里」。收益很大，
风险也全在同一处：一旦「可达」被当成「可信」，局域网里任何一台能连上我们的
机器（同事的笔记本、访客的手机、打印机、摄像头）就成了可指挥对象。

所以门禁钉的不是「能不能发现」，是**那道墙还在不在**：

  ① 结构边界：可达层的设备**不注册成 BaseDevice**，于是注册表里根本没有它们，
     `reg.call('shell.exec', device=<对端>)` 无路可走 —— 不是靠自觉不调，
     是结构上够不着。这条如果破了，其它都不用看。
  ② 通道状态机：一台设备是多条通道，断一条**设备不消失**（锁屏 30 秒 ≠ 掉网），
     全断才从默认列表里隐去（账本仍保留），恢复就回来。
  ③ 身份是 fingerprint 不是 IP：同设备换通道仍是同一台，长不出鬼影。
  ④ 凭据绝不落盘（PIN / token 一个都不许进账本）。
  ⑤ 空账本不是故障：没设备时 reach.list 必须 ok，否则界面会显示红叉 ——
     正确答案是「现在网里没别人」。

全离线：本机回环，账本与设备表都指到临时文件，不碰真账本。
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="t52_")
os.environ["FLEET_REACH"] = os.path.join(TMP, "reach.json")
os.environ["FLEET_LAN_PORT"] = "53921"
from fleet import reach                                     # noqa: E402
from fleet.kinds.lan_transfer import LanTransfer            # noqa: E402
from fleet.kinds.reach_layer import ReachLayer              # noqa: E402

FAIL = []


def check(ok, msg):
    print(("  [ok] " if ok else "  [X ] ") + msg, flush=True)
    if not ok:
        FAIL.append(msg)


def main():
    print("T52 可达层（连得上就在网里，但没执行权）", flush=True)
    reach.reset()
    rl = ReachLayer()

    # ---- 1. 空账本不是坏 ----
    ok, why = rl.probe()
    check(ok, "空账本时这一层仍报可用（空 ≠ 坏）")
    r = rl.invoke("reach.list", {})
    check(r.ok and r.data.get("count") == 0, "空账本能正常列出 0 台")

    # ---- 2. 设备身份 = fingerprint，不是 IP ----
    PHONE = "a" * 64
    reach.note(PHONE, alias="手机", channel=reach.channel_key("lan", "192.168.1.9", 53317),
               meta={"protocol": "http", "model": "Phone"})
    d = reach.get(PHONE)
    check(d is not None and d["alias"] == "手机", "按 fingerprint 建了设备")
    check(len(reach.devices()) == 1, "账本里 1 台")

    # 同一台设备换一条通道（蓝牙），不许变成第二台
    reach.note(PHONE, channel=reach.channel_key("ble", "AA:BB:CC:DD:EE:FF"))
    check(len(reach.devices()) == 1, "同设备多通道仍是同一台（长不出鬼影）")
    check(len(reach.get(PHONE)["channels"]) == 2, "两条通道都记着")

    # ---- 3. 通道状态机 ----
    reach.mark_failed(PHONE, reach.channel_key("lan", "192.168.1.9", 53317), why="超时")
    dev = reach.get(PHONE)
    check(len(reach.devices()) == 1, "断一条通道，设备**不消失**")
    check(reach.best_channel(dev).startswith("ble:"), "可用通道排到前面")
    check(reach.get(PHONE)["channels"][reach.channel_key("lan", "192.168.1.9", 53317)]["fails"] == 1,
          "失败次数记下来了（不是只有一句 why）")

    reach.mark_failed(PHONE, reach.channel_key("ble", "AA:BB:CC:DD:EE:FF"), why="不在范围")
    check(reach.devices() == [], "全断才从默认列表里隐去")
    check(reach.get(PHONE) is not None, "但账本里仍在（不因一次失败就遗忘）")
    check(len(reach.devices(only_available=False)) == 1, "all 模式还能看见它")

    reach.note(PHONE, channel=reach.channel_key("lan", "192.168.1.9", 53317))
    check(len(reach.devices()) == 1, "通道恢复，它自己回来了")
    check(reach.get(PHONE)["channels"][reach.channel_key("lan", "192.168.1.9", 53317)]["fails"] == 0,
          "恢复后失败计数清零（不留旧账误导判断）")

    # ---- 4. 凭据绝不落盘 ----
    reach.note("b" * 64, alias="可疑", channel="lan:10.0.0.2:53317",
               meta={"pin": "9527", "token": "s3cret", "password": "pw"})
    raw = open(reach.PATH, encoding="utf-8").read()
    for bad in ("9527", "s3cret", '"pin"', '"token"', '"password"'):
        check(bad not in raw, "账本里没有 %s" % bad)

    # ---- 5. 🔴 结构边界：可达层不产生可执行设备 ----
    from fleet.catalog import CAPABILITIES
    from fleet.core import Registry
    reg = Registry(path=os.path.join(TMP, "devices.json"), gate=None, ttl=0.05)
    reg.register_all(CAPABILITIES)
    reg.add(rl)
    reg.add(LanTransfer())
    names = [getattr(x, "name", "") for x in reg.devices()]
    check("手机" not in names and PHONE not in names,
          "可达设备**没有**出现在注册表设备列表里（结构上够不着）")
    check(not any(n.startswith("reach:") and n != "reach:layer" for n in names),
          "外圈没有被偷偷装成设备")

    # 用一个可达设备的名字去调执行类能力 —— 必须无处可去
    out = reg.call("shell.exec", device="手机", args={"cmd": "echo hi"})
    check(not out.ok, "拿可达设备名当设备调 shell.exec ⇒ 拒绝（%s）" % out.code)

    # 可达层自己申报的能力里，不许出现任何执行类
    declared = set(rl.caps())
    bad_caps = {c for c in declared if c.split(".")[0] in
                ("shell", "exec", "file", "pc", "pcctl", "input", "app", "job")}
    check(not bad_caps, "可达层申报的能力里没有执行类（越界：%s）" % (bad_caps or "无"))

    # ---- 6. 名字寻址：人记名字，工具记指纹 ----
    lt = LanTransfer()
    tgt, how, deny = __import__("fleet.kinds.lan_transfer", fromlist=["x"])._resolve_target("手机")
    check(tgt is not None and tgt["host"] == "192.168.1.9",
          "按名字解析到正确地址（how=%s）" % how)
    check(how.startswith("reach/"), "并且说明是经可达层解析的，不是瞎猜 IP")
    tgt2, _, deny2 = __import__("fleet.kinds.lan_transfer", fromlist=["x"])._resolve_target("没这台")
    check(tgt2 is None and "可达层里没有" in deny2, "找不到时明说，并提示先扫一遍")

    # ---- 7. forget 是真删 ----
    r = rl.invoke("reach.forget", {"id": "手机"})
    check(r.ok and r.data.get("removed"), "按名字删掉了")
    check(reach.get(PHONE) is None, "账本里确实没了")
    r2 = rl.invoke("reach.forget", {"id": "手机"})
    check(r2.ok and not r2.data.get("removed"), "再删一次如实说「本来就没有」，不假装成功")

    # ---- 8. 设备不许被「声明」，只能被观察 ----
    check("reach.add" not in {c.name for c in CAPABILITIES},
          "没有 reach.add（账本不能凭空长设备）")

    print()
    if FAIL:
        print("T52 红：%d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("T52 全绿")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

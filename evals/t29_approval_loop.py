#!/usr/bin/env python
"""门禁 t29：审批闭环端到端复测（批次 A2）—— 钉死「点了要生效、不点要拒绝」。

修复前的真 bug：审批超时 120s，无窗口 --link 模式下主人不在电脑前，
「帮我打开微信」干等两分钟才来一句超时——界面全静默像卡死。压到 30s 后，
闭环本身（挂起 → 送达 → 生效）是否还灵、超时是否真拒绝、重放是否拦住、
破坏性是否双确认，都得有量化验收，否则哪天回归了又是玄学。

全离线：直接驱动 ApprovalBroker，不拉 HTTP / 不碰真机 UI。
"""
import os
import sys
import time
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "fleet", "gui"))

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok", name)
    else:
        FAIL.append(f"{name}: {detail}")
        print("  FAIL", name, "-", detail)


def main():
    from approval import ApprovalBroker

    class Cap:
        def __init__(self, name, title="", destructive=False):
            self.name = name
            self.title = title
            self.destructive = destructive

    class Dev:
        def __init__(self, name=""):
            self.name = name

    print("[1] 闭环生效：gate 挂起 → 0.3s 后 respond 同意 → (True,'') 且 <2s")
    b = ApprovalBroker(timeout=4.0, session="s1")
    gate = b.gate_for(tag="t1", session="s1")
    res = {"v": None}
    th = threading.Thread(target=lambda: res.__setitem__("v",
                        gate(Cap("open_app", "打开应用"), Dev("手机"), {"pkg": "x"}, 1)))
    t0 = time.time()
    th.start()
    time.sleep(0.3)
    rid = b.pending()[0]["request_id"]
    ok, why = b.respond(rid, True)
    th.join(timeout=3)
    check("respond 报送达", ok is True, why)
    check("gate 返回 (True, '')", res["v"] == (True, ""), repr(res["v"]))
    check("生效延迟 < 2s（远小于超时）", (time.time() - t0) < 2.0, "%.2fs" % (time.time() - t0))

    print("[2] 超时拒绝（fail-closed）：不点 → 约 4s 后 (False, 含'超时')")
    b2 = ApprovalBroker(timeout=4.0, session="s2")
    gate2 = b2.gate_for(tag="t2", session="s2")
    res2 = {"v": None}
    th2 = threading.Thread(target=lambda: res2.__setitem__("v",
                         gate2(Cap("x"), Dev("手机"), {}, 1)))
    t2 = time.time()
    th2.start()
    th2.join(timeout=6)
    el = time.time() - t2
    check("超时返回 (False, 提及超时)", res2["v"][0] is False and "超时" in res2["v"][1],
          repr(res2["v"]))
    check("耗时落在 (3s,6s]（约=超时）", 3.0 < el <= 6.0, "%.2fs" % el)

    print("[3] 防重放：同一 request_id 第二次回应被拒")
    b3 = ApprovalBroker(timeout=4.0, session="s3")
    gate3 = b3.gate_for(tag="t3", session="s3")
    res3 = {"v": None}
    th3 = threading.Thread(target=lambda: res3.__setitem__("v",
                         gate3(Cap("x"), Dev("手机"), {}, 1)))
    th3.start()
    time.sleep(0.3)
    rid3 = b3.pending()[0]["request_id"]
    ok3a, _ = b3.respond(rid3, True)
    ok3b, why3b = b3.respond(rid3, True)   # 第二次
    th3.join(timeout=3)
    check("第一次送达", ok3a is True, "first")
    check("第二次被拒（已结束）", ok3b is False and "结束" in why3b, why3b)
    check("首次 approve 已生效", res3["v"] == (True, ""), repr(res3["v"]))

    print("[4] 破坏性双确认：不可逆动作一点不过，double=true 才过")
    b4 = ApprovalBroker(timeout=4.0, session="s4")
    gate4 = b4.gate_for(tag="t4", session="s4")
    res4 = {"v": None}
    th4 = threading.Thread(target=lambda: res4.__setitem__("v",
                         gate4(Cap("rm", "删除文件", destructive=True), Dev("手机"), {}, 2)))
    th4.start()
    time.sleep(0.3)
    rid4 = b4.pending()[0]["request_id"]
    ok4a, why4a = b4.respond(rid4, True, double=False)   # 第一次：单确认
    ok4b, _ = b4.respond(rid4, True, double=True)        # 第二次：双确认
    th4.join(timeout=3)
    check("单确认被拒（需二次确认）", ok4a is False and "二次确认" in why4a, why4a)
    check("双确认送达", ok4b is True, "second")
    check("破坏性最终生效", res4["v"] == (True, ""), repr(res4["v"]))


if __name__ == "__main__":
    main()
    if FAIL:
        print("\n=== t29 FAIL ===")
        for f in FAIL:
            print(" -", f)
        sys.exit(1)
    print("\n=== t29 ALL PASS ===")
    sys.exit(0)

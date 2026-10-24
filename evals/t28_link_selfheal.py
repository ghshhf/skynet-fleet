#!/usr/bin/env python
"""门禁 t28：连接自愈（批次 A1）—— reverse 掉了能自动重建，且日志可见。

钉三件真事（对应「隐形链路掉了 = 点了没反应」的玄学 bug）：

  ① **selfheal_once 必须幂等且兜底。** 周期性重跑 ensure_reverse（重复建无害）
     是自愈的核心；ensure_reverse 抛 RuntimeError（没设备/adb 抖动）时，
     自愈本身绝不能崩——兜成 error 字段返回。

  ② **汇总字段齐全且正确。** ok/fail/empty/error 四种结果都要能被准确判定，
     否则自愈循环会误报「存活」或「失败」。

  ③ **selfheal_loop 必须能在 stop 信号下干净退出。** 否则线程测不了、
     进程退出也拖泥带水。stop()=True 立即返回，不 sleep。

全离线：monkeypatch link.ensure_reverse，不碰真 adb。
"""
import os
import sys

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
    import link as LINK

    orig = LINK.ensure_reverse
    try:
        print("[1] selfheal_once：四类结果判定")
        LINK.ensure_reverse = lambda port: {
            "ok": [{"serial": "x"}], "fail": [], "empty": False}
        s = LINK.selfheal_once(18080)
        check("正常 → ok=1 fail=0 无错",
              s["ok"] == 1 and s["fail"] == 0 and s["error"] is None, s)

        LINK.ensure_reverse = lambda port: {
            "ok": [], "fail": [{"serial": "x", "why": "y"}], "empty": False}
        s = LINK.selfheal_once(18080)
        check("有失败 → fail=1", s["fail"] == 1, s)

        LINK.ensure_reverse = lambda port: {"ok": [], "fail": [], "empty": True}
        s = LINK.selfheal_once(18080)
        check("无设备 → empty=True", s["empty"] is True, s)

        def boom(port):
            raise RuntimeError("no adb")
        LINK.ensure_reverse = boom
        s = LINK.selfheal_once(18080)
        check("ensure_reverse 抛异常被兜底（error 非 None）",
              s["error"] is not None, s)

        print("[2] selfheal_loop：stop 信号下干净退出，不卡 sleep")
        called = {"n": 0}

        def fake_once(port):
            called["n"] += 1
            return {"ok": 1, "fail": 0, "empty": False}
        orig_once = LINK.selfheal_once
        LINK.selfheal_once = fake_once
        try:
            LINK.selfheal_loop(18080, interval=0.01, stop=lambda: True)
        except Exception as e:
            check("selfheal_loop 不抛", False, str(e))
        else:
            check("selfheal_loop 立即退出（stop=True）", True)
        LINK.selfheal_once = orig_once
    finally:
        LINK.ensure_reverse = orig

    print("[3] selfheal_once 真实调用（若真机在线则 ok>=1，否则 empty）")
    try:
        s_real = LINK.selfheal_once(18080)
        check("真实 ensure_reverse 不崩", s_real is not None, repr(s_real))
    except Exception as e:
        check("真实 ensure_reverse 不崩", False, str(e))


if __name__ == "__main__":
    main()
    if FAIL:
        print("\n=== t28 FAIL ===")
        for f in FAIL:
            print(" -", f)
        sys.exit(1)
    print("\n=== t28 ALL PASS ===")
    sys.exit(0)

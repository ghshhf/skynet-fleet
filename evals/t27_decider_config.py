#!/usr/bin/env python
"""门禁 t27：决策模型地址不硬编码 + 不可达时如实告知（批次 A3）。

钉三件真事（对应「决策模型消失 = 界面静默」的玄学 bug）：

  ① **决策地址可经环境变量 AT_DECIDER_URL 覆盖。** 原代码是字面常量
     http://127.0.0.1:18080，注释写着「可用环境变量覆盖」但代码根本没读
     环境变量 ⇒ 换机/换端口就得改源码重发。钉成：运行时读环境变量，
     缺省才用常量。

  ② **不可达端点必须返回 None、不抛。** decide() 连不上/超时是「暂时状态」，
     抛异常会把「工具面不可用」升级成「对话炸了」，违背「增强不是前置」。

  ③ **run_tools 在决策层不可达时必须 fire("warn") 说出来。** 原代码把
     `None`（决策层挂）和 `none`（模型说不用）合并静默 break —— 决策模型
     消失时界面表现为「什么都没发生」，极难排查。钉成：None 单独分支，
     推一条可见 warn 事件（server 端落 [tool-warn] 日志）。

全离线：不连真网络，用假 urlopen / 假 decide 驱动判定。
"""
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok", name)
    else:
        FAIL.append(f"{name}: {detail}")
        print("  FAIL", name, "-", detail)


def main():
    from fleet import agent_tools as AT

    print("[1] 不可达端点 → None 不抛，且快速返回")
    t0 = time.time()
    r = AT.decide("比特币现在多少钱", url="http://127.0.0.1:1", timeout=1.5)
    check("不可达端点返回 None", r is None, repr(r))
    check("不可达快速返回 (<5s)", time.time() - t0 < 5, f"{time.time()-t0:.2f}s")

    print("[2] 环境变量 AT_DECIDER_URL 覆盖决策地址")
    captured = {}
    orig_open = urllib.request.urlopen

    def fake_open(req, timeout=None):
        captured["url"] = req.full_url
        raise urllib.error.URLError("fake-unreachable")

    urllib.request.urlopen = fake_open
    os.environ["AT_DECIDER_URL"] = "http://envhost.invalid:1234"
    try:
        r2 = AT.decide("比特币现在多少钱", url=None, timeout=1.0)
    finally:
        urllib.request.urlopen = orig_open
        os.environ.pop("AT_DECIDER_URL", None)
    check("decide 实际请求了环境变量里的地址",
          "envhost.invalid:1234" in captured.get("url", ""), captured)
    check("覆盖后不可达仍返回 None", r2 is None, repr(r2))

    print("[3] run_tools 决策层不可达 → fire('warn') 可见，不静默")
    phases = []

    def mock_ev(phase, data=None):
        phases.append(phase)

    orig_decide = AT.decide
    AT.decide = lambda *a, **k: None  # 模拟决策层整体不可用
    try:
        AT.run_tools("比特币现在多少钱",
                     lambda cap, args: {"ok": True, "code": "ok", "data": {}},
                     tiers=AT.load_tiers(), on_event=mock_ev)
    finally:
        AT.decide = orig_decide
    check("fire 了 warn 事件", "warn" in phases, phases)
    check("未静默 break（warn 在 none 之前）",
          phases and phases[0] == "warn", phases)


if __name__ == "__main__":
    t = __import__("time")
    main()
    if FAIL:
        print("\n=== t27 FAIL ===")
        for f in FAIL:
            print(" -", f)
        sys.exit(1)
    print("\n=== t27 ALL PASS ===")
    sys.exit(0)

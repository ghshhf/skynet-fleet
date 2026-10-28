# -*- coding: utf-8 -*-
"""决策准确率跑分：拿 30 条 gold 唯一的题去打**产线** decide()。

## 为什么要用产线函数，不另写探针

探针里手写的 system 提示、schema、解析逻辑，和 `fleet/agent_tools.py` 里的
是两份代码。两份必然漂——2026-10-29 那次「提示词从没提 tool 字段」就是
漂出来的：探针里我自己写了一句"填在 tool 字段"，所以我测出来是对的，
而产线里那句话根本不存在。**准确率必须打真身。**

## 怎么用

    python tools/decide_bench.py                 # 用算力页里配的那个（推荐）
    python tools/decide_bench.py 18083           # 换端口
    python tools/decide_bench.py http://x:1234   # 换远端

不传参数时**走算力配置**（`compute.route("decide")`），不是写死的 18083。
写死端口的后果 2026-10-29 真踩过：算力页上明明配好了 zen-gate（1.9 秒），
跑分脚本却还去打 18083 那个没起的本地模型，跑出一片超时。

`--full` 走一级制（58 个工具一次全给），不传就走两级分域。
实测（同一批 30 条）：云端一级制 87% / 2.0s，两级 100% / 2.9s。

每条都清缓存，确保每条都真打模型（缓存会把成绩刷成假的）。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

argv = [a for a in sys.argv[1:] if not a.startswith("--")]
if "--full" in sys.argv:
    os.environ["AT_DECIDE_FULL"] = "1"

if argv:
    target = argv[0]
    os.environ["AT_DECIDER_URL"] = target if target.startswith("http") else \
        "http://127.0.0.1:%s" % target
else:
    # 不传就用算力配置。这里不清 AT_DECIDER_URL —— 用户自己 export 过的话
    # 那是有意的，_decider_target 会按「显式 > 环境变量 > 算力配置」的顺序取。
    pass

from fleet import agent_tools as A                            # noqa: E402
from decide_cases import CASES                                # noqa: E402

_t = A._decider_target()
print("目标: %s | 模型: %s | 组织: %s" % (
    _t[0], _t[2] or "(端点默认)",
    "一级制 58 选 1" if os.environ.get("AT_DECIDE_FULL") == "1" else "两级分域"))
print()

ok = 0
rows = []
tt = 0.0
for q, gold, _why in CASES:
    A._DECIDE_CACHE.clear()
    t0 = time.time()
    r = A.decide(q)
    el = time.time() - t0
    got = (r or {}).get("tool") if isinstance(r, dict) else None
    good = got == gold
    ok += good
    tt += el
    rows.append((good, q, gold, got, el, A.LAST_FAIL[0], list(A.LAST_TRACE)))
    print("%s %-22s 应=%-20s 实=%-20s %5.1fs" %
          ("ok " if good else "错 ", q, gold, str(got), el), flush=True)

n = len(CASES)
print("\n==== %d/%d = %.0f%% | 平均 %.1fs | 端点 %s ====" %
      (ok, n, 100.0 * ok / n if n else 0, tt / max(n, 1),
       # 用解析后的实际目标，不是环境变量 —— 不传参数时环境变量根本不存在，
       # 直接读会 KeyError 把整轮成绩吞掉（本轮踩过）。
       _t[0]))
for good, q, gold, got, el, fail, trace in rows:
    if not good:
        print("  错 %-22s 应=%-18s 实=%-18s %s" % (q, gold, str(got), fail or ""))
        for t in trace:
            print("       %s" % t)
sys.exit(0 if ok == n else 1)

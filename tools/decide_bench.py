# -*- coding: utf-8 -*-
"""决策准确率跑分：拿 30 条 gold 唯一的题去打**产线** decide()。

## 为什么要用产线函数，不另写探针

探针里手写的 system 提示、schema、解析逻辑，和 `fleet/agent_tools.py` 里的
是两份代码。两份必然漂——2026-10-29 那次「提示词从没提 tool 字段」就是
漂出来的：探针里我自己写了一句"填在 tool 字段"，所以我测出来是对的，
而产线里那句话根本不存在。**准确率必须打真身。**

## 怎么用

    python tools/decide_bench.py                 # 默认 18083
    python tools/decide_bench.py 18080           # 换端口
    python tools/decide_bench.py http://x:1234   # 换远端

每条都清缓存，确保每条都真打模型（缓存会把成绩刷成假的）。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

target = sys.argv[1] if len(sys.argv) > 1 else "18083"
os.environ["AT_DECIDER_URL"] = target if target.startswith("http") else \
    "http://127.0.0.1:%s" % target

from fleet import agent_tools as A                            # noqa: E402
from decide_cases import CASES                                # noqa: E402

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
       os.environ["AT_DECIDER_URL"]))
for good, q, gold, got, el, fail, trace in rows:
    if not good:
        print("  错 %-22s 应=%-18s 实=%-18s %s" % (q, gold, str(got), fail or ""))
        for t in trace:
            print("       %s" % t)
sys.exit(0 if ok == n else 1)

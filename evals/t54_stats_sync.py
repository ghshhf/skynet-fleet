#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T54 统计数字不许漂移：`docs/_stats.json` 必须等于脚本现算出来的值。

为什么要有这一项
----------------
天网的文档里到处是进度数字（能力数 / 门禁数 / eval 数 / 文档数 / App build），
这条历史上已经漂成过四个门禁数同时流通（52 / 58 / 67 / 74）、三份文档数并存。
根子不是有人手滑，是**数字与真源之间没有断言**：`tools/gen_stats.py` 算得出
真值，但没人保证入库的那份 `_stats.json` 跟它一样。

于是这里钉一条：入库的数字与现算的数字逐字段相等。改代码后忘了重跑
`python tools/gen_stats.py`，这一项就红——**漂移当场暴露，而不是等半年后
有人对着旧文档汇报进度**。

同时它还兜住另一件事：`gen_stats.py` 自己的计数口径也归它管。能力数若退回
「只数 catalog.py」，vendored_* 里的能力就统计不到，数字会假性变小——
这一项会同时发现「口径变了」和「文件没更新」。

门禁映射
--------
本项显式声明它额外覆盖数据文件 `docs/_stats.json`（GATE_COVERS）。
import 闭包只能表达 .py 之间的依赖，数据文件靠这条声明进映射，
否则改它永远「无自测覆盖」。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 让门禁映射看得见 docs/_stats.json（import 闭包表达不了数据文件的依赖）
GATE_COVERS = ("docs/_stats.json",)

from tools import gen_stats                                # noqa: E402

FAIL = []

# 每次现算都会变的字段：它们描述「这次跑的时刻」或「此刻环境」，不是代码事实。
# 拿它们做相等断言等于让门禁恒红，红得没有信息量。
VOLATILE = ("generated_at", "capabilities_live", "devices_live", "live_sampled_at")


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


def main():
    stats_path = os.path.join(ROOT, "docs", "_stats.json")
    check(os.path.isfile(stats_path), "docs/_stats.json 不存在（先跑 tools/gen_stats.py）")
    if FAIL:
        print("T54 红：%d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1

    with open(stats_path, encoding="utf-8") as f:
        try:
            saved = json.load(f)
        except Exception as e:
            print("T54 红：docs/_stats.json 不是合法 JSON：%s" % e)
            return 1

    live = gen_stats.collect()

    # ---- 逐字段相等：入库的每个数字都要跟现算的一致（易变字段豁免）----
    live_stable = {k: v for k, v in live.items() if k not in VOLATILE}
    saved_stable = {k: v for k, v in saved.items() if k not in VOLATILE}
    for k, v in live_stable.items():
        check(k in saved_stable, "入库数字缺字段 %s（现算=%r）" % (k, v))
        if k in saved_stable:
            check(str(saved_stable[k]) == str(v),
                  "数字漂移：%s 入库=%r / 现算=%r（改完代码要跑 python tools/gen_stats.py）"
                  % (k, saved_stable[k], v))
    for k in saved_stable:
        check(k in live_stable, "入库多出字段 %s（gen_stats 已不再产出这一项）" % k)

    # ---- 口径不能悄悄退回：能力数必须盖住 vendored_* 与 kinds/ ----
    caps = live.get("capabilities_declared") or 0
    vendored = 0
    for fn in os.listdir(os.path.join(ROOT, "fleet")):
        if fn.startswith("vendored_") and fn.endswith(".py"):
            vendored += 1
    check(caps > vendored * 10,
          "能力声明数 %d 明显低于 vendored_* 文件数（%d 个），计数口径可能退回只数 catalog.py"
          % (caps, vendored))

    # ---- 退役能力名不许有残留（2026-11-03 加）----
    # 一次归并在三处踩到同一件事：groups 的等价声明消失、远程白名单静默 404、
    # 两张角色卡 tool_filter 让整个名册加载失败。共同点是**都不报「名字错了」**，
    # 而是报别的现象。所以旧名登记成清单，扫到 0 为止。
    gone = live.get("retired_refs") or []
    check(not gone,
          "有 %d 处还在用已退役的能力名：%s —— 改成新名（gen_stats.retired_refs 有对照）"
          % (len(gone), "、".join("%s(%s)" % (g["file"], g["cap"]) for g in gone[:5])))

    # ---- 四层口径必须都在，且不许自相矛盾 ----
    own = live.get("capabilities_owned")
    gat = live.get("capabilities_gated")
    check(isinstance(own, int), "capabilities_owned 不是整数（注册表没建起来？现算=%r）" % (own,))
    check(isinstance(gat, int) and gat >= 0, "capabilities_gated 不是整数（%r）" % (gat,))
    if isinstance(own, int) and caps:
        check(own <= caps, "有主能力 %d 大于声明 %d —— 口径串了" % (own, caps))
    if isinstance(gat, int) and isinstance(own, int):
        check(gat <= own, "有门禁能力 %d 大于有主 %d —— 门禁在测不存在的能力？" % (gat, own))

    print("现算：%s" % json.dumps(live, ensure_ascii=False))
    print("入库：%s" % json.dumps(saved, ensure_ascii=False))
    print()
    if FAIL:
        print("T54 红：%d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("T54 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())

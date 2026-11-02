#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T63 文档 ↔ 代码对齐：文档里说的东西必须真的存在，数字必须来自脚本。

为什么要有这一项
----------------
t54 管的是「代码 → `docs/_stats.json`」这一段：改代码不重跑 gen_stats 就红。
但 t54 看不到**正文**——`README.md` 和总纲里的表格、能力清单、门禁文件名，
全是另一套手写的东西，它们和 `_stats.json` 之间没有任何断言。

于是历史上出现过：README 停在第 160 条能力、总纲停在 151 条、代码实际 404 条，
三份数字同时在流通，谁都没错、谁都不是真的。

本项钉住三件事（都要求零误报，宁可少抓，不可乱红）：

  ① **自动块必须存在且与脚本现算一致**——数字只有一个来源。
  ② **正文不许写累加绝对值**（`能力表 151 → 160` 这种链）。链一旦有分支就会断，
     断过一次：README 里 378 排在 380 后面，两个数都还标着「能力表」。
  ③ **正文引用必须落地**——文档里写的 `evals/xxx.py`、`tools/xxx.py`、
     `fleet/xxx.py` 这些路径，文件得真的在。文档引用一个已删文件，
     比说错一个数字更坑：照着去找会白找半天。

同时顺带钉一件事：**游离门禁必须为 0**。`evals/` 里有、`CASES` 里没有，
等于写了没人跑；`docs/_stats.json` 的 `orphan_evals` 就是这份账。

分工（别重复）：
    t54 = 代码 ↔ _stats.json
    t63 = _stats.json ↔ 正文，以及正文引用的文件是否存在
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tools import gen_stats                                # noqa: E402

FAIL = []
WARN = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


def warn(msg):
    WARN.append(msg)


def _docs():
    """要检查的文档：README + 总纲 + docs 下所有 md（都受同一套纪律约束）。"""
    out = [os.path.join(ROOT, "README.md")]
    d = os.path.join(ROOT, "docs")
    try:
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".md"):
                out.append(os.path.join(d, fn))
    except OSError:
        pass
    return [p for p in out if os.path.isfile(p)]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# 正文路径引用：`evals/t60_parse.py` / `tools/gen_stats.py` / `fleet/net.py`
_PATH_RE = re.compile(r"`((?:evals|tools|fleet|docs)/[A-Za-z0-9_./\-]+\.(?:py|md|json))`")
# 累加绝对值：能力表 151 → 160（也认 -> 和 全角 →）
# 正则故意只认「能力表」这个前缀：这是本项目历史上的固定写法，抓它零误报。
# 想把口子放宽到「能力数 / 能力 N 个」就会误伤"举反例"的段落，得不偿失。
_SUM_RE = re.compile(r"能力表\s*\d+\s*(?:→|->|=>)\s*\d+")


def main():
    st = gen_stats.collect()

    # ---------- ① 自动块存在且同步 ----------
    bad_block = gen_stats.check_docs(st)
    check(not bad_block,
          "自动块与代码不一致：%s（修：python tools/gen_stats.py --write-docs）"
          % (", ".join(bad_block) or "-"))

    for rel in gen_stats.DOC_FILES:
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        text = _read(path)
        has_begin = gen_stats.BEGIN in text
        check(has_begin, "%s 没有自动块标记（数字必须进 <!--STATS:BEGIN--> 区块）" % rel)

    # ---------- ② 正文不许写累加绝对值 ----------
    for path in _docs():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        text = _read(path)
        for m in _SUM_RE.finditer(text):
            line = text[:m.start()].count("\n") + 1
            fail_msg = ("%s:%d 出现累加绝对值「%s」——改成本轮 +N，"
                        "绝对值只许出现在自动块里" % (rel, line, m.group(0)))
            FAIL.append(fail_msg)

    # ---------- ③ 正文引用的文件必须存在 ----------
    seen = set()
    for path in _docs():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        text = _read(path)
        for m in _PATH_RE.finditer(text):
            target = m.group(1)
            if target in seen:
                continue
            seen.add(target)
            full = os.path.join(ROOT, target.replace("/", os.sep))
            if not os.path.exists(full):
                line = text[:m.start()].count("\n") + 1
                FAIL.append("%s:%d 引用了不存在的文件 `%s`" % (rel, line, target))

    # ---------- ④ 游离门禁必须为 0 ----------
    orph = st.get("orphan_evals") or []
    check(not orph,
          "有 %d 份门禁写了没登记（等于没写）：%s —— 登记到 run_selftests.py 的 CASES，"
          "或删掉它" % (len(orph), "、".join(orph)))

    # ---------- ⑤ 口径自洽（防止把某一层当全部） ----------
    dec, own, gat = (st.get("capabilities_declared"), st.get("capabilities_owned"),
                     st.get("capabilities_gated"))
    if isinstance(dec, int) and isinstance(own, int):
        check(own <= dec, "有主 %s > 声明 %s，口径串了" % (own, dec))
    if isinstance(gat, int) and isinstance(own, int):
        check(gat <= own, "有门禁 %s > 有主 %s，门禁在测不存在的能力？" % (gat, own))
    # gated 是「正文/门禁里被点名」的下限，不是全覆盖。这条只做提示，不红。
    if isinstance(gat, int) and isinstance(own, int) and gat < own:
        warn("有门禁 %d / 有主 %d：`gated` 是**下限**（只认门禁里出现完整能力名的），"
             "不代表没被点名的能力就没测过" % (gat, own))

    for w in WARN:
        print("提示：" + w)

    if FAIL:
        print("T63 红：%d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1

    print("T63 全绿：自动块与代码一致 · 无累加绝对值 · 正文引用全部落地 · "
          "游离门禁 0 · 四层口径自洽")
    return 0


if __name__ == "__main__":
    sys.exit(main())

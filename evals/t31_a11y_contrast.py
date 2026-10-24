#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T31 活线门禁：桌面两套主题的无障碍 + 对比度（WCAG AA）。

为什么要它
----------
「看起来还行、实际看不清」是眼睛最不可靠的地方，尤其浅色主题下的 10~11px 小字
和一堆浅灰图标。上一版只跑 axe，结果报「违规 0 项」——**假绿**：
axe 的 color-contrast 规则对**图标字形**（单字符、纯非文字）直接判 incomplete，
本页面 16 个 `.ic` / `#themeBtn` / `#tbClose` 全在盲区里，恰恰是看得最吃力那批。
所以这门禁同时验两层：

  ① axe（真实引擎扫 wcag2a/2aa/2.1a/2.1aa 标签规则）不得有 serious/critical；
  ② 自算对比度（含图标字形、含渐变背景的**每个色标**取最差）：
     文字 ≥ 4.5、图标 ≥ 3.0（WCAG 1.4.11 非文本对比度），两套主题都要过。

环境缺 node / playwright / chromium 时 **skip 并说明缺什么**，不假装通过。
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UICHECK = os.path.join(ROOT, "tools", "uicheck")
A11Y = os.path.join(UICHECK, "a11y.js")

FAIL = []


def check(ok, msg):
    print(("  [ok] " if ok else "  [X ] ") + msg, flush=True)
    if not ok:
        FAIL.append(msg)


def node_bin():
    return os.environ.get("NODE_BIN") or "node"


def has_playwright():
    return os.path.isdir(os.path.join(UICHECK, "node_modules", "playwright"))


def main():
    check(os.path.exists(A11Y), "找到 tools/uicheck/a11y.js")
    try:
        v = subprocess.run([node_bin(), "--version"], capture_output=True, text=True, timeout=20)
        node_ok = v.returncode == 0
    except (OSError, subprocess.SubprocessError):
        node_ok = False
    check(node_ok, "node 在（%s --version 可执行）" % node_bin())
    if not node_ok:
        print("  [skip] 没 node ⇒ 活线对比度门禁跳过（不是通过）", flush=True)
        print("t31_a11y_contrast: SKIP", flush=True)
        return 0
    if not has_playwright():
        print("  [skip] 没装 playwright（tools/uicheck/node_modules/playwright）"
              " ⇒ 跳过。装法：cd tools/uicheck && npm i", flush=True)
        print("t31_a11y_contrast: SKIP", flush=True)
        return 0
    check(True, "playwright 已装")

    env = dict(os.environ)
    env["FLEET_PY"] = sys.executable          # UI 起的后端用当前解释器，不猜路径

    for theme in ("light", "dark"):
        r = subprocess.run([node_bin(), A11Y, "--theme", theme],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=360)
        out = (r.stdout or "") + (r.stderr or "")
        tail = "\n".join(out.strip().splitlines()[-6:])
        check(r.returncode == 0, "%s 主题 a11y 全过（rc=%d）" % (theme, r.returncode))
        check("对比度全部达标" in out,
              "%s 主题对比度达标（含图标字形 ≥3.0）" % theme)
        m = re.search(r"axe 违规 (\d+) 项", out)
        check(bool(m) and m.group(1) == "0",
              "%s 主题 axe 违规 0 项（实得 %s）" % (theme, m.group(1) if m else "?"))
        m2 = re.search(r"对比度不达标 (\d+) 处", out)
        check(not m2, "%s 主题没有对比度不达标项（实得 %s 处）"
              % (theme, m2.group(1) if m2 else 0))
        jf = os.path.join(UICHECK, "shots", "a11y-%s.json" % theme)
        if os.path.exists(jf):
            with open(jf, encoding="utf-8") as f:
                d = json.load(f)
            check(d.get("checked", 0) > 500,
                  "%s 主题真扫到了内容（%s 个文本节点，不是空壳页面）"
                  % (theme, d.get("checked")))
            check(len(d.get("contrast") or []) == 0,
                  "%s 主题 JSON 里 contrast 不达标清单为空" % theme)
        else:
            check(False, "%s 主题没产出 %s" % (theme, jf))
        if r.returncode != 0:
            print("---- %s 输出尾部 ----\n%s" % (theme, tail), flush=True)

    if FAIL:
        print("\nt31_a11y_contrast: FAIL (%d 项)" % len(FAIL), flush=True)
        return 1
    print("t31_a11y_contrast: PASS（两套主题 0 axe 违规 / 0 对比度不达标）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

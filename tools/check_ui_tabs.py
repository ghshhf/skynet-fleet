#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""静态门禁：资产页 Tab 的数量徽标必须**全部来自接口**（X1）。

为什么值得单独立一条门禁
------------------------
「技能 18」「连接器 12」这种手写数量是**不会报错的错**：后端加了一个能力，
界面上的数就永远停在旧值，没有任何测试会红，只有主人肉眼看出来。
总账把这条记成「X1 三 Tab 前端零硬编码，数量 = 接口返回值」。
静态门禁能挡住的是它的一半（另一半是 live 门禁，见 tools/uicheck/a11y.js
与 clickaudit.js 真渲染后取数）：

  ① index.html 里五个 tab（编队/专家/技能/连接器/声音）各有一个 `.tn` 徽标，
     且带 id（没有 id 就没法被数据填，等于装饰）；
  ② tab 的**标签文字里不许出现数字**（数字必须由 JS 填进 .tn）；
  ③ app.js 必须有 paintTabCounts()，且五个 tn id 都从 ASSET_CACHE 取数，
     取的是哪一个键要能对上接口返回的字段；
  ④ app.js 不许给这几个 id 写死字面量（`$("tnSkills").textContent = "18"`）。

退出码：0 全过；1 有不符项。
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "ui", "index.html")
JS = os.path.join(ROOT, "ui", "app.js")

# tab key → (徽标 id, ASSET_CACHE 里的键, 接口路径, 计数字段)
TABS = [
    ("fleet", "tnFleet", "fleet", "/api/fleet", "devices"),
    ("cards", "tnCards", "cards", "/api/cardstore", "cards"),
    ("skills", "tnSkills", "skills", "/api/skills", "skills"),
    ("conns", "tnConns", "conns", "/api/connectors", "connectors"),
    ("audio", "tnAudio", "audio", "/api/audio/presets", "presets"),
]

FAIL = []


def check(ok, msg):
    if not ok:
        FAIL.append(msg)
    print(("  [ok] " if ok else "  [X ] ") + msg, flush=True)


def read(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


def main():
    if not os.path.exists(HTML) or not os.path.exists(JS):
        print("  [X ] 找不到 ui/index.html 或 ui/app.js")
        return 1
    html = read(HTML)
    js = read(JS)

    # ---------- ① 每个 tab 都有带 id 的 .tn 徽标 ----------
    for key, tid, _ck, _api, _f in TABS:
        btn = re.search(r'<button[^>]*data-tab="%s"[^>]*>(.*?)</button>' % key, html, re.S)
        check(bool(btn), 'index.html 有 data-tab="%s" 的 tab 按钮' % key)
        if not btn:
            continue
        inner = btn.group(1)
        m = re.search(r'<span[^>]*class="tn"[^>]*id="%s"' % tid, inner)
        check(bool(m), '「%s」tab 带数量徽标 #%s' % (key, tid))
        # ---------- ② 标签文字里不许出现数字 ----------
        label = re.sub(r"<[^>]+>", "", inner).strip()
        check(not re.search(r"\d", label),
              '「%s」tab 标签无硬编码数字（现在是「%s」）' % (key, label))

    # ---------- ③ paintTabCounts 存在，且五个 id 都从缓存取数 ----------
    check("function paintTabCounts" in js, "app.js 定义了 paintTabCounts()")
    check("paintTabCounts()" in js.replace("function paintTabCounts", ""),
          "paintTabCounts() 有调用点（定义了不调等于没写）")
    body = js.split("function paintTabCounts", 1)[1][:1600] if "function paintTabCounts" in js else ""
    for key, tid, ck, _api, field in TABS:
        check(tid in body, '#%s 在 paintTabCounts 里被赋值' % tid)
        check("ASSET_CACHE." + ck in body,
              '#%s 的数取自 ASSET_CACHE.%s（接口数据，不是字面量）' % (tid, ck))
        check(field in body,
              '#%s 计数字段是 %s（与接口返回一致）' % (tid, field))
        # 该 id 的赋值语句里不许出现数字字面量（"18" / 18 都不行）
        m = re.search(r'_setTn\(\s*"%s"\s*,(.*?)\);' % tid, body, re.S)
        check(bool(m) and not re.search(r'["\']\d+["\']', m.group(1) if m else ""),
              '#%s 的赋值表达式里没有数字字面量' % tid)

    # ---------- ④ 不许给这些 id 写死字面量 ----------
    for _key, tid, _ck, _api, _f in TABS:
        m = re.search(r'\$\("%s"\)\.textContent\s*=\s*"(?:\d+)' % tid, js)
        check(not m, '#%s 没有被写死成字面量' % tid)

    # ---------- ⑤ 启动就要亮：不点 tab 也应拉一次 ----------
    check("refreshTabCounts()" in js, "启动时调 refreshTabCounts()（不点 Tab 也看得到规模）")
    for _key, _tid, _ck, api_path, _f in TABS:
        check('"%s"' % api_path in js, "app.js 引用了 %s" % api_path)

    if FAIL:
        print("\nui_tab_counts: FAIL (%d 项)" % len(FAIL), flush=True)
        for f in FAIL:
            print("   - " + f, flush=True)
        return 1
    print("ui_tab_counts: PASS（五个 Tab 徽标全部来自接口，前端零硬编码）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

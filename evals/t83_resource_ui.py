# -*- coding: utf-8 -*-
"""t83 —— 资源流手机版 UI 与本轮接口适配的回归守护。

理由（计划书 P26 第一条：加东西要说出真实使用场景）：本轮同时改了页面
（推特式信息流 / 分类界面 / 内嵌素材 / 关注状态）与 resource_center 的
feed / act / stats 契约，这些点没有别处覆盖，需要一条门禁防退化。
纯静态检查 + 临时 SQLite，秒级，不是耗时门禁。
"""
from __future__ import annotations

import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fleet.resource_page import PAGE              # noqa: E402
from fleet.resource_center import ResourceCenter  # noqa: E402

FAIL = []


def check(ok, name):
    print(("  [ok] " if ok else "  [X]  ") + name)
    if not ok:
        FAIL.append(name)


print("① 页面契约")
check("<!doctype html>" in PAGE and "</html>" in PAGE, "单页 HTML 完整")
check(PAGE.count("<script") == 1 and PAGE.count("</script>") == 1, "脚本块成对")
check("prompt(" not in PAGE and "alert(" not in PAGE, "没有原生弹窗")
for el in ("feed", "chips", "view-explore", "exploreBody", 
           "composer", "detail", "toast", "mask"):
    check('id="%s"' % el in PAGE, "元素 %s 在" % el)
check("资源流" in PAGE, "页面名称与既有门禁一致")
check("data:image/jpeg;base64," in PAGE and "__HERO_IMG__" not in PAGE,
      "本地生图素材随包内嵌")
check("app-version" in PAGE, "带版本更新标识")
for frag in ("@media (max-width:380px)", "@media (min-width:700px)",
             "prefers-color-scheme:dark", "env(safe-area-inset-bottom)"):
    check(frag in PAGE, "自适应 " + frag.split("(")[0].strip())
check("kind" in PAGE and "domain" in PAGE and "/api/resources?" in PAGE,
      "分类筛选参数接到既有接口")
check("IntersectionObserver" in PAGE, "无限滚动有特性检测")
check("confirm(" not in PAGE, "没有自加的二次确认闸门")

print("② 接口契约")
db = tempfile.mktemp(suffix=".db")
rc = ResourceCenter(db)
rc.ingest("t83", [
    {"url": "https://a.example/x", "title": "端侧推理", "summary": "AI",
     "kind": "article", "tags": ["AI"]},
    {"url": "https://b.example/y", "title": "视频生成", "summary": "视频",
     "kind": "video", "tags": ["视频"]},
])
check(all(x["kind"] == "video" for x in rc.feed(kind="video")["items"]), "按资源类型筛选")
check(all(x["domain"] == "b.example" for x in rc.feed(domain="b.example")["items"]),
      "来源只作筛选项")
check(len(rc.feed(topic="AI", kind="article")["items"]) == 1, "主题 + 类型组合筛选")
rid = rc.feed()["items"][0]["id"]
check(rc.act(rid, "favorite")["ok"], "收藏动作可写入")
check(len(rc.feed(mine="favorite")["items"]) == 1, "「我」页按行为筛出收藏")
check(len(rc.feed(mine="like")["items"]) == 0, "没赞过的不混进赞过列表")
r = rc.act("", "focus", "AI")
check(r["ok"] and "AI" in r["focus"], "关注主题落在服务端")
check("AI" in rc.stats()["focus"], "换设备能从服务端读回关注")
r2 = rc.act("", "focus", "AI")
check(r2["ok"] and "AI" not in r2["focus"], "再点一次取消关注")
check(rc.act("r_missing", "like").get("ok") is False, "无效资源仍诚实报错")

print("t83 resource_ui:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL))
sys.exit(1 if FAIL else 0)

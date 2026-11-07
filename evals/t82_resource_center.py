# -*- coding: utf-8 -*-
"""t82：资源整合 MVP —— 资源卡片、去重、信息流、行为偏好、NetForce 导入。

守计划书第 27–28 页的验收线：
  ① 一条内容只出现一次（规范链接 + 内容指纹两层去重）；
  ② 每条卡片保留来源与原始链接，多源转载合并不丢出处；
  ③ 点赞/收藏/评论落库并反向影响标签权重，重复点赞不刷权重；
  ④ 信息流有透明 `why`、同站限流、可分页；
  ⑤ 现有 resources.json / classified.json 能直接导入，不重爬。

全离线：临时 SQLite + 临时 JSON，不依赖 LLM、不联网。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet.resource_center import ResourceCenter, canon_url

FAIL = []


def check(cond, msg):
    print(("  [ok] " if cond else "  [X]  ") + msg)
    if not cond:
        FAIL.append(msg)


tmp = tempfile.mkdtemp(prefix="rc_")
db = os.path.join(tmp, "resources.db")
rc = ResourceCenter(db)

print("① 规范链接与入库去重")
check(canon_url("https://www.Example.com/a/?utm_source=x&b=2#top")
      == "https://example.com/a?b=2", "去 www / utm / fragment / 末尾斜杠")
check(canon_url("example.com/a") == "https://example.com/a", "裸域名补 https")

r = rc.ingest("test", [
    {"url": "https://example.com/a?utm_source=x", "title": "AI 模型入门",
     "summary": "从零讲注意力机制", "tags": ["AI", "教程"]},
    {"url": "https://example.com/b", "title": "另一个资源", "tags": ["工具"]},
    {"url": "https://mirror.com/a", "title": "AI 模型入门",
     "summary": "从零讲注意力机制", "tags": ["AI"]},
])
check(r["ok"] and r["new"] == 2 and r["merged"] == 1, "3 条入库 = 2 新 + 1 跨站合并")
check(rc.ingest("test", [{"url": "https://example.com/a?utm_source=y",
                          "title": "AI 模型入门", "summary": "从零讲注意力机制"}])["new"] == 0,
      "换 utm 再入库不会新增")

feed = rc.feed("", limit=10)
check(feed["ok"] and feed["total"] == 2, "信息流只有 2 张卡（去重后）")
check(all(x["why"] for x in feed["items"]), "每张卡都有透明 why")
check(any("AI" in x["tags"] for x in feed["items"]), "标签跟着卡片出来")

print("② 详情与多源出处")
a = next(x for x in feed["items"] if "AI" in x["tags"])
det = rc.detail(a["id"])
check(det["ok"] and len(det["item"]["origins"]) == 2, "转载合并后仍保留 2 个出处")
check(det["item"]["url"].startswith("https://example.com/a"), "卡片用规范链接")

print("③ 行为与偏好")
check(rc.act(a["id"], "comment", "")["ok"] is False, "空评论被拒")
r1 = rc.act(a["id"], "like")
check(r1["ok"] and any(p["tag"] == "AI" for p in r1["prefs"]), "点赞把 AI 权重抬起来")
check(rc.act(a["id"], "like").get("duplicate") is True, "重复点赞不重复加权")
check(rc.act(a["id"], "favorite")["ok"], "收藏落库")
check(rc.act(a["id"], "comment", "讲得清楚")["ok"], "评论落库")
det = rc.detail(a["id"])
check(len(det["item"]["actions"]) == 3, "详情里能看到 3 条行为")
f = rc.feed("", limit=10, session_id="s2")
first = f["items"][0]
check(first["id"] == a["id"], "偏好生效：点赞过的 AI 卡排第一")
check(first["liked"] and first["faved"] and first["comments"] == 1,
      "信息流卡片带回赞/收藏/评论标记")

print("④ 主题筛选 / 搜索 / 分页")
check(rc.feed("AI", limit=10)["total"] == 1, "主题 AI 只回 1 条")
check(rc.search("注意力")["items"], "搜索命中摘要")
more = [{"url": "https://site%d.com/p" % i, "title": "条目 %d" % i,
         "summary": "普通内容 %d" % i, "tags": ["杂项"]} for i in range(5)]
rc.ingest("test", more)
p1 = rc.feed("", limit=2, cursor=0, session_id="page")
p2 = rc.feed("", limit=2, cursor=p1["next_cursor"], session_id="page")
check(p1["next_cursor"] == 2 and len(p1["items"]) == 2, "第一页 2 条 + next_cursor=2")
check(not ({x["id"] for x in p1["items"]} & {x["id"] for x in p2["items"]}), "两页不重复")

print("⑤ NetForce 既有成果导入")
res = [{"url": "https://good.example", "domain": "good.example", "type": "site",
        "found_via": "seed", "noise": False},
       {"url": "https://noise.example", "domain": "noise.example", "type": "site",
        "noise": True}]
cls = [{"domain": "good.example", "title": "好站", "desc": "一个资源站",
        "category": "AI/工具", "http_ok": True}]
rp = os.path.join(tmp, "resources.json")
cp = os.path.join(tmp, "classified.json")
json.dump(res, open(rp, "w", encoding="utf-8"))
json.dump(cls, open(cp, "w", encoding="utf-8"))
imp = rc.import_existing(rp, cp)
check(imp["ok"] and imp["scanned"] == 1, "noise 站点被跳过，只搬 1 条")
check(rc.search("好站")["items"], "导入后可搜到标题")
st = rc.stats()
check(st["resources"] >= 8 and st["sources"] >= 2, "统计口径：资源数/来源数")
check(st["prefs"] and st["actions"] == 3, "偏好与行为统计对得上")

print("⑥ HTTP 接线")
import threading
import time
import urllib.error
import urllib.request

os.environ["FLEET_RESOURCE_DB"] = db
from fleet.agent_server import FleetAgentServer  # noqa: E402

srv = FleetAgentServer(host="127.0.0.1", port=0, token="test-token", build=False)
threading.Thread(target=srv.start, daemon=True).start()
for _ in range(100):
    if srv.bind_port:
        break
    time.sleep(0.05)
base = "http://127.0.0.1:%d" % srv.bind_port


def get(path, token=None):
    req = urllib.request.Request(base + path)
    if token:
        req.add_header("X-Fleet-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


code, html = get("/resources")
check(code == 200 and "资源流" in html, "GET /resources 出单页")
code, _ = get("/api/resources")
check(code == 401, "资源 API 没 token 仍 401")
code, body = get("/api/resources?limit=1", "test-token")
check(code == 200 and json.loads(body)["ok"], "带 token 能读信息流")
code, body = get("/api/resources/stats", "test-token")
check(code == 200 and json.loads(body)["resources"] >= 8, "stats 走 HTTP")
srv.stop()

print("t82 resource_center:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL))
sys.exit(1 if FAIL else 0)

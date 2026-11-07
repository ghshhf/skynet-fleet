# -*- coding: utf-8 -*-
"""resource_center —— 全域资源整合 MVP（资源卡片 + 混合信息流 + 行为偏好）。

计划书（2026-11-07《总体规划与资源整合路线图》）第 27–28 页的落地：
先盘查、再定义契约、最后才写代码。现有 NetForce 已经把「资源发现 / 分类 /
落盘」做出来了（`github_runner/nf/resource_finder.py`、`classify.py`），
这里**不重复采集**，只做三件它没有的事：

  ① 统一资源卡：一条内容 = 标题 / 摘要 / 规范链接 / 模态 / 来源 / 多源出处；
  ② 行为与偏好：点赞、评论、收藏落 SQLite，反向累计标签权重；
  ③ 透明推荐：规则打分（兴趣、质量、新鲜、没刷过、主题匹配）+ 同站限流，
     每条都带 `why`，不做黑盒。

边界：
  · 纯 stdlib + SQLite，不依赖 LLM、不联网；
  · 采集交给现有 finder，这里只 `ingest()` / `import_existing()`；
  · 去重两层：规范链接（同一条目换 utm 仍算同一条）+ 内容指纹（跨站转载合并）；
  · 数据库默认 `~/.fleet/resources.db`，测试用 `FLEET_RESOURCE_DB` 指向临时文件。
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime
from hashlib import sha256
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY, name TEXT, kind TEXT DEFAULT '',
  config TEXT DEFAULT '{}', created_at REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS resources (
  id TEXT PRIMARY KEY, canonical_url TEXT NOT NULL UNIQUE,
  fingerprint TEXT NOT NULL, title TEXT, summary TEXT,
  kind TEXT DEFAULT '', domain TEXT DEFAULT '', quality REAL DEFAULT 0.5,
  published_at REAL, created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS idx_res_fp ON resources(fingerprint);
CREATE INDEX IF NOT EXISTS idx_res_domain ON resources(domain);
CREATE INDEX IF NOT EXISTS idx_res_created ON resources(created_at);
CREATE TABLE IF NOT EXISTS origins (
  resource_id TEXT, source_id TEXT, external_id TEXT DEFAULT '',
  url TEXT, fetched_at REAL, raw TEXT,
  PRIMARY KEY (resource_id, source_id, external_id));
CREATE TABLE IF NOT EXISTS tags (
  resource_id TEXT, tag TEXT, weight REAL DEFAULT 1.0,
  PRIMARY KEY (resource_id, tag));
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, resource_id TEXT, action TEXT,
  text TEXT DEFAULT '', created_at REAL);
CREATE INDEX IF NOT EXISTS idx_actions_res ON actions(resource_id);
CREATE TABLE IF NOT EXISTS exposures (
  resource_id TEXT, session_id TEXT, times INTEGER DEFAULT 0,
  clicked INTEGER DEFAULT 0, last_at REAL,
  PRIMARY KEY (resource_id, session_id));
CREATE TABLE IF NOT EXISTS prefs (
  tag TEXT PRIMARY KEY, weight REAL DEFAULT 0, updated_at REAL);
CREATE TABLE IF NOT EXISTS focus (
  tag TEXT PRIMARY KEY, created_at REAL);
"""

_TRACK = {
    "fbclid", "gclid", "yclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src",
    "spm", "scm", "from", "share_token", "share_source", "utm_medium",
}
_DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".fleet", "resources.db")


def _now():
    return time.time()


def _iso(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")


def canon_url(url):
    """规范链接：去跟踪参数、统一小写域名/https、去掉末尾斜杠。

    与 `github_runner/nf/resource_finder.py:canon()` 同一套思路；这里不 import
    那个 117KB 的爬虫模块（它会拉起 SSL/代理上下文），保持资源中心可单测。
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    u = urlsplit(raw)
    netloc = (u.netloc or "").lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    netloc = re.sub(r":(80|443)$", "", netloc)
    path = re.sub(r"/+", "/", u.path or "/").rstrip("/") or "/"
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
         if not k.lower().startswith("utm_") and k.lower() not in _TRACK]
    return urlunsplit(("https", netloc, path, urlencode(sorted(q)), ""))


def _fingerprint(title, summary, canonical):
    """内容指纹：有摘要才按「标题+摘要」跨站去重，否则退回链接。"""
    title = (title or "").strip().lower()
    summary = (summary or "").strip().lower()
    base = (title + "\n" + summary[:800]) if summary else canonical
    return sha256(base.encode("utf-8")).hexdigest()


def _rid(fingerprint):
    return "r_" + fingerprint[:16]


def _tags_of(item):
    raw = item.get("tags") or item.get("category") or item.get("type") or []
    if isinstance(raw, str):
        raw = re.split(r"[/、,，|]+", raw)
    out, seen = [], set()
    for t in raw:
        t = str(t).strip()[:24]
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:8]


class ResourceCenter:
    """一个 SQLite 文件就是一套资源库；所有写操作走短连接，天然适配多线程 HTTP。"""

    def __init__(self, path=None):
        self.path = path or os.environ.get("FLEET_RESOURCE_DB") or _DEFAULT_DB
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # ------------------------------------------------------------ 来源 / 入库

    def upsert_source(self, source_id, name="", kind="", config=None):
        sid = (source_id or "").strip()
        if not sid:
            raise ValueError("source_id 不能为空")
        with self._conn() as c:
            c.execute(
                "INSERT INTO sources(id,name,kind,config,created_at,last_seen) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "name=COALESCE(NULLIF(excluded.name,''),sources.name), "
                "kind=COALESCE(NULLIF(excluded.kind,''),sources.kind), "
                "config=excluded.config, last_seen=excluded.last_seen",
                (sid, name or sid, kind or "", json.dumps(config or {}, ensure_ascii=False),
                 _now(), _now()))
        return sid

    def ingest(self, source_id, items, source_name="", source_kind=""):
        """把一批标准化资源写进库。item: url/title/summary/kind/tags/id/quality。"""
        if not source_id:
            return {"ok": False, "err": "缺 source_id"}
        if isinstance(items, dict):
            items = [items]
        self.upsert_source(source_id, source_name, source_kind)
        stat = {"new": 0, "merged": 0, "skipped": 0}
        now = _now()
        with self._conn() as c:
            for it in (items or []):
                if not isinstance(it, dict) or not (it.get("url") or "").strip():
                    stat["skipped"] += 1
                    continue
                canonical = canon_url(it["url"])
                if not canonical:
                    stat["skipped"] += 1
                    continue
                domain = (urlsplit(canonical).netloc or "").split(":")[0]
                title = (it.get("title") or "").strip() or domain
                summary = (it.get("summary") or it.get("desc") or "").strip()
                kind = (it.get("kind") or it.get("type") or "article").strip()
                quality = float(it.get("quality", 0.5) or 0.5)
                fp = _fingerprint(title, summary, canonical)
                row = c.execute(
                    "SELECT id FROM resources WHERE canonical_url=? OR fingerprint=? LIMIT 1",
                    (canonical, fp)).fetchone()
                if row:
                    rid = row["id"]
                    c.execute(
                        "UPDATE resources SET updated_at=?, "
                        "title=CASE WHEN COALESCE(title,'')='' THEN ? ELSE title END, "
                        "summary=CASE WHEN COALESCE(summary,'')='' THEN ? ELSE summary END "
                        "WHERE id=?", (now, title, summary, rid))
                    stat["merged"] += 1
                else:
                    rid = _rid(fp)
                    c.execute(
                        "INSERT OR IGNORE INTO resources(id,canonical_url,fingerprint,title,"
                        "summary,kind,domain,quality,published_at,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, canonical, fp, title, summary, kind, domain, quality,
                         it.get("published_at") or None, now, now))
                    stat["new"] += 1
                ext = str(it.get("id") or it.get("external_id") or canonical)[:200]
                c.execute(
                    "INSERT INTO origins(resource_id,source_id,external_id,url,fetched_at,raw) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(resource_id,source_id,external_id) "
                    "DO UPDATE SET url=excluded.url, fetched_at=excluded.fetched_at",
                    (rid, source_id, ext, canonical, now,
                     json.dumps(it, ensure_ascii=False)[:4000]))
                for tag in _tags_of(it):
                    c.execute(
                        "INSERT INTO tags(resource_id,tag,weight) VALUES(?,?,1.0) "
                        "ON CONFLICT(resource_id,tag) DO NOTHING", (rid, tag))
        out = {"ok": True, **stat}
        out["total"] = stat["new"] + stat["merged"]
        return out

    def import_existing(self, resources_path=None, classified_path=None):
        """把 NetForce 已有的 resources.json / classified.json 搬进资源库。

        只搬非 noise 的资源站；标题/摘要/分类来自 classified，缺了就退回域名。
        这保证「已有采集成果」立刻能出现在信息流里，而不是另起炉灶重爬。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        nf = os.environ.get("NETFORCE_OUT") or os.path.join(root, "github_runner", "nf")
        rp = resources_path or os.path.join(nf, "resources.json")
        cp = classified_path or os.path.join(nf, "classified.json")
        try:
            rows = json.load(open(rp, encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "err": "读不到 resources.json：%s" % e}
        meta = {}
        try:
            for x in json.load(open(cp, encoding="utf-8")):
                meta[x.get("domain", "")] = x
        except Exception:
            meta = {}
        items = []
        for x in rows if isinstance(rows, list) else []:
            if not isinstance(x, dict) or not x.get("domain"):
                continue
            if x.get("noise"):
                continue
            dom = x["domain"]
            m = meta.get(dom, {})
            cat = (m.get("category") or "").strip()
            title = (m.get("title") or "").strip() or dom
            summary = (m.get("desc") or "").strip()
            tags = [t for t in re.split(r"[/、,，|]+", cat) if t]
            if not tags:
                tags = [x.get("type") or "站点"]
            items.append({
                "url": x.get("url") or ("https://" + dom),
                "title": title, "summary": summary, "kind": "site",
                "tags": tags, "id": dom,
                "quality": 0.62 if m.get("http_ok") and m.get("title") else 0.42,
            })
        r = self.ingest("netforce", items, "NetForce 资源发现", "crawler")
        r["source"] = "netforce"
        r["scanned"] = len(items)
        return r

    # ------------------------------------------------------------ 信息流

    def feed(self, topic="", limit=20, cursor=0, session_id="default",
             kind="", domain="", mine=""):
        """混合信息流。topic 按标题/摘要/标签模糊匹配，kind / domain 精确筛选。

        计划书 P13：来源（domain）是筛选项，不是主分类；分类按内容（tags/kind）。
        mine = favorite / like：只看本人有该行为的资源（「我」页的收藏/赞过）。
        """
        topic = (topic or "").strip()
        kind = (kind or "").strip()
        domain = (domain or "").strip()
        mine = (mine or "").strip()
        try:
            limit = max(1, min(50, int(limit)))
            cursor = max(0, int(cursor))
        except Exception:
            limit, cursor = 20, 0
        now = _now()
        with self._conn() as c:
            conds, args = [], []
            if topic:
                like = "%" + topic + "%"
                conds.append("(r.title LIKE ? OR r.summary LIKE ? OR EXISTS("
                             "SELECT 1 FROM tags t WHERE t.resource_id=r.id AND t.tag LIKE ?))")
                args += [like, like, like]
            if kind:
                conds.append("r.kind = ?")
                args.append(kind)
            if domain:
                conds.append("r.domain = ?")
                args.append(domain)
            if mine in ("favorite", "like"):
                conds.append("EXISTS(SELECT 1 FROM actions a WHERE a.resource_id=r.id "
                             "AND a.action=?)")
                args.append(mine)
            where = ("WHERE " + " AND ".join(conds)) if conds else ""
            rows = c.execute(
                "SELECT r.* FROM resources r %s ORDER BY r.created_at DESC LIMIT 500" % where,
                args).fetchall()
            if not rows:
                return {"ok": True, "items": [], "next_cursor": None,
                        "total": 0, "session": session_id}
            ids = [r["id"] for r in rows]
            marks = ",".join("?" * len(ids))
            tag_rows = c.execute(
                "SELECT resource_id,tag FROM tags WHERE resource_id IN (%s)" % marks,
                ids).fetchall()
            tags = {}
            for t in tag_rows:
                tags.setdefault(t["resource_id"], []).append(t["tag"])
            prefs = {p["tag"]: p["weight"] for p in
                     c.execute("SELECT tag,weight FROM prefs").fetchall()}
            exp = {e["resource_id"]: {"times": e["times"], "clicked": e["clicked"]}
                   for e in c.execute(
                       "SELECT resource_id,times,clicked FROM exposures WHERE session_id=?",
                       (session_id,)).fetchall()}
            # 同站看得越多，越往后压：避免一屏全是同一个站。
            dom_by_id = {r["id"]: r["domain"] for r in rows}
            dom_seen = {}
            for rid, e in exp.items():
                d = dom_by_id.get(rid, "")
                if d:
                    dom_seen[d] = dom_seen.get(d, 0) + max(1, e["times"])

        scored = []
        for r in rows:
            rt = tags.get(r["id"], [])
            pref = sum(prefs.get(t, 0.0) for t in rt)
            interest = max(0.0, min(1.0, pref / 3.0))
            age_days = max(0.0, now - (r["published_at"] or r["created_at"] or now)) / 86400.0
            fresh = math.exp(-age_days / 14.0)
            times = exp.get(r["id"], {}).get("times", 0)
            novel = 1.0 / (1.0 + times)
            match = 0.0
            if topic and (topic.lower() in (r["title"] or "").lower()
                          or topic in (r["summary"] or "")
                          or any(topic in t for t in rt)):
                match = 1.0
            # 确定性探索：同一个 session 里没见过的条目轻微抬升，不靠随机数。
            explore = (int(sha256((r["id"] + session_id).encode()).hexdigest()[:4], 16)
                       % 100) / 100.0
            domain_pen = min(0.36, 0.12 * dom_seen.get(r["domain"], 0))
            score = (0.35 * interest + 0.20 * (r["quality"] or 0.5)
                     + 0.20 * fresh + 0.15 * novel + 0.10 * explore
                     + 0.25 * match - domain_pen)
            why = []
            if interest > 0:
                why.append("兴趣 +%.2f" % (0.35 * interest))
            if match:
                why.append("主题匹配")
            if times == 0:
                why.append("没刷过")
            if fresh > 0.8:
                why.append("新鲜")
            if domain_pen:
                why.append("同站已多看")
            scored.append({
                "id": r["id"], "title": r["title"] or r["domain"],
                "summary": (r["summary"] or "")[:220], "url": r["canonical_url"],
                "domain": r["domain"], "kind": r["kind"], "tags": rt,
                "published_at": _iso(r["published_at"] or r["created_at"]),
                "score": round(score, 4), "why": " · ".join(why[:2]) or "随机看看",
            })
        scored.sort(key=lambda x: (-x["score"], x["id"]))
        page = _diverse(scored[cursor:cursor + limit * 2], limit)
        with self._conn() as c:
            # 一次性取这一页的行为标记，避免每张卡再查三次（手机端一屏 20 张）。
            ids = [it["id"] for it in page]
            marks = {}
            if ids:
                q = ("SELECT resource_id,action,COUNT(*) n FROM actions WHERE resource_id IN ("
                     + ",".join("?" * len(ids)) + ") GROUP BY resource_id,action")
                for a in c.execute(q, ids):
                    marks.setdefault(a["resource_id"], {})[a["action"]] = a["n"]
            for it in page:
                m = marks.get(it["id"], {})
                it["liked"] = "like" in m
                it["faved"] = "favorite" in m
                it["comments"] = m.get("comment", 0)
            for it in page:
                c.execute(
                    "INSERT INTO exposures(resource_id,session_id,times,clicked,last_at) "
                    "VALUES(?,?,1,0,?) ON CONFLICT(resource_id,session_id) "
                    "DO UPDATE SET times=exposures.times+1, last_at=excluded.last_at",
                    (it["id"], session_id, now))
        nxt = cursor + len(page) if len(scored) > cursor + len(page) else None
        return {"ok": True, "items": page, "next_cursor": nxt,
                "total": len(scored), "session": session_id}

    def search(self, q="", limit=20):
        q = (q or "").strip()
        if not q:
            return {"ok": True, "items": []}
        like = "%" + q + "%"
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT r.* FROM resources r LEFT JOIN tags t ON t.resource_id=r.id "
                "WHERE r.title LIKE ? OR r.summary LIKE ? OR t.tag LIKE ? "
                "ORDER BY r.created_at DESC LIMIT ?", (like, like, like, int(limit))).fetchall()
        return {"ok": True, "items": [{
            "id": r["id"], "title": r["title"], "summary": (r["summary"] or "")[:200],
            "url": r["canonical_url"], "domain": r["domain"], "kind": r["kind"],
        } for r in rows]}

    # ------------------------------------------------------------ 详情 / 行为

    def detail(self, resource_id):
        with self._conn() as c:
            r = c.execute("SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
            if not r:
                return {"ok": False, "err": "没有这条资源：%s" % resource_id}
            origins = [dict(x) for x in c.execute(
                "SELECT source_id,external_id,url,fetched_at FROM origins WHERE resource_id=?",
                (resource_id,)).fetchall()]
            tags = [x["tag"] for x in c.execute(
                "SELECT tag FROM tags WHERE resource_id=?", (resource_id,)).fetchall()]
            actions = [dict(x) for x in c.execute(
                "SELECT action,text,created_at FROM actions WHERE resource_id=? "
                "ORDER BY id DESC LIMIT 50", (resource_id,)).fetchall()]
        return {"ok": True, "item": {
            "id": r["id"], "title": r["title"], "summary": r["summary"],
            "url": r["canonical_url"], "domain": r["domain"], "kind": r["kind"],
            "quality": r["quality"], "tags": tags, "origins": origins,
            "actions": actions,
            "liked": any(a["action"] == "like" for a in actions),
            "faved": any(a["action"] == "favorite" for a in actions),
            "comments": sum(1 for a in actions if a["action"] == "comment"),
            "published_at": _iso(r["published_at"] or r["created_at"]),
        }}

    def list_focus(self):
        """关注的主题存服务端：换机 / 多台手机打开同一地址，关注列表都还在。"""
        with self._conn() as c:
            return [x["tag"] for x in c.execute(
                "SELECT tag FROM focus ORDER BY created_at DESC").fetchall()]

    def act(self, resource_id, action, text=""):
        action = (action or "").strip().lower()
        text = (text or "").strip()[:500]
        if action == "focus":
            tag = text[:24]
            if not tag:
                return {"ok": False, "err": "关注主题不能为空"}
            with self._conn() as c:
                old = c.execute("SELECT 1 FROM focus WHERE tag=?", (tag,)).fetchone()
                if old:
                    c.execute("DELETE FROM focus WHERE tag=?", (tag,))
                else:
                    c.execute("INSERT OR IGNORE INTO focus(tag,created_at) VALUES(?,?)",
                              (tag, _now()))
            return {"ok": True, "action": "focus", "added": not bool(old),
                    "focus": self.list_focus()}
        if action not in ("like", "favorite", "comment"):
            return {"ok": False, "err": "action 只支持 like / favorite / comment / focus"}
        if action == "comment" and not text:
            return {"ok": False, "err": "评论不能为空"}
        with self._conn() as c:
            r = c.execute("SELECT id FROM resources WHERE id=?", (resource_id,)).fetchone()
            if not r:
                return {"ok": False, "err": "没有这条资源：%s" % resource_id}
            if action in ("like", "favorite"):
                old = c.execute(
                    "SELECT id FROM actions WHERE resource_id=? AND action=? LIMIT 1",
                    (resource_id, action)).fetchone()
                if old:
                    return {"ok": True, "duplicate": True,
                            "id": resource_id, "action": action}
            c.execute("INSERT INTO actions(resource_id,action,text,created_at) VALUES(?,?,?,?)",
                      (resource_id, action, text, _now()))
            gain = {"like": 1.0, "favorite": 2.0, "comment": 0.5}[action]
            for row in c.execute("SELECT tag FROM tags WHERE resource_id=?",
                                 (resource_id,)).fetchall():
                c.execute(
                    "INSERT INTO prefs(tag,weight,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(tag) DO UPDATE SET weight=prefs.weight+excluded.weight, "
                    "updated_at=excluded.updated_at", (row["tag"], gain, _now()))
            c.execute("UPDATE exposures SET clicked=1 WHERE resource_id=?",
                      (resource_id,))
            top = c.execute("SELECT tag,weight FROM prefs ORDER BY weight DESC LIMIT 5").fetchall()
        return {"ok": True, "id": resource_id, "action": action,
                "prefs": [{"tag": x["tag"], "weight": round(x["weight"], 2)} for x in top]}

    def stats(self):
        with self._conn() as c:
            def one(sql):
                return c.execute(sql).fetchone()[0]
            prefs = [{"tag": x["tag"], "weight": round(x["weight"], 2)} for x in c.execute(
                "SELECT tag,weight FROM prefs ORDER BY weight DESC LIMIT 10").fetchall()]
        return {"ok": True, "resources": one("SELECT COUNT(*) FROM resources"),
                "sources": one("SELECT COUNT(*) FROM sources"),
                "origins": one("SELECT COUNT(*) FROM origins"),
                "actions": one("SELECT COUNT(*) FROM actions"),
                "tags": one("SELECT COUNT(DISTINCT tag) FROM tags"),
                "prefs": prefs, "focus": self.list_focus()}


def _diverse(items, limit):
    """同站最多 2 条；不够再从剩余里补，保证一屏不全是同一个站。"""
    picked, rest, per = [], [], {}
    for it in items:
        d = it["domain"]
        if per.get(d, 0) < 2:
            per[d] = per.get(d, 0) + 1
            picked.append(it)
            if len(picked) >= limit:
                return picked
        else:
            rest.append(it)
    for it in rest:
        if len(picked) >= limit:
            break
        picked.append(it)
    return picked

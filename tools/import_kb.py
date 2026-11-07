# -*- coding: utf-8 -*-
"""把各采集节点的 kb.db 实体作为**对等来源**灌进资源中心。

理由：资源页是汇聚各独立节点采集结果的展示面；采集能力在各节点自己手里，
这里只做「节点数据 → 资源卡」的字段适配，不重爬、不新建存储、不新增服务。

用法:
  python tools/import_kb.py                       # 手机采集节点 phone_kb.db
  python tools/import_kb.py --db <path> --source-id <id>
  python tools/import_kb.py --merged             # 手机+电脑合并后的全量库
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fleet.resource_center import ResourceCenter  # noqa: E402

_PHONE_KB = os.path.join(os.path.dirname(_ROOT), "netforce", "_kb", "phone_kb.db")
_MERGED_KB = os.path.join(os.path.dirname(_ROOT), "netforce", "_kb", "merged.db")


def read_entities(path):
    if not os.path.exists(path):
        raise SystemExit("库不存在: %s" % path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        for r in con.execute(
                "SELECT id, platform, type, title, url, depth, payload, ts FROM entities"):
            yield dict(r)
    finally:
        con.close()


def to_item(r):
    """kb.db 的 entity → resource_center.ingest() 的 item。"""
    url = (r.get("url") or "").strip()
    if not url:
        return None
    raw = r.get("payload") or ""
    summary = ""
    if isinstance(raw, str) and raw.strip().startswith(("{", "[")):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                for k in ("summary", "desc", "text", "content", "body"):
                    if obj.get(k):
                        summary = obj[k]
                        break
                if not summary:
                    summary = json.dumps(obj, ensure_ascii=False)
            else:
                summary = json.dumps(obj, ensure_ascii=False)
        except Exception:
            summary = raw
    else:
        summary = raw
    summary = str(summary).strip()[:400]
    platform = (r.get("platform") or "采集").strip()
    kind = (r.get("type") or "article").strip()
    return {
        "url": url,
        "title": (r.get("title") or "").strip() or url,
        "summary": summary,
        "kind": kind,
        "tags": [platform, kind],
        "id": r.get("id"),
        "quality": 0.6,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=_PHONE_KB, help="采集节点的 kb.db 路径")
    ap.add_argument("--merged", action="store_true", help="改用手机+电脑合并库")
    ap.add_argument("--source-id", default="", help="来源节点 id（默认按库名生成）")
    ap.add_argument("--limit", type=int, default=0, help="只灌前 N 条（调试）")
    a = ap.parse_args()

    path = _MERGED_KB if a.merged else a.db
    source_id = a.source_id or ("kb-merged" if a.merged else "kb-phone-node")
    source_name = "手机采集节点" if not a.merged else "多节点汇聚库"

    items, seen = [], 0
    for r in read_entities(path):
        seen += 1
        it = to_item(r)
        if it:
            items.append(it)
        if a.limit and len(items) >= a.limit:
            break

    rc = ResourceCenter()
    out = rc.ingest(source_id, items, source_name, "crawler")
    print("库:", path)
    print("扫描实体:", seen, "有效资源卡:", len(items))
    print("写入:", out)
    st = rc.stats()
    print("资源库现有:", {k: st[k] for k in
                     ("resources", "sources", "origins", "tags")})
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

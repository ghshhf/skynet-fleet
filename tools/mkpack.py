"""把一份可读可 git 的 markdown 人格源，编译成记忆包。

为什么用 markdown 当源而不是直接手写 pack.json：
  pack.json 里每片都带 sha 指纹，人手写不动也改不动；而人格这种东西
  是要反复改的（一句话的口吻不对就要调）。所以源必须是人能读能 diff 的，
  指纹交给机器算 —— 改完重编译，id 自动跟着变，同步端只看 id 就够了。

源格式（见 personas/dafeiyu.md）：
    # name: dafeiyu
    ## identity            ← 层名，可带权重：## value 2.0
    - 一条记忆
    - [2026-10-23] 带日期的经历
    - 一条记忆 #tag1 #tag2
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fleet import memory_pack as M      # noqa: E402

_HEAD = re.compile(r"^##\s+([a-z]+)\s*([0-9.]+)?\s*$")
_NAME = re.compile(r"^#\s*name:\s*(\S+)\s*$")
_DATE = re.compile(r"^\[(\d{4})-(\d{2})-(\d{2})\]\s*")
_TAG = re.compile(r"#([\w\u4e00-\u9fff-]+)")


def parse(text: str):
    name, cur, weight = "main", None, 1.0
    out = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        m = _NAME.match(line.strip())
        if m:
            name = m.group(1)
            continue
        m = _HEAD.match(line.strip())
        if m:
            cur = m.group(1).lower()
            weight = float(m.group(2)) if m.group(2) else 1.0
            continue
        if not line.lstrip().startswith("-"):
            continue
        if cur is None:
            raise SystemExit(f"有条目出现在任何层之前：{line[:60]}")
        body = line.lstrip()[1:].strip()
        ts = None
        m = _DATE.match(body)
        if m:
            try:
                ts = time.mktime(time.strptime(m.group(0).strip("[] "), "%Y-%m-%d"))
            except ValueError:
                ts = None
            body = body[m.end():].strip()
        tags = _TAG.findall(body)
        body = _TAG.sub("", body).strip()
        out.append({"kind": cur, "text": body, "tags": tags or (), "ts": ts,
                    "weight": weight})
    return name, out


def build(src_path, out_dir=None):
    with open(src_path, encoding="utf-8") as f:
        name, rows = parse(f.read())
    pack = M.Pack(name=name, meta={"source": os.path.basename(src_path)})
    skipped = 0
    for r in rows:
        try:
            pack.add(M.shard(r["kind"], r["text"], tags=r["tags"],
                             weight=r["weight"], ts=r["ts"], source=name))
        except ValueError:
            skipped += 1
    if not out_dir:
        base = os.path.splitext(os.path.basename(src_path))[0]
        out_dir = os.path.join(os.path.dirname(os.path.abspath(src_path)), base)
    saved = pack.save(out_dir)
    return {"name": name, "count": saved["count"], "pack": saved["pack"],
            "by_kind": pack.by_kind(), "skipped": skipped, "dir": out_dir}


def main(argv=None):
    ap = argparse.ArgumentParser(description="人格源(md) → 记忆包(json)")
    ap.add_argument("src")
    ap.add_argument("-o", "--out", default="")
    args = ap.parse_args(argv)
    r = build(args.src, args.out or None)
    print(f"[=] {r['name']}: {r['count']} 片 {r['pack']}")
    print(f"[i] by_kind={r['by_kind']} skipped={r['skipped']}")
    print(f"[i] 落盘 {r['dir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

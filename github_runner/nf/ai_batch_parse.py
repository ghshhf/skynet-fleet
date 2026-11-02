# -*- coding: utf-8 -*-
"""解析主人的手写批次文件（类别|域名,域名,...）→ _ai_manual.json"""
import json, os, re, sys

# 落盘走 store（唯一临时名 + fsync），避免覆写判断表时被打断
import sys as _sys
_FLEET_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _FLEET_ROOT not in _sys.path:
    _sys.path.insert(0, _FLEET_ROOT)
from fleet import store  # noqa: E402
BASE = os.environ.get("NETFORCE_OUT") or os.path.dirname(os.path.abspath(__file__))
MANUAL_JSON = os.path.join(BASE, "_ai_manual.json")
VALID = re.compile(r"^[a-z0-9.\-]+$")

def main(paths):
    manual = json.load(open(MANUAL_JSON, encoding="utf-8")) if os.path.exists(MANUAL_JSON) else {}
    added = bad = dup = 0
    for p in paths:
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line or "|" not in line:
                continue
            cat, doms = line.split("|", 1)
            cat = cat.strip()
            if not cat or cat.endswith("?NO"):
                continue
            for d in doms.split(","):
                d = d.strip().lower()
                d = re.sub(r"\?NO$", "", d).strip()
                if not d or " " in d or "?" in d or d.endswith("NO") and not VALID.match(d):
                    if d: bad += 1
                    continue
                if not VALID.match(d):
                    bad += 1
                    continue
                if d in manual and manual[d] != cat:
                    dup += 1
                manual[d] = cat
                added += 1
    store.atomic_write_json(MANUAL_JSON, manual, indent=1)
    print("写入 %d 条判断（冲突 %d，无效 %d），累计 %d" % (added, dup, bad, len(manual)))

if __name__ == "__main__":
    main(sys.argv[1:])

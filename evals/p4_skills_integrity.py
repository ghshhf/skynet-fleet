#!/usr/bin/env python
"""门禁 p4：vendored 技能完整性（第4层技能自进化地基）。

钉三件事：
  ① 仓库 skills/ 下**每个**子技能都必须带 _audit.json，且结论为 clean。
     没有审计文件 = 未审查直接进仓 ⇒ 红（ClawHavoc 教训：盲拉带脚本技能会中招）。
  ② vendored 技能的 name 必须与目录名一致（防篡改 / 防张冠李戴）。
  ③ MANIFEST.json 的登记项与实际目录一一对应：
     登记了但目录没了 ⇒ 红；目录在但没登记 ⇒ 红。

全离线：只读本地文件，不起网络、不调模型。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet import skills as SK

FAIL = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  x ", msg)
        FAIL.append(msg)


def main():
    root = SK.REPO_SKILLS_DIR
    print("== p4 技能完整性门禁 ==")
    if not os.path.isdir(root):
        print("  -- 仓库 skills/ 不存在（尚无 vendored 技能，跳过）")
        print("\nP4 OK (skills/ 为空)")
        sys.exit(0)

    skill_dirs = sorted(
        e for e in os.listdir(root)
        if os.path.isdir(os.path.join(root, e))
    )
    check(len(skill_dirs) > 0, "skills/ 下至少有一个 vendored 技能（否则本门禁无意义）")

    manifest_path = os.path.join(root, "MANIFEST.json")
    manifest_names = set()
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                mdata = json.load(f)
            manifest_names = {e["name"] for e in mdata.get("entries", [])}
        except Exception as e:
            check(False, "MANIFEST.json 能解析：%s" % e)
    else:
        check(False, "MANIFEST.json 存在（每个 vendored 技能都要登记）")

    for d in skill_dirs:
        dp = os.path.join(root, d)
        audit_path = os.path.join(dp, "_audit.json")
        check(os.path.isfile(audit_path),
              "技能 %s 带 _audit.json（未审查进仓 = 红）" % d)
        if not os.path.isfile(audit_path):
            continue
        try:
            with open(audit_path, encoding="utf-8") as f:
                ad = json.load(f)
        except Exception as e:
            check(False, "%s 的 _audit.json 能解析：%s" % (d, e))
            continue
        check(ad.get("verdict") == "clean",
              "%s 审查结论为 clean（needs_review 不许进仓）" % d)
        check(ad.get("name") == d,
              "%s 的审计 name 与目录名一致" % d)
        check(d in manifest_names,
              "%s 在 MANIFEST.json 中登记" % d)

    for mn in manifest_names:
        check(os.path.isdir(os.path.join(root, mn)),
              "MANIFEST 登记的 %s 目录实际存在" % mn)

    if FAIL:
        print("\nP4 FAIL (%d 项)" % len(FAIL))
        sys.exit(1)
    print("\nP4 OK（%d 个 vendored 技能全部 clean + 已登记）" % len(skill_dirs))
    sys.exit(0)


if __name__ == "__main__":
    main()

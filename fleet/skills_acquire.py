"""技能 vendoring 落盘器（带静态审查，fail-closed）。

为什么另写这个而不是 `cp -r`：
  从开源市场搬技能进来 = 把别人代码放进仓库。本仓规矩（红线 + ClawHavoc 教训）：
  ① 进仓前必须过 skills_audit 静态审查；
  ② 结论 needs_review 一律不自动落盘（除非显式 --force 且人工确认）；
  ③ 每个技能随附 _audit.json，并在 MANIFEST.json 登记来源/license/审查结论/日期——
     以后审计「这个技能哪来的、安不安全」一眼可查，不许静默混入。

不自动联网拉 ClawHub：避免盲拉带脚本的未知源。联网拉作为后续能力，
且必须显式带 --insecure-ok 才允许从 URL 拉（本期不实现 URL 拉取）。
"""
import datetime
import json
import os
import shutil
import sys

from . import skills as SK
from . import skills_audit as SA


MANIFEST = os.path.join(SK.REPO_SKILLS_DIR, "MANIFEST.json")


def _read_manifest():
    if os.path.isfile(MANIFEST):
        try:
            with open(MANIFEST, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"version": 1, "entries": []}


def _write_manifest(data):
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def acquire(src_dir, source_label, force=False):
    """把一个本地技能目录 vendoring 进仓库 skills/。

    返回 (ok: bool, report: dict)。ok=False 表示被拒绝（未落盘）。
    """
    if not os.path.isdir(src_dir):
        return False, {"error": "源目录不存在：%s" % src_dir}
    audit = SA.audit_skill(src_dir)
    if audit["verdict"] == "error":
        return False, {"error": "审计失败", "audit": audit}
    if audit["verdict"] != "clean" and not force:
        return False, {
            "error": "审查结论为 needs_review，拒绝自动落盘（用 --force 显式确认后仍可落盘，但需在 MANIFEST 标注并人工复核）",
            "audit": audit}

    name = audit["name"]
    dest = os.path.join(SK.REPO_SKILLS_DIR, name)
    if os.path.exists(dest):
        return False, {"error": "目标已存在（不肯覆盖，避免静默覆盖已登记技能）：%s" % dest}

    # 复制（清掉 __pycache__ 等构建残留）
    def _ignore(path, names):
        return {n for n in names if n in ("__pycache__", ".git", "node_modules")}

    shutil.copytree(src_dir, dest, ignore=_ignore)

    # 随附审计
    with open(os.path.join(dest, "_audit.json"), "w", encoding="utf-8") as f:
        json.dump(audit, f, ensure_ascii=False, indent=2)

    # 登记
    manifest = _read_manifest()
    manifest["entries"].append({
        "name": name,
        "source": source_label,
        "license": audit.get("license") or "UNKNOWN",
        "verdict": audit["verdict"],
        "acquired_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "dir": os.path.relpath(dest, os.path.dirname(SK.REPO_SKILLS_DIR)),
    })
    _write_manifest(manifest)
    return True, {"name": name, "dest": dest, "audit": audit,
                  "manifest_entry": manifest["entries"][-1]}


def _main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("用法：python -m fleet.skills_acquire <src_dir> --source <label> [--force]")
        sys.exit(2)
    src = None
    source = "manual"
    force = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--force":
            force = True
        elif a == "--source":
            i += 1
            source = args[i]
        elif a.startswith("--source="):
            source = a.split("=", 1)[1]
        elif src is None:
            src = a
        i += 1
    ok, rep = acquire(src, source or "manual", force=force)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main()

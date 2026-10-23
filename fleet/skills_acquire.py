"""技能 vendoring 落盘器（带静态审查，fail-closed）。

为什么另写这个而不是 `cp -r`：
  从开源市场搬技能进来 = 把别人代码放进仓库。本仓规矩（红线 + ClawHavoc 教训）：
  ① 进仓前必须过 skills_audit 静态审查；
  ② 结论 needs_review 一律不自动落盘（除非显式 --force 且人工确认）；
  ③ 每个技能随附 _audit.json，并在 MANIFEST.json 登记来源/license/审查结论/日期——
     以后审计「这个技能哪来的、安不安全」一眼可查，不许静默混入。

关于联网拉（ClawHub 等）：
  🔴 **绝不猜第三方市场的 API 路径**（记忆铁律：ClawHub/角色卡站都是 SPA，curl 猜路径必错）。
  所以这里只做**通用的 URL / git 拉取器**：用户给一个具体的归档（.zip/.tar.gz）
  或 git 仓库 URL，我们下载/克隆到临时目录，**再走和本地完全相同的审计→登记→落盘闭环**。
  URL 拉取**默认关闭**，必须显式 `--insecure-ok`（盲拉带脚本的未知源 = ClawHavoc 风险，
  开之前你自己确认来源可信）。ClawHub 的具体技能 URL 由调用方提供，本模块不内置任何站点专属 API。
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

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


def acquire(src_dir, source_label, force=False, allow_assets=False):
    """把一个本地技能目录 vendoring 进仓库 skills/。

    返回 (ok: bool, report: dict)。ok=False 表示被拒绝（未落盘）。

    `allow_assets=True` 时，图片/字体/媒体这类资产文件不再算审查失败，
    但会登记进 MANIFEST 的 assets 字段 —— **放行要留痕**。
    """
    if not os.path.isdir(src_dir):
        return False, {"error": "源目录不存在：%s" % src_dir}
    audit = SA.audit_skill(src_dir, allow_assets=allow_assets)
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
        "assets": audit.get("assets") or [],
    })
    _write_manifest(manifest)
    return True, {"name": name, "dest": dest, "audit": audit,
                  "manifest_entry": manifest["entries"][-1]}


def _locate_skill_root(tmp):
    """从下载/克隆目录里找含 SKILL.md 的根：优先 tmp 直接含，否则第一个含的子目录。"""
    if os.path.isfile(os.path.join(tmp, "SKILL.md")):
        return tmp
    for root, _dirs, files in os.walk(tmp):
        if "SKILL.md" in files:
            return root
    return tmp


def _download_archive(url, insecure_ok):
    """下载 .zip / .tar.gz 归档到临时目录并解压，返回 (skill_root, error)。"""
    if not insecure_ok:
        return None, {"error": "URL 拉取默认关闭，须显式 --insecure-ok（盲拉带脚本未知源 = ClawHavoc 风险）"}
    tmp = tempfile.mkdtemp(prefix="skill_dl_")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "device-fleet-skills/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        arc = os.path.join(tmp, "dl")
        with open(arc, "wb") as f:
            f.write(data)
    except Exception as e:  # noqa: BLE001 — 网络错误如实回传，不静默
        shutil.rmtree(tmp, ignore_errors=True)
        return None, {"error": "下载失败：%s" % e}
    try:
        if url.endswith(".zip"):
            with zipfile.ZipFile(arc) as z:
                z.extractall(tmp)
        else:
            with tarfile.open(arc) as t:
                t.extractall(tmp)
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        return None, {"error": "解压失败：%s" % e}
    return _locate_skill_root(tmp), {}


def _clone_repo(url, insecure_ok):
    """git clone --depth 1 到临时目录，返回 (skill_root, error)。"""
    if not insecure_ok:
        return None, {"error": "URL 拉取默认关闭，须显式 --insecure-ok（盲拉带脚本未知源 = ClawHavoc 风险）"}
    tmp = tempfile.mkdtemp(prefix="skill_git_")
    try:
        subprocess.run(["git", "clone", "--depth", "1", url, tmp],
                       check=True, capture_output=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        return None, {"error": "git clone 失败：%s" % e}
    return _locate_skill_root(tmp), {}


def acquire_from_url(url, source_label, force=False, insecure_ok=False,
                     allow_assets=False):
    """从 URL 拉取技能并 vendoring 进仓。

    - url 为 .zip/.tar.gz 归档 → 下载解压；
    - url 为 git 仓库（.git 或 github.com 仓库根）→ clone；
    - 其余类型拒绝。
    拉取默认关闭，须 insecure_ok=True（--insecure-ok）。拉到后走 acquire 的审计→登记→落盘。
    返回 (ok, report)。
    """
    if url.endswith((".zip", ".tar.gz", ".tgz")):
        root, err = _download_archive(url, insecure_ok)
    elif url.endswith(".git") or ("github.com" in url and "/archive/" not in url
                                  and not url.endswith((".zip", ".tar.gz", ".tgz"))):
        root, err = _clone_repo(url, insecure_ok)
    else:
        return False, {"error": "不支持的 URL 类型（仅支持 .zip/.tar.gz 归档或 git 仓库 URL）"}
    if root is None:
        return False, err
    # 拉到的内容交给与本地完全相同的闭环
    ok, rep = acquire(root, source_label or url, force=force,
                      allow_assets=allow_assets)
    if not ok:
        shutil.rmtree(root, ignore_errors=True)
    return ok, rep


def _main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("用法：")
        print("  本地：python -m fleet.skills_acquire <src_dir> --source <label> "
              "[--force] [--allow-assets]")
        print("  URL：python -m fleet.skills_acquire --url <url> --source <label> "
              "[--force] [--allow-assets] --insecure-ok")
        print("  --allow-assets：图片/字体/媒体等资产文件显式放行（会登记进 MANIFEST 的 assets）")
        sys.exit(2)
    src = None
    url = None
    source = "manual"
    force = False
    insecure_ok = False
    allow_assets = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--force":
            force = True
        elif a == "--insecure-ok":
            insecure_ok = True
        elif a == "--allow-assets":
            allow_assets = True
        elif a == "--source":
            i += 1
            source = args[i]
        elif a.startswith("--source="):
            source = a.split("=", 1)[1]
        elif a == "--url":
            i += 1
            url = args[i]
        elif a.startswith("--url="):
            url = a.split("=", 1)[1]
        elif src is None and url is None:
            src = a
        i += 1
    if url:
        ok, rep = acquire_from_url(url, source or url, force=force,
                                   insecure_ok=insecure_ok, allow_assets=allow_assets)
    else:
        ok, rep = acquire(src, source or "manual", force=force,
                          allow_assets=allow_assets)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main()

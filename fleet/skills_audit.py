"""技能安全审查器（fail-closed）。

为什么需要它：
  本仓现在能从开源市场（ClawHub 等）vendoring 技能进来。
  ClawHub 的技能是「可执行的 Markdown + 脚本」，2026-02 有过 ClawHavoc
  恶意技能事件（341 个，Atomic Stealer，下架 2419 个）。
  ⇒ 任何技能进仓前必须过这道静态审查；结论非 clean 一律不自动放行。

结论二分（fail-closed）：
  · clean        —— 只有文档文件（.md/.json/.txt/.yaml…），且内容没命中危险模式
  · needs_review —— 含可执行脚本、含非文档文件，或文档里命中危险模式
                   （eval/exec/外网请求/下载执行/密钥路径等）。
                   这种不自动放行，要人工确认后才登记。
"""
import os
import re
import sys

from . import skills as SK


# 可执行脚本扩展名（出现即标需审查，因为能真的跑）
SCRIPT_EXTS = {".py", ".js", ".mjs", ".cjs", ".sh", ".bat", ".ps1",
               ".cmd", ".exe", ".dll", ".so", ".pyc", ".pyd"}
# 允许作为纯文档存在的扩展名
DOC_EXTS = {".md", ".markdown", ".txt", ".json", ".jsonl", ".yaml",
            ".yml", ".toml", ".rst", ".cfg", ".ini", ".license", ".LICENSE"}

# (label, pattern) —— 命中即 needs_review。宁可错杀，不可放过。
DANGER_PATTERNS = [
    ("dynamic_exec", re.compile(r"\b(?:eval|exec)\s*\(")),
    ("subprocess", re.compile(r"\b(?:subprocess|os\.system|os\.popen|popen|spawn)\b")),
    ("node_child_process", re.compile(r"child_process|require\(['\"]child_process")),
    ("http_request", re.compile(r"\b(?:requests\.(?:get|post)|urllib|fetch\s*\(|https?://)")),
    ("download_exec", re.compile(r"\b(?:curl|wget|Invoke-WebRequest|invoke-restmethod)\b", re.I)),
    ("powershell", re.compile(r"\bpowershell\b", re.I)),
    ("dynamic_import", re.compile(r"\b(?:__import__|importlib|load_source|exec\s+open)")),
    ("secret_path", re.compile(r"(?:\.ssh|id_rsa|\btoken\b|\bcredential\b|\bpassword\b|local\.env|\bsecret\b|api[_-]?key)", re.I)),
]


def _ext(path):
    return os.path.splitext(path)[1].lower()


def audit_skill(skill_dir):
    """审查一个技能目录，返回报告 dict。"""
    if not os.path.isdir(skill_dir):
        return {"name": os.path.basename(skill_dir), "dir": skill_dir,
                "verdict": "error", "findings": ["目录不存在"], "scanned": []}

    name = os.path.basename(os.path.abspath(skill_dir))
    findings = []
    scanned = []
    license_ = ""

    # 1) SKILL.md 必须有，且 frontmatter 有 name / 能读 license
    skill_md = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(skill_md):
        findings.append("缺少 SKILL.md（不符合技能格式）")
    else:
        try:
            with open(skill_md, "r", encoding="utf-8") as f:
                text = f.read()
            meta, _ = SK.parse_frontmatter(text, where=skill_md)
            if not (meta.get("name") or "").strip():
                findings.append("SKILL.md frontmatter 缺 name")
            license_ = (meta.get("license") or "").strip()
        except Exception as e:
            findings.append("读 SKILL.md 失败：%s: %s" % (type(e).__name__, e))

    # 2) 遍历所有文件
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "node_modules")]
        for fn in files:
            fp = os.path.join(root, fn)
            ext = _ext(fp)
            rel = os.path.relpath(fp, skill_dir)
            scanned.append(rel)
            if ext in SCRIPT_EXTS:
                findings.append("含可执行脚本：%s" % rel)
                continue
            if ext not in DOC_EXTS:
                findings.append("含非文档文件（保守标需审查）：%s" % rel)
                continue
            # 文档文件：扫内容危险模式
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                continue
            for label, pat in DANGER_PATTERNS:
                hit = pat.search(content)
                if hit:
                    snippet = content[max(0, hit.start() - 24):hit.start() + 28]
                    snippet = snippet.replace("\n", " ").replace("\r", " ")
                    findings.append("[%s] %s: …%s…" % (label, rel, snippet))
                    break  # 一个文件一种模式只报一次

    verdict = "clean" if not findings else "needs_review"
    return {"name": name, "dir": skill_dir, "verdict": verdict,
            "license": license_, "findings": findings, "scanned": scanned}


def audit_repo(root=None):
    """扫整个仓库 skills 目录，返回每个子目录的报告列表。"""
    if root is None:
        root = SK.REPO_SKILLS_DIR
    if not os.path.isdir(root):
        return []
    out = []
    for fn in sorted(os.listdir(root)):
        d = os.path.join(root, fn)
        if os.path.isdir(d):
            out.append(audit_skill(d))
    return out


def _main():
    rep = audit_repo()
    if not rep:
        print("仓库 skills/ 目录为空或不存在：%s" % SK.REPO_SKILLS_DIR)
        return
    bad = 0
    for r in rep:
        flag = "CLEAN " if r["verdict"] == "clean" else "REVIEW"
        if r["verdict"] != "clean":
            bad += 1
        print("[%s] %-30s license=%-12s files=%d" % (
            flag, r["name"], r.get("license") or "-", len(r["scanned"])))
        for f in r["findings"]:
            print("        - %s" % f)
    print("\n共 %d 个技能，%d 个需人工审查" % (len(rep), bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    _main()

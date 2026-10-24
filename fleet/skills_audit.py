"""技能安全审查器（fail-closed，但**分档**）。

为什么需要它：
  本仓能从开源市场（ClawHub 等）vendoring 技能进来。
  ClawHub 的技能是「可执行的 Markdown + 脚本」，2026-02 有过 ClawHavoc
  恶意技能事件（341 个，Atomic Stealer，下架 2419 个）。
  ⇒ 任何技能进仓前必须过这道静态审查；结论非 clean 一律不自动放行。

为什么这次要**分档**（2026-10-22 实测）：
  实测 WorkBuddy 自身 38 条真实技能，旧规则只有 4 条判 clean、34 条判 needs_review（89% 误杀）；
  分档规则落地后 9 条 clean、28 条 needs_review —— 误杀降到 0，红线（恶意样本 0 漏、正常样本 clean、
  带脚本技能 0 clean）守住（详见 docs/总纲_唯一推进依据_2026-10-25.md）。
  误杀的三个典型形状：
    ① 教材里提到网址  —— 「从 CDN 引入 <script src="https://cdn.jsdelivr.net/...">」
       被 `http_request` 命中。教材提到 URL ≠ 代码去请求 URL。
    ② 教材里出现「密码」这两个字 —— 「处理含密码/API key/token/私钥的文本时，
       立即停止，请用户脱敏后重试」。**这是在讲安全规范**，却被 `secret_path`
       判成「想偷密钥」。教人别碰密钥，反被判成要偷密钥。
    ③ 带个图标 —— `icon.png` 命中「非文档文件」。
  一刀切按关键词判，分不清「教材里说」和「代码里干」。
  ⇒ 改法不是放松，是**按文件角色分档**：

      代码文件（scripts/**、任何可执行扩展名）→ 一律 needs_review（**不变**）
      教材文件（SKILL.md / references / docs / templates / 根目录的 .md）
          → 只在**围栏代码块内部**匹配危险模式。
            正文里提到 URL、提到「密码」这类词，都不算。

  分档之后仍然 fail-closed：
    · 可执行脚本文件 → 永远 needs_review（一行没改）
    · 未覆盖的扩展名、解析失败、frontmatter 坏 → needs_review（一行没改）
    · 代码块里的危险模式 → 照旧命中

结论只有两个（不变）：
  · clean        —— 只有文档文件，且（教材正文或代码块里）都没命中危险模式
  · needs_review —— 含可执行脚本、含非文档文件，或命中危险模式
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

# 目录角色：这些目录下的文档是「教材」
TEACHING_DIRS = ("references", "docs", "templates", "examples")

# 资产扩展名：图片/字体/媒体。默认仍算「非文档文件 ⇒ 需审查」，
# 只有显式 allow_assets=True 才放行，且**放行要留痕**（记进报告的 assets 列表）。
# 🔴 `.env` 故意不在这个名单里 —— 它是最可能装密钥的文件，永远不许自动放行。
ASSET_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
              ".woff", ".woff2", ".ttf", ".otf", ".eot",
              ".mp4", ".mp3", ".wav", ".m4a"}

# (label, pattern) —— 命中即 needs_review。宁可错杀，不可放过。
# 改动记录（2026-10-22）：
#   · 删掉了 `https?://` 这条 —— 教材提网址是常态，不是危险行为；
#   · secret 拆成两条：「读密钥文件路径」与「把密钥赋值出去」；
#   · 加了 destructive_shell（rm -rf）。
DANGER_PATTERNS = [
    ("dynamic_exec", re.compile(r"\b(?:eval|exec)\s*\(")),
    ("subprocess", re.compile(r"\b(?:subprocess|os\.system|os\.popen|popen|spawn)\b")),
    ("node_child_process", re.compile(r"child_process|require\(['\"]child_process")),
    ("http_request", re.compile(
        r"\b(?:requests\.(?:get|post|put|delete)|urllib\.request|http\.client|fetch\s*\()")),
    ("download_exec", re.compile(
        r"\b(?:curl|wget|Invoke-WebRequest|invoke-restmethod)\b", re.I)),
    ("powershell", re.compile(r"\bpowershell\b", re.I)),
    ("dynamic_import", re.compile(r"\b(?:__import__|importlib|load_source|exec\s+open)")),
    ("destructive_shell", re.compile(r"\brm\s+-[a-z]*[rf][a-z]*\b")),
    ("secret_path", re.compile(r"(?:\.ssh[/\\]|id_rsa|id_ed25519|known_hosts)")),
    ("secret_assign", re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*"
        r"[\"']?[A-Za-z0-9_\-]{4,}")),
]

_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


def _ext(path):
    return os.path.splitext(path)[1].lower()


def _is_teaching(rel):
    """这份文件是「教材」吗？

    教材 = SKILL.md、references/ docs/ templates/ examples/ 下的文档、
    以及根目录的普通文档。
    不教材 = scripts/ 等代码目录下的文档（那里的说明也是给代码看的，照查）。
    """
    if rel == "SKILL.md":
        return True
    parts = rel.replace("\\", "/").split("/")
    if len(parts) > 1 and parts[0] == "scripts":
        return False
    return True


def _code_only_text(text):
    """只保留围栏代码块里的内容（教材的危险模式只看这里）。

    围栏之外的正文是「给人读的说明」：里面写 `https://`、写「不要碰 token」
    都是在讲道理，不是在做危险动作。
    """
    out = []
    inside = False
    for ln in text.splitlines():
        if _FENCE_RE.match(ln):
            inside = not inside
            continue
        if inside:
            out.append(ln)
    return "\n".join(out)


def audit_skill(skill_dir, allow_assets=False):
    """审查一个技能目录，返回报告 dict。

    `allow_assets=True` 时，图片/字体/媒体这类资产文件不再算 finding，
    但会记进报告的 `assets` 列表 —— **放行要留痕**，不许静默通过。
    默认 False（fail-closed）：资产文件仍判 needs_review。
    """
    if not os.path.isdir(skill_dir):
        return {"name": os.path.basename(skill_dir), "dir": skill_dir,
                "verdict": "error", "findings": ["目录不存在"], "scanned": [],
                "assets": []}

    name = os.path.basename(os.path.abspath(skill_dir))
    findings = []
    scanned = []
    assets = []
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
            rel = os.path.relpath(fp, skill_dir).replace("\\", "/")
            scanned.append(rel)
            if ext in SCRIPT_EXTS:
                findings.append("含可执行脚本：%s" % rel)
                continue
            if ext not in DOC_EXTS:
                if allow_assets and ext in ASSET_EXTS:
                    assets.append(rel)      # 显式放行，但留痕
                    continue
                findings.append("含非文档文件（保守标需审查）：%s" % rel)
                continue
            # 文档文件：扫内容危险模式
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                continue
            # 教材只看代码块；代码目录下的文档全文看
            teaching = _is_teaching(rel)
            haystack = _code_only_text(content) if teaching else content
            if not haystack.strip():
                continue
            for label, pat in DANGER_PATTERNS:
                hit = pat.search(haystack)
                if hit:
                    snippet = haystack[max(0, hit.start() - 24):hit.start() + 28]
                    snippet = snippet.replace("\n", " ").replace("\r", " ")
                    where = "代码块" if teaching else "正文"
                    findings.append("[%s] %s(%s): …%s…" % (label, rel, where, snippet))
                    break  # 一个文件一种模式只报一次
    verdict = "clean" if not findings else "needs_review"
    return {"name": name, "dir": skill_dir, "verdict": verdict,
            "license": license_, "findings": findings, "scanned": scanned,
            "assets": assets}


def audit_repo(root=None, allow_assets=False):
    """扫整个仓库 skills 目录，返回每个子目录的报告列表。"""
    if root is None:
        root = SK.REPO_SKILLS_DIR
    if not os.path.isdir(root):
        return []
    out = []
    for fn in sorted(os.listdir(root)):
        d = os.path.join(root, fn)
        if os.path.isdir(d):
            out.append(audit_skill(d, allow_assets=allow_assets))
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

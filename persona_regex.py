"""正则脚本：卡作者用来改字面的那一层（SillyTavern `extensions.regex_scripts`）。

为什么需要（2026-11-03）：

很多卡不用改提示词，靠正则就把格式做出来了——去掉 `*动作*` 的星号、把
`[状态: 好感5]` 折叠成一行、给特定台词加引用块。天网原来完全不认这层：
卡里写好的 20 条正则被当成不存在的字段，正文就带着一堆原始标记进模型。
这不是「少个花活」，是**卡的实际表现和作者预期不一样**。

实现口径：
  - `findRegex` 认 `/pattern/flags` 也认裸 pattern；
  - `replaceString` 支持 `$1..$9` 与 `{{match}}`；
  - `placement` 按 ST 的位掩码：1 用户输入 / 2 AI 输出 / 3 斜杠命令 / 5 世界书 / 6 推理；
  - `markdownOnly` / `promptOnly` 两个方向开关照做（显示与提示词走不同规则）；
  - `trimStrings` 替换后清掉残留片段。

不做：replaceString 里的 EJS（`<% %>`）。那需要 JS 运行时，天网不引；遇到就
**显式报告跳过**，让调用方知道这条没生效——静默执行一半比不做更坏。
"""
import re

PLACEMENT_USER_INPUT = 1
PLACEMENT_AI_OUTPUT = 2
PLACEMENT_SLASH = 3
PLACEMENT_WORLD_INFO = 5
PLACEMENT_REASONING = 6


def _split_pattern(find):
    """`/abc/gi` → ('abc', 'gi')；裸 pattern → (原文, '')。"""
    raw = str(find or "")
    if len(raw) > 2 and raw.startswith("/"):
        # 从右往左找最后一个未被转义的 /
        i = len(raw) - 1
        while i > 0:
            if raw[i] == "/" and raw[i - 1] != "\\":
                return raw[1:i], raw[i + 1:]
            i -= 1
    return raw, ""


def _flags(flag_text):
    f = 0
    for ch in str(flag_text or ""):
        f |= {"i": re.I, "m": re.M, "s": re.S, "x": re.X}.get(ch.lower(), 0)
    return f


def normalize(raw):
    """把原始规则数组（ST 字段名）归一成内部结构。

    对外也用它：调用方直接给规则时走同一条解析路，不会因为少写一个字段就静默跳过。
    """
    out = []
    for i, r in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(r, dict):
            continue
        out.append({
            "index": i,
            "name": str(r.get("scriptName") or r.get("name") or "规则%d" % (i + 1)),
            "find": str(r.get("findRegex") or r.get("find") or ""),
            "replace": str(r.get("replaceString") or r.get("replace") or ""),
            "trim": [str(x) for x in (r.get("trimStrings") or r.get("trim") or [])],
            "placement": list(r.get("placement") or [PLACEMENT_AI_OUTPUT]),
            "disabled": bool(r.get("disabled", False)),
            "markdown_only": bool(r.get("markdownOnly", False)),
            "prompt_only": bool(r.get("promptOnly", False)),
            "run_on_edit": bool(r.get("runOnEdit", False)),
            "min_depth": r.get("minDepth"),
            "max_depth": r.get("maxDepth"),
        })
    return out


def rules_of(card_data):
    """取出卡里的正则规则（含 extensions.regex_scripts 与顶层同名数组两种写法）。"""
    data = card_data or {}
    # 容错：没剥壳的 V3 卡（{"spec":..., "data": {...}}）
    if isinstance(data, dict) and isinstance(data.get("data"), dict) \
            and ("spec" in data or "spec_version" in data):
        data = data["data"]
    ext = data.get("extensions") if isinstance(data.get("extensions"), dict) else {}
    raw = ext.get("regex_scripts") or data.get("regex_scripts") or []
    return normalize(raw)


def _expand(repl, match):
    """把 $1 / {{match}} 展开。

    ⚠️ Python 的 `Match.expand` 认 `\\1` 和 `\\g<1>`，**不认 `$1`**（那是 JS 写法）。
    卡里的规则全是 JS 口径，所以先把 `$n` 翻成 `\\g<n>` 再交给 expand；
    反斜杠要先自身转义，否则作者写的 `\\n` 会被当成模板转义吃掉。
    """
    if not repl:
        return ""
    py = repl.replace("\\", "\\\\")
    py = re.sub(r"\$(\d)", r"\\g<\1>", py)
    try:
        out = match.expand(py)
    except (re.error, IndexError):
        # 作者写了不存在的组号：原样返回替换串，别把整条规则变成异常
        out = repl
    return out.replace("{{match}}", match.group(0))


def apply(text, card_data=None, placement=PLACEMENT_AI_OUTPUT, for_prompt=True, rules=None):
    """按 placement 依次跑规则，返回 (结果文本, 报告)。

    报告里每条规则是什么结局都写清楚：applied / skipped(prompt_only) /
    skipped(markdown_only) / skipped(ejs) / disabled / error。可追溯比「悄悄不动」重要。
    """
    text = str(text or "")
    if not text:
        return text, {"applied": [], "skipped": [], "errors": []}
    rules = rules if rules is not None else rules_of(card_data)
    applied, skipped, errors = [], [], []
    for r in rules:
        if r["disabled"]:
            skipped.append({"name": r["name"], "why": "disabled"})
            continue
        if placement not in r["placement"]:
            continue
        if for_prompt and r["markdown_only"]:
            skipped.append({"name": r["name"], "why": "markdown_only（只改显示）"})
            continue
        if (not for_prompt) and r["prompt_only"]:
            skipped.append({"name": r["name"], "why": "prompt_only（只改提示词）"})
            continue
        if "<%" in r["replace"]:
            skipped.append({"name": r["name"], "why": "replace 里是 EJS，本层不执行 JS"})
            continue
        pat, flag_text = _split_pattern(r["find"])
        if not pat:
            skipped.append({"name": r["name"], "why": "find 为空"})
            continue
        try:
            rx = re.compile(pat, _flags(flag_text))
        except re.error as e:
            errors.append({"name": r["name"], "error": "正则不合法: %s" % e})
            continue
        new = rx.sub(lambda m: _expand(r["replace"], m), text)
        if new == text:
            continue
        for t in r["trim"]:
            if t:
                new = new.replace(t, "")
        applied.append({"name": r["name"], "removed": len(text) - len(new)})
        text = new
    return text, {"applied": applied, "skipped": skipped, "errors": errors}


def lint(card_data):
    """体检：坏正则、空规则、只在显示生效的规则都值得提醒。"""
    issues = []
    for r in rules_of(card_data):
        if r["disabled"]:
            continue
        pat, flag_text = _split_pattern(r["find"])
        if not pat:
            issues.append({"rule": r["name"], "level": "warn", "issue": "findRegex 为空"})
            continue
        try:
            re.compile(pat, _flags(flag_text))
        except re.error as e:
            issues.append({"rule": r["name"], "level": "error", "issue": "正则不合法: %s" % e})
        if "<%" in r["replace"]:
            issues.append({"rule": r["name"], "level": "info",
                           "issue": "replace 用了 EJS，天网只做纯文本替换（会跳过）"})
        if r["markdown_only"] and not r["prompt_only"]:
            issues.append({"rule": r["name"], "level": "info",
                           "issue": "仅改显示，不改提示词"})
    return {"rules": len(rules_of(card_data)), "issues": issues}

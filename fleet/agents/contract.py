"""五段式输出契约：子智能体必须交一份**结构固定**的答卷，父级才能机械消费。

为什么要有这个（学 DeepSeek Harness 的第 4 条机制）：

  自由文本的子智能体输出是**散文**。父级想用它的「证据」，只能整段读一遍再自己挑，
  于是：①挑错挑不到（父级没有稳定的取材位置）；②出了问题定位不到是哪一步出的问题
  （本仓实测：`ag.py pipe` 三角色流水线走通但**中间角色超时**，自由文本太长导致慢，
  慢在哪一段说不清）；③子智能体可以自由省略它没做的事（"大概都弄好了"）。

  Harness 的做法是把输出钉成五段、顺序固定、每段有明确语义：

    SUMMARY:    一段话，你做了什么、发生了什么
    CHANGES:    改了哪些文件，各一行说明；只读就写 "None."
    EVIDENCE:   path:line-range 引用 + 关键发现，每条一个要点
    RISKS:      可能出什么问题 / 父级该复查什么
    BLOCKERS:   什么挡住了；干净收尾就写 "None."

  父级**只机械消费 EVIDENCE 段**作为下一轮的工作集。

设计纪律（与本仓既有铁律一致）：

1. **不许兜底**。缺段 / 乱序 / 段为空 ⇒ 判失败并**说清缺哪段**，
   绝不「大概齐就算过」，也绝不替它补一段空的。
2. **不静默**。解析失败必须带原因，调用方能据此重试或换角色。
3. **纯 stdlib、零依赖**——本仓铁律「纯 CPU 文本处理一律本机做」。

「None.」是**合法的**（Harness 原文：只读任务 CHANGES 写 "None."），
但**空字符串不是**。二者要分得清清楚楚。
"""
import re

# 五段的规范顺序与标题。标题大小写不敏感（模型爱写 Summary / summary / SUMMARY）。
SECTIONS = ("SUMMARY", "CHANGES", "EVIDENCE", "RISKS", "BLOCKERS")

# 每一段的语义说明——既是给人看的，也是塞进 prompt 让模型照着填的。
SECTION_HINTS = {
    "SUMMARY": "一段话：你做了什么、发生了什么（不写过程细节）",
    "CHANGES": "改了哪些文件，每行一个「路径 — 一句话说明」；只读任务写 None.",
    "EVIDENCE": "证据：path:line-range 引用 + 关键发现，每条一个要点（父级只读这一段）",
    "RISKS": "可能出什么问题 / 父级该复查什么；没有就写 None.",
    "BLOCKERS": "什么挡住了你；干净收尾就写 None.",
}

# 段落标题的识别：允许 "SUMMARY:" / "## SUMMARY" / "**SUMMARY**:" / "Summary:" 等写法。
_HEAD = r"(?im)^\s{0,6}(?:#{1,6}\s*)?(?:\*\*|__|`)?\s*(%s)\s*(?:\*\*|__|`)?\s*[:：]?\s*"
_HEAD_RE = re.compile(_HEAD % "|".join(SECTIONS), re.M)

# 段内允许的「空」写法：模型爱写 "None." / "None" / "N/A" / "无" / "没有" / "无风险"。
# ⚠️ 这些是**合法的明确回答**（Harness 明文允许只读任务写 "None."），
# 与「这一段什么都没说」是**两回事**，必须分开判。
_NONE_TOKENS = {"none", "none.", "n/a", "na", "nil", "无", "没有", "无风险", "（无）", "(无)"}


class ContractError(Exception):
    """答卷不合契约。message 必须写清「哪一段、为什么」，不许含糊。"""


def _strip_markup(t):
    """去掉 markdown 强调/列表符号/编号，返回实质内容。"""
    t2 = re.sub(r"^[\s\-*>#]+", "", t or "", flags=re.M)
    return t2.strip("`*_ ").strip()


def is_blank(text):
    """这一段**什么都没说**（空串或纯空白）。

    注意与 is_none_marker 区分：「None.」不是空白——它是**明确的**「没有」。
    """
    t = (text or "").strip()
    if not t:
        return True
    return not _strip_markup(t)


def is_none_marker(text):
    """这一段写的是「None.」这类**合法的明确否定**。"""
    t2 = _strip_markup((text or "").strip())
    if not t2:
        return False
    return t2.lower() in _NONE_TOKENS


def parse(text):
    """把一份答卷解析成 {段名: 内容}。

    **不做校验**（校验交给 check()，因为解析与校验的失败原因要分开报）。
    找不到任何标题时抛 ContractError——那是「压根没按格式答」，最严重的一种。
    """
    if not (text or "").strip():
        raise ContractError("答卷是空的：子智能体没有产出任何内容")
    matches = list(_HEAD_RE.finditer(text))
    if not matches:
        raise ContractError(
            "答卷里找不到任何段落标题（需要 " + "/".join(SECTIONS) + " 五段）")
    out = {}
    for i, m in enumerate(matches):
        name = m.group(1).upper()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        # 同一段出现两次：后写的覆盖先写的，但要留痕（父级要知道有重复）
        if name in out:
            prev = out[name]
            out[name] = (prev + "\n" + body).strip()
            out.setdefault("__dup__", []).append(name)
        else:
            out[name] = body
    return out


def check(text):
    """校验一份答卷合不合契约。返回 (ok, 原因列表, 解析结果)。

    失败原因**逐条**列出（缺哪段、哪段是空的、哪段重复），不合并成一句含糊话。
    """
    reasons = []
    try:
        parsed = parse(text)
    except ContractError as e:
        return False, [str(e)], {}

    for name in SECTIONS:
        if name not in parsed:
            reasons.append(f"缺 {name} 段")
        elif is_blank(parsed.get(name)):
            reasons.append(f"{name} 段是空的（没写内容，也没写 None.）")
    for name in parsed.get("__dup__", []):
        reasons.append(f"{name} 段出现了多次（内容已合并，但重复说明输出乱了）")
    return (not reasons), reasons, parsed


def evidence(parsed):
    """取 EVIDENCE 段——父级唯一机械消费的那一段。

    传入 parse() 的结果或原始文本都行。取不到就返回空串（**不编造**）。
    """
    if isinstance(parsed, str):
        try:
            parsed = parse(parsed)
        except ContractError:
            return ""
    return (parsed.get("EVIDENCE") or "").strip()


def prompt_block():
    """给子智能体看的格式要求。拼进 system prompt 用。

    刻意写得**像合同条款**而不是「建议」——模型对「必须/不许」的执行率远高于「请尽量」。
    """
    lines = ["【输出格式 · 必须严格遵守】",
             "你的最终回答必须正好是五段，标题用全大写英文冒号，顺序不许变："]
    for name in SECTIONS:
        lines.append(f"{name}: {SECTION_HINTS[name]}")
    lines += [
        "",
        "硬要求：",
        "1. 五段一个都不能少，每段都要有内容；真的没有就写 None.。",
        "2. 不许在五段之外加别的段落、不许写开场白或结语。",
        "3. EVIDENCE 段最重要：父级只读这一段，你不给可核对的引用，父级无法验证你的结论。",
        "4. 拿不到证据时**如实写「未找到公开来源」或 None.**，绝不编造数字、日期、路径。",
    ]
    return "\n".join(lines)


def render(parsed):
    """把解析结果渲染回规范五段文本（父级存档/再交给下一轮时用）。"""
    if not isinstance(parsed, dict):
        raise ContractError("render() 只接受 parse() 的结果")
    out = []
    for name in SECTIONS:
        body = (parsed.get(name) or "").strip()
        out.append(f"{name}: {body if body else 'None.'}")
    return "\n".join(out)


def _selftest():
    ok_samples = [
        ("SUMMARY: 修好了崩溃\nCHANGES: a.py — 加了判空\nEVIDENCE: a.py:12-18 修了空指针\n"
         "RISKS: None.\nBLOCKERS: None.",
         ["SUMMARY", "CHANGES", "EVIDENCE", "RISKS", "BLOCKERS"]),
    ]
    for text, want in ok_samples:
        good, why, parsed = check(text)
        assert good, why
        for w in want:
            assert w in parsed, f"{w} 没解析出来"
        assert evidence(parsed).startswith("a.py:12-18"), evidence(parsed)

    # 缺段
    good, why, _ = check("SUMMARY: 只有一段")
    assert not good and any("缺" in x for x in why), why

    # 段是空的（不是 None.）
    good, why, _ = check("SUMMARY: a\nCHANGES:\nEVIDENCE: b\nRISKS: c\nBLOCKERS: d")
    assert not good and any("CHANGES" in x and "空" in x for x in why), why

    # None. 是合法的
    good, why, _ = check("SUMMARY: a\nCHANGES: None.\nEVIDENCE: b\nRISKS: None.\nBLOCKERS: None.")
    assert good, why

    # 压根没格式
    good, why, _ = check("我给你讲个故事……")
    assert not good and any("找不到" in x for x in why), why

    # 空答卷
    good, why, _ = check("")
    assert not good, why

    # 大小写与 markdown 变体
    good, why, _ = check("## summary\nx\n**CHANGES**:\ny\nEVIDENCE:\nz\nrisks:\nw\nblockers:\nv")
    assert good, why

    print("contract selftest OK")
    return True


if __name__ == "__main__":
    _selftest()

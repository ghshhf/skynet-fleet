"""世界书引擎：决定「这一轮到底该塞哪几条设定进上下文」。

天网原来的 match_world_book 已经能认 constant / selective / use_regex，但只是
「命中就全塞」：没有扫描深度、没有逻辑模式区分、没有预算上限。真拿它跑长卡会出事——
十几条设定一起注入，把上下文挤爆，反而把最近对话挤掉。

ElecKoi 移植过来的四点（2026-11-03）：

1. **scan_depth 四级回退**（shared/foundation/tavernWorldBook.ts）：
   `entry.extensions.scan_depth` → `entry.scanDepth` → `book.scan_depth` → 默认 2，最小 1。
   语义是「只看最近 N 条消息」，不是「整段对话一起匹配」——差别很大：
   一个词第 3 轮提过一次，不该让第 30 轮还在注入。

2. **selectiveLogic 四种模式**（不是只有 AND）：
   0 AND_ANY（默认，主命中 + 次任一）｜1 NOT_ALL（主命中 + 次未全中）
   2 NOT_ANY（主命中 + 次全不中）｜3 AND_ALL（主 + 次全中）
   官方卡里用 NOT_ANY 做「排除某条线」很常见，只实现 AND_ANY 等于把这类卡演错。

3. **整词匹配**（match_whole_words）：`art` 不该命中 `partial`。

4. **预算裁剪**：先放常驻（constant），再按 insertion_order 递增填，填不下就丢，
   并把丢掉的原因记下来——「为什么这条设定没进来」要能回答，不能悄悄吞掉。

不做：向量检索、跨卡世界书合并。那是另一层的事，这里只做「卡内世界书」。
"""
import math
import random
import re

DEFAULT_SCAN_DEPTH = 2

AND_ANY, NOT_ALL, NOT_ANY, AND_ALL = 0, 1, 2, 3
LOGIC_NAMES = {AND_ANY: "AND_ANY", NOT_ALL: "NOT_ALL", NOT_ANY: "NOT_ANY", AND_ALL: "AND_ALL"}


# --------------------------------------------------------------------------
# 条目解析
# --------------------------------------------------------------------------

def _as_list(v):
    if isinstance(v, list):
        return [str(x) for x in v if str(x or "")]
    if isinstance(v, str) and v:
        return [v]
    return []


def _unwrap(x):
    """容错：也接受没剥壳的 V3 卡（{"spec":..., "data": {...}}）。

    设备侧走 persona_card.load_card 拿到的是剥好的 data，但 agent 可能直接把
    一整份卡 JSON 贴进来；这时候不该报「没有世界书」，而该自己认出来。
    """
    if isinstance(x, dict) and isinstance(x.get("data"), dict) \
            and ("spec" in x or "spec_version" in x):
        return x["data"]
    return x if isinstance(x, dict) else {}


def entries_of(card_data):
    """把卡里的 character_book 摊平成统一结构（保留 extensions，别丢字段）。"""
    card_data = _unwrap(card_data)
    book = (card_data or {}).get("character_book") or {}
    raw = book.get("entries") if isinstance(book, dict) else None
    out = []
    for i, e in enumerate(raw or []):
        if not isinstance(e, dict):
            continue
        ext = e.get("extensions") if isinstance(e.get("extensions"), dict) else {}
        out.append({
            "index": i,
            "name": str(e.get("name") or e.get("comment") or "条目%d" % (i + 1)),
            "keys": _as_list(e.get("keys") or ext.get("keys")),
            "secondary_keys": _as_list(e.get("secondary_keys") or ext.get("secondary_keys")),
            "content": str(e.get("content") or ""),
            "enabled": e.get("enabled", True) is not False,
            "constant": bool(e.get("constant", False)),
            "selective": bool(e.get("selective", False)),
            "use_regex": bool(e.get("use_regex", False)),
            "case_sensitive": bool(e.get("case_sensitive", False)),
            "match_whole_words": bool(e.get("match_whole_words") or ext.get("match_whole_words")),
            "selective_logic": _logic(e, ext),
            "insertion_order": e.get("insertion_order") if isinstance(e.get("insertion_order"), (int, float)) else 100,
            "position": str(e.get("position") or "before_char"),
            "depth": e.get("depth"),
            "probability": _prob(e, ext),
            "group": str(e.get("group") or ext.get("group") or ""),
            "scan_depth": scan_depth_of(e, book),
            "ext": ext,
        })
    return out


def _logic(entry, ext):
    v = entry.get("selectiveLogic", ext.get("selectiveLogic"))
    if isinstance(v, str):
        names = {n: k for k, n in LOGIC_NAMES.items()}
        return names.get(v.upper(), AND_ANY)
    if isinstance(v, int) and v in LOGIC_NAMES:
        return v
    return AND_ANY


def _prob(entry, ext):
    v = entry.get("probability", ext.get("probability"))
    if not isinstance(v, (int, float)):
        return 100
    return max(0, min(int(v), 100))


def scan_depth_of(entry, book=None):
    """四级回退，非法值视为未声明（精确移植 tavernEntryScanDepth）。"""
    def num(v):
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

    ext = entry.get("extensions") if isinstance(entry.get("extensions"), dict) else {}
    depth = num(ext.get("scan_depth"))
    if depth is None:
        depth = num(entry.get("scanDepth"))
    if depth is None and isinstance(book, dict):
        depth = num(book.get("scan_depth"))
    if depth is None:
        depth = DEFAULT_SCAN_DEPTH
    return max(depth, 1)


# --------------------------------------------------------------------------
# 匹配
# --------------------------------------------------------------------------

def _word_re(word, case_sensitive):
    flags = 0 if case_sensitive else re.I
    return re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(word), flags)


def _hit(words, text, entry):
    for w in words:
        if not w:
            continue
        if entry["match_whole_words"] and not entry["use_regex"]:
            if _word_re(w, entry["case_sensitive"]).search(text):
                return True
        else:
            probe = w if entry["case_sensitive"] else w.lower()
            hay = text if entry["case_sensitive"] else text.lower()
            if probe in hay:
                return True
    return False


def match_entry(entry, text, rng=None):
    """这条设定在给定文本里该不该激活。返回 (bool, 原因)。"""
    if not entry["enabled"]:
        return False, "disabled"
    if not entry["content"].strip():
        return False, "empty"

    if entry["constant"] and not entry["use_regex"]:
        return True, "constant"

    if entry["use_regex"]:
        for pat in entry["keys"]:
            try:
                flags = 0 if entry["case_sensitive"] else re.I
                if re.search(pat, text, flags):
                    return True, "regex"
            except re.error:
                if pat in text:
                    return True, "regex(坏正则→当关键词)"
        return False, "no-key-hit"

    primary = _hit(entry["keys"], text, entry)
    if not primary:
        return False, "no-key-hit"
    if not entry["selective"]:
        return True, "key"

    secondary = entry["secondary_keys"]
    if not secondary:
        return True, "key(无次键)"
    hits = [w for w in secondary if _hit([w], text, entry)]
    logic = entry["selective_logic"]
    if logic == AND_ANY:
        ok = bool(hits)
    elif logic == AND_ALL:
        ok = len(hits) == len(secondary)
    elif logic == NOT_ANY:
        ok = not hits
    else:  # NOT_ALL
        ok = len(hits) < len(secondary)
    return ok, "%s(次键命中 %d/%d)" % (LOGIC_NAMES[logic], len(hits), len(secondary))


# --------------------------------------------------------------------------
# 预算
# --------------------------------------------------------------------------

def estimate_tokens(text):
    """粗估 token：英文按 4 字符、中文按 0.7 字，取上整。

    不引 tiktoken（零依赖、几十种模型各一套词表）；世界书的预算只需要「别塞爆」，
    不需要精确到个位。宁可估高一点，留安全余量。
    """
    t = str(text or "")
    cjk = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
    other = len(t) - cjk
    return max(1, int(math.ceil(cjk * 0.7 + other / 4.0)))


# --------------------------------------------------------------------------
# 选条目
# --------------------------------------------------------------------------

def _recent(messages, depth):
    """取最近 depth 条消息拼成匹配窗口。"""
    if isinstance(messages, str):
        return messages
    msgs = [str(m or "") for m in (messages or [])]
    if depth <= 0:
        return ""
    return "\n".join(msgs[-depth:])


def select(card_data, messages, budget_tokens=1500, per_entry_depth=True, rng=None):
    """算出这一轮要注入的世界书内容。

    返回：
      before / after    按 position 分好的文本块（直接拼进 system prompt 对应段）
      used              实际注入的条目（名字/顺序/预计 token/命中原因）
      dropped           没进的原因（disabled / 超预算 / 概率没过 / 同组被顶掉）
    """
    rng = rng or random
    entries = entries_of(card_data)
    window_cache = {}
    used, dropped = [], []

    for e in entries:
        depth = e["scan_depth"] if per_entry_depth else DEFAULT_SCAN_DEPTH
        if depth not in window_cache:
            window_cache[depth] = _recent(messages, depth)
        ok, reason = match_entry(e, window_cache[depth], rng)
        if not ok:
            dropped.append({"name": e["name"], "reason": reason})
            continue
        if e["probability"] < 100 and rng.randint(1, 100) > e["probability"]:
            dropped.append({"name": e["name"], "reason": "probability %d%%" % e["probability"]})
            continue
        used.append(dict(e, reason=reason))

    # 同组只留一条（权重大的优先），ST 的 group 语义
    by_group = {}
    for e in used:
        if not e["group"]:
            continue
        cur = by_group.get(e["group"])
        if cur is None or e["insertion_order"] < cur["insertion_order"]:
            if cur is not None:
                dropped.append({"name": cur["name"], "reason": "group %s 被顶掉" % e["group"]})
            by_group[e["group"]] = e
        else:
            dropped.append({"name": e["name"], "reason": "group %s 被顶掉" % e["group"]})
    used = [e for e in used if not e["group"] or by_group.get(e["group"]) is e]

    # 预算：常驻先占位，其余按 insertion_order 递增填
    used.sort(key=lambda e: (0 if e["constant"] else 1, e["insertion_order"], e["name"]))
    total = 0
    kept = []
    for e in used:
        cost = estimate_tokens(e["content"])
        if total + cost > budget_tokens:
            dropped.append({"name": e["name"], "reason": "超预算（需 %d，剩 %d）"
                            % (cost, max(0, budget_tokens - total))})
            continue
        total += cost
        kept.append(dict(e, tokens=cost))

    before = [e["content"] for e in kept if e["position"] != "after_char"]
    after = [e["content"] for e in kept if e["position"] == "after_char"]
    return {
        "before": before,
        "after": after,
        "used": [{"name": e["name"], "order": e["insertion_order"],
                  "position": e["position"], "tokens": e["tokens"],
                  "reason": e["reason"], "depth": e["scan_depth"]} for e in kept],
        "dropped": dropped,
        "tokens": total,
        "budget": budget_tokens,
    }


def lint(card_data):
    """给世界书做体检：作者最常犯的四种错。"""
    card_data = _unwrap(card_data)
    entries = entries_of(card_data)
    issues = []
    seen_keys = {}
    for e in entries:
        if not e["enabled"]:
            continue
        if not e["content"].strip():
            issues.append({"entry": e["name"], "level": "warn", "issue": "内容为空"})
        if not e["keys"] and not e["constant"] and not e["use_regex"]:
            issues.append({"entry": e["name"], "level": "warn",
                           "issue": "既没关键词也不是常驻 ⇒ 永远不会触发"})
        for k in e["keys"]:
            if len(k) <= 1:
                issues.append({"entry": e["name"], "level": "warn",
                               "issue": "关键词 %r 太短，容易误触发" % k})
            prev = seen_keys.get(k)
            if prev and prev != e["name"]:
                issues.append({"entry": e["name"], "level": "info",
                               "issue": "关键词 %r 与「%s」重复，会同时激活" % (k, prev)})
            seen_keys[k] = e["name"]
        if e["selective"] and not e["secondary_keys"]:
            issues.append({"entry": e["name"], "level": "info",
                           "issue": "开了 selective 但没填次键，等同普通触发"})
        if e["scan_depth"] > 20:
            issues.append({"entry": e["name"], "level": "info",
                           "issue": "scan_depth=%d 过大，老词会一直命中" % e["scan_depth"]})
    return {"entries": len(entries), "issues": issues}

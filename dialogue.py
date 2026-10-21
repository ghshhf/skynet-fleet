"""对话编排层：把「人设 + 记忆 + 流水」拼成一次完整的「你说→她回」。

这个模块只做三件事，不碰模型、不碰语音、不碰界面：

1. **拼上下文**（`build_context`）：按固定顺序注入
   身份锚(角色卡) → 世界观(卡内) → 记忆(往事 + 欠账) → 对话历史。
   顺序是刻意的：先定「她是谁」，再定「她在哪儿」，再给「她记得什么」，
   最后给「刚刚聊了什么」——反过来模型会拿记忆去覆盖人设。
2. **跑一轮对话**（`reply`）：拼提示词 → 调 `llm.complete` → **清洗输出** → 落流水 → 可选出声。
3. **降级不崩**（`degraded`）：任何一环缺能力都不抛异常，只在 `degraded` 里记一笔，
   让人知道「这次她是在没有记忆/没有语音/没有模型的条件下答的」。

两个硬性约定（都来自实测踩坑，注释里写了原因）：

- **被动应答不过 `ProactiveEngine.allow()`**：频控/静默是给「主动开口」的刹车，
  拿它拦用户提问，等于「你问我我不理」——那是本项目最容易写错的一处。
- **输出必须清洗**：GGUF 里还留着官方 Qwen3 的 `enable_thinking` 条件模板，
  推理端每条回答开头会泄漏 `</think>`；修完标签后模型还爱用 ``` 围栏把自己包起来。
  详见 `clean_reply()`。

纯 stdlib，import 时不建窗口、不读真库。
"""
import os
import re
import sys
import time

# ---- 可选依赖：缺了不能崩，只降级 ----
try:
    import llm
except Exception:                    # pragma: no cover - 推理适配层缺失
    llm = None

try:
    import journal as journal_mod
except Exception:                    # pragma: no cover
    journal_mod = None

try:
    import recall
except Exception:                    # pragma: no cover
    recall = None

try:
    import followups
except Exception:                    # pragma: no cover
    followups = None

try:
    import persona
except Exception:                    # pragma: no cover
    persona = None

try:
    import persona_card
except Exception:                    # pragma: no cover - 角色卡模块由同事并行实现
    persona_card = None

try:
    import voice
except Exception:                    # pragma: no cover - 语音模块由同事并行实现
    voice = None


# ---- 降级标记（统一前缀，方便上层筛） ----
D_LLM_PLACEHOLDER = "llm:placeholder"
D_LLM_MISSING = "llm:missing"
D_LLM_ERROR = "llm:error"
D_JOURNAL_MISSING = "journal:missing"
D_JOURNAL_ERROR = "journal:error"
D_CTX_MISSING = "ctx:missing"
D_ST_MISSING = "st:missing"
D_CARD_MISSING = "persona_card:missing"
D_CARD_ERROR = "persona_card:error"
D_RECALL_MISSING = "recall:missing"
D_FOLLOWUPS_MISSING = "followups:missing"
D_VOICE_MISSING = "voice:missing"
D_VOICE_ERROR = "voice:error"

# 剥完是空句时的兜底文案（要像人话，不要像日志）
FALLBACK_SAY = "（她没接上话）主人，我在。"

# 记忆注入的硬约束：只许引用，不许编造
MEMORY_CONSTRAINT = "只能引用，不要编造"


def _strip_codefence_enabled():
    """围栏剥离开关：FLEET_STRIP_CODEFENCE=0 时保留原文（人工核对用）。"""
    return os.environ.get("FLEET_STRIP_CODEFENCE", "1").strip() not in ("0", "false", "False")


# ----------------------------------------------------------------------
# 输出清洗
# ----------------------------------------------------------------------

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_PAIR = re.compile(r"```[^\n`]*\n(.*?)```", re.S)
_FENCE_LINE = re.compile(r"(?m)^[ \t]*```[^\n]*\n?")


def _strip_codefence(text):
    """剥掉 markdown 代码围栏，**保留围栏里的正文**。

    实测原文形如 ``'\\n```\\n你要是能帮我的话，我骄傲的主人。'``：
    成对的 ``` 把正文包起来；孤立的 ``` 只在开头或结尾出现。
    """
    if "```" not in text:
        return text
    pairs = _FENCE_PAIR.findall(text)
    if pairs:
        body = "\n".join(p.strip("\n") for p in pairs).strip()
        rest = _FENCE_LINE.sub("", _FENCE_PAIR.sub("", text)).strip()
        if rest:
            body = (rest + "\n" + body).strip() if body else rest
        return body
    # 只有孤立围栏：直接去掉围栏行
    return _FENCE_LINE.sub("", text)


def clean_reply(text, fallback=FALLBACK_SAY):
    """把模型原始输出洗成人能直接看/能直接念的句子。

    三步固定顺序：
      ① 去掉 `<think>` / `</think>` 残留（成对块整体丢弃，孤立标签摘掉）及其后的空行；
      ② 剥 markdown 代码围栏（保留围栏内正文）；
      ③ 剥完若为空，回退成一句普通应答文案（绝不返回空串）。
    """
    s = "" if text is None else str(text)
    # ① think 残留
    s = _THINK_BLOCK.sub("", s)
    s = re.sub(r"(?is)\A[\s]*</?think>", "", s)
    s = s.replace("<think>", "").replace("</think>", "")
    s = s.strip().lstrip("\r\n").strip()
    # ② 代码围栏
    if _strip_codefence_enabled():
        s = _strip_codefence(s)
    s = s.strip()
    # ③ 空则回退
    if not s:
        s = fallback
    return s


# ----------------------------------------------------------------------
# 上下文拼装
# ----------------------------------------------------------------------

def _card_anchor(card_id=None, degraded=None):
    """身份锚 + 世界观。角色卡模块缺失时退回人格态里的 name/traits/catchphrase。"""
    degraded = degraded if degraded is not None else []
    system = ""
    meta = {}
    cid = card_id
    if persona_card is not None:
        try:
            if not cid:
                cid = persona_card.current()
        except Exception:
            cid = card_id
        if cid:
            try:
                compiled = persona_card.compile_card(cid) or {}
                system = compiled.get("system", "") or ""
                meta = compiled.get("meta", {}) or {}
            except Exception:
                degraded.append(D_CARD_ERROR)
            if not system:
                try:
                    system = persona_card.build_system(cid) or ""
                except Exception:
                    degraded.append(D_CARD_ERROR)
    else:
        degraded.append(D_CARD_MISSING)

    if not system:
        # 没有角色卡：用人格态拼一个最小身份锚，保证 system 永远不空
        name, traits, catch = "她", [], ""
        if persona is not None:
            try:
                snap = persona.DEFAULT_PERSONA
                name = snap.get("name", "她")
                traits = list(snap.get("traits", []))
                catch = snap.get("catchphrase", "")
            except Exception:
                pass
        system = ("[身份]\n你是「%s」。%s%s" % (
            name,
            ("性格：" + "、".join(traits) + "。") if traits else "",
            ("常用口癖：「%s」。" % catch) if catch else "",
        ))
    return cid, system, meta


def _worldview(meta):
    """世界观只取卡里的显式字段，不做语义总结（总结就是臆测）。"""
    if not isinstance(meta, dict):
        return ""
    for k in ("world", "worldview", "setting", "scene"):
        v = meta.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _memory_block(ctx, st, journal, now, recall_k, followup_k, degraded):
    """往事 + 欠账，都附原文与「只能引用，不要编造」约束。"""
    recall_items = []
    follow_items = []

    if ctx is not None and st is not None:
        if recall is not None:
            try:
                recall_items = recall.pick(ctx, st, now=now, k=recall_k) or []
            except Exception:
                degraded.append(D_RECALL_MISSING)
        else:
            degraded.append(D_RECALL_MISSING)
    elif ctx is None:
        degraded.append(D_CTX_MISSING)
    elif st is None:
        degraded.append(D_ST_MISSING)

    if journal is not None and st is not None:
        if followups is not None:
            try:
                follow_items = followups.pick(journal, st, now=now, k=followup_k) or []
            except Exception:
                degraded.append(D_FOLLOWUPS_MISSING)
        else:
            degraded.append(D_FOLLOWUPS_MISSING)

    parts = []
    if recall_items and recall is not None:
        hint = (recall.prompt_hint(recall_items) or "").rstrip()
        if hint:
            parts.append(hint)
    if follow_items and followups is not None:
        hint = (followups.prompt_hint(follow_items) or "").rstrip()
        if hint:
            parts.append(hint)

    evidence = {"recall": recall_items, "followups": follow_items, "journal_ids": []}
    if not parts:
        return "", evidence

    head = ("[记忆]\n" + MEMORY_CONSTRAINT +
            "：下面每条都是原文，可以照原样提起，不许添油加醋，" +
            "也不许提起没发生过的往事。")
    return head + "\n" + "\n\n".join(parts), evidence


def _history_block(history):
    """对话历史渲染成人话两行一组。"""
    if not history:
        return ""
    lines = ["[对话历史]"]
    for h in history:
        who = "你" if h.get("role") == "user" else "她"
        lines.append("%s：%s" % (who, h.get("content", "")))
    return "\n".join(lines)


def build_context(card_id=None, journal=None, ctx=None, st=None, now=None,
                  turns=8, recall_k=3, followup_k=2):
    """拼一次对话的上下文。**任何一项缺失都不抛异常**，缺的留空即可。

    返回：
        {"system": str,
         "history": [{"role": "user"|"assistant", "content": str}, ...],
         "evidence": {"recall": [...], "followups": [...], "journal_ids": [...]},
         "degraded": [str, ...]}
    """
    degraded = []
    now = time.time() if now is None else now
    turns = max(0, int(turns or 0))

    # 1) 身份锚  2) 世界观
    cid, card_system, meta = _card_anchor(card_id, degraded)
    world = _worldview(meta)

    # 3) 记忆
    mem_text, evidence = _memory_block(ctx, st, journal, now, recall_k, followup_k, degraded)

    # 4) 对话历史（来自流水，时间正序）
    history = []
    if journal is None:
        degraded.append(D_JOURNAL_MISSING)
    else:
        try:
            rows = journal.recent(hours=24, now=now, kinds=("user", "her"), limit=turns * 2)
            for r in rows:
                role = "user" if r.get("kind") == "user" else "assistant"
                content = (r.get("body") or "").strip()
                if not content:
                    continue
                if history and history[-1]["role"] == role:
                    # 保持 user/assistant 严格交替：同角色相邻就并成一条
                    history[-1]["content"] = (history[-1]["content"] + "\n" + content).strip()
                else:
                    history.append({"role": role, "content": content})
                evidence["journal_ids"].append(r.get("id"))
        except Exception:
            degraded.append(D_JOURNAL_ERROR)

    blocks = [card_system.strip()]
    if world:
        blocks.append("[世界观]\n" + world)
    if mem_text:
        blocks.append(mem_text)
    hist_text = _history_block(history)
    if hist_text:
        blocks.append(hist_text)

    return {
        "system": "\n\n".join(b for b in blocks if b),
        "history": history,
        "evidence": evidence,
        "degraded": degraded,
        "card": cid,
    }


# ----------------------------------------------------------------------
# 一轮对话
# ----------------------------------------------------------------------

def reply(text, card_id=None, journal=None, ctx=None, st=None, speak=False, now=None):
    """你说一句，她回一句。

    返回：
        {"say": str, "backend": str, "card": card_id, "degraded": [str],
         "ms": int, "evidence": {...}}

    注意：这里**不会**调用 `persona.ProactiveEngine.allow()`——被动应答不受
    频控/静默约束，那是「主动开口」才用的刹车。
    """
    t0 = time.time()
    degraded = []
    text = (text or "").strip()

    context = build_context(card_id=card_id, journal=journal, ctx=ctx, st=st, now=now)
    degraded.extend(context.get("degraded", []))
    cid = context.get("card") or card_id

    # 提示词：优先用最后一句（历史已经在 system 里了，不重复喂）
    prompt = text
    if not prompt:
        for h in reversed(context.get("history", [])):
            if h.get("role") == "user":
                prompt = h.get("content", "")
                break
    if not prompt:
        prompt = "（沉默）"

    backend = "none"
    raw = ""
    if llm is None:
        degraded.append(D_LLM_MISSING)
        raw = FALLBACK_SAY
    else:
        try:
            raw, backend = llm.complete(prompt=prompt, system=context.get("system"))
        except Exception as e:
            degraded.append(D_LLM_ERROR)
            raw = "%s（%s）" % (FALLBACK_SAY, e)

    if backend == "placeholder":
        degraded.append(D_LLM_PLACEHOLDER)

    say = clean_reply(raw)

    # 落盘：先记你说的话，再记她回的话（顺序即时间序）
    if journal is None:
        if D_JOURNAL_MISSING not in degraded:
            degraded.append(D_JOURNAL_MISSING)
    else:
        try:
            journal.log("user", "user", text)
            journal.log("her", "her", say)
        except Exception:
            degraded.append(D_JOURNAL_ERROR)

    # 出声（可选）：按当前卡的音色念，切卡就换嗓子
    if speak:
        if voice is None:
            degraded.append(D_VOICE_MISSING)
        else:
            try:
                if hasattr(voice, "say_for_card"):
                    voice.say_for_card(say, card_id=cid)
                else:
                    voice.say(say)
            except Exception:
                degraded.append(D_VOICE_ERROR)

    return {
        "say": say,
        "backend": backend,
        "card": cid,
        "degraded": degraded,
        "ms": int((time.time() - t0) * 1000),
        "evidence": context.get("evidence", {}),
    }


# ----------------------------------------------------------------------
# 离线自测
# ----------------------------------------------------------------------

def _selftest():
    """用临时 sqlite 流水 + 假 ctx 验证：不崩、顺序对、落盘对、清洗对。"""
    import tempfile

    done = []

    def check(cond, msg):
        assert cond, msg
        done.append(msg)

    # --- 清洗规则 ---
    dirty = "</think>\n\n```\n你要是能帮我的话，我骄傲的主人。\n```"
    cleaned = clean_reply(dirty)
    check("<think>" not in cleaned and "</think>" not in cleaned, "think 标签已剥")
    check("```" not in cleaned, "代码围栏已剥")
    check("你要是能帮我的话" in cleaned, "围栏内正文保留")
    check(clean_reply("") == FALLBACK_SAY, "空输出回退成普通应答")
    check(clean_reply("</think>\n\n<think>x</think>\n\n在的。") == "在的。", "think 残留 + 空行已清")
    os.environ["FLEET_STRIP_CODEFENCE"] = "0"
    check("```" in clean_reply("```\n原文\n```"), "开关关闭时保留围栏")
    os.environ["FLEET_STRIP_CODEFENCE"] = "1"

    # --- 假流水（临时 sqlite）+ 假 ctx ---
    fd, dbpath = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(dbpath)
    fd, ps = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(ps)

    j = journal_mod.Journal(dbpath) if journal_mod else None
    st = persona.PersonaState(ps) if persona else None

    class FakeCtx:
        """假 L3：只提供 recall 需要的 items()。"""
        def __init__(self, items):
            self._items = items
        def items(self, k=200):
            return list(self._items[:k])

    now = 1_800_000_000.0
    ctx = FakeCtx([
        {"ns": "her", "key": "game", "val": "昨晚那局还没玩完，说到第3关", "ts": now - 26 * 3600},
    ])

    try:
        if j is not None:
            j.log("user", "user", "你还记得昨天干嘛了吗", ts=now - 300)
            j.log("her", "her", "记得呀，那局还没打完", ts=now - 280)
            j.log("task", "PC-A", "备份失败：磁盘满", topic="intent:备份", ts=now - 200)

        # ① history 顺序正序
        c = build_context(journal=j, ctx=ctx, st=st, now=now)
        hist = c["history"]
        check(len(hist) == 2, "history 取到 2 条")
        check(hist[0]["role"] == "user" and hist[1]["role"] == "assistant", "user/assistant 交替")
        check("还记得昨天" in hist[0]["content"] and "记得呀" in hist[1]["content"], "history 时间正序")

        # ② system 含「只能引用」约束（有 recall 时）
        check(MEMORY_CONSTRAINT in c["system"], "system 含「只能引用，不要编造」")
        check("昨晚那局还没玩完" in c["system"], "system 含往事原文")
        check("备份失败" in c["system"], "system 含欠账原文")

        # 缺件不崩
        c2 = build_context(journal=None, ctx=None, st=None)
        check(isinstance(c2["system"], str) and c2["system"], "无 journal/ctx/st 也不崩")
        check(c2["history"] == [], "无流水时 history 为空")

        # ③ user/her 各落一条流水
        before = j.count() if j is not None else 0
        r = reply("今天有点累", journal=j, ctx=ctx, st=st, now=now)
        if j is not None:
            check(j.count() == before + 2, "user/her 各落一条流水")
            # 用 id 倒序取最后两条（自测里 now 是固定时钟，真实时钟落的行 ts 不在同一窗口）
            rows_all = sorted(j.recent(hours=10 ** 6), key=lambda x: x["id"])
            kinds = [row["kind"] for row in rows_all[-2:]]
            check(kinds == ["user", "her"], "落盘顺序：先 user 后 her")

        # ④ 裸环境（无 cloud/cmd/http）时走 mock 保底，也必须给得出话
        saved_cmd = os.environ.pop("FLEET_LLM_CMD", None)
        saved_url = os.environ.pop("FLEET_LLM_URL", None)
        saved_cloud = os.environ.pop("FLEET_CLOUD_URL", None)
        try:
            r2 = reply("在吗", journal=None, ctx=None, st=None)
            check(r2["backend"] in ("mock", "placeholder"),
                  "裸环境落到 mock/placeholder 保底")
            check(bool(r2["say"]), "保底后端也给得出话")
        finally:
            if saved_cmd is not None:
                os.environ["FLEET_LLM_CMD"] = saved_cmd
            if saved_url is not None:
                os.environ["FLEET_LLM_URL"] = saved_url
            if saved_cloud is not None:
                os.environ["FLEET_CLOUD_URL"] = saved_cloud

        # ⑤ 回复文本里没有残留 </think> / <think>
        check("</think>" not in r["say"], "回复里无 </think> 残留")
        check("<think>" not in r["say"], "回复里无 <think> 残留")

        # 假模型注入脏输出：确认端到端也干净
        if llm is not None:
            orig = llm.complete
            def fake_complete(prompt=None, model=None, system=None):
                return ("</think>\n\n```\n我在的，主人。\n```", "cmd")
            llm.complete = fake_complete
            try:
                r3 = reply("喂", journal=None, ctx=None, st=None)
                check("</think>" not in r3["say"] and "```" not in r3["say"], "端到端输出已清洗")
                check("我在的，主人。" in r3["say"], "清洗后正文保留")
                check(r3["backend"] == "cmd", "后端名透传")
            finally:
                llm.complete = orig

        for line in done:
            print("[selftest] %s OK" % line)
        print("[selftest] PASS")
    finally:
        try:
            if j is not None:
                j.close_connection()
        except Exception:
            pass
        for f in (dbpath, ps):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()

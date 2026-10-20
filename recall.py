"""她记得：从 L3 共享上下文里唤醒「昨天/前天」的具体事。

验收标准「她记得 —— 隔日 + 换设备，能主动带出昨天的具体事」的空白不在模型，
而在**没人去取**：`_persona_tick` 之前只会说「你在干嘛？」或复述执念。
本模块只做三件事，且都可离线确定性验证：

1. **取候选**：按年龄窗口（默认 12h–72h）从 L3 扫，「刚发生的」不用回忆，
   「一个多月前的」当八卦没意义；跳过她自己的元状态（last_thought 等）与技术缓存。
2. **去重**：同一件事 N 天内不重复说，否则「不重复说」这条验收直接失守。
3. **组织语言**：给模型一段提示词（含原文），或给模板句——模板**只引用库里原文，
   绝不编造记忆**；没记忆时明确返回 None，绝不假装记得。

设计约束：纯 stdlib；`now` 可注入，故自测不依赖真实时钟与时序。
"""
import time

# 她自己的元状态不是「记忆」，当成往事说出来会让对话穿帮
RESERVED = {
    ("her", "last_thought"),
    ("her", "pending_thought"),
    ("her", "last_recall"),
}
# 技术缓存不是生活记忆
SKIP_NS = {"compute_cache"}

DEFAULT_WINDOW = (12 * 3600, 72 * 3600)   # 12h–72h：隔日的、还记得住的事
DEFAULT_PREFER_AGE = 26 * 3600            # 偏好「昨天这个时候」
DEFAULT_DEDUPE_DAYS = 3.0


def human_age(ts, now=None):
    """把时间戳说成人话——她不该用 unix 时间跟人聊天。"""
    now = time.time() if now is None else now
    d = (now - ts) / 86400.0
    if d < 0.75:
        return "今天"
    if d < 1.75:
        return "昨天"
    if d < 2.75:
        return "前天"
    return f"{int(d)}天前"


def _age_ok(ts, now, window):
    lo, hi = window
    age = now - ts
    return lo <= age <= hi


def candidates(ctx, now=None, window=DEFAULT_WINDOW, k=3,
               prefer_age=None, skip=RESERVED, skip_ns=SKIP_NS):
    """从 L3 取「值得回忆」的条目，按「离昨天有多近」排序。"""
    now = time.time() if now is None else now
    prefer_age = DEFAULT_PREFER_AGE if prefer_age is None else prefer_age
    out = []
    for it in ctx.items(500):
        if (it["ns"], it["key"]) in skip:
            continue
        if it["ns"] in skip_ns:
            continue
        if not (it.get("val") or "").strip():
            continue
        if not _age_ok(it["ts"], now, window):
            continue
        row = dict(it)
        row["age"] = now - it["ts"]
        row["when"] = human_age(it["ts"], now)
        row["score"] = -abs(row["age"] - prefer_age)
        out.append(row)
    out.sort(key=lambda r: (r["score"], r["ts"]), reverse=True)
    return out[:k]


def recalled_key(it):
    return f"{it['ns']}/{it['key']}"


def pick(ctx, st, now=None, window=DEFAULT_WINDOW, k=3, prefer_age=None,
         dedupe_days=DEFAULT_DEDUPE_DAYS):
    """候选 - 最近已说过的 = 这次可以提的。"""
    now = time.time() if now is None else now
    seen = (st.data.get("recalled") or {})
    fresh = []
    for it in candidates(ctx, now=now, window=window, k=k * 3 + 3, prefer_age=prefer_age):
        last = seen.get(recalled_key(it))
        if last is not None and now - last < dedupe_days * 86400:
            continue
        fresh.append(it)
        if len(fresh) >= k:
            break
    return fresh


def mark_recalled(st, ctx, items, now=None, dedupe_days=DEFAULT_DEDUPE_DAYS):
    """说过就记账：N 天内不再提同一件事；顺带写 L3，换设备也能看到她提过什么。"""
    now = time.time() if now is None else now
    seen = st.data.setdefault("recalled", {})
    for it in items:
        seen[recalled_key(it)] = now
    # 过期记录定期清掉，人格态文件不无限膨胀
    cutoff = now - dedupe_days * 86400
    for key in [k for k, ts in seen.items() if ts < cutoff]:
        del seen[key]
    st.save()
    if items:
        ctx.put("her", "last_recall", items[0]["val"])
    return len(items)


def prompt_hint(items):
    """给模型的提示片段：原文照抄，不许它自己编往事。"""
    if not items:
        return ""
    lines = ["你记得的往事（来自共享记忆，只能引用，不要编造）："]
    for it in items:
        lines.append(f"- {it['when']}：{it['val']}")
    return "\n".join(lines)


def template_line(items, catchphrase=""):
    """没接模型时的模板句：只拼接库里原文，不含任何虚构成分。"""
    if not items:
        return None
    it = items[0]
    lead = {"昨天": "昨天的事我还记着", "前天": "前天的事我没忘"}.get(it["when"], "我一直记着")
    return f"{catchphrase}{lead}——{it['val']}"


def _selftest():
    """离线验证：窗口过滤 / 元状态排除 / 排序 / 去重 / 不编造。"""
    import os
    import tempfile
    import memory as mem_mod
    import persona as p

    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(db)
    fd, ps = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(ps)
    ctx = mem_mod.SharedContext(db)
    st = p.PersonaState(ps)
    try:
        now = 1_800_000_000.0            # 固定时钟：自测不吃时序的亏
        ctx.put("her", "game_lastnight", "昨晚那局还没玩完，说到第3关", ts=now - 26 * 3600)
        ctx.put("her", "just_now", "刚喝了口水", ts=now - 5 * 60)
        ctx.put("her", "ancient", "上个月修过路由器", ts=now - 40 * 86400)
        ctx.put("her", "last_thought", "我刚说过的一句话", ts=now - 26 * 3600)
        ctx.put("compute_cache", "abc", "模型缓存内容", ts=now - 26 * 3600)
        ctx.put("user", "tired", "昨天加班到十一点", ts=now - 20 * 3600)

        got = candidates(ctx, now=now)
        keys = [c["key"] for c in got]
        assert "game_lastnight" in keys and "tired" in keys, keys
        for bad in ("just_now", "ancient", "last_thought", "abc"):
            assert bad not in keys, f"{bad} 不该进候选: {keys}"
        print("[selftest] 窗口过滤 + 元状态/技术缓存排除 OK")

        # 排序：默认偏好「昨天这个时候」，20h 比 26h 更近 prefer_age(26h)? 20h 差 6h，26h 差 0
        # 故 26h 的应排第一
        assert got[0]["key"] == "game_lastnight", [c["key"] for c in got]
        print("[selftest] 按「离昨天有多近」排序 OK")

        assert got[0]["when"] == "昨天", got[0]["when"]
        assert human_age(now - 50 * 3600, now) == "前天"
        print("[selftest] 时间说成人话（昨天/前天） OK")

        hint = prompt_hint(got)
        assert "昨晚那局还没玩完" in hint and "共享记忆" in hint, hint
        assert "我刚说过的一句话" not in hint, "元状态不许当往事说"
        print("[selftest] 提示词只含真实记忆 OK")

        line = template_line(got, "大肥鱼~")
        assert line and "昨晚那局还没玩完，说到第3关" in line, line
        assert template_line([], "大肥鱼~") is None, "没记忆就必须闭嘴"
        print("[selftest] 模板引用原文且不编造 OK")

        # 去重：说过就 N 天内不再说
        picked = pick(ctx, st, now=now)
        assert [c["key"] for c in picked][0] == "game_lastnight"
        mark_recalled(st, ctx, picked, now=now)
        after = pick(ctx, st, now=now, k=5)
        assert "game_lastnight" not in [c["key"] for c in after], after
        assert ctx.get("her", "last_recall")["ok"], "说过的事应写进 L3（换设备也知道）"
        print("[selftest] 同一件事 3 天内不重复说 + 跨设备可见 OK")

        # 去重会过期（窗口同步放宽，证明是去重过期而非窗口挡的）
        later = now + 4 * 86400
        again = pick(ctx, st, now=later, window=(72 * 3600, 200 * 3600), k=5)
        assert "game_lastnight" in [c["key"] for c in again], again
        print("[selftest] 去重过期后可再提 OK")

        # 人格态不膨胀：再记一次，旧记录被清
        mark_recalled(st, ctx, again, now=later)
        assert all(later - ts < 3 * 86400 for ts in st.data["recalled"].values()), st.data["recalled"]
        print("[selftest] 去重记账自动清理 OK")

        # 空库：明确没有记忆
        empty = mem_mod.SharedContext(":memory:")
        assert candidates(empty, now=now) == []
        assert pick(empty, st, now=now) == []
        print("[selftest] 空库不乱认亲 OK")
        print("[selftest] PASS")
    finally:
        ctx.close()
        for f in (db, ps):
            if os.path.exists(f):
                os.remove(f)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()

"""未完成项抽取：把过去 N 小时的对话翻成「她该起的话题」。

上游是 `journal.py`（对话流水）。本模块回答三个问题：
1. **哪些事没闭环**：有 topic、状态 open、在窗口内——按 topic 合并多次记录成一件；
2. **哪些不该再问**：同一 topic 最近 N 小时已追问过（记账在人格态 `followups`）；
3. **怎么起话**：给模型一段含原文的提示词，或给模板句——同样**只引用流水原文，
   绝不编造一件没发生过的事**；没未完成项就明确返回 None，不硬找话。

与 `recall.py` 的分工：recall 唤醒「记忆里的一件事」（KV 快照），
本模块追「对话里的一笔账」（事件流）。前者是回忆，后者是跟进。

设计约束：纯 stdlib；`now` 可注入；排序取**最近欠的优先**——
刚失败的事用户还记得、最有跟进价值；三天前的旧账翻出来只是骚扰。
"""
import time

import recall  # 复用「时间说成人话」，不重造

DEFAULT_HOURS = 24
DEFAULT_K = 1                     # 每次最多起一个话头：她不是复读机
DEFAULT_REASK_HOURS = 24          # 同一笔账 24h 内不重复追问
MAX_BODY = 40                     # 模板句里引用原文的长度上限（口语要短）


def task_topic(text):
    """意图编排的话题名：同一句话重试算同一笔账。"""
    return f"intent:{str(text)[:40].strip()}"


def queue_topic(name, cmd):
    """离线排队的话题名。"""
    return f"queue:{name}:{str(cmd)[:30].strip()}"


def her_topic(cap):
    """她的执念话题名（P4 被拒后她自己记的账）。"""
    return f"her:{cap}"


def extract(journal, now=None, hours=DEFAULT_HOURS, kinds=("task", "her")):
    """未闭环项：按 topic 合并，带**证据 id**（指回流水原文，方便人核对）。"""
    rows = journal.open_topics(hours=hours, now=now, kinds=kinds)
    merged = {}
    for r in rows:
        t = r["topic"]
        if t not in merged:
            merged[t] = {
                "topic": t, "kind": r["kind"], "actor": r["actor"],
                "first_ts": r["ts"], "last_ts": r["ts"],
                "count": 0, "body": r["body"], "evidence_id": r["id"],
            }
        m = merged[t]
        m["count"] += 1
        m["last_ts"] = max(m["last_ts"], r["ts"])
        if r["ts"] >= m["first_ts"]:          # 保留最新表述（重试后的说法更准）
            m["body"] = r["body"]
            m["evidence_id"] = r["id"]
    items = list(merged.values())
    for it in items:
        it["when"] = recall.human_age(it["last_ts"], now)
    items.sort(key=lambda r: r["last_ts"], reverse=True)   # 最近欠的优先
    return items


def _asked(st):
    return (st.data.get("followups") or {})


def pick(journal, st, now=None, hours=DEFAULT_HOURS, k=DEFAULT_K,
         reask_hours=DEFAULT_REASK_HOURS):
    """未闭环 - 最近问过的 = 这次可以起的。"""
    now = time.time() if now is None else now
    asked = _asked(st)
    out = []
    for it in extract(journal, now=now, hours=hours):
        last = asked.get(it["topic"])
        if last is not None and now - last < reask_hours * 3600:
            continue
        out.append(it)
        if len(out) >= k:
            break
    return out


def mark_asked(st, journal, items, now=None):
    """追问过就记账（24h 内不重复）；同时把她的话记进流水——追问本身也是对话。"""
    now = time.time() if now is None else now
    asked = st.data.setdefault("followups", {})
    for it in items:
        asked[it["topic"]] = now
    cutoff = now - 7 * 86400                     # 记账只留 7 天，人格态不膨胀
    for t in [t for t, ts in asked.items() if ts < cutoff]:
        del asked[t]
    st.save()
    for it in items:
        journal.log("her", "her", it.get("said") or it["body"],
                    topic=it["topic"], ref=it["evidence_id"], ts=now)
    return len(items)


def resolve(journal, st, topic):
    """闭环：账清了，从此不再提；人格态里对应的追问记账一并清掉。"""
    n = journal.close(topic)
    asked = st.data.get("followups") or {}
    if topic in asked:
        del asked[topic]
        st.data["followups"] = asked
        st.save()
    return n


def prompt_hint(items):
    """给模型的提示片段：原文照抄 + 明确「只能引用」+ 要求她起话而不是汇报。"""
    if not items:
        return ""
    lines = ["以下是你和主人之间还没闭环的事（来自对话流水原文，只能引用，不要编造）："]
    for it in items:
        lines.append(f"- {it['when']}开的头（流水 #{it['evidence_id']}）：{it['body']}")
    lines.append("用一句口语起话头问问进展，不超过30字。")
    return "\n".join(lines)


def template_line(items, catchphrase=""):
    """没接模型时的模板句：只拼接流水原文 + 一个追问尾巴。"""
    if not items:
        return None
    it = items[0]
    body = it["body"] if len(it["body"]) <= MAX_BODY else it["body"][:MAX_BODY] + "…"
    if it["kind"] == "task":
        return f"{catchphrase}{it['when']}那事还没完——{body}。现在弄？"
    return f"{catchphrase}{body}（还没答应我呢）"


def _selftest():
    """离线验证：合并 / 去重追问 / 闭环即消 / 窗口 / 不编造。"""
    import os
    import tempfile
    import journal as jmod
    import persona as p

    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(db)
    fd, ps = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(ps)
    j = jmod.Journal(db)
    st = p.PersonaState(ps)
    try:
        now = 1_800_000_000.0
        # 一笔失败两次的账（同一 topic 两条） + 一笔刚失败的 + 一笔她想说没说的
        j.log("task", "PC-A", "意图『备份』失败：type 不存在的文件", topic=task_topic("备份"),
              ts=now - 6 * 3600)
        j.log("task", "PC-A", "意图『备份』又失败：磁盘满", topic=task_topic("备份"),
              ts=now - 2 * 3600)
        fresh = j.log("task", "PC-Ghost", "排队未执行：echo 上线重放OK", topic=queue_topic("PC-Ghost", "echo 上线重放OK"),
                      ts=now - 30 * 60)
        j.log("her", "her", "我还在想 phone.screen.on", topic=her_topic("phone.screen.on"),
              ts=now - 3 * 3600)

        items = extract(j, now=now)
        assert len(items) == 3, [i["topic"] for i in items]
        # 同一 topic 合并成一件，保留最新表述与证据 id
        bk = [i for i in items if i["topic"] == task_topic("备份")][0]
        assert bk["count"] == 2 and "磁盘满" in bk["body"], bk
        # 最近欠的优先：30 分钟前的排队排最前
        assert items[0]["evidence_id"] == fresh, items[0]
        print("[selftest] 按 topic 合并 + 保留最新表述与证据 id OK")

        picked = pick(j, st, now=now, k=2)
        assert len(picked) == 2 and picked[0]["topic"] == queue_topic("PC-Ghost", "echo 上线重放OK")
        print("[selftest] 取最近欠的优先 OK")

        # 追问一次后，24h 内不再问同一笔（没问过的第 3 笔仍可起）
        mark_asked(st, j, picked, now=now)
        rest = pick(j, st, now=now, k=5)
        assert [i["topic"] for i in rest] == [her_topic("phone.screen.on")], rest
        print("[selftest] 同一笔账 24h 内不重复追问 OK")

        # 追问本身也记进流水（她的话不是凭空来的）
        rows = [r for r in j.recent(hours=1, now=now, kinds=("her",))
                if r["topic"] == picked[0]["topic"]]
        assert rows and rows[-1]["ref"] == picked[0]["evidence_id"], rows
        print("[selftest] 追问带证据 id 落流水 OK")

        # 过期后可再问（窗口仍覆盖，证明是去重过期而非窗口挡的）
        later = now + 2 * 3600
        assert pick(j, st, now=later, reask_hours=1, k=1), "去重应已过期"
        print("[selftest] 追问记账过期后可再问 OK")

        # 闭环即消：任务完成 -> 这笔账不再出现
        resolve(j, st, picked[0]["topic"])
        assert picked[0]["topic"] not in [i["topic"] for i in extract(j, now=later)], "闭环后不该再提"
        print("[selftest] 闭环即消（完成的事不再翻） OK")

        # 模板与提示词：只含原文，空项闭嘴
        it2 = extract(j, now=now)[0]
        line = template_line([it2], "大肥鱼~")
        assert line and it2["body"][:20] in line and line.rstrip().endswith("？"), line
        hint = prompt_hint([it2])
        assert "流水" in hint and it2["body"][:20] in hint and "不要编造" in hint, hint
        assert template_line([]) is None and prompt_hint([]) == ""
        print("[selftest] 模板/提示词只引用原文，空项不硬找话 OK")

        # 窗口：25h 前开的头不再翻（陈年旧账是骚扰）
        j.log("task", "PC-Old", "很久以前的失败", topic=task_topic("老账"), ts=now - 25 * 3600)
        assert all(i["topic"] != task_topic("老账") for i in extract(j, now=now)), "陈账不该翻"
        print("[selftest] 24h 窗口过滤 OK")
        print("[selftest] PASS")
    finally:
        j.close_connection()
        for f in (db, ps):
            if os.path.exists(f):
                os.remove(f)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()

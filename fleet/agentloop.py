"""云端 agent loop：问一句 → 模型要工具 → 真执行 → 结果回填 → 模型说人话。

为什么还要这一层（toolschema 不是已经能读懂回信了吗）：
`toolschema.py` 只管「信封」——把能力表包成 OpenAI/Anthropic 的形状，
再把模型吐回来的 tool_call 解析成能力名。**它不跑。**
信封和回环都齐了却没人把它们串起来，等于造了一段谁也没走过的路。

这一层就是那个「走」的动作：发起请求 → 拿回 tool_call → 过闸门真执行 →
把结果喂回模型 → 让它用真数字说人话。跑完这一圈，P4 才叫真 Agent，
否则只是「能发一张工具表」。

和 `agent_tools.py` 的分工（**别混**）：

    agent_tools.run_tools   本地小模型（手机 1.7B）：7 选 1 枚举决策。
                            注释里写死了原因——1.7B 发不出 tool_calls
                            （BFCL V4：4B 多轮只有 22%），枚举才守得住。
    本模块                  云端大模型（三家网关）：真 function calling。

两条路的**缰绳必须一样**（轮次上限 / 总预算 / 同一能力不重试），
只是决策形式不同。缰绳另发明一套，就是给「跑飞」开第二个口子。

本模块**不自己执行能力**：执行交给 `Registry.call`，闸门在那一层
（没注入闸门 ⇒ fail-closed 拒绝）。这里唯一的例外是「模型叫错名字」——
那种情况不执行任何东西，把错误原文回填给模型让它自己改。
"""
import time
import json

from . import providers as P
from . import toolschema as TS


#: 默认系统提示。刻意短——云模型上下文按 token 花钱，且工具表已经很长了。
SYSTEM = (
    "你是天网的执行层。能用工具就先用工具，别凭印象回答："
    "能查到的事实必须查。工具结果说什么就是什么，不许补充、不许推测、"
    "不许把没查到的东西说成查到了。查不到就直说查不到。"
)

#: 轮次上限。模型不能自己决定什么时候停——它会一直调下去。
MAX_ROUNDS = 3
#: 总预算（秒）。决策 + 执行都算在内，超了立刻收手。
BUDGET_S = 90


class Step:
    """走了一步的记录（每步一个工具调用）。"""

    __slots__ = ("round", "cap", "args", "ok", "text", "error")

    def __init__(self, rnd=0, cap="", args=None, ok=False, text="", error=""):
        self.round = rnd
        self.cap = cap
        self.args = args or {}
        self.ok = ok
        self.text = text
        self.error = error

    def as_dict(self):
        return {"round": self.round, "cap": self.cap, "args": self.args,
                "ok": self.ok, "text": self.text, "error": self.error}

    def __repr__(self):
        if self.error:
            return f"<Step r{self.round} {self.cap}: {self.error}>"
        return f"<Step r{self.round} {self.cap} ok={self.ok} len={len(self.text)}>"


def _summarize_steps(steps, why=""):
    """总结没成 / 总结是空的时，用**已经真发生的事**拼一句人话。

    🔴 为什么必须有这个（2026-10-25 真机实测）
      工具跑完了、动作也真做了，但「收尾总结」那次调用回来 `content` 是空串
      （免费网关常这样）。原先直接把空串当 answer 发下去，手机端就是**一片空白**
      —— 用户不知道刚才到底动没动手，只能再问一遍，白烧一轮。
      空白是最坏的结果：它既没告诉人成了，也没告诉人没成。

      所以这里不许编造结论，只做一件事：**如实复述这轮真跑了什么**。
      模型没给结论就写「没给结论」，不替它下判断。
    """
    if not steps:
        return why or "这一轮没跑到任何工具，也没拿到结论。可以再说具体点吗？"
    lines = []
    for s in steps:
        mark = "✓" if s.ok else "✗"
        brief = (s.text or s.error or "").strip().replace("\n", " ")
        if len(brief) > 120:
            brief = brief[:120] + "…"
        lines.append("%s %s%s" % (mark, s.cap, ("：" + brief) if brief else ""))
    head = "（模型没给出总结，下面是这一轮实际跑了的东西）" if not why else \
           "（%s）" % why
    return head + "\n" + "\n".join(lines)


def _exec(executor, cap, args):
    """调一次执行层，把结果统一成 (ok, 文本)。

    执行层可能是 Registry（返回 DeviceResult），也可能只是个函数（返回 str）。
    两种都得接住——**结果形态不统一是「工具跑了但没内容」这类 bug 的来源**。
    """
    try:
        r = executor(cap, args)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)

    if r is None:
        return False, "执行层没返回任何东西"
    if isinstance(r, str):
        return True, r

    # ① 先把「两种形态」收敛成同一份字段。
    #    🔴 这里原来是个真伤（2026-10-25 查出来的）：
    #       载荷用 `getattr(r, "data", None)` 取 —— 对 **dict** 永远取不到
    #       （dict 没有 .data 属性），于是 `data is None` 直接返回空字符串。
    #       而 `FleetAgentServer.executor` 返回的正是 dict ⇒ **所有**工具结果
    #       送到模型眼前都是空白。模型等于蒙着眼睛调工具，答不出来只能瞎猜，
    #       表现成「工具跑了但没内容」——工具本身一点问题都没有。
    #    所以：dict 走 `.get`，对象走 `getattr`，谁也不许借对方的语法。
    if isinstance(r, dict):
        raw_ok = r.get("ok")
        data = r.get("data")
        err = r.get("err") or ""
    else:
        raw_ok = getattr(r, "ok", None)
        data = getattr(r, "data", None)
        err = getattr(r, "err", "") or ""
        if hasattr(r, "as_dict"):
            d = r.as_dict()
            err = err or (d.get("err") or "")
            data = data if data is not None else d.get("data")

    ok = True if raw_ok is None else bool(raw_ok)
    if not ok:
        return False, err or "执行失败（没说原因）"
    if data is None or data == {}:
        return True, ""
    if isinstance(data, str):
        return True, data
    try:
        import json
        return True, json.dumps(data, ensure_ascii=False)
    except Exception:
        return True, str(data)


def run(question, executor, caps=None, tools=None, index=None,
        provs=None, need=None, model=None, account="", quota=None,
        max_rounds=MAX_ROUNDS, budget_s=BUDGET_S, system=SYSTEM,
        flavor=TS.FLAVOR_OPENAI, on_event=None):
    """跑完一圈。返回 dict。

    参数：
      `executor`  执行层：`f(cap_name, args) -> str | DeviceResult | None`。
                  **必须给**——本模块不自己碰设备。给 Registry 时写成
                  `lambda cap, args: registry.call(cap, args=args)`。
      `caps`      能力表（默认全仓 CAPABILITIES，只读的那些）。
      `tools`     外部算好的工具表；给了就不重算（调方想收窄时用）。
      `index`     ToolIndex；不给就按 caps 现建。

    返回：
      ok / answer / steps / used / used_model / rounds / err / why
      `steps` 是每一步的真记录——**判据就靠它**：手机上/日志里要能看见
      真的 `node.exec` 和真的目录输出，不是 Demo 数据。

    缰绳三条（与 agent_tools 同源，不另发明）：
      1. 轮次硬上限 `max_rounds`，模型不能自己决定停；
      2. 总预算 `budget_s`，超了立刻收手；
      3. 同一个能力不重复调（重复 = 模型没听话，直接把「已经调过」告诉它）。
    """
    t0 = time.time()
    steps = []
    used_caps = set()

    def fire(phase, payload=None):
        if on_event:
            try:
                on_event(phase, payload or {})
            except Exception:
                pass

    if caps is None:
        from fleet.catalog import CAPABILITIES
        caps = list(CAPABILITIES)
    if index is None:
        index = TS.ToolIndex(caps)
    if tools is None:
        # 默认只给只读：云端模型不该在没人看着的时候动手（见 toolschema 口径）
        tools = TS.tools_for(flavor, caps, include_unsafe=False)

    # **工具表就是权限边界**，不是给模型看的菜单。
    # 模型完全可以叫一个没出现在表里的名字（幻觉、或者它记得别家的工具名），
    # 而能力表里真有那个名字（比如 pc.shell）——只查「能力存不存在」就放行，
    # 等于把「默认只给只读」这条线拆了。所以执行前必须再对一次表名。
    if flavor == TS.FLAVOR_OPENAI:
        allowed = {t["function"]["name"] for t in tools}
    else:
        allowed = {t["name"] for t in tools}
    quota = quota if quota is not None else P.Quota()

    messages = [{"role": "system", "content": system},
                {"role": "user", "content": question}]
    last = {"used": "", "used_model": "", "why": []}

    for rnd in range(1, max_rounds + 1):
        if time.time() - t0 >= budget_s:
            fire("budget", {"round": rnd})
            return {"ok": True, "answer": "", "steps": [s.as_dict() for s in steps],
                    "rounds": rnd - 1, "err": "预算用尽（%ds）先收手" % budget_s,
                    "budget": True, **last}

        fire("thinking", {"round": rnd})
        res = P.chat(messages, provs=provs, need=need, model=model,
                     tools=tools, quota=quota, account=account)
        last = {"used": res.get("used", ""), "used_model": res.get("used_model", ""),
                "why": res.get("why", [])}
        if not res.get("ok"):
            fire("error", {"round": rnd, "err": res.get("err")})
            return {"ok": False, "answer": "", "steps": [s.as_dict() for s in steps],
                    "rounds": rnd - 1, "err": res.get("err") or "请求失败",
                    "why": res.get("why", []), **last}

        raw = res.get("raw") or {}
        if not TS.has_calls(raw, flavor):
            # 这轮没要工具 ⇒ 它认为可以答了
            fire("answer", {"round": rnd})
            return {"ok": True, "answer": res.get("content") or _summarize_steps(steps),
                    "steps": [s.as_dict() for s in steps], "rounds": rnd,
                    "why": res.get("why", []), **last}

        calls = TS.extract_calls(raw, flavor, index)
        if not calls:
            return {"ok": True,
                    "answer": res.get("content") or _summarize_steps(
                        steps, "模型说要调工具，但解析不出来"),
                    "steps": [s.as_dict() for s in steps], "rounds": rnd,
                    "err": "模型说要调工具，但解析不出来（形状不认识）", **last}

        # 回填前必须把 assistant 那条带 tool_calls 的消息原样放回，
        # 否则 tool_call_id 对不上，OpenAI 协议直接拒。
        try:
            assistant_msg = raw["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            assistant_msg = {"role": "assistant", "content": res.get("content", "")}
        messages.append(assistant_msg)

        results = {}
        for c in calls:
            if c.error:
                # 叫错名字 / 参数不合法：不执行任何东西，把错误原文还给它
                st = Step(rnd, c.cap or c.tool, c.args, False, "", c.error)
                steps.append(st)
                results[c.id or c.tool] = "调用没成立：" + c.error
                fire("bad_call", {"round": rnd, "tool": c.tool, "err": c.error})
                continue
            if c.cap in used_caps:
                st = Step(rnd, c.cap, c.args, False, "",
                          "这个能力这轮已经调过了，不再重复")
                steps.append(st)
                results[c.id or c.tool] = st.error
                continue
            if c.tool not in allowed:
                # 有这能力，但这次没给它 —— 不许执行，把边界告诉它
                st = Step(rnd, c.cap, c.args, False, "",
                          "这个工具这次没给你（不在工具表里），不许调")
                steps.append(st)
                results[c.id or c.tool] = st.error
                fire("denied", {"round": rnd, "cap": c.cap})
                continue
            used_caps.add(c.cap)
            fire("exec", {"round": rnd, "cap": c.cap,
                          "args": json.dumps(c.args, ensure_ascii=False)})
            ok, text = _exec(executor, c.cap, c.args)
            text = TS.clip_result(text)
            steps.append(Step(rnd, c.cap, c.args, ok, text, "" if ok else text))
            results[c.id or c.tool] = text
            # 🔴 手机端要在这张卡上显示真结果 —— 必须把 text 透传过去，
            # 只发 len 的话手机端只能显示「完成」却看不到工具到底吐了什么。
            fire("done", {"round": rnd, "cap": c.cap, "ok": ok,
                          "text": text, "ms": 0})

        messages.extend(TS.tool_result_messages(calls, results, flavor))

    # 轮次用尽还没答 ⇒ 把已经拿到的东西交给模型做最后一次总结（不带工具表，
    # 逼它出结论而不是再来一轮）
    fire("finalize", {"rounds": max_rounds})
    final = P.chat(messages, provs=provs, need=need, model=model,
                   quota=quota, account=account)
    content = (final.get("content") or "") if final.get("ok") else ""
    if content.strip():
        return {"ok": True, "answer": content,
                "steps": [s.as_dict() for s in steps], "rounds": max_rounds,
                "used": final.get("used", ""), "used_model": final.get("used_model", ""),
                "why": final.get("why", [])}
    # 总结没成、或者总结回来是空串 ⇒ 用真跑过的东西兜底，绝不给一个空白回答
    why = "" if final.get("ok") else ("最后一轮总结失败：" + (final.get("err") or ""))
    return {"ok": True, "answer": _summarize_steps(steps, why),
            "steps": [s.as_dict() for s in steps], "rounds": max_rounds,
            "used": final.get("used", ""), "used_model": final.get("used_model", ""),
            "why": final.get("why", []), "fallback": True}


# ---------------------------------------------------------------- 自测

def _selftest():
    """全离线：chat 用假的、executor 用假的。验的是「串得对不对」，
    不是「网关今天活着」——和 providers 的门禁一个道理。
    """
    import json
    from fleet.catalog import CAPABILITIES
    from fleet.core.registry import Capability, Param

    fails = []

    def ck(cond, why):
        if not cond:
            fails.append(why)

    caps = list(CAPABILITIES)
    index = TS.ToolIndex(caps)

    # ---- 假执行层：只认 pc.state，别的说不会 ----
    seen = []

    def executor(cap, args):
        seen.append((cap, args))
        if cap == "pc.state":
            return "CPU 12% / 内存 41% / 磁盘剩余 233G"
        if cap == "screen.read":
            return None                     # 执行层啥也没回
        if cap == "pc.usage":
            raise RuntimeError("记账文件锁住了")
        return "不支持"

    def _mkresp(calls, content=None):
        return {"choices": [{"message": {"role": "assistant", "content": content,
                "tool_calls": [{"id": "c%d" % i, "type": "function",
                                "function": {"name": TS.to_mcp_name(n),
                                             "arguments": json.dumps(a)}}
                               for i, (n, a) in enumerate(calls)]}}]}

    # ---- 假 chat：第一轮要工具，第二轮出答案 ----
    posts = []

    def fake_chat(messages, provs=None, need=None, model=None, tools=None,
                  quota=None, account="", tool_choice=None, **kw):
        posts.append({"n": len(messages), "tools": tools, "last": messages[-1]})
        if len(posts) == 1:
            return {"ok": True, "used": "zen-gate", "used_model": "m1",
                    "content": "", "tokens": 10, "why": [],
                    "raw": _mkresp([("pc.state", {"clip": False})])}
        return {"ok": True, "used": "zen-gate", "used_model": "m1",
                "content": "这台机器现在 CPU 12%、内存 41%。", "tokens": 20,
                "why": [], "raw": {"choices": [{"message": {
                    "role": "assistant", "content": "这台机器现在 CPU 12%、内存 41%。"}}]}}

    orig = P.chat
    P.chat = fake_chat
    try:
        r = run("这台机器现在怎么样", executor, caps=caps, index=index,
                quota=P.Quota(":memory:"))
    finally:
        P.chat = orig

    ck(r["ok"] is True, "应跑通，实得 %r" % r.get("err"))
    ck("CPU 12%" in r["answer"], "答案里要有真数字，实得 %r" % r["answer"])
    ck(len(r["steps"]) == 1, "应记 1 步，实得 %d" % len(r["steps"]))
    ck(r["steps"][0]["cap"] == "pc.state", "步骤里必须是能力真名")
    ck("CPU 12%" in r["steps"][0]["text"], "步骤里要留真结果")
    ck(seen == [("pc.state", {"clip": False})], "执行层收到的参数不对：%r" % seen)
    # 关键：第二轮请求必须带 assistant 的 tool_calls + tool 结果，否则协议不认
    ck(len(posts) == 2, "应是两轮，实得 %d" % len(posts))
    ck(posts[0]["tools"] and len(posts[0]["tools"]) == 85,
       "第一轮要带工具表（默认只读 85），实得 %r" % (
           len(posts[0]["tools"]) if posts[0]["tools"] else None))
    ck(posts[1]["tools"] and len(posts[1]["tools"]) == 85,
       "循环内继续带工具表（模型可能要连着调几个），实得 %r" % (
           len(posts[1]["tools"]) if posts[1]["tools"] else None))
    ck(posts[1]["last"]["role"] == "tool", "回填消息必须是 role=tool")
    ck(posts[1]["last"]["tool_call_id"] == "c0", "tool_call_id 必须对上")

    # ---- 模型叫错名字：不执行任何东西，把错还给它，再让它自己改 ----
    posts2 = []
    seen2 = []

    def fake_chat2(messages, provs=None, need=None, model=None, tools=None,
                   quota=None, account="", tool_choice=None, **kw):
        posts2.append(messages[-1])
        if len(posts2) == 1:
            return {"ok": True, "used": "p", "used_model": "m", "content": "",
                    "tokens": 1, "why": [],
                    "raw": _mkresp([("no_such_tool", {})])}
        return {"ok": True, "used": "p", "used_model": "m",
                "content": "我刚才叫错了工具名，重来。", "tokens": 1, "why": [],
                "raw": {"choices": [{"message": {"role": "assistant",
                                                 "content": "我刚才叫错了工具名，重来。"}}]}}

    def ex2(cap, args):
        seen2.append(cap)
        return "x"

    P.chat = fake_chat2
    try:
        r2 = run("随便问", ex2, caps=caps, index=index, quota=P.Quota(":memory:"))
    finally:
        P.chat = orig
    ck(seen2 == [], "工具名错了就**不许执行任何东西**，实得 %r" % seen2)
    ck(r2["steps"] and "没有这个工具" in r2["steps"][0]["error"],
       "错要说清，实得 %r" % (r2["steps"][0] if r2["steps"] else None))
    ck("调用没成立" in posts2[-1]["content"], "错误要回填给模型让它改")

    # ---- 执行层返回 None / 抛异常：都不许把 loop 拖崩 ----
    posts3 = []

    def fake_chat3(messages, provs=None, need=None, model=None, tools=None,
                   quota=None, account="", tool_choice=None, **kw):
        posts3.append(messages[-1])
        if len(posts3) == 1:
            return {"ok": True, "used": "p", "used_model": "m", "content": "",
                    "tokens": 1, "why": [],
                    "raw": _mkresp([("screen.read", {}), ("pc.usage", {"day": "x"}),
                                    ("pc.state", {})])}
        return {"ok": True, "used": "p", "used_model": "m", "content": "收尾",
                "tokens": 1, "why": [],
                "raw": {"choices": [{"message": {"role": "assistant",
                                                 "content": "收尾"}}]}}

    P.chat = fake_chat3
    try:
        r3 = run("看屏幕再执行个命令", executor, caps=caps, index=index,
                 quota=P.Quota(":memory:"))
    finally:
        P.chat = orig
    ck(r3["ok"] is True, "执行层出问题也不许整体失败")
    ck(len(r3["steps"]) == 3, "三个调用都要有记录，实得 %d" % len(r3["steps"]))
    ck(r3["steps"][0]["ok"] is False, "执行层返回 None 要判失败")
    ck("RuntimeError" in r3["steps"][1]["error"], "异常要变成可读的错误，实得 %r"
       % r3["steps"][1]["error"])
    ck(r3["steps"][2]["ok"] is True, "第三个该正常")

    # ---- 权限边界：能力表里真有 pc.shell，但默认只给了只读 ⇒ 不许执行 ----
    # 这条最要命：只查「能力存不存在」就放行，等于把「默认只给只读」拆了。
    seen5 = []
    posts5 = []

    def fake_chat5(messages, provs=None, need=None, model=None, tools=None,
                   quota=None, account="", tool_choice=None, **kw):
        posts5.append(messages[-1])
        if len(posts5) == 1:
            return {"ok": True, "used": "p", "used_model": "m", "content": "",
                    "tokens": 1, "why": [],
                    "raw": _mkresp([("pc.shell", {"cmd": "rm -rf /"})])}
        return {"ok": True, "used": "p", "used_model": "m", "content": "不执行",
                "tokens": 1, "why": [],
                "raw": {"choices": [{"message": {"role": "assistant",
                                                 "content": "不执行"}}]}}

    def ex5(cap, args):
        seen5.append(cap)
        return " executed"

    P.chat = fake_chat5
    try:
        r8 = run("帮我清一下磁盘", ex5, caps=caps, index=index,
                 quota=P.Quota(":memory:"))
    finally:
        P.chat = orig
    ck(seen5 == [], "**没给的工具绝对不许执行**，实得 %r" % seen5)
    ck("没给你" in r8["steps"][0]["error"], "要明说为什么不给，实得 %r"
       % r8["steps"][0]["error"])

    # ---- 缰绳：同一能力不重复调 ----
    posts4 = []

    def fake_chat4(messages, provs=None, need=None, model=None, tools=None,
                   quota=None, account="", tool_choice=None, **kw):
        posts4.append(messages[-1])
        if len(posts4) <= 2:
            return {"ok": True, "used": "p", "used_model": "m", "content": "",
                    "tokens": 1, "why": [], "raw": _mkresp([("pc.state", {})])}
        return {"ok": True, "used": "p", "used_model": "m", "content": "停",
                "tokens": 1, "why": [],
                "raw": {"choices": [{"message": {"role": "assistant", "content": "停"}}]}}

    seen4 = []

    def ex4(cap, args):
        seen4.append(cap)
        return "ok"

    P.chat = fake_chat4
    try:
        r4 = run("重复调用", ex4, caps=caps, index=index, quota=P.Quota(":memory:"))
    finally:
        P.chat = orig
    ck(seen4 == ["pc.state"], "同一能力只许调一次，实得 %r" % seen4)
    ck("已经调过" in r4["steps"][-1]["error"], "要告诉模型为什么没再调")

    # ---- 缰绳：轮次上限 ----
    tools_log = []

    def always_call(messages, provs=None, need=None, model=None, tools=None,
                    quota=None, account="", tool_choice=None, **kw):
        tools_log.append(tools)
        return {"ok": True, "used": "p", "used_model": "m", "content": "",
                "tokens": 1, "why": [], "raw": _mkresp([("pc.state", {})])}

    P.chat = always_call
    try:
        r5 = run("无限循环", ex4, caps=caps, index=index, quota=P.Quota(":memory:"),
                 max_rounds=3)
    finally:
        P.chat = orig
    ck(r5["rounds"] == 3, "轮次要被 max_rounds 卡住，实得 %r" % r5["rounds"])
    ck(r5["ok"] is True, "卡住也要能体面收尾")
    # 卡住之后那次收尾**不带工具表**——再给一次工具表，它就会再要一次工具
    ck(len(tools_log) == 4 and tools_log[-1] is None,
       "轮次用尽后收尾必须不带工具表，实得 %r" % tools_log)

    # ---- 缰绳：预算 ----
    def slow(messages, provs=None, need=None, model=None, tools=None,
             quota=None, account="", tool_choice=None, **kw):
        time.sleep(0.02)
        return {"ok": True, "used": "p", "used_model": "m", "content": "",
                "tokens": 1, "why": [], "raw": _mkresp([("pc.state", {})])}

    P.chat = slow
    try:
        r6 = run("慢", ex4, caps=caps, index=index, quota=P.Quota(":memory:"),
                 budget_s=0)
    finally:
        P.chat = orig
    ck(r6.get("budget") is True, "0 预算必须一轮都不跑")
    ck(r6["steps"] == [], "超预算不许留下步骤")

    # ---- 上游失败要透传，不许伪装成「没有工具」 ----
    def dead(messages, provs=None, need=None, model=None, tools=None,
             quota=None, account="", tool_choice=None, **kw):
        return {"ok": False, "err": "没有可用 provider", "why": ["都死了"]}

    P.chat = dead
    try:
        r7 = run("上游挂了", ex4, caps=caps, index=index, quota=P.Quota(":memory:"))
    finally:
        P.chat = orig
    ck(r7["ok"] is False and "没有可用 provider" in r7["err"],
       "上游失败要如实透传，实得 %r" % r7.get("err"))

    # ---- 自定义能力表（小模型撑不住 85 个工具时要能收窄） ----
    small = [Capability("demo.echo", "回声",
                        params=(Param("text", "str", True, "要说的话"),))]
    P.chat = fake_chat
    try:
        pass
    finally:
        P.chat = orig
    tt = TS.openai_tools(small)
    ck(len(tt) == 1 and tt[0]["function"]["parameters"]["required"] == ["text"],
       "收窄后的工具表要对")

    if fails:
        print("agentloop selftest: FAIL")
        for f in fails:
            print("  x " + f)
        return 1
    print("agentloop selftest: OK")
    return 0


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()

    # 真跑：默认只读工具表 + 本机能力表，执行层走 Registry
    q = argv[0] if argv else "这台机器现在怎么样"
    from fleet import catalog
    # 必须走 catalog.build()：注册表要有**设备声明**才算「有人能做这项能力」，
    # 手工 register 一堆 Capability 但没设备，owners() 是空的，
    # 每次调用都会死在「没有任何设备声明能做 xxx」——看起来像能力坏了。
    reg = catalog.build()

    def fire(phase, payload):
        print("  · [%s] %s" % (phase, payload))

    r = run(q, lambda cap, args: reg.call(cap, args=args), on_event=fire)
    print("used=%s model=%s rounds=%s" % (r.get("used"), r.get("used_model"),
                                          r.get("rounds")))
    for s in r["steps"]:
        print("  步骤 r%s %s ok=%s %s" % (s["round"], s["cap"], s["ok"],
                                          (s["text"] or s["error"])[:120]))
    print("answer:", r.get("answer") or r.get("err"))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

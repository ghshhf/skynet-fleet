"""能力表 → 云端模型的 function-calling 工具表（OpenAI / Anthropic 两种形状）。

为什么在 `mcp/schema.py` 之外还要这一层：

同一个能力表有三个出口，每个出口的信封都不一样——

    MCP 客户端（IDE / 别的机器）  {name, description, inputSchema}
    OpenAI 兼容（freellmapi / zen-gate / dsh 的 chat 口）
                                 {"type":"function",
                                  "function":{name, description, parameters}}
    Anthropic 原生（dsh 的 /v1/messages）
                                 {name, description, input_schema}   ← snake_case

形状不同而已，**内容与规则必须同源**。所以名字映射、参数 schema、安全过滤
一律复用 `mcp/schema.py`，本模块只做两件事：

  1. 换信封（按 flavor 包成那家的形状）；
  2. 读回信（把模型吐回来的 tool_call / tool_use 解析成「能力名 + 参数」）。

为什么不在 `providers.py` 里顺手写了：
providers 管「用哪条车道」，本模块管「给模型看什么、模型说的话怎么读回来」。
混在一起的结果是改一家协议要动额度代码——分开才改得动。

安全口径（和 mcp/schema 同一套，**不许有第二套**）：

  · 默认只导出只读能力（149 项里 85 项）。有副作用的一律不出现在云端工具表里，
    除非显式 `include_unsafe=True`。理由：云模型在别人的机房里，工具表一旦给出去，
    它调什么不归本机管；而本仓红线是「动手能力必须过审批闸门」。
  · **模型给的参数是不可信数据**：`validate_args` 逐个按 Param 类型校验，
    缺 required 就把错误回给模型（不是崩，也不是替它悄悄补默认值）。
  · 本模块**不执行任何东西**。它只解析出（能力名, 参数），执行交给
    `Registry.call`，闸门在那一层（无 gate 即 fail-closed 拒绝）。
    本模块不碰 adb、不碰网络、不碰文件。
"""
import json
import re

from .mcp.schema import (ToolIndex, input_schema, is_valid_mcp_name,
                         to_mcp_name)
from .core.registry import Capability, Param

#: 两种信封。
FLAVOR_OPENAI = "openai"
FLAVOR_ANTHROPIC = "anthropic"
FLAVORS = (FLAVOR_OPENAI, FLAVOR_ANTHROPIC)

#: 工具结果回填进上下文的上限（字符）。
#: 与 agent_tools.MAX_RESULT_CHARS 同一个数：一次搜索塞 31 万字撑爆 4096
#: 上下文的教训（HTTP 400），这里再防一道——云模型窗口更小、按 token 花钱。
MAX_RESULT_CHARS = 1200


class ToolCall:
    """模型说要调的一个工具。

    `cap` 是**能力真名**（`screen.read`）——由 ToolIndex 查表反解得到，
    不是把下划线换回点号那种有损反解（那种解在 `a_b` 和 `a.b` 同时存在时会指错）。

    `error` 非空表示这一条**不可用**（工具表里没这个名 / 参数不合法）。
    调用方必须把 error 原样回给模型，而不是静默跳过——模型不知道自己叫错了
    名字，跳过它只会换个名字再错一次。

    `note` 是「能用，但有话要说」（例如模型多传了几个不认识的键，已忽略）。
    它不进 error，因为那不是失败；也不该被静默吞掉，因为「我传了但没生效」
    是最难查的一类 bug。
    """

    __slots__ = ("id", "tool", "cap", "args", "error", "note")

    def __init__(self, cid="", tool="", cap="", args=None, error="", note=""):
        self.id = cid
        self.tool = tool          # 模型用的工具名（pc_state）
        self.cap = cap            # 本仓能力名（pc.state）
        self.args = args or {}
        self.error = error
        self.note = note

    def as_dict(self):
        return {"id": self.id, "tool": self.tool, "cap": self.cap,
                "args": self.args, "error": self.error, "note": self.note}

    def __repr__(self):
        if self.error:
            return f"<ToolCall {self.tool}: 不可用（{self.error}）>"
        return f"<ToolCall {self.cap} args={self.args}>"


# ---------------------------------------------------------------- 工具表（发出去）

def _desc_of(cap):
    """工具描述：标题 + note + 能力真名。

    为什么要带上能力真名：模型拿到的是 `pc_state`，报错日志里写的是 `pc.state`。
    两边名字不一致时，查错的人要在脑子里做一次映射——把真名写进描述里，
    日志和工具表就能直接对上。
    """
    d = cap.title or cap.name
    if cap.note:
        d = f"{d}。{cap.note}"
    return f"{d}（本仓能力名 {cap.name}）"


def _visible(cap, include_unsafe, owners):
    """这条能力该不该出现在给云的工具表里。"""
    if not include_unsafe and (cap.side_effect or cap.destructive):
        return False
    if owners is not None and cap.name not in owners:
        return False
    return True


def openai_tools(caps, include_unsafe=False, owners=None):
    """能力表 → OpenAI function-calling 的 `tools` 数组。

    形状按 OpenAI 规范：外层 `{"type":"function","function":{...}}`，
    参数 schema 放在 `function.parameters`（不是 MCP 的 `inputSchema`）。
    """
    out = []
    for cap in caps:
        if not _visible(cap, include_unsafe, owners):
            continue
        out.append({
            "type": "function",
            "function": {
                "name": to_mcp_name(cap.name),
                "description": _desc_of(cap),
                "parameters": input_schema(cap),
            },
        })
    out.sort(key=lambda t: t["function"]["name"])
    return out


def anthropic_tools(caps, include_unsafe=False, owners=None):
    """能力表 → Anthropic 原生 `tools` 数组。

    与 OpenAI 只有两处不同，但**两处都会让请求被拒**：
      · 参数 schema 的键是 `input_schema`（snake_case），不是 `parameters`；
      · 外层没有 `{"type":"function"}` 那层壳。
    """
    out = []
    for cap in caps:
        if not _visible(cap, include_unsafe, owners):
            continue
        out.append({
            "name": to_mcp_name(cap.name),
            "description": _desc_of(cap),
            "input_schema": input_schema(cap),
        })
    out.sort(key=lambda t: t["name"])
    return out


def tools_for(flavor, caps, include_unsafe=False, owners=None):
    """按 flavor 出工具表。未知 flavor 抛错（不给「默认按 OpenAI 处理」的窗口）。"""
    if flavor == FLAVOR_OPENAI:
        return openai_tools(caps, include_unsafe=include_unsafe, owners=owners)
    if flavor == FLAVOR_ANTHROPIC:
        return anthropic_tools(caps, include_unsafe=include_unsafe, owners=owners)
    raise ValueError(f"未知 flavor：{flavor!r}（只认 {FLAVORS}）")


# ---------------------------------------------------------------- 工具表收窄
# 为什么必须收窄（11-01 实测，不是理论）：
#   全量 204 条工具表 = **93.1K 字符 ≈ 26.6K token**，一次问答就烧掉这么多。
#   同一句话做对照：
#     给 1 条工具  → 368 token、1.9s、**正确**调 echo
#     给 204 条    → 26,601 token、4.2s、**调错**（调了个 llmgw_compress）
#   也就是说：大工具表不只贵 72 倍，它还会**把模型淹掉**——工具越多，选得越不准。
#   Pi（115k star 的终端代理）默认只给 4 个工具，同一个道理。
#
# 所以工具表不是「能力清单」，是**这一轮允许动的手**。少而准 > 多而全。

#: 底座工具：不管问什么都给。它们是「先看看情况」的最小集合，
#: 缺了它们模型只能瞎猜（实测就是这样：问机器状态，它说「查不到」）。
HOT_BASE = ("pc.state", "apps.list", "app.foreground", "screen.read",
            "screen.look", "tree.read", "jobs.list", "node.list")

#: 默认上限。18 条 ≈ 2.3K token，实测这个量级模型选得准。
DEFAULT_TOOL_LIMIT = 18

#: 重型/专业域：不是日常能力，只在**明确问到时**才该占位子。
#:
#: 为什么单独列（11-01 实测）：问「查一下 github.com 的解析」，18 条表里挤进 10 条
#: `rea.*`（英文长名的逆向套件，Frida/GDB/objdump/Rizin 那一整套）。它们因为描述里
#: 有「解析」「application」这类字被中文 2-gram 撞上——**语言不通导致的假命中**。
#: 模型拿到这种表，等于在通用问题上先被塞了十把手术刀。
HEAVY_PREFIXES = ("pentest.", "re.", "rizin.", "tun.", "nat.", "rdv.",
                  "card.", "render.", "mqtt.", "radio.")
#: 重型域在一张表里最多占几个位子。明确问逆向时它照样能进，只是不挤满。
HEAVY_QUOTA = 3


def _is_heavy(name):
    return str(name).startswith(HEAVY_PREFIXES)


def _q_grams(question):
    """把一句话拆成可用于匹配的小碎片（中英通吃）。

    没有分词器，用 2 字滑窗——中文最短的有效词就是两个字（"截屏""前台""电量"），
    英文按词切。宁可粗一点：匹配多算几个候选，好过漏掉真正该给的那只手。
    """
    q = re.sub(r"[^\w\u4e00-\u9fff]+", " ", (question or "").lower())
    grams = set()
    for tok in q.split():
        if len(tok) <= 2:
            grams.add(tok)
        else:
            grams.add(tok)
            for i in range(len(tok) - 1):
                grams.add(tok[i:i + 2])
    return {g for g in grams if g}


def pick_caps(caps, question="", limit=DEFAULT_TOOL_LIMIT, must=HOT_BASE):
    """按问题挑「这一轮该给模型看的能力子集」。

    规则（按优先级）：
      1. `must` 里的底座能力永远进（存在才进，名字打错不报错——它不是权限声明）；
      2. 问题碎片命中 title/note/name 的加分，命中 name 的**段**再加权
         （`app.foreground` 对「前台」这种词命中更硬）；
      3. 危险/有副作用的能力跳过（与 `_visible` 同口径，别在这儿另立一套）；
      4. 分数相同的按原顺序，保证同一问题每次拿到的表**稳定**（不稳定会让模型反复横跳）。

    返回 list[Capability]，**保持传入顺序**（工具表排序稳定，diff 好读）。
    """
    caps = list(caps or [])
    if not caps:
        return []
    # 先折叠再打分：模型看到的是动作，评分也该按**动作名**算
    # （"pc.screen" 的 screen 段比 "host.screen" 更能命中「截屏/看看屏幕」这类问法）。
    # `must` 同步映射：HOT_BASE 里写的 screen.read 折叠后成了 phone.screen，
    # 不映射就会「底座全部找不到」——那是最糟的退步，模型连基本盘都没了。
    caps = present_caps(caps)
    limit = max(1, int(limit or DEFAULT_TOOL_LIMIT))
    by_name = {c.name: c for c in caps}
    chosen = set()
    for n in (must or ()):
        c = by_name.get(n) or by_name.get(action_of(n))
        if c is not None and not (c.side_effect or c.destructive):
            chosen.add(c.name)
    if len(chosen) >= limit:
        return [c for c in caps if c.name in chosen][:limit]

    grams = _q_grams(question)
    # 中文口语会在词里塞量词/助词（"截**个**屏"、"看**下**电量"），2 字滑窗切出来是
    # 「截个/个屏」，跟「截屏」永远对不上。单字兜底只为**把明显相关的拉进候选**，
    # 权重故意低（0.3），不许抢掉精确命中的位子。
    qchars = {ch for ch in (question or "") if "\u4e00" <= ch <= "\u9fff"}
    scored = []
    for i, c in enumerate(caps):
        if c.name in chosen:
            continue
        if c.side_effect or c.destructive:
            continue
        hay = " ".join([c.name.replace(".", " "), c.title or "", c.note or ""]).lower()
        score = 0
        for g in grams:
            if g in hay:
                score += 1
        if score:
            for seg in c.name.split("."):
                if seg and seg.lower() in grams:
                    score += 2
        # 单字兜底：至少命中两个字才算，防「的/了/看」这种虚字单独把一堆拉进来。
        # 上限 0.6 —— **必须小于一次精确 2-gram 命中的 1 分**，否则单字噪音会
        # 把精确命中挤下去（实测 0.3×4=1.2 时，问「查解析」拉进来 voice.interpret、
        # memory.pack.render 这些半点关系没有的）。
        if qchars:
            ch_hit = sum(1 for ch in qchars if ch in hay)
            if ch_hit >= 2:
                score += 0.2 * min(ch_hit, 3)
        if score:
            scored.append((score, -i, c))     # -i：同分时保持原顺序
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    heavy = 0
    for _s, _i, c in scored:
        if len(chosen) >= limit:
            break
        if _is_heavy(c.name):
            # 重型域限流：不是不让进，是不许把日常问题的表挤满。
            # 得分门槛更高（要求 ≥2，即精确命中过），且数量封顶。
            if _s < 2 or heavy >= HEAVY_QUOTA:
                continue
            heavy += 1
        chosen.add(c.name)
    return [c for c in caps if c.name in chosen]


# ---------------------------------------------------------------- 呈现层（折叠 + 动作名）

def present_caps(caps):
    """工具表对外呈现：**每个动作只出现一条**，名字用动作名，不用实现名。

    为什么这一步是必须的（11-01 盘点）：

        357 项能力里，同一件事有好几套实现且参数名还不一样：
            pcctl.input(keys=...)     vs  host.input(key=...)
            osint.dns(domain=...)     vs  web.dns(url=...)
            pc.state / pcctl.sysinfo / host.monitor   —— 三个都答「本机怎么样」
        模型面对这些不是「多了几个选择」，而是**多了几个能抓错的手**。

    为什么用动作名而不是主实现名：
        `host.screen` / `pcctl.screenshot` / `pcctl.look` 是**手段**，
        「看本机屏幕」才是**意图**。工具表该写意图——手段是可以换的
        （实测 pcctl.look 就是坏的，host.screen 才出预览），把它写进工具表
        等于把可替换的实现细节焊死给模型看。

    折叠只影响「给模型看什么」：能力本身、设备声明、审批档一概不动。
    """
    from . import groups as G
    out = []
    for c in G.collapse(caps):
        n = getattr(c, "name", "")
        a = G.canonical(n)
        if not a or a == n:
            out.append(c)
            continue
        try:
            from dataclasses import replace
            c = replace(c, name=a, title=G.label(a) or c.title)
        except Exception:
            pass                      # 换不动就留主名，绝不因为改标签把能力弄丢
        out.append(c)
    return out


def action_of(name):
    """工具名 → 动作名（不认识的返回原名）。执行层用它把动作名翻回实现链。"""
    from . import groups as G
    return G.canonical(name) or name


def index_caps(caps):
    """给 ToolIndex 用的能力表：**真名 + 动作名都要能解析**。

    工具表发给模型的是动作名（`pc_screen`），模型回话里也只会说这个。
    若 ToolIndex 只按真名建，`pc_screen` 会被判成「叫错名字」——工具明明给了，
    模型一调就被拒，看起来像它不会用工具。
    反过来真名也得留着：模型记得老名字、或者别的入口（MCP）用的是真名。
    两边都要认，且动作名不许覆盖真名（`pc.state` 既是动作名又是真名，留着真身）。
    """
    caps = list(caps or [])
    have = {getattr(c, "name", "") for c in caps}
    extra = [c for c in present_caps(caps) if getattr(c, "name", "") not in have]
    return caps + extra


# ---------------------------------------------------------------- 回信（读回来）

def validate_args(cap, args):
    """按 Param 表校验模型给的参数。返回 (ok, 清洗后的 dict 或 错误文本)。

    三件事，全都是「模型给的东西不可信」这条引出来的：
      · 类型不对 → 尽力转（"3" → 3），转不了就报错回给模型；
      · required 缺了 → 报错，不替它补默认值（补了就是替模型做决定）；
      · 表里没有的字段 → **丢掉**，并在错误里说明丢了什么。
        不严拒是因为模型爱加 `reasoning` 之类的自用字段；不静默吞是因为
        「我传了但没生效」是最难查的一类 bug。
    """
    args = args if isinstance(args, dict) else {}
    known = {p.name: p for p in cap.params}
    cleaned = {}
    dropped = []
    for k, v in args.items():
        p = known.get(k)
        if p is None:
            dropped.append(k)
            continue
        if p.type == "int":
            try:
                v = int(v)
            except (TypeError, ValueError):
                return False, f"参数 {k} 要整数，收到 {v!r}"
        elif p.type == "float":
            try:
                v = float(v)
            except (TypeError, ValueError):
                return False, f"参数 {k} 要数字，收到 {v!r}"
        elif p.type == "bool":
            if isinstance(v, bool):
                pass
            elif isinstance(v, str):
                v = v.strip().lower() in ("1", "true", "yes", "y", "on")
            else:
                v = bool(v)
        elif p.type == "json":
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except ValueError:
                    return False, f"参数 {k} 要 JSON，收到 {v!r}"
        else:                                   # str / 未知类型
            if not isinstance(v, str):
                v = str(v)
        cleaned[k] = v

    missing = [p.name for p in cap.params if p.required and p.name not in cleaned]
    if missing:
        return False, f"缺必填参数：{'、'.join(missing)}"
    if dropped:
        # 这不是失败：参数能用，只是多了几个不认识的键。
        # 记进 error 让调用方有机会回给模型，避免它以为自己传成功了。
        return True, {"args": cleaned, "note": f"已忽略多余参数：{'、'.join(dropped)}"}
    return True, cleaned


def _finish(call, index, validate=True):
    """把 (工具名, 原始参数) 收成一条 ToolCall：查表 → 校验。"""
    cap = index.resolve(call.tool) if index else None
    if cap is None:
        call.error = f"没有这个工具：{call.tool}"
        call.args = {}
        return call
    call.cap = cap.name
    if not validate:
        return call
    ok, res = validate_args(cap, call.args)
    if not ok:
        call.error = res
        call.args = {}
        return call
    if isinstance(res, dict) and "args" in res:      # 带「忽略多余参数」说明
        call.args = res["args"]
        call.note = res.get("note", "")
    else:
        call.args = res
    return call


def extract_calls(resp, flavor=FLAVOR_OPENAI, index=None, validate=True):
    """从模型响应里抽出工具调用。返回 `list[ToolCall]`。

    OpenAI 给的是 `choices[0].message.tool_calls`，`function.arguments` 是
    **JSON 字符串**（不是 dict）；Anthropic 给的是 `content` 块数组里的
    `{"type":"tool_use","id","name","input"}`，`input` 已经是对象。

    两个都覆盖，因为 dsh 说自己是双协议，走 `/v1/messages` 时给的是后者。

    解不出来的（不是这两家的形状、没有 tool_calls 字段）一律返回空列表，
    **不抛、不猜**——模型只是这轮没调工具而已，不是错误。
    """
    if not isinstance(resp, dict):
        return []
    if index is None:
        index = ToolIndex([])          # 空索引 ⇒ 每条都变成「没有这个工具」

    calls = []
    if flavor == FLAVOR_OPENAI:
        try:
            msg = resp["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            return []
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw = fn.get("arguments", "")
            if isinstance(raw, str):
                try:
                    args = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    c = ToolCall(cid=tc.get("id", ""),
                                 tool=fn.get("name", ""))
                    c.error = f"参数不是合法 JSON：{raw[:80]}"
                    calls.append(c)
                    continue
            else:
                args = raw or {}
            if not isinstance(args, dict):
                args = {}
            calls.append(_finish(ToolCall(
                cid=tc.get("id", ""), tool=fn.get("name", ""),
                args=args), index, validate))
    elif flavor == FLAVOR_ANTHROPIC:
        blocks = resp.get("content")
        if not isinstance(blocks, list):
            return []
        for b in blocks:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            args = b.get("input") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            calls.append(_finish(ToolCall(
                cid=b.get("id", ""), tool=b.get("name", ""),
                args=args), index, validate))
    else:
        raise ValueError(f"未知 flavor：{flavor!r}")
    return calls


def has_calls(resp, flavor=FLAVOR_OPENAI):
    """这轮响应里有没有工具调用（不解析参数，只看有没有）。

    单独给这个函数是因为：调用方常常只需要知道「要不要进工具循环」，
    而解析要走索引、要校验参数——为了回答一个是/否去建索引不划算。
    """
    if not isinstance(resp, dict):
        return False
    if flavor == FLAVOR_OPENAI:
        try:
            return bool(resp["choices"][0]["message"].get("tool_calls"))
        except (KeyError, IndexError, TypeError):
            return False
    if flavor == FLAVOR_ANTHROPIC:
        blocks = resp.get("content")
        return isinstance(blocks, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks)
    raise ValueError(f"未知 flavor：{flavor!r}")


# ---------------------------------------------------------------- 结果回填

def clip_result(text, limit=MAX_RESULT_CHARS):
    """工具结果截断到上限。

    截断而不是报错：一次 `web.search` 回 31 万字不是它的错，是上下文装不下。
    但要**如实说截了多少**，否则模型会以为自己看到了全部。
    """
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[:limit] + f"…（已截断，原长 {len(t)} 字符）"


def tool_result_messages(calls, results, flavor=FLAVOR_OPENAI):
    """把执行结果包成「回填给模型」的消息。

    OpenAI：一条 `{"role":"tool","tool_call_id":...,"content":...}` 对应一个调用，
    **并且必须按 id 对上**——对不上模型会当成孤儿消息直接报错。
    Anthropic：塞进 user 消息的 content 块里，类型是 `tool_result`，带 `tool_use_id`。

    `results` 按调用 id 或工具名取（先按 id，取不到再按工具名），
    取不到的填「执行层没有返回结果」——不编造内容。
    """
    by_id = {}
    for c in calls:
        if c.id and c.id in results:
            by_id[c.id] = results[c.id]
        elif c.tool in results:
            by_id[c.id or c.tool] = results[c.tool]

    if flavor == FLAVOR_OPENAI:
        out = []
        for c in calls:
            r = results.get(c.id) or results.get(c.tool) or "执行层没有返回结果"
            out.append({"role": "tool", "tool_call_id": c.id,
                        "content": clip_result(r if isinstance(r, str) else json.dumps(
                            r, ensure_ascii=False))})
        return out
    if flavor == FLAVOR_ANTHROPIC:
        blocks = []
        for c in calls:
            r = results.get(c.id) or results.get(c.tool) or "执行层没有返回结果"
            blocks.append({"type": "tool_result", "tool_use_id": c.id,
                           "content": clip_result(r if isinstance(r, str) else json.dumps(
                               r, ensure_ascii=False))})
        return [{"role": "user", "content": blocks}]
    raise ValueError(f"未知 flavor：{flavor!r}")


# ---------------------------------------------------------------- 自测

def _selftest():
    from fleet.catalog import CAPABILITIES

    caps = list(CAPABILITIES)
    index = ToolIndex(caps)

    # ---- 全仓真能力表都能投影，且名字合规 ----
    assert len(caps) >= 149, f"能力数变少了（{len(caps)}），catalog 是不是没加载全"
    # 只读可导出数量由 catalog 现算，不写死。
    # 写死过一次就过期一次：85 -> 178，逆向/爬虫/渗透三批能力加进来后当场炸。
    expected = sum(1 for c in caps if _visible(c, False, None))
    for flavor in FLAVORS:
        tools = tools_for(flavor, caps)
        assert len(tools) == expected, \
            f"{flavor} 默认只该导出只读能力，实得 {len(tools)}（按 catalog 应为 {expected}）"
        for t in tools:
            n = t["function"]["name"] if flavor == FLAVOR_OPENAI else t["name"]
            assert is_valid_mcp_name(n), f"{flavor} 工具名不合规：{n}"

    # ---- 两种信封的差异别写错（写错就是请求被拒） ----
    ot = openai_tools(caps)
    at = anthropic_tools(caps)
    assert ot[0]["type"] == "function"
    assert "parameters" in ot[0]["function"] and "inputSchema" not in ot[0]["function"]
    assert "input_schema" in at[0] and "parameters" not in at[0]
    assert [t["function"]["name"] for t in ot] == [t["name"] for t in at], "两边顺序要一致"

    # ---- include_unsafe 才给副作用能力 ----
    assert len(openai_tools(caps, include_unsafe=True)) == len(caps)
    unsafe_names = {to_mcp_name(c.name) for c in caps
                    if c.side_effect or c.destructive}
    assert not (unsafe_names & {t["function"]["name"] for t in ot})
    assert unsafe_names <= {t["function"]["name"]
                            for t in openai_tools(caps, include_unsafe=True)}

    # ---- owners 过滤：一台设备只看见自己会的 ----
    assert len(openai_tools(caps, owners={"screen.read"})) == 1

    # ---- 未知 flavor 抛错，不静默按 OpenAI 处理 ----
    try:
        tools_for("gemini", caps)
    except ValueError as e:
        assert "未知 flavor" in str(e)
    else:
        raise AssertionError("未知 flavor 应该抛")

    # ---- OpenAI 回信：arguments 是 JSON 字符串，要解成能力真名 ----
    resp = {"choices": [{"message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "screen_read",
                                         "arguments": "{}"}}]}}]}
    assert has_calls(resp) is True
    calls = extract_calls(resp, FLAVOR_OPENAI, index)
    assert len(calls) == 1
    assert calls[0].cap == "screen.read", calls[0].cap
    assert calls[0].tool == "screen_read"
    assert calls[0].id == "call_1"
    assert calls[0].error == ""

    # ---- Anthropic 回信：content 块里的 tool_use ----
    aresp = {"content": [{"type": "text", "text": "看下屏幕"},
                         {"type": "tool_use", "id": "tu_9",
                          "name": "pc_state", "input": {"clip": True}}]}
    assert has_calls(aresp, FLAVOR_ANTHROPIC) is True
    acalls = extract_calls(aresp, FLAVOR_ANTHROPIC, index)
    assert len(acalls) == 1 and acalls[0].cap == "pc.state"
    assert acalls[0].args == {"clip": True}

    # ---- 没调工具 ⇒ 空列表，不抛 ----
    assert extract_calls({"choices": [{"message": {"content": "你好"}}]},
                         FLAVOR_OPENAI, index) == []
    assert has_calls({"choices": [{"message": {"content": "你好"}}]}) is False
    assert extract_calls("not a dict", FLAVOR_OPENAI, index) == []

    # ---- 叫错名字：如实报错，不许猜、不许静默 ----
    bad = {"choices": [{"message": {"tool_calls": [
        {"id": "c2", "function": {"name": "no_such_tool", "arguments": "{}"}}]}}]}
    bc = extract_calls(bad, FLAVOR_OPENAI, index)
    assert len(bc) == 1 and "没有这个工具" in bc[0].error

    # ---- 参数校验 ----
    cap = Capability("demo.echo", "回声",
                     params=(Param("text", "str", True, "要说的话"),
                             Param("n", "int", doc="几次"),
                             Param("flag", "bool")))
    ok, r = validate_args(cap, {"text": "hi", "n": "3"})
    assert ok and r == {"text": "hi", "n": 3}, r          # 字符串 "3" 转成 int 3
    ok, r = validate_args(cap, {"n": 1})                  # 缺必填
    assert not ok and "缺必填参数" in r
    ok, r = validate_args(cap, {"text": "hi", "n": "abc"})
    assert not ok and "要整数" in r
    ok, r = validate_args(cap, {"text": "hi", "flag": "true"})
    assert ok and r["flag"] is True
    ok, r = validate_args(cap, {"text": "hi", "reasoning": "想了一下"})
    assert ok and isinstance(r, dict) and "args" in r      # 多余参数忽略 + 说明
    assert r["args"] == {"text": "hi"} and "reasoning" in r["note"]
    ok, r = validate_args(cap, "not a dict")               # 不是 dict 也不崩
    assert not ok

    # ---- 参数不是合法 JSON ----
    broken = {"choices": [{"message": {"tool_calls": [
        {"id": "c3", "function": {"name": "screen_read", "arguments": "{oops"}}]}}]}
    pc = extract_calls(broken, FLAVOR_OPENAI, index)
    assert "不是合法 JSON" in pc[0].error

    # ---- 结果回填：OpenAI 按 id 对，Anthropic 走 tool_result 块 ----
    omsg = tool_result_messages(calls, {"call_1": "屏幕上是桌面"}, FLAVOR_OPENAI)
    assert omsg[0]["role"] == "tool" and omsg[0]["tool_call_id"] == "call_1"
    assert omsg[0]["content"] == "屏幕上是桌面"
    amsg = tool_result_messages(acalls, {"tu_9": "负载 12%"}, FLAVOR_ANTHROPIC)
    assert amsg[0]["role"] == "user"
    assert amsg[0]["content"][0]["type"] == "tool_result"
    assert amsg[0]["content"][0]["tool_use_id"] == "tu_9"

    # 没结果不编造
    assert "没有返回结果" in tool_result_messages(
        calls, {}, FLAVOR_OPENAI)[0]["content"]

    # ---- 截断要说清截了多少 ----
    long = "字" * 5000
    clipped = clip_result(long)
    assert clipped.endswith("原长 5000 字符）") and len(clipped) < 1300
    assert clip_result("短") == "短"

    # ---- 工具表收窄（pick_caps）：204 条 → 18 条 ----
    # 为什么这条必须进自测：收窄是「省钱 + 让模型选得准」的唯一手段，
    # 而它最容易悄悄退化成「全给」（谁把 limit 传成 None 就退回 204 条/26.6K token）。
    # 11-01 实测对照：1 条表 368 token 选对；204 条表 26,601 token 还选错。
    for q in ("手机现在前台是什么应用", "这台机器状态怎么样", "帮我看看装了什么应用"):
        sub = pick_caps(caps, q, limit=18)
        assert 0 < len(sub) <= 18, (q, len(sub))
        assert all(not (c.side_effect or c.destructive) for c in sub), \
            "收窄不许把危险能力带进来（与 _visible 同口径）"
    # 底座永远在：问什么都先给「看看情况」的几只眼睛
    base = {c.name for c in pick_caps(caps, "", limit=18)}
    for n in ("pc.state", "apps.list"):
        if any(c.name == n for c in caps):
            assert n in base, f"底座能力 {n} 没进任何问题的默认表"
    # 意图命中：问前台 → app.foreground 必须在
    fg = {c.name for c in pick_caps(caps, "手机现在前台是什么应用", limit=18)}
    assert "app.foreground" in fg or not any(c.name == "app.foreground" for c in caps), fg
    # 空问题 = 只给底座，不瞎猜
    assert len(pick_caps(caps, "", limit=18)) <= len(HOT_BASE) + 2, \
        "空问题不该因为「全都不匹配」而漏成一大片"
    # 顺序稳定：同一问题问两次，表必须一模一样（不然模型会反复横跳）
    a1 = [c.name for c in pick_caps(caps, "截个屏看看", limit=18)]
    a2 = [c.name for c in pick_caps(caps, "截个屏看看", limit=18)]
    assert a1 == a2, (a1, a2)

    # ---- 呈现层：动作名上桌，实现名退场（fleet/groups.py）----
    from . import groups as G
    pres = present_caps(caps)
    pnames = {c.name for c in pres}
    # ① 折叠必须真减少，且组内只留一条
    assert len(pres) < len(caps), "折叠没生效：%d → %d" % (len(caps), len(pres))
    for _a, (_l, ms) in G.ACTIONS.items():
        present = [m for m in ms if m in pnames]
        assert len(present) <= 1, "动作 %s 折叠后还有多套实现：%s" % (_a, present)
    # ② 有动作名的组，工具表里不该再出现实现名（模型不该被要求记住是谁实现的）
    assert "pc.screen" in pnames and "host.screen" not in pnames, sorted(pnames)[:5]
    assert "net.dns" in pnames and "osint.dns" not in pnames
    # ③ 动作名要有自己的 title（不能把实现名的标题照搬，那样模型仍按实现理解）
    assert G.label("pc.screen") and G.label("pc.screen") != [c for c in caps
                                                            if c.name == "host.screen"][0].title

    # ---- index 必须同时认动作名与真名（否则模型照着工具表叫，却被判「叫错名字」）----
    idx = ToolIndex(index_caps(caps))
    assert idx.resolve("pc_screen") is not None, "动作名解析不出来"
    assert idx.resolve("pc_screen").name == "pc.screen"
    assert idx.resolve("host_screen") is not None, "真名不许丢（MCP/老入口还在用）"

    # ---- 重型域限流：泛问不许被逆向套件挤满 ----
    heavy = [c.name for c in pick_caps(caps, "查一下这个域名的解析", limit=18)
             if _is_heavy(c.name)]
    assert len(heavy) <= HEAVY_QUOTA, "重型域超配额：%s" % heavy
    # 但**明确问**的时候必须给得出来（限流不是禁用）
    fr = [c.name for c in pick_caps(caps, "用 frida 挂上进程看参数", limit=18)
          if c.name.startswith("re.")]
    assert fr, "明确问逆向却一条 re 只读工具都没给"
    # re.dynamic 会附加到进程，是副作用能力：默认工具表（只读）本来就不该给，
    # 与「明确问」无关——安全边界优先于相关性。要用它走 include_unsafe。
    assert "re.dynamic" not in fr, "副作用能力不许混进默认只读工具表"

    # ---- 中文口语插量词也要能命中（"截个屏" 切不出 "截屏"）----
    shot = {c.name for c in pick_caps(caps, "帮我截个屏看看", limit=18)}
    assert "pc.screen" in shot or "phone.screen" in shot, shot

    print("fleet.toolschema selftest ok")


def main(argv=None):
    """CLI：给人看工具表，给门禁跑自测。

    --openai / --anthropic  打印那一家的工具表（JSON，可直接贴进请求体）
    --unsafe                把有副作用的能力也算进来（默认只给只读）
    --stats                 只打数字
    --selftest              跑自测（门禁走这条）
    """
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        _selftest()
        return 0

    from fleet.catalog import CAPABILITIES
    caps = list(CAPABILITIES)
    unsafe = "--unsafe" in argv
    if "--stats" in argv or not argv:
        ro = [c for c in caps if not (c.side_effect or c.destructive)]
        print("能力 %d 项（只读 %d / 有副作用 %d）" %
              (len(caps), len(ro), len(caps) - len(ro)))
        print("导出（%s）：OpenAI %d 个工具 / Anthropic %d 个工具" % (
            "含副作用" if unsafe else "仅只读",
            len(openai_tools(caps, include_unsafe=unsafe)),
            len(anthropic_tools(caps, include_unsafe=unsafe))))
        return 0

    flavor = FLAVOR_ANTHROPIC if "--anthropic" in argv else FLAVOR_OPENAI
    print(json.dumps(tools_for(flavor, caps, include_unsafe=unsafe),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

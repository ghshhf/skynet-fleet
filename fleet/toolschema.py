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
    for flavor in FLAVORS:
        tools = tools_for(flavor, caps)
        assert len(tools) == 85, f"{flavor} 默认只该导出只读能力，实得 {len(tools)}"
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

"""能力清单 → MCP 工具表。

为什么有这一层：
  MCP 规范（2025-06-18）里 tool 的 **name 必须匹配 `^[a-zA-Z0-9_-]{1,64}$`**，
  而本仓的能力名是 `domain.verb`（`pc.state`、`pcctl.clipboard.read`）——
  带点，不合规。所以要有一层**确定性的双向映射**：

      pc.state            ⇄  pc_state
      pcctl.clipboard.read ⇄  pcctl_clipboard_read

  规则只有一条：`.` → `_`。不做别的变换（不 camelCase、不加前缀），
  因为规则越简单越可逆，出问题时人肉能对得上。

  为什么不是「直接拿能力名当工具名」：
  有些 MCP 客户端会严格校验工具名，`.` 直接被拒；而且工具名会被塞进
  模型的上下文里，`pc_state` 比 `pc.state` 少一层心智负担。

  映射必须**可逆且无冲突**——否则 `a.b` 和 `a_b` 会撞成一个。
  `_selftest()` 里钉死了这条：真扫全仓能力名，撞了就炸。
"""
import re

#: MCP 规范允许的工具名字符集。
MCP_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def to_mcp_name(cap_name):
    """能力名 → MCP 工具名（`.` → `_`）。"""
    return cap_name.replace(".", "_")


def from_mcp_name(tool_name):
    """MCP 工具名 → 能力名（`_` → `.`）。

    注意：这是**有损反解**——`pc_state` 能解回 `pc.state`，但如果真存在
    一个叫 `pc_state` 的能力（带下划线），反解会指向别的能力。
    所以真要用某个工具名找能力时，必须走 `resolve_tool`（它查表，
    查不到就说「没有这个工具」，绝不猜）。
    """
    return tool_name.replace("_", ".")


def is_valid_mcp_name(name):
    """这个名字能不能直接当 MCP 工具名用。"""
    return bool(MCP_NAME_RE.match(name))


# ---------------------------------------------------------------- 参数类型

#: 本仓 Param.type → JSON Schema type。
#: 本仓只有 str / int / bool 三种（catalog.py 里 58/37/11 个），
#: json 留给以后；映射表里没有的类型一律当 string 处理并**不静默**——
#: 真出现未知类型时，schema 里照原样写进 description，由人看。
_JSON_TYPES = {
    "str": "string",
    "int": "integer",
    "bool": "boolean",
    "json": "object",
}


def _param_schema(param):
    """一个 Param → JSON Schema property。"""
    jt = _JSON_TYPES.get(param.type, "string")
    prop = {"type": jt}
    if param.doc:
        prop["description"] = param.doc
    if param.type not in _JSON_TYPES:
        prop["description"] = (
            (param.doc + " ") if param.doc else "") + \
            f"（注意：本仓参数类型 {param.type!r} 不在已知映射表里，按 string 给了 schema）"
    return prop


def input_schema(cap):
    """一条 Capability → MCP 的 `inputSchema`。

    形状按规范：`{"type":"object", "properties":{...}, "required":[...]}`。
    无参数时给 `{"type":"object", "properties":{}, "additionalProperties":false}`
    ——规范明确推荐这样写（"accepts only empty objects"），比空 properties 松的
    `{"type":"object"}` 更安全：外来客户端传了没定义的参数会被拒。
    """
    props = {}
    required = []
    for p in cap.params:
        props[p.name] = _param_schema(p)
        if p.required:
            required.append(p.name)
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    else:
        schema["additionalProperties"] = False
    return schema


# ---------------------------------------------------------------- 工具构造

def tool_for(cap, include_unsafe=False, owners=None):
    """一条 Capability → 一个 MCP tool 字典。

    `include_unsafe=False`（默认）时，**有副作用的能力不导出**。
    这不是功能缺失，是安全默认：
      · MCP 是「给别的机器用的口」，那边的模型拿到工具就能直接调；
      · 而本仓的红线 3 是「动手能力必须过审批闸门」，
        而 stdio 的 MCP server 进程**没有 GUI 弹窗可等**。
      · 所以默认只给只读口。想开动手口必须显式 include_unsafe=True，
        且那时 registry.call 没有 gate 会 fail-closed 拒绝（见 server.py）。

    `owners` 是这台设备（或这台 MCP server）支持的能力名集合；
    给了就只导出自己支持的那些（= 一台设备就是一个 MCP server）。
    """
    if not include_unsafe and (cap.side_effect or cap.destructive):
        return None
    if owners is not None and cap.name not in owners:
        return None

    name = to_mcp_name(cap.name)
    desc = cap.title or cap.name
    if cap.note:
        desc = f"{desc}。{cap.note}"
    desc += f"（本仓能力名 {cap.name}）"

    tool = {
        "name": name,
        "title": cap.title or cap.name,
        "description": desc,
        "inputSchema": input_schema(cap),
        "annotations": {
            # 只读口 ⇒ 模型调它不会改变外部世界。
            "readOnlyHint": not (cap.side_effect or cap.destructive),
            # 会改外部世界但不是不可逆的（例如开应用、发通知）。
            "destructiveHint": bool(cap.destructive),
            # 本仓能力都是「查一下 / 做一次」，不是幂等的增删。
            "idempotentHint": False,
            # 多数能力要碰真实环境（真文件、真网络）。
            "openWorldHint": True,
        },
    }
    return tool


def tool_table(caps, include_unsafe=False, owners=None):
    """一批 Capability → MCP 的 `tools` 数组（tools/list 的 result）。

    排序必须**确定**：规范建议 server 返回稳定的顺序（便于客户端缓存、
    便于模型侧 prompt cache 命中）。所以按工具名排，不按注册顺序。
    """
    out = []
    for cap in caps:
        t = tool_for(cap, include_unsafe=include_unsafe, owners=owners)
        if t is not None:
            out.append(t)
    out.sort(key=lambda t: t["name"])
    return out


class ToolIndex:
    """工具名 ⇄ 能力名的双向索引。

    为什么不做「反解 + 查表」而是**建索引**：
    `_` → `.` 的反解是有损的（`pc_state` 反解成 `pc.state`，
    但若真有能力叫 `pc_state` 就指错了）。索引是**按真名建**的，
    所以 `resolve` 一定指向真身；`resolve_mcp_name` 找不到就说找不到。
    """

    def __init__(self, caps):
        self._by_mcp = {}          # mcp 名 → Capability
        self._by_cap = {}          # 能力名 → mcp 名
        for c in caps:
            mn = to_mcp_name(c.name)
            if mn in self._by_mcp:
                raise ValueError(
                    f"工具名冲突：能力 {self._by_mcp[mn].name} 与 {c.name} "
                    f"都映射成 {mn}。映射规则只有 '.'→'_'，撞了说明能力命名有问题")
            self._by_mcp[mn] = c
            self._by_cap[c.name] = mn

    def resolve(self, tool_name):
        """工具名 → Capability。找不到返回 None。"""
        return self._by_mcp.get(tool_name)

    def resolve_cap(self, cap_name):
        """能力名 → MCP 工具名。找不到返回 None。"""
        return self._by_cap.get(cap_name)

    def names(self):
        return sorted(self._by_mcp)

    def __len__(self):
        return len(self._by_mcp)


# ---------------------------------------------------------------- 自测

def _selftest():
    # 映射可逆
    assert to_mcp_name("pc.state") == "pc_state"
    assert from_mcp_name("pc_state") == "pc.state"
    assert to_mcp_name("pcctl.clipboard.read") == "pcctl_clipboard_read"

    # 生成的工具名都合规
    for n in ("pc_state", "pcctl_clipboard_read", "adb_wifi_list"):
        assert is_valid_mcp_name(n), n
    assert not is_valid_mcp_name("pc.state")
    assert not is_valid_mcp_name("")
    assert not is_valid_mcp_name("a" * 65)

    # 参数类型映射
    from fleet.core.registry import Capability, Param
    cap = Capability("demo.echo", "回声",
                     params=(Param("text", "str", True, "要说的话"),
                             Param("n", "int", doc="几次"),
                             Param("flag", "bool")))
    s = input_schema(cap)
    assert s["type"] == "object"
    assert s["properties"]["text"]["type"] == "string"
    assert s["properties"]["n"]["type"] == "integer"
    assert s["properties"]["flag"]["type"] == "boolean"
    assert s["required"] == ["text"]
    assert "additionalProperties" not in s

    # 无参数的能力 ⇒ additionalProperties: false
    cap2 = Capability("demo.now", "现在几点")
    s2 = input_schema(cap2)
    assert s2.get("additionalProperties") is False

    # 副作用能力默认不导出
    cap3 = Capability("demo.kill", "删东西", side_effect=True, destructive=True)
    assert tool_for(cap3) is None
    assert tool_for(cap3, include_unsafe=True) is not None
    t = tool_for(cap3, include_unsafe=True)
    assert t["annotations"]["readOnlyHint"] is False
    assert t["annotations"]["destructiveHint"] is True

    # owners 过滤
    assert tool_for(cap, owners={"demo.other"}) is None
    assert tool_for(cap, owners={"demo.echo"}) is not None

    # 索引：'.→_' 是有损的，a.b 与 a_b 会撞 ⇒ 索引必须当场炸，不许静默覆盖
    try:
        ToolIndex([Capability("a.b", "x"), Capability("a_b", "y")])
    except ValueError as e:
        assert "工具名冲突" in str(e)
    else:
        raise AssertionError("a.b 与 a_b 撞名，索引应该抛错")

    # 同一个能力名重复登记也要炸
    try:
        ToolIndex([Capability("a.b", "x"), Capability("a.b", "x2")])
    except ValueError:
        pass
    else:
        raise AssertionError("同名能力应该撞出来")

    # 全仓真能力名不许撞
    from fleet.catalog import CAPABILITIES
    ToolIndex(list(CAPABILITIES))          # 撞了这里就抛

    # 工具表顺序确定
    caps = [Capability("z.b", "z"), Capability("a.a", "a")]
    t1 = tool_table(caps)
    t2 = tool_table(list(reversed(caps)))
    assert [t["name"] for t in t1] == [t["name"] for t in t2] == ["a_a", "z_b"]

    print("fleet.mcp.schema selftest ok")


if __name__ == "__main__":
    _selftest()

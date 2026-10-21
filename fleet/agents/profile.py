"""角色边界：从角色卡（chara_card v3）里读出「这个角色能调什么、能往下派几层、找谁要模型」。

为什么要角色边界（学 DeepSeek Harness 的第 3 条机制）：

  Harness 的一条硬规矩是「**上下文继承 ≠ 工具权限继承**」。子级在进程内 Spawn/Fork 里
  拿到的是**全新的扁平工具 Scope**；父级当时的工具视图、`restrict()` 的结果**不会**自动
  导入子级。Fork 带走对话历史但**不复制工具限制**。要收窄必须**显式**设 `toolFilter`。

  换成人话：子智能体天生比它爹能干的活多，权限不设边界就等于把整屋子的钥匙撒出去。
  本仓的铁律是「审批判定必须在注册表层」——所以角色的 `tool_filter` 只能**收窄**
  Registry 已有的能力集，**不能**凭空造一个新能力，也不能放宽审批。

字段放在角色卡的 `data.extensions.agent` 里（chara_card_v3 规范留了扩展位，
不破坏 SillyTavern / RisuAI / AIRI 的导入）。**不另起 YAML 目录**——一份真相。

三个字段：
  provider    : 这个角色用哪个后端出脑（如 "local-gateway" / "kimi-coding"）。缺省走默认。
  tool_filter : 允许使用的**能力名白名单**。None = 不额外收窄（但仍受注册表审批约束）。
  max_depth   : 这个角色作为父级时，**允许再往下派几层**。0 = 只能自己做，不许派生。
"""
from dataclasses import dataclass, field

# 角色卡里扩展字段的存放位置
EXT_KEY = "agent"


class ProfileError(Exception):
    """角色卡读不出角色边界。message 必须写清是哪个字段、为什么。"""


@dataclass
class RoleProfile:
    """一个角色的边界。"""

    name: str
    provider: str = ""
    tool_filter: tuple = ()          # 能力名白名单；空元组 = 不收窄
    max_depth: int = 0               # 作为父级时允许再派几层
    system_prompt: str = ""
    source: str = ""                 # 卡片来源路径（便于报错定位）

    def allows_depth(self, child_depth):
        """能不能派生第 child_depth 层的子角色（相对自身是 child_depth-1 层往下）。"""
        return (child_depth - 1) <= self.max_depth

    def permits(self, cap_name):
        """这个角色能不能用某个能力。

        tool_filter 是**收窄**用的：非空则只许白名单里的。
        注意——它**不**改变审批：能不能真动手，Registry 说了算。
        """
        if not self.tool_filter:
            return True
        return cap_name in self.tool_filter

    def to_dict(self):
        return {
            "name": self.name,
            "provider": self.provider,
            "tool_filter": list(self.tool_filter),
            "max_depth": self.max_depth,
        }


def from_card(card, source="", known_caps=None):
    """从角色卡 dict 构造 RoleProfile。

    `card` 可以是完整的 chara_card_v3（{"spec":..., "data":{...}}），
    也可以直接是 data 段。两种都收，省得调用方自己拆。

    `known_caps` 给了就把 tool_filter 里的能力名逐个校验——
    校验放在**这里**而不是只在名册层，是为了「一处真相」：
    谁调 from_card 都得到同样的保护。
    """
    if not isinstance(card, dict):
        raise ProfileError(f"角色卡不是 dict：{type(card).__name__}")

    data = card.get("data") if isinstance(card.get("data"), dict) else card
    name = (data.get("name") or "").strip()
    if not name:
        raise ProfileError("角色卡里没有 name，没法给这个角色定身份")

    ext = data.get("extensions")
    agent = {}
    if isinstance(ext, dict) and isinstance(ext.get(EXT_KEY), dict):
        agent = ext[EXT_KEY]

    provider = (agent.get("provider") or "").strip()
    if not isinstance(provider, str):
        raise ProfileError(f"角色「{name}」的 provider 必须是字符串")

    raw_filter = agent.get("tool_filter")
    if raw_filter is None:
        tf = ()
    elif isinstance(raw_filter, (list, tuple)):
        bad = [x for x in raw_filter if not isinstance(x, str)]
        if bad:
            raise ProfileError(f"角色「{name}」的 tool_filter 含非字符串项：{bad[:3]}")
        tf = tuple(dict.fromkeys(x.strip() for x in raw_filter if x.strip()))
    else:
        raise ProfileError(f"角色「{name}」的 tool_filter 必须是列表或 null，"
                           f"实际是 {type(raw_filter).__name__}")

    if known_caps is not None:
        known = set(known_caps)
        missing = [c for c in tf if c not in known]
        if missing:
            raise ProfileError(
                f"角色「{name}」的 tool_filter 里有注册表里不存在的能力："
                f"{missing[:5]}{'…' if len(missing) > 5 else ''}")

    depth = agent.get("max_depth", 0)
    if isinstance(depth, bool) or not isinstance(depth, int):
        raise ProfileError(f"角色「{name}」的 max_depth 必须是整数，实际是 {depth!r}")
    if depth < 0:
        raise ProfileError(f"角色「{name}」的 max_depth 不能是负数：{depth}")

    return RoleProfile(
        name=name,
        provider=provider,
        tool_filter=tf,
        max_depth=depth,
        system_prompt=(data.get("system_prompt") or "").strip(),
        source=source,
    )


def load(path):
    """从磁盘读一张角色卡并解析出 RoleProfile。"""
    import json
    import os
    try:
        with open(path, "r", encoding="utf-8") as f:
            card = json.load(f)
    except FileNotFoundError:
        raise ProfileError(f"角色卡不存在：{path}")
    except json.JSONDecodeError as e:
        raise ProfileError(f"角色卡不是合法 JSON：{path}（{e}）")
    return from_card(card, source=os.path.abspath(path))

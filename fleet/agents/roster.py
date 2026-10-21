"""角色名册：把 `cards/` 下的角色卡读成一组带边界的角色。

为什么有名册：角色卡是**静态的**（人设 + 边界），但「现在有哪些角色、谁能干什么、
谁归谁管」是**运行时真相**。本仓铁律是「能力唯一真相在注册表」，
所以这里只做**校验与汇总**，不新造能力、不改审批判定。

`tool_filter` 里写错能力名是**硬错误**——它意味着这个角色想去调一个不存在的能力，
跑起来才发现就晚了。名册加载时就把不存在的名字挑出来，报清楚是哪个角色、哪个名字。
"""
import os

from .profile import RoleProfile, from_card, ProfileError

CARDS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "cards")


class RosterError(Exception):
    """名册不合法。message 必须写清哪个角色、哪个字段。"""


def load_all(cards_dir=None, known_caps=None):
    """读入 cards/ 下所有角色卡，返回 {角色名: RoleProfile}。

    known_caps 给了就把 tool_filter 里的能力名逐个校验；
    不给就**跳过校验**（不静默丢弃，只是不校验）。
    """
    d = cards_dir or CARDS_DIR
    if not os.path.isdir(d):
        raise RosterError(f"角色卡目录不存在：{d}")

    out = {}
    problems = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith((".json", ".png")):
            continue
        if fn == "active.json":      # 指针文件，不是角色卡
            continue
        # 🔴 点开头的都是本仓的内部文件（下载账 .downloads.json 等），
        #    不是角色卡。实测踩过：.downloads.json 以 .json 结尾 ⇒
        #    被当角色卡读 ⇒ 报「角色卡里没有 name」⇒ 整个名册加载失败。
        if fn.startswith("."):
            continue
        path = os.path.join(d, fn)
        try:
            prof = from_card(_read_card(path), source=path, known_caps=known_caps)
        except ProfileError as e:
            problems.append(str(e))
            continue
        out[prof.name] = prof

    if problems:
        raise RosterError("；".join(problems))
    return out


def _read_json(path):
    import json
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_card(path):
    """读一张角色卡（.json 或 .png）。

    🔴 PNG 也收：从卡商店下载的卡是 PNG 格式（卡数据在 tEXt 块里），
    名册只认 .json 的话，下载来的卡就进不了名册、不能当执行角色。
    统一走 `persona_card.load_card` —— 那是本仓读卡的唯一真相，
    它认 V1/V2/V3 与已知私有 schema，陌生 schema 会**明确报错**而不是读空。
    """
    if path.lower().endswith(".png"):
        import persona_card as pc
        return pc.load_card(path)
    return _read_json(path)


def active_name(cards_dir=None):
    """读 `cards/active.json`，返回当前激活的角色名。"""
    import json
    d = cards_dir or CARDS_DIR
    p = os.path.join(d, "active.json")
    if not os.path.exists(p):
        raise RosterError(f"没有 active.json，指定不了激活哪个角色：{p}")
    with open(p, "r", encoding="utf-8") as f:
        obj = json.load(f)
    fn = obj.get("file")
    if not fn:
        raise RosterError("active.json 里没有 file 字段")
    if not fn.endswith(".json"):
        fn += ".json"
    card = _read_json(os.path.join(d, fn))
    return (card.get("data") or {}).get("name") or os.path.splitext(fn)[0]


def summary(profiles, known_caps=None):
    """把名册汇总成可展示的表（给 GUI / CLI 都能用）。"""
    known = set(known_caps or [])
    rows = []
    for name in sorted(profiles):
        p = profiles[name]
        row = {"name": p.name, "provider": p.provider or "(默认)",
               "max_depth": p.max_depth,
               "cap_allow": len(p.tool_filter) if p.tool_filter else "全部",
               "source": os.path.basename(p.source) if p.source else ""}
        if p.tool_filter and known:
            row["unknown_caps"] = [c for c in p.tool_filter if c not in known]
        rows.append(row)
    return rows

"""角色卡的变量世界：stat_data 的初始化、寻址、事务与快照。

为什么要有这一层（2026-11-03，融合 ElecKoi 的 MVU 体系）：

天网原来的 `persona_card` 只能把卡**读**出来拼提示词——卡里的世界状态
（好感度、持有物、时间线、事件标记）完全不存在。结果是：
  - 模型每轮都要从正文里猜「现在好感多少」，越聊越飘；
  - 卡的作者写了 `[InitVar]` 初始态、开场白里埋了 `<initvar>` / `<json_patch>`，
    这些数据被当成普通文本，白白丢掉；
  - 没有快照 ⇒ 想回到「第 12 条消息时」只能重开。

ElecKoi 的做法值得抄：状态是**结构化数据**，由专用工具读改写、每次写入前校验、
写坏了能退回上一个快照。本模块把那套语义落成零依赖的 Python：
  - `StoryState.from_card()` 认三种初始化来源（InitVar / initvar / json_patch）；
  - `patch()` 走事务：要么全成，要么一点不动（不是「尽量改」）；
  - `begin/commit/rollback` + 每条消息的 `snapshot()` ⇒ 可回溯。

边界：**不做 Zod**。卡里的 JS schema（registerMvuSchema）需要跑 JS 才能求值，
这里只做「结构校验 + 可选类型声明」，认不出的 schema 保留原文不猜。
"""
import copy
import json
import re

INIT_TAG_RE = re.compile(r"<initvar>(.*?)</initvar>", re.S | re.I)
PATCH_TAG_RE = re.compile(r"<json_?patch>(.*?)</json_?patch>", re.S | re.I)
INITVAR_TITLE_RE = re.compile(r"\[\s*init\s*var\s*\]", re.I)
MVU_UPDATE_TITLE_RE = re.compile(r"\[\s*mvu[_\s-]*update\s*\]", re.I)


class StateError(ValueError):
    """状态操作失败（路径不合法 / 事务没开 / 补丁结构错）。"""


# --------------------------------------------------------------------------
# 宽松 JSON：卡作者手写的初始态经常带注释和尾逗号
# --------------------------------------------------------------------------

def lenient_json(text):
    """解析一段「像 JSON」的文本；失败返回 None（不抛，让调用方决定怎么兜）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    cleaned = re.sub(r"^\s*//.*$", "", raw, flags=re.M)      # 行注释
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.S)   # 块注释
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)          # 尾逗号
    try:
        return json.loads(cleaned)
    except Exception:
        return None


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------

_SEG_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def parse_path(path):
    """`stat_data.背包[0].名字` → ['stat_data','背包',0]。

    JSON Pointer（/stat_data/好感）也认，因为很多卡的工具调用用指针写法。
    """
    p = str(path or "").strip()
    if p.startswith("/"):
        return [seg for seg in p.split("/")[1:]]
    if p in ("", "stat_data"):
        return ["stat_data"]
    segs = []
    for m in _SEG_RE.finditer(p):
        if m.group(1) is not None:
            segs.append(m.group(1))
        else:
            segs.append(int(m.group(2)))
    if not segs:
        raise StateError("路径不合法: %r" % path)
    return segs


def get_path(state, path, default=None):
    try:
        segs = parse_path(path)
    except StateError:
        return default
    cur = state
    for seg in segs:
        if isinstance(cur, dict):
            if seg not in cur:
                return default
            cur = cur[seg]
        elif isinstance(cur, list):
            if not isinstance(seg, int) or not (-len(cur) <= seg < len(cur)):
                return default
            cur = cur[seg]
        else:
            return default
    return cur


def set_path(state, path, value):
    segs = parse_path(path)
    if not segs:
        raise StateError("不能写空路径")
    # 按索引遍历：路径里可能出现重复键名，不能靠 index() 回查
    cur = state
    for i, seg in enumerate(segs[:-1]):
        nxt_is_index = isinstance(segs[i + 1], int)
        if isinstance(seg, int):
            if not isinstance(cur, list):
                raise StateError("路径 %r 在 %r 处不是数组" % (path, seg))
            while len(cur) <= seg:
                cur.append(None)
            if cur[seg] is None:
                cur[seg] = [] if nxt_is_index else {}
            cur = cur[seg]
        else:
            if not isinstance(cur, dict):
                raise StateError("路径 %r 在 %r 处不是对象" % (path, seg))
            if seg not in cur or cur[seg] is None:
                cur[seg] = [] if nxt_is_index else {}
            cur = cur[seg]
    last = segs[-1]
    if isinstance(last, int):
        if not isinstance(cur, list):
            raise StateError("路径 %r 末段是下标但容器不是数组" % path)
        while len(cur) <= last:
            cur.append(None)
        cur[last] = value
    else:
        if not isinstance(cur, dict):
            raise StateError("路径 %r 末段是键但容器不是对象" % path)
        cur[last] = value
    return state


def del_path(state, path):
    segs = parse_path(path)
    parent = get_path(state, ".".join(str(s) for s in segs[:-1])) if len(segs) > 1 else state
    last = segs[-1]
    if isinstance(parent, dict):
        if last not in parent:
            raise StateError("路径不存在: %r" % path)
        del parent[last]
    elif isinstance(parent, list) and isinstance(last, int):
        if not (-len(parent) <= last < len(parent)):
            raise StateError("路径不存在: %r" % path)
        del parent[last]
    else:
        raise StateError("路径不存在: %r" % path)
    return state


# --------------------------------------------------------------------------
# 卡 → 初始状态
# --------------------------------------------------------------------------

def _unwrap(x):
    """容错：允许直接传 V3 包装体（{"spec":..., "data": {...}}）。"""
    if isinstance(x, dict) and isinstance(x.get("data"), dict) \
            and ("spec" in x or "spec_version" in x):
        return x["data"]
    return x if isinstance(x, dict) else {}


def init_sources(card_data):
    """扫出卡里所有「状态初始化」的来源，按语义分好类。

    返回 (world_entries, update_rules)：
      world_entries  [(标题, JSON 文本)]  —— [InitVar] 世界书条目
      update_rules   {路径: 规则文本}      —— [mvu_update] 条目（给模型看的更新约束）
    """
    book = (_unwrap(card_data)).get("character_book") or {}
    entries = book.get("entries") if isinstance(book, dict) else None
    init, rules = [], {}
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        if e.get("enabled") is False:
            continue
        title = str(e.get("name") or e.get("comment") or "")
        content = str(e.get("content") or "")
        if INITVAR_TITLE_RE.search(title):
            init.append((title, content))
        elif MVU_UPDATE_TITLE_RE.search(title):
            rules.update(parse_update_rules(content))
    return init, rules


def parse_update_rules(text):
    """`[mvu_update]` 条目的内容 → {路径: 规则}。

    常见写法是每行 `路径: 规则` 或 `路径：规则`；认不出就整段丢掉，不硬猜。
    """
    out = {}
    for line in str(text or "").splitlines():
        line = line.strip().lstrip("-*").strip()
        if not line:
            continue
        m = re.match(r"^(stat_data(?:\.[^:：\s]+)*)\s*[:：]\s*(.+)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def merge_state(base, extra):
    """深合并：对象递归合并，其余后来者覆盖（数组整体替换，跟 MVU 一致）。"""
    if not isinstance(base, dict) or not isinstance(extra, dict):
        return copy.deepcopy(extra)
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = merge_state(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def initial_state(card_data, opening=0):
    """按卡的语义算开局状态：世界书 [InitVar] → 开场白 <initvar> → <json_patch>。"""
    data = _unwrap(card_data)
    init, rules = init_sources(data)
    state = {}
    for _title, content in init:
        parsed = lenient_json(content)
        if isinstance(parsed, dict):
            state = merge_state(state, parsed)

    openings = []
    fm = str(data.get("first_mes") or "")
    if fm:
        openings.append(fm)
    for g in (data.get("alternate_greetings") or []):
        openings.append(str(g))
    text = openings[opening] if 0 <= opening < len(openings) else (openings[0] if openings else "")

    inline = INIT_TAG_RE.findall(text or "")
    for chunk in inline:
        parsed = lenient_json(chunk)
        if isinstance(parsed, dict):
            state = merge_state(state, parsed)

    if not isinstance(state.get("stat_data"), dict):
        state = {"stat_data": state if state else {}}

    for chunk in PATCH_TAG_RE.findall(text or ""):
        ops = lenient_json(chunk)
        if isinstance(ops, dict):
            ops = ops.get("patch") or ops.get("ops") or []
        if isinstance(ops, list):
            try:
                apply_ops(state, ops, strict=False)
            except StateError:
                pass
    return state, rules


# --------------------------------------------------------------------------
# JSON Patch（RFC 6902 的实用子集）
# --------------------------------------------------------------------------

def apply_ops(state, ops, strict=True):
    """按顺序应用补丁；strict=True 时任一失败就整体抛出（由调用方回滚）。"""
    if not isinstance(ops, list):
        raise StateError("补丁必须是数组")
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise StateError("第 %d 条补丁不是对象" % (i + 1))
        kind = str(op.get("op") or "replace").lower()
        path = op.get("path")
        if not path:
            raise StateError("第 %d 条补丁缺 path" % (i + 1))
        if kind in ("add", "replace", "set"):
            set_path(state, path, op.get("value"))
        elif kind == "remove":
            try:
                del_path(state, path)
            except StateError:
                if strict:
                    raise
        elif kind == "append":
            cur = get_path(state, path)
            if not isinstance(cur, list):
                if strict:
                    raise StateError("%r 不是数组，不能 append" % path)
                cur = []
                set_path(state, path, cur)
            cur.append(op.get("value"))
        elif kind == "increment":
            cur = get_path(state, path)
            if not isinstance(cur, (int, float)):
                if strict:
                    raise StateError("%r 不是数字，不能 increment" % path)
                cur = 0
            set_path(state, path, cur + (op.get("value") or 1))
        elif kind == "merge":
            cur = get_path(state, path)
            if not isinstance(cur, dict):
                if strict:
                    raise StateError("%r 不是对象，不能 merge" % path)
                cur = {}
            set_path(state, path, merge_state(cur, op.get("value") or {}))
        else:
            raise StateError("不认识的补丁类型: %r" % kind)
    return state


# --------------------------------------------------------------------------
# 状态机
# --------------------------------------------------------------------------

class StoryState:
    """一局演出的世界状态。

    用法：
        st = StoryState.from_card(card)
        st.begin("好感 +5")
        st.patch([{"op": "increment", "path": "stat_data.好感", "value": 5}])
        st.commit()
    失败时 commit 不写、rollback 恢复；history 里按消息累积快照。
    """

    def __init__(self, data=None, rules=None, card_id=""):
        data = copy.deepcopy(data) if isinstance(data, dict) else {}
        if "stat_data" not in data:
            data = {"stat_data": data}
        self.data = data
        self.rules = dict(rules or {})
        self.card_id = card_id
        self.history = []
        self._txn = None

    # ---- 构造 ----

    @classmethod
    def from_card(cls, card_data, opening=0):
        state, rules = initial_state(card_data, opening)
        return cls(state, rules, card_id=str((card_data or {}).get("name") or ""))

    # ---- 读 ----

    def get(self, path, default=None):
        return get_path(self.data, path, default)

    def summary(self, limit=40):
        stat = self.data.get("stat_data") if isinstance(self.data, dict) else {}
        if not isinstance(stat, dict):
            return {}
        return {k: v for k, v in list(stat.items())[:limit]
                if not isinstance(v, (dict, list))}

    # ---- 事务 ----

    def begin(self, note=""):
        if self._txn is not None:
            raise StateError("已经有事务在跑（%s），先 commit 或 rollback" % self._txn.get("note"))
        self._txn = {"note": note, "before": copy.deepcopy(self.data)}

    def commit(self):
        if self._txn is None:
            raise StateError("没有在跑的事务")
        before = self._txn["before"]
        note = self._txn["note"]
        self._txn = None
        if before != self.data:
            self.history.append({"note": note, "before": before,
                                 "after": copy.deepcopy(self.data)})
        return self

    def rollback(self):
        if self._txn is None:
            raise StateError("没有在跑的事务")
        self.data = self._txn["before"]
        self._txn = None
        return self

    def patch(self, ops, note=""):
        """一口气改完：要么全成、要么一点不动。"""
        self.begin(note or "patch")
        try:
            apply_ops(self.data, ops, strict=True)
        except Exception:
            self.rollback()
            raise
        return self.commit()

    # ---- 快照 ----

    def snapshot(self, note=""):
        snap = {"note": note, "data": copy.deepcopy(self.data)}
        self.history.append({"note": note, "before": None, "after": copy.deepcopy(self.data),
                             "snapshot": True})
        return snap

    def restore(self, index):
        """回到第 index 条快照（负数从末尾数）。"""
        snaps = [h for h in self.history if h.get("snapshot")]
        if not snaps:
            raise StateError("没有快照可回")
        idx = index if index >= 0 else len(snaps) + index
        if not (0 <= idx < len(snaps)):
            raise StateError("快照下标越界：%d（共 %d 条）" % (index, len(snaps)))
        self.data = copy.deepcopy(snaps[idx]["after" if "after" in snaps[idx] else "data"])
        return self

    # ---- 序列化 ----

    def to_dict(self):
        return {"card_id": self.card_id, "data": self.data, "rules": self.rules,
                "history": self.history[-50:]}

    @classmethod
    def from_dict(cls, obj):
        obj = obj or {}
        st = cls(obj.get("data") or {}, obj.get("rules") or {},
                 card_id=str(obj.get("card_id") or ""))
        st.history = list(obj.get("history") or [])
        return st

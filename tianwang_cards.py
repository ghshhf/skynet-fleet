"""天网语音线 → X6 统一卡层的嵌合件（adapter）。

角色：`persona_card.py`（X6 两天打磨的统一卡层，ccv2/v3 JSON/PNG）是**唯一真源**，
本模块不重复实现任何卡逻辑，只做三件小事：

  1. 把统一层的 API（load_card / build_system / list_cards / set_active /
     active_card）包成语音栈（dialogue / desktop / web / triage）惯用的
     id 制接口：list_cards() / current() / switch(id) / compile_card(id) /
     build_system(id, memory=None)。
  2. md 源稿 → ccv2 json 的编译由 tools/md2card.py 负责（跑一次落盘），
     运行时**只认 json/png**，与 X6 卡商店同一批卡。
  3. 冻扁包里 cards/ 被 runtime hook 指到 %APPDATA%/fleet/cards（可写），
     首次为空时从 MEIPASS 把出厂卡种子进去（见 tools/fleet_runtime_hook.py）。

默认卡：没有 active.json 时回落到「whale_girl.json」，不动统一层的指针语义。
"""
import json
import os
import sys

try:
    import persona_card as _pc          # X6 统一卡层（唯一真源）
except Exception:                        # pragma: no cover
    _pc = None

DEFAULT_CARD = "whale_girl.json"


def _dir():
    """卡库目录：统一层的 DEFAULT_CARD_DIR 已被 runtime hook 按 FLEET_CARD_DIR 重定向。"""
    return getattr(_pc, "DEFAULT_CARD_DIR", os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "cards"))


def _card_path(cid):
    """id（文件名或去扩展名）→ 卡文件路径；找不到返回 None。"""
    d = _dir()
    for cand in (cid,
                 cid + ".json", cid + ".png",
                 os.path.join(d, cid),
                 os.path.join(d, cid + ".json"),
                 os.path.join(d, cid + ".png")):
        if os.path.isfile(cand):
            return cand
    return None


def list_cards():
    """语音栈 UI 要的形状：[{"id", "name", "file"}]；坏卡跳过不拖垮列表。

    🔴 2026-10-26 修：统一层的 list_cards 会把同一张卡的 .json 和 .md
    **各算一遍**，于是 16 张卡列出 26 条——手机卡库页会看到每张卡两遍，
    点哪一条都一样。这里按 id 去重，保留先出现的那条（.json 排在前面，
    是权威数据；.md 是编译源）。
    """
    if _pc is None:
        return []
    out, seen = [], set()
    for c in _pc.list_cards():
        if not c.get("ok"):
            continue
        fn = c["file"]
        cid = os.path.splitext(fn)[0]
        if cid in seen:
            continue
        seen.add(cid)
        out.append({"id": cid, "file": fn,
                    "name": (c.get("summary") or {}).get("name") or cid})
    return out


def _active_filename():
    """active.json 指针里存的文件名（只读，不写）。"""
    p = os.path.join(_dir(), "active.json")
    try:
        rec = json.load(open(p, encoding="utf-8"))
        return rec.get("file") or None
    except Exception:
        return None


def current():
    """当前卡 id；没选过回落默认卡；连默认卡都没有返回 None。"""
    fn = _active_filename()
    if fn and _card_path(fn):
        return os.path.splitext(fn)[0]
    if _card_path(DEFAULT_CARD):
        return os.path.splitext(DEFAULT_CARD)[0]
    cs = list_cards()
    return cs[0]["id"] if cs else None


def switch(cid):
    """切卡（写统一层的 active.json 指针）。返回 {"ok": ...}。"""
    if _pc is None:
        return {"ok": False, "error": "persona_card 不可用"}
    p = _card_path(cid)
    if not p:
        return {"ok": False, "error": "没有这张卡：%s" % cid}
    try:
        _pc.set_active(os.path.basename(p))
        return {"ok": True, "id": os.path.splitext(os.path.basename(p))[0]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _data(cid):
    """id → 归一化卡数据（统一层 load_card）。"""
    if _pc is None:
        raise RuntimeError("persona_card 不可用")
    p = _card_path(cid)
    if not p:
        raise FileNotFoundError("没有这张卡：%s" % cid)
    return _pc.load_card(p)


def compile_card(cid):
    """旧接口兼容：返回 {"name", "system", "meta"}。"""
    try:
        data = _data(cid)
    except Exception as e:
        return {"name": cid, "system": "", "meta": {"error": str(e)}}
    try:
        system = _pc.build_system(data)
    except Exception as e:
        system = ""
        data = {"name": data.get("name", cid), "_error": str(e)}
    return {"name": data.get("name") or cid, "system": system,
            "meta": {"spec": data.get("_spec"), "file": _card_path(cid)}}


def build_system(cid, memory=None):
    """id → system prompt；memory（她记得的往事）经 extra 传入统一层。"""
    data = _data(cid)
    extra = ("[她记得]\n" + str(memory).strip()) if memory else ""
    return _pc.build_system(data, extra=extra)


def first_message(cid=None):
    """开场白（没有就返回空串，上层走无开场逻辑）。"""
    if _pc is None or not hasattr(_pc, "first_message"):
        return ""
    try:
        data = _data(cid or current())
        return _pc.first_message(data) if data else ""
    except Exception:
        return ""


if __name__ == "__main__":              # 自检：列卡 + 渲染当前卡
    cs = list_cards()
    print("卡库 %d 张" % len(cs))
    for c in cs:
        print("  -", c["id"], "|", c["name"])
    cur = current()
    print("当前卡:", cur)
    if cur:
        s = build_system(cur)
        print("system len =", len(s))
        assert len(s) > 100, "system 太短，卡没吃进去"
    print("SELFTEST OK")

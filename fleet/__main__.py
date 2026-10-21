"""设备层的入口：`python -m fleet <命令>`。

    list        设备 + 能力 + 现在能不能用（默认）
    devices     只看设备
    caps        只看能力
    transports  四条通道通不通
    call        调一项能力（--args 传 JSON）
    save        把「这设备是什么」落盘（绝不含凭据）
    crew        角色编队：名册 / 容量闸 / 五段式契约
    mcp        把本仓变成 MCP server（--serve 起 stdio；--list 列工具表）
    skills     列出可用的技能（SKILL.md）

为什么用纯文本而不用 rich：这份输出要被 evals 与自测断言（`fleet list | grep`），
ANSI 与框线会让断言变脆。终端（fleet/shell.py）才是给人看的，那里才上 rich。
"""
import argparse
import json
import sys
import unicodedata

from .catalog import build
from .core import result as R
from .transports import probe_all


def _w(s):
    """按显示宽度算长度（中文占两格，否则表格必歪）。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s, n):
    s = str(s)
    return s + " " * max(0, n - _w(s))


def _clip(s, n):
    s = str(s or "").replace("\n", " ")
    if _w(s) <= n:
        return s
    out = ""
    for c in s:
        if _w(out) + _w(c) > n - 1:
            return out + "…"
        out += c
    return out


def _cmd_devices(reg, as_json=False):
    rows = reg.availability()
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return rows
    online = sum(1 for r in rows if r["ok"])
    print(f"== 设备 {len(rows)} 台（在线 {online}） ==")
    if not rows:
        print("  （一台都没有：注册表是空的，检查 catalog.build）")
    for r in rows:
        state = "在线" if r["ok"] else "不可用"
        print(f"  {_pad(r['name'], 18)} {_pad(r['kind'], 13)} {_pad(r['via'], 8)} "
              f"{_pad(state, 6)} {len(r['caps'])} 项"
              + ("" if r["ok"] else f"  {_clip(r['reason'], 70)}"))
    return rows


def _cmd_caps(reg, as_json=False):
    rows = []
    for c in reg.capabilities():
        ok, reason, dev = reg.check(c.name)
        rows.append({"name": c.name, "title": c.title, "ok": ok,
                     "device": dev.name if dev else "",
                     "reason": reason, "side_effect": c.side_effect,
                     "destructive": c.destructive, "note": c.note,
                     "params": [p.as_dict() for p in c.params]})
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return rows
    print(f"== 能力 {len(rows)} 项（现在可调 {sum(1 for r in rows if r['ok'])}） ==")
    for r in rows:
        kind = "动手" if r["side_effect"] else "只读"
        if r["destructive"]:
            kind = "破坏"
        state = "可用" if r["ok"] else "不可用"
        tail = "" if r["ok"] else f"  {_clip(r['reason'], 62)}"
        if r["ok"] and r["device"]:
            tail = f"  → {r['device']}"
        print(f"  {_pad(r['name'], 16)} {_pad(r['title'], 34)} {_pad(kind, 5)} "
              f"{_pad(state, 6)}{tail}")
    return rows


def _cmd_transports():
    rows = probe_all()
    print(f"== 通道 {len(rows)} 条 ==")
    for r in rows:
        state = "通" if r["ok"] else "不通"
        print(f"  {_pad(r['name'], 10)} {_pad(state, 4)} {_pad(r['title'], 34)}"
              + ("" if r["ok"] else f"  {_clip(r['reason'], 60)}"))
    return rows


def _gate_cli(cap, dev, args, level, assume_yes=False):
    """命令行闸门：--yes 全放行；否则动手放行（打印一行），破坏性拒绝。"""
    if assume_yes:
        return True, ""
    if level >= 2:
        return False, "破坏性动作要在终端里确认（这里没给 --yes）"
    print(f"  · 动手能力已执行：{cap.name} @ {dev.name}", file=sys.stderr)
    return True, ""


def _cmd_call(reg, cap_name, device="", args=None, assume_yes=False):
    res = reg.call(cap_name, device=device, args=args or {},
                   gate=lambda c, d, a, lv: _gate_cli(c, d, a, lv, assume_yes))
    print(json.dumps(res.as_dict(), ensure_ascii=False, indent=2))
    return 0 if res.ok else 2


def _cmd_crew(reg, as_json, argv):
    """角色编队：名册 / 容量闸 / 契约。

    子命令：
      roster           角色名册（谁、什么后端、能用哪些能力、能往下派几层）
      gate             容量闸当前状态
      contract         五段式契约说明 + 给模型的格式条款
    """
    from .agents import capacity as cap
    from .agents import contract as ct
    from .agents import roster

    sub = argv.pop(0).lower() if argv and not argv[0].startswith("-") else "roster"
    known = set(reg.cap_names())

    if sub == "roster":
        try:
            profiles = roster.load_all(known_caps=known)
        except roster.RosterError as e:
            print(f"名册不合法：{e}", file=sys.stderr)
            return 1
        rows = roster.summary(profiles, known)
        if as_json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        print(f"角色名册（{len(rows)} 个）：")
        for r in rows:
            print(f"  · {r['name']}  后端={r['provider']}  "
                  f"能力={r['cap_allow']}  可派层={r['max_depth']}  [{r['source']}]")
        try:
            print(f"\n当前激活：{roster.active_name()}")
        except roster.RosterError as e:
            print(f"\n激活角色读不到：{e}", file=sys.stderr)
            return 1
        return 0

    if sub == "gate":
        gate = cap.CapacityGate(capacity=4, max_depth=1)
        snap = gate.snapshot()
        if as_json:
            print(json.dumps(snap, ensure_ascii=False, indent=2))
            return 0
        print(f"容量闸：容量 {snap['capacity']} / 已占 {snap['in_use']} / "
              f"空闲 {snap['free']}，深度上限 {snap['max_depth']}")
        print("默认容量 4（不是 8）：16GB 内存 + 多个角色打同一个免费端点抢额度。")
        return 0

    if sub == "contract":
        if as_json:
            print(json.dumps({"sections": list(ct.SECTIONS),
                              "hints": ct.SECTION_HINTS}, ensure_ascii=False, indent=2))
            return 0
        print("五段式输出契约（子智能体必须交这份答卷）：")
        for name in ct.SECTIONS:
            print(f"  {name:9s} {ct.SECTION_HINTS[name]}")
        print("\n给模型的格式条款：")
        print(ct.prompt_block())
        return 0

    print(f"未知 crew 子命令: {sub}\n"
          f"可用：roster / gate / contract", file=sys.stderr)
    return 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]

    sub = "list"
    if argv and not argv[0].startswith("-"):
        sub = argv.pop(0).lower()

    if sub in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    reg = build()
    try:
        if sub in ("list", "all"):
            _cmd_devices(reg, as_json)
            print()
            _cmd_caps(reg, as_json)
            if not as_json:
                print()
                _cmd_transports()
            st = reg.stats()
            if not as_json:
                print(f"\n共 {st['devices']} 台设备 / {st['online']} 台在线 / "
                      f"{st['caps']} 项能力")
            return 0
        if sub == "devices":
            _cmd_devices(reg, as_json)
            return 0
        if sub == "caps":
            _cmd_caps(reg, as_json)
            return 0
        if sub == "transports":
            _cmd_transports()
            return 0
        if sub == "save":
            p = reg.save()
            print(f"已落盘: {p or '（没有写：路径不可写）'}")
            return 0 if p else 1
        if sub == "crew":
            return _cmd_crew(reg, as_json, argv)
        if sub == "call":
            ap = argparse.ArgumentParser(prog="python -m fleet call")
            ap.add_argument("cap")
            ap.add_argument("--device", default="")
            ap.add_argument("--args", default="{}")
            ap.add_argument("--yes", action="store_true")
            a = ap.parse_args(argv)
            try:
                payload = json.loads(a.args or "{}")
            except ValueError as e:
                print(f"--args 不是合法 JSON: {e}", file=sys.stderr)
                return 1
            return _cmd_call(reg, a.cap, a.device, payload, a.yes)
        print(f"未知子命令: {sub}\n{__doc__}", file=sys.stderr)
        return 1
    finally:
        if sub != "save":
            reg.save()          # 见过一次就记一笔（只记「是什么」）


if __name__ == "__main__":
    sys.exit(main())

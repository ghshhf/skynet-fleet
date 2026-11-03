"""能力归组：把「同一个动作的几种实现」收成一张表，让模型只看见一件事。

## 为什么需要这一层（11-01 实测，不是设计洁癖）

357 项能力里，同一台机器上的同一件事有好几套实现，而且**参数名还不一样**：

    pcctl.input(keys=...)        vs  host.input(key=...)              keys / key
    pcctl.mouse(action=click)    vs  host.input(action=click)
    pcctl.input(action=text)     vs  host.input(action=type)
    osint.dns(domain=...)        vs  web.dns(url=...)                 domain / url
    pc.state / pcctl.sysinfo / host.monitor                           三个都答「本机怎么样」
    host.screen / pcctl.screenshot / pcctl.look                       三个都答「截个屏」

模型面对这些只会做一件事：**抓错那只手**。204 条工具表的对照实验已经证明了这一点
（368 token 正确调用 vs 26,601 token 调错）。所以「重复」不是无害的冗余，
它是会直接把模型带偏的噪音。

## 做法：不删代码，声明等价

每套实现背后都有自己的设备与历史，删掉会丢掉真东西（`host.screen` 会出 braille 预览，
`pcctl.screenshot` 只给路径——各有各的用处）。所以：

  1. `ACTIONS`  动作 → 实现列表，第一个是**主名**（模型看到的那个）
  2. `ARG_ALIAS`  动作 → 参数别名（`key`/`keys`、`url`/`domain`/`host`/`target`）
  3. `collapse()` 工具表只露主名 ⇒ 表从 357 条收敛到「动作数」
  4. `run()`     主实现失败（坏实现 / 没设备 / 参数不认）→ 自动试组内下一个

## 为什么兜底是真收益，不是花架子

实测 `pcctl.look` 直接失败（ok=False、data 空），而 `host.screen` 能出预览。
组内互备把「一套坏掉」从「这项能力不可用」降级成「换个实现」——这是白捡的可靠性。

**但绝不冒充**：结果里带 `_impl`（真用的是哪套）和 `_tried`（试过哪些），
降级了就说降级了。这是本仓的老口径：降级可以，冒充不行。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 动作表
#
# 结构：动作 id → (中文标签, [实现名...])
#   · 第一个是实现列表的**首选**；动作 id 本身就是「对外那个名字」，
#     只有当一个动作只有一套实现时，动作 id 才等于能力名。
#   · 不在表里的能力自成一组，行为完全不变（不逼所有人入组）。

ACTIONS = {
    # ---- 本机（Windows）----
    "pc.state": ("本机现在怎么样（前台/在座/负载/磁盘）",
                 ["pc.state", "host.monitor", "pcctl.sysinfo"]),
    "pc.screen": ("看本机屏幕（截图 + 文字预览）",
                  ["host.screen", "pcctl.screenshot", "pcctl.look"]),
    "pc.click": ("在本机屏幕坐标点击",
                 ["pcctl.mouse", "host.input"]),
    "pc.type": ("在本机输入文字",
                ["pcctl.input", "host.input"]),
    "pc.key": ("在本机按键（单键或组合键）",
               ["pcctl.input", "host.input"]),
    "pc.launch": ("启动本机程序",
                  ["pcctl.app.launch"]),
    # ---- 手机 ----
    "phone.screen": ("看手机屏幕（前台 + 可见文字）",
                     ["screen.read", "phone.see"]),
    # ---- 网络 ----
    "net.dns": ("查域名解析记录",
                ["osint.dns", "pentest.dns_resolve", "web.dns"]),
    "net.fetch": ("取一个网页",
                  ["web.fetch", "crawl.http_fetch"]),
    "net.scan_ports": ("扫端口",
                       ["pentest.port_scan", "net.scan"]),
    "net.probe_http": ("探一个网址活不活",
                       ["pentest.live_probe", "web.probe"]),
    # ---- 模型 ----
    "llm.chat": ("问模型一句话",
                 ["llmgw.chat", "ai.chat", "ai.complete"]),
    "llm.models": ("现在有哪些模型/端点可用",
                   ["llmgw.models", "ai.list"]),
}

#: 反向索引：能力名 → 动作 id
_BY_NAME: dict[str, str] = {}
for _a, (_l, _ms) in ACTIONS.items():
    for _m in _ms:
        _BY_NAME[_m] = _a

# ---------------------------------------------------------------- 参数别名
#
# 表形状：动作 → {概念: {实现名: 该实现里这概念叫什么, "*": 剩下的实现叫什么}}
#
# **为什么是「按实现声明」而不是「别名 ↔ 别名」**：
# 一开始写成 {"keys": ["key"], "key": ["keys"]}（互为别名），结果第一趟把 key 搬成
# keys、第二趟又搬回 key —— 互指会把结果转丢。正名不该由表决定，该由**目标实现**决定：
# 你要发给 pcctl.input，它就一定叫 keys；发给 host.input，它就一定叫 key。
# 这样每趟只有「别的实现叫过的名字 → 目标实现要的名字」一个方向，转不回去。

ARG_ALIAS = {
    "pc.key": {"键": {"pcctl.input": "keys", "host.input": "key", "*": "keys"},
               "等待": {"*": "wait"}},
    "net.dns": {"域名": {"osint.dns": "domain", "pentest.dns_resolve": "domain",
                        "web.dns": "url", "*": "domain"}},
    "net.fetch": {"网址": {"*": "url"}},
    "net.probe_http": {"网址": {"pentest.live_probe": "url", "web.probe": "url",
                              "*": "url"}},
    "net.scan_ports": {"目标": {"pentest.port_scan": "host", "net.scan": "host",
                              "*": "host"}},
    "llm.chat": {"提示": {"llmgw.chat": "prompt", "ai.chat": "prompt",
                        "ai.complete": "prompt", "*": "prompt"}},
}


#: 组名 → {目标实现: 必须补的固定参数}（2026-11-03 加）。
#
# 能力从「一个名字 = 一个动作」变成「域 + action」之后，归组层只改参数名不够了：
# 把 pc.click 转到 pcctl.mouse，参数全对，但没告诉它 action="click"，
# 它不知道要点鼠标还是移动鼠标——参数齐了，动作是空的。
# 所以「组 → 实现」要连带它的固定参数一起给。已有值不覆盖（调用方显式给了就听调用方的）。
GROUP_CONST = {
    "pc.click": {"pcctl.mouse": {"action": "click"},
                 "host.input": {"action": "click"}},
    "pc.type": {"pcctl.input": {"action": "text"},
                "host.input": {"action": "type"}},
    "pc.key": {"pcctl.input": {"action": "key"},
               "host.input": {"action": "key"}},
}


def aliases_for(name_or_action: str) -> dict:
    """取某动作的参数别名表（按概念分组）。"""
    a = canonical(name_or_action) or name_or_action
    return dict(ARG_ALIAS.get(a) or {})


# ---------------------------------------------------------------- 查询

def canonical(name: str) -> str | None:
    """能力名 → 动作 id。不在表里的返回 None（不强行入组）。

    动作 id 本身也要认：`impls("pc.screen")` 传进来的就是动作名，
    若只查「实现名 → 动作」的反向索引，动作名会落空成单实现组（踩过）。
    """
    if name in ACTIONS:
        return name
    return _BY_NAME.get(name)


def label(name_or_action: str) -> str:
    a = canonical(name_or_action) or name_or_action
    return (ACTIONS.get(a) or ("", []))[0]


def impls(name_or_action: str) -> list[str]:
    """动作 → 实现列表（有序）。不在表里的当成只有一个实现。"""
    a = canonical(name_or_action)
    if a:
        return list(ACTIONS[a][1])
    return [name_or_action]


def primary(name_or_action: str) -> str:
    a = canonical(name_or_action)
    if a:
        return ACTIONS[a][1][0]
    return name_or_action


def is_alias(name: str) -> bool:
    """是不是「组内非主名」——工具表要折叠掉这些。"""
    a = canonical(name)
    if not a:
        return False
    ms = ACTIONS[a][1]
    return bool(ms) and name != ms[0]


def is_impl(name: str) -> bool:
    """这个名字是不是**具体实现**（某组里的某一套）。

    用来决定「要不要兜底」：调用者点名了某套实现（`host.screen`），
    就照它说的打，别自作主张换成另一套——它可能就是要那一套的返回形状。
    只有说的是**动作**（`pc.screen`，真名里没有的名字）才走整组兜底。
    """
    return name in _BY_NAME


def collapse(caps) -> list:
    """把能力表折叠成「每动作一条」，保留**传入顺序**。

    折叠规则：动作内主名在表里就留主名；主名不在（那台设备没注册）就留第一个在的。
    这样「同一件事」永远只出现一次，且出现的是当下真能用的那个。
    """
    caps = list(caps or [])
    have = {getattr(c, "name", c) for c in caps}
    keep: set[str] = set()
    seen: set[str] = set()
    for c in caps:
        n = getattr(c, "name", c)
        a = canonical(n)
        if not a:
            keep.add(n)
            continue
        if a in seen:
            continue
        seen.add(a)
        pick = None
        for m in ACTIONS[a][1]:
            if m in have:
                pick = m
                break
        if pick:
            keep.add(pick)
    return [c for c in caps if getattr(c, "name", c) in keep]


# ---------------------------------------------------------------- 参数归一

def norm_args(name: str, args: dict, target: str = "") -> dict:
    """把参数改写成 `target` 这套实现认识的名字。

    每个概念只看两件事：`target` 要求叫什么（`want`），以及**别的实现**叫过什么（`cands`）。
    正名由目标实现决定 ⇒ 永远不会来回转（互指表的老 bug 见 ARG_ALIAS 上面那段注释）。
    已经有 `want` 的值就不动；其余参数一律原样带过去，一个都不丢。
    """
    out = dict(args or {})
    a = canonical(target or name) or (target or name)
    tbl = ARG_ALIAS.get(a) or {}
    for _concept, per in tbl.items():
        want = per.get(target) or per.get("*")
        if not want:
            continue
        if out.get(want) not in (None, "", [], {}):
            continue                      # 正名参数已经有值，不许被别名挤掉
        cands = []
        for v in per.values():
            if v and v != want and v not in cands:
                cands.append(v)
        for c in cands:
            if c in out and out[c] not in (None, "", [], {}):
                out[want] = out.pop(c)
                break
    # 域级能力要 action：转过去的参数全对、动作不能空着。
    cst = (GROUP_CONST.get(a) or {}).get(target) or {}
    for k, v in cst.items():
        out.setdefault(k, v)
    return out


# ---------------------------------------------------------------- 兜底调用

def run(executor, name: str, args: dict | None = None, accept=None,
        no_fallback: bool = False):
    """按组内顺序试实现，第一个成功就返回。

    executor：可调用 (cap_name, args) → 结果对象（要有 .as_dict() 或本身是 dict）
    accept：可选，判断「这个结果算不算成功」。默认看 ok / status。
    no_fallback：只打主实现一次。**副作用能力必须传 True**——
        「按一次键」重试成「按两次键」不是兜底，是另一个动作。
        调用方拿不准时看 Capability.side_effect / destructive（agentloop 已经这么做）。
    """
    args = dict(args or {})
    no_fb = no_fallback or bool(args.pop("_no_fallback", False))
    # 不兜底 = 只打主实现那一次。**必须解析到真实能力名**——执行层不认动作 id，
    # 直接把 "pc.key" 丢给 registry 会得到「注册表里没有这项能力」。
    chain = [primary(name)] if no_fb else impls(name)
    tried, last = [], None
    for impl in chain:
        a = norm_args(name, args, target=impl)
        try:
            r = executor(impl, a)
        except Exception as e:                    # 实现自身崩了也要能换下一套
            tried.append({"impl": impl, "err": "exception: %s" % str(e)[:120]})
            continue
        d = r.as_dict() if hasattr(r, "as_dict") else (r if isinstance(r, dict) else {})
        ok = bool(d.get("ok")) if "ok" in d else bool(d.get("status") in ("ok", True))
        if accept is not None:
            ok = bool(accept(d))
        tried.append({"impl": impl, "ok": ok,
                      "err": "" if ok else str(d.get("err") or d.get("reason") or "")[:120]})
        if ok:
            if len(tried) > 1 or impl != chain[0]:
                # 降级要说出来：用了哪套、前几套为什么没成
                if isinstance(d, dict):
                    d = dict(d)
                    d["_impl"] = impl
                    d["_tried"] = tried
            return d
        last = d
    # 全灭：把每次的原因拼回去，别只说「失败了」
    msg = "; ".join("%s: %s" % (t["impl"], t["err"] or "失败") for t in tried)
    out = dict(last or {})
    out.update({"ok": False, "_tried": tried,
                "err": ("组内 %d 套实现全失败 —— %s" % (len(tried), msg))[:400]})
    return out


# ---------------------------------------------------------------- 自测

def _selftest():
    from .catalog import CAPABILITIES
    caps = list(CAPABILITIES)
    by = {c.name: c for c in caps}

    # ① 表里写的每个实现都必须是真实存在的能力（防止手滑写死名字）
    for a, (_l, ms) in ACTIONS.items():
        for m in ms:
            assert m in by, "动作 %s 声明了不存在的能力 %s" % (a, m)

    # ② 同一「能力 + 固定参数」只能属于一个动作（重复归属会让折叠结果随机）。
    #    2026-11-03 起能力是「域 + action」：host.input 既可能是 click 也可能是 type，
    #    光看名字分不开，必须连 GROUP_CONST 里的 action 一起当键。
    seen = {}
    for a, (_l, ms) in ACTIONS.items():
        for m in ms:
            const = tuple(sorted((GROUP_CONST.get(a, {}).get(m) or {}).items()))
            key = (m, const)
            assert key not in seen, "%s(%s) 同时属于 %s 和 %s" % (
                m, dict(const) or "无固定参", seen[key], a)
            seen[key] = a

    # ③ 折叠后同一动作只剩一条，且总数下降
    col = collapse(caps)
    assert len(col) < len(caps), "折叠应减少条数：%d → %d" % (len(caps), len(col))
    acts = [canonical(c.name) for c in col]
    acts = [a for a in acts if a]
    assert len(acts) == len(set(acts)), "折叠后仍出现重复动作"

    # ④ 折叠后主名优先：主名存在就不该留别名
    names = {c.name for c in col}
    for a, (_l, ms) in ACTIONS.items():
        present = [m for m in ms if m in names]
        assert len(present) <= 1, "动作 %s 折叠后仍有多个实现：%s" % (a, present)
        if present and present[0] != ms[0] and ms[0] in by:
            # 主名在能力表里却没被留下 —— 说明顺序没生效
            raise AssertionError("动作 %s 应留主名 %s，实际留了 %s"
                                 % (a, ms[0], present[0]))

    # ⑤ 参数别名：keys/key 双向归一，且不碰无关参数
    #    （2026-11-03：目标实现换成归并后的域名，同时要带上 action）
    assert norm_args("pc.key", {"key": "enter"}, "pcctl.input").get("keys") == "enter"
    assert norm_args("pc.key", {"keys": "ctrl+c"}, "host.input").get("key") == "ctrl+c"
    a = norm_args("pc.key", {"key": "enter", "wait": 3}, "pcctl.input")
    assert a.get("keys") == "enter" and a.get("wait") == 3, "别名归一不许丢无关参数"
    # 域级能力必须带上 action，否则参数齐了动作是空的
    assert a.get("action") == "key", "转到域级实现时必须补 action"
    assert norm_args("pc.click", {"x": 1, "y": 2}, "pcctl.mouse").get("action") == "click"
    # 正名已存在时不许被别名覆盖
    a = norm_args("net.dns", {"domain": "a.com", "url": "b.com"}, "osint.dns")
    assert a.get("domain") == "a.com", "正名参数不许被别名挤掉"

    # ⑥ DNS 组：url 也能喂给要 domain 的实现
    assert norm_args("net.dns", {"url": "x.com"}, "osint.dns").get("domain") == "x.com"

    # ⑦ 兜底：第一套失败自动换第二套，并如实标注
    calls = []

    def fake_exec(cap, args):
        calls.append(cap)
        if cap == "host.screen":
            return {"ok": False, "err": "no preview"}
        return {"ok": True, "data": {"path": "/x.png"}}

    r = run(fake_exec, "pc.screen", {})
    assert r.get("ok") and r.get("_impl") == "pcctl.screenshot", r
    assert calls == ["host.screen", "pcctl.screenshot"], calls
    assert r.get("_tried") and r["_tried"][0]["ok"] is False

    # ⑧ 第一套就成功时不标降级（别给正常路径加噪音）
    calls.clear()

    def ok_exec(cap, args):
        calls.append(cap)
        return {"ok": True}

    r = run(ok_exec, "pc.screen", {})
    assert r.get("ok") and "_impl" not in r, "主名成功不该带 _impl"

    # ⑨ 全灭时把每套的原因都带回来（不是一句「失败了」）
    def dead_exec(cap, args):
        return {"ok": False, "err": "boom-" + cap}

    r = run(dead_exec, "pc.screen", {})
    assert not r.get("ok") and "boom-host.screen" in r["err"] and "boom-pcctl.look" in r["err"], r

    # ⑩ _no_fallback 只打主名（副作用能力靠这个不进兜底；动作名必须落成真实能力名）
    def rec_exec(cap, args):
        calls.append(cap)
        return {"ok": False, "err": "boom-" + cap}

    calls.clear()
    run(rec_exec, "pc.screen", {"_no_fallback": True})
    assert calls == ["host.screen"], calls

    # ⑪ 非组能力行为不变
    assert impls("mqtt.pub") == ["mqtt.pub"] and not is_alias("mqtt.pub")
    assert not is_alias("host.screen"), "主名不是别名"

    print("fleet.groups selftest ok（%d 组 / 折叠 %d→%d）" % (len(ACTIONS), len(caps), len(col)))


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    _selftest()

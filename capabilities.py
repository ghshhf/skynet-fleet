"""能力自描述：节点不只上报能力「名字」，还上报它的「说明书」（参数 schema）。

为什么需要它：统一意图编排要「发现」编队里能做什么、需要什么参数——硬编码的
能力名（exec/ai/…）只能被广播，不能被编排。有了自描述，Commander（与她）才能
按意图去找「能干这件事的节点 + 怎么调它」。

唯一真源是 `fleet/catalog.py` 的 `CAPABILITIES`：`spec_of()` 先查它，查不到才落到
本文件的 `SPECS`。`SPECS` 只保留**节点协议动词**（exec/push/pull/ctx/compute/ai/
persona/perceive）——它们不是 domain.verb 能力名，不会进 catalog。

退役原则（2026-11-12）：重命名后的旧名、已被 catalog 接管的条目一律删除，不留
「兼容别名」——同一件事只许有一个真源，否则「查得到、但查的是旧的」会静默错下去。
未登记的能力仍自动兜底为「能力名即标题」，保证可发现性。
"""

# 节点协议动词的说明书：{动词: {"title","desc","params":[{name,type,required,desc}]}}
# 只放不带点的协议动词；domain.verb 能力一律登记到 fleet/catalog.py。
SPECS = {
    "exec": {
        "title": "执行命令",
        "desc": "在节点上下发一条白名单命令，取回 stdout/stderr/退出码",
        "params": [
            {"name": "cmd", "type": "str", "required": True, "desc": "命令（首词须在白名单）"},
        ],
    },
    "push": {
        "title": "推送文件",
        "desc": "把中心（手机工具箱）里的文件反向推送到目标节点",
        "params": [
            {"name": "path", "type": "str", "required": True, "desc": "目标端落盘文件名"},
            {"name": "data", "type": "base64", "required": True, "desc": "文件内容"},
        ],
    },
    "pull": {
        "title": "拉取文件",
        "desc": "从目标节点把文件取回中心",
        "params": [
            {"name": "path", "type": "str", "required": True, "desc": "目标端沙箱内相对路径"},
        ],
    },
    "ctx": {
        "title": "共享上下文",
        "desc": "读写 L3 跨设备共同记忆（ns/key/val，逐条可删）",
        "params": [
            {"name": "ns", "type": "str", "required": True, "desc": "命名空间（如 her / her_mind）"},
            {"name": "key", "type": "str", "required": True, "desc": "键"},
            {"name": "val", "type": "str", "required": False, "desc": "值（put 时必填）"},
        ],
    },
    "compute": {
        "title": "本地推理（重活）",
        "desc": "在本节点跑一次本地模型推理，承接她三段式里的「重活」",
        "params": [
            {"name": "prompt", "type": "str", "required": True, "desc": "提示词"},
            {"name": "model", "type": "str", "required": False, "desc": "指定本地模型"},
        ],
    },
    "ai": {
        "title": "远程 AI 即服务",
        "desc": "本节点有脑：替瘦客户端跑对话/推理（真·流式回吐）",
        "params": [
            {"name": "prompt", "type": "str", "required": True, "desc": "提问"},
            {"name": "model_hint", "type": "str", "required": False, "desc": "档位 small/4b/gpu"},
            {"name": "ctx_ns", "type": "str", "required": False, "desc": "附带的共享上下文命名空间"},
        ],
    },
    "persona": {
        "title": "她的人格运行时",
        "desc": "本节点承载「她」：心念 tick、主动开口、诉求授权闭环",
        "params": [
            {"name": "tick", "type": "int", "required": False, "desc": "心念 tick 间隔秒"},
        ],
    },
    "perceive": {
        "title": "感官输入",
        "desc": "上报一次感知事件（在场/环境），喂给她的世界模型",
        "params": [
            {"name": "kind", "type": "str", "required": True, "desc": "感知种类（presence 等）"},
            {"name": "value", "type": "str", "required": True, "desc": "感知值（home/away 等）"},
        ],
    },
}

# 旧基线的类型词汇 → 设备层的类型词汇
TYPES = {"base64": "str", "str": "str", "int": "int", "bool": "bool", "json": "json"}


_CATALOG_VIEW = None


def _catalog_view():
    """catalog 的能力名 -> 说明书视图。

    懒加载：capabilities 是基线文件，必须能不依赖设备层单独 import；
    catalog 才是能力的唯一真源，本文件的 SPECS 只兜底协议动词。
    """
    global _CATALOG_VIEW
    if _CATALOG_VIEW is None:
        try:
            from fleet.catalog import CAPABILITIES
            _CATALOG_VIEW = {c.name: c.as_dict() for c in CAPABILITIES}
        except Exception:
            _CATALOG_VIEW = {}
    return _CATALOG_VIEW


def spec_of(cap):
    """取能力说明书：catalog 优先，协议动词兜底，未登记按能力名兜底。"""
    live = _catalog_view().get(cap)
    if live:
        return {"title": live["title"],
                "desc": live.get("note", ""),
                "params": live.get("params", []),
                "side_effect": live.get("side_effect", False),
                "destructive": live.get("destructive", False),
                "approval": live.get("approval", "auto")}
    s = SPECS.get(cap)
    if s:
        return s
    return {"title": cap, "desc": "未登记的能力（节点自描述缺失，按能力名兜底）", "params": []}


def describe(caps):
    """把一组能力名 -> {名: 说明书}，供节点注册时上报。"""
    return {c: spec_of(c) for c in caps}


def param_names(cap):
    return [p["name"] for p in spec_of(cap)["params"] if p.get("required")]


def params_of(cap):
    """说明书里的参数 -> 设备层的 Param 原语。

    返回 dict 列表（不返回 Param 对象）：这个文件是基线，必须能不依赖设备层单独 import。
    """
    return [{"name": p["name"],
             "type": TYPES.get(p.get("type", "str"), "str"),
             "required": bool(p.get("required")),
             "doc": p.get("desc", "")}
            for p in spec_of(cap)["params"]]


def known(cap):
    """这条能力有没有说明书（catalog 能力或节点协议动词）。"""
    return cap in SPECS or bool(_catalog_view().get(cap))


# ---------------------------------------------------------------- 自测

def _selftest():
    # 协议动词：节点协议层仍在用（node_agent 默认 exec,push,pull）
    assert spec_of("exec")["title"] == "执行命令"
    assert [p["name"] for p in params_of("push")] == ["path", "data"]
    assert params_of("push")[1]["type"] == "str", "base64 要落成 str"
    # 能力名以 catalog 为准
    assert spec_of("shell.exec")["title"] == "在编队节点上执行命令（节点侧白名单把关）"
    assert param_names("shell.exec") == ["cmd"]
    assert known("file.push") and not known("nope.nope")
    # 退役名不再有说明书：旧名只许落到兜底，不许再冒充真源
    assert spec_of("job.run")["title"] == "job.run", "退役别名必须已删除"
    s = spec_of("brand.new")
    assert s["title"] == "brand.new" and s["params"] == []
    assert len(describe(["exec", "push"])) == 2
    print("capabilities 自测：全过")


if __name__ == "__main__":
    _selftest()

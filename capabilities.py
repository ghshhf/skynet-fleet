"""能力自描述：节点不只上报能力「名字」，还上报它的「说明书」（参数 schema）。

为什么需要它：统一意图编排要「发现」编队里能做什么、需要什么参数——硬编码的
能力名（exec/ai/…）只能被广播，不能被编排。有了自描述，Commander（与她）才能
按意图去找「能干这件事的节点 + 怎么调它」。

约定：`SPECS` 登记已知能力；未登记的能力字符串自动兜底为「能力名即标题」，
保证任何节点上报的能力都有一份说明书，不会因缺失而不可发现。
"""

# 能力说明书：{能力名: {"title","desc","params":[{name,type,required,desc}]}}
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


def spec_of(cap):
    """取能力说明书；未登记的能力自动兜底，保证「可发现」。"""
    s = SPECS.get(cap)
    if s:
        return s
    return {"title": cap, "desc": "未登记的能力（节点自描述缺失，按能力名兜底）", "params": []}


def describe(caps):
    """把一组能力名 -> {名: 说明书}，供节点注册时上报。"""
    return {c: spec_of(c) for c in caps}


def param_names(cap):
    return [p["name"] for p in spec_of(cap)["params"] if p.get("required")]

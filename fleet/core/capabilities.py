"""能力自描述基线（合自 `origin/feat/capability-self-description:capabilities.py`）。

这个文件是 **schema 基线**，不是能力清单：
  - `SPECS` 是旧分支上已经写好的那份说明书，**原样搬过来，不重写**；
  - 设备层的能力名是 `domain.verb`（shell.exec / file.push），旧名是扁平的（exec / push），
    两者的对应关系放在 `ALIASES` 里，**由这一处负责翻译**，调用方不许各写一份映射；
  - 真正的能力清单在 `fleet/catalog.py`，那里没声明参数的能力，由注册表从这里继承。

为什么不直接并入 registry：基线要能被「还没进设备层的旧代码」单独 import
（比如 node_agent 注册时上报自己的说明书），所以它不许依赖本包里任何东西。
"""
from . import result  # noqa: F401  # 仅为包加载顺序，不用其实体

# —— 以下 SPECS 全文来自 origin/feat/capability-self-description:capabilities.py ——
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

# 新名（domain.verb）→ 旧名（扁平）。新增能力在这里登记一行即可，
# 漏登记不致命（会落到兜底说明书），但会失去参数说明。
ALIASES = {
    "shell.exec": "exec",
    "file.push": "push",
    "file.pull": "pull",
    "ctx.read": "ctx",
    "ctx.write": "ctx",
    "compute.run": "compute",
    "ai.chat": "ai",
    "persona.tick": "persona",
    "perceive.report": "perceive",
}

# 旧基线的类型词汇 → 设备层的类型词汇
TYPES = {"base64": "str", "str": "str", "int": "int", "bool": "bool", "json": "json"}


def spec_of(cap):
    """取能力说明书；未登记的能力自动兜底，保证「可发现」。"""
    s = SPECS.get(cap)
    if s:
        return s
    s = SPECS.get(ALIASES.get(cap, ""))
    if s:
        return s
    return {"title": cap, "desc": "未登记的能力（节点自描述缺失，按能力名兜底）", "params": []}


def describe(caps):
    """把一组能力名 -> {名: 说明书}，供节点注册时上报。"""
    return {c: spec_of(c) for c in caps}


def param_names(cap):
    return [p["name"] for p in spec_of(cap)["params"] if p.get("required")]


def params_of(cap):
    """说明书里的参数 → 设备层的 Param 原语（返回 dict，由注册表自己转成 Param）。

    刻意不 import registry 的 Param：基线文件必须能单独 import。
    """
    out = []
    for p in spec_of(cap)["params"]:
        out.append({"name": p["name"],
                    "type": TYPES.get(p.get("type", "str"), "str"),
                    "required": bool(p.get("required")),
                    "doc": p.get("desc", "")})
    return out


def known(cap):
    """这条能力在基线里有没有说明书（没有就说明是新能力，该补登记）。"""
    return cap in SPECS or cap in ALIASES


# ---------------------------------------------------------------- 自测

def _selftest():
    assert spec_of("exec")["title"] == "执行命令"
    # 新名要能翻译到旧基线
    assert spec_of("shell.exec")["title"] == "执行命令"
    assert param_names("shell.exec") == ["cmd"]
    assert known("file.push") and not known("nope.nope")
    # 未登记的能力不许抛异常，且要有兜底说明书（可发现性 > 精确性）
    s = spec_of("brand.new")
    assert s["title"] == "brand.new" and s["params"] == []
    # 旧的扁平名照样能用（node_agent 上报时就是扁平名）
    assert len(describe(["exec", "push"])) == 2
    assert [p["name"] for p in params_of("push")] == ["path", "data"]
    assert params_of("push")[1]["type"] == "str", "base64 要落成 str"
    print("capabilities 自测：7 组全过")


if __name__ == "__main__":
    _selftest()

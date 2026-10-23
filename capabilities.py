"""能力自描述：节点不只上报能力「名字」，还上报它的「说明书」（参数 schema）。

为什么需要它：统一意图编排要「发现」编队里能做什么、需要什么参数——硬编码的
能力名（exec/ai/…）只能被广播，不能被编排。有了自描述，Commander（与她）才能
按意图去找「能干这件事的节点 + 怎么调它」。

约定：`SPECS` 登记已知能力；未登记的能力字符串自动兜底为「能力名即标题」，
保证任何节点上报的能力都有一份说明书，不会因缺失而不可发现。

（2026-10-21 合入主线时补）设备层的能力名是 `domain.verb`（shell.exec / file.push），
旧名是扁平的（exec / push）；新旧名的对应关系只登记在 `ALIASES` 这一处，
由 `spec_of()` 统一翻译，任何调用方都不许再各写一份映射。
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
    "image.gen": {
        "title": "安卓出图（手机算力）",
        "desc": "在本节点挂载的安卓手机上跑 SD 出图（SdBridge/QueueRunner），返回 PNG 路径；"
                "PC 调度手机算力的实质落地（路线甲·N+2）",
        "params": [
            {"name": "prompt", "type": "str", "required": True, "desc": "正向提示词"},
            {"name": "neg", "type": "str", "required": False, "desc": "负向提示词"},
            {"name": "size", "type": "str", "required": False, "desc": "尺寸，如 512x512（按模型原生封顶）"},
            {"name": "steps", "type": "int", "required": False, "desc": "步数（Turbo 4~8，SD1.5 20+）"},
            {"name": "seed", "type": "int", "required": False, "desc": "随机种子，-1 随机"},
            {"name": "cfg", "type": "float", "required": False, "desc": "CFG 系数"},
            {"name": "model", "type": "str", "required": False, "desc": "模型文件（默认 Turbo）"},
        ],
    },
    "video.gen": {
        "title": "视频生成（壳 + 远端路由）",
        "desc": "生成一段短视频；手机本地算力撑不住视频扩散模型，本节点未配视频后端时"
                "如实说「未配置」，由编排层路由到 PC/云端视频节点（经 relay 端口转发）",
        "params": [
            {"name": "prompt", "type": "str", "required": True, "desc": "正向提示词"},
            {"name": "duration", "type": "int", "required": False, "desc": "时长秒（默认 3）"},
            {"name": "fps", "type": "int", "required": False, "desc": "帧率（默认 24）"},
            {"name": "size", "type": "str", "required": False, "desc": "尺寸，如 512x512"},
            {"name": "seed", "type": "int", "required": False, "desc": "随机种子，-1 随机"},
            {"name": "model", "type": "str", "required": False, "desc": "模型文件（留给后端）"},
        ],
    },
    "fleet.memory.sync": {
        "title": "跨设备记忆同步",
        "desc": "导出/导入本节点大脑 memory_store 的记忆，实现跨节点记忆同步",
        "params": [
            {"name": "op", "type": "str", "required": False, "desc": "export(默认) 或 import"},
            {"name": "items", "type": "list", "required": False, "desc": "import 时传入的记忆列表"},
        ],
    },
    "job.submit": {
        "title": "提交长任务（作业）",
        "desc": "把一项耗时任务提交成作业：可排队、可查询、可取消；节点掉线会退回队列换机重跑",
        "params": [
            {"name": "cap", "type": "str", "required": True, "desc": "要执行的能力名，如 image.gen"},
            {"name": "args", "type": "json", "required": False, "desc": "该能力的参数"},
            {"name": "node", "type": "str", "required": False, "desc": "指定节点，留空自动挑"},
            {"name": "max_attempts", "type": "int", "required": False, "desc": "最多尝试次数，默认 3"},
            {"name": "note", "type": "str", "required": False, "desc": "备注"},
        ],
    },
    "job.list": {
        "title": "作业列表",
        "desc": "按提交时间倒序列出作业，可按状态过滤",
        "params": [
            {"name": "state", "type": "str", "required": False,
             "desc": "queued/running/done/failed/cancelled"},
            {"name": "limit", "type": "int", "required": False, "desc": "最多几条，默认 50"},
        ],
    },
    "job.get": {
        "title": "作业详情",
        "desc": "查一个作业的状态、尝试次数、结果与状态历史",
        "params": [{"name": "id", "type": "str", "required": True, "desc": "作业 id"}],
    },
    "job.cancel": {
        "title": "取消作业",
        "desc": "取消排队中或运行中的作业；已终态的作业会如实说不必取消",
        "params": [
            {"name": "id", "type": "str", "required": True, "desc": "作业 id"},
            {"name": "reason", "type": "str", "required": False, "desc": "取消原因"},
        ],
    },
    "artifact.put": {
        "title": "收产物",
        "desc": "把一个文件或一段字节收进产物仓库，返回内容寻址 id（换机器也能引用）",
        "params": [
            {"name": "path", "type": "str", "required": False, "desc": "本机文件路径"},
            {"name": "b64", "type": "str", "required": False, "desc": "base64 内容（与 path 二选一）"},
            {"name": "kind", "type": "str", "required": False, "desc": "image/video/text/blob"},
            {"name": "name", "type": "str", "required": False, "desc": "显示名"},
            {"name": "job", "type": "str", "required": False, "desc": "关联作业 id"},
            {"name": "source", "type": "str", "required": False, "desc": "来源节点"},
            {"name": "ttl_days", "type": "float", "required": False, "desc": "保留天数，0=永久"},
        ],
    },
    "artifact.get": {
        "title": "取产物",
        "desc": "按 id 取回产物内容（base64）",
        "params": [{"name": "id", "type": "str", "required": True, "desc": "产物 id"}],
    },
    "artifact.list": {
        "title": "产物列表",
        "desc": "列出产物仓库里的内容，可按 kind/job 过滤",
        "params": [
            {"name": "kind", "type": "str", "required": False, "desc": "按类型过滤"},
            {"name": "job", "type": "str", "required": False, "desc": "按作业过滤"},
            {"name": "limit", "type": "int", "required": False, "desc": "最多几条"},
        ],
    },
    "skynet.relay": {
        "title": "SkyNet relay 状态",
        "desc": "返回本节点是否跑着 SkyNet relay 及其监听端口，供他机经端口转发取页",
        "params": [],
    },
    "skynet.relay.fetch": {
        "title": "经 SkyNet relay 代取网页",
        "desc": "经指定节点的 SkyNet relay 代取 URL，返回页面正文与状态码；"
                "relay 默认只绑 127.0.0.1，跨机需经端口转发",
        "params": [
            {"name": "node", "type": "str", "required": True, "desc": "目标节点名"},
            {"name": "url", "type": "str", "required": True, "desc": "要取的 URL"},
        ],
    },
    "phone.node.status": {
        "title": "手机算力节点现状",
        "desc": "返回手机上的推理服务是否在跑、装了哪些模型、剩多少内存、有哪些人格包",
        "params": [],
    },
    "memory.pack.list": {
        "title": "人格记忆包列表",
        "desc": "列出可用的人格包：每个包的指纹、片数与分层（身份/原则/技能/关系/经历）统计",
        "params": [],
    },
    "memory.pack.render": {
        "title": "渲染人格为 system prompt",
        "desc": "把人格包按预算渲染成一段 system prompt；identity 与 value 两层永不被裁掉，"
                "其余按时间衰减与查询相关度竞争预算",
        "params": [
            {"name": "pack", "type": "str", "required": True, "desc": "人格包名"},
            {"name": "budget", "type": "int", "required": False, "desc": "token 预算，默认 800"},
            {"name": "query", "type": "str", "required": False, "desc": "相关度查询"},
        ],
    },
    "memory.pack.diff": {
        "title": "人格包同步差异",
        "desc": "比对本地人格包与手机上已同步的内容，返回缺几片、多几片、是否已同步",
        "params": [{"name": "pack", "type": "str", "required": True, "desc": "人格包名"}],
    },
    "memory.pack.sync": {
        "title": "增量下发人格包到手机",
        "desc": "只把手机缺的分片推过去，推完两端重算 digest 校验；"
                "写手机磁盘，会改变「手机认为它是谁」",
        "params": [{"name": "pack", "type": "str", "required": True, "desc": "人格包名"}],
    },
    "phone.llm.ask": {
        "title": "用手机上的人格回答",
        "desc": "用指定人格包 + 手机上的权重回答一句话。同一份权重换包就换人，"
                "服务不用重启（实测首字延迟 ~130ms）",
        "params": [
            {"name": "ask", "type": "str", "required": True, "desc": "问什么"},
            {"name": "pack", "type": "str", "required": False, "desc": "人格包名，留空不带人格"},
            {"name": "n", "type": "int", "required": False, "desc": "最多生成多少 token，默认 160"},
            {"name": "budget", "type": "int", "required": False, "desc": "记忆渲染预算，默认 800"},
            {"name": "query", "type": "str", "required": False, "desc": "相关度查询"},
            {"name": "temp", "type": "float", "required": False, "desc": "温度，默认 0.7"},
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
    "image.gen.run": "image.gen",
    "video.gen.run": "video.gen",
    "fleet.memory.sync.run": "fleet.memory.sync",
    "job.run": "job.submit",
    "art.put": "artifact.put",
    "art.get": "artifact.get",
    "art.list": "artifact.list",
    "mem.list": "memory.pack.list",
    "mem.render": "memory.pack.render",
    "mem.diff": "memory.pack.diff",
    "mem.sync": "memory.pack.sync",
    "phone.status": "phone.node.status",
    "phone.ask": "phone.llm.ask",
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
    """说明书里的参数 -> 设备层的 Param 原语。

    返回 dict 列表（不返回 Param 对象）：这个文件是基线，必须能不依赖设备层单独 import。
    """
    return [{"name": p["name"],
             "type": TYPES.get(p.get("type", "str"), "str"),
             "required": bool(p.get("required")),
             "doc": p.get("desc", "")}
            for p in spec_of(cap)["params"]]


def known(cap):
    """这条能力在基线里有没有说明书（没有就说明是新的，该补登记）。"""
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
    # 旧的扁平名照样能用（节点上报时就是扁平名）
    assert len(describe(["exec", "push"])) == 2
    assert [p["name"] for p in params_of("push")] == ["path", "data"]
    assert params_of("push")[1]["type"] == "str", "base64 要落成 str"
    print("capabilities 自测：7 组全过")


if __name__ == "__main__":
    _selftest()

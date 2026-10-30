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
        "desc": "在本节点挂载的安卓手机上出图，返回 PNG 路径。后端三选一，按速度排："
                "local-dream（骁龙 QNN NPU，同一 prompt 比 sd.cpp 快数倍）、"
                "SdBridge/QueueRunner（sd.cpp，CPU/Vulkan）、PC sd-cli。"
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
    "image.upscale": {
        "title": "放大 4 倍（手机 NPU）",
        "desc": "把一张图在手机上做 4 倍放大（QNN 超分）。/upscale 是二进制协议："
                "body 传裸 RGB，尺寸与权重路径走 header，回 JPEG。"
                "性价比路线：512×512 出图 4s + 放大 4s = **8 秒拿到 2048×2048**，"
                "比直接出 1024×768（38s）又大又快。需先 /select 过模型（复用同一 runtime）",
        "params": [
            {"name": "image_path", "type": "str", "required": True,
             "desc": "要放大的图（image.gen 的产出）"},
            {"name": "upscaler", "type": "str", "required": False,
             "desc": "放大模型：realistic（写实，24MB）/ anime（动漫，11MB），默认 realistic"},
        ],
    },
    "video.gen": {
        "title": "视频生成（手机 NPU：稀疏关键帧 + 几何运镜）",
        "desc": "生成一段短视频。**时间轴是显式的**：运动由相机轨迹（zoom/pan/rotate）"
                "精确决定，扩散模型只补细节 —— 关键帧先按运镜 warp 过再喂 img2img，"
                "播放帧则用关键帧 warp + 交叉淡化合成，**不花算力**。"
                "实测 512x512：关键帧 2.7~5.4s/张，播放帧合成近乎免费，"
                "所以 3 秒 24fps 片只需 19 张关键帧而不是 72 张。"
                "反面教材（已废弃的旧做法）：逐帧 img2img、seed+i、denoise 0.35 —— "
                "帧间没有几何联系，观感是抽搐/无目的漂移（实测帧间差 12.3，新做法 3.4）。"
                "未配手机时退回 ffmpeg 占位片（黑底打字），那不是画面。",
        "params": [
            {"name": "prompt", "type": "str", "required": True, "desc": "正向提示词"},
            {"name": "duration", "type": "int", "required": False, "desc": "时长秒（默认 3）"},
            {"name": "fps", "type": "int", "required": False,
             "desc": "**播放**帧率（默认 24）。可以随便拉高，播放帧不花算力"},
            {"name": "gen_fps", "type": "float", "required": False,
             "desc": "**关键帧**密度（每秒生成几张，默认 6）。这才是真正烧算力的旋钮："
                     "6 ≈ 电影感、2~3 ≈ 慢运镜够用、1 ≈ 省到极限"},
            {"name": "motion", "type": "str", "required": False,
             "desc": "运镜：push_in(推近,默认)/pull_out/pan_left/pan_right/tilt_up/"
                     "tilt_down/dolly/sway/orbit/none"},
            {"name": "prompts", "type": "json", "required": False,
             "desc": '提示词时间表 [[秒, "提示词"], ...]（Deforum 的 prompt travel）：'
                     "到点换词，并在该点自动抬高 denoise 让画面真变，其余时刻保持低 denoise"},
            {"name": "size", "type": "str", "required": False,
             "desc": "尺寸，只允许 512x512/768x768/1024x1024/768x512/512x768/1024x768/768x1024（表外没有 patch，会出噪声）"},
            {"name": "seed", "type": "int", "required": False, "desc": "随机种子，-1 随机"},
            {"name": "model", "type": "str", "required": False,
             "desc": "模型 id（anythingv5 动漫 / chilloutmix、absolutereality 写实）"},
            {"name": "denoise", "type": "float", "required": False,
             "desc": "关键帧 img2img 强度（默认 0.28）。运动交给 warp，这里别调高，高了就是重绘"},
            {"name": "steps", "type": "int", "required": False,
             "desc": "首帧步数（默认 12）；后续关键帧自动按 65% 走（低 denoise 走不满轨迹，全步数纯烧时间）"},
        ],
    },
    "video.story": {
        "title": "漫剧成片（画布 + 分镜 + 字幕 + 配音）",
        "desc": "一份 board → 一条可直接发的竖屏漫剧。这一层对应市面上的"
                "「节点式无限画布」工作台（提示词→出图→图生视频→配音→剪辑），"
                "这里压成一次调用。**台词决定镜头时长**：先合成配音拿到真实语音秒数，"
                "镜头时长取 max(编剧写的, 语音+0.35s)，再反过来出画面 —— "
                "这是行业做法，也是上一版「时间帧不对」的另一半原因。"
                "分工：屏幕画面换成了手机 NPU 出的关键帧 + 几何运镜，字幕/转场/混音本地算。"
                "诚实标注：角色一致性只能靠文字锚点 + 固定 seed，做不到 IP-Adapter/LoRA 那级。",
        "params": [
            {"name": "board", "type": "json", "required": True,
             "desc": "分镜数据：{title, canvas(9:16/16:9/3:4/4:5/1:1/2.39:1), fps, gen_fps,"
                     " style, voice, bgm:{preset,gain}, characters:[{name,anchor}],"
                     " shots:[{prompt, sec, camera, cast, subtitle, transition(cut/fade/dissolve),"
                     " denoise, seed}]}"},
            {"name": "model", "type": "str", "required": False,
             "desc": "出图模型（默认跟 board.model）"},
            {"name": "dry_run", "type": "bool", "required": False,
             "desc": "只算账不出片：返回分镜数/秒数/关键帧数/预估耗时"},
        ],
    },
    "image.poster": {
        "title": "海报排版（出图 → 成品）",
        "desc": "把出好的图排成可直接发的成品海报：版式、留白、标题层级、强调色全部本地算"
                "（PIL + 系统字体，0.2 秒/张，不调模型不联网）。强调色从画面自己取，"
                "所以每张海报跟着图走，不是写死的品牌色。四种版式：poster/square/story/full。",
        "params": [
            {"name": "image_path", "type": "str", "required": True, "desc": "底图路径（image.gen 的产出）"},
            {"name": "title", "type": "str", "required": False, "desc": "主标题（按字数自动降字号，最多两行）"},
            {"name": "sub", "type": "str", "required": False, "desc": "副标题 / 一行说明"},
            {"name": "tag", "type": "str", "required": False, "desc": "左上角小标签（强调色底块）"},
            {"name": "layout", "type": "str", "required": False,
             "desc": "poster(1080×1350 上图下文) / square(1080×1080) / story(1080×1920) / full(全幅+底部渐变)"},
            {"name": "footer", "type": "str", "required": False, "desc": "页脚自定义文字"},
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
    "video.story.run": "video.story",
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

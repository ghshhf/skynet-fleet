# -*- coding: utf-8 -*-
"""llmgw.* —— 本地 LLM 网关协议栈（天网自有实现，不依赖别人的客户端）。

来源与许可（均为 MIT，可合法吃进）：
- OFM（Our Free Model）service 源码 MIT (Ebony-Vinyl)，本机 D:/Our Free Model/service
- zen-gate（LAGcomcom/zen-gate）Go 网关 MIT，本机克隆 D:/code/vendor-src/zen-gate

天网侧自有的部分：OpenAI / Anthropic 兼容客户端、端点健康探测、
key 自动发现（读各软件本地配置文件，不硬编码）、限流时多模型 fallback。
lane 协议（会话铸造/指纹门，zen-gate internal/lane 约 2.1 万行 Go）暂不移植，
经由本地网关转换——这一口径如实写进能力 note。
"""

from .core import Capability, Param

def _discover_ok(reg, dev):
    from .kinds.llmgw_device import discover_gateways, ANON_ENDPOINTS
    gws = discover_gateways()
    if gws:
        return True, ""
    if ANON_ENDPOINTS:
        return True, "无本地网关（OFM/zen-gate），但匿名端点（pollinations/llm7/kilo/OVH）可用"
    return False, "未发现任何网关"


LLMGW_CAPABILITIES = [
    Capability("llmgw.endpoints", "盘点本机 LLM 网关：OFM/zen-gate 自动发现 + 自定义端点，健康与延迟",
               params=(Param("base", "str", doc="自定义端点 base（http://127.0.0.1:PORT）"),
                       Param("key", "str", doc="自定义端点的 API Key（缺省只盘点自动发现的）")),
               side_effect=False, check=_discover_ok,
               note="[LLMGW] key 自动发现：OFM=~/.our-free-model/settings.json forwardKey；zen-gate=%APPDATA%/zen-gate/config.json mainKey"),
    Capability("llmgw.models", "列出网关可用模型清单（OpenAI /v1/models 协议）",
               params=(Param("endpoint", "str", doc="端点名 ofm/zen-gate 或自定义 base；缺省列全部"),
                       Param("key", "str", doc="自定义端点 key")),
               side_effect=False, check=_discover_ok,
               note="[LLMGW] 同上"),
    Capability("llmgw.chat", "对话补全（OpenAI 协议）。models 可给多个做限流 fallback，响应标注实际用的模型",
               params=(Param("messages", "json", True, "OpenAI 格式消息列表"),
                       Param("model", "str", doc="单模型；或用 models 给候选链"),
                       Param("models", "json", doc="候选模型链，前一个失败/限流自动换下一个"),
                       Param("endpoint", "str", doc="端点名 ofm/zen-gate 或自定义 base；缺省第一个可用"),
                       Param("key", "str", doc="自定义端点 key"),
                       Param("max_tokens", "int", doc="默认 1024"),
                       Param("temperature", "float", doc="缺省走网关默认"),
                       Param("timeout", "int", doc="秒，默认 120"),
                       Param("stream", "bool", doc="流式：返回 ttft_s/chunks/partial；"
                                                  "首字节前失败换上游，首字节后断流保留已得内容"),
                       Param("allow_paid", "bool", doc="允许第一轮就用额度型上游；"
                                                      "默认免费端点优先，额度型只做兜底"),
                       Param("session", "str", doc="粘性会话 id，30 分钟内复用同一 (网关,模型)"),
                       Param("compress", "int", doc="上下文压缩级别 0/1/2，默认 1")),
               side_effect=True, check=_discover_ok,
               note="[LLMGW] 真 token 消耗；走本地网关（OFM 18900 / zen-gate 8787）。"
                    "多上游聚合：匿名端点 → 本机网关 → 带 key 额度型（免费优先，额度兜底）"),
    Capability("llmgw.probe", "单模型体检：可用性 + 首字延迟（学 zen-gate 的探测口径）",
               params=(Param("model", "str", True, "模型 id"),
                       Param("endpoint", "str", doc="缺省逐个端点试到通为止"),
                       Param("timeout", "int", doc="秒，默认 30")),
               side_effect=True, check=_discover_ok,
               note="[LLMGW] 流式请求测 TTFB，只发一句 ping"),
    Capability("llmgw.embeddings", "向量嵌入（OpenAI /v1/embeddings）。家族内 failover：不同模型向量不混",
               params=(Param("input", "json", True, "字符串或字符串列表"),
                       Param("model", "str", doc="embeddings 模型 id；缺省自动挑"),
                       Param("endpoint", "str", doc="端点名；缺省全部"),
                       Param("timeout", "int", doc="秒，默认 60")),
               side_effect=True, check=_discover_ok,
               note="[LLMGW] embeddings 跨模型家族 failover 会被拒（向量维度/语义空间不兼容）"),
    Capability("llmgw.cooldowns", "查看当前冷却中的 (网关,模型) 对（429/5xx/timeout/auth 后自动冷却）",
               params=(), side_effect=False, check=_discover_ok,
               note="[LLMGW] FreeLLMAPI 模式：per-key cooldown + 自动解冻"),
    Capability("llmgw.sticky", "粘性会话：同一 session_id 30 分钟内复用同一 (网关,模型)，避免 mid-conversation 模型切换",
               params=(Param("action", "str", doc="list（默认）或 clear"),
                       Param("session", "str", doc="会话 id（clear 时指定；不给则清全部）")),
               side_effect=False, check=_discover_ok,
               note="[LLMGW] FreeLLMAPI sticky session 模式"),
    # ↓ 以下 5 项 handler 早已落地但一直没进 catalog（规划器看不见 = 等于不存在），
    #   这里补齐：能力清单与 _HANDLERS 必须一一对应（依赖清单 ↔ 生产代码互相盯）。
    Capability("llmgw.compress", "网关层上下文压缩：结构化内容去空白 + 超长截断，省 token 少撞上下文上限",
               params=(Param("messages", "json", True, "OpenAI 格式消息列表"),
                       Param("level", "int", doc="0=关 1=结构化去空白 2=再截断（默认 1）"),
                       Param("cap", "int", doc="level=2 时的单条字符上限，默认 16000")),
               side_effect=False, check=_discover_ok,
               note="[LLMGW] 纯本地零网络；RTK/Caveman 同思路的保守安全版"),
    Capability("llmgw.egress", "网络出口状态：免费端点直连即可用；环境若有 HTTPS_PROXY 则透明走（可选增强）",
               params=(), side_effect=False, check=_discover_ok,
               note="[LLMGW] 代理不是出网的前提，只是可选加速；LLMW_NO_PROXY=1 强制忽略"),
    Capability("llmgw.health", "可用性图谱：各 (网关,模型) 实时健康状态（流量驱动，已知死的降权/过滤）",
               params=(), side_effect=False, check=_discover_ok,
               note="[LLMGW] 连续失败≥3 且无近期成功 → 降权；一次成功即自愈"),
    Capability("llmgw.quota", "额度总览：借 OFM/zen-gate 自己探上游的结论 + 自建健康图谱，零配额成本",
               params=(Param("refresh", "bool", doc="强制重读外部文件（依然不发网络请求）"),),
               side_effect=False, check=_discover_ok,
               note="[LLMGW] 只读 ~/.our-free-model/availability.json 与 %APPDATA%/zen-gate/quota.json；"
                    "另报额度型上游「配没配 key」与投放窗口，不回显凭据"),
    Capability("llmgw.discover", "扫 new-api 系聚合站的公开元数据：模型清单/倍率/赠送额度/有无充值/投放窗口。零登录零额度",
               params=(Param("sites", "json", doc="站点列表，默认 agentrouter.org；可传多个"),
                       Param("timeout", "int", doc="秒，默认 10")),
               side_effect=False, check=_discover_ok,
               note="[LLMGW] 只读公开的 /api/status 与 /api/pricing，不碰账号与 key；"
                    "公告里的投放窗口会被解析成小时列表，喂给 quota 冷却"),
    Capability("llmgw.codex_login",
               "ChatGPT 订阅号 device-code 授权：无浏览器，一条能力走完 OAuth。"
               "连上后 codex 后端（chatgpt.com/backend-api/codex）自动进路由池",
               params=(Param("action", "str", doc="start（拿 user_code）/ poll（换 token，可带 wait 秒）"
                                               "/ status / logout；默认 status"),
                       Param("wait", "int", doc="poll 时阻塞等待秒数，默认 0（只探一次）")),
               side_effect=True, check=_discover_ok,
               note="[LLMGW] 凭据只落 ~/.fleet/codex_auth.json，能力输出永不回显 token。"
                    "auth.openai.com 按出口 IP 判地区，受限地区会 403，需换出口再试"),
]

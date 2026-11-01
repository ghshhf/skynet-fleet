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
                       Param("timeout", "int", doc="秒，默认 120")),
               side_effect=True, check=_discover_ok,
               note="[LLMGW] 真 token 消耗；走本地网关（OFM 18900 / zen-gate 8787）"),
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
]

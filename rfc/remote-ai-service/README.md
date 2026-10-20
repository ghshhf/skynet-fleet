# RFC：远程 AI 即服务（Thin-Client 模型）

> 分支 `rfc/remote-ai-service` · 与主线计划「本地每台都装 AI」不同的发散方向
> 状态：提案（未合入 main）

## 0. 一句话

不是每个设备都装得动 / 值得装一个 AI 模型。**AI 能力集中到有算力的少数节点，其余设备做瘦客户端：发 prompt + 上下文，收回文本/动作**——就像调一个本地服务器（用户原话：「就跟咱们调用服务器一样」）。

## 1. 问题

- 盒子 / 老手机 / 轻量 Windows 本：**跑不动 4B 以上模型**，或跑得极慢。
- **重复下载模型没意义**：同一份权重在 5 台设备各下一份 = 带宽浪费 + 版本漂移 + 存储翻倍。
- 手机的 AI（北极星/她）是最强端，但电视、盒子、网吧临时机不该各自养一份。
- 主线计划默认「各端按需装 AI」；本分支主张**默认不装，按需远程调**。

## 2. 架构

```
瘦客户端（无权重）           Commander（路由）             AI 节点（有权重）
┌────────────┐  ai.chat   ┌──────────┐  ai.chat   ┌──────────────┐
│ 电视/盒子   │ ─────────► │          │ ─────────► │ 手机 K80 Ultra │
│ 临时网吧机  │            │ 选「谁能 │            │ (4B, 12-18/s)  │
│ 老 Windows  │            │  干 AI」 │            │ 或电脑(更大)   │
└────────────┘ ◄───────── └──────────┘ ◄───────── └──────────────┘
   只发/收文本      (复用 dev_pick)        本地推理，回 chunk/result
```

- 瘦客户端**不持有权重、不做训练**；模型更新只在 AI 节点一处。
- 上下文跨设备走 **L3 共享上下文**（瘦客户端把会话上下文 ns 带上，AI 节点从 L3 读）。
- 与 `compute_runner` 的关系：`compute_runner` 跑「任意命令」；`ai.chat` 是「跑对话/人格推理」的特化通道，两者都经 Commander 路由，共享 `pick_node("ai", prefer=...)`.

## 3. 协议扩展（common.py 新增）

```python
AI_CHAT      = "ai.chat"       # client -> commander -> ai-node: {prompt, ctx_ns, model_hint, stream}
AI_CHUNK     = "ai.chunk"      # ai-node -> client: {id, delta}
AI_RESULT    = "ai.result"     # ai-node -> client: {id, text, model, usage}
```

`model_hint` ∈ {small, 4b, gpu}：客户端声明要什么档，Commander 路由到匹配节点。

## 4. 最小实现

- `remote_ai_client.py`：`ask(node, prompt, ctx_ns=None, model_hint=None)` 阻塞拿完整结果；可选 `stream=True` 逐块回调。
- AI 节点侧：在 `node_agent` 注册 `ai` 能力，收到 `AI_CHAT` 调本地推理接口（北极星 / 她 的推理封装），回 `AI_CHUNK`/`AI_RESULT`。
- 无 AI 节点在线时：Commander 返回 `no_ai_node`，客户端可降级到 `rfc/free-compute-relay`（免费外部端点）。

## 5. 边界与风险

- 瘦客户端把 prompt 发往 AI 节点 → **不离开编队内网**（Tailscale）就不出隐私问题；禁止把含隐私的 prompt 路由到外部端点。
- 延迟：同局域网/虚网友好；跨公网走 Tailscale 也 ok，但大模型生成长文本有首字延迟，客户端应流式。
- 失败：AI 节点掉线 → Commander 自动重路由到其他 AI 节点或降级外部端点，对客户端透明。

## 6. 与主线关系

主线 §五「她」默认手机常驻跑人格；本分支把「她」的推理能力**暴露给全编队**当服务，是主线「电脑=她的手跑重活」的自然延伸，但更彻底：手（电视/盒子）自己不装脑，脑在手机。

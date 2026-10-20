# Cherry Studio 拆解：能学什么、不能碰什么

> 拆解对象：[CherryHQ/cherry-studio](https://github.com/CherryHQ/cherry-studio)（42k+ stars，Electron+React+TS，AGPL-3.0）
> 拆解日期：2026-10-21 · 服务于「免费 AI 接入设备层」（docs/设备层扩展计划_2026-10-21.md 的 AI 段）

## 0. 🔴 法律红线（先说）

**AGPL-3.0**：一行代码都不能搬进本项目，连「参考着重写」都有传染风险。
**只学架构与产品思路，不碰任何源码、字符串、图标、文案。**
它家 MIT 的只有 `packages/provider-registry`（README 明示），但保险起见该包也只学schema思路。

## 1. 它是什么

桌面端 AI 聚合客户端：一个界面接 60+ 家模型商（OpenAI/Claude/Gemini/DeepSeek/硅基流动…）
+ 本地推理（Ollama/LM Studio）+ 300+ 预设助手 + MCP 工具调用 + 知识库。
对我们的参考价值集中在**它怎么管「几十家异构模型商」**这件事上。

## 2. 值得学的 7 件事（按对本项目的用处排序）

### ① 免费端点当「一等公民」，开箱即用零配置
它内置 `CherryAI (免费)` 这个 provider——用户不填任何 key 就能先聊起来。
**印证了咱们的路**：`ai.chat` 的默认后端 = pollinations（免 key 实测 0.6s 真回话），
首次启动零配置可用，这是产品体验的分水岭。

### ② provider 按「协议」分类，不按「厂商」分类
它的 provider 类型表：OpenAI 兼容 / Anthropic 兼容 / Gemini / Bedrock / **本地推理** / 特殊网关。
**对我们的直接启示**：一个 AI 后端 = `(protocol, base_url, model, 可选key)` 四元组，
pollinations 和本地 llama.cpp/Ollama **是同一个协议类的两个实例**——一个调用器吃遍所有。
llm.py 的 `FLEET_LLM_URL` 设计不用改，pollinations 就是另一个 base_url 而已。

### ③ 目录三份拆开，信任级别不同（provider-registry 的核心设计）
| 目录 | 内容 | 信任级 | 更新方式 |
|---|---|---|---|
| providers | 受信的端点定义 | 随应用打包 | 只跟版本走 |
| models | 模型清单 | 快照可远程更新 | 校验后**整体原子切换** |
| provider-models | 每家模型的覆盖/别名 | 快照可远程更新 | 同上 |

铁律（他们 README 原话意译）：**远程未签名数据可以更新模型清单，但绝不能碰受信的
连接定义和凭证**。更新失败 = 当前视图照常可用。
→ 咱们的 AI 后端登记表照抄这个分层：免 key 默认表（代码里）与用户自定义表
（devices.json/环境变量）分开；永远不允许外部数据注入凭证。

### ④ 数据文件绝不手改：生成器 + 内容哈希版本
`data/*.json` 是生成物，改 `src/creators/` 后跑 `generate-catalog.ts` 再生成，
文件带 content-hash version，哈希只证身份不证新旧（先后要看发布流）。
→ 咱们的对应物：能力清单 `catalog.py` 已经是「唯一真相」，同理**别绕过它手写**
终端补全或 help；将来 AI 后端表若落盘，也走生成+哈希，不手改。

### ⑤ 每个后端两个按钮：`获取模型列表` + `检测`
配好 base_url 后：GET `/v1/models` 枚举模型；`检测` 用任一模型真发一条验证连通。
→ 咱们的 `ai.status` 就该这么做：**对每个后端真探测**（不是报登记表），
返回 `(ok, 原因)`——与 fleet 的 probe() 契约完全同构，等于白捡的印证。

### ⑥ 流式解析必须容错
他们专门 patch 了 `@ai-sdk/openai-compatible` 来容忍**不完整的 tool_calls 流片段**。
→ 咱们的 SSE 解析（llm.py `_stream_http`）要补同类防御：残缺 JSON 行跳过不炸，
这正是 t2/t3 门禁之外的第三个「事故型 eval」候选。

### ⑦ 多模型同问对比
同一条 prompt 并发发给多个适配器、并列展示。→ 咱们 `ai.chat` 加个
`all_backends=true` 参数就能白拿这个能力（就 2 个后端，对比成本低）。

## 3. 不值得学 / 不适用

| 项 | 为什么不学 |
|---|---|
| Electron/React 技术栈 | 咱们是 Textual + stdlib，重量级完全不同 |
| 300+ 预设助手 | 与「终端优先」路线冲突，是产品形态差异不是工程差距 |
| 知识库 UI、WebDAV 备份 | fleet 的记忆走 TinyContext（E-7），不重复造 |
| 多 Key 轮询、`#` 固定路径 | 记下作为将来付费端点的扩展位，现在免 key 用不上 |

## 4. 落到咱们 AI 接入方案的修订

原方案（已拍板：pollinations + 本地模型 + AI 标签页）不变，按 Cherry 补 4 条：

1. **后端四元组数据化**：`{name, protocol:"openai-compat", base_url, model, key_env?}`，
   免 key 默认表（pollinations）+ 用户表（FLEET_LLM_URL 的本地端）分两层
2. **ai.status 真探测**：逐后端发一条 1-token 请求验连通，结果带时延
3. **SSE 解析加容错** + 补一条事故型 eval（残缺流片段不炸）
4. **LLM7 按可选后端登记**（`FLEET_LLM7_TOKEN` 有则活、无则自动离线）——
   实测它已改收费 token 制（每天 10 万 token 免费），不再是免 key；kilo 误报剔除、
   ovh 老地址已 404

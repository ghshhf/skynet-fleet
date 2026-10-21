---
name: self-improving-agent
version: "1.0.0"
display_name: "自我改进（self-improving-agent）"
display_name_en: "self-improvement (self-improving-agent)"
description: "self-improvement capability in the vertical scenarios and business APIs domain. Use when the user wants to 捕获学习、错误和纠正，让 Agent 自动积累经验并持续改进. Triggers: self-improving-agent, session-logs. Not for requests unrelated to self-improvement. Risk level L1. 中文触发：self-improving-agent、session-logs。"
description_zh: "捕获学习、错误和纠正，让 Agent 自动积累经验并持续改进。（类别：七、垂直场景与商业 API 类；风险等级：中（持久化记忆可能被污染））当用户需要自我改进相关能力、或提到 self-improving-agent、session-logs 时使用；与自我改进无关的请求不要触发。"
description_en: "self-improvement capability in the vertical scenarios and business APIs domain. Use when the user wants to 捕获学习、错误和纠正，让 Agent 自动积累经验并持续改进. Triggers: self-improving-agent, session-logs. Not for requests unrelated to self-improvement. Risk level L1."
agent_created: true
---

# 自我改进（self-improving-agent）

## 用途

捕获学习、错误和纠正，让 Agent 自动积累经验并持续改进。

> 分类：七、垂直场景与商业 API 类（vertical scenarios and business APIs）  
> 技能类型：自我改进  
> 风险等级：中（持久化记忆可能被污染）  
> 门禁档位：**L1** — 涉及读写个人数据或外部账号，先列影响面，关键写操作逐项确认。

## 触发条件

- 用户提到 `self-improving-agent`、需要自我改进能力，或提出对应诉求时触发。
- 典型触发词：self-improving-agent、捕获学习、错误和纠正，让 Agent 自动积累经验并持续改进。
- 关联技能：session-logs。

## 何时不要使用

- 与自我改进无关的请求：不要触发，交给更合适的技能或直接回答。
- 前置依赖未就绪（未授权 / 缺 CLI / 缺网络）时：**停下并报告**，不要降级为让用户粘贴明文凭据。
- 超出本技能声明范围的目标（其他系统、其他账号）：不越权操作。

## 功能清单

- 捕获学习点、错误与纠正
- 自动积累经验
- 持续优化后续表现

## 标准操作流程

1. 严格区分测试/生产环境，生产密钥绝不明文出现在命令或日志。
2. 涉及资金、交易、订单、客户数据的写操作，必须逐项取得明确授权。
3. 优先在沙箱/测试账户验证流程，再过渡到生产。
4. 遵循最小权限：仅申请业务必需的 scope/权限。
5. 每次操作后回报状态、金额/数量、以及异常时的止损步骤。

## 外部依赖与前置检查

- 依赖：本地记忆/日志存储
- 执行前确认依赖已就绪（账号已授权 / CLI 已安装 / 环境变量已设置）。
- 凭据通过环境变量或密钥管理器注入，不落盘、不进版本控制。
- 详细配置见 `references/setup.md`。

## 典型使用场景

1. 长期项目的经验沉淀
2. 减少重复犯错

## 使用提示

建议定期审阅积累的经验，避免错误信息被固化。

## 备注

- 关联技能：session-logs。
- 本技能由规划规格 README 自动落地生成，操作细节（具体 API/CLI/连接）应在接入真实运行时补全。
- 通用铁律：先验证现状 → 列影响面 → 取得确认 → 再执行；不可逆或涉钱/涉密的动作用最高门禁。

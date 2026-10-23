# MemFlywheel 学习笔记

> 仓库：https://github.com/iflytek/memflywheel （Apache-2.0，Node ≥22，pnpm monorepo）
> 学习日期：2026-10-22　方法：shallow clone 后通读 README.zh.md / docs/architecture.md / docs/comparison.md
> 定位：**不直接装，搬机制**。原因见 §5。

---

## 1. 它解决的是什么问题

一句话：**给 Agent 加“记忆飞轮”——执行前召回、执行后沉淀、重复流程变成技能。**

它不碰模型、鉴权、工具权限，只管「记忆与学习闭环」这一层。宿主（Pi / Hermes / OpenCode / OpenClaw）继续管会话生命周期和工具，它挂上去当记忆插件。

这个边界划得很干净，也是它对我们最有价值的地方：**它把“记忆”当成一个有生命周期的内核，而不是一堆写死的 markdown。**

---

## 2. 四个零件

### 2.1 存储：文件原生，目录即类型

```
<root>/
├── MEMORY.md            # 派生索引（可重建，永不手改）
├── identity/  preference/  style/
├── workflow/  context/    ambient/
├── .memflywheel/sources/   # 清洗后的执行轨迹 JSONL（不进索引）
├── .memory-task-lock  .consolidate-lock  .last-extraction
└── .audit.log            # 只追加的审计
```

每条记忆 = YAML frontmatter + 正文：

```markdown
---
name: 用户称呼
description: 用户偏好的称呼        # 一行路由摘要，不是正文摘要
type: identity
retrieval_terms: [用户称呼, preferred name]   # 给检索用的短词，不抄正文
---
叫用户小钟。
```

六类固定类型（identity / preference / style / workflow / context / ambient），但文档明说这是**默认值不是封闭分类**，宿主可以加自己的目录 —— 只要 adapter、扫描规则、prompt 契约三边对得上。

### 2.2 召回：渐进式，只注入索引

`buildContext()` 是纯确定性流水线：scan → sync index → read → truncate → aging hints，返回两段：

- `systemPrompt`：稳定的记忆规则（**cache 友好的前缀**，不随记忆变化）
- `preludePrompt`：索引本身（或检索出的子集），包在 `<system-reminder>` 里

主模型只看到索引行，**它自己决定要不要 Read 正文**。这就是“渐进”二字：先给目录，再给章节。

索引硬上限：**200 行 / 25 000 字节**，超了截断并加标记；`context`/`ambient` 这类会过期的时间敏感类型还会被 `applyAgingHints` 加时效提示。

embedding 预召回是**可选**的：不配也能用；索引超过约 200 条后才值得开。开了也只对**索引行**做 embedding + BM25 + RRF 混排，**正文永远不做向量** —— 这一步很省钱，也很克制。

### 2.3 提取：turn-end 跑一个带工具的子 Agent

不是“总结一下对话”，而是：拿写锁 → 按游标取新消息 → 清洗后追加进 `sources/session-<hash>.jsonl` → 交给注入的 `ExtractionAgentRunner`（一个有 glob/grep/read + write/edit/bash 的子 Agent）→ **它的工具调用本身就是改动** → 把顶层散落文件归位 → 重建索引 → 成功才推进游标。

关键设计：写记忆时自动挂 `## Sources` 指向 JSONL 的行号区间。**每条记忆都能溯回原始对话的第几行到第几行**，多个记忆共享同一段来源就共享同一个引用。

### 2.4 Dream：空闲时整理，不是又总结一遍

两段式，拿 `consolidate-lock`：

1. **确定性预清理（无 LLM）**：删正文完全相同的重复、把路径和 type 不匹配的文件归位。
2. **语义整理（子 Agent）**：读全文，合并、压缩、重判类型，仍然是通过普通文件工具直接改。

触发条件：距上次 ≥ **24 小时** 或 ≥ **5 个会话**。

### 2.5 技能飞轮

重复出现的流程 → 演化为 learned skill（先写进暂存区，校验包结构/路径范围/目标树没被外部改过，才发布，失败回滚）。同时让 dream 把冗余的流程记忆压缩成「一条指向该技能的线索」—— 细节从记忆搬进技能，记忆只留指路牌。

这就是 flywheel：**记忆 → 技能 → 更短的记忆 → 更快的下一轮。**

---

## 3. 横切保障（这块最容易被自己写漏）

| 项 | 做法 |
|---|---|
| 隐私 | `<private>…</private>` → `[REDACTED]`；可选 `refuseSecrets` 硬拒 SSH/PEM、api-key/token 前缀、bearer/JWT、`password:`/`cookie:` 赋值，返回打码后的命中位置 |
| 并发 | 每 root 一把文件锁 `.memory-task-lock`，带 stale 检测（持锁进程崩了能接管） |
| 写入 | 原子写：临时文件 + rename |
| 审计 | `.audit.log` 追加记录 写/删/归档 |
| 解析 | 手写 frontmatter 解析，只读头 2048 字节，块必须在限定行数内闭合，`name` + 合法 `type` 缺一即忽略该条 |

---

## 4. 与我们现有记忆体系的对照

现状：`~/.workbuddy/.../MEMORY.md`（用户级规则）+ `D:/.workbuddy/memory/{MEMORY.md, YYYY-MM-DD.md}`（项目级，手写）。

| 能力 | MemFlywheel | 我们 | 差距后果 |
|---|---|---|---|
| 索引 | 派生、可重建、有硬上限 | 手写，越写越长 | 每次会话都在烧 token 读冗余 |
| 召回 | 索引 → 正文 → 证据 三层，可选向量 | 靠“先读 MAP.md 再下钻”的人工纪律 | 纪律靠自觉，会漏读 |
| 溯源 | 自动挂 `## Sources` 行号区间 | 靠纪律“只写跑出来的” | 无法自动核验，推测混进结论 |
| 沉淀 | turn-end 子 Agent 自动提取 | 我手动追加日志 | 该记的常忘，不该记的写一堆 |
| 整理 | dream 定时去重/合并/重判类型 | 无 | 只进不出，30 天后手动蒸 |
| 安全 | 脱敏 + 原子写 + 锁 + 审计 | 无 | 写一半崩了就坏档；密钥可能被写进记忆 |

**结论：我们的痛点它都有对应零件，而且它的默认值（200 行 / 25KB / 24h / 5 会话）正好压在我们现在最疼的地方。**

---

## 5. 为什么不能直接装

1. **宿主不匹配**：它只做了 Pi / Hermes / OpenCode / OpenClaw 四个 Node 宿主的适配层。我们的宿主是 Python 侧（天网 `fleet`）+ WorkBuddy 自身，接不上。
2. **语言栈不同**：core 是 TypeScript，我们记忆相关逻辑在 Python / markdown。
3. **它要接管 turn 生命周期**：我们这边的 turn 由 WorkBuddy 管，插不进它的 pre-recall / turn-end 钩子。

**但 `@memflywheel/core` 是“纯文件系统 + 注入 ports”，机制可以照搬自己实现** —— 这也是主人明确认可的「学习/适配」路线，不是抄代码。

---

## 6. 如果要落地，我会这么搬（三步，每步有验收）

### 步一：记忆核（最省事、收益最大）
`tools/memory_kernel.py`，只做确定性部分，**不碰 LLM**：

- `scan(root)`：遍历 `type/*.md`，解析 frontmatter，按 mtime 倒序，上限 200 条
- `sync_index(root)`：重写 `MEMORY.md`，一行一条 `- [name](type/file.md) - description`
- `build_context(root, query=None)`：返回 `(规则前缀, 索引块)`，索引截断到 200 行 / 25KB
- `write_doc()`：脱敏 → 临时文件 → rename → 追加审计

验收：拿现有的 `D:/.workbuddy/memory/` 跑一遍，生成的索引 ≤200 行；`build_context` 输出字节数 < 25KB；连跑两次索引内容一致（幂等）。

### 步二：溯源闸（治“推测混进结论”）
每条记忆必须带 `## Sources` 指到 `memory/sources/*.jsonl` 的行号区间；**没 Sources 的条目只许进 `pending/`，不许进正式目录**。索引里给 pending 单独一段。

验收：手写一条没有 Sources 的记忆，`scan()` 必须把它算作 pending 而不是正式条目。

### 步三：dream（最晚做，风险最高）
只在「≥24h 或 ≥5 会话」时跑，且**第一段（确定性去重/归位）先只跑不带 LLM 的版本**。语义合并那步等前两步稳了再说。

⚠️ 我们的 dream 必须比上游多守一条：**主人的「强制规定」区是只读的，dream 不许合并、压缩、删除、改措辞。** 上游没有这个概念（它假设所有记忆都是可演化的），我们有铁律，这是硬差异。

---

## 7. 三个不能照搬的地方

1. **不许让模型自动写“结论”**。上游的子 Agent 直接 write 记忆，等于把推测也写进去。我们的提取契约要加一句硬约束：**没有可指向的证据（命令输出/文件路径/报告）的句子，一律写进 `pending/`，并在正文首行标「待核」。**
2. **铁律区只读**（见步三）。规则不会被“整理”掉，是这套系统能长期信任的前提。
3. **不引入 embedding 也别心虚**。上游自己说这是可选项；我们索引短，BM25/关键词足够。先不上向量，避免又一个要维护的服务。

---

## 8. 一句话总结

**它把「记忆」从一次性写死的文件，做成了有扫描、有索引、有提取、有整理、有审计的流水线；我们缺的正是这条流水线的确定性骨架，而它的 LLM 部分恰恰是我们最该谨慎对待的部分。**

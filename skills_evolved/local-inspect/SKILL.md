---
name: local-inspect
description: 本机巡检：读系统信息、进程列表、运行状态、已装应用、卡片自检、AI 后端状态
agent_created: true
license: internal
evolved_from: [7557, 7559, 7561, 7563, 7567]
caps: [pcctl.sysinfo, pcctl.proc.list, pc.state, host.apps, ai.status]
guard: 任一步失败即停，不许继续往下试
---

# local-inspect

本机巡检：读系统信息、进程列表、运行状态、已装应用、卡片自检、AI 后端状态

## 步骤

1. 调 `pcctl.sysinfo`（证据 journal#7557）
2. 调 `pcctl.proc.list`（证据 journal#7559）
3. 调 `pc.state`（证据 journal#7561）
4. 调 `host.apps`（证据 journal#7563）
5. 调 `ai.status`（证据 journal#7567）

## 失败怎么办

- 任一步失败即停，不许继续往下试
- 出错就停，把错误原样报出来，不许自己换一条路继续。

## 这张卡从哪来

- 来自 trace `exec:inspect` 的实测执行（证据 id：7557, 7559, 7561, 7563, 7567）。
- 它证明的是「这些能力按这个顺序跑通过」，不证明「在任何机器上都成立」。

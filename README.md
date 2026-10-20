# 天网工程 · Skynet Fleet

> 个人多设备统一调用控制面 + 本地「她」人格层。
> 把你的 Windows / Linux / Android / Apple / 路由器 收敛成**一个「在动」的系统**，而不是一堆互相割裂、各管各的控制面。

这是个人设备编排工具。**只部署在你拥有或经授权管理的设备上。** 详见文末「安全与伦理边界」。

---

## 当前进度

- 主线交付 **Windows + Linux**（纯 stdlib，便携 Python 3.12 即可跑）；自测走 loopback 回路，不碰真机。
- 已落地并**全自动自测通过**（loopback 多节点，不碰真机）：
  - **L0 连通 + 认证**：Commander 控制面，节点密码认证后注册、维持心跳、上报能力。
  - **L1 执行 + 文件反向推送**：本机经密码在另一台机器上执行命令 / 推拉文件（单份工具箱，不各端自拉）。
  - **L1 常驻**：`install_windows.py` 把节点装成 SYSTEM 级计划任务（开机自起 / 隐藏 / 最高权限 / 断线重连），卸载须经**密码闸 或 配对设备一次性令牌闸**。`install_linux.py` 同款落地：systemd root 服务（`User=root`、`WantedBy=multi-user.target`、`Restart=always`），同一套双闸卸载。
  - **统一扇出**：一条 `broadcast` 指令同时抵达所有节点（统一调用平面，不割裂）。
  - **L3 共享上下文（TinyContext 式）**：跨设备同一份记忆，逐条可删（E-7）；节点经协议读写，她的节点能跨设备读到（海马体）。
  - **离线队列 + 上线重放（Phase 4 体验）**：节点离线时命令进队，上线自动补，不手动重发。
  - **她 P4 能力请求 + 授权门**：她的节点发 `cap_req` → 路由到用户批准 → `cap_grant` 限时会话（拒绝则执念+1）。人格/训练交手机（不在此仓库训）。
  - **她的人格运行时（P3/P4/P5）**：节点侧 `--persona-daemon` 跑心念 tick——到点先过刹车（频控 ≤2/h + 静默时段 + 重要度），再主动开口（`her_speak`）+ 发能力请求；批准=限时会话+心情↑，拒绝=执念+1，被抑制的话只写进她的「心事」（L3，逐条可删）。
  - **人格唯一性（定调）**：全编队**统一由一个模型**承载她——人格/记忆/状态都注入同一个 model，节点是薄客户端、**不各自孕育人格**（故不存在人格分裂）；`persona_state.json` 只属「她本体节点」。详见计划书 §5.1。
  - **反向推送 bootstrap（第三原则）**：中心起迷你 HTTP 分发几 KB bootstrap + 工具箱，loopback 字节校验一致。
  - **共享算力池（§九 dev_pick）**：节点上报 CPU/GPU/模型；Commander 按「谁能干」路由计算任务；`computemap` 批量分发多节点聚合；结果进共享上下文缓存，全编队复用（更加一步共享）。
  - **远程 AI 即服务（RFC 合入，真·流式）**：不是每台设备都装得动 / 值得装 AI——瘦客户端（电视/盒子/临时网吧机/老本子）**不持有权重**，经 Commander 把 prompt + L3 共享上下文路由到「有脑」的 AI 节点（她本人跑在手机/盒子），**一边想一边说**逐段回吐；`ai`/`ainodes` 两条 REPL + `remote_ai_client.py` 瘦客户端（`--stream`）。
  - **本地推理多后端接入（`llm.py`）**：`FLEET_LLM_CMD`（如 `ollama run qwen3:4b` / `llama-cli ... -p {prompt}`）或 `FLEET_LLM_URL`（llama.cpp server / MLC / Ollama / vLLM 的 OpenAI 兼容端点，SSE 流式）——**改环境变量即切换，业务代码零改动**；都没有则占位降级，链路仍端到端跑通。共享算力与远程 AI 共用同一个「她的大脑」。
  - **控制面审计（`audit.py`）**：append-only JSONL，统一调用面的每个动作留痕（`node.up/down`、`exec`、`token.issue`、`her.speak/grant/deny`、`ai.route/timeout`），纯旁路不改控制流；REPL `audit [n]` 查看、可导出归档。
- **延后**（等真机/拍板）：Android `adb` 真机推、Apple macOS SSH / iOS 屏共、路由器 OpenWrt 中枢接入、临时节点（网吧）会话注入、`她` SFT 模型训练（交手机/盒子跑）。

自测命令（loopback 多节点，不碰真机）：

```bash
python commander.py --self-test --password demo123          # L0–L3 + 她（授权+运行时）+ 反向推送 + 共享算力 + 远程 AI(流式) + 审计
python install_windows.py --selftest --password demo123     # Windows L1 常驻 + 令牌闸（需管理员）
python install_linux.py   --selftest --password demo123     # Linux systemd root 常驻 + 令牌闸（需 root）
```

---

## 四原则（设计铁律）

1. **锚定最底层 OS 平台层**：只对接 Android / Windows / Linux / Apple 四大平台原生能力，绝不对接厂商 App / 云。米家 leaf（灯/插座/传感器）是 MCU+RTOS 非安卓，经你自控的 Linux/Android 网关桥接；Apple 标「半可达」（仅屏共/快捷指令）。
2. **单一自包含工具箱 + 反向推送**：不要求每台设备各自去公网下载 agent；由手机/家服持一份自包含工具箱（二进制+依赖+配置+幂等安装脚本），接触即推。
3. **病毒式执行模型（同源 RMM/MDM/远控）**：一次进入 → 提权到最高 → 无弹窗 → 常驻后台 → 可远程 → 叠能力。区分合法/恶意只凭三点：归属（是否有权管理）、同意（本人或授权人许可）、范围（只部署在有权限设备、不向他人扩散、不窃他人数据）。
4. **无条件最高权限 + 常驻 + 零运行时分支**：agent 内部不设「能不能推 / 是不是家」之类 if——拿到 admin 即提满、常驻、静默、可远程；**边界靠两道外部闸**（非运行时逻辑，故无 bug）：①管理员启动闸（OS 强制，无 admin 提不上去）②环境复位闸（重装/合规清理自清）。

> **L1 落点**：Windows = SYSTEM 服务 / 计划任务；Linux = systemd root；Android = 已 root 应用 UID。跨重启常驻；重装系统即清——**这是特性不是缺陷**（不被「删不掉」反噬）。L2 内核驱动（抗本机管理员卸）与 L3 固件级均**不做**。

---

## 四层架构

```
L0  连通          Tailscale 虚网 / 直连    → 节点密码认证、心跳、在线表、能力注册
L1  注册/存在感    节点 agent              → 常驻、远程执行、文件推拉、算力画像
L2  统一调用平面    Commander              → 一条意图扇出多端协同（broadcast / compute 路由）
L3  共享上下文      TinyContext 式         → 跨设备记忆 / 上下文共享（已落地，可逐条删）
L4  体验/离线队列   「她」人格层            → 主动性、连续存在、情绪态、能力请求授权门（脚手架已落地）
```

---

## 目录结构

```
skynet-fleet/
├── common.py            # 长度前缀 JSON 协议 + exec 白名单 + 全部消息类型
├── commander.py         # L0 控制面 + L1 执行枢纽 + L2 路由 + L3 上下文 + 她路由 + 共享算力 + REPL + 自测
├── node_agent.py        # 被控端：连/认证/注册(带能力)/exec/push/pull/ctx/compute/ai/心跳 + 断线重连 + 她的人格运行时
├── install_windows.py   # Windows L1 常驻安装器（SYSTEM 计划任务 + 密码/令牌双闸）
├── install_linux.py     # Linux L1 常驻安装器（systemd root 服务 + 同款双闸）
├── memory.py            # L3 共享上下文（SQLite+FTS5，逐条可删）
├── persona.py           # 她脚手架（人格态/心念tick频控/能力请求门/红线）；人格态只属「她本体节点」
├── bootstrap.py         # 第三原则反向推送：中心分发 bootstrap + 工具箱
├── compute_runner.py    # 共享算力：节点侧本地推理接口（委托 llm.py）
├── llm.py               # 本地推理适配层：多后端（CMD/URL/占位）+ 真·流式，改环境变量即切换
├── audit.py             # 控制面审计：append-only JSONL，统一调用面动作留痕
├── remote_ai_client.py  # 远程 AI 即服务：瘦客户端（不装权重，经 Commander 调她，支持 --stream）
├── platform_android.py  # Android 节点入网（adb 反向推送）
├── platform_apple.py    # Apple 桥接（macOS SSH / iOS 屏共 半可达）
├── platform_router.py   # 路由器/OpenWrt 节点（常开中枢候选）
├── docs/
│   └── 个人设备编队与本地她_计划书.md   # 完整计划书（任务书）
├── README.md
└── LICENSE
```

---

## 真机部署（Windows）

```bash
# 1) 一台当 Commander（建议走 Tailscale 虚网，不在公网裸奔）
python commander.py --password 你的密码 --host 0.0.0.0 --port 9000

# 2) 另一台装常驻节点（管理员跑一次即永久常驻、无弹窗）
python install_windows.py --install --name PC-B --host <CommanderIP> --port 9000 --password 你的密码
sudo python3 install_linux.py --install --name BOX-A --host <CommanderIP> --port 9000 --password 你的密码

# 3) 控制台
nodes                      # 看在线节点 + 能力
exec PC-B ipconfig         # 单点执行
broadcast echo 统一指令    # 一条意图扇出所有节点
ctx_put her key 值         # 写共享上下文（跨设备记忆）
compute 帮我写段代码        # 路由到算力节点跑重活（她三段式 ③）
computemap a;b;c          # 批量分发多算力节点聚合
computepool               # 看算力池
ainodes                   # 看 AI 池（有脑/有权重的节点）
ai 大肥鱼               # 远程 AI 即服务：经 Commander 调她（真·流式）
audit 20                 # 看控制面审计流水（谁在什么时候调了什么）
cap / token ...           # 她能力授权 / 配对设备令牌
```

> **接真模型（改环境变量即可，业务代码零改动）**：在「她」所在的节点上设
> ```bash
> export FLEET_LLM_CMD='ollama run qwen3:4b'                 # 或
> export FLEET_LLM_URL='http://127.0.0.1:8080' FLEET_LLM_MODEL=qwen3-4b   # llama.cpp server / MLC / vLLM
> ```
> 不设则占位降级（链路仍通）。瘦客户端流式：`python remote_ai_client.py --ask "你好" --stream --ctx-ns her`。

> 真部署须收紧 exec 白名单并走 Tailscale；当前白名单仅含演示命令。

---

## 路线图（分支线）

```
决策分叉 ── 持久化深度 ── L1 Win(SYSTEM)/Linux(systemd root) ✓(已建+自测) ── L2 内核驱动（延后）
           └─ 平台 ── Windows ✓ + Linux ✓(systemd，已建+自测) ── Android / Apple / 路由（适配器已写，延后真机）
           └─ 节点性质 ── 常驻 ✓ ── 临时(网吧会话注入)（延后）
           └─ 认证 ── 密码 ✓ + 配对设备一次性令牌 ✓ ── 手机扫码入网（延后）
           └─ 推送 ── 反向推送 ✓(bootstrap+文件) ── 手机 adb 真推（延后）
           └─ 调用 ── 单点 exec ✓ + broadcast 扇出 ✓ + 共享算力路由 ✓ + 远程 AI 即服务(真·流式) ✓ ── 统一意图编排（延后）
           └─ 上下文 ── L3 共享 ✓(已建+自测) ── 与她记忆深绑（延后训练）
           └─ 推理 ── 多后端接入 llm.py ✓(CMD/URL/占位) ── SFT 人格训练(交手机)（延后）
           └─ 可观测 ── 控制面审计 audit.py ✓(已建+自测) ── 面板/告警（延后）
           └─ 她 ── P4 授权门 ✓ + P3/P5 人格运行时 ✓(脚手架) ── SFT 人格训练(交手机)（延后）
```

---

## 安全与伦理边界

- **只管自己的设备**：部署在你拥有或被明确授权管理的机器上；不向无权/陌生机器推送，不窃取他人数据。
- **不碰恶意线**：不做 UAC 静默绕过（属提权漏洞利用）、不用 BYOVD（借漏洞签名驱动）、不写固件级持久化。
- **可清除**：L1 常驻重装即清；卸载须经密码闸门（`install_windows.py --uninstall --password ...`）。
- **合规兜底**：临时/公共节点（如网吧）采用无状态执行端 + 身份随身（记忆留手机侧），下机即清、不落凭据，与场所合规清理互补。

---

## 免责声明

个人项目，按「自有/授权设备」前提使用。作者不对任何误部署、越权使用或由此引发的后果负责。

MIT License © 2026 ghshhf（黄果强）

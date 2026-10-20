# 天网工程 · Skynet Fleet

> 个人多设备统一调用控制面 + 本地「她」人格层。
> 把你的 Windows / Linux / Android / Apple / 路由器 收敛成**一个「在动」的系统**，而不是一堆互相割裂、各管各的控制面。

这是个人设备编排工具。**只部署在你拥有或经授权管理的设备上。** 详见文末「安全与伦理边界」。

---

## 当前进度

- 初期交付**全部 Windows**，自测也走 Windows 回路（纯 stdlib，便携 Python 3.12 即可跑）。
- 已落地并**全自动自测通过**：
  - **L0 连通 + 认证**：Commander 控制面，节点密码认证后注册、维持心跳。
  - **L1 执行**：本机经密码在另一台机器上执行命令。
  - **L1 常驻**：`install_windows.py` 把节点装成 SYSTEM 级计划任务（开机自起 / 隐藏 / 最高权限 / 断线重连）。
  - **统一扇出**：一条 `broadcast` 指令同时抵达所有节点（统一调用平面，不割裂）。
  - **反向推送**：中心向设备推/拉文件（单份工具箱，不各端自拉）。
- **延后**（初始全 Windows，按决策推后）：L3 共享上下文（TinyContext 跨设备）、「她」人格层（SFT 训练交手机）、Android / Apple / 路由器适配、临时节点注入、Linux systemd 单元。

自测命令（loopback 两台 Windows 节点，不碰真机）：

```bash
python commander.py --self-test --password demo123
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
L0  连通          Tailscale 虚网 / 直连    → 节点密码认证、心跳、在线表
L1  注册/存在感    节点 agent              → 常驻、远程执行、文件推拉
L2  统一调用平面    Commander              → 一条意图扇出多端协同（broadcast）
L3  共享上下文      TinyContext            → 跨设备记忆 / 上下文共享（待落地）
L4  体验/离线队列   「她」人格层            → 主动性、连续存在、情绪态（待落地）
```

---

## 目录结构

```
skynet-fleet/
├── common.py            # 长度前缀 JSON 协议 + exec 白名单
├── commander.py         # L0 控制面 + L1 执行枢纽 + broadcast 扇出 + REPL + 自测
├── node_agent.py        # 被控端：连/认证/注册/exec/push/pull/心跳 + 断线重连
├── install_windows.py   # Windows L1 常驻安装器（零依赖，SYSTEM 计划任务）
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

# 3) 控制台
nodes                      # 看在线节点
exec PC-B ipconfig         # 单点执行
broadcast echo 统一指令    # 一条意图扇出所有节点
```

> 真部署须收紧 exec 白名单并走 Tailscale；当前白名单仅含演示命令。

---

## 路线图（分支线）

```
决策分叉 ── 持久化深度 ── L1 SYSTEM 服务 ✓(已建+自测) ── L2 内核驱动（延后）
           └─ 平台 ── Windows ✓(本期) ── Linux / Android / Apple / 路由（延后）
           └─ 节点性质 ── 常驻 ✓ ── 临时(网吧)（延后）
           └─ 认证 ── 密码 ✓ ── 配对设备一次性令牌（延后）
           └─ 推送 ── 反向推送 ✓ ── 手机 bootstrap / adb（延后）
           └─ 调用 ── 单点 exec ✓ + broadcast 扇出 ✓ ── 统一意图编排（延后）
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

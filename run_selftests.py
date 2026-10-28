"""天网工程 · 一键离线自测。

把仓库里所有「不需要真机、不需要网络」的自测串起来跑，作为每轮改动的门禁：
  python run_selftests.py            # 全跑
  python run_selftests.py ui rule    # 只跑名字含 ui/rule 的

任一失败即退出码 1。用便携 python 亦可：/d/tools/python/python.exe run_selftests.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

# (名字, 命令参数, 工作目录, 超时秒)
CASES = [
    # 120s 不够了：2026-10-23 合并后编队扩到 5 个模拟节点 + 统一意图编排
    # （6 步带依赖 + 逆序回滚），本机实测 3.5 分钟。超时预算小于真实耗时
    # ＝门禁恒红，且红得没有信息量（看起来像代码坏了）。
    ("commander", ["commander.py", "--self-test", "--password", "demo123"], HERE, 420),
    ("node_agent", ["node_agent.py", "--selftest"], HERE, 30),
    ("install_windows", ["install_windows.py", "--selftest", "--password", "demo123"], HERE, 90),
    ("ui_automation", ["ui_automation.py", "--selftest"], os.path.join(HERE, "ui_automation"), 30),
    ("rules_relay", ["rules_relay.py", "--selftest"], os.path.join(HERE, "ui_automation"), 30),
    ("rule_engine", ["rule_engine.py", "--selftest"], os.path.join(HERE, "rfc", "rule-engine"), 30),
    ("policies", ["policies.py", "--selftest"], HERE, 30),
    ("arbiter", ["arbiter.py", "--selftest"], HERE, 30),
    ("discovery", ["discovery.py", "--selftest"], HERE, 120),
    ("sweep", ["sweep.py", "--selftest"], HERE, 120),
    ("accounts", ["accounts.py", "--selftest"], HERE, 60),
    ("compute_runner", ["compute_runner.py", "--selftest"], HERE, 40),
    ("queue_store", ["queue_store.py", "--selftest"], HERE, 30),
    ("persona", ["persona.py", "--selftest"], HERE, 30),
    ("persona_card", ["persona_card.py", "--selftest"], HERE, 30),
    ("recall", ["recall.py", "--selftest"], HERE, 30),
    ("enroll", ["enroll.py", "--selftest"], HERE, 30),
    ("enroll_install", ["install_windows.py", "--enroll-selftest"], HERE, 30),
    ("journal", ["journal.py", "--selftest"], HERE, 30),
    ("followups", ["followups.py", "--selftest"], HERE, 30),
    ("android_device", ["android_device.py", "--selftest"], HERE, 30),
    ("infrared", ["infrared.py", "--selftest"], HERE, 30),
    ("radio", ["radio.py", "--selftest"], HERE, 30),
    ("thermal", ["thermal.py", "--selftest"], HERE, 30),
    # 联网组件：31 项纯离线断言（要不要联网的判定、垃圾结果过滤、HTML 清洗、
    # 资料拼接）。真联网的活不进门禁 —— 门禁不许依赖外网。
    ("webtool", ["webtool.py", "--selftest"], HERE, 30),
    ("agent_phone", ["agent_phone.py", "--selftest"], HERE, 60),
    ("survey", ["survey.py", "--selftest"], HERE, 30),
    ("ble", ["ble.py", "--selftest"], HERE, 30),
    ("nearby", ["nearby.py", "--selftest"], HERE, 30),
    ("perceive", ["perceive.py", "--selftest"], HERE, 30),
    ("phone_state", ["phone_state.py", "--selftest"], HERE, 30),
    ("pc_state", ["pc_state.py", "--selftest"], HERE, 30),
    ("pc_usage", ["pc_usage.py", "--selftest"], HERE, 30),
    ("netpath", ["netpath.py", "--selftest"], HERE, 30),
    ("registry", ["registry.py", "--selftest"], HERE, 30),
    ("capabilities", ["capabilities.py", "--selftest"], HERE, 30),
    # ---- D0 契约层（结果信封 / 设备基类 / 注册表）----
    ("fleet_result", ["-m", "fleet.core.result"], HERE, 30),
    ("fleet_registry", ["-m", "fleet.core.registry"], HERE, 30),
    # ---- 逆向拆解层（L7）：全部离线，不碰真网关不碰真机 ----
    # re_llm 起一次性本地假端点；reverse 用红外编码器造往返向量。
    # 门禁纪律：不许依赖外网/真机，所以这两个模块的 LLM 活体测试不进门禁。
    ("fleet_re_llm", ["-m", "fleet.re_llm", "--selftest"], HERE, 60),
    ("fleet_reverse", ["-m", "fleet.reverse", "--selftest"], HERE, 60),
    # 站点逆向层（自建引擎，2026-10-24 P0）：re_site 的 21 项全是离线断言
    # （真机采集的 GBK 负载/JSON 当向量）。--live 才联网，按门禁纪律不进门禁。
    ("fleet_re_site", ["-m", "fleet.re_site"], HERE, 30),
    # 距离感知引擎（2026-10-24 P0）：48 项全离线（合成 RSSI 序列当向量），
    # 模型单调性/校准回归/热冷判定/三角定位收敛/航向/PDR 都覆盖。
    ("fleet_proximity", ["-m", "fleet.proximity"], HERE, 30),
    # 交互式逐个试码（2026-10-24 P1）：全离线——红外发射打桩（只数脉冲不真发），
    # 会话生命周期/候选排序/派生键/短语解析/晋升码库都覆盖，自测会自清理。
    ("fleet_ir_trial", ["-m", "fleet.ir_trial"], HERE, 30),
    # 热点叠加编队（2026-10-24 P2）：全离线——shell/creative.hotspot 全打桩，
    # ARP 解析（真机格式+IPv6/残缺行）/基线差异（MAC 主键）/deploy 流水线顺序
    # 都覆盖，自测自清理不碰真 HOME。
    ("fleet_hotspot_roam", ["-m", "fleet.hotspot_roam"], HERE, 30),
    # 设备暴露面探测下放（2026-10-24 P3）：全离线——传输层(PCSender/BridgeSender)
    # 全打桩，四态判定/SDP 解析(帧率·分辨率)/ONVIF 组播/时间线落盘/report_only
    # 不发包/编排层参数切分都覆盖，自测自清理不碰真网。
    ("fleet_wallprobe", ["-m", "fleet.wallprobe"], HERE, 30),
    ("fleet_list", ["-m", "fleet", "list"], HERE, 120),
    # ---- Android 静态门禁：XML 标签 vs Java 强转类型 ----
    # 一次真事故一条：activity_device.xml 里 btnDevBack 是 <TextView>，
    # Java 却声明成 Button → 点「基准测试」必崩 ClassCastException。
    # 这种错编译期查不出来（findViewById 按声明类型推导，不校验布局），
    # 只能在真机上点一下才知道，所以必须静态拦。纯离线、秒级。
    ("android_viewcast", ["tools/check_viewcast.py"], HERE, 30),
    # ---- Android 静态门禁 ②（总纲 §四 第 7 条）：运行时权限 vs Manifest 声明 ----
    # 代码用 Manifest.permission.X 但清单没声明 ⇒ 运行时 SecurityException，
    # 编译期查不出、栈里也看不出跟权限有关（详见 tools/check_permissions.py 头部）。
    ("android_permissions", ["tools/check_permissions.py"], HERE, 30),
    # ---- 桌面静态门禁 ③（总纲 §四/P2-14）：Tab 数量徽标不许硬编码 ----
    # 「技能 18」这种手写数量不会报错，只会一直停在旧值。查五件事：
    # 每个 tab 有带 id 的 .tn 徽标 / 标签里没数字 / paintTabCounts 从缓存取数 /
    # 赋值语句无数字字面量 / 启动就拉一次。纯离线、秒级。
    # 另一半靠 live 门禁（clickaudit 渲染后真点、a11y 真算对比度）。
    ("ui_tab_counts", ["tools/check_ui_tabs.py"], HERE, 30),
    # ---- evals：一次真事故一个脚本 ----
    ("eval_d0_bridge_offline", ["evals/d0_bridge_offline.py"], HERE, 90),
    ("eval_t0_input", ["evals/t0_input_not_clobbered.py"], HERE, 90),
    # ---- T1：全屏界面 + 两个显影面共用一套 dispatch ----
    ("fleet_tui_smoke", ["-m", "fleet.tui", "--smoke"], HERE, 240),
    ("eval_t1_two_faces", ["evals/t1_cli_and_tui_share_dispatch.py"], HERE, 240),
    # 这条超时就是失败：它卡住说明界面在长任务期间被拖死了
    ("eval_t2_ui_alive", ["evals/t2_ui_alive_under_job_stream.py"], HERE, 120),
    # ---- T3/T4/T5：论文直播开关 / 免费 AI 后端 / 本机操控设备 ----
    ("eval_t3_live_switch", ["evals/t3_live_switch_papers.py"], HERE, 240),
    ("eval_t4_ai_free", ["evals/t4_ai_free_backends.py"], HERE, 240),
    ("eval_t5_host_control", ["evals/t5_host_control.py"], HERE, 120),
    # ---- T6：WiFi ADB 设备（kind=adb_wifi）----
    # 这条门禁必须完全离线：它连一条 adb 命令都不发、不连一个 IP。
    # 本机没插设备、PATH 里没 adb 都不能让它变红（t4 的教训）。
    ("eval_t6_adb_wifi", ["evals/t6_adb_wifi.py"], HERE, 120),
    # ---- W1 图形版：本地后端 + 前端壳（离线；起真服务打真接口）----
    # 这条起的是真 HTTP 服务（随机端口 + 一次性 token），门禁里打真接口，
    # 所以它能抓到「接口形状变了」「令牌校验漏了」「SSE 退化成一次性」这类坏。
    ("eval_t7_gui_backend", ["evals/t7_gui_backend.py"], HERE, 180),
    # ---- T8：语音输入链路（kind=voice）----
    # 必须完全离线：假录音器 + 假 STT 注入，绝不真开麦、绝不真跑模型。
    # 门禁红 = 代码坏了（识别失败没带原因 / 简体 prompt 没传 / token 预算没减
    # / 两段式闸门松了 / STT 文本被当指令）。
    ("eval_t8_voice", ["evals/t8_voice.py"], HERE, 120),
    # ---- W2：审批弹窗（真审批）----
    # 必须完全离线：假设备 + 假注册表，网络 I/O 一律假掉。
    # 门禁红 = 代码坏了（没确认就执行 / 确认信号能重放 / 超时没拒绝 /
    # 并发互相堵死 / 401、403 分不清 / 审批记录能跨会话重放）。
    ("eval_t9_approval", ["evals/t9_approval.py"], HERE, 180),
    # ---- T10：PC 端操控电脑（kind=pc_control）----
    # 必须完全离线：鼠标键盘注入、进程操作、屏幕抓取一律假掉，
    # **绝不真动用户桌面**。门禁红 = 代码坏了（审批档写错 / caps 返对象 /
    # 失败不带原因 / 高危动作没标风险与可逆性 / 键盘注入闷声注入）。
    ("eval_t10_pc_control", ["evals/t10_pc_control.py"], HERE, 120),
    # 节点生命周期（2026-10-21 真事故的离线复现）：
    # 同名重连覆盖注册表 ⇒ 旧连接 finally 找不到自己 ⇒ **session 清理被整段跳过**
    # ⇒ 临时节点「下机即清」失效（钥匙留在门上）；`_drop_node` 按名 pop 会误杀新连接；
    # 客户端 socket 建连超时(10s) < 心跳间隔(15s) ⇒ **必然**每 ~13 秒周期性重连。
    # 完全离线：不开真端口、不发真密码、不启动真节点。
    ("eval_t11_node_lifecycle", ["evals/t11_node_lifecycle.py"], HERE, 120),
    # W3 设备页（主机可视化）：
    # 截图端点不许变成任意文件读取的洞（登记制 + 路径穿越必须 404）；
    # 设备页只走 registry.call（前端绕不过闸门）；
    # 状态取自 reg.availability()（界面不自己算一套）；取不到必须说原因。
    ("eval_t12_device_panel", ["evals/t12_device_panel.py"], HERE, 120),
    # 角色编队（契约 / 容量闸 / 角色边界）：
    # 五段式契约不许把「空段」和「合法的 None.」混为一谈（混了 = 静默丢证据）；
    # 容量闸必须 fail-fast 不排队（排队 = 父代理等自己占着的名额 = 死等）；
    # 名额必须真正结束后才归还（提前还 = 容量虚增 = 实际并发超限）；
    # 深度是独立于容量的第二个闸；角色卡写错能力名必须当场红。
    # 全离线：不连网络、不调真模型，角色执行用假 exec_fn。
    ("eval_t13_agent_crew", ["evals/t13_agent_crew.py"], HERE, 120),
    # T14：MCP 出口 + 技能装载 + 渲染能力
    # 钉四件事：① MCP 按规范应答（协议版本/三方法/错误码/脏行不退出）；
    # ② 动手能力在 MCP 路径上照样过闸门（默认不导出；include_unsafe 也没 gate
    #   ⇒ 注册表 fail-closed 拒绝）；③ 不许静默失败（「压根没这个工具」与
    #   「存在但没导出」不许混成一句）；④ 技能 frontmatter 坏了要带行号。
    # 渲染层：引擎在不在都如实；引擎在而单帧渲染炸了 = 代码坏了，必须红。
    ("eval_t14_mcp_and_skills", ["evals/t14_mcp_and_skills.py"], HERE, 300),
    # T23：HTML 渲染后端（Video as Code）
    # 钉四件事：① 真能出一条能播的 MP4（分辨率/帧数/时长/帧率都对）；
    # ② **确定性**：同一份 HTML 渲两次要逐字节一致（不然没法进 git、没法 diff）；
    # ③ 确定性 lint：Date.now()/Math.random()/渲染时发网络请求 ⇒ 拒绝渲染并说清原因；
    # ④ 写 MP4 是副作用 ⇒ 没闸门时 fail-closed 拒绝。
    # 环境齐不齐都如实：缺 node/Playwright/Chromium 就 skip 并说明缺什么。
    ("eval_t23_html_render", ["evals/t23_html_render.py"], HERE, 600),
    # T15：角色卡商店（浏览 / 下载 / 体检 / 入库 / 切换）
    # 钉六件事：① 陌生 spec 抛明确原因（不许降级读空）；
    # ② 体检判据是「拼得出人设」不是「字段在不在」（personality 空不误伤好卡）；
    # ③ 下载必须完整（本机会中途截断 ⇒ 半张 PNG 不许入库、不许留半成品）；
    # ④ 下载有 30 分钟冷却（重复打要如实说冷却多久）；
    # ⑤ 内部记账文件不许被当成角色卡；
    # ⑥ 下载/入库是副作用 ⇒ 没闸门时 fail-closed 拒绝。
    # 全离线：下载层打桩，不连真网络。
    ("eval_t15_card_store", ["evals/t15_card_store.py"], HERE, 180),
    # T16：netforce skynet 采集内核接进 web.* 能力族
    # 钉六件事：① 适配层字段名不许猜（probe 的真实字段是 alive/score/verdict/
    # text_chars，不是 ok/text_len/density/why）；
    # ② structure() 的键类型也不许猜（sitemap_files 是 int 不是 list，
    #    当成 list 会 TypeError 直接炸能力）；
    # ③ 内核不在 ⇒ 不可用且说清怎么装（不许静默降级成空回）；
    # ④ web.download 是副作用 ⇒ 没闸门时 fail-closed 拒绝；
    # ⑤ check 判「skynet 在不在」而不是「netforce 在不在」；
    # ⑥ 不许 vendoring（只 import，不复制源码）。
    # 全离线：门禁自造假 skynet 包，不连真网络。
    ("eval_t16_skynet_bridge", ["evals/t16_skynet_bridge.py"], HERE, 120),
    # T17：代码生成音频（procedural audio）
    # 钉六件事：① 每个预设都要真的合成出非空 WAV（能下到声音≠能出到声音）；
    # ② 同 seed ⇒ 逐字节相同，且**跨进程**也一样（str hash 每进程加盐，
    #    用 hash() 会让声音每次都变 ⇒ 不可复现）；
    # ③ 不同 seed ⇒ 不同声音（每台设备声音不同，重启后一样）；
    # ④ 陌生预设必须抛明确错误并说清「只认哪几种」（不许静默退回默认）；
    # ⑤ 零第三方依赖（本仓是后端进程，不引入 Tone.js/SDL/numpy）；
    # ⑥ audio.synth 写文件 ⇒ 副作用 ⇒ 没闸门时 fail-closed。
    # 全离线：合成全在内存与临时目录里完成。
    ("eval_t17_audio_synth", ["evals/t17_audio_synth.py"], HERE, 180),
    # T26：证据分级 authority——「读到的」和「猜的」必须是数据结构。
    # 钉三件事：① 同一结论同一证据 id（sha256 前 16 位，authority 参与哈希）；
    # ② 每条 web.* 结果带 authority 且取值合法（非法值 mk_evidence 抛 ValueError）；
    # ③ discovery 端口投票必须 analyst-inference、设备自报压过端口投票
    #    （554 的 Windows 被判摄像头、5555 被判手机两个真实误判的根）。
    # 全离线：全部假数据驱动判定助手。
    # 🔴 编号 t25 已被另一台机器推的 t25_tool_catalog 占 ⇒ 本门禁用 t26
    ("eval_t26_authority", ["evals/t26_authority.py"], HERE, 90),
    # ---- P4：vendored 技能完整性（第4层技能自进化地基）----
    # 仓库 skills/ 下每个 vendored 技能必须带 _audit.json 且结论 clean，
    # 且 MANIFEST.json 登记与实际目录一一对应。未审查进仓 = 红。
    ("eval_p4_skills_integrity", ["evals/p4_skills_integrity.py"], HERE, 120),
    # N+1 桌面端技能机制：列出/选用/记分/提交/回滚 + 按钮到端点活线。
    # 超时给宽一点：里面要起一次真 FleetServer + 一次 Playwright。
    ("eval_t18_skill_desktop", ["evals/t18_skill_desktop.py"], HERE, 300),
    # N+1b 第 1 步 配对信箱内核：广播字段最小化（AST 静态扫）/验证码
    # 一次性+过期+试爆锁/fail-closed/会话令牌 revoke 即死/审计闭环且
    # 令牌不进 journal/fleetpair:// 链接往返/组播 TTL=1 LAN-only/tap 滤坏包。
    # 全离线：socket 全假、journal 用临时 sqlite。
    ("eval_t19_pair_mailbox", ["evals/t19_pair_mailbox.py"], HERE, 180),
    # N+1c 手机出图：manifest/java 契约静态扫（RUN_COMMAND 常量/权限/不写死模型）
    # + worker 行为（假 sd+假 sysfs：prompt/steps/size/插电/温度/并发锁/Vulkan
    # 诚实边界/二进制缺失）。全离线、可测。
    ("eval_t20_phone_image", ["evals/t20_phone_image.py"], HERE, 120),
    # T24：工具面 v2 —— Sky MCP over HTTP + 权限三档 + 多轮缰绳
    # 钉四组：① 同一请求走 stdio 与 HTTP 两条传输，响应必须一致（协议只有一份，
    # 以后改一边忘另一边 = 手机连上了但工具永远空，极难定位）；
    # ② 权限是**工具名 + 档位**两层白名单，未知工具即便被环境变量配成 auto 也禁
    # （配置来源比代码更容易被动手脚，不能靠它开口子）；
    # ③ 多轮缰绳在代码里不在模型嘴里：轮次上限 2 / 预算耗尽即停 / 同工具不重复 /
    # 首轮失败即止（BFCL：小模型多轮必崩，让它自己决定什么时候停 = 让它胡说）；
    # ④ 资料块带引用纪律且结果硬限 1200 字符（31 万字撑爆上下文的事故不再可能）。
    # 全离线：registry 与决策模型都打桩。
    ("eval_t24_agent_tools_v2", ["evals/t24_agent_tools_v2.py"], HERE, 120),
    ("eval_t25_tool_catalog", ["evals/t25_tool_catalog.py"], HERE, 120),
    # ---- 2026-10-25 门禁接全（总纲 §四 第 1 条）----
    # 这 16 个 eval 一直躺在 evals/ 里但没进门禁 ⇒「写了不跑 = 没写」。
    # 接进来当场就抓到两个真 bug：metrics 直方图 Prometheus 格式非法
    # （{backend="x"},le="5" —— le 拼在花括号外，抓取端整条丢），
    # 以及 video.gen 的「未配置」断言在装了 ffmpeg 的机器上必红（测试假设过时）。
    # 全部离线：不连网、不碰真机。
    ("eval_t27_decider_config", ["evals/t27_decider_config.py"], HERE, 90),
    ("eval_t28_link_selfheal", ["evals/t28_link_selfheal.py"], HERE, 90),
    ("eval_t29_approval_loop", ["evals/t29_approval_loop.py"], HERE, 120),
    ("eval_t30_heartbeat", ["evals/t30_heartbeat.py"], HERE, 90),
    # T31：桌面两套主题的无障碍 + 对比度（P2-13，WCAG AA）
    # 两层判据：① axe 真实引擎，serious/critical 必须为 0；
    # ② 自算对比度，**含图标字形**（axe 对单字符直接判 incomplete，是盲区）
    #    与渐变背景的每个色标取最差；文字 ≥4.5、图标 ≥3.0。
    # 缺 node/playwright 时 skip 并说明，不假装绿。
    ("eval_t31_a11y_contrast", ["evals/t31_a11y_contrast.py"], HERE, 420),
    # T32：桌面界面死链（P2-12，X0）。真浏览器点一遍，判据是
    # 「页面指纹没变 且 没发出任何请求」才算死链；遮罩/disabled/当前选中记跳过。
    # 同时卡覆盖率 ≥90%——覆盖率太低时「死链 0」不可信。
    ("eval_t32_deadlink", ["evals/t32_deadlink.py"], HERE, 920),
    # T33：后台循环心跳（P0-2）。静默卡住是最坏的失败形态：进程还在、端口还开、
    # 日志不刷，用户只觉得「点了没反应」。这条钉子三件事：
    # ① 账本语义（从没 beat = stale / 超时 = stale / 失败原因必留 / 空原因显式标出）；
    # ② 四个常驻循环真接了心跳（名字与清单一致，防「改了名清单没跟着动」）；
    # ③ 端到端：/api/health 里 loops 必须存在，且人为塞的卡住循环被标 stale。
    ("eval_t33_loops_beat", ["evals/t33_loops_beat.py"], HERE, 120),
    # T34：D1 控制面对外协议（P1-9 真代发）。真端口真认证真转发：
    # 假节点连上控制面注册，客户端走 AUTH+CLIENT 发 nodes/exec/push/pull 拿真回执；
    # 同时钉三类失败可分辨（密码错/未知 op/节点离线）与「能力申报必须诚实」
    # （缺 FLEET_PASS 就整条不申报，不长假按钮）。全离线，只在本机随机端口跑。
    ("eval_t34_d1_control", ["evals/t34_d1_control.py"], HERE, 180),
    # T35：两条通道真伪（ssh / mqtt，总纲「C 档 5 条通道」收口）。不 mock 协议：
    # 门禁里现起一个真 MQTT broker（独立实现，不复用被测模块的编解码），
    # 真投递 pub→sub 且 payload 原样到；QoS1 等不到 PUBACK 必须报 timeout；
    # 密码错 ⇒ denied 且说清「有服务不让你进」。SSH 侧：假 sshd 吐真版本行、
    # 普通 TCP 服务必须判 is_ssh=False（不冒充）、给密码 ⇒ denied（argv 里的密码等于公开）。
    ("eval_t35_channels", ["evals/t35_channels.py"], HERE, 180),
    # 逆向深化第一层：未知二进制协议拆解。用**已知真值的假协议**验，
    # 不拿真设备碰运气；96 帧纯随机必须 0 校验命中（假阳性钉子）；
    # 增量校验状态机必须与独立写的暴力版逐位等价（性能优化不许改结果）。
    ("eval_t36_proto_reverse", ["evals/t36_proto_reverse.py"], HERE, 180),
    # 逆向深化第二层：位打包协议（字段不落在字节边界上）。字节层对这类数据
    # 必然看不出结构，位层是唯一路径；三种规模的随机数据必须 found=False
    # （递归切分天然能切出碎片，把碎片当结构就是假绿）。
    ("eval_t37_bit_reverse", ["evals/t37_bit_reverse.py"], HERE, 180),
    # ---- T38：Provider 注册表（融合计划书 P0）----
    # 这一层是「两个手机端 + 三个网络层」的底座：三家免费网关（freellmapi /
    # zen-gate / dsh）+ 本地 llama.cpp 抽象成同一种资源的多个实例。
    # 门禁必须全离线——任何一家没起、没配 Key 都不能让它变红，
    # 它验的是「注册表与探测逻辑」，不是「网关今天活着」。
    ("eval_t38_providers", ["-m", "fleet.providers", "--selftest"], HERE, 120),
    # ---- T39：能力表 → 云端 function-calling 工具表（融合计划书 P4 的云侧那一步）
    # 149 项能力已经有了 MCP 形状（t14 覆盖），这一层补的是 OpenAI / Anthropic
    # 两种信封 + 回环解析（模型吐回来的 tool_call 要还原成能力真名）。
    # 全离线：不连任何网关，验的是「投影与解析」，不是「模型今天调得对」。
    ("eval_t39_toolschema", ["-m", "fleet.toolschema", "--selftest"], HERE, 120),
    # ---- T40：云端 agent loop（P4 的「跑」那一半）
    # toolschema 只管信封，这一层才是「真执行并把结果喂回模型」。
    # 全离线：chat 用假的、执行层用假的，验的是串得对不对 + 三条缰绳 +
    # **工具表即权限边界**（模型叫了没给它的 pc.shell ⇒ 不许执行）。
    ("eval_t40_agentloop", ["-m", "fleet.agentloop", "--selftest"], HERE, 120),
    ("eval_t41_doctor", ["-m", "fleet.doctor", "--selftest"], HERE, 60),
    # 人格注入：云侧 system 必须带人格且排在执行纪律之前。
    # 这条断过一次（新链绕开老链的人格锚，同一台机器换链路换了个人），
    # 断的时候代码不报错、只有真跑看得出，所以要用门禁钉死。
    ("eval_t42_persona_wired", ["evals/t42_persona_wired.py"], HERE, 60),
    # 视觉（2026-10-28）：仓库里四个取图能力全止步于「存成 PNG」，
    # 模型这一侧一个字都读不到（screen.read 只回文字树，不是画面）。
    # 这条钉的是「图真的进了 messages + 四种失败可分辨 + 画面外发必过闸门」。
    # 全离线：chat 用假的，验接线不验模型质量。
    ("eval_t43_vision_look", ["evals/t43_vision_look.py"], HERE, 90),
    # 人格真的递到模型手里了吗（2026-10-28）。同「没生效」那件事栽过两次：
    # 10-26 新链绕开老链的人格锚，system 只剩执行层口径；10-28 active.json
    # 指向「验收者」，问她"你能干什么"得到"逐条复现 EVIDENCE、判 FAIL"。
    # 两次都静默——106 项门禁全绿，因为没一条问过「她收到的是谁」。
    # 分层验：库能列卡 → 指针指到真卡 → system 含卡名 → 服务侧同一份 →
    # 不许是遗留的流水线帽子 → 卡库坏了也不许把服务搞挂。
    ("eval_t44_persona_reaches_model", ["evals/t44_persona_reaches_model.py"], HERE, 60),
    # 代码里不许写死盘符（软绑定）。起因见文件头：三套启动脚本各把 G:\ E:\ 写死。
    ("eval_t45_no_hardcoded_drive", ["evals/t45_no_hardcoded_drive.py"], HERE, 90),
    # 「模型到底看到了什么」。起因见文件头：--chat-template 参数名错（路径被当模板
    # 源码编译）+ 提示词从不说 tool 字段 + 58 工具 6141 tokens 超 25s 超时线，
    # 三重故障静默了十天。判据④是「prompt_tokens 随 system 变长而变长」，
    # 因为「HTTP 200 + 有 tool 字段」在模板完全失效时也可能碰巧成立。
    # 无模型/无 llama-server 时 ④ 会明确 skip，不算通过。给 180s（要起进程）。
    ("eval_t46_decider_sees_tools", ["evals/t46_decider_sees_tools.py"], HERE, 180),
    ("eval_t5_metrics", ["evals/t5_metrics.py"], HERE, 90),
    ("eval_t6_mcp", ["evals/t6_mcp.py"], HERE, 90),
    ("eval_t7_memory", ["evals/t7_memory.py"], HERE, 90),
    ("eval_t8_relay", ["evals/t8_relay.py"], HERE, 90),
    ("eval_phone_pair", ["evals/test_device_pair.py"], HERE, 90),
    ("eval_fleet_overview", ["evals/test_fleet_overview.py"], HERE, 90),
    ("eval_jobs_artifacts", ["evals/test_jobs_artifacts.py"], HERE, 120),
    ("eval_memory_pack", ["evals/test_memory_pack.py"], HERE, 90),
    ("eval_memory_sync", ["evals/test_memory_sync.py"], HERE, 90),
    ("eval_model_catalog", ["evals/test_model_catalog.py"], HERE, 60),
    ("eval_phone_node", ["evals/test_phone_node.py"], HERE, 90),
    ("eval_video_gen", ["evals/test_video_gen.py"], HERE, 90),
]


def _dec(b):
    if isinstance(b, str):
        return b
    for enc in ("utf-8", "cp936", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def run_case(name, args, cwd, timeout):
    cmd = [PY] + args
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)
        out = _dec(r.stdout)
        err = _dec(r.stderr)
        ok = r.returncode == 0
        return ok, out, err, r.returncode
    except subprocess.TimeoutExpired:
        return False, "", f"超时({timeout}s)", -1
    except FileNotFoundError as e:
        return False, "", f"找不到: {e}", -1


#: 门禁跑完后必须还原的本机状态文件。
#:
#: 🔴 2026-10-28 的教训：**有门禁会改产品状态**。eval_t32_deadlink 点界面死链，
#: 而卡库每张卡都带「切换到这张」按钮——点一次就是一次真实切卡。16 张点完，
#: 指针停在列表最后一个「验收者」上。于是每跑一次门禁，天网的人格就从
#: 「大肥鱼」变成验收员（问她"你能干什么"，答"逐条复现 EVIDENCE、判 FAIL"），
#: 而 106 项门禁全绿——因为没一条门禁问过「她收到的是谁」。
#: 10-26 修过一次同样的漂移，当时以为是人手切的，又漂回来：根源就在这儿。
#:
#: t32 自己已经加了 finally 还原（超时被 kill 时它兜不住）。这里是**最后一道闸**：
#: 不管哪条门禁干的、也不管它有没有正常退出，跑完都对一遍、改回来、点出名字。
_STATE_GUARD = [os.path.join(HERE, "cards", "active.json")]


def _snap_states():
    out = {}
    for p in _STATE_GUARD:
        try:
            with open(p, "rb") as f:
                out[p] = f.read()
        except OSError:
            out[p] = None
    return out


def _restore_states(snap):
    """把被门禁改掉的本机状态还回去。返回 [(路径, 之前, 之后)]。"""
    bad = []
    for p, before in snap.items():
        try:
            with open(p, "rb") as f:
                now = f.read()
        except OSError:
            now = None
        if now == before:
            continue
        bad.append((p, before, now))
        if before is not None:
            try:
                with open(p, "wb") as f:
                    f.write(before)
            except OSError:
                pass
    if bad:
        print("\n⚠️  有门禁改动了本机状态，已还原（这些门禁该改成只读）：")
        for p, b, a in bad:
            print("   %s" % p)
            print("     跑之前: %s" % (b or b"").decode("utf-8", "replace").replace("\n", " ")[:90])
            print("     跑之后: %s" % (a or b"").decode("utf-8", "replace").replace("\n", " ")[:90])
    return bad


def main():
    only = [a.lower() for a in sys.argv[1:]]
    cases = [c for c in CASES if not only or any(o in c[0].lower() for o in only)]
    print(f"=== 天网工程离线自测：{len(cases)} 项 ===", flush=True)
    results = []
    snap = _snap_states()
    try:
        for name, args, cwd, timeout in cases:
            print(f"\n----- {name} -----", flush=True)
            ok, out, err, rc = run_case(name, args, cwd, timeout)
            tail = "\n".join((out or "").strip().splitlines()[-8:])
            if tail:
                print(tail)
            if err.strip():
                print("[stderr]", "\n".join(err.strip().splitlines()[-4:]))
            print(f"[{'PASS' if ok else 'FAIL'}] {name} (rc={rc})", flush=True)
            results.append((name, ok))
    finally:
        _restore_states(snap)

    passed = sum(1 for _, ok in results if ok)
    print(f"\n=== 结果：{passed}/{len(results)} 通过 ===")
    if passed != len(results):
        print("失败项:", ", ".join(n for n, ok in results if not ok))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

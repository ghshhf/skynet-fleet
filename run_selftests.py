"""天网工程 · 一键离线自测。

把仓库里所有「不需要真机、不需要网络」的自测串起来跑，作为每轮改动的门禁。

  python run_selftests.py --changed         # 【日常用这个】只跑被改动文件波及的项
  python run_selftests.py --changed HEAD~1  # 连同与某基线的差异一起算
  python run_selftests.py video storyboard  # 按名字子串挑（老用法，仍在）
  python run_selftests.py --all             # 全量（117 项，十几分钟，发布前跑）
  python run_selftests.py --slowest         # 看历史耗时排行，谁在拖后腿

`--changed` 的映射由 `tools/gate_map.py --build` 从代码里推出（入口文件 + import
闭包），改了哪几个文件就只跑覆盖它们的项 —— 改 `fleet/motion.py` 只需 10 项几十秒，
不必重跑手机配对和桌面壳。碰上公共模块（`capabilities.py` / `common.py` 这类被
几十项依赖的）会明说「影响面是全仓级的」，那时才值得补一次 `--all`。

任一失败即退出码 1。用便携 python 亦可：/d/tools/python/python.exe run_selftests.py
"""
import os
import subprocess
import sys
import time

# 🔴 本机是 GBK 控制台：不只是子进程会死（run_case 里已注入 PYTHONIOENCODING），
# 门禁**自己**打印子进程输出 tail 时同样会死（tail 里有 `✓`/`⇒` 就整个门禁崩）。
# 2026-11-01 实测：三项被 GBK 打倒后，点名重跑时死在门禁自己的 print(tail) 上。
# 统一 UTF-8 + replace：宁可字符糊掉，门禁不许因编码退出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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
    # 手机出图后端（local-dream / QNN NPU）：两个本地假 HTTP 服务冒充手机的
    # 8808/8081，把 /info -> /status -> /select -> /generate(SSE) 真跑一遍。
    # 断言的是一条「协议拼错就必然失败」的链路，不依赖真机、不联网。
    ("image_gen_runner", ["image_gen_runner.py", "--selftest"], HERE, 90),
    # 引擎直驱：patch 命名规则 + 启动脚本拼装 + endpoint 解析。
    # 这几项是「能不能脱离 App 自己拉起 NPU 引擎」的全部要点：patch 挑错会出纯噪声、
    # LD_LIBRARY_PATH/ADSP_LIBRARY_PATH 少一段就是 QnnDevice_create 0x36b1。
    ("engine", ["-m", "fleet.engine"], HERE, 60),
    # 时间线引擎：相机模型 + affine warp + 关键帧合成播放帧。
    # 纯 CPU 纯数学，断言的是「方向对不对」（放大 2 倍=取中心一半、平移 1/4 画宽、
    # zoom-out 不出黑边），这类错看不出来但会毁掉整条片。
    ("motion", ["-m", "fleet.motion"], HERE, 60),
    # 手机 NPU 出视频：假手机（两个本地 HTTP 服务）+ 假后端每帧变暗，
    # 锁死四件事——关键帧数=duration*gen_fps+1、播放帧不花算力（调用次数对不上就挂）、
    # 运镜真的 warp 进了 img2img（none 差 0 / push_in 差 >0）、稳定器把亮度拉回首帧。
    ("video_gen_runner", ["video_gen_runner.py", "--selftest"], HERE, 180),
    # 漫剧层：画布适配/挑尺寸/折行/字幕/编码/淡入淡出/叠化/board 校验/音频拼接。
    # 不碰手机不联网（TTS 那部分只在真跑时用）。
    ("storyboard", ["-m", "fleet.storyboard"], HERE, 120),
    # 出图 → 成品海报：四种版式尺寸、超长标题不溢出、缺图/未知版式如实报错。
    ("poster_runner", ["poster_runner.py", "--selftest"], HERE, 60),
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
    # t47：两级决策（规则定域 → 域内选工具）。t46 保证「模型看得见选项」，
    # 这条保证「选项被收窄到能算得动」。最狠的一条是域覆盖：加了工具忘记归域，
    # 那个工具永远不会出现在候选里，不报错、不掉分，只是「说了它不动」。
    ("eval_t47_two_stage_decide", ["evals/t47_two_stage_decide.py"], HERE, 120),
    # t48：算力层（接外部算力）。t46 保证「模型看得见选项」，t47 保证
    # 「选项收窄到算得动」，这条保证「用的算力是对的」——58 个工具本地 2B 要
    # 52.9s，接上外部算力只要 2s，差的不是模型智力而是算力选错了。
    # 重点在②：probe 说「通了」必须是真算过。三种假端点分别复现
    # 「200+报错文本」「tokens 恒等」「真读到输入」，判据不看格式看副作用。
    ("eval_t48_compute_layer", ["evals/t48_compute_layer.py"], HERE, 180),
    # t49：桌面端成品化（单实例 / 托盘常驻 / 开机自启 / 窄窗口）。
    # 起因见文件头：这程序是常驻服务，不是一次性窗口；但双击两次会起两份服务、
    # 关掉窗口等于把她关了、开机不自启、最小宽度 1080 > 断点 900（断点从没生效）。
    # [A] 段盯的是最容易悄悄退化的那处：pystray 不进 requirements →
    # 包里 import 失败 → 托盘静默失效，代码全对能力没了（与 edge_tts 同形）。
    ("eval_t49_desktop_shell", ["evals/t49_desktop_shell.py"], HERE, 120),
    # t51/t52：「有连接即入网」这一层的两半（2026-11-01）。
    # t51 是"能不能传"：不 mock 协议、真发真文件比对 sha256，并钉住四道写盘闸
    #     （路径逃逸 / 字节数不符 / 数量上限 / 窗口寿命）。这里最容易的失败不是
    #     传不动，是**传歪了还报成功**。
    # t52 是"连上了能干什么"：可达 ≠ 可执行。可达层的设备不注册成 Device，
    #     所以执行类能力结构上够不着它们。这条破了就是局域网里谁都能下令。
    ("eval_t51_lan_transfer", ["evals/t51_lan_transfer.py"], HERE, 180),
    ("eval_t52_reach_layer", ["evals/t52_reach_layer.py"], HERE, 120),
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


#: 慢门禁：需要真实浏览器 / 真实模型 / 真实设备，单条十几分钟。
#:
#: 2026-10-29 实测：全量 24 分 45 秒，**其中 t32 一条占约 15 分钟**
#: （Playwright 逐个点界面元素，点一次等 2 秒判"有没有反应"，超时给了 920s）。
#: 主人的规矩是「全部干完再跑一次」，那这一次就不该被一条慢活拖成半小时。
#: 所以拆两档：
#:   默认      → 快档（跳过这里列出的），2~4 分钟，日常改完就跑
#:   --slow    → 快档 + 慢门禁，发版前跑
#:   --all     → 同上（alias，语义更直白）
#:   显式点名   → 无论快慢都跑（`python run_selftests.py t32`）
SLOW = {"eval_t32_deadlink"}


def run_case(name, args, cwd, timeout):
    cmd = [PY] + args
    # 🔴 中文 Windows 控制台是 GBK，子进程 print 一个 `⇒`/`✓`/`R²` 就直接
    # UnicodeEncodeError 退出（2026-11-01 实测打倒 commander / poster_runner /
    # proximity 三项——它们在 CI 的 UTF-8 环境里是绿的，一到本机就红）。
    # 门禁跑在谁的机器上都不该被控制台代码页决定生死：统一强制子进程 UTF-8。
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env)
    except FileNotFoundError as e:
        return False, "", f"找不到: {e}", -1
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode == 0, _dec(out), _dec(err), p.returncode
    except subprocess.TimeoutExpired:
        # 🔴 超时必须连**整棵进程树**一起杀。2026-10-29 实测：
        # t32 起的是 node + chromium，只 kill 直接子进程会让孙子们继续跑到天荒地老
        # ——既占 CPU，又可能继续改产品状态（t32 点界面会真的切卡）。
        # 原实现 subprocess.run(timeout=) 就是只杀父，于是「超时」形同虚设。
        killed = 0
        try:
            import psutil
            parent = psutil.Process(p.pid)
            kids = parent.children(recursive=True)
            for k in kids:
                try:
                    k.kill()
                    killed += 1
                except Exception:
                    pass
            try:
                parent.kill()
                killed += 1
            except Exception:
                pass
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        try:
            out, err = p.communicate(timeout=20)
        except Exception:
            out, err = b"", b""
        return False, _dec(out), f"超时({timeout}s)，已杀 {killed} 个进程", -1


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


def _git_changed(base=None):
    """当前工作区改动的 .py/.json 文件（含未跟踪），可选加上与 base 的差异。"""
    files = set()
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=HERE,
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        for line in out.splitlines():
            if len(line) < 4:
                continue
            p = line[3:].strip()
            if " -> " in p:
                p = p.split(" -> ", 1)[1]
            files.add(p.replace("\\", "/"))
        if base:
            out = subprocess.run(["git", "diff", "--name-only", base], cwd=HERE,
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace").stdout
            files |= {x.replace("\\", "/") for x in out.splitlines() if x.strip()}
    except OSError:
        return None
    return {f for f in files if f.endswith((".py", ".json", ".yml", ".yaml"))}


def _select_by_changed(base=None):
    """按改动文件反查该跑哪些项。返回 (要跑的项, 说明行)。

    映射由 tools/gate_map.py 从代码里推出来（入口文件 + import 闭包），
    不是手贴标签，所以新增自测项自动生效、不会漂。
    """
    path = os.path.join(HERE, "tools", ".gate_map.json")
    # 映射会过期的：新写的文件、新加的 import、挪走的模块，都能让它悄悄失准，
    # 而「按改动跑」失准的后果是漏测（比慢严重得多）。生成只要 4 秒，
    # 所以每次 --changed 都先重建，不赌缓存。
    if "--stale-map" not in sys.argv:
        gm = os.path.join(HERE, "tools", "gate_map.py")
        if os.path.isfile(gm):
            r = subprocess.run([PY, gm, "--build"], cwd=HERE,
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=180)
            if r.returncode != 0:
                tail = (r.stderr or "").strip().splitlines()
                print("（重建影响面映射失败，用上次的；原因：%s）"
                      % (tail[-1] if tail else "未知"))
    if not os.path.isfile(path):
        print("还没有影响面映射，先跑：python tools/gate_map.py --build")
        return None, ""
    import json as _j
    data = _j.load(open(path, encoding="utf-8"))
    files = _git_changed(base)
    if files is None:
        print("取不到 git 改动（不是仓库？），退回全量")
        return CASES, ""
    if not files:
        print("工作区没有代码改动（只剩纯文档/未改）—— 不需要跑门禁。")
        print("要强制全量：python run_selftests.py --all")
        return [], ""

    # 门禁/映射自身变了：任何映射都不足以判断，直接全量。
    meta = {f for f in files
            if os.path.basename(f) in ("run_selftests.py", "gate_map.py")}
    if meta:
        print("改的是门禁基础设施（%s）—— 跑全量。" % ", ".join(sorted(meta)))
        return CASES, ""

    rev = data.get("reverse") or {}
    hit = {}
    orphan = []
    for f in sorted(files):
        pairs = rev.get(f) or []
        if not pairs:
            orphan.append(f)
        for n, d in pairs:
            prev = hit.get(n)
            if prev is None or d < prev[0]:
                hit[n] = (d, f)
    if not hit:
        print("改动的文件没有任何自测项覆盖：")
        for f in orphan:
            print("  " + f)
        print("这不是「不用测」，而是「测不到」—— 要么补一项，"
              "要么这次改动手动挑几项跑。")
        return [], ""
    deep = "--deep" in sys.argv
    keep = [n for n, (d, _) in hit.items() if d <= 1 or deep]
    far = len(hit) - len(keep)
    cases = [c for c in CASES if c[0] in keep]
    print("改动 %d 文件 -> %d 项%s" % (
        len(files), len(cases),
        "（含 %d 项间接依赖，因为加了 --deep）" % far if deep and far else ""))
    if far and not deep:
        print("另有 %d 项是三层以上的传递依赖（改这里动不到它们的断言），"
              "要一起跑加 --deep" % far)
    # 只报「直接依赖」够多才算公共模块；深层传递被拉进来的不算
    heavy = [f for f in files
             if sum(1 for x in (rev.get(f) or []) if x[1] <= 1) >= 20]
    if heavy:
        print("\n⚠ 碰到公共模块（影响面本来就大，不是映射出错）：")
        for f in heavy:
            print("  %-38s %d 项直接依赖" % (
                f, sum(1 for x in rev[f] if x[1] <= 1)))
        print("  这些项跑完如果不放心，再补一次全量：python run_selftests.py --all")
    return cases, ""


def _record_times(spent):
    """把每项耗时攒下来，下次就知道谁在拖后腿。"""
    import json as _j
    path = os.path.join(HERE, "tools", ".gate_times.json")
    hist = {}
    if os.path.isfile(path):
        try:
            hist = _j.load(open(path, encoding="utf-8"))
        except ValueError:
            hist = {}
    for k, v in spent.items():
        # 留最近一次，不取平均：代码变快变慢都要能立刻看见
        hist[k] = round(float(v), 1)
    try:
        _j.dump(hist, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except OSError:
        pass


def show_slowest(n=15):
    import json as _j
    path = os.path.join(HERE, "tools", ".gate_times.json")
    if not os.path.isfile(path):
        print("还没有耗时记录，跑一次门禁（任意范围）就会开始记。")
        return 0
    hist = _j.load(open(path, encoding="utf-8"))
    rows = sorted(hist.items(), key=lambda kv: -kv[1])[:n]
    total = sum(hist.values())
    print("已记录 %d 项，累计 %.1f 秒。最慢 %d 项：" % (len(hist), total, len(rows)))
    for k, v in rows:
        print("  %7.1fs  %-34s %4.1f%%" % (v, k, 100.0 * v / max(total, 1e-9)))
    return 0


def main():
    raw = sys.argv[1:]
    flags = {a.lower() for a in raw if a.startswith("--")}
    only = [a.lower() for a in raw if not a.startswith("--")]
    include_slow = bool(flags & {"--slow", "--all"})
    if "--slowest" in flags:
        return show_slowest()

    # --changed [基线]：只跑被改动文件影响到的项。这是日常该用的入口 ——
    # 117 项全量十几分钟，改一行 fleet/motion.py 没必要重跑手机配对。
    cases = None
    if "--changed" in flags:
        base = None
        for i, a in enumerate(raw):
            if a == "--changed":
                nxt = raw[i + 1] if i + 1 < len(raw) else ""
                if nxt and not nxt.startswith("-"):
                    base = nxt
                break
        cases, _ = _select_by_changed(base)
        if cases is not None and not cases:
            return 0

    if cases is None:
        cases = [c for c in CASES if not only or any(o in c[0].lower() for o in only)]
        if not include_slow and not only:
            skipped = [c[0] for c in cases if c[0] in SLOW]
            cases = [c for c in cases if c[0] not in SLOW]
            if skipped:
                print("（跳过 %d 条慢门禁：%s —— 要跑加 --slow）"
                      % (len(skipped), ", ".join(skipped)), flush=True)
    print(f"=== 天网工程离线自测：{len(cases)} 项 ===", flush=True)
    results = []
    spent = {}
    snap = _snap_states()
    try:
        for name, args, cwd, timeout in cases:
            print(f"\n----- {name} -----", flush=True)
            _t0 = time.time()
            ok, out, err, rc = run_case(name, args, cwd, timeout)
            spent[name] = time.time() - _t0
            tail = "\n".join((out or "").strip().splitlines()[-8:])
            if tail:
                print(tail)
            if err.strip():
                print("[stderr]", "\n".join(err.strip().splitlines()[-4:]))
            print(f"[{'PASS' if ok else 'FAIL'}] {name} "
                  f"(rc={rc}, {spent[name]:.1f}s)", flush=True)
            results.append((name, ok))
    finally:
        _restore_states(snap)
        _record_times(spent)

    passed = sum(1 for _, ok in results if ok)
    total_s = sum(spent.values())
    print(f"\n=== 结果：{passed}/{len(results)} 通过，耗时 {total_s:.0f}s ===")
    if passed != len(results):
        slow = sorted(spent.items(), key=lambda kv: -kv[1])[:3]
        print("失败项:", ", ".join(n for n, ok in results if not ok))
        print("这次最慢三项:", ", ".join("%s %.0fs" % kv for kv in slow))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

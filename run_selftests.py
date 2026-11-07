"""天网工程 · 一键离线自测。

把仓库里所有「不需要真机、不需要网络」的自测串起来跑。

**日常入口就是不带参数：`python run_selftests.py`** —— 按改动跑，纯新增只跑结构面
（契约 / 注册表 / 结果信封）+ 改谁测谁。全量交给远程 CI：push 到 main 自动 4 片并行，
红了在 Job Summary 里直接写清哪一项、红在哪几行，不用去翻日志。

  python run_selftests.py                   # 【日常】按改动跑，通常几秒
  python run_selftests.py --domains         # 369 条能力按域摊开，标出哪些域门禁空着
  python run_selftests.py --domain image    # 【按分类跑】只跑 image 域的门禁
  python run_selftests.py --domain drive,geo
  python run_selftests.py --changed HEAD~1  # 连同与某基线的差异一起算
  python run_selftests.py video storyboard  # 按名字子串挑
  python run_selftests.py --all             # 全量（164 项，24 分钟起步，少用）
  python run_selftests.py --shard 1/4       # 全量的第 1/4 片（CI 矩阵并行用）
  python run_selftests.py --slowest         # 看历史耗时排行，谁在拖后腿

**分类 = 域 = 模块**。能力名一律 `域.动作`，域到模块的映射从代码推（设备 `caps()`
说自己认哪些名字，`type(dev).__module__` 说实现在哪个文件），所以改一个域
就 `--domain 那个域`，不用记哪个文件对应哪条测试。

`--changed` 的映射由 `tools/gate_map.py --build` 从代码里推出（入口文件 + import
闭包）。碰上「纯新增」的文件（diff 里一行都没删）不进闭包，只跑结构面 ——
追加代码动不到已有代码路径，把整圈拉起来是白跑。2026-11-06 实测那笔账：
43 项 193 秒，对新写的四个模块覆盖率为 0。

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
    # catalog × 设备契约（2026-11-06）：全仓 362 条能力 1.4 秒校一遍。
    # 存在的意义是**替代**按 import 闭包白跑的那几十项（见 tools/check_catalog.py
    # 顶部说明）：加一条能力定义，真正会坏的只有这四件事，一项就够。
    # 它抓到的第一个真 bug 是 web.quote —— 设备实现了、声明了、唯独没登记。
    ("catalog_contract", ["tools/check_catalog.py"], HERE, 30),
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
    # ---- LLM 网关协议栈（llmgw.*）：全离线，monkeypatch 网关与 HTTP ----
    # 10 组断言覆盖成功路径 / sticky / 429+Retry-After→authoritative 冷却 /
    # 无 Retry-After→heuristic 冷却 / 冷却候选跳过 / embeddings 家族内 /
    # endpoints·models 盘点 / probe 计速 / sticky clear。
    # 真联网的活（打 OFM·zen-gate·匿名端点）不进门禁——门禁不许依赖外网。
    ("fleet_llmgw", ["-m", "fleet.kinds.llmgw_device"], HERE, 30),
    # 网关进程本身（2026-11-01）：**同端口只许一个实例**。
    # 网关挂上 Windows 计划任务后，自愈与手动拉起撞车是常态；两个进程同时听 8790 时
    # Windows 会把连接随机分给其中一个（SO_REUSEADDR 的语义和 POSIX 完全不同），
    # 报的错互相打架。全离线：只绑 127.0.0.1 的临时端口，不碰真网关、不打外网。
    ("fleet_llmgw_srv", ["-m", "fleet.llmgw_server", "--selftest"], HERE, 30),
    # ---- 2026-11-06 新增的四个 kinds：此前**一项都没进门禁** ----
    # 上一轮那 43 项 193 秒里没有一项真的跑过它们（只是 import 了 catalog）。
    # 四个加起来 5.7 秒，45 条断言：这笔账才是对的。
    ("fleet_geo", ["-m", "fleet.kinds.geo", "--selftest"], HERE, 30),
    ("fleet_drive", ["-m", "fleet.kinds.drive", "--selftest"], HERE, 60),
    ("fleet_circle", ["-m", "fleet.kinds.circle", "--selftest"], HERE, 30),
    ("fleet_wintool", ["-m", "fleet.kinds.wintool", "--selftest"], HERE, 60),
    # 图片流水线（2026-11-06 从 2 项扩到 9 项）：造 3 张不同比例的图当向量，
    # 断言尺寸/格式/字节关系，不断言「没抛异常」——「没抛但输出错」才是这类模块的病。
    ("fleet_image_ops", ["-m", "fleet.kinds.image_ops", "--selftest"], HERE, 60),
    # 通用格式转换（2026-11-06）：素材由 ffmpeg 现场合成（1 秒测试片 + 正弦波），
    # 不依赖仓库里任何样例文件；26 条断言覆盖嗅探/图片/音频/跨类抽音轨/视频/GIF/抽帧/批量。
    ("fleet_convert", ["-m", "fleet.kinds.convert", "--selftest"], HERE, 180),
    # 压缩包（2026-11-06）：19 条断言，含两个反向用例 —— 带 ../ 的恶意 zip 与带盘符的
    # 条目都必须被拒且真的没写出文件。安全逻辑不写测试等于没写。
    ("fleet_archive", ["-m", "fleet.kinds.archive", "--selftest"], HERE, 120),
    ("fleet_fileops", ["-m", "fleet.kinds.fileops", "--selftest"], HERE, 120),
    ("fleet_textops", ["-m", "fleet.kinds.textops", "--selftest"], HERE, 120),
    ("fleet_pdfops", ["-m", "fleet.kinds.pdfops", "--selftest"], HERE, 180),
    ("fleet_sheetops", ["-m", "fleet.kinds.sheetops", "--selftest"], HERE, 180),
    ("fleet_wordops", ["-m", "fleet.kinds.wordops", "--selftest"], HERE, 180),
    ("fleet_pptops", ["-m", "fleet.kinds.pptops", "--selftest"], HERE, 180),
    ("fleet_live", ["-m", "fleet.live", "--selftest"], HERE, 180),
    ("fleet_agentops", ["-m", "fleet.kinds.aiops", "--selftest"], HERE, 600),
    ("fleet_chartops", ["-m", "fleet.kinds.chartops", "--selftest"], HERE, 240),
    ("fleet_codeops", ["-m", "fleet.kinds.codeops", "--selftest"], HERE, 120),
    ("fleet_mediaops", ["-m", "fleet.kinds.mediaops", "--selftest"], HERE, 300),
    # 音频细节层（2026-11-06）：素材由标准库 wave 现场合成，真跑 ffmpeg；
    # 75 条断言覆盖规格/电平/静音/波形/标签 + 转码/归一化/切分/混音/改标签，
    # 并验「源文件字节不变」「同名输出另取编号」「失败不留残件」。
    ("fleet_audioops", ["-m", "fleet.kinds.audioops", "--selftest"], HERE, 300),
    # 视频生成（2026-11-06）：把 motion/storyboard 两套算法接成可调能力。
    # 打桩出图后端离线真跑「关键帧 → warp 播放帧 → mp4」，ffprobe 验帧，
    # 并验参数边界、seed 台账、同名不覆盖、后端缺失时 unavailable 且附计划。
    ("fleet_videogen", ["-m", "fleet.kinds.videogen", "--selftest"], HERE, 300),
    # 生图（2026-11-06）：网络与出图后端全打桩，34 条断言覆盖参数边界、
    # 结果信封副作用标记、seed 台账、按魔术字节认格式、同名不覆盖、后端缺失降级。
    ("fleet_ai_image", ["-m", "fleet.kinds.ai", "--selftest"], HERE, 120),
    ("fleet_capindex", ["-m", "fleet.capindex", "--selftest"], HERE, 120),
    ("fleet_netproxy", ["-m", "fleet.netproxy", "--selftest"], HERE, 60),
    ("fleet_flowops", ["-m", "fleet.kinds.flowops", "--selftest"], HERE, 180),
    ("fleet_endpoints", ["-c", "import sys; from fleet.endpoints import _selftest; sys.exit(_selftest())"], HERE, 60),
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
    # ---- T1：终端只留一个面（命令行），dispatch 的两条渲染路径仍要一起验 ----
    # 全屏那套已于 2026-11-07 删除（与命令行本来就是同一套命令）；这条同时盯着
    # 「入口不许回来」和「console.print / emit 两条路不许一边响一边哑」。
    ("eval_t1_single_surface", ["evals/t1_single_terminal_surface.py"], HERE, 120),
    # ---- T80：文档四层（PDF / 表格 / Word / PPT）----
    # 四个域此前有设备认领但门禁为 0。这里只做契约面 + 起子进程跑模块自带自测，
    # 实测 20 秒上下；整轮超过 120 秒由脚本自己判红（门禁变慢当缺陷修）。
    ("eval_t80_office_ops", ["evals/t80_office_ops.py"], HERE, 180),
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
    # 验的是「注册表与探测逻辑」，不是「网关当前是否在线」。
    ("eval_t38_providers", ["-m", "fleet.providers", "--selftest"], HERE, 120),
    # ---- T39：能力表 → 云端 function-calling 工具表（融合计划书 P4 的云侧那一步）
    # 149 项能力已经有了 MCP 形状（t14 覆盖），这一层补的是 OpenAI / Anthropic
    # 两种信封 + 回环解析（模型吐回来的 tool_call 要还原成能力真名）。
    # 全离线：不连任何网关，验的是「投影与解析」，不是「模型当前输出」。
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
    ("eval_t53_nat_traversal", ["evals/t53_nat_traversal.py"], HERE, 240),
    # t54 是「数字不许漂移」：docs/_stats.json 必须等于 tools/gen_stats.py 现算的值。
    #     天网文档里的进度数字漂过四个门禁数并存，根子是数字与真源之间没有断言。
    ("eval_t54_stats_sync", ["evals/t54_stats_sync.py"], HERE, 60),
    ("eval_t55_net_accel", ["evals/t55_net_accel.py"], HERE, 300),
    # t56 是「打不通也不许断」：t53 证明隔着 NAT 能打通，但打通是动作不是通路 ——
    #     真实网络里打不通是常态。t56 钉的是「先中继可用、后台再升级」这套契约，
    #     以及 IPv6 一票否决整套 NAT 分析的优先级。
    ("eval_t56_tunnel", ["evals/t56_tunnel.py"], HERE, 120),
    # t57 是「会变的地址不是问题」：t56 把中继当注入替身，数据面并没接通。
    #     t57 钉对等中继（两端各接进来、中继转发彼此字节）+ 地址簿 + 墙外自检，
    #     覆盖指纹稳定、地址漂移的场景。
    ("eval_t57_relay_rdv", ["evals/t57_relay_rdv.py"], HERE, 180),
    # t58 是「把上面那些手动能力接成一条自己会跑的循环」。
    #     复用天网已有的 beat（心跳骨架此前没有任何循环在用）+ Rendezvous，
    #     钉五档状态机、测不了≠进不来、失败退避、地址簿重启不丢、坏文件不阻断启动。
    ("eval_t58_rdv_auto", ["evals/t58_rdv_auto.py"], HERE, 300),
    # ---- T59：能力归组（同一动作的几套实现 → 一张表）
    # 全离线。验的是「357 项里哪些其实是同一件事」这件事本身：
    # 折叠后每动作只剩一条、动作名可解析、参数别名能双向喂、
    # 兜底只在「说的是动作」时发生（点名实现就不许换手）。
    ("eval_t59_groups", ["-m", "fleet.groups"], HERE, 120),
    # ---- T60：解析层（下载层前置：分享链接→直链+元数据）----
    # 全离线：短链抽取/平台识别/无水印trick/三重签名/SM3向量 锁死框架正确性；
    # 再用真实 invoke(parse.resolve, download=True) 端到端打本地 Range server → 下载引擎落盘。
    ("eval_t60_parse", ["evals/t60_parse.py"], HERE, 90),
    # ---- T61：代码转化（LLM 机器转 + 语法/编译机器验）----
    # 全离线：mock llmgw，锁死 去围栏 / python 语法校验 / 端到端转译 / 网关失败透传。
    ("eval_t61_code_convert", ["evals/t61_code_convert.py"], HERE, 90),
    # ---- T62：出向网络层（僵尸代理不许拖死全站）----
    # 全离线：自建本地 server 当目标 + 指向死端口的假代理（自己造毒，不依赖本机当前状态）。
    # 锁死：默认直连 / 死代理必报错不静默回退 / 毒环境下 net.urlopen 仍通 / import fleet 全局兜底生效。
    ("eval_t62_net", ["evals/t62_net.py"], HERE, 150),
    # ---- 补登（2026-11-02 统一图景时清账）----
    # 这两个脚本写了很久但一直没进 CASES：`_stats.json` 的 orphan_evals 一直报 2，
    # 没人管。t50 红是因为它自己坏了（pc_melo 未定义 + 引擎循环没按 kind 分流），
    # 坏掉的脚本没人登记 → 更没人跑 → 一直坏。这就是「写了不跑 = 没写」的活样本。
    ("eval_t50_voice_synth", ["evals/t50_voice_synth.py"], HERE, 300),
    # t59 号已被 eval_t59_groups 占了（两支线并行写的），这里用 t59b 不撞号。
    ("eval_t59b_pinhole", ["evals/t59_pinhole.py"], HERE, 120),
    # ---- T63：文档 ↔ 代码对齐（统一图景的守门人）----
    # t54 管「代码 → _stats.json」，t63 管「_stats.json → 正文 + 正文引用是否落地」。
    # 锁死：自动块必须存在且同步 / 正文不许写累加绝对值 / 引用的文件必须真在 / 游离门禁 0。
    ("eval_t63_docs_sync", ["evals/t63_docs_sync.py"], HERE, 120),
    ("eval_t64_protocol", ["evals/t64_protocol.py"], HERE, 180),
    ("eval_t65_slider", ["evals/t65_slider.py"], HERE, 180),
    ("eval_t66_wall", ["evals/t66_wall.py"], HERE, 120),
    ("eval_t67_solve_page", ["evals/t67_solve_page.py"], HERE, 180),
    # 可选依赖越界：一个"没装 Pillow"能把 415 条能力一起带走（11-02 核最后一环时抓到）
    ("eval_t68_slider_deps", ["evals/t68_slider_deps.py"], HERE, 120),
    # 指纹层三件套：档案自洽 / 注入正确 / 沙箱隔离（含 23 种矛盾注入的反向验证）
    ("eval_t69_fingerprint", ["evals/t69_fingerprint.py"], HERE, 150),
    # 注册流水线：四段分开 / 失败不互相掩盖 / 不产生假成功（11-03 一轮浏览器乱点换来的）
    ("eval_t70_signup_kit", ["evals/t70_signup_kit.py"], HERE, 150),
    # 网络层总览（11-03）：不是新探测，是把散在六处的状态收成一条能力。
    # 守的是「只读不留痕 / 默认不出网 / 缺数据不填默认值 / 一侧炸不塌」这四条，
    # 外加顶层不许 import 可选依赖（铁律 10 的第五次复发防线）。
    ("eval_t71_net_face", ["evals/t71_net_face.py"], HERE, 150),
    # 本地资料库接总线（11-03）：论文元数据 / 域名分类 / 任意格式文档转换，
    # 另含内容生成补强（免 key 出图 + 图片看/缩）。守「可搜、按需解析不落索引、
    # 空库不崩、坏行不整库废」四条。
    ("eval_t72_library_bus", ["evals/t72_library_bus.py"], HERE, 180),
    # 角色设备接总线（11-03，融合 ElecKoi）：卡不只是一段提示词，是一局有状态的世界。
    # 守「宏按中文边界渲染 / 世界书按 scan_depth 与 selectiveLogic 选 / 变量事务失败
    # 一点不改 / 正则两档分明且 EJS 显式跳过 / 坏卡不拖垮卡库」五条。
    ("eval_t73_persona_bus", ["evals/t73_persona_bus.py"], HERE, 180),
    # 落盘层（11-03，学 CodeAssist 的 durable 纪律）：全仓 23 处各自拼 tmp+replace，
    # 共享临时名会让两个写者交错成「A 的头 + B 的尾」，仍是合法 JSON，坏得没人发现。
    # 守「旧写法确实坏 / 新写法必是完整 payload / 失败不毁旧文件 / 1.5 万条往返一致
    # / 三个真实写点接线到位 / 并发不留残留」六条。
    ("eval_t74_durable", ["evals/t74_durable.py"], HERE, 180),
    ("eval_t75_text_clock", ["evals/t75_text_clock.py"], HERE, 240),
    # 网络观测（11-03，V1.1 计划书第六节）：抓取不再「只拿数据」，
    # 每次请求都产出耗时/重定向链/状态码/错误分类，并能诊断卡在哪一层。
    ("eval_t76_netprobe", ["evals/t76_netprobe.py"], HERE, 120),
    # 诊断深化（11-03）：结果未知识别（没收到结果 ≠ 失败）、故障沉淀成知识库、
    # 诊断带 task_id 可串请求链。
    ("eval_t77_netdiag", ["evals/t77_netdiag.py"], HERE, 120),
    # 出网自适应（11-03）：默认直连，只有网络层失败才回退代理；HTTP 错误不回退。
    ("eval_t78_net_adaptive", ["evals/t78_net_adaptive.py"], HERE, 120),
    # 仓库自检（11-03）：HEAD 里的导入目标是否都在库里。补的是「门禁只在
    # 工作区跑，所以本地有、仓库没有的模块永远是绿的」这个盲区——
    # 当天就抓到 fleet/gemini.py 裸引用了从没入库的 fleet/vault.py。
    ("eval_t79_repo_selfcheck", ["evals/t79_repo_selfcheck.py"], HERE, 120),
    # 爬虫 B 线（14475ef）写的三个离线门禁：写了没登记 = 没写。
    # t63 的「游离门禁 0」**第三次**当场抓到 —— 这条断言存在的意义就在这儿。
    ("eval_t_cloud", ["evals/t_cloud.py"], HERE, 60),
    ("eval_t_ghcode", ["evals/t_ghcode.py"], HERE, 60),
    ("eval_t_selfindex", ["evals/t_selfindex.py"], HERE, 60),
    # 并行支线（7659e49 卡死守卫 + 节奏控制）写的两个门禁：写了但没登记 = 没写。
    # t63 的「游离门禁 0」当场抓到它们，这正是那条断言存在的意义。
    ("eval_t_guard_stall", ["evals/t_guard_stall.py"], HERE, 120),
    ("eval_t_pacing", ["evals/t_pacing.py"], HERE, 120),
    # 批次2（6bb4851 上下文压缩）同样写了没登记 —— t63 又抓到一次。
    ("eval_t_compress", ["evals/t_compress.py"], HERE, 120),
    # ---- 下载层门禁：解析层的下游（直链/多源/续传/hash校验/任务接口）----
    # 全离线（本地 Range server），与 t60 同源登记，避免「写了不跑=没写」。
    ("eval_t55_download", ["evals/t55_download.py"], HERE, 120),
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
    ("eval_phone_mcp_pair", ["evals/test_phone_mcp_pair.py"], HERE, 120),
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

#: 纯新增快车道必跑的**结构面**三项（见 `_select_by_changed` 里的说明）。
#:
#: 为什么只要这三项：追加代码不可能改变某个行为断言的结论，它能弄坏的只有
#: 「catalog 起不来 / 能力契约对不上 / 注册表与结果信封这两块地基」。
#: 2026-11-06 实测：这一组 1.4~6 秒，换掉的是 43 项 193 秒。
#: 它抓到的第一个真 bug（web.quote 实现了没登记）此前 158 项门禁全绿也没抓到 ——
#: **不是跑得少，是没跑对地方。**
FASTLANE_CORE = ("catalog_contract", "fleet_registry", "fleet_result")



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


def _git_additive_only(base=None):
    """返回「纯新增」的文件集合：diff 里一行都没删。

    判据只看 `git diff --numstat` 的删除列。改一行必然表现为「1 加 1 删」，
    所以删除列为 0 就是真正的追加，没动过任何已有行。

    为什么这条判据值得存在：追加代码动不到已有代码路径，能坏的只剩
    「导入崩了 / 契约不一致」这类**结构面**的问题，跟某个具体断言无关。
    按 import 闭包把几十项拉起来跑，是把「被 import」当成了「受影响」。
    2026-11-06 实测这笔账：catalog.py 追加 162 行 → 43 项 193 秒，
    那 43 项里没有一项的断言跟新增内容有关。
    """
    try:
        cmd = ["git", "diff", "--numstat"]
        cmd.append(base if base else "HEAD")
        out = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
    except OSError:
        return None
    keep = set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add, dele, path = parts[0], parts[1], parts[2].strip()
        # 二进制文件给的是 "-"，当成「不确定」处理，不进快车道
        if add == "-" or dele == "-":
            continue
        if int(dele or 0) == 0 and int(add or 0) > 0:
            keep.add(path.replace("\\", "/"))
    # 未跟踪的新文件本身就是纯新增（还没进 diff）
    try:
        st = subprocess.run(["git", "status", "--porcelain"], cwd=HERE,
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace").stdout
        for line in st.splitlines():
            if line.startswith("?? ") or line.startswith("A  "):
                keep.add(line[3:].strip().replace("\\", "/"))
    except OSError:
        pass
    return keep


def _domain_index():
    """能力域 -> 实现它的设备模块。

    「几百个功能通过分类，跟模块一样」（2026-11-06 主人）—— 362 条能力本来就按
    `drive.*` / `image.*` / `pcctl.*` 这种域分好了，那门禁也该按域挑，
    而不是让人记住「改哪个文件对应哪几条测试」。

    映射不手贴：设备 `caps()` 说自己认哪些名字，`type(dev).__module__`
    说实现在哪个文件 —— 两边一拼就是域到模块的真映射。
    """
    sys.path.insert(0, HERE)
    try:
        import fleet.catalog as CAT
        reg = CAT.build()
    except Exception:
        return {}
    idx = {}
    for d in reg.devices():
        try:
            caps = list(d.caps() or ())
        except Exception:
            continue
        mod = sys.modules.get(type(d).__module__)
        path = getattr(mod, "__file__", None)
        if not path:
            continue
        rel = os.path.relpath(os.path.abspath(path), HERE).replace("\\", "/")
        for c in caps:
            if isinstance(c, str) and "." in c:
                idx.setdefault(c.split(".")[0], set()).add(rel)
    return idx


def _domain_cases(domains, data, verbose=True):
    """按域挑门禁项。返回 (项, 没覆盖到的域)。

    归属判据用**入口文件**，不用 import 闭包：一个模块就是这一域的门禁，
    这正是「跟模块一样」。用闭包的话每个域都会显示 59 项 —— 因为 catalog.py
    被所有人 import，那又变回「被 import 当成受影响」的老毛病了。
    """
    idx = _domain_index()
    if not idx:
        print("推不出能力域映射（fleet.catalog 起不来）—— 退回全量")
        return CASES, []
    entries = {n: (v or {}).get("entry", "")
               for n, v in (data.get("cases") or {}).items()}
    rev = data.get("reverse") or {}
    pick = set()
    holes = []
    for dom in domains:
        mods = idx.get(dom) or set()
        if not mods:
            holes.append((dom, "没有设备声明这个域"))
            continue
        in_dom = {n for n, e in entries.items() if e and e in mods}
        if not in_dom:
            # 没有专属入口的域，退一步用「直接依赖」（dist<=1），仍不算传递依赖
            for f in mods:
                for name, d in (rev.get(f) or []):
                    if d <= 1:
                        in_dom.add(name)
        if not in_dom:
            holes.append((dom, "域内模块没有任何门禁项覆盖"))
        pick |= in_dom
    cases = [c for c in CASES if c[0] in pick]
    if verbose:
        print("域：%s -> %d 项" % ("、".join(domains), len(cases)))
        for dom, why in holes:
            print("  ⚠ %s：%s" % (dom, why))
    return cases, holes


def _show_domains():
    """`--domains`：把 369 条能力按域摊开，顺手标出哪些域门禁是空的。"""
    idx = _domain_index()
    if not idx:
        print("推不出能力域映射（fleet.catalog 起不来）")
        return 1
    path = os.path.join(HERE, "tools", ".gate_map.json")
    data = {}
    if os.path.isfile(path):
        import json as _j
        data = _j.load(open(path, encoding="utf-8"))
    entries = {n: (v or {}).get("entry", "")
               for n, v in (data.get("cases") or {}).items()}
    rev = data.get("reverse") or {}
    sys.path.insert(0, HERE)
    import fleet.catalog as CAT
    reg = CAT.build()
    count, per = {}, {}
    for c in reg.capabilities():
        dom = c.name.split(".")[0]
        count[dom] = count.get(dom, 0) + 1
        per.setdefault(dom, set()).add(c.name)
    rows = []
    for dom in sorted(count):
        mods = idx.get(dom) or set()
        gates = {n for n, e in entries.items() if e and e in mods}
        if not gates:
            for f in mods:
                for name, d in (rev.get(f) or []):
                    if d <= 1:
                        gates.add(name)
        rows.append((dom, count[dom], gates))
    rows.sort(key=lambda r: (-r[1], r[0]))
    print("能干的事 %d 条，分在 %d 个域里。\n" % (len(reg.capabilities()), len(rows)))
    print("%-10s %5s  %s" % ("域", "能力", "盖住它的门禁项"))
    for dom, n, gates in rows:
        if gates:
            g = "、".join(sorted(gates)[:4]) + ("…" if len(gates) > 4 else "")
        else:
            g = "⚠ 空着"
        print("%-10s %5d  %s" % (dom, n, g))
    empty = [r[0] for r in rows if not r[2]]
    print("\n门禁空着的域：%d/%d 个%s"
          % (len(empty), len(rows), "（%s）" % "、".join(empty[:12]) if empty else ""))
    return 0


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

    # 门禁/映射自身变了：映射不再可信，但**也不该在本地拉全量**。
    # 2026-11-06 主人定：「不要在本地中搞那个什么门禁时间那么长，搞完推上去，从上面检测」。
    # 所以这里只跑结构面（契约 + 注册表 + 结果信封），全量交给远程 CI。
    #
    # 🔴 但结构面之外还要加上「改动文件自己对应的项」：改了 run_selftests.py
    # 常常就是在**加门禁项**，而「加了一条门禁却没跑过它」等于没验证。
    # 这个漏洞 2026-11-06 实打实地踩了一次：加完 fleet_pdfops 跑 --changed，
    # 只跑了 3 项结构面，新模块一条断言都没执行。
    meta = {f for f in files
            if os.path.basename(f) in ("run_selftests.py", "gate_map.py",
                                       "check_catalog.py")}
    if meta:
        entries = {n: (v or {}).get("entry", "")
                   for n, v in (data.get("cases") or {}).items()}
        own = {n for n, e in entries.items() if e and e in set(files)}
        keep_names = own | set(FASTLANE_CORE)
        keep = [c for c in CASES if c[0] in keep_names]
        print("改的是门禁基础设施（%s）—— 跑结构面 %d 项"
              "%s，全量交给远程 CI。要立刻全量就显式 --all。"
              % (", ".join(sorted(meta)), len(FASTLANE_CORE),
                 (" + 改动文件自己的 %d 项（%s）"
                  % (len(own), "、".join(sorted(own)))) if own else ""))
        return keep, ""

    # ---- 纯新增快车道 ----
    # 全部改动文件都「一行没删」⇒ 动不到已有代码路径，能坏的只有结构面。
    # 这时按 import 闭卡拉几十项是把「被 import」当成「受影响」，是过度防御
    # （2026-11-06 主人原话：又不是造核弹）。只跑两类：
    #   ① 入口就是被改文件的项（改谁测谁）
    #   ② 结构面三项：catalog 契约 / 注册表基类 / 结果信封
    # 不放心就加 --deep（走原闭包）或 --all（全量）。
    if "--deep" not in sys.argv:
        addonly = _git_additive_only(base)
        if addonly is not None and files and set(files) <= addonly:
            entries = {n: (v or {}).get("entry", "")
                       for n, v in (data.get("cases") or {}).items()}
            own = {n for n, e in entries.items() if e and e in files}
            keep = own | set(FASTLANE_CORE)
            cases = [c for c in CASES if c[0] in keep]
            print("改动 %d 文件（全是纯新增，没删改任何已有行）-> %d 项快车道"
                  % (len(files), len(cases)))
            if own:
                print("  改谁测谁：%s" % ", ".join(sorted(own)))
            print("  结构面：%s" % ", ".join(sorted(set(FASTLANE_CORE) & set(keep))))
            print("  要按 import 闭包跑加 --deep，要全量加 --all")
            return cases, ""

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


def _write_ci_summary(results, spent, shard=None):
    """在 GitHub Actions 的 Job Summary 上写一份「哪项红了、红在哪」。

    2026-11-06 主人定：本地不再跑长门禁，改完推上去让远程检测 ——
    「他能红了基本上也是能提示哪里有问题，还能给你日志呢」。
    既然如此，远程这份输出就必须自己把话说完：不能只丢一句 exit 1 让人去翻
    几万行日志里找 `[FAIL]`。所以失败项连同它自己的输出尾巴一起进摘要。
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    passed = sum(1 for r in results if r[1])
    lines = []
    head = "门禁分片 %d/%d" % shard if shard else "门禁（本地/全量）"
    lines.append("## %s：%d/%d 通过" % (head, passed, len(results)))
    bad = [r for r in results if not r[1]]
    if not bad:
        lines.append("")
        lines.append("全绿，耗时 %.0f 秒。" % sum(spent.values()))
    else:
        lines.append("")
        lines.append("### ❌ 失败 %d 项" % len(bad))
        for name, _ok, rc, tail in bad:
            lines.append("")
            lines.append("<details><summary><code>%s</code>（rc=%s，%.1fs）</summary>"
                         % (name, rc, spent.get(name, 0.0)))
            lines.append("")
            lines.append("```")
            lines.append((tail or "（无输出）")[-1500:])
            lines.append("```")
            lines.append("</details>")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def main():
    raw = sys.argv[1:]
    flags = {a.lower() for a in raw if a.startswith("--")}
    # --shard / --domain / --changed 后面跟的是**值**，不是门禁名过滤词。
    # 不排除它们，--shard 1/4 会把 "1/4" 当成名字去筛 CASES，结果四片各跑 0 项，
    # CI 还一片绿。分片本身在下面按位置切片，不该在这里先被过滤掉。
    _value_flags = {"--shard", "--domain", "--changed"}
    _value_pos = {i + 1 for i, a in enumerate(raw) if a in _value_flags}
    only = [a.lower() for i, a in enumerate(raw)
            if i not in _value_pos and not a.startswith("--")]
    include_slow = bool(flags & {"--slow", "--all"})
    if "--slowest" in flags:
        return show_slowest()
    if "--domains" in flags:
        return _show_domains()

    # --domain image[,drive]：按能力域跑。域 -> 模块 -> 门禁项，全从代码推。
    doms = []
    for i, a in enumerate(raw):
        if a == "--domain":
            nxt = raw[i + 1] if i + 1 < len(raw) else ""
            doms = [x.strip() for x in nxt.replace("，", ",").split(",") if x.strip()]
    if doms:
        _p = os.path.join(HERE, "tools", ".gate_map.json")
        _d = {}
        if os.path.isfile(_p):
            import json as _j
            _d = _j.load(open(_p, encoding="utf-8"))
        cases, _holes = _domain_cases(doms, _d)
        if not cases:
            print("这些域没有覆盖到任何门禁项（见上面的 ⚠）")
            return 0

    # 🔴 默认（不带任何参数）= 按改动跑，而且纯新增只跑结构面。
    #
    # 2026-11-06 主人定调：「不要在本地中搞那个什么门禁时间那么长，搞完推上去，
    # 从上面检测」。所以本地默认**不再**是全量 —— 全量 24 分 45 秒这件事
    # 交给远程 CI（push 到 main 自动 4 片并行，红了给日志给摘要）。
    #
    # 想全量就明说 --all；CI 自己用 --shard，不受这里影响。
    if not raw:
        cases, _ = _select_by_changed(None)
        if cases is not None and not cases:
            return 0
    elif not doms:
        cases = None

    # --changed [基线]：只跑被改动文件影响到的项。这是日常该用的入口 ——
    # 117 项全量十几分钟，改一行 fleet/motion.py 没必要重跑手机配对。
    if cases is None and "--changed" in flags:
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

    # --shard i/n：按位置切片，把串行全量摊到 n 个 runner 上跑。
    # 按位置而不是按耗时均衡：结果可复现，不需要维护一张权重表。
    shard = None
    for i, a in enumerate(raw):
        if a == "--shard":
            nxt = raw[i + 1] if i + 1 < len(raw) else ""
            try:
                i_, n_ = (int(x) for x in nxt.split("/"))
            except ValueError:
                print("--shard 用法：--shard 1/4（从 1 开始，分母为正整数）")
                return 2
            if n_ < 1 or not 1 <= i_ <= n_:
                print("--shard 越界：%s（要求 1 <= 分子 <= 分母）" % nxt)
                return 2
            shard = (i_, n_)
    if shard:
        cases = cases[shard[0] - 1::shard[1]]
        print("（分片 %d/%d：本片 %d 项）" % (shard[0], shard[1], len(cases)), flush=True)

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
            results.append((name, ok, rc, tail))
    finally:
        _restore_states(snap)
        _record_times(spent)

    passed = sum(1 for r in results if r[1])
    total_s = sum(spent.values())
    print(f"\n=== 结果：{passed}/{len(results)} 通过，耗时 {total_s:.0f}s ===")
    _write_ci_summary(results, spent, shard)
    if passed != len(results):
        slow = sorted(spent.items(), key=lambda kv: -kv[1])[:3]
        print("失败项:", ", ".join(r[0] for r in results if not r[1]))
        print("这次最慢三项:", ", ".join("%s %.0fs" % kv for kv in slow))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

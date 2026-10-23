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
    ("fleet_list", ["-m", "fleet", "list"], HERE, 120),
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


def main():
    only = [a.lower() for a in sys.argv[1:]]
    cases = [c for c in CASES if not only or any(o in c[0].lower() for o in only)]
    print(f"=== 天网工程离线自测：{len(cases)} 项 ===", flush=True)
    results = []
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

    passed = sum(1 for _, ok in results if ok)
    print(f"\n=== 结果：{passed}/{len(results)} 通过 ===")
    if passed != len(results):
        print("失败项:", ", ".join(n for n, ok in results if not ok))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

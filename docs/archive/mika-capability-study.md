# Mika 能力调研 → 天网手机线融入清单

> 2026-11-02 · 源码 /e/xmanbian/_vendor-src/Mika（Kotlin，~700 文件，clone 自 wangzan101/Mika）
> 定性：Mika 作为产品 = 手机陪伴类，价值不高；**作为能力参考 = 高价值**，尤其护栏/节奏/压缩全是纯逻辑，Python 移植零摩擦。
> 天网现状对照：Python 无障碍注入（fleet-bridge）+ ui_xml 解析（android_device.py）+ LLM 单循环，**无节奏控制、无知识库、无卡死守卫**。

## 一、核心机制（按模块）

### 1. agent/ — 规划循环
- AgentLoop：单工具调用循环，每轮只执行第一个工具调用，其余以 parallel_tool_call_skipped 退回；不带工具的回复才是最终回答（AgentLoop.kt:96,449-702）。
- 空回复/漏调用/语言不对各提醒一次再收尾（:503-561）；步数默认 60，用尽后 finalTurn 只留 end_not_done（:936-982）。
- ContextBuilder：`<context kind source trust>` 外壳 + token 预算（总量 8k/知识 5k/单份 2k）；每轮注入运行时状态（位置/步数/时间/用户语言）（ContextBuilder.kt:24-71,148-165）。
- **确定性压缩**（ContextCompressor.kt:131-204）：不调模型，旧工具往返折成一行 `Step N tool(target) → applied/failed`；任务原话/追问/确认往返不折；压缩阶梯三档（缩旧结果→折摘要→狠压只留 6 往返）。保护前缀缓存。
- ModelRouter（:34-56）：轻量模型接"同 App、上步没出错、无图"的简单步；出错/守卫/画面没变后 2 步强制主模型；放弃/确认/接管只许主模型。
- OutcomeJudge（:44-72）：交付后后台用轻量模型判 done/partial/not_possible（30s 超时，取回复最后一个 JSON）。
- IntentGate（:12-45）：第一次要用工具前，小模型判 DISCUSS/READ/ACT，讨论类直接撤掉工具调用。

### 2. cap/phone/ — 虚拟屏与工具集
- 虚拟屏 = `DisplayManager.createVirtualDisplay` + `ActivityOptions.setLaunchDisplayId`（ShellService.kt:109-112）；两任务抢同 App → START_IN_USE_BY_OTHER，模型用 phone_launch_app(wait=N) 排队，死锁互等被识别（:179-281）。
- 工具两层：通用屏幕工具（observe/tap/type/scroll/…）+ 手机专属查询类（phone_query/find/collect/extract/wait/event/dialog/gesture/set_wheel/run）。
- CommandSteer（PhoneAdvancedTools.kt:327-330）：模型用 shell 点/打字时先提醒、再犯直接拒（防绕过结构化工具）。

### 3. access/ + action/ — 感知与执行
- ScreenReader：遍历无障碍窗口（滤输入法/浮层）→ UiTreeNode 树 + FNV 界面指纹（ScreenReader.kt:41-69）。
- **ActionExecutor = 所有写操作唯一入口**，顺序固定：操作守卫→锁屏关卡→不可逆判定+同意核对→对外间隔→界面停留→执行→事后守卫（ActionExecutor.kt:105-174）。
- RiskJudge：屏幕有 ¥/$ 金额时提交类按"花钱"判需确认（:29-41）。
- **OutwardPacing**（对外节奏，:21-30）：首次 8-20s 随机，每多一次 +0.5-1s，上限 30s，30 分钟窗口；"从上次做完算起、只补不够的部分"。
- **AppPacing**（操作节奏，:33-57）：同屏停 0.5-1.2s、新屏 0.9-2s；CAREFUL 档新屏 2.5-5s + 每字 15ms 阅读时间。

### 4. guard/ — 卡死守卫（⭐ 最值得抄）
- OperationGuards 四类事实检测停滞：重复动作/无效果（动过界面仍一样）/重读/兜圈（2-3 状态循环）/连续失败，各配"提醒→再提醒→叫停"阶梯（GuardRules.kt:11-22：重读 5/8 提醒 12 叫停、失败 2/4/8、兜圈按步数 1/5 叫停）。
- 提醒时附"卡在哪+三条出路"塞进下一轮 prompt；画面一变计数全作废。全纯逻辑约 400 行。

### 5. knowledge/ — App 使用知识库
- Markdown 文件（frontmatter：name/display/packages/aliases/open/pace）+ 正文「是什么/界面/功能/已知现象」，写易混淆点不写操作步骤。
- 检索 = 确定性字符串匹配（包名/域名/别名，命中越长越具体，不调模型）；开局按任务匹配 ≤3 份，切 App 时按包名补注一次；外壳标"可能过时,以当前屏幕为准"。

### 6. memory/ — 记忆
- 任务后单独一次轻量模型调用从"用户原话"提取 add/update/forget；**每条必须带 quote 且逐字出现在用户话里**（防编造）；"这次/今天先"开头的一次性参数不许改长期偏好。
- SensitiveFilter：密码/验证码关键词、身份证 MOD11-2、卡号 Luhn、API key 正则，命中拒存。
- 注入 Auto：≤4k token 全量（缓存友好），超了退回检索。

### 7. eval/tasks/ — 评估
- 每任务一个 JSON：goal+category（barrier/impossible/irreversible/memory/read/skills/web/write）+checks+expect+marker。
- impossible 类验"如实说做不到"；irreversible 类验该确认时确认；memory 类 session 配对（-1-tell / -2-use）。

## 二、工具集全清单

通用屏幕：observe / list_elements / screenshot / describe / find_visually / tap / scroll / swipe / type_text / clear_text / back / press_key / diagnose
手机专属：phone_query（树内查控件）/ phone_find（自动滚动找到为止）/ phone_collect（多屏合并）/ phone_extract（结构化提取）/ phone_wait / phone_event / phone_dialog / phone_gesture / set_wheel / phone_run / phone_system_command
App 管理：phone_list_apps / phone_launch_app（user=分身, wait=排队）/ phone_app_after_task（keep/close）/ phone_share_text / phone_open_link
系统：phone_set_alarm / phone_set_timer / phone_show_alarms / phone_add_calendar_event
网页：navigate / read_page / evaluate_js / web_run_site_command / tabs / downloads / upload / site_mode / script / interact
对话/元：ask_user / request_confirmation / hand_to_user / handle_obstacle / end_not_done / do_steps（一次多步批）/ view_past_screens / view_user_screen
知识/记忆/技能：knowledge_search/read / skill_search/read/save / memory_search/save / profile_edit / history_read / schedule_task
沙箱：run_command / run_bundled_script / read_file / write_file / search_files / share_file

## 三、融入天网手机线（按性价比排序）

| # | 能力 | 工作量 | 抄什么 | 落点 |
|---|---|---|---|---|
| 1 | 卡死守卫 | 天级 | 界面指纹哈希 + 五条阶梯（重读5/8/12、无效果3/6/9/12、兜圈1/5、失败2/4/8），warn 附"卡在哪+出路" | 新建 `guards.py`，指纹挂 ui_xml 解析输出 |
| 2 | 对外/操作节奏 | 小时级 | OutwardPacing+AppPacing 两类 ~250 行纯逻辑直译 | 执行动作前插 `pacing.wait_before()` |
| 3 | 不可逆+确认关卡 | 天级 | send/pay/delete 关键词 + 金额正则 `[¥￥$]\d`，命中挂起等确认，同意一次有效 | tool 执行层加 `confirm_gate.py` |
| 4 | 确定性上下文压缩 | 天级 | 旧往返折一行，原话/确认不折，前缀只追加 | 历史管理改造，新建 `compress.py` |
| 5 | APP.md 知识库 | 小时级建机制 | frontmatter 格式 + 包名/任务文字确定性匹配 + "可能过时"外壳 | `knowledge/` 目录 + loop 开局注入 |
| 6 | 复合工具（find/collect） | 小时级 | 本地确定性循环（2 次指纹不变即停），一轮省 5-20 次调用 | 工具注册处加 2 个工具 |
| 7 | 结局判定+收尾核对 | 小时级 | 异步轻量模型判 done/partial；步数用尽只留 end_not_done | loop 收尾处 |
| 8 | 记忆提取+敏感过滤 | 天级 | quote 逐字校验 + Luhn/MOD11-2/API key 正则 | 新建 `memory.py` + `sensitive.py` |

次优先：IntentGate 讨论分流（小时级）；主/轻模型路由（小时级，出错回切 2 步）。

package app.skynet.tianwang.next

import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject

/**
 * 把手机本机能力映射成 MCP 工具。
 *
 * 工具名不用 `self.ui.dump`（MCP 工具名不允许点号），统一改成下划线；
 * 调用时再翻回 `PhoneSelf` 的能力名。两层名字只在这里做一次映射，
 * 避免控制面看到的工具名和手机实际能力各说各话。
 */
object PhoneMcp {
    private val tools: List<McpTool> = listOf(
        McpTool(
            "self_status", "查看无障碍通道是否在线、当前前台 App、屏幕分辨率。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_ui_dump", "把当前界面压成紧凑控件树（文字/描述/id/坐标/可点可滚）。",
            schema(mapOf("limit" to prop("integer", "最多返回多少个控件，默认 120，上限 600")), emptyList()),
        ),
        McpTool(
            "self_ui_find", "按文字、内容描述或控件 id 查找，返回匹配项与坐标。",
            schema(
                mapOf(
                    "query" to prop("string", "要匹配的文字/描述/控件 id 的一部分"),
                    "regex" to prop("boolean", "把 query 当正则解释，默认 false"),
                ),
                listOf("query"),
            ),
        ),
        McpTool(
            "self_ui_tap", "点中匹配控件；不可点时向上找可点父容器，再不行按坐标点。",
            schema(
                mapOf(
                    "query" to prop("string", "要点的控件文字/描述/控件 id"),
                    "regex" to prop("boolean", "query 当正则解释"),
                    "index" to prop("integer", "匹配到多个时点第几个，从 0 开始"),
                    "long" to prop("boolean", "长按，默认 false"),
                ),
                listOf("query"),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_ui_tap_at", "按屏幕坐标点一下（WebView/自绘界面用），可长按。",
            schema(
                mapOf(
                    "x" to prop("integer", "屏幕横坐标（像素）"),
                    "y" to prop("integer", "屏幕纵坐标（像素）"),
                    "long" to prop("boolean", "长按，默认 false"),
                ),
                listOf("x", "y"),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_ui_swipe", "按坐标从 (x1,y1) 滑到 (x2,y2)，可调持续时间。",
            schema(
                mapOf(
                    "x1" to prop("integer", "起点横坐标"),
                    "y1" to prop("integer", "起点纵坐标"),
                    "x2" to prop("integer", "终点横坐标"),
                    "y2" to prop("integer", "终点纵坐标"),
                    "duration" to prop("integer", "滑动毫秒数，默认 400，范围 100–3000"),
                ),
                listOf("x1", "y1", "x2", "y2"),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_ui_text", "把文字写进当前焦点输入框；给 query 就写进匹配到的那个。",
            schema(
                mapOf(
                    "text" to prop("string", "要写入的文字"),
                    "query" to prop("string", "可选：定位输入框的文字/描述/id"),
                ),
                listOf("text"),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_ui_scroll", "向前或向后滚一屏；没有可滚控件时退化成滑动屏幕。",
            schema(
                mapOf(
                    "direction" to prop("string", "down=向后滚（看下面），up=向前滚（看上面）"),
                    "query" to prop("string", "可选：定位某个可滚容器"),
                ),
                listOf("direction"),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_ui_key", "发 back / home / recents / notifications 全局动作。",
            schema(mapOf("key" to prop("string", "back｜home｜recents｜notifications")), listOf("key")),
            sideEffect = true,
        ),
        McpTool(
            "self_ui_wait", "轮询当前界面，直到文字/描述/id 出现或超时。",
            schema(
                mapOf(
                    "query" to prop("string", "要等的文字/描述/控件 id 的一部分"),
                    "timeout" to prop("integer", "最多等多少毫秒，默认 5000，上限 30000"),
                    "regex" to prop("boolean", "把 query 当正则解释，默认 false"),
                ),
                listOf("query"),
            ),
        ),
        McpTool(
            "self_app_launch", "按包名打开 App（例：com.android.settings）。",
            schema(mapOf("pkg" to prop("string", "包名")), listOf("pkg")),
            sideEffect = true,
        ),
        McpTool(
            "self_app_open_url", "用系统浏览器打开 http/https 链接（不是网页抓取）。",
            schema(mapOf("url" to prop("string", "要打开的 http/https 链接")), listOf("url")),
            sideEffect = true,
        ),
        McpTool(
            "self_app_list", "列出桌面有启动入口的应用（名称 + 包名），可按名称/包名过滤。",
            schema(
                mapOf(
                    "query" to prop("string", "可选：按名称或包名过滤"),
                    "limit" to prop("integer", "最多返回多少个，默认 40，上限 200"),
                ),
                emptyList(),
            ),
        ),
        McpTool(
            "self_ui_screenshot", "系统无障碍截图（Android 11+）：回传压缩后的 JPEG 图片内容，同时在手机私有目录留一张 PNG。",
            schema(mapOf("max" to prop("integer", "JPEG 最长边像素，默认 1280，范围 320–2048")), emptyList()),
        ),
        McpTool(
            "self_notif_list", "读最近通知（发件 App、标题、正文）。需用户先授予通知使用权。",
            schema(mapOf("limit" to prop("integer", "最多返回多少条，默认 20，上限 40")), emptyList()),
        ),
        McpTool(
            "self_notif_clear", "清空本机内存里的通知缓存；不删除系统通知。",
            schema(emptyMap()),
            sideEffect = true,
        ),
        McpTool(
            "self_clipboard_get", "读当前剪贴板文字；后台读取可能被 Android 10+ 拒绝。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_clipboard_set", "把文字写进系统剪贴板。",
            schema(mapOf("text" to prop("string", "要写入的文字")), listOf("text")),
            sideEffect = true,
        ),
        McpTool(
            "self_file_read", "读本机交换目录里的文件（base64 内联），目录 = 私有外部目录/lanfiles。",
            schema(mapOf("path" to prop("string", "文件名，落在 lanfiles 目录下"))),
        ),
        McpTool(
            "self_file_write", "把一段 base64 写到本机交换目录（文件名落在 lanfiles 下）。",
            schema(mapOf(
                "path" to prop("string", "文件名"),
                "data" to prop("string", "base64 文件内容"),
                "overwrite" to prop("boolean", "已存在时是否覆盖，默认 false"),
            ), listOf("path", "data")),
            sideEffect = true,
        ),
        McpTool(
            "self_code_runtime", "读手机内置 Linux 运行时状态（引擎、rootfs、proot、工作区占用）。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_code_files", "列出手机代码工作区里的文件。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_code_read", "读手机代码工作区里某个文件的文本。",
            schema(mapOf("name" to prop("string", "文件名")), listOf("name")),
        ),
        McpTool(
            "self_code_write", "把一段代码写进手机代码工作区（不执行）。",
            schema(mapOf(
                "name" to prop("string", "文件名"),
                "code" to prop("string", "代码内容"),
                "lang" to prop("string", "sh / py / js / html，默认 sh"),
            ), listOf("name", "code")),
            sideEffect = true,
        ),
        McpTool(
            "self_code_run", "在手机上跑一段代码：shell 开箱可用，Python/Node 需 Linux 用户态。",
            schema(mapOf(
                "code" to prop("string", "代码内容"),
                "lang" to prop("string", "sh / py / js / html，默认 sh"),
                "name" to prop("string", "保存的文件名，可省"),
                "args" to prop("string", "附在命令后的参数"),
                "timeout_ms" to prop("integer", "超时毫秒，默认 15000"),
            ), listOf("code")),
            sideEffect = true,
        ),
        McpTool(
            "self_code_command", "在手机上执行一条原始 shell 命令（工作区为当前目录）。",
            schema(mapOf(
                "command" to prop("string", "要执行的命令"),
                "timeout_ms" to prop("integer", "超时毫秒，默认 15000"),
            ), listOf("command")),
            sideEffect = true,
        ),
        McpTool(
            "self_code_task", "让手机自己跑一组预设步骤（体检/清单/搭骨架/生成网页/清理）。",
            schema(mapOf("key" to prop("string", "probe/manifest/scaffold/page/cleanup"))),
            sideEffect = true,
        ),
        McpTool(
            "self_code_history", "读手机最近 20 条代码运行记录。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_tty_list", "列出长驻 shell 会话：后端、是否在跑、累计输出、当前目录。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_tty_open", "开一个长驻 shell 会话（stdin 可写、输出流式、不截断到 8KB）。",
            schema(mapOf(
                "cwd" to prop("string", "工作目录，空则用应用私有目录"),
                "pty" to prop("boolean", "要真 tty 就传 true；系统没有 script 会如实回落管道模式"),
            )),
            sideEffect = true,
        ),
        McpTool(
            "self_tty_write", "往会话写一段输入（默认补回车）。发 \\u0003 可当中断前台命令，不必杀会话。",
            schema(mapOf(
                "id" to prop("string", "会话 id，如 s1"),
                "text" to prop("string", "要写入的内容"),
                "enter" to prop("boolean", "是否补回车，默认 true"),
            ), listOf("id", "text")),
            sideEffect = true,
        ),
        McpTool(
            "self_tty_read", "取会话增量输出；since 传上一轮的 cursor，只回新内容。",
            schema(mapOf(
                "id" to prop("string", "会话 id"),
                "since" to prop("integer", "上一轮返回的 cursor，首次传 0"),
                "max_chars" to prop("integer", "本次最多回多少字，默认 8000，上限 32000"),
            ), listOf("id")),
        ),
        McpTool(
            "self_tty_close", "关会话：先 TERM，1 秒不退才强杀。不传 id 则关全部。",
            schema(mapOf("id" to prop("string", "会话 id，留空关全部"))),
            sideEffect = true,
        ),
        McpTool(
            "self_tty_run", "一条命令跑完就取走输出（开→写→边等边收→关），省来往四趟。",
            schema(mapOf(
                "command" to prop("string", "要执行的命令"),
                "cwd" to prop("string", "工作目录"),
                "wait_ms" to prop("integer", "最多等多久，默认 4000"),
            ), listOf("command")),
            sideEffect = true,
        ),
        McpTool(
            "self_battery_status", "电量、充电、省电模式、是否在电池优化白名单。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_audio_volume", "读媒体音量（索引 / 最大值 / 百分比）。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_audio_set_volume", "把媒体音量设成指定百分比（0–100），静默执行。",
            schema(mapOf("percent" to prop("integer", "目标音量百分比，0–100")), listOf("percent")),
            sideEffect = true,
        ),
        McpTool(
            "self_net_status", "当前网络类型、是否计费、是否已验证可上网。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_device_info", "厂商型号、Android 版本、ABI、包名、是否已长期配对。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_screen_state", "屏幕亮灭与锁屏状态（锁屏下无障碍点不到 App）。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_diag", "全通道自检：无障碍、通知、权限、电池白名单、云侧、MCP、Shizuku、本地模型。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_audit", "最近有副作用动作的审计记录（参数摘要，敏感值隐藏）。",
            schema(mapOf("limit" to prop("integer", "最多返回多少条，默认 20，上限 50")), emptyList()),
        ),
        McpTool(
            "self_perm_matrix", "权限矩阵：每条能力走哪条通道、当前是否可用、缺权时的下一步（只读，不弹授权）。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_mcp_servers", "外部 MCP 服务器列表：名称/地址/启用/工具数/最近错误（只读）。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_mcp_add", "添加一个外部 MCP 服务器（Streamable HTTP），成功后立刻拉取工具列表。",
            schema(
                mapOf(
                    "name" to prop("string", "服务名，工具前缀会用它，建议短英文"),
                    "url" to prop("string", "MCP 端点，如 http://192.168.1.10:8765/mcp"),
                    "token" to prop("string", "可选 Bearer token，不填则不带 Authorization"),
                ),
                listOf("name", "url"),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_mcp_remove", "移除一个外部 MCP 服务器及其工具。",
            schema(mapOf("name" to prop("string", "服务名")), listOf("name")),
            sideEffect = true,
        ),
        McpTool(
            "self_mcp_refresh", "重新拉取外部 MCP 的工具列表（name 为空=刷新全部）。",
            schema(mapOf("name" to prop("string", "服务名，留空刷新全部")), emptyList()),
            sideEffect = true,
        ),
        McpTool(
            "self_shizuku_status", "Shizuku 装没装 / 服务跑没跑 / 本 App 授没授权 / shell 会话状态。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_shizuku_request", "向 Shizuku 申请授权（弹 Shizuku 自己的确认框）；已授权时不重复弹。",
            schema(emptyMap()),
            sideEffect = true,
        ),
        McpTool(
            "self_shell_exec", "经 Shizuku 在 shell（uid 2000）身份下执行命令；stdout/stderr 合并，输出上限 8KB。",
            schema(
                mapOf(
                    "cmd" to prop("string", "要执行的 shell 命令"),
                    "cwd" to prop("string", "工作目录，默认当前目录"),
                    "timeout" to prop("integer", "超时毫秒，默认 15000，上限 60000"),
                ),
                listOf("cmd"),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_owner_status", "设备所有者状态：本 App 是否所有者、设备管理是否激活、是否有 Dhizuku。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_owner_provision", "给出把本 App 设为设备所有者的 adb 命令与前提（无账号）；只读，不改状态。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_owner_grant_perm", "以设备所有者身份静默授予/撤销某 App 的运行时权限。",
            schema(
                mapOf(
                    "pkg" to prop("string", "目标包名"),
                    "permission" to prop("string", "完整权限名，如 android.permission.CAMERA"),
                    "mode" to prop("string", "granted｜denied｜default"),
                ),
                listOf("pkg", "permission", "mode"),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_owner_restriction", "以设备所有者身份添加/解除用户限制（如 no_camera、no_usb_file_transfer）。",
            schema(
                mapOf(
                    "key" to prop("string", "限制键名"),
                    "enabled" to prop("boolean", "true=添加限制，false=解除"),
                ),
                listOf("key", "enabled"),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_owner_reboot", "以设备所有者身份立刻重启手机（连接会断开后自动重连）。",
            schema(emptyMap()),
            sideEffect = true,
        ),
        McpTool(
            "self_lan_status",
            "局域网 / IPv6 直连状态：服务端口、本机可入站地址（IPv6 优先，链路本地带 %网卡）、mDNS 发现的手机。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_lan_start",
            "开手机直连服务（TCP + mDNS）。回执含完整口令；同一 Wi-Fi 走 fe80，跨网走全局 IPv6，不经过中继。",
            schema(
                mapOf(
                    "port" to prop("integer", "监听端口，默认 18765，范围 1024–65535"),
                    "rotate" to prop("boolean", "true=换新口令并重启服务（旧口令立即失效）"),
                ),
                emptyList(),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_lan_stop", "停手机直连服务与 mDNS 广播；不影响云中继。",
            schema(emptyMap()),
            sideEffect = true,
        ),
        McpTool(
            "self_lan_call", "直连另一台手机并调一个 MCP 工具，回包压成文字。",
            schema(
                mapOf(
                    "host" to prop("string", "对端地址：fe80::1%wlan0 / [2408:...] / 192.168.1.7"),
                    "port" to prop("integer", "对端端口，默认 18765"),
                    "token" to prop("string", "对端 self_lan_start 回执里的完整口令"),
                    "tool" to prop("string", "MCP 工具名，如 self_status"),
                    "args" to prop("string", "工具参数 JSON，默认 {}"),
                    "timeout" to prop("integer", "超时毫秒，默认 30000"),
                ),
                listOf("host", "token", "tool"),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_lan_speed", "两台手机之间实测 TCP 有效载荷速度（MB/s 与 Mbps）。",
            schema(
                mapOf(
                    "host" to prop("string", "对端地址"),
                    "port" to prop("integer", "对端端口，默认 18765"),
                    "token" to prop("string", "对端口令"),
                    "mb" to prop("integer", "传输多少 MB，默认 8，上限 64"),
                    "down" to prop("boolean", "true=下行，默认上行"),
                ),
                listOf("host", "token"),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_peer_add", "把另一台手机加入集群名册（名字 + 地址 + 对端口令；口令只存本机）。",
            schema(
                mapOf(
                    "name" to prop("string", "对端短名，如 b-phone"),
                    "host" to prop("string", "地址或域名，支持 IPv6 / [IPv6]:端口"),
                    "port" to prop("integer", "端口，默认 18765"),
                    "token" to prop("string", "对端 self.lan.start 回执里的完整口令"),
                ),
                listOf("name", "host", "token"),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_peer_remove", "把手机移出集群名册（只删本机，不影响对端）。",
            schema(mapOf("name" to prop("string", "对端短名")), listOf("name")),
            sideEffect = true,
        ),
        McpTool(
            "self_peer_list", "集群名册：电量、充电、亮屏、能力数、最近错误。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_peer_probe", "逐台握手，刷新机型、工具表、电量与充电状态。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_peer_pick", "按 suitability 排序候选手机；只按对端有没有这个工具过滤。",
            schema(
                mapOf(
                    "tool" to prop("string", "要派给对端的 MCP 工具名"),
                    "min_battery" to prop("integer", "最低电量，默认 15"),
                ),
                listOf("tool"),
            ),
        ),
        McpTool(
            "self_peer_call",
            "把任务派给集群：读操作失败可换机重试；写操作只派一台，失败不自动重试。",
            schema(
                mapOf(
                    "tool" to prop("string", "MCP 工具名"),
                    "args" to prop("string", "工具参数 JSON，默认 {}"),
                    "name" to prop("string", "指定某一台；不填自动选路"),
                    "timeout" to prop("integer", "超时毫秒，默认 30000"),
                    "min_battery" to prop("integer", "自动选路最低电量，默认 15"),
                ),
                listOf("tool"),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_agent_goal",
            "把一句自然语言目标整单交给本机本地模型：它自己选工具、执行、回最终答复。执行期间独占模型。",
            schema(
                mapOf(
                    "goal" to prop("string", "要完成的目标"),
                    "max_steps" to prop("integer", "最多连续调几次工具，默认 3，上限 6"),
                ),
                listOf("goal"),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_peer_goal",
            "把一句自然语言目标整单派给集群里最合适的手机，由它的本地模型跑完只回结果。",
            schema(
                mapOf(
                    "goal" to prop("string", "要完成的目标"),
                    "name" to prop("string", "指定某一台；不填自动选路"),
                    "max_steps" to prop("integer", "对端最多连续调几次工具，默认 3，上限 6"),
                    "timeout" to prop("integer", "超时毫秒，默认 300000，上限 600000"),
                    "min_battery" to prop("integer", "自动选路最低电量，默认 15"),
                ),
                listOf("goal"),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_model_ask",
            "用本机本地模型单轮回答一句话（只读；不调工具；执行期间独占模型）。",
            schema(
                mapOf(
                    "prompt" to prop("string", "要问的话"),
                    "max_tokens" to prop("integer", "最多生成多少 token，默认 256，上限 2048"),
                ),
                listOf("prompt"),
            ),
        ),
        McpTool(
            "self_api_ask",
            "用电脑/API 算力回答一句话：问题交给电脑，由电脑按自己的算力注册表" +
                "选本机或云端端点；手机不持有任何厂商 key。",
            schema(
                mapOf(
                    "prompt" to prop("string", "要问的话"),
                    "max_tokens" to prop("integer", "最多生成多少 token，默认 256，上限 4096"),
                    "purpose" to prop("string", "用途：chat（默认）/ decide / vision / voice"),
                    "endpoint" to prop("string", "钉死电脑侧的算力端点 id；留空=按用途自动选"),
                ),
                listOf("prompt"),
            ),
        ),
        McpTool(
            "self_peer_pool",
            "双机并行算力：本机与对端同时用各自的本地模型回答同一句话；" +
                "电脑/API 也会同时参与（pc=true 且已配对时），" +
                "第一波全失败或本机上下文/显存不够时，自动升级到名册里其余手机。只问不写。",
            schema(
                mapOf(
                    "prompt" to prop("string", "要问的话"),
                    "mode" to prop("string", "race=谁先回用谁（默认），all=所有答案都列出"),
                    "local" to prop("boolean", "本机是否参与，默认 true"),
                    "pc" to prop("boolean", "电脑/API 是否参与，默认 true（没配对则自动跳过）"),
                    "api" to prop("string", "额外钉死电脑侧 API 端点 id，逗号分隔，如 deepseek,zhipu"),
                    "purpose" to prop("string", "电脑算力用途，默认 chat"),
                    "max_peers" to prop("integer", "第一波最多叫几台对端，默认 1，上限 4"),
                    "timeout" to prop("integer", "总超时毫秒，默认 120000，上限 600000"),
                    "max_tokens" to prop("integer", "每台最多生成多少 token，默认 256，上限 2048"),
                    "min_battery" to prop("integer", "自动选路最低电量，默认 15"),
                    "escalate" to prop("boolean", "第一波没结果时是否升级到其余手机，默认 true"),
                ),
                listOf("prompt"),
            ),
        ),
        McpTool(
            "self_dns_resolve", "把域名解析成地址并标出类型，顺序就是直连时优先尝试的顺序。",
            schema(mapOf("name" to prop("string", "域名或 IP 字面量")), listOf("name")),
        ),
        McpTool(
            "self_ddns_status", "DDNS 配置、当前全局 IPv6、上次发布结果（不回显密钥）。",
            schema(emptyMap()),
        ),
        McpTool(
            "self_ddns_config",
            "把动态 IPv6 绑到稳定域名：duckdns 填子域 + token；custom 填含 {ipv6} 的更新 URL。",
            schema(
                mapOf(
                    "provider" to prop("string", "duckdns｜custom｜off"),
                    "domain" to prop("string", "DuckDNS 子域"),
                    "token" to prop("string", "更新密钥；留空沿用已存的"),
                    "url" to prop("string", "custom 更新 URL 模板，必须含 {ipv6}"),
                    "enabled" to prop("boolean", "配置后是否自动发布，默认 true"),
                ),
                listOf("provider"),
            ),
            sideEffect = true,
        ),
        McpTool(
            "self_ddns_publish", "立刻把当前全局 IPv6 发布到已配置的域名。",
            schema(emptyMap()),
            sideEffect = true,
        ),
        McpTool(
            "self_ddns_watch", "开/关自动发布：网络或 IPv6 变化时更新，另有 10 分钟兜底。",
            schema(mapOf("enabled" to prop("boolean", "true=开，false=关")), listOf("enabled")),
            sideEffect = true,
        ),
        // 网盘聚合：本机作为对等节点对外提供能力，别的设备可 self_lan_call / self_peer_call 调用。
        McpTool(
            "cloud_nodes", "列出本机保存的网盘节点连接，并自动发现局域网里可达的其它节点。",
            schema(emptyMap()),
        ),
        McpTool(
            "cloud_list", "列出某个网盘节点目录下的文件与文件夹（默认第一个连接、根目录）。",
            schema(
                mapOf(
                    "index" to prop("integer", "用第几个连接，从 0 开始，默认 0"),
                    "host" to prop("string", "或按节点地址指定，可临时用 user/pass 直连"),
                    "user" to prop("string", "host 直连时的账号"),
                    "pass" to prop("string", "host 直连时的密码（不回显、不落盘）"),
                    "path" to prop("string", "目录路径，默认 /"),
                ),
            ),
        ),
        McpTool(
            "cloud_search", "在网盘节点里按文件名搜索；节点没建索引时自动遍历目录树。",
            schema(
                mapOf(
                    "query" to prop("string", "文件名关键词"),
                    "index" to prop("integer", "用第几个连接，默认 0"),
                    "host" to prop("string", "或按节点地址指定"),
                    "user" to prop("string", "host 直连时的账号"),
                    "pass" to prop("string", "host 直连时的密码"),
                ),
                listOf("query"),
            ),
        ),
        McpTool(
            "cloud_file_url", "取网盘里某个文件的下载直链（不下载，只返回地址）。",
            schema(
                mapOf(
                    "path" to prop("string", "文件完整路径"),
                    "index" to prop("integer", "用第几个连接，默认 0"),
                    "host" to prop("string", "或按节点地址指定"),
                    "user" to prop("string", "host 直连时的账号"),
                    "pass" to prop("string", "host 直连时的密码"),
                ),
                listOf("path"),
            ),
        ),
    ) + WebTool.tools

    private val core = McpCore(
        tools = { toolList() },
        actionsAllowed = { SafeMode.allowsActions() },
        call = { name, args -> runBlocking { executeTool(name, args) } },
    )

    /** 一行原始 JSON → 一行回包；通知返回 null。传输层直接写回。 */
    fun handleLine(line: String): String? = core.handleLine(line)

    /**
     * 局域网直连的权限标准：**口令即授权**。
     * 执行方只判断「自己有没有这个工具」，不叠加平时的只读开关；
     * 否则同一个项目里会同时存在「控制方」和「执行方」两套标准。
     * 唯一例外是安全模式总闸 —— 那是本机主人按下的应急停止，不是第二套权限体系。
     */
    private val lanCore = McpCore(
        tools = { toolList() },
        actionsAllowed = { SafeMode.allowsLanActions() },
        call = { name, args -> runBlocking { executeTool(name, args) } },
    )

    fun handleLanLine(line: String): String? = lanCore.handleLine(line)

    fun toolCount(): Int = toolList().size

    /**
     * 本机工具表 = 静态能力 + 外部 MCP 联邦出来的工具。
     * MCP 入网、LAN 和本地模型工具协议共用同一份，避免三边名字漂移。
     */
    fun toolList(): List<McpTool> = tools + McpServers.cachedTools()

    /** 直接执行一次工具（本地模型工具闭环用）；失败不抛异常，统一走 ToolResult。 */
    suspend fun executeTool(mcpName: String, args: JSONObject): ToolResult {
        val startedAt = System.currentTimeMillis()
        // 外部 MCP 联邦工具：名字形如 mcp__<server>__<tool>，原样转给对应服务。
        if (mcpName.startsWith("mcp__")) {
            return audited(mcpName, args, startedAt, McpServers.call(mcpName, args))
        }
        // 外部 MCP 管理面：控制面可以远程给手机挂新能力。
        when (mcpName) {
            "self_mcp_servers" -> return ToolResult(true, text = McpServers.describe())
            "self_mcp_add" -> return audited(mcpName, args, startedAt,
                McpServers.add(args.optString("name"), args.optString("url"), args.optString("token")))
            "self_mcp_remove" -> return audited(mcpName, args, startedAt,
                McpServers.remove(args.optString("name")))
            "self_mcp_refresh" -> return audited(mcpName, args, startedAt,
                ToolResult(true, text = McpServers.refresh(args.optString("name").takeIf { it.isNotBlank() })))
        }
        // 网盘能力不走 PhoneSelf 的无障碍分派：它不依赖界面，也不该被无障碍开关挡住。
        if (mcpName.startsWith("cloud_")) return cloudTool(mcpName, args)
        // 联网同一个道理：能不能上网和「无障碍通没通」是两件事。
        if (mcpName.startsWith("web_")) return WebTool.run(mcpName, args)
        // 代码运行时：与界面无关，不走 PhoneSelf 的无障碍分派；
        // 局域网对端（电脑 / 另一台手机）可直接让这台手机写代码、跑脚本、跑任务。
        if (mcpName.startsWith("self_code_")) return CodeMcp.run(mcpName, args)
        // 会话层：同样跟界面无关。stdin 可写、输出流式、长驻，
        // 是 apk add / 编译 / 长任务能在手机上真跑起来的前提。
        if (mcpName.startsWith("self_tty_")) return TtyMcp.run(mcpName, args)
        val selfName = when (mcpName) {
            "self_status" -> "self.status"
            "self_ui_dump" -> "self.ui.dump"
            "self_ui_find" -> "self.ui.find"
            "self_ui_tap" -> "self.ui.tap"
            "self_ui_tap_at" -> "self.ui.tap_at"
            "self_ui_swipe" -> "self.ui.swipe"
            "self_ui_text" -> "self.ui.text"
            "self_ui_scroll" -> "self.ui.scroll"
            "self_ui_key" -> "self.ui.key"
            "self_ui_wait" -> "self.ui.wait"
            "self_app_launch" -> "self.app.launch"
            "self_app_open_url" -> "self.app.open_url"
            "self_app_list" -> "self.app.list"
            "self_ui_screenshot" -> "self.ui.screenshot"
            "self_notif_list" -> "self.notif.list"
            "self_notif_clear" -> "self.notif.clear"
            "self_clipboard_get" -> "self.clipboard.get"
            "self_clipboard_set" -> "self.clipboard.set"
            "self_file_read" -> "self.file.read"
            "self_file_write" -> "self.file.write"
            "self_battery_status" -> "self.battery.status"
            "self_audio_volume" -> "self.audio.volume"
            "self_audio_set_volume" -> "self.audio.set_volume"
            "self_net_status" -> "self.net.status"
            "self_device_info" -> "self.device.info"
            "self_screen_state" -> "self.screen.state"
            "self_diag" -> "self.diag"
            "self_audit" -> "self.audit"
            "self_lan_status" -> "self.lan.status"
            "self_lan_start" -> "self.lan.start"
            "self_lan_stop" -> "self.lan.stop"
            "self_lan_call" -> "self.lan.call"
            "self_lan_speed" -> "self.lan.speed"
            "self_peer_add" -> "self.peer.add"
            "self_peer_remove" -> "self.peer.remove"
            "self_peer_list" -> "self.peer.list"
            "self_peer_probe" -> "self.peer.probe"
            "self_peer_pick" -> "self.peer.pick"
            "self_peer_call" -> "self.peer.call"
            "self_agent_goal" -> "self.agent.goal"
            "self_peer_goal" -> "self.peer.goal"
            "self_model_ask" -> "self.model.ask"
            "self_api_ask" -> "self.api.ask"
            "self_peer_pool" -> "self.peer.pool"
            "self_dns_resolve" -> "self.dns.resolve"
            "self_ddns_status" -> "self.ddns.status"
            "self_ddns_config" -> "self.ddns.config"
            "self_ddns_publish" -> "self.ddns.publish"
            "self_ddns_watch" -> "self.ddns.watch"
            "self_perm_matrix" -> "self.perm.matrix"
            "self_shizuku_status" -> "self.shizuku.status"
            "self_shizuku_request" -> "self.shizuku.request"
            "self_shell_exec" -> "self.shell.exec"
            "self_owner_status" -> "self.owner.status"
            "self_owner_provision" -> "self.owner.provision"
            "self_owner_grant_perm" -> "self.owner.grant_perm"
            "self_owner_restriction" -> "self.owner.restriction"
            "self_owner_reboot" -> "self.owner.reboot"
            else -> return ToolResult(false, err = "未知工具：$mcpName")
        }
        val map = LinkedHashMap<String, Any?>()
        val keys = args.keys()
        while (keys.hasNext()) {
            val k = keys.next()
            map[k] = if (args.isNull(k)) null else args.get(k)
        }
        // 远端看不到手机文件系统，截屏必须把图片本体塞进 MCP 回包。
        if (mcpName == "self_ui_screenshot") map["inline"] = true
        val result = runBlocking { PhoneSelf.run(selfName, map) }
        // 有副作用的工具才进审计；读界面/读电量这种不刷屏。
        if (toolList().firstOrNull { it.name == mcpName }?.sideEffect == true) {
            ActionLog.add(mcpName, args, result.ok, result.err, System.currentTimeMillis() - startedAt)
        }
        return result
    }

    /** 外部工具与管理面不进 PhoneSelf 分派，审计要在这里补一刀。 */
    private fun audited(name: String, args: JSONObject, startedAt: Long, r: ToolResult): ToolResult {
        ActionLog.add(name, args, r.ok, r.err, System.currentTimeMillis() - startedAt)
        return r
    }

    /**
     * 网盘聚合能力：这台手机作为对等节点，把「我能看到的网盘」开放给编队里其它设备。
     * 连接地址/凭据只从本机配置读取（或调用方临时直给），不回显密码、不写日志。
     */
    private suspend fun cloudTool(name: String, args: JSONObject): ToolResult {
        val conns = CloudDrivePrefs.load()
        if (name == "cloud_nodes") {
            val found = runCatching { CloudDriveNodes.discover() }.getOrDefault(emptyList())
            return ToolResult(true, text = buildString {
                append("本机保存的节点连接（${conns.size}）：\n")
                if (conns.isEmpty()) append("  （暂无，可在工具箱「网盘聚合」里添加）\n")
                conns.forEachIndexed { i, c ->
                    append("  [$i] ${c.name} · ${c.type} · ${c.host}\n")
                }
                append("可达节点（${found.size}）：\n")
                if (found.isEmpty()) append("  （没发现，节点地址可手动填）\n")
                found.forEach { append("  ${it.name} · ${it.host}\n") }
            })
        }
        val host = args.optString("host").orEmpty()
        val idx = if (args.has("index")) args.optInt("index", 0) else 0
        val c = if (host.isNotBlank()) {
            conns.firstOrNull { it.host.trimEnd('/') == host.trimEnd('/') }
                ?: CloudConn(
                    type = if (host.contains("webdav", true)) "webdav" else "openlist",
                    name = host, host = host,
                    user = args.optString("user"), pass = args.optString("pass"),
                )
        } else {
            conns.getOrNull(idx)
        } ?: return ToolResult(false, err = "本机没有第 $idx 个节点连接，先添加或传 host")
        return try {
            when (name) {
                "cloud_list" -> {
                    val p = args.optString("path").ifBlank { "/" }
                    val list = CloudDriveApi.list(c, p)
                    ToolResult(true, text = buildString {
                        append("节点 ${c.name} 目录 $p（${list.size} 项）：\n")
                        list.forEach {
                            append(if (it.isDir) "  [目录] " else "  [文件] ")
                                .append(it.name).append(" · ").append(it.path).append('\n')
                        }
                    })
                }
                "cloud_search" -> {
                    val q = args.optString("query")
                    if (q.isBlank()) return ToolResult(false, err = "缺 query")
                    val list = CloudDriveApi.search(c, q)
                    ToolResult(true, text = buildString {
                        append("节点 ${c.name} 搜「$q」（${list.size} 项）：\n")
                        list.forEach { append("  ").append(it.path).append('\n') }
                    })
                }
                "cloud_file_url" -> {
                    val p = args.optString("path")
                    if (p.isBlank()) return ToolResult(false, err = "缺 path")
                    ToolResult(true, text = CloudDriveApi.fileUrl(c, p))
                }
                else -> ToolResult(false, err = "未知工具：$name")
            }
        } catch (t: Throwable) {
            ToolResult(false, err = t.message ?: t.javaClass.simpleName)
        }
    }

    private fun prop(type: String, desc: String): JSONObject =
        JSONObject().put("type", type).put("description", desc)

    private fun schema(
        props: Map<String, JSONObject>,
        required: List<String> = emptyList(),
    ): JSONObject {
        val p = JSONObject()
        props.forEach { (k, v) -> p.put(k, v) }
        return JSONObject()
            .put("type", "object")
            .put("properties", p)
            .put("required", JSONArray(required))
            .put("additionalProperties", false)
    }
}

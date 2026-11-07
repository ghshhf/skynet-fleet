package app.skynet.tianwang.next

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Rect
import android.media.AudioManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.util.Base64
import android.view.accessibility.AccessibilityManager
import android.view.accessibility.AccessibilityNodeInfo
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

/** 一条手机本机能力。字段与控制面能力表同构，方便以后直接映射成 `phone.self.*`。 */
data class SelfCap(
    val name: String,
    val title: String,
    val desc: String,
    val params: List<CapParam> = emptyList(),
)

/**
 * 手机自持能力面：不连电脑也能查界面、找控件、点击、输入、滚动、开 App、截屏。
 *
 * 🔴 通道没开时**每条都显式报「没开」**，不静默失败、不退回假数据。
 * 这与文档里的「权限降级要显式，不许重试」是同一条纪律。
 */
object PhoneSelf {
    val caps: List<SelfCap> = listOf(
        SelfCap(
            "self.status", "通道与当前界面",
            "查看无障碍通道是否在线、当前前台是哪个 App、屏幕分辨率。先跑这条，能排查一半「点了没反应」。",
        ),
        SelfCap(
            "self.ui.dump", "读当前界面（紧凑树）",
            "把当前屏幕的控件树压成紧凑 JSON：文字、描述、id、坐标、可点/可滚/可输入。不截屏，不烧视觉 token。",
            listOf(CapParam("limit", "int", "最多返回多少个控件，默认 120", false)),
        ),
        SelfCap(
            "self.ui.find", "按文字找控件",
            "在当前界面里按文字 / 内容描述 / 控件 id 查找，返回匹配项与坐标。",
            listOf(
                CapParam("query", "str", "要匹配的文字、描述或控件 id 的一部分", true),
                CapParam("regex", "bool", "把 query 当正则解释，默认 false", false),
            ),
        ),
        SelfCap(
            "self.ui.tap", "按文字点控件",
            "找到匹配的控件并点击；控件本身不可点时自动向上找可点的父容器，仍不行则按坐标点。",
            listOf(
                CapParam("query", "str", "要点的控件文字 / 描述 / id", true),
                CapParam("regex", "bool", "query 当正则解释", false),
                CapParam("index", "int", "匹配到多个时点第几个，从 0 开始", false),
                CapParam("long", "bool", "长按，默认 false", false),
            ),
        ),
        SelfCap(
            "self.ui.tap_at", "按坐标点屏幕",
            "直接点 (x,y)。给 WebView / 自绘界面用：这类界面读不到可点控件，但 dump 或截图能拿到坐标。",
            listOf(
                CapParam("x", "int", "屏幕横坐标（像素）", true),
                CapParam("y", "int", "屏幕纵坐标（像素）", true),
                CapParam("long", "bool", "长按，默认 false", false),
            ),
        ),
        SelfCap(
            "self.ui.swipe", "按坐标滑动手势",
            "从 (x1,y1) 滑到 (x2,y2)，可调持续时间。翻页、拖拽、画轨迹都用这条。",
            listOf(
                CapParam("x1", "int", "起点横坐标", true),
                CapParam("y1", "int", "起点纵坐标", true),
                CapParam("x2", "int", "终点横坐标", true),
                CapParam("y2", "int", "终点纵坐标", true),
                CapParam("duration", "int", "滑动毫秒数，默认 400，范围 100–3000", false),
            ),
        ),
        SelfCap(
            "self.ui.text", "向输入框写字",
            "把文字写进当前焦点输入框；给了 query 就写进匹配到的那个输入框。",
            listOf(
                CapParam("text", "str", "要写入的文字", true),
                CapParam("query", "str", "可选：定位输入框的文字 / 描述 / id", false),
            ),
        ),
        SelfCap(
            "self.ui.scroll", "滚动界面",
            "向前或向后滚一屏；没有可滚控件时退化成滑动屏幕。",
            listOf(
                CapParam("direction", "str", "down=向后滚（看下面），up=向前滚（看上面）", true),
                CapParam("query", "str", "可选：定位某个可滚容器", false),
            ),
        ),
        SelfCap(
            "self.ui.key", "系统导航键",
            "发 back / home / recents / notifications 四个全局动作。",
            listOf(CapParam("key", "str", "back｜home｜recents｜notifications", true)),
        ),
        SelfCap(
            "self.ui.wait", "等界面出现文字",
            "轮询当前界面，直到文字 / 描述 / id 出现或超时。适合「点完等跳转」这类必须等画面更新再做下一步的场景。",
            listOf(
                CapParam("query", "str", "要等的文字、描述或控件 id 的一部分", true),
                CapParam("timeout", "int", "最多等多少毫秒，默认 5000，上限 30000", false),
                CapParam("regex", "bool", "把 query 当正则解释，默认 false", false),
            ),
        ),
        SelfCap(
            "self.app.launch", "按包名打开 App",
            "用系统启动意图打开指定包名（例：com.android.settings）。不靠 adb。",
            listOf(CapParam("pkg", "str", "包名", true)),
        ),
        SelfCap(
            "self.app.open_url", "用系统浏览器打开链接",
            "只接受 http/https，交给系统处理（浏览器 / 已注册的 App Link）。不是网页抓取，抓取走云侧工具。",
            listOf(CapParam("url", "str", "要打开的 http/https 链接", true)),
        ),
        SelfCap(
            "self.app.list", "列出可打开的 App",
            "列出桌面有启动入口的应用（名称 + 包名），可按名称/包名过滤。给「打开 XX」找包名用。",
            listOf(
                CapParam("query", "str", "可选：按名称或包名过滤", false),
                CapParam("limit", "int", "最多返回多少个，默认 40，上限 200", false),
            ),
        ),
        SelfCap(
            "self.ui.screenshot", "截屏存本机 / 回传图片",
            "系统无障碍截图（Android 11+），PNG 落在 App 私有目录。给 inline=true 时再把压缩后的 JPEG 直接回给调用方；远端 MCP 默认走 inline。",
            listOf(
                CapParam("inline", "bool", "把图片内容直接回给调用方（JPEG），默认 false 只存文件", false),
                CapParam("max", "int", "inline 时最长边像素，默认 1280，范围 320–2048", false),
            ),
        ),
        SelfCap(
            "self.notif.list", "读最近通知",
            "读通知监听拿到的最近通知（发件 App、标题、正文）。需先在系统设置授予通知使用权；只保留本机内存 40 条。",
            listOf(CapParam("limit", "int", "最多返回多少条，默认 20", false)),
        ),
        SelfCap(
            "self.notif.clear", "清空通知缓存",
            "清空本机内存里的通知缓存；不会删除系统通知栏里的通知。",
        ),
        SelfCap(
            "self.clipboard.get", "读剪贴板",
            "读当前剪贴板文字。Android 10+ 在后台 App 上可能被系统拒绝返回内容。",
        ),
        SelfCap(
            "self.clipboard.set", "写剪贴板",
            "把文字放进系统剪贴板；Android 13+ 系统会弹一次「已复制」提示。",
            listOf(CapParam("text", "str", "要写入的文字", true)),
        ),
        SelfCap(
            "self.battery.status", "电量与保活状态",
            "电量、是否充电、是否已在电池优化白名单里 —— 长任务被厂商杀后台时先看这条。",
        ),
        SelfCap(
            "self.audio.volume", "读媒体音量",
            "读当前媒体音量（索引 / 最大值 / 百分比）。",
        ),
        SelfCap(
            "self.audio.set_volume", "调媒体音量",
            "把媒体音量设成指定百分比（0–100）。静默执行，不弹系统音量条。",
            listOf(CapParam("percent", "int", "目标音量百分比，0–100", true)),
        ),
        SelfCap(
            "self.net.status", "网络状态",
            "当前有没有网、走 Wi-Fi/蜂窝/以太网/VPN、是否计费网络、是否已验证可上网。",
        ),
        SelfCap(
            "self.audit", "最近动作审计",
            "看远程最近点了什么、成没成（只记有副作用的工具，参数里的正文只留字数，密钥隐藏；进程重启清空）。",
            listOf(CapParam("limit", "int", "最多返回多少条，默认 20，上限 50", false)),
        ),
        SelfCap(
            "self.screen.state", "屏幕与锁屏状态",
            "屏幕亮没亮、设备锁没锁。准备点界面前先看这条：锁屏下无障碍点不到任何 App。",
        ),
        SelfCap(
            "self.diag", "全通道自检",
            "一条命令看齐所有开关：无障碍、通知监听、通知/录音权限、电池白名单、云侧配置与配对、MCP 入网、Shizuku、本地模型数量。真机验收先跑这条。",
        ),
        SelfCap(
            "self.device.info", "设备档案",
            "厂商型号、Android 版本、ABI、App 包名、是否已做长期设备配对（不暴露 secret）。",
        ),
        SelfCap(
            "self.perm.matrix", "权限矩阵（通道降级）",
            "逐条列出读界面 / 点击 / 截屏 / 读通知 / shell / 装应用等能力走哪条通道、当前可不可用、缺权时下一步做什么。不重试、不假装成功。",
        ),
        SelfCap(
            "self.shizuku.status", "Shizuku 通道状态",
            "Shizuku 装没装、服务跑没跑、本 App 授没授权、shell 会话连没连。想用 shell 先跑这条。",
        ),
        SelfCap(
            "self.shizuku.request", "申请 Shizuku 授权",
            "向 Shizuku 发起授权请求（会弹 Shizuku 自己的确认框）。已授权时不会重复弹。",
        ),
        SelfCap(
            "self.shell.exec", "跨 App shell（uid 2000）",
            "经 Shizuku 在 shell 身份下执行 `sh -c` 命令，stdout/stderr 合并返回，超时默认 15 秒、输出上限 8KB。没授权时直接报原因。",
            listOf(
                CapParam("cmd", "str", "要执行的 shell 命令", true),
                CapParam("cwd", "str", "工作目录，默认当前目录", false),
                CapParam("timeout", "int", "超时毫秒，默认 15000，上限 60000", false),
            ),
        ),
        SelfCap(
            "self.owner.status", "设备所有者状态（第二层假 root）",
            "本 App 是不是设备所有者、有没有激活设备管理、设备上有没有 Dhizuku。全新手机无需 root / 解锁即可 provision。",
        ),
        SelfCap(
            "self.owner.provision", "如何成为设备所有者",
            "给出把本 App 设为设备所有者的那条 adb 命令与前提（无账号）。不改任何状态，只回步骤。",
        ),
        SelfCap(
            "self.owner.grant_perm", "静默授予 / 撤销权限",
            "以设备所有者身份直接改某个 App 的运行时权限，不弹系统框。目标 App 必须已安装且该权限是运行时权限。",
            listOf(
                CapParam("pkg", "str", "目标包名", true),
                CapParam("permission", "str", "完整权限名，如 android.permission.CAMERA", true),
                CapParam("mode", "str", "granted｜denied｜default", true),
            ),
        ),
        SelfCap(
            "self.owner.restriction", "添加 / 解除系统限制",
            "以设备所有者身份开关用户限制，如 no_camera、no_usb_file_transfer、no_screen_capture。",
            listOf(
                CapParam("key", "str", "限制键名", true),
                CapParam("enabled", "bool", "true=添加限制，false=解除", true),
            ),
        ),
        SelfCap(
            "self.owner.reboot", "远程重启设备",
            "设备所有者专用重启。手机立刻重启，远程连接会断，重启后自动重连。",
        ),
        SelfCap(
            "self.lan.status", "局域网 / IPv6 直连状态",
            "看直连服务开没开、端口、本机所有可入站地址（IPv6 优先，链路本地带 %网卡）、mDNS 发现的手机。排「连不上」先跑这条。",
        ),
        SelfCap(
            "self.lan.start", "开手机直连服务",
            "在本机开 TCP 服务并广播 mDNS。回执里有完整口令和对端可填的地址；同一 Wi-Fi 走 fe80 链路本地，跨网络走全局 IPv6，流量不经过中继。口令即授权，rotate=true 会立刻换掉旧口令。",
            listOf(
                CapParam("port", "int", "监听端口，默认 18765，范围 1024–65535", false),
                CapParam("rotate", "bool", "true=换新口令并重启服务（旧口令立即失效）", false),
            ),
        ),
        SelfCap(
            "self.lan.stop", "关手机直连服务",
            "停掉 TCP 服务与 mDNS 广播；不影响云中继（如果还开着）。",
        ),
        SelfCap(
            "self.lan.call", "直连调另一台手机的工具",
            "连另一台手机的 LAN 服务并调一个 MCP 工具，回包压成文字。走 Wi-Fi/IPv6 直连，比云中继少一跳。",
            listOf(
                CapParam("host", "str", "对端地址：fe80::1%wlan0 / [2408:...] / 192.168.1.7", true),
                CapParam("port", "int", "对端端口，默认 18765", false),
                CapParam("token", "str", "对端 self.lan.start 回执里的完整口令", true),
                CapParam("tool", "str", "MCP 工具名，如 self_status / self_ui_dump", true),
                CapParam("args", "str", "工具参数 JSON，如 {\"limit\":20}；默认 {}", false),
                CapParam("timeout", "int", "超时毫秒，默认 30000，上限 120000", false),
            ),
        ),
        SelfCap(
            "self.lan.speed", "手机直连测速",
            "在两台手机之间灌数据，实测 TCP 有效载荷速度（MB/s 与 Mbps）。Wi-Fi 6 跑几百 Mbps 是正常的，别拿中继的体感当上限。",
            listOf(
                CapParam("host", "str", "对端地址", true),
                CapParam("port", "int", "对端端口，默认 18765", false),
                CapParam("token", "str", "对端口令", true),
                CapParam("mb", "int", "传输多少 MB，默认 8，上限 64", false),
                CapParam("down", "bool", "true=测下行，默认测上行", false),
            ),
        ),
        SelfCap(
            "self.peer.add", "把另一台手机加入集群",
            "登记名字、地址、对端口令。名字是以后派单用的短名；口令只存本机、列表里只露头尾。",
            listOf(
                CapParam("name", "str", "对端短名，如 b-phone", true),
                CapParam("host", "str", "地址或域名，支持 IPv6 / [IPv6]:端口 / 域名", true),
                CapParam("port", "int", "端口，默认 18765", false),
                CapParam("token", "str", "对端 self.lan.start 回执里的完整口令", true),
            ),
        ),
        SelfCap(
            "self.peer.remove", "把手机移出集群",
            "只删本机名册，不影响对端。",
            listOf(CapParam("name", "str", "对端短名", true)),
        ),
        SelfCap(
            "self.peer.list", "集群名册",
            "看每台的电量、充电、亮屏、能力数、最近一次错误；也会列出 mDNS 发现但还没入册的手机。写操作能不能派，只看控制方这一档。",
        ),
        SelfCap(
            "self.peer.probe", "探测集群里每台手机",
            "逐台握手，拿回机型、Android 版本、工具表、电量与充电状态。派单前先跑这条。",
        ),
        SelfCap(
            "self.peer.pick", "选最合适的一台",
            "按「充电中 > 电量高 > 最近见到」排序，只按对端有没有这个工具过滤。只给建议，不执行。",
            listOf(
                CapParam("tool", "str", "要派给对端的 MCP 工具名，如 self_ui_tap", true),
                CapParam("min_battery", "int", "电量低于此值且没充电的不派，默认 15", false),
            ),
        ),
        SelfCap(
            "self.peer.call", "把任务派给集群",
            "按选路结果逐台尝试，成功即返回是哪台做的。写操作只派一台，失败不换机重试（避免重复点击/重复发送）。写操作能否派出，只看控制方这一档权限。",
            listOf(
                CapParam("tool", "str", "MCP 工具名", true),
                CapParam("args", "str", "工具参数 JSON，默认 {}", false),
                CapParam("name", "str", "指定某一台，不填则自动选路", false),
                CapParam("timeout", "int", "超时毫秒，默认 30000，上限 600000", false),
                CapParam("min_battery", "int", "自动选路时的最低电量，默认 15", false),
            ),
        ),
        SelfCap(
            "self.agent.goal", "把一句话目标整单交给本机模型",
            "本机本地模型自己选工具、执行、回最终答复；适合只给目标、不想逐步指挥。执行期间独占模型；不会停 LAN、换口令、重启或再派整单目标。",
            listOf(
                CapParam("goal", "str", "要完成的目标，一句话说清", true),
                CapParam("max_steps", "int", "最多连续调几次工具，默认 3，上限 6", false),
            ),
        ),
        SelfCap(
            "self.peer.goal", "把一句话目标派给集群",
            "选一台最合适的手机，把整单目标交给它的本地模型跑完，只回结果。默认超时 5 分钟；写操作能否派出仍只看控制方这一档。",
            listOf(
                CapParam("goal", "str", "要完成的目标，一句话说清", true),
                CapParam("name", "str", "指定某一台，不填则自动选路", false),
                CapParam("max_steps", "int", "对端最多连续调几次工具，默认 3，上限 6", false),
                CapParam("timeout", "int", "超时毫秒，默认 300000，上限 600000", false),
                CapParam("min_battery", "int", "自动选路时的最低电量，默认 15", false),
            ),
        ),
        SelfCap(
            "self.dns.resolve", "用名字查当前地址",
            "把域名解析成地址并标出类型（全局 IPv6 / ULA / 私网 IPv4…），顺序就是直连时优先尝试的顺序。动态 IPv6 换地址后，用名字不用改配置。",
            listOf(CapParam("name", "str", "域名，如 myphone.duckdns.org；也接受 IP 字面量", true)),
        ),
        SelfCap(
            "self.ddns.status", "动态 IPv6 名字状态",
            "看 DDNS 配置、当前全局 IPv6、上次发布结果。不显示密钥。",
        ),
        SelfCap(
            "self.ddns.config", "配置动态 IPv6 名字",
            "把会自动变化的全局 IPv6 绑定到一个稳定域名：duckdns 填子域 + token；custom 填含 {ipv6}（可选 {token}）的更新 URL。密钥只存本机、不回显。",
            listOf(
                CapParam("provider", "str", "duckdns｜custom｜off", true),
                CapParam("domain", "str", "DuckDNS 子域，如 myphone 或 myphone.duckdns.org", false),
                CapParam("token", "str", "更新密钥；留空表示沿用已存的", false),
                CapParam("url", "str", "custom 的更新 URL 模板，必须含 {ipv6}", false),
                CapParam("enabled", "bool", "配置后是否自动发布，默认 true", false),
            ),
        ),
        SelfCap(
            "self.ddns.publish", "立刻发布当前 IPv6",
            "把当前全局 IPv6 发到已配置的域名。成功只代表 DNS 已更新，不代表运营商允许入站。",
        ),
        SelfCap(
            "self.ddns.watch", "开/关自动发布",
            "网络变化或全局 IPv6 变化时自动更新域名；每 10 分钟兜底检查一次（隐私地址会轮换）。",
            listOf(CapParam("enabled", "bool", "true=开，false=关", true)),
        ),
    )

    /** 用户在系统设置里给本 App 开了无障碍服务。 */
    fun enabled(ctx: Context): Boolean {
        val am = ctx.getSystemService(AccessibilityManager::class.java) ?: return false
        return runCatching {
            am.getEnabledAccessibilityServiceList(AccessibilityServiceInfo.FEEDBACK_ALL_MASK)
                .any { it.resolveInfo?.serviceInfo?.packageName == ctx.packageName }
        }.getOrDefault(false)
    }

    fun connected(): Boolean = PhoneAccessService.instance != null

    fun statusLine(ctx: Context): String = when {
        connected() -> "已连接 · 可操作"
        enabled(ctx) -> "已授权 · 等待系统连接"
        else -> "通道未开启"
    }

    fun openSettings(ctx: Context): Boolean = runCatching {
        ctx.startActivity(
            Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
        true
    }.getOrDefault(false)

    suspend fun run(name: String, args: Map<String, Any?>): ToolResult =
        withContext(Dispatchers.Main) {
            try {
                // 通知 / 剪贴板 / 电量 / 音视频 / 网络 / 应用清单都不依赖无障碍服务，
                // 先分流 —— 通道没开时它们仍可用，不该被一句「无障碍没开」一起挡掉。
                when (name) {
                    // 状态检查本身不该被「通道没开」挡住 —— 它正是用来报通道状态的。
                    "self.status" -> status(App.ctx)
                    "self.notif.list" -> notifList(args)
                    "self.notif.clear" -> {
                        NotificationWatch.clear()
                        ToolResult(true, text = "通知缓存已清空")
                    }
                    "self.clipboard.get" -> clipboardGet(App.ctx)
                    "self.clipboard.set" -> clipboardSet(App.ctx, args)
                    "self.battery.status" -> batteryStatus(App.ctx)
                    "self.audio.volume" -> volumeRead(App.ctx)
                    "self.audio.set_volume" -> volumeSet(App.ctx, args)
                    "self.net.status" -> netStatus(App.ctx)
                    "self.device.info" -> deviceInfo(App.ctx)
                    "self.diag" -> diag(App.ctx)
                    "self.perm.matrix" -> ToolResult(true, text = Perms.format(Perms.snapshot(App.ctx)))
                    "self.shizuku.status" -> shizukuStatus()
                    "self.shizuku.request" -> {
                        val st = ShizukuBridge.status()
                        if (!st.running) ToolResult(false, err = st.line())
                        else ToolResult(true, text = ShizukuBridge.requestPermission())
                    }
                    // shell 不依赖无障碍，必须放在通道闸门之前。
                    "self.shell.exec" -> ShizukuBridge.exec(
                        cmd = strArg(args, "cmd"),
                        cwd = strArg(args, "cwd"),
                        timeoutMs = intArg(args, "timeout", 15_000),
                    )
                    "self.owner.status" -> ownerStatus(App.ctx)
                    "self.owner.provision" -> ToolResult(
                        true, text = OwnerBridge.provisionHint(App.ctx),
                    )
                    "self.owner.grant_perm" -> OwnerBridge.grantPermission(
                        App.ctx,
                        pkg = strArg(args, "pkg"),
                        permission = strArg(args, "permission"),
                        mode = strArg(args, "mode"),
                    )
                    "self.owner.restriction" -> OwnerBridge.setRestriction(
                        App.ctx,
                        key = strArg(args, "key"),
                        enabled = flagArg(args, "enabled"),
                    )
                    "self.owner.reboot" -> OwnerBridge.reboot(App.ctx)
                    // 手机直连也不依赖无障碍：LAN 服务是独立 TCP 监听。
                    "self.lan.status" -> ToolResult(true, text = LanHub.statusText())
                    "self.lan.start" -> withContext(Dispatchers.IO) {
                        LanHub.start(
                            App.ctx,
                            intArg(args, "port", Prefs.lanPort),
                            flagArg(args, "rotate"),
                        )
                    }
                    "self.lan.stop" -> {
                        LanHub.stop(App.ctx)
                        ToolResult(true, text = "局域网直连已停止")
                    }
                    // 直连是阻塞 socket，必须离开主线程，否则测速会把 UI 卡死。
                    "self.lan.call" -> withContext(Dispatchers.IO) { LanHub.call(args) }
                    "self.lan.speed" -> withContext(Dispatchers.IO) { LanHub.speed(args) }
                    "self.peer.add" -> LanHub.peerAdd(args)
                    "self.peer.remove" -> LanHub.peerRemove(args)
                    "self.peer.list" -> LanHub.peerList()
                    "self.peer.probe" -> withContext(Dispatchers.IO) { LanHub.peerProbe() }
                    "self.peer.pick" -> LanHub.peerPick(args)
                    "self.peer.call" -> withContext(Dispatchers.IO) { LanHub.peerCall(args) }
                    "self.agent.goal" -> withContext(Dispatchers.Default) {
                        LocalGoal.run(strArg(args, "goal"), intArg(args, "max_steps", 3))
                    }
                    "self.peer.goal" -> withContext(Dispatchers.IO) { LanHub.peerGoal(args) }
                    // 名字解析与 DDNS 都是网络操作，必须离开主线程。
                    "self.dns.resolve" -> withContext(Dispatchers.IO) {
                        val name = strArg(args, "name")
                        if (name.isBlank()) {
                            ToolResult(false, err = "缺 name：填域名或 IP")
                        } else {
                            ToolResult(true, text = Dns6.describe(name))
                        }
                    }
                    "self.ddns.status" -> ToolResult(true, text = Ddns.statusText())
                    "self.ddns.config" -> Ddns.save(
                        provider = strArg(args, "provider"),
                        domain = strArg(args, "domain"),
                        token = strArg(args, "token"),
                        url = strArg(args, "url"),
                        enabled = if (args.containsKey("enabled")) flagArg(args, "enabled") else true,
                    )
                    "self.ddns.publish" -> withContext(Dispatchers.IO) { Ddns.publish() }
                    "self.ddns.watch" -> if (!args.containsKey("enabled")) {
                        ToolResult(false, err = "缺 enabled：true=开自动发布，false=关")
                    } else {
                        Ddns.setWatch(flagArg(args, "enabled"))
                    }
                    "self.audit" -> audit(args)
                    "self.screen.state" -> screenState(App.ctx)
                    "self.app.list" -> appList(App.ctx, args)
                    "self.app.open_url" -> openUrl(App.ctx, args)
                    else -> {
                        val s = PhoneAccessService.instance
                            ?: return@withContext ToolResult(
                                false,
                                err = "无障碍通道没开。去「手机本机」页点「去开启」，在系统设置里打开「天网 Next」的无障碍服务。",
                            )
                        when (name) {
                            "self.ui.dump" -> dump(s, args)
                            "self.ui.find" -> find(s, args)
                            "self.ui.tap" -> tap(s, args)
                            "self.ui.tap_at" -> tapAt(s, args)
                            "self.ui.swipe" -> swipe(s, args)
                            "self.ui.text" -> type(s, args)
                            "self.ui.scroll" -> scroll(s, args)
                            "self.ui.key" -> key(s, args)
                            "self.ui.wait" -> waitFor(s, args)
                            "self.app.launch" -> launch(s.applicationContext, args)
                            "self.ui.screenshot" -> screenshot(s, args)
                            else -> ToolResult(false, err = "未知的手机本机能力：$name")
                        }
                    }
                }
            } catch (t: Throwable) {
                ToolResult(false, err = "${t.javaClass.simpleName}: ${t.message ?: "执行失败"}")
            }
        }

    // ---------------------------------------------------------------- 各能力

    private fun status(ctx: Context): ToolResult {
        val s = PhoneAccessService.instance
        val dm = ctx.resources.displayMetrics
        val txt = buildString {
            append("通道：").append(statusLine(ctx)).append('\n')
            if (s != null) {
                append("当前界面：").append(s.currentPackage().ifBlank { "（读不到包名）" }).append('\n')
            }
            append("屏幕：").append(dm.widthPixels).append('×').append(dm.heightPixels)
            append(" @").append(dm.densityDpi).append("dpi\n")
            append("截图：").append(if (Build.VERSION.SDK_INT >= 30) "可用" else "不可用（需 Android 11+）")
        }
        return ToolResult(true, text = txt)
    }

    private fun dump(s: PhoneAccessService, args: Map<String, Any?>): ToolResult {
        val root = s.snapshot()
            ?: return ToolResult(false, err = "读不到当前窗口（锁屏、系统界面或无障碍服务刚断开？）")
        val limit = intArg(args, "limit", UiTree.DEFAULT_MAX_NODES).coerceIn(1, 600)
        val nodes = UiTree.compact(root, limit)
        if (nodes.isEmpty()) {
            return ToolResult(true, text = "当前界面没有可读控件（可能是 WebView/自绘界面，需要视觉兜底）。")
        }
        return ToolResult(true, text = UiTree.toJson(nodes) + "\n\n" + UiTree.format(nodes.take(40)))
    }

    private fun find(s: PhoneAccessService, args: Map<String, Any?>): ToolResult {
        val q = strArg(args, "query")
        if (q.isBlank()) return ToolResult(false, err = "query 不能为空")
        val root = s.snapshot() ?: return ToolResult(false, err = "读不到当前窗口")
        val hits = UiTree.findAll(root, q, flagArg(args, "regex"))
        if (hits.isEmpty()) return ToolResult(false, err = "当前界面没找到「$q」")
        return ToolResult(true, text = "找到 ${hits.size} 个：\n" + UiTree.format(hits.take(30)))
    }

    private fun tap(s: PhoneAccessService, args: Map<String, Any?>): ToolResult {
        val q = strArg(args, "query")
        if (q.isBlank()) return ToolResult(false, err = "query 不能为空")
        val index = intArg(args, "index", 0).coerceAtLeast(0)
        val long = flagArg(args, "long")
        val root = s.currentRoot() ?: return ToolResult(false, err = "读不到当前窗口")
        val hits = findNodes(root, q, flagArg(args, "regex"))
        if (hits.isEmpty()) return ToolResult(false, err = "当前界面没找到「$q」")
        if (index >= hits.size) {
            return ToolResult(false, err = "只找到 ${hits.size} 个匹配项，没有第 $index 个")
        }
        val node = hits[index]
        val label = node.text?.toString()?.ifBlank { node.contentDescription?.toString() }
            ?.ifBlank { node.viewIdResourceName } ?: q

        val target = clickableAncestor(node)
        val acted = target?.let {
            it.performAction(
                if (long) AccessibilityNodeInfo.ACTION_LONG_CLICK
                else AccessibilityNodeInfo.ACTION_CLICK
            )
        } ?: false
        if (acted) {
            return ToolResult(true, text = (if (long) "已长按「" else "已点击「") + label + "」（第 $index 个）")
        }

        // 控件不响应就直接按坐标点；这是最后的兜底，不是首选。
        val r = Rect()
        node.getBoundsInScreen(r)
        if (r.width() > 0 && r.height() > 0 && s.tap(r.centerX(), r.centerY(), long)) {
            return ToolResult(
                true,
                text = (if (long) "已按坐标长按「" else "已按坐标点击「") + label + "」（第 $index 个）"
            )
        }
        return ToolResult(false, err = "「$q」找到了但点不动（控件不可点，坐标也为空）")
    }

    private fun tapAt(s: PhoneAccessService, args: Map<String, Any?>): ToolResult {
        val x = intArg(args, "x", -1)
        val y = intArg(args, "y", -1)
        val dm = s.resources.displayMetrics
        if (x !in 0 until dm.widthPixels || y !in 0 until dm.heightPixels) {
            return ToolResult(
                false,
                err = "坐标 ($x,$y) 不在屏幕内（屏幕 ${dm.widthPixels}×${dm.heightPixels}）",
            )
        }
        val long = flagArg(args, "long")
        return if (s.tap(x, y, long)) {
            ToolResult(true, text = (if (long) "已长按" else "已点击") + " ($x,$y)")
        } else {
            ToolResult(false, err = "手势没被系统接受（锁屏或系统界面？）")
        }
    }

    private fun swipe(s: PhoneAccessService, args: Map<String, Any?>): ToolResult {
        val x1 = intArg(args, "x1", -1)
        val y1 = intArg(args, "y1", -1)
        val x2 = intArg(args, "x2", -1)
        val y2 = intArg(args, "y2", -1)
        val ms = intArg(args, "duration", 400).coerceIn(100, 3_000)
        val dm = s.resources.displayMetrics
        val w = dm.widthPixels
        val h = dm.heightPixels
        val inside = x1 in 0 until w && x2 in 0 until w && y1 in 0 until h && y2 in 0 until h
        if (!inside) {
            return ToolResult(false, err = "滑动坐标超出屏幕（当前 ${w}×${h}）")
        }
        return if (s.swipe(x1, y1, x2, y2, ms.toLong())) {
            ToolResult(true, text = "已滑动 ($x1,$y1)→($x2,$y2)，${ms}ms")
        } else {
            ToolResult(false, err = "滑动手势没被系统接受（锁屏或系统界面？）")
        }
    }

    private fun type(s: PhoneAccessService, args: Map<String, Any?>): ToolResult {
        val text = strArg(args, "text")
        if (text.isEmpty()) return ToolResult(false, err = "text 不能为空")
        val q = strArg(args, "query")
        val root = s.currentRoot() ?: return ToolResult(false, err = "读不到当前窗口")
        val node = if (q.isNotBlank()) {
            findNodes(root, q, false).firstNotNullOfOrNull { editableTarget(it) }
        } else {
            firstNode(root) { it.isFocused && it.isEditable } ?: firstNode(root) { it.isEditable }
        } ?: return ToolResult(false, err = if (q.isBlank()) "当前界面没有输入框" else "没找到「$q」对应的输入框")

        node.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
        val bundle = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        val ok = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, bundle)
        if (ok) return ToolResult(true, text = "已写入 ${text.length} 个字")

        // 少数 WebView / 自绘输入框不认 ACTION_SET_TEXT，却认系统粘贴。
        // 退一步走剪贴板，别直接判死；成功时如实说明「用了剪贴板」。
        val cm = App.ctx.getSystemService(ClipboardManager::class.java)
        if (cm != null) {
            cm.setPrimaryClip(ClipData.newPlainText("天网输入", text))
            node.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
            if (node.performAction(AccessibilityNodeInfo.ACTION_PASTE)) {
                return ToolResult(true, text = "输入框不认直接写入，已用剪贴板粘贴 ${text.length} 个字")
            }
        }
        return ToolResult(false, err = "输入框拒绝了写入（可能是只读控件）")
    }

    private fun scroll(s: PhoneAccessService, args: Map<String, Any?>): ToolResult {
        val dir = strArg(args, "direction").ifBlank { "down" }.lowercase()
        val q = strArg(args, "query").ifBlank { null }
        val root = s.currentRoot() ?: return ToolResult(false, err = "读不到当前窗口")
        val node =
            (if (q != null) findNodes(root, q, false).firstOrNull { it.isScrollable } else null)
                ?: firstNode(root) { it.isScrollable }

        if (node != null) {
            val action = if (dir == "up") AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
            else AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
            if (node.performAction(action)) {
                return ToolResult(true, text = "已${if (dir == "up") "向上" else "向下"}滚动一屏")
            }
        }

        // 没有可滚控件（常见于自绘界面）就滑动屏幕。
        val dm = s.resources.displayMetrics
        val x = dm.widthPixels / 2
        val ok = if (dir == "up") {
            s.swipe(x, (dm.heightPixels * 0.35f).toInt(), x, (dm.heightPixels * 0.75f).toInt())
        } else {
            s.swipe(x, (dm.heightPixels * 0.75f).toInt(), x, (dm.heightPixels * 0.25f).toInt())
        }
        return if (ok) ToolResult(true, text = "已滑动屏幕（${dir}）")
        else ToolResult(false, err = "没找到可滚控件，滑动手势也没被系统接受")
    }

    private fun key(s: PhoneAccessService, args: Map<String, Any?>): ToolResult {
        val k = strArg(args, "key").lowercase()
        val action = when (k) {
            "back" -> AccessibilityService.GLOBAL_ACTION_BACK
            "home" -> AccessibilityService.GLOBAL_ACTION_HOME
            "recents" -> AccessibilityService.GLOBAL_ACTION_RECENTS
            "notifications", "notification" -> AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS
            else -> return ToolResult(false, err = "key 只认 back / home / recents / notifications")
        }
        return if (s.global(action)) ToolResult(true, text = "已发送 $k")
        else ToolResult(false, err = "系统拒绝了 $k")
    }

    /**
     * 等界面出现目标文字。点击后画面常常要几百毫秒才换，立刻去读树会读到旧界面，
     * 于是「点了没反应」—— 这条把等待显式化，别让上层靠 sleep 猜。
     */
    private suspend fun waitFor(s: PhoneAccessService, args: Map<String, Any?>): ToolResult {
        val q = strArg(args, "query")
        if (q.isBlank()) return ToolResult(false, err = "query 不能为空")
        val timeout = intArg(args, "timeout", 5_000).coerceIn(200, 30_000)
        val regex = flagArg(args, "regex")
        val deadline = System.currentTimeMillis() + timeout
        var sawWindow = false
        while (System.currentTimeMillis() < deadline) {
            val root = s.snapshot()
            if (root != null) {
                sawWindow = true
                val hits = UiTree.findAll(root, q, regex)
                if (hits.isNotEmpty()) {
                    return ToolResult(
                        true,
                        text = "等到了「$q」（${hits.size} 个）：\n" + UiTree.format(hits.take(10)),
                    )
                }
            }
            delay(150)
        }
        return ToolResult(
            false,
            err = "等了 ${timeout}ms 没等到「$q」" + if (sawWindow) "" else "（期间一直读不到窗口）",
        )
    }

    private fun openUrl(ctx: Context, args: Map<String, Any?>): ToolResult {
        val url = strArg(args, "url").trim()
        if (url.isBlank()) return ToolResult(false, err = "url 不能为空")
        val scheme = runCatching { android.net.Uri.parse(url).scheme }.getOrNull()
            ?.lowercase()
        if (scheme != "http" && scheme != "https") {
            return ToolResult(false, err = "只允许 http/https 链接（收到 scheme=$scheme）")
        }
        val intent = Intent(Intent.ACTION_VIEW, android.net.Uri.parse(url))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return runCatching {
            ctx.startActivity(intent)
            ToolResult(true, text = "已交给系统打开：$url")
        }.getOrElse {
            ToolResult(false, err = "系统没有能打开这个链接的 App：${it.message}")
        }
    }

    private fun screenState(ctx: Context): ToolResult {
        val pm = ctx.getSystemService(PowerManager::class.java)
        val km = ctx.getSystemService(android.app.KeyguardManager::class.java)
        val interactive = pm?.isInteractive ?: true
        val locked = runCatching { km?.isKeyguardLocked ?: false }.getOrDefault(false)
        val txt = buildString {
            append("屏幕：").append(if (interactive) "亮着" else "熄灭").append('\n')
            append("锁屏：").append(if (locked) "已锁定（无障碍点不到 App）" else "未锁定")
        }
        return ToolResult(true, text = txt)
    }

    private fun launch(ctx: Context, args: Map<String, Any?>): ToolResult {
        val pkg = strArg(args, "pkg")
        if (pkg.isBlank()) return ToolResult(false, err = "pkg 不能为空")
        val intent = ctx.packageManager.getLaunchIntentForPackage(pkg)
            ?: return ToolResult(false, err = "找不到可启动的包：$pkg")
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return runCatching {
            ctx.startActivity(intent)
            ToolResult(true, text = "已打开 $pkg")
        }.getOrElse { ToolResult(false, err = "打开失败：${it.message}") }
    }

    /**
     * 桌面有启动入口的应用清单。只走 launcher intent，不申请 QUERY_ALL_PACKAGES；
     * 返回「名称 + 包名」而不是图标，避免一次几百张图把内存和响应时间打爆。
     */
    private fun appList(ctx: Context, args: Map<String, Any?>): ToolResult {
        val q = strArg(args, "query")
        val limit = intArg(args, "limit", 40).coerceIn(1, 200)
        val pm = ctx.packageManager
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val resolved = runCatching { pm.queryIntentActivities(intent, 0) }
            .getOrElse { return ToolResult(false, err = "读应用列表失败：${it.message}") }
        val rows = resolved.mapNotNull { ri ->
            val pkg = ri.activityInfo?.packageName ?: return@mapNotNull null
            val label = runCatching { ri.loadLabel(pm)?.toString() }.getOrNull()
                ?.ifBlank { pkg } ?: pkg
            if (q.isNotBlank() &&
                !label.contains(q, ignoreCase = true) &&
                !pkg.contains(q, ignoreCase = true)
            ) null else "$label\t$pkg"
        }.distinct().sortedBy { it.lowercase() }
        if (rows.isEmpty()) {
            return ToolResult(true, text = if (q.isBlank()) "没有读到可启动的应用" else "没有匹配「$q」的应用")
        }
        val shown = rows.take(limit)
        val head = "共 ${rows.size} 个可启动应用" + if (shown.size < rows.size) "（显示前 ${shown.size} 个）" else ""
        return ToolResult(true, text = head + "：\n" + shown.joinToString("\n") { "· $it" })
    }

    private suspend fun screenshot(s: PhoneAccessService, args: Map<String, Any?>): ToolResult {
        val bmp = s.screenshot().getOrElse {
            return ToolResult(false, err = it.message ?: "截图失败")
        }
        val saved = withContext(Dispatchers.IO) {
            runCatching {
                val dir = File(s.getExternalFilesDir(null), "shots").apply { mkdirs() }
                val f = File(dir, "shot-${System.currentTimeMillis()}.png")
                FileOutputStream(f).use { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }
                f
            }
        }.getOrElse {
            bmp.recycle()
            return ToolResult(false, err = "截图存盘失败：${it.message}")
        }

        if (!flagArg(args, "inline")) {
            bmp.recycle()
            return ToolResult(true, text = "已截屏：${saved.absolutePath}\n（${saved.length() / 1024} KB，只在手机本机）")
        }

        // PNG 直接塞进 MCP 太胖；缩放 + JPEG 后通常只有几十到两百 KB，
        // 远端既能看清文字，也不至于把一帧撑到几 MB。
        val max = intArg(args, "max", 1_280).coerceIn(320, 2_048)
        val longSide = maxOf(bmp.width, bmp.height)
        val scaled = if (longSide > max) {
            val ratio = max.toFloat() / longSide
            Bitmap.createScaledBitmap(
                bmp,
                (bmp.width * ratio).toInt().coerceAtLeast(1),
                (bmp.height * ratio).toInt().coerceAtLeast(1),
                true,
            )
        } else {
            bmp
        }
        val jpg = ByteArrayOutputStream()
        val ok = scaled.compress(Bitmap.CompressFormat.JPEG, 80, jpg)
        val bytes = jpg.toByteArray()
        if (scaled !== bmp) scaled.recycle()
        bmp.recycle()
        if (!ok || bytes.isEmpty()) {
            return ToolResult(false, err = "截图压缩失败（图片已存：${saved.absolutePath}）")
        }
        return ToolResult(
            true,
            text = "已截屏：${saved.absolutePath}\n（内联 JPEG ${longSide.coerceAtMost(max)}px 长边，" +
                "${bytes.size / 1024} KB；PNG 原图也在手机上）",
            imageBase64 = Base64.encodeToString(bytes, Base64.NO_WRAP),
            mimeType = "image/jpeg",
        )
    }

    private fun notifList(args: Map<String, Any?>): ToolResult {
        if (!NotificationWatch.enabled(App.ctx)) {
            return ToolResult(
                false,
                err = "通知监听没授权。去系统设置 → 通知使用权 → 勾选「天网 Next」；授权后要有新通知进来才会出现。",
            )
        }
        val limit = intArg(args, "limit", 20).coerceIn(1, 40)
        val items = NotificationWatch.list(limit)
        if (items.isEmpty()) {
            return ToolResult(true, text = "还没有读到通知（授权后要有新通知进来才会出现）。")
        }
        return ToolResult(true, text = items.joinToString("\n") { "· " + it.line() })
    }

    private fun clipboardGet(ctx: Context): ToolResult {
        val cm = ctx.getSystemService(ClipboardManager::class.java)
            ?: return ToolResult(false, err = "系统没有剪贴板服务")
        val clip = cm.primaryClip
            ?: return ToolResult(true, text = "剪贴板为空")
        val text = (0 until clip.itemCount).joinToString("\n") {
            clip.getItemAt(it).coerceToText(ctx)?.toString().orEmpty()
        }
        return if (text.isBlank()) {
            ToolResult(true, text = "剪贴板里没有文字（可能是图片，或系统在后台拒绝读取）")
        } else {
            ToolResult(true, text = text)
        }
    }

    private fun clipboardSet(ctx: Context, args: Map<String, Any?>): ToolResult {
        val text = strArg(args, "text")
        if (text.isEmpty()) return ToolResult(false, err = "text 不能为空")
        val cm = ctx.getSystemService(ClipboardManager::class.java)
            ?: return ToolResult(false, err = "系统没有剪贴板服务")
        cm.setPrimaryClip(ClipData.newPlainText("天网", text))
        return ToolResult(true, text = "已写入剪贴板 ${text.length} 个字")
    }

    private fun batteryStatus(ctx: Context): ToolResult {
        val bm = ctx.getSystemService(BatteryManager::class.java)
        val level = bm?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY) ?: -1
        val charging = bm?.isCharging ?: false
        val pm = ctx.getSystemService(PowerManager::class.java)
        val whitelisted = runCatching {
            pm?.isIgnoringBatteryOptimizations(ctx.packageName) ?: false
        }.getOrDefault(false)
        val powerSave = pm?.isPowerSaveMode ?: false
        val txt = buildString {
            append("电量：").append(if (level in 0..100) "$level%" else "读不到").append('\n')
            append("充电：").append(if (charging) "是" else "否").append('\n')
            append("省电模式：").append(if (powerSave) "开" else "关").append('\n')
            append("电池优化白名单：").append(if (whitelisted) "已在名单（长任务更稳）" else "不在名单（息屏后可能被限流/杀后台）")
        }
        return ToolResult(true, text = txt)
    }

    private fun volumeRead(ctx: Context): ToolResult {
        val am = ctx.getSystemService(AudioManager::class.java)
            ?: return ToolResult(false, err = "系统没有音频服务")
        val max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC).coerceAtLeast(1)
        val cur = am.getStreamVolume(AudioManager.STREAM_MUSIC).coerceIn(0, max)
        val pct = cur * 100 / max
        val tag = if (cur == 0) "（静音）" else ""
        return ToolResult(true, text = "媒体音量：$cur/$max（$pct%）$tag")
    }

    private fun volumeSet(ctx: Context, args: Map<String, Any?>): ToolResult {
        val pct = intArg(args, "percent", -1)
        if (pct !in 0..100) {
            return ToolResult(false, err = "percent 要在 0–100 之间")
        }
        val am = ctx.getSystemService(AudioManager::class.java)
            ?: return ToolResult(false, err = "系统没有音频服务")
        val max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC).coerceAtLeast(1)
        // 四舍五入到最近的档位；直接用 pct*max/100 会永远向下取整，100% 也可能差一档
        val idx = ((pct * max + 50) / 100).coerceIn(0, max)
        am.setStreamVolume(AudioManager.STREAM_MUSIC, idx, 0)
        return ToolResult(true, text = "媒体音量已设为 $pct%（档位 $idx/$max）")
    }

    private fun netStatus(ctx: Context): ToolResult {
        val cm = ctx.getSystemService(ConnectivityManager::class.java)
            ?: return ToolResult(false, err = "系统没有网络服务")
        val net = cm.activeNetwork
            ?: return ToolResult(true, text = "当前没有活动网络（飞行模式或没连网）")
        val caps = cm.getNetworkCapabilities(net)
            ?: return ToolResult(true, text = "有活动网络，但读不到它的能力")
        val kinds = buildList {
            if (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) add("Wi-Fi")
            if (caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) add("蜂窝")
            if (caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)) add("以太网")
            if (caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) add("VPN")
            if (caps.hasTransport(NetworkCapabilities.TRANSPORT_BLUETOOTH)) add("蓝牙")
        }.ifEmpty { listOf("未知") }
        val metered = !caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED)
        val validated = caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
        val down = caps.linkDownstreamBandwidthKbps
        val txt = buildString {
            append("网络：").append(kinds.joinToString(" + ")).append('\n')
            append("计费网络：").append(if (metered) "是（走流量）" else "否").append('\n')
            append("已验证可上网：").append(if (validated) "是" else "否（连上了但可能没出口）")
            if (down > 0) append("\n下行带宽：约 ").append(down).append(" kbps")
        }
        return ToolResult(true, text = txt)
    }

    private fun audit(args: Map<String, Any?>): ToolResult {
        val limit = intArg(args, "limit", 20).coerceIn(1, 50)
        val rows = ActionLog.list(limit)
        if (rows.isEmpty()) {
            return ToolResult(true, text = "还没有动作记录（只记点击/输入/滑动/开 App/调音量这类有副作用的工具）。")
        }
        return ToolResult(true, text = "最近 ${rows.size} 条动作（最新在上）：\n" +
            rows.joinToString("\n") { ActionLog.format(it) })
    }

    private fun diag(ctx: Context): ToolResult {
        val pm = ctx.getSystemService(PowerManager::class.java)
        val notifPerm = if (Build.VERSION.SDK_INT >= 33) {
            ctx.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) ==
                android.content.pm.PackageManager.PERMISSION_GRANTED
        } else {
            true
        }
        val mic = ctx.checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED
        val whitelisted = runCatching {
            pm?.isIgnoringBatteryOptimizations(ctx.packageName) ?: false
        }.getOrDefault(false)
        val txt = buildString {
            append("无障碍：").append(statusLine(ctx)).append('\n')
            append("通知监听：").append(if (NotificationWatch.enabled(ctx)) "已授权" else "未授权").append('\n')
            append("通知权限：").append(if (notifPerm) "已授予" else "未授予（Android 13+ 需要）").append('\n')
            append("录音权限：").append(if (mic) "已授予" else "未授予（语音输入不可用）").append('\n')
            append("电池优化白名单：").append(if (whitelisted) "在名单" else "不在名单（长任务可能被杀）").append('\n')
            append("云侧配置：").append(if (Net.hasConfig()) "已配置" else "未配置").append('\n')
            append("云侧长期配对：").append(if (Prefs.paired()) "已配对" else "未配对").append('\n')
            append("MCP 入网：").append(if (Prefs.mcpConfigured()) "已配置" else "未配置")
            append("；开关：").append(if (Prefs.mcpEnabled) "自动连接" else "关闭")
            append("；状态：").append(PhoneMcpLink.status).append('\n')
            append("Shizuku：").append(ShizukuBridge.status().line()).append('\n')
            append("设备所有者：").append(OwnerBridge.status(App.ctx).line()).append('\n')
            append("局域网直连：").append(
                if (LanHub.running) "运行中 · 端口 ${LanHub.port}"
                else "未启动（跑 self.lan.start）"
            ).append('\n')
            append("本地模型：").append(ModelCatalog.local().size).append(" 个 GGUF")
        }
        return ToolResult(true, text = txt)
    }

    private fun ownerStatus(ctx: Context): ToolResult {
        val st = OwnerBridge.status(ctx)
        val txt = buildString {
            append("设备所有者：").append(st.line()).append('\n')
            append("本 App 是设备所有者：").append(if (st.deviceOwner) "是" else "否").append('\n')
            append("设备管理已激活：").append(if (st.adminActive) "是" else "否").append('\n')
            append("检测到 Dhizuku：").append(if (st.dhizukuInstalled) "是（本 App 未接其 API）" else "否").append('\n')
            append("能力边界：").append(
                if (st.deviceOwner) "静默授权、系统限制、重启；不是 shell（shell 走 Shizuku）"
                else "未解锁；先跑 self.owner.provision 看设置步骤"
            )
        }
        return ToolResult(true, text = txt)
    }

    private fun shizukuStatus(): ToolResult {
        val st = ShizukuBridge.status()
        val txt = buildString {
            append("Shizuku：").append(st.line()).append('\n')
            append("已安装：").append(if (st.installed) "是" else "否").append('\n')
            append("服务在运行：").append(if (st.running) "是" else "否").append('\n')
            append("本 App 已授权：").append(if (st.authorized) "是" else "否").append('\n')
            if (st.running) {
                append("版本：v").append(st.version).append("；shell uid=").append(st.uid).append('\n')
            }
            append("shell 会话：")
            append(if (st.bound) "已连接" else "未连接（首次执行 self.shell.exec 时自动绑定）")
            if (ShizukuBridge.lastError.isNotBlank()) {
                append("\n最近错误：").append(ShizukuBridge.lastError)
            }
        }
        return ToolResult(true, text = txt)
    }

    private fun deviceInfo(ctx: Context): ToolResult {
        val txt = buildString {
            append("厂商型号：").append(Build.MANUFACTURER).append(' ').append(Build.MODEL).append('\n')
            append("系统：Android ").append(Build.VERSION.RELEASE)
                .append("（SDK ").append(Build.VERSION.SDK_INT).append("）\n")
            append("ABI：").append(Build.SUPPORTED_ABIS.joinToString(", ")).append('\n')
            append("App 包名：").append(ctx.packageName).append('\n')
            append("长期设备配对：")
            if (Prefs.paired()) {
                append("已配对（deviceId=").append(Prefs.deviceId).append("）")
            } else {
                append("未配对（只能用一次性 Token）")
            }
        }
        return ToolResult(true, text = txt)
    }

    // ---------------------------------------------------------------- 节点遍历

    /** 深度优先收集匹配节点。顺序与 [UiTree.findAll] 一致，index 才有意义。 */
    private fun findNodes(
        root: AccessibilityNodeInfo,
        query: String,
        regex: Boolean,
    ): List<AccessibilityNodeInfo> {
        val out = ArrayList<AccessibilityNodeInfo>()
        fun walk(n: AccessibilityNodeInfo) {
            val fields = listOf(
                n.text?.toString().orEmpty(),
                n.contentDescription?.toString().orEmpty(),
                n.viewIdResourceName.orEmpty(),
            )
            if (UiTree.matches(fields, query, regex)) out.add(n)
            for (i in 0 until n.childCount) n.getChild(i)?.let(::walk)
        }
        walk(root)
        return out
    }

    private fun firstNode(
        root: AccessibilityNodeInfo,
        predicate: (AccessibilityNodeInfo) -> Boolean,
    ): AccessibilityNodeInfo? {
        if (predicate(root)) return root
        for (i in 0 until root.childCount) {
            firstNode(root.getChild(i) ?: continue, predicate)?.let { return it }
        }
        return null
    }

    /** 匹配到的可能是包着输入框的容器；ACTION_SET_TEXT 必须落在真正可编辑的那个节点上。 */
    private fun editableTarget(n: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        if (n.isEditable) return n
        for (i in 0 until n.childCount) {
            editableTarget(n.getChild(i) ?: continue)?.let { return it }
        }
        return null
    }

    private fun clickableAncestor(n: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        var cur: AccessibilityNodeInfo? = n
        var hops = 0
        while (cur != null && hops < 8) {
            if (cur.isClickable) return cur
            cur = cur.parent
            hops++
        }
        return null
    }

    // ---------------------------------------------------------------- 参数

    private fun strArg(args: Map<String, Any?>, key: String): String =
        args[key]?.toString()?.trim().orEmpty()

    private fun intArg(args: Map<String, Any?>, key: String, def: Int): Int = when (val v = args[key]) {
        is Number -> v.toInt()
        is String -> v.trim().toIntOrNull() ?: def
        else -> def
    }

    private fun flagArg(args: Map<String, Any?>, key: String): Boolean = when (val v = args[key]) {
        is Boolean -> v
        is Number -> v.toInt() != 0
        is String -> v.equals("true", true) || v == "1"
        else -> false
    }
}

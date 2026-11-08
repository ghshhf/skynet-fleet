package app.skynet.tianwang.next

/**
 * 工具面裁剪：把 60 个工具按「这句话要干哪类事」收窄到 ≤8 个，再喂本地模型。
 *
 * 为什么必须裁 —— 同一个项目在 PC 侧实测过（总纲体检表）：
 *   4 个工具 5.6 秒 / 20 个 14.9 秒 / 58 个 56.2 秒，而决策超时线是 25 秒；
 *   给到 6 个近邻工具时，0.6B~1.7B 就开始选错、并出现位置偏置。
 * 结论不是「给得越多越保险」，而是**给得越多越不会选**。
 *
 * 两级结构（沿用 PC 侧已验证的七域两级，按手机能力落成八域）：
 *   第一级：按目标关键词判域，命中不了走通用域；
 *   第二级：域内按「核心工具 → 原顺序」排序，截到 limit。
 *
 * 刻意不做的事：不在这里过滤 sideEffect。读写权限由调用方
 * （[LocalAgent.run]）先过滤，本对象只管「面」的宽窄 —— 职责分开才测得准。
 *
 * 纯 Kotlin + org.json，不碰 Android，JVM 单测可验。
 */
object ToolScope {

    /** 默认预算：8 个。总纲给的安全线是「约 2000 tokens / 20 个工具」，8 个留足余量。 */
    const val DEFAULT_LIMIT = 8

    /** 一次裁剪的结果。 */
    data class Picked(
        /** 本轮真正给模型的工具。 */
        val tools: List<McpTool>,
        /** 领域中文名（可能两个域并列，用「、」连接）；走通用域时为「通用」。 */
        val domain: String,
        /** 是否真的裁掉过工具。false 表示原本就不超预算，原样给出。 */
        val narrowed: Boolean,
    )

    /**
     * 九个域。`words` 是中文/英文触发词（按出现即记分），`prefixes` 是工具名前缀归属，
     * `core` 是该域里最该先给模型的那几个（顺序即优先级），`veto` 是让本域出局的否决词。
     */
    private enum class Domain(
        val label: String,
        val words: List<String>,
        val prefixes: List<String>,
        val core: List<String>,
        /**
         * 命中这些词时本域直接出局。
         *
         * 存在的理由只有一个：**同一个动词属于不同域**。说「网盘里搜一下」时，
         * 「搜」是网盘动作；不加否决，联网域会靠「搜一下」拿到同样的分，
         * 结果两边各给一半工具，模型反而更容易选错。
         */
        val veto: List<String> = emptyList(),
    ) {
        UI(
            "界面操作",
            listOf(
                "点", "点击", "点一下", "按一下", "长按", "双击", "输入", "填", "打字", "写入",
                "滑动", "滑", "滚", "下拉", "翻页", "返回", "首页", "主屏", "截图", "截屏",
                "界面", "屏幕", "按钮", "图标", "控件", "找到", "查找", "定位", "选", "勾",
                // 英文：对等节点派单时目标常是英文
                "tap", "click", "swipe", "scroll", "type", "input", "screenshot", "screen",
                "button", "icon", "find", "long press",
            ),
            listOf("self_ui_"),
            listOf(
                "self_ui_dump", "self_ui_find", "self_ui_tap", "self_ui_text",
                "self_ui_scroll", "self_ui_key", "self_status",
            ),
        ),
        APP(
            "应用与系统",
            listOf(
                "打开", "启动", "应用", "app", "安装", "卸载", "电量", "电池", "充电", "音量",
                "静音", "网络", "wifi", "流量", "设备", "型号", "版本", "权限", "自检",
                "诊断", "审计", "剪贴", "通知", "锁屏", "亮屏",
                "launch", "battery", "volume", "device", "permission", "uninstall",
            ),
            listOf(
                "self_app_", "self_status", "self_screen_state", "self_battery_",
                "self_audio_", "self_net_", "self_device_", "self_diag", "self_perm_", "self_audit",
            ),
            listOf(
                "self_app_launch", "self_app_list", "self_status",
                "self_screen_state", "self_diag",
            ),
        ),
        PEER(
            "集群协作",
            listOf(
                "另一台", "另一个手机", "另一部", "别的手机", "集群", "对端", "派给", "派单",
                "局域网", "直连", "配对", "节点", "手机上", "两台",
                "peer", "another phone", "cluster", "lan", "other device",
            ),
            listOf("self_peer_", "self_lan_"),
            listOf(
                "self_peer_list", "self_peer_pick", "self_peer_call",
                "self_lan_status", "self_peer_probe",
            ),
        ),
        MODEL(
            "模型与算力",
            listOf(
                "模型", "算力", "推理", "问一下", "让模型", "本地模型", "生成", "回答", "思考",
                "model", "llm", "inference",
            ),
            listOf(
                "self_model_ask", "self_agent_goal", "self_api_ask",
                "self_peer_goal", "self_peer_pool",
            ),
            listOf("self_model_ask", "self_api_ask", "self_agent_goal"),
        ),
        NET(
            "网络接入",
            listOf(
                "域名", "解析", "ddns", "公网", "ipv6", "动态域名", "外网", "端口",
                "domain", "dns", "public ip", "port",
            ),
            listOf("self_dns_", "self_ddns_"),
            listOf("self_dns_resolve", "self_ddns_status", "self_ddns_publish"),
        ),
        WEB(
            "联网查资料",
            listOf(
                "联网", "上网", "网上", "网页", "网站", "网址", "链接", "搜索", "搜一下", "搜搜",
                "搜下", "查一下", "查查", "查下", "查询", "百度", "必应", "谷歌",
                "新闻", "资讯", "头条", "天气", "股价", "汇率", "百科", "维基", "教程",
                "文档", "资料", "最新", "官网",
                // 「把网页交给它自己解析」这条线：用户通常说「这几篇/这个链接/帮我看看/讲了啥」
                "几篇", "这篇", "这个链接", "这篇文章", "文章", "博客", "帖子", "原文",
                "解析", "读一下", "读读", "看看这个", "看看这", "讲什么", "讲了什么",
                "讲了啥", "说了什么", "说了啥", "总结一下", "摘要", "翻译", "英文", "外文",
                "search", "google", "bing", "web", "webpage", "website", "url", "online",
                "browse", "news", "weather", "wiki", "look up", "article", "translate",
            ),
            listOf("web_"),
            // 多篇汇总排第一：一句话给几个链接时，模型该一次读完，而不是逐条 read。
            listOf("web_digest", "web_search", "web_read"),
            // 「网盘里搜一下」是网盘动作，不是上网查资料。
            veto = listOf("网盘", "文件夹", "相册", "剪贴板", "openlist", "alist"),
        ),
        CLOUD(
            "网盘文件",
            listOf(
                "网盘", "文件", "目录", "文件夹", "下载", "上传", "搜文件", "搜索文件", "存储",
                "openlist", "alist",
                "cloud", "folder", "download", "upload", "drive", "storage",
            ),
            listOf("cloud_"),
            listOf("cloud_nodes", "cloud_list", "cloud_search", "cloud_file_url"),
        ),
        NOTIFY(
            "通知与剪贴板",
            listOf(
                "通知栏", "通知", "消息", "剪贴板", "复制", "粘贴", "拷贝",
                "notification", "clipboard", "copy", "paste",
            ),
            listOf("self_notif_", "self_clipboard_"),
            listOf("self_notif_list", "self_clipboard_get", "self_clipboard_set"),
        ),
        ROOT(
            "高权限",
            listOf(
                "shizuku", "shell", "命令", "设备所有者", "root", "提权", "adb", "静默授权",
                "device owner", "command",
            ),
            listOf("self_shizuku_", "self_shell_", "self_owner_"),
            listOf("self_shizuku_status", "self_owner_status", "self_shell_exec"),
        ),
    }

    /** 前缀冲突的显式归属：`self_peer_*` 里这两个其实是「模型算力」，不是集群协作。 */
    private val overrides: Map<String, Domain> = mapOf(
        "self_peer_goal" to Domain.MODEL,
        "self_peer_pool" to Domain.MODEL,
    )

    /**
     * 补位下限：域内工具少于这个数才补，**不为了凑满 [DEFAULT_LIMIT] 而塞无关工具**。
     *
     * 5 而不是 8：联网 / 网络 / 通知三个域本来各只有 3 个工具，硬凑 8 个时
     * 补进去的全是与这件事无关的动作能力 —— 见下方补位块的实测记录。
     */
    private const val FILL_FLOOR = 5

    /**
     * **只读**观察底座：任何域补不满时只用这几个，绝不补动作工具。
     *
     * 为什么单独列一份：`self_ui_tap` 放在补位表里会酿成误动作 ——
     * 用户问「这网页讲了什么」，工具面里只要有 tap，模型就会去点页面标题。
     * 工具面里存在的动作，模型一定会找到理由去用它。
     */
    private val COMMON_OBSERVE = listOf("self_status", "self_diag", "self_ui_dump")

    /** 跨域通用的「观察 + 操作」底座：只给通用域用（那里用户可能真要动手）。 */
    private val COMMON_BASE = listOf(
        "self_status", "self_ui_dump", "self_ui_find", "self_ui_tap",
    )

    /** 通用域：目标里一个域关键词都没命中时给的 8 个最常用能力（看状态→找→点/输→开 App）。 */
    private val COMMON_CORE = COMMON_BASE + listOf(
        "self_ui_text", "self_app_launch", "self_app_list", "self_model_ask",
    )

    /**
     * 裁一次工具面。
     *
     * @param goal 用户这句话（只用最新一条，不要把整段历史都当信号，多轮里第一句会带偏）
     * @param all 已经按读写权限过滤过的工具表
     * @param limit 本轮最多给几个工具
     */
    fun pick(goal: String, all: List<McpTool>, limit: Int = DEFAULT_LIMIT): Picked {
        if (all.isEmpty()) return Picked(emptyList(), "无", false)
        if (all.size <= limit) return Picked(all, "", false)

        val g = goal.lowercase()
        val scores = Domain.entries
            .map { it to score(it, g) }
            .filter { it.second > 0 }
            .sortedByDescending { it.second }

        if (scores.isEmpty()) {
            val picked = ArrayList<McpTool>(limit)
            COMMON_CORE.forEach { n ->
                all.firstOrNull { it.name == n }?.let { if (picked.none { p -> p.name == n }) picked.add(it) }
            }
            // 核心不满就按原顺序补，优先读工具（只读的失败代价低）。
            all.asSequence()
                .filter { t -> picked.none { it.name == t.name } }
                .sortedBy { it.sideEffect }
                .forEach { if (picked.size < limit) picked.add(it) }
            return Picked(picked.take(limit), "通用", true)
        }

        // 并列最高分最多合并两个域，避免「什么都沾一点」又变回全量。
        val top = scores.first().second
        val chosen = scores.filter { it.second == top }.take(2)

        val picked = ArrayList<McpTool>(limit)
        for ((d, _) in chosen) {
            val bucket = all.filter { domainOf(it.name) == d }
            bucket.sortedByDescending { rank(it, d, g) }
                .forEach { t ->
                    if (picked.size < limit && picked.none { it.name == t.name }) picked.add(t)
                }
        }
        // 两个域加起来仍不满：只补**只读**观察底座，补到 FILL_FLOOR 就收，不硬凑满 limit。
        //
        // 曾经这里是「先补 status/dump/find/tap，再按原顺序兜底到满 8 个」，
        // 真机实测踩了坑：联网域只有 3 个工具，补位把 self_ui_tap 塞了进去，
        // 1.7B 读完正文看见列表里有 tap，真的去点页面标题 —— 用户要的是结论，
        // 它去开浏览器，还因为无障碍没开把整轮任务拖进失败。
        // 结论：补位只能是「观察」，绝不能是「操作」；宁可少给，不可乱给。
        if (picked.size < FILL_FLOOR) {
            COMMON_OBSERVE.forEach { n ->
                if (picked.size < FILL_FLOOR && picked.none { it.name == n }) {
                    all.firstOrNull { it.name == n && !it.sideEffect }?.let { picked.add(it) }
                }
            }
            all.asSequence()
                .filter { !it.sideEffect }
                .forEach { t ->
                    if (picked.size < FILL_FLOOR && picked.size < limit &&
                        picked.none { p -> p.name == t.name }
                    ) {
                        picked.add(t)
                    }
                }
        }
        return Picked(picked, chosen.joinToString("、") { it.first.label }, true)
    }

    /** 域归属：先查显式例外，再按前缀匹配。 */
    private fun domainOf(name: String): Domain? {
        overrides[name]?.let { return it }
        return Domain.entries.firstOrNull { d -> d.prefixes.any { name.startsWith(it) } }
    }

    /** 给单测用的可读域标签；无归属返回空串（60 个工具一个都不该为空）。 */
    internal fun domainLabel(name: String): String = domainOf(name)?.label ?: ""

    /**
     * 域得分：命中一个触发词记 1 分；命中多个词自然更高，避免单个歧义词决定全局。
     * 命中否决词直接 0 分 —— 见 [Domain.veto]。
     *
     * 额外一条硬信号：句子里出现 http(s) 链接就是联网任务。用户最常见的说法是
     * 直接甩一条链接（「https://…」），句子里没有任何「网页/搜索」字样，
     * 光靠词表会判成通用域 —— 模型手里没有联网工具，只能说做不到。
     */
    private fun score(d: Domain, goal: String): Int {
        if (d.veto.any { hit(goal, it) }) return 0
        var s = d.words.count { hit(goal, it) }
        if (d == Domain.WEB && (goal.contains("http://") || goal.contains("https://"))) s += 10
        return s
    }

    /**
     * 词命中判定：中文按包含，英文按词边界。
     *
     * 英文不能用 contains —— `file` 会命中 `profile`、`port` 会命中 `report`、
     * `lan` 会命中 `plan`，一句无关的话会被判成某个域。
     */
    private fun hit(goal: String, w: String): Boolean {
        if (w.isEmpty()) return false
        if (w.any { it.code >= 128 }) return goal.contains(w)
        return Regex("(?<![a-z0-9])" + Regex.escape(w) + "(?![a-z0-9])").containsMatchIn(goal)
    }

    /**
     * 域内排序键。
     *
     * 两条信号，权重刻意拉开：
     *  - 核心工具：只给很小的加成（100 递减），保证「没有更具体的线索时」按常用度排；
     *  - **长词命中工具名或描述**：+300，直接压过核心顺序 —— 说「截图」就该先给
     *    `self_ui_screenshot`，而不是让它死在 core 列表后面被 limit 截掉。
     *
     * 只认长度 ≥2 的词：中文单字（点、按、滑）几乎命中所有 ui 工具的描述，
     * 加了分等于没加，反而把真正的信号淹掉。
     */
    private fun rank(t: McpTool, d: Domain, goal: String): Int {
        var s = 0
        val i = d.core.indexOf(t.name)
        if (i >= 0) s += 100 - i
        d.words.asSequence()
            .filter { it.length >= 2 && hit(goal, it) }
            .forEach { w ->
                if (t.name.contains(w) || t.description.contains(w)) s += 300
            }
        return s
    }
}

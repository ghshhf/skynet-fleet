package app.skynet.tianwang.next

/**
 * 本地工具闭环的失败诊断与止损。
 *
 * 小模型在界面上最常见的两种浪费：
 * 1. 同一个动作原样重试 —— 权限没开、屏幕锁着、控件不在，重试一百次结果一样；
 * 2. 一路失败还继续调 —— 三步用光，回给控制方的只有一句「失败了」，不知道停在哪。
 *
 * 这里不调模型、不猜原因：只按工具回包里的确定文字分类，给下一轮一条可执行的换路建议；
 * 命中硬止损条件就立刻停手，并把已经做成的步骤如实带回去。
 *
 * 🔴 原则：分类只做事实到类别的映射，证据不足一律 UNKNOWN，不臆造理由。
 */
object Replan {

    /** 失败的大类。标签直接进提示词，给模型看的。 */
    enum class Kind(val label: String) {
        PERMISSION("通道/权限未开"),
        NO_WINDOW("读不到界面"),
        LOCKED("屏幕锁定"),
        REJECTED("系统拒绝注入"),
        TIMEOUT("等待超时"),
        NOT_FOUND("没找到目标"),
        ARGUMENT("参数不合法"),
        EXEC("工具自身异常"),
        UNKNOWN("原因不明"),
    }

    /**
     * 一步工具执行的事实。`argKey` 只用于判重，必须是规范化后的参数串。
     */
    data class Step(
        val name: String,
        val argKey: String,
        val ok: Boolean,
        val err: String = "",
        val text: String = "",
        val sideEffect: Boolean = false,
    )

    /** 按回包文字分类。顺序有讲究：先认最具体、最不该重试的那类。 */
    fun classify(err: String): Kind {
        val e = err.trim()
        if (e.isEmpty()) return Kind.UNKNOWN
        return when {
            // 通道没开：重试同一个动作永远不会变
            e.contains("无障碍通道没开") || e.contains("无障碍服务没开") ||
                e.contains("没授权") || e.contains("未授权") || e.contains("没有授权") ||
                e.contains("缺少权限") || e.contains("没有权限") ||
                e.contains("权限不足") || e.contains("Shizuku") ->
                Kind.PERMISSION

            // 「读不到窗口」比「锁屏」更接近事实：先按读不到处理，建议里再让人查锁屏
            e.contains("读不到当前窗口") || e.contains("读不到窗口") -> Kind.NO_WINDOW

            // 手势/输入被系统挡回
            e.contains("没被系统接受") || e.contains("系统拒绝") ||
                e.contains("拒绝了写入") || e.contains("拒绝了") -> Kind.REJECTED

            e.contains("锁屏") || e.contains("已锁定") || e.contains("屏幕锁着") -> Kind.LOCKED

            e.contains("没等到") || e.contains("超时") || e.contains("timeout", true) ->
                Kind.TIMEOUT

            e.contains("没找到") || e.contains("找不到") ||
                e.contains("只找到") || e.contains("没有第") -> Kind.NOT_FOUND

            e.startsWith("缺 ") || e.contains("不能为空") || e.contains("只允许") ||
                e.contains("不在屏幕内") || e.contains("超出屏幕") ||
                e.contains("只认") || e.contains("参数") -> Kind.ARGUMENT

            e.contains("Exception") || e.contains("Error") || e.contains("异常") ||
                e.startsWith("打开失败") || e.startsWith("执行失败") -> Kind.EXEC

            else -> Kind.UNKNOWN
        }
    }

    /**
     * 失败后塞进下一轮的具体建议。
     *
     * 每条都必须是「换一条路」而不是「再试一次」——原样重试是这里最想拦的行为。
     */
    fun hint(kind: Kind, tool: String): String = when (kind) {
        Kind.PERMISSION ->
            "通道或权限没开，原样重试不会变。停手并如实说明缺哪个权限；只读信息可改用 self_status / self_diag。"
        Kind.NO_WINDOW ->
            "先 self_status 看无障碍是否还在线；刚断开就停手报告，不要连续重试读窗口。"
        Kind.LOCKED ->
            "锁屏时无障碍点不到 App。先用 self_screen_state 确认，锁屏就停手请用户解锁，不要连续注入手势。"
        Kind.REJECTED ->
            "系统拒绝了这次注入。先 self_screen_state 看是否锁屏或停在系统界面；不要原样再发一次。"
        Kind.TIMEOUT ->
            "目标是等出来的，不是重等出来的。先 self_ui_dump 看当前到底在哪个界面，再决定换关键词还是换路径。"
        Kind.NOT_FOUND -> when (tool) {
            "self_ui_find", "self_ui_tap", "self_ui_text" ->
                "定位没命中。换更短、更稳定的关键词重新 self_ui_find；仍没有就先 self_ui_scroll 再找。不要重复同一个 query。"
            "self_app_launch" ->
                "包名可能不对。先用 self_app_list 查已安装的包名，再启动；不要反复试同一个包名。"
            else ->
                "没找到目标。先 self_ui_dump 重新读界面，再换定位方式；不要重复同一个 query。"
        }
        Kind.ARGUMENT ->
            "参数不合法。按工具说明改一次；改不了就停手报告，不要靠反复试。"
        Kind.EXEC ->
            "工具自身异常。换一个等价工具或停手报告；不要原样再调。"
        Kind.UNKNOWN ->
            "原因不明。先只读自查（self_status / self_ui_dump），再决定下一步；不要原样重试。"
    }

    /**
     * 执行前的硬止损：同一个写操作已经成功过，就不许再来一遍。
     *
     * 返回非空 = 拒绝执行。读操作重复（例如连读两次界面）不拦——那可能是刻意等待。
     */
    fun blockBefore(steps: List<Step>, name: String, argKey: String, sideEffect: Boolean): String? {
        if (!sideEffect) return null
        val done = steps.count { it.ok && it.name == name && it.argKey == argKey }
        return if (done > 0) {
            "同一个写操作已经成功执行过 $done 次，拒绝重复执行（避免重复点击 / 重复发送）"
        } else {
            null
        }
    }

    /**
     * 执行后的硬止损：返回非空 = 立刻停手。
     *
     * 计数规则：成功后「连续失败」重新起算；但累计失败仍会记录，
     * 因为成功一次不代表后面不会继续绕圈。
     */
    fun stall(steps: List<Step>): String? {
        val last = steps.lastOrNull() ?: return null
        if (last.ok) return null

        val kind = classify(last.err)
        if (kind == Kind.PERMISSION) return "通道/权限没开，继续调用只会重复失败"
        if (kind == Kind.LOCKED) return "屏幕锁着，无障碍点不到 App，继续调用没有意义"

        val same = steps.count { !it.ok && it.name == last.name && it.argKey == last.argKey }
        if (same >= 2) return "同一动作已失败 $same 次，换路或停手"

        val tail = steps.takeLastWhile { !it.ok }
        if (tail.size >= 2 && tail.all { classify(it.err) == kind }) {
            return "${kind.label}已连续失败 ${tail.size} 次，不再原样重试"
        }
        if (steps.count { !it.ok } >= 3) return "累计失败 3 步，停手"
        return null
    }

    /**
     * 停手时回给控制方的事实账本：做了几步、成几步、最后卡在哪。
     *
     * 不写「大概完成了」这类判断——只有工具回包能证明的事才写进来。
     */
    fun summary(steps: List<Step>): String {
        if (steps.isEmpty()) return "没有执行任何工具。"
        val ok = steps.count { it.ok }
        return buildString {
            append("已执行 ${steps.size} 步，成功 $ok 步：")
            steps.forEachIndexed { i, s ->
                append("\n${i + 1}. ${s.name} → ")
                if (s.ok) {
                    append("成功")
                    if (s.text.isNotBlank()) append("（返回 ${s.text.length} 字）")
                } else {
                    append("失败：").append(clip(s.err))
                }
            }
            val lastErr = steps.lastOrNull { !it.ok }?.err
            if (!lastErr.isNullOrBlank()) append("\n最后失败原因：").append(clip(lastErr))
        }
    }

    private fun clip(s: String, n: Int = 120): String {
        val one = s.replace('\n', ' ').trim()
        return if (one.length <= n) one else one.take(n) + "…"
    }
}

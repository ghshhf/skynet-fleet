package app.skynet.tianwang.next

import org.json.JSONArray
import org.json.JSONObject

/**
 * 手机自持通道的「纯逻辑」那半 —— 不碰 Android API，单测能直接跑。
 *
 * 为什么要拆出来：无障碍服务拿到的 `AccessibilityNodeInfo` 在 JVM 单测里是空壳，
 * 但「树怎么压扁、文字怎么匹配、点第几个」这些最容易错的判断跟 Android 无关。
 * 拆成 [UiNode] 之后，这些判断可以在电脑上几十秒跑完，不用插手机。
 */
data class UiBounds(
    val left: Int = 0,
    val top: Int = 0,
    val right: Int = 0,
    val bottom: Int = 0,
) {
    val width: Int get() = (right - left).coerceAtLeast(0)
    val height: Int get() = (bottom - top).coerceAtLeast(0)
    val centerX: Int get() = left + width / 2
    val centerY: Int get() = top + height / 2
    val empty: Boolean get() = left >= right || top >= bottom

    override fun toString(): String = "[$left,$top][$right,$bottom]"
}

/**
 * 界面树的一帧快照。字段只留「模型/人判断要用到的」，不照搬 AccessibilityNodeInfo
 * 的几十个属性 —— 一个屏幕 200 个节点，属性全带上就是几千 token。
 */
data class UiNode(
    val text: String = "",
    val desc: String = "",
    val viewId: String = "",
    val cls: String = "",
    val clickable: Boolean = false,
    val longClickable: Boolean = false,
    val scrollable: Boolean = false,
    val editable: Boolean = false,
    val checkable: Boolean = false,
    val checked: Boolean = false,
    val enabled: Boolean = true,
    val focused: Boolean = false,
    val selected: Boolean = false,
    val bounds: UiBounds = UiBounds(),
    val depth: Int = 0,
    val children: List<UiNode> = emptyList(),
)

object UiTree {
    const val DEFAULT_MAX_NODES = 120
    private const val MAX_TEXT = 80

    /**
     * 压扁成「有信息量的节点」列表。
     *
     * 判定沿用 DroidRun 的思路：有文字/描述，或者本身能点/能滚/能输入，才留；
     * 纯布局容器不留 —— 它们只贡献层级噪音，对「点哪里」没有帮助。
     */
    fun compact(root: UiNode, maxNodes: Int = DEFAULT_MAX_NODES): List<UiNode> {
        val cap = maxNodes.coerceAtLeast(1)
        val out = ArrayList<UiNode>(cap)
        fun walk(n: UiNode) {
            if (out.size >= cap) return
            if (meaningful(n)) out.add(n)
            for (c in n.children) {
                if (out.size >= cap) return
                walk(c)
            }
        }
        walk(root)
        return out
    }

    fun meaningful(n: UiNode): Boolean =
        n.text.isNotBlank() || n.desc.isNotBlank() ||
            n.clickable || n.longClickable || n.scrollable || n.editable || n.checkable

    /** 整棵树（含纯容器）的深度优先平铺 —— 找控件要全量，不能只看有标签的。 */
    fun flatten(root: UiNode, maxNodes: Int = Int.MAX_VALUE): List<UiNode> {
        val out = ArrayList<UiNode>()
        fun walk(n: UiNode) {
            if (out.size >= maxNodes) return
            out.add(n)
            for (c in n.children) walk(c)
        }
        walk(root)
        return out
    }

    /**
     * 按文字 / 描述 / 控件 id 找节点，返回深度优先顺序（index 就是这个顺序）。
     * 顺序稳定很重要：「点第 2 个」必须每次都是同一个，否则重试会点到别处。
     */
    fun findAll(
        root: UiNode,
        query: String,
        regex: Boolean = false,
        ignoreCase: Boolean = true,
    ): List<UiNode> {
        val q = query.trim()
        if (q.isEmpty()) return emptyList()
        val re = if (regex) regexOf(q, ignoreCase) ?: return emptyList() else null
        return flatten(root).filter { hits(it, q, re, ignoreCase) }
    }

    fun matches(
        values: List<String>,
        query: String,
        regex: Boolean = false,
        ignoreCase: Boolean = true,
    ): Boolean {
        val q = query.trim()
        if (q.isEmpty()) return false
        val re = if (regex) regexOf(q, ignoreCase) ?: return false else null
        return values.filter { it.isNotBlank() }.any { v ->
            when {
                re != null -> re.containsMatchIn(v)
                ignoreCase -> v.contains(q, true)
                else -> v.contains(q)
            }
        }
    }

    private fun hits(n: UiNode, q: String, re: Regex?, ignoreCase: Boolean): Boolean =
        listOf(n.text, n.desc, n.viewId).filter { it.isNotBlank() }.any { v ->
            when {
                re != null -> re.containsMatchIn(v)
                ignoreCase -> v.contains(q, true)
                else -> v.contains(q)
            }
        }

    private fun regexOf(q: String, ignoreCase: Boolean): Regex? =
        runCatching {
            Regex(q, if (ignoreCase) setOf(RegexOption.IGNORE_CASE) else emptySet())
        }.getOrNull()

    /** 紧凑 JSON：短键 + 只带有信息量的节点。给模型看，省 token。 */
    fun toJson(nodes: List<UiNode>): String {
        val arr = JSONArray()
        for (n in nodes) {
            val o = JSONObject()
            if (n.text.isNotBlank()) o.put("t", cut(n.text))
            if (n.desc.isNotBlank()) o.put("d", cut(n.desc))
            if (n.viewId.isNotBlank()) o.put("id", n.viewId)
            if (n.cls.isNotBlank()) o.put("c", n.cls)
            o.put("b", n.bounds.toString())
            if (!n.enabled) o.put("off", 1)
            if (n.clickable) o.put("clk", 1)
            if (n.longClickable) o.put("lclk", 1)
            if (n.scrollable) o.put("scr", 1)
            if (n.editable) o.put("ed", 1)
            if (n.checkable) o.put("chk", if (n.checked) 1 else 0)
            if (n.focused) o.put("foc", 1)
            if (n.selected) o.put("sel", 1)
            o.put("dp", n.depth)
            arr.put(o)
        }
        return JSONObject().put("count", arr.length()).put("nodes", arr).toString()
    }

    /** 人看的列表：一行一个控件，排查「为什么没点到」比翻 JSON 快得多。 */
    fun format(nodes: List<UiNode>): String {
        if (nodes.isEmpty()) return "（没有匹配的控件）"
        val sb = StringBuilder()
        nodes.forEachIndexed { i, n ->
            sb.append('#').append(i).append(' ')
            if (n.cls.isNotBlank()) sb.append(n.cls).append(' ')
            if (n.text.isNotBlank()) sb.append('"').append(cut(n.text)).append('"')
            if (n.desc.isNotBlank()) sb.append(" desc=\"").append(cut(n.desc)).append('"')
            n.bounds.let { if (!it.empty) sb.append(' ').append(it) }
            val marks = buildList {
                if (n.clickable) add("click")
                if (n.longClickable) add("long")
                if (n.scrollable) add("scroll")
                if (n.editable) add("edit")
                if (n.checkable) add(if (n.checked) "checked" else "checkable")
                if (!n.enabled) add("disabled")
                if (n.focused) add("focused")
            }
            if (marks.isNotEmpty()) sb.append(" [").append(marks.joinToString(",")).append(']')
            if (n.viewId.isNotBlank()) sb.append(" @").append(n.viewId)
            if (i < nodes.lastIndex) sb.append('\n')
        }
        return sb.toString()
    }

    private fun cut(s: String, max: Int = MAX_TEXT): String =
        s.replace('\n', ' ').trim().let { if (it.length <= max) it else it.take(max) + "…" }
}

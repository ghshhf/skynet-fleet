package app.skynet.tianwang.next

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 手机自持通道的纯逻辑：压树、找控件、顺序稳定。
 *
 * 这几条如果错了，真机上表现是「点到了别的按钮」或「明明有字却找不到」——
 * 而这类错误在 adb 日志里看不出，只能在树这一层钉死。
 */
class UiTreeTest {

    private fun node(
        text: String = "",
        desc: String = "",
        id: String = "",
        cls: String = "TextView",
        clickable: Boolean = false,
        editable: Boolean = false,
        scrollable: Boolean = false,
        children: List<UiNode> = emptyList(),
    ) = UiNode(
        text = text, desc = desc, viewId = id, cls = cls,
        clickable = clickable, editable = editable, scrollable = scrollable,
        children = children,
    )

    private fun screen() = node(
        cls = "FrameLayout",
        children = listOf(
            node(text = "设置", id = "com.android.settings:id/title"),
            node(text = "搜索", clickable = true, id = "search"),
            node(
                cls = "LinearLayout",
                children = listOf(
                    node(desc = "返回上一级", clickable = true),
                    node(text = "", cls = "Space"),
                ),
            ),
        ),
    )

    @Test
    fun `按文字找，深度优先顺序稳定`() {
        val root = screen()
        val hits = UiTree.findAll(root, "设置")
        assertEquals(1, hits.size)
        assertEquals("设置", hits[0].text)

        val back = UiTree.findAll(root, "返回")
        assertEquals(1, back.size)
        assertEquals("返回上一级", back[0].desc)
    }

    @Test
    fun `按控件 id 也能找`() {
        val hits = UiTree.findAll(screen(), "com.android.settings:id/title")
        assertEquals(1, hits.size)
        assertEquals("设置", hits[0].text)
    }

    @Test
    fun `正则匹配与非法正则`() {
        assertEquals(2, UiTree.findAll(screen(), "^设|搜索$", regex = true).size)
        // 非法正则不崩、也不乱匹配 —— 调用方据此报「pattern 写错了」
        assertTrue(UiTree.findAll(screen(), "[", regex = true).isEmpty())
    }

    @Test
    fun `空 query 不返回整棵树`() {
        assertTrue(UiTree.findAll(screen(), "   ").isEmpty())
        assertFalse(UiTree.matches(listOf("abc"), " "))
    }

    @Test
    fun `压树只留有信息量的节点 —— 纯容器和空占位不留`() {
        val flat = UiTree.compact(screen())
        val texts = flat.map { it.text.ifBlank { it.desc } }
        assertTrue(texts.contains("设置"))
        assertTrue(texts.contains("搜索"))
        assertTrue(texts.contains("返回上一级"))
        assertFalse(texts.contains(""))       // 空 Space 不该出现
        // 根 FrameLayout 没有文字/动作，不算有信息量；只剩 3 个真控件
        assertEquals(3, flat.size)
    }

    @Test
    fun `压树上限真的生效`() {
        val root = node(children = (1..50).map { node(text = "按钮$it", clickable = true) })
        assertEquals(10, UiTree.compact(root, maxNodes = 10).size)
        // 上限不能变成 0 或负数
        assertEquals(1, UiTree.compact(root, maxNodes = 0).size)
    }

    @Test
    fun `紧凑 JSON 的字段与 count`() {
        val nodes = UiTree.compact(screen())
        val j = JSONObject(UiTree.toJson(nodes))
        assertEquals(nodes.size, j.getInt("count"))
        val first = j.getJSONArray("nodes").getJSONObject(0)
        assertTrue(first.has("b"))
    }

    @Test
    fun `人看的列表带可点标记与坐标`() {
        val txt = UiTree.format(UiTree.findAll(screen(), "搜索"))
        assertTrue(txt.contains("#0"))
        assertTrue(txt.contains("[click]"))
        assertTrue(txt.contains("@search"))
    }
}

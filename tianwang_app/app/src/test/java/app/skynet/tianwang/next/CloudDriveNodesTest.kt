package app.skynet.tianwang.next

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** 节点发现不能把「任何返回 JSON 的服务」都当成 OpenList。 */
class CloudDriveNodesTest {

    @Test
    fun `OpenList 公开设置才算可达节点`() {
        assertTrue(CloudDriveNodes.looksLikeOpenList("""{"code":200,"message":"success","data":{}}"""))
    }

    @Test
    fun `缺 code 字段的普通 JSON 不算节点`() {
        assertFalse(CloudDriveNodes.looksLikeOpenList("""{"status":"ok"}"""))
        assertFalse(CloudDriveNodes.looksLikeOpenList("""{"name":"router","model":"AX3000"}"""))
    }

    @Test
    fun `错误码与非 JSON 不算节点`() {
        assertFalse(CloudDriveNodes.looksLikeOpenList("""{"code":401,"message":"login please"}"""))
        assertFalse(CloudDriveNodes.looksLikeOpenList("<html>admin</html>"))
        assertFalse(CloudDriveNodes.looksLikeOpenList(""))
    }
}

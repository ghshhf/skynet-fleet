package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 网盘 WebDAV 解析的纯逻辑测试（不联网、不碰 Android）。
 *
 * 这几个用例都是真机上「连接正常但目录空 / 目录点不进去」的根因：
 * 命名空间前缀大小写、绝对 href、路径里的字面 `+`、中文目录自身条目、XML 实体。
 */
class CloudDriveApiTest {

    private val nextcloud = """
        <?xml version="1.0"?>
        <d:multistatus xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns">
         <d:response>
          <d:href>/remote.php/dav/files/u/</d:href>
          <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat>
         </d:response>
         <d:response>
          <d:href>/remote.php/dav/files/u/%E4%B8%AD%E6%96%87%E7%9B%AE%E5%BD%95/</d:href>
          <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat>
         </d:response>
         <d:response>
          <d:href>/remote.php/dav/files/u/a%2Bb%20c.txt</d:href>
          <d:propstat><d:prop><d:resourcetype/><d:getcontentlength>12</d:getcontentlength></d:prop></d:propstat>
         </d:response>
         <d:response>
          <d:href>/remote.php/dav/files/u/A%20%26%20B.txt</d:href>
          <d:propstat><d:prop><d:resourcetype/><d:getcontentlength>3</d:getcontentlength></d:prop></d:propstat>
         </d:response>
        </d:multistatus>
    """.trimIndent()

    @Test
    fun `小写命名空间能解析且跳过自身`() {
        val items = CloudDriveApi.parseWebdavXml(nextcloud, "/remote.php/dav/files/u")
        assertEquals(listOf("中文目录", "A & B.txt", "a+b c.txt"), items.map { it.name })
        val dir = items.first { it.name == "中文目录" }
        assertTrue("目录不能被当成文件", dir.isDir)
        assertEquals("/remote.php/dav/files/u/中文目录", dir.path)
        val f = items.first { it.name == "a+b c.txt" }
        assertFalse(f.isDir)
        assertEquals(12L, f.size)
    }

    @Test
    fun `大写前缀同样能解析`() {
        val upper = nextcloud.replace("<d:", "<D:").replace("</d:", "</D:")
        val items = CloudDriveApi.parseWebdavXml(upper, "/remote.php/dav/files/u")
        assertEquals(3, items.size)
    }

    @Test
    fun `默认命名空间没有前缀也能解析`() {
        val xml = """
            <multistatus xmlns="DAV:">
              <response><href>/dav/x.txt</href><propstat><prop>
                <resourcetype/><getcontentlength>7</getcontentlength>
              </prop></propstat></response>
            </multistatus>
        """.trimIndent()
        val items = CloudDriveApi.parseWebdavXml(xml, "/")
        assertEquals(1, items.size)
        assertEquals("x.txt", items.single().name)
        assertEquals(7L, items.single().size)
    }

    @Test
    fun `绝对 href 只取 path`() {
        val xml = """
            <D:multistatus xmlns:D="DAV:">
              <D:response><D:href>https://cloud.example.com/remote.php/dav/files/u/a%20b.txt</D:href>
                <D:propstat><D:prop><D:resourcetype/></D:prop></D:propstat></D:response>
            </D:multistatus>
        """.trimIndent()
        val items = CloudDriveApi.parseWebdavXml(xml, "/remote.php/dav/files/u")
        assertEquals("/remote.php/dav/files/u/a b.txt", items.single().path)
    }

    @Test
    fun `字面加号不能被解成空格`() {
        assertEquals("/dav/a+b.txt", CloudDriveApi.davHrefPath("/dav/a+b.txt"))
        assertEquals("/dav/a+b.txt", CloudDriveApi.davHrefPath("/dav/a%2Bb.txt"))
    }

    @Test
    fun `XML 实体先还原再解百分号`() {
        assertEquals("/dav/a & b.txt", CloudDriveApi.davHrefPath("/dav/a &amp; b.txt"))
    }

    @Test
    fun `query 与 fragment 不进入路径`() {
        assertEquals("/dav/x.txt", CloudDriveApi.davHrefPath("/dav/x.txt?download=1"))
        assertEquals("/dav/x.txt", CloudDriveApi.davHrefPath("https://h/dav/x.txt#frag"))
    }

    @Test
    fun `没有 collection 但以斜杠结尾也算目录`() {
        val xml = """
            <d:multistatus xmlns:d="DAV:">
              <d:response><d:href>/dav/sub/</d:href>
                <d:propstat><d:prop><d:resourcetype/></d:prop></d:propstat></d:response>
            </d:multistatus>
        """.trimIndent()
        assertTrue(CloudDriveApi.parseWebdavXml(xml, "/dav").single().isDir)
    }

    @Test
    fun `重复条目按路径去重`() {
        val one = """
            <d:response><d:href>/dav/a.txt</d:href>
              <d:propstat><d:prop><d:resourcetype/></d:prop></d:propstat></d:response>
        """.trimIndent()
        val xml = "<d:multistatus xmlns:d=\"DAV:\">$one$one</d:multistatus>"
        assertEquals(1, CloudDriveApi.parseWebdavXml(xml, "/").size)
    }

    @Test
    fun `拼接 URL 时路径段编码`() {
        assertEquals(
            "http://h:5244/dav/%E4%B8%AD%E6%96%87%20%E7%A9%BA%231.txt",
            CloudDriveApi.joinWebdav("http://h:5244/", "/dav/中文 空#1.txt"),
        )
        assertEquals("http://h:5244/", CloudDriveApi.joinWebdav("http://h:5244", "/"))
    }

    @Test
    fun `识别 token 失效但别把普通错误当失效`() {
        assertTrue(CloudDriveApi.isAuthError("HTTP 401 @/api/fs/list"))
        assertTrue(CloudDriveApi.isAuthError("login please"))
        assertTrue(CloudDriveApi.isAuthError("登录已过期，正在重新登录"))
        assertFalse(CloudDriveApi.isAuthError("search not available"))
        assertFalse(CloudDriveApi.isAuthError("HTTP 500 @/api/fs/list"))
        assertFalse(CloudDriveApi.isAuthError("please add a storage"))
    }
}

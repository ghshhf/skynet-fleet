package app.skynet.tianwang.next

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.ConcurrentHashMap

/**
 * 首屏预加载：像视频平台那样，进页面之前先把首页 20 条的封面和摘要备好。
 *
 * 慢的根因不是本机库，而是卡片封面要串两次网络 —— 滚到可见才去目标站抓
 * OG 拿到图片地址，再临时下图。这里把这一步提前到 App 启动时后台做完：
 *   1) 取首页 20 条（与资源页同一个 session、同一套排序，但不写曝光）
 *   2) 分批补 OG 元数据，只补没抓过的、低并发
 *   3) 把规则封面 / OG 图全部下载到磁盘缓存
 * 之后进页面直接命中磁盘，零等待出图；滑动时不再边看边等。
 *
 * 三条硬规矩：只补缓存里没有的、前 4 条优先、总并发交给底层两个闸门（各 2），
 * 不跟文章正文 / 抓取器抢带宽。
 */
object ResourcePrefetchLogic {
    const val FIRST_PAGE = 20
    const val HEAD = 4          // 首屏真正看得见的几条，优先备好
    const val OG_BATCH = 6      // LinkPreviewLoader.ensure 单次上限
    const val NEXT_AHEAD = 8    // 提前几条触发下一页，别等滚到底才拉

    /** 只留能联网取的、去掉重复、按上限截断。纯函数，JVM 单测直接验。 */
    fun normalize(urls: List<String>, limit: Int = FIRST_PAGE): List<String> =
        urls.asSequence()
            .map { it.trim() }
            .filter { it.startsWith("http", true) }
            .distinct()
            .take(limit)
            .toList()

    /** 优先级切分：首屏前几条先做，其余后台慢慢补。 */
    fun split(urls: List<String>, head: Int = HEAD): Pair<List<String>, List<String>> =
        urls.take(head) to urls.drop(head)

    fun batches(urls: List<String>, size: Int = OG_BATCH): List<List<String>> =
        urls.chunked(size)

    /** 提前分页判据：还剩 8 条就开始拉下一页，视频平台也是这么做的。 */
    fun due(last: Int, size: Int, hasNext: Boolean): Boolean =
        hasNext && size > 0 && last >= size - NEXT_AHEAD
}

object ResourcePrefetch {
    @Volatile private var firstDone = false
    // 同一条链接同时只预热一次：滑动补漏和启动预热撞上时不重复发请求。
    private val inflight = ConcurrentHashMap.newKeySet<String>()

    /** App 启动后台跑一次；进程内不重复，缓存命中时几乎零请求。 */
    suspend fun warmFirstPage(session: String, force: Boolean = false) {
        if (firstDone && !force) return
        firstDone = true
        withContext(Dispatchers.IO) {
            val urls = runCatching {
                ResourceStore.feed("", 0, session, recordExposure = false).items.map { it.url }
            }.getOrDefault(emptyList())
            warm(ResourcePrefetchLogic.normalize(urls))
        }
    }

    /**
     * 预热一批（首页或新一页）。返回本批能立刻显示的预览，界面可直接铺上；
     * 真实下载结果落磁盘缓存，下次连进程重启都不用再下。
     */
    suspend fun warm(urls: List<String>): Map<String, LinkPreview> = withContext(Dispatchers.IO) {
        val list = ResourcePrefetchLogic.normalize(urls).filter { inflight.add(it) }
        if (list.isEmpty()) return@withContext emptyMap()
        try {
            val out = HashMap<String, LinkPreview>()
            // 1) 零请求先铺：缓存里的 OG、能静态推出来的规则封面
            for (u in list) LinkPreviewStore.instant(u)?.let { out[u] = it }

            val (head, tail) = ResourcePrefetchLogic.split(list)
            // 2) 首屏前 4 条优先补元数据 + 下图，先把看得见的部分撑起来
            for (b in ResourcePrefetchLogic.batches(head)) {
                out.putAll(runCatching { LinkPreviewLoader.ensure(b) }.getOrDefault(emptyMap()))
            }
            ImageCache.prefetch(App.ctx, out.values.mapNotNull { p ->
                p.image.takeIf { it.isNotBlank() }
            })
            // 3) 其余 16 条后台慢慢补，不阻塞首屏
            for (b in ResourcePrefetchLogic.batches(tail)) {
                out.putAll(runCatching { LinkPreviewLoader.ensure(b) }.getOrDefault(emptyMap()))
            }
            ImageCache.prefetch(App.ctx, out.values.mapNotNull { p ->
                p.image.takeIf { it.isNotBlank() }
            })
            out
        } finally {
            list.forEach { inflight.remove(it) }
        }
    }
}

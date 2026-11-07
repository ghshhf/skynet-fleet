package app.skynet.tianwang.next

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

/**
 * 后台常驻爬取：手机自己联网抓全球资源，不依赖用户下拉刷新。
 *
 * 每 30 分钟由 WorkManager 唤醒一次（系统 Doze / 省电会自然顺延，是 best-effort 的
 * 「约每半小时」）。上门约束：有网、且不在低电量时；抓之前再让 crawlDue 兜底拦一道，
 * 避免和刚发生过的手动刷新撞车重复吃流量。单个源失败由 crawler 内部各自吞掉，
 * 这里整轮失败才重试。
 */
class CrawlWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        // 半小时内刚抓过（含用户手动下拉）就跳过，省流量。
        if (!ResourceApi.crawlDue()) return@withContext Result.success()
        runCatching { ResourceCrawler.crawl() }.fold(
            onSuccess = { Result.success() },
            onFailure = { Result.retry() },
        )
    }
}

/** 幂等登记后台爬取任务：重复调用只会保留一份周期性任务，不会叠加。 */
object CrawlScheduler {
    private const val NAME = "skynet-resource-crawl"
    private const val INTERVAL_MIN = 30L

    fun ensure(ctx: Context) {
        val req = PeriodicWorkRequestBuilder<CrawlWorker>(INTERVAL_MIN, TimeUnit.MINUTES)
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .setRequiresBatteryNotLow(true)
                    .build(),
            )
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.MINUTES)
            .build()
        WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(
            NAME, ExistingPeriodicWorkPolicy.KEEP, req,
        )
    }
}

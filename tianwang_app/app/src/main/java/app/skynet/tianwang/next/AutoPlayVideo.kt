package app.skynet.tianwang.next

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.viewinterop.AndroidView
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import kotlinx.coroutines.flow.distinctUntilChanged

/**
 * 列表内自动预览的档位。默认「仅 WiFi」——信息流视频自动播最耗的是流量，
 * 不是电量；关掉或改「开」都由用户在资源页顶部一键切换，选择落本机。
 */
enum class AutoPlayMode(val label: String) {
    OFF("关"), WIFI("仅 WiFi"), ANY("开");

    companion object {
        fun of(v: Int): AutoPlayMode = values().getOrElse(v) { WIFI }
        fun next(m: AutoPlayMode): AutoPlayMode = values()[(m.ordinal + 1) % values().size]
    }
}

/** 与 Prefs 共用一份私有 SharedPreferences，键名独立，不碰推理 / 网络等既有配置。 */
object AutoPlayPrefs {
    private const val SP = "tianwang_prefs"
    private const val K_MODE = "autoplay_muted_mode"
    private fun sp() = App.ctx.getSharedPreferences(SP, Context.MODE_PRIVATE)

    var mode: AutoPlayMode
        get() = AutoPlayMode.of(sp().getInt(K_MODE, AutoPlayMode.WIFI.ordinal))
        set(v) { sp().edit().putInt(K_MODE, v.ordinal).apply() }
}

/** ACCESS_NETWORK_STATE 已在 Manifest 声明；以太网同样按「不花钱的网」对待。 */
fun isOnWifi(ctx: Context): Boolean {
    val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
        ?: return false
    val n = cm.activeNetwork ?: return false
    val cap = cm.getNetworkCapabilities(n) ?: return false
    return cap.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ||
        cap.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)
}

/**
 * 全局唯一的静音播放器：所有视频卡片共用一个实例，从结构上保证「最多一个在播」，
 * 不会像每卡片各建一个 ExoPlayer 那样滚动时炸出多个解码器。
 * 生命周期跟随资源页，离开页面即释放。
 */
@Composable
fun rememberMutedPlayer(): ExoPlayer {
    val ctx = LocalContext.current
    val player = remember(ctx) {
        ExoPlayer.Builder(ctx).build().apply {
            volume = 0f
            repeatMode = Player.REPEAT_MODE_ONE
            playWhenReady = false
        }
    }
    DisposableEffect(player) {
        onDispose { runCatching { player.release() } }
    }
    return player
}

/**
 * 从可视区里挑「最靠近视口中线」的那条直链视频，作为唯一自动播放目标。
 * 页面型视频（YouTube / B 站等网页）不在此列 —— 它们交给 WebView。
 * 滑出可视区或没有候选时返回 null，上层据此暂停。
 */
@Composable
fun rememberActiveVideoId(
    listState: LazyListState,
    items: List<ResourceCard>,
    enabled: Boolean,
): String? {
    var active by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(listState, items, enabled) {
        if (!enabled) {
            active = null
            return@LaunchedEffect
        }
        snapshotFlow {
            val info = listState.layoutInfo
            val vis = info.visibleItemsInfo
            if (vis.isEmpty()) {
                null
            } else {
                val mid = (info.viewportStartOffset + info.viewportEndOffset) / 2
                vis.mapNotNull { v ->
                    val c = items.getOrNull(v.index)
                    val center = v.offset + v.size / 2
                    if (c != null && c.video.isNotBlank() && isDirectMedia(c.video) &&
                        center >= info.viewportStartOffset &&
                        center <= info.viewportEndOffset
                    ) {
                        v.index to kotlin.math.abs(center - mid)
                    } else {
                        null
                    }
                }.minByOrNull { it.second }?.first?.let { items.getOrNull(it)?.id }
            }
        }.distinctUntilChanged().collect { active = it }
    }
    return active
}

/**
 * 卡片上的静音预览画面。只有当前激活的卡片才挂播放器表面，其余卡片仍是封面图；
 * 表面创建时透明快门，视频首帧出来前露出封面，避免滚动时闪黑块。
 */
@Composable
fun AutoPlaySurface(
    player: ExoPlayer,
    active: Boolean,
    url: String,
    modifier: Modifier = Modifier,
) {
    if (!active) return
    // 首帧解码出来之前让播放器表面完全透明，继续露出封面图。
    // SurfaceView 在视频渲染前默认是黑的，不这么做滚动时会闪一块黑。
    var ready by remember(url) { mutableStateOf(false) }
    DisposableEffect(player, url) {
        val listener = object : Player.Listener {
            override fun onRenderedFirstFrame() { ready = true }
        }
        player.addListener(listener)
        onDispose { player.removeListener(listener) }
    }
    AndroidView(
        modifier = modifier,
        factory = { c: Context ->
            PlayerView(c).apply {
                useController = false
                setShutterBackgroundColor(android.graphics.Color.TRANSPARENT)
                resizeMode = AspectRatioFrameLayout.RESIZE_MODE_ZOOM
                alpha = 0f
            }
        },
        update = {
            it.player = player
            it.alpha = if (ready) 1f else 0f
        },
        onRelease = { it.player = null },
    )
}

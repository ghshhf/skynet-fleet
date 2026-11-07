package app.skynet.tianwang.next

import android.content.Context
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.viewinterop.AndroidView
import androidx.media3.common.MediaItem
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.PlayerView

/**
 * 内嵌视频：直链 mp4/webm/m3u8 等用系统自带 ExoPlayer 播放，不跳浏览器。
 * 页面型视频（YouTube/B站）交给 ResourceViewer 的 WebView，这里不接。
 */
@Composable
fun VideoInline(url: String, modifier: Modifier = Modifier) {
    val ctx = LocalContext.current
    val player = remember(ctx) {
        ExoPlayer.Builder(ctx).build().apply {
            setMediaItem(MediaItem.fromUri(url))
            prepare()
            playWhenReady = false
        }
    }
    DisposableEffect(player) {
        onDispose { runCatching { player.release() } }
    }
    AndroidView(
        modifier = modifier.fillMaxWidth().aspectRatio(16f / 9f),
        factory = { c: Context -> PlayerView(c).apply { this.player = player; useController = true } },
    )
}

/** 是否为可直链播放的媒体文件（ExoPlayer 能啃的），页面型视频 / 普通网页不算。 */
fun isDirectMedia(u: String): Boolean {
    val s = u.lowercase()
    return s.endsWith(".mp4") || s.endsWith(".webm") || s.endsWith(".m4v") ||
        s.endsWith(".mov") || s.endsWith(".avi") || s.endsWith(".3gp") ||
        s.endsWith(".m3u8") || s.endsWith(".mpd") || s.startsWith("rtsp:")
}

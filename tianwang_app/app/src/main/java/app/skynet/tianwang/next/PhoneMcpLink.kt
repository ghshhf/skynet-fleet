package app.skynet.tianwang.next

import java.net.URI
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject

/**
 * 手机入网：出站连 `/pair` 中继，配对后把 [PhoneMcp] 的 JSON-RPC 回包写回去。
 *
 * 为什么是出站：手机通常没有公网入口，也不该为了被控制去开端口。
 * 手机和 PC 各自往外连中继，配对后中继盲转发字节 —— 不依赖 adb/VPN/sshd。
 *
 * 断线策略：配对/连接失败后 3 秒重连，直到用户点「断开」。
 * 状态全部显式（连接中 / 等 PC / 已入网 / 失败原因），不静默假装在线。
 */
data class McpLinkConfig(
    val url: String,
    val room: String,
    val secret: String,
) {
    fun validError(): String = when {
        url.isBlank() -> "中继地址为空"
        !(url.startsWith("ws://") || url.startsWith("wss://")) -> "中继地址要 ws:// 或 wss://"
        room.isBlank() -> "房间为空"
        secret.isBlank() -> "密钥为空"
        else -> ""
    }

    /** 裸地址补 `/pair`；已有路径原样用（与 PC 侧 `normalize_pair_url` 同规则）。 */
    fun pairUrl(): String {
        val raw = url.trim()
        return runCatching {
            val u = URI(raw)
            val path = u.path.orEmpty()
            if (path.isBlank() || path == "/") raw.trimEnd('/') + "/pair" else raw
        }.getOrDefault(raw)
    }
}

object PhoneMcpLink {
    @Volatile
    var status: String = "未连接"
        private set

    @Volatile
    var lastError: String = ""
        private set

    @Volatile
    var connected: Boolean = false
        private set

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var job: Job? = null

    /** 每次 start/stop 递增；旧连接的迟到回调不能覆盖新连接的状态。 */
    @Volatile
    private var generation = 0

    private val client: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .pingInterval(20, TimeUnit.SECONDS)
            .build()
    }

    /** 读已保存的配置开连；配置不全时只改状态，不启动空转重连。 */
    fun start() {
        val cfg = McpLinkConfig(Prefs.mcpUrl, Prefs.mcpRoom, Prefs.mcpSecret)
        val bad = cfg.validError()
        if (bad.isNotBlank()) {
            status = "未配置"
            lastError = bad
            return
        }
        Prefs.mcpEnabled = true
        val gen = ++generation
        job?.cancel()
        connected = false
        lastError = ""
        status = "连接中…"
        job = scope.launch { reconnectLoop(cfg, gen) }
    }

    /** App 重启后按上次的「连接」状态恢复。 */
    fun startIfEnabled() {
        if (!Prefs.mcpEnabled || !Prefs.mcpConfigured()) return
        start()
    }

    fun stop() {
        Prefs.mcpEnabled = false
        pause()
    }

    /** 只停当前连接，不改「用户上次开着」的意图；前台服务被系统回收时用。 */
    fun pause() {
        generation++
        job?.cancel()
        job = null
        connected = false
        lastError = ""
        status = "未连接"
    }

    private suspend fun reconnectLoop(cfg: McpLinkConfig, gen: Int) {
        while (gen == generation && kotlinx.coroutines.currentCoroutineContext().isActive) {
            connectOnce(cfg, gen)
            if (gen == generation && kotlinx.coroutines.currentCoroutineContext().isActive) {
                status = "已断开，3 秒后重连…"
                delay(3000)
            }
        }
    }

    private suspend fun connectOnce(cfg: McpLinkConfig, gen: Int) {
        if (gen != generation) return
        val pairedSignal = CompletableDeferred<Boolean>()
        val closed = CompletableDeferred<Unit>()
        val rx = StringBuilder()
        var ws: WebSocket? = null
        val live = { gen == generation }
        lastError = ""
        try {
            val request = Request.Builder().url(cfg.pairUrl()).build()
            ws = client.newWebSocket(request, object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    val hello = JSONObject()
                        .put("room", cfg.room)
                        .put("token", PairToken.of(cfg.room, cfg.secret))
                    webSocket.send(hello.toString())
                }

                override fun onMessage(webSocket: WebSocket, text: String) {
                    val j = runCatching { JSONObject(text) }.getOrNull()
                    if (j == null) {
                        if (live()) {
                            lastError = "中继回了非 JSON：${text.take(80)}"
                            status = "协议错误"
                        }
                        pairedSignal.complete(false)
                        webSocket.close(1000, "bad relay frame")
                        return
                    }
                    when {
                        j.optBoolean("paired") -> {
                            if (live()) {
                                connected = true
                                status = "已入网 · 等待控制面调用"
                            }
                            pairedSignal.complete(true)
                        }
                        j.optBoolean("waiting") -> {
                            if (live()) status = "已连中继 · 等 PC 接入"
                        }
                        !j.optBoolean("ok", true) -> {
                            if (live()) {
                                lastError = j.optString("err", "中继拒绝配对")
                                status = "配对被拒"
                            }
                            pairedSignal.complete(false)
                            webSocket.close(1000, "pair failed")
                        }
                    }
                }

                override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                    if (!live() || !connected) return
                    rx.append(bytes.utf8())
                    var nl = rx.indexOf("\n")
                    while (nl >= 0) {
                        val line = rx.substring(0, nl)
                        rx.delete(0, nl + 1)
                        if (line.isNotBlank()) {
                            val reply = try {
                                PhoneMcp.handleLine(line)
                            } catch (t: Throwable) {
                                JSONObject()
                                    .put("jsonrpc", "2.0")
                                    .put("id", JSONObject.NULL)
                                    .put("error", JSONObject()
                                        .put("code", -32603)
                                        .put("message", "手机内部错误：${t.javaClass.simpleName}"))
                                    .toString()
                            }
                            reply?.let {
                                webSocket.send(it.toByteArray(Charsets.UTF_8).toByteString())
                            }
                        }
                        nl = rx.indexOf("\n")
                    }
                    // 半条巨帧不能让缓冲区无限涨；正常一帧只有几 KB。
                    if (rx.length > 256 * 1024) {
                        rx.clear()
                        lastError = "收到超过 256KB 的半帧，已丢弃"
                    }
                }

                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    if (live()) lastError = "${t.javaClass.simpleName}: ${t.message ?: "连接失败"}"
                    pairedSignal.complete(false)
                    closed.complete(Unit)
                }

                override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                    closed.complete(Unit)
                }

                override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                    webSocket.close(1000, null)
                }
            })

            val paired = withTimeoutOrNull(45_000) { pairedSignal.await() } ?: false
            if (!paired) {
                if (status == "连接中…") status = "配对失败或超时"
                ws.cancel()
                withTimeoutOrNull(5_000) { closed.await() }
            } else {
                closed.await()
            }
        } finally {
            ws?.cancel()
            if (live()) {
                connected = false
                status = if (lastError.isBlank()) "已断开" else "连接失败"
            }
        }
    }
}

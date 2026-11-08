package app.skynet.tianwang.next

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import java.io.File
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * 真机验证桥：**只在 debug 构建注册**（manifest 占位符 `debugBridge`）。
 *
 * 为什么需要它：这台手机的 ROM 把 `adb shell input` 的 INJECT_EVENTS 权限删了，
 * 点不了屏幕、也打不了字 —— 但「能力到底在真机上跑不跑得通」必须验，
 * 不能靠单测和推理代替。于是给 debug 包开一个广播口：从电脑直接调工具、
 * 直接跑一次完整的本地模型工具闭环，结果写进文件用 adb 取回。
 *
 * 三道闸：
 *  1. release 构建里 `android:enabled="false"`，整条通道不存在；
 *  2. 必须带对 token（首次启动随机生成，存在应用私有外部目录，其它 App 读不到）；
 *  3. 只调本机工具，不接受任何文件写入路径之类的额外指令。
 */
class DebugBridge : BroadcastReceiver() {

    companion object {
        private const val TAG = "TianwangDebug"
        const val ACTION = "app.skynet.tianwang.next.DEBUG"

        fun tokenFile(ctx: Context): File =
            File(ctx.getExternalFilesDir(null), "debug_token")

        /** App 启动时调一次：没有就生成，有就复用。返回当前 token。 */
        fun ensureToken(ctx: Context): String {
            val f = tokenFile(ctx)
            val old = runCatching { f.readText().trim() }.getOrDefault("")
            if (old.length >= 8) return old
            val fresh = java.util.UUID.randomUUID().toString().replace("-", "").take(12)
            runCatching {
                f.parentFile?.mkdirs()
                f.writeText(fresh)
            }
            return fresh
        }

        private fun outFile(ctx: Context): File =
            File(ctx.getExternalFilesDir(null), "debug_out.txt")
    }

    override fun onReceive(ctx: Context, intent: Intent) {
        val token = intent.getStringExtra("token").orEmpty()
        val expect = ensureToken(ctx)
        if (token != expect) {
            Log.w(TAG, "token 不匹配，忽略这条广播")
            return
        }
        // 🔴 onReceive 必须立刻返回，**不调 goAsync**。
        //    真机实测（0.1.44）：在 goAsync() 里跑「模型工具闭环」会触发
        //    broadcast ANR —— 播放超时约 10 秒，而闭环要 30 秒以上，
        //    系统直接杀进程（Killing … user request after error），结果文件一行没写。
        //    所以长任务一律挂到 App.scope，接收器只管收下就走。
        Log.i(TAG, "收到广播，转后台执行")
        App.scope.launch {
            val text = try {
                handle(intent)
            } catch (t: Throwable) {
                "异常：${t.javaClass.simpleName}: ${t.message}"
            }
            runCatching {
                outFile(ctx).appendText(
                    "\n===== ${System.currentTimeMillis()} =====\n$text\n",
                )
            }
            Log.i(TAG, text)
        }
    }

    private suspend fun handle(intent: Intent): String {
        val tool = intent.getStringExtra("tool").orEmpty()
        val chat = intent.getStringExtra("chat").orEmpty()
        return when {
            chat.isNotBlank() -> chatOnce(chat)
            tool.isNotBlank() -> {
                val args = runCatching {
                    JSONObject(intent.getStringExtra("args") ?: "{}")
                }.getOrElse { JSONObject() }
                val started = System.currentTimeMillis()
                val r = PhoneMcp.executeTool(tool, args)
                val ms = System.currentTimeMillis() - started
                buildString {
                    append("工具：").append(tool).append('\n')
                    append("参数：").append(args).append('\n')
                    append("耗时：").append(ms).append(" ms\n")
                    append("结果：").append(if (r.ok) "成功" else "失败").append('\n')
                    append(r.text.ifBlank { r.err })
                }
            }
            else -> "缺参数：tool=<工具名>&args=<JSON> 或 chat=<一句话>"
        }
    }

    /** 跑一次完整的本地模型闭环：选工具 → 执行 → 回灌 → 回答。 */
    private suspend fun chatOnce(prompt: String): String {
        if (!ModelGate.enter()) return "本机模型正忙（界面上可能正在对话）"
        try {
            if (Demo.modelDir.isBlank()) {
                Demo.modelDir = File(ctx().getExternalFilesDir(null), "models").absolutePath
            }
            val models = ModelCatalog.local()
            if (models.isEmpty()) return "本机没有 GGUF 模型"
            val base = models.firstOrNull { it.id == Prefs.lastModelId } ?: models.first()
            val profile = base.copy(maxTokens = 512)
            val trace = StringBuilder()
            val result = LocalAgent.run(
                profile = profile,
                history = listOf(ChatMsg(role = ChatMsg.Role.USER, text = prompt)),
                tools = PhoneMcp.toolList(),
                allowActions = true,
                execute = { n, a -> PhoneMcp.executeTool(n, a) },
                onDelta = {},
                onTool = { c ->
                    trace.append("[工具] ").append(c.name).append(' ').append(c.args)
                        .append(" → ").append(c.state.name).append(' ')
                        .append((c.result.ifBlank { "" }).take(120)).append('\n')
                },
            )
            return buildString {
                append("问：").append(prompt).append('\n')
                append("模型：").append(profile.label).append('\n')
                if (trace.isNotEmpty()) append(trace).append('\n')
                result.fold(
                    onSuccess = { append("回答：\n").append(it) },
                    onFailure = { append("失败：").append(it.message ?: it.javaClass.simpleName) },
                )
            }
        } finally {
            ModelGate.exit()
        }
    }

    private fun ctx(): Context = App.ctx
}
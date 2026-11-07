package app.skynet.tianwang.next

import java.io.File

/**
 * 本机本地模型的单句问答：只生成，不调工具。
 *
 * 这是并行算力的最小单元 —— 两台手机各跑自己的模型回答同一句话，
 * 谁先回用谁、或者把两边答案并排给用户看。
 */
object LocalAsk {
    suspend fun ask(prompt: String, maxTokens: Int = 256): ToolResult {
        if (prompt.isBlank()) return ToolResult(false, err = "缺 prompt：写清要问什么")
        if (!ModelGate.enter()) {
            return ToolResult(false, err = "本机模型正忙（另一个目标/问答还在跑），等它结束再派")
        }
        try {
            if (Demo.modelDir.isBlank()) {
                Demo.modelDir = File(App.ctx.getExternalFilesDir(null), "models").absolutePath
            }
            val models = ModelCatalog.local()
            if (models.isEmpty()) {
                return ToolResult(false, err = "本机没有 GGUF 模型。先在「模型」页下载一个")
            }
            val base = models.firstOrNull { it.id == Prefs.lastModelId } ?: models.first()
            val profile = base.copy(maxTokens = maxTokens.coerceIn(32, 2048))
            return LocalBackend.chat(
                profile = profile,
                messages = listOf(ChatMsg(role = ChatMsg.Role.USER, text = prompt)),
                onDelta = {},
                onTool = {},
            ).fold(
                onSuccess = { text ->
                    if (text.isBlank()) ToolResult(false, err = "模型回了空串（可能上下文/显存不够）")
                    else ToolResult(true, text = "由 ${profile.label} 回答：\n$text")
                },
                onFailure = { e ->
                    ToolResult(false, err = "本地模型回答失败：${e.message ?: e.javaClass.simpleName}")
                },
            )
        } finally {
            ModelGate.exit()
        }
    }
}

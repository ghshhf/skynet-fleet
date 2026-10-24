package app.skynet.tianwang.next

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateContentSize
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowUpward
import androidx.compose.material.icons.filled.Bolt
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Cloud
import androidx.compose.material.icons.filled.ExpandLess
import androidx.compose.material.icons.filled.ExpandMore
import androidx.compose.material.icons.filled.Memory
import androidx.compose.material.icons.filled.SmartToy
import androidx.compose.material3.AssistChip
import androidx.compose.material3.AssistChipDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextField
import androidx.compose.material3.TextFieldDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 聊天页。这块是「照 Operit 抄」抄得最实的一处，抄的是**信息层级**而不是像素：
 *
 *   1. 助手消息不用气泡包起来，直接排在底上——气泡会把它框成一"段话"，
 *      而它其实是一串动作 + 一段结论；
 *   2. 思维链单独一层、默认收起，点开才看。它属于"我想知道过程时才有用"的东西，
 *      默认展开会把人淹掉；
 *   3. 每次工具调用一张卡片，显示 名字 / 参数 / 结果 / 耗时 / 状态色。
 *      用户真正想知道的是"它到底动了我机器上的什么"，不给这个就只是聊天壳子。
 */
@Composable
fun ChatScreen(
    msgs: List<ChatMsg>,
    model: ModelProfile,
    onPickModel: () -> Unit,
    onSend: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    var draft by remember { mutableStateOf("") }
    val list = rememberLazyListState()

    // 新消息到了就贴底（没有这条，流式输出会推到屏幕外面去）
    LaunchedEffect(msgs.size, msgs.lastOrNull()?.text?.length) {
        if (msgs.isNotEmpty()) list.animateScrollToItem(msgs.lastIndex)
    }

    Column(modifier.fillMaxSize()) {
        ModelBar(model, onPickModel)

        LazyColumn(
            state = list,
            modifier = Modifier.weight(1f).fillMaxWidth(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(
                start = 14.dp, end = 14.dp, top = 10.dp, bottom = 14.dp,
            ),
            verticalArrangement = Arrangement.spacedBy(18.dp),
        ) {
            items(msgs, key = { it.id }) { m ->
                when (m.role) {
                    ChatMsg.Role.USER -> UserBubble(m.text)
                    ChatMsg.Role.ASSISTANT -> AssistantBlock(m)
                    ChatMsg.Role.SYSTEM -> SystemNote(m.text)
                }
            }
        }

        Composer(draft, { draft = it }, onSend = {
            val t = draft.trim()
            if (t.isNotEmpty()) { onSend(t); draft = "" }
        })
    }
}

/* ---------------------------------- 顶栏 ---------------------------------- */

@Composable
private fun ModelBar(model: ModelProfile, onPick: () -> Unit) {
    val isLocal = model.kind == BackendKind.LOCAL
    Row(
        Modifier.fillMaxWidth()
            .background(MaterialTheme.colorScheme.surface)
            .clickable(onClick = onPick)
            .padding(horizontal = 14.dp, vertical = 9.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(
            if (isLocal) Icons.Filled.Memory else Icons.Filled.Cloud,
            contentDescription = null,
            tint = if (isLocal) StatusColors.ok else MaterialTheme.colorScheme.primary,
            modifier = Modifier.size(15.dp),
        )
        Spacer(Modifier.width(7.dp))
        Text(
            model.label,
            fontSize = 12.5.sp,
            fontWeight = FontWeight.Medium,
            color = MaterialTheme.colorScheme.onSurface,
        )
        Spacer(Modifier.width(7.dp))
        Text(
            if (isLocal) "本地 · 断网可用" else "云端 · 长上下文",
            fontSize = 11.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.weight(1f))
        Icon(
            Icons.Filled.ExpandMore, contentDescription = "切换模型",
            tint = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.size(18.dp),
        )
    }
    HorizontalDivider(color = MaterialTheme.colorScheme.outline)
}

/* ---------------------------------- 消息 ---------------------------------- */

@Composable
private fun UserBubble(text: String) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
        Surface(
            color = MaterialTheme.colorScheme.primaryContainer,
            shape = RoundedCornerShape(16.dp, 16.dp, 4.dp, 16.dp),
            modifier = Modifier.widthIn(max = 300.dp),
        ) {
            Text(
                text,
                modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp),
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.onPrimaryContainer,
            )
        }
    }
}

@Composable
private fun SystemNote(text: String) {
    Text(
        text,
        modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp),
        fontSize = 11.5.sp,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
}

@Composable
private fun AssistantBlock(m: ChatMsg) {
    var thoughtOpen by remember { mutableStateOf(false) }

    Row(Modifier.fillMaxWidth().height(IntrinsicSize.Min)) {
        // 左侧一条竖线代替头像：省空间，又让人一眼看出"这一整段是它做的"
        Box(
            Modifier.width(2.dp).fillMaxHeight()
                .background(MaterialTheme.colorScheme.primary.copy(alpha = 0.35f))
        )
        Spacer(Modifier.width(11.dp))

        Column(Modifier.weight(1f)) {
            if (m.thought.isNotBlank()) {
                ThoughtStrip(m.thought, thoughtOpen) { thoughtOpen = !thoughtOpen }
                Spacer(Modifier.height(10.dp))
            }

            m.tools.forEachIndexed { i, t ->
                ToolCard(t)
                if (i != m.tools.lastIndex) Spacer(Modifier.height(7.dp))
            }
            if (m.tools.isNotEmpty()) Spacer(Modifier.height(11.dp))

            if (m.text.isNotBlank()) {
                MarkdownLite(m.text)
            }
            if (m.streaming) {
                Spacer(Modifier.height(6.dp))
                TypingDots()
            }
            if (m.failed.isNotBlank()) {
                Spacer(Modifier.height(6.dp))
                Text(m.failed, fontSize = 12.sp, color = MaterialTheme.colorScheme.error)
            }
        }
    }
}

/** 思维链：一行摘要 + 展开全文。摘要取第一句，够让人判断"要不要展开"。 */
@Composable
private fun ThoughtStrip(text: String, open: Boolean, toggle: () -> Unit) {
    val head = text.lineSequence().firstOrNull()?.take(48) ?: ""
    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(8.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f))
            .clickable(onClick = toggle)
            .padding(horizontal = 10.dp, vertical = 7.dp)
            .animateContentSize(),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(
                Icons.Filled.Bolt, null,
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.size(12.dp),
            )
            Spacer(Modifier.width(6.dp))
            Text(
                if (open) "思考过程" else "思考：$head…",
                fontSize = 11.5.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.weight(1f),
            )
            Icon(
                if (open) Icons.Filled.ExpandLess else Icons.Filled.ExpandMore,
                null, tint = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.size(15.dp),
            )
        }
        AnimatedVisibility(open) {
            Text(
                text,
                modifier = Modifier.padding(top = 7.dp),
                fontSize = 12.sp,
                lineHeight = 18.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/** 工具卡片。状态用色条 + 小圆点表示，不用整卡换色（整卡换色太吵）。 */
@Composable
private fun ToolCard(t: ToolCall) {
    var open by remember { mutableStateOf(false) }
    val (dot, label) = when (t.state) {
        ToolCall.State.RUNNING -> StatusColors.running to "运行中"
        ToolCall.State.OK -> StatusColors.ok to "完成"
        ToolCall.State.FAIL -> StatusColors.fail to "失败"
    }

    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surface)
            .clickable { open = !open }
            .animateContentSize()
            .padding(horizontal = 11.dp, vertical = 9.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(6.dp).clip(CircleShape).background(dot))
            Spacer(Modifier.width(8.dp))
            Text(
                t.name,
                fontSize = 12.5.sp,
                fontWeight = FontWeight.Medium,
                color = MaterialTheme.colorScheme.onSurface,
                modifier = Modifier.weight(1f),
            )
            if (t.state == ToolCall.State.RUNNING) {
                CircularProgressIndicator(
                    Modifier.size(11.dp), strokeWidth = 1.5.dp, color = dot,
                )
                Spacer(Modifier.width(7.dp))
            }
            Text(label, fontSize = 10.5.sp, color = dot)
            if (t.ms > 0) {
                Spacer(Modifier.width(7.dp))
                Text("${t.ms}ms", fontSize = 10.5.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.width(4.dp))
            Icon(
                if (open) Icons.Filled.ExpandLess else Icons.Filled.ExpandMore,
                null, tint = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.size(15.dp),
            )
        }
        AnimatedVisibility(open) {
            Column(Modifier.padding(top = 8.dp)) {
                if (t.args.isNotBlank()) {
                    Text("参数", fontSize = 10.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Text(t.args, style = MonoStyle, modifier = Modifier.padding(top = 2.dp))
                }
                if (t.result.isNotBlank()) {
                    Spacer(Modifier.height(7.dp))
                    Text("结果", fontSize = 10.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Text(t.result, style = MonoStyle, modifier = Modifier.padding(top = 2.dp))
                }
            }
        }
    }
}

/**
 * 极简 Markdown：只认 **粗体**、`行内代码`、- 列表、以及围栏代码块。
 *
 * 为什么不直接引第三方 markdown 渲染库：那类库基本都带一整套 CommonMark 解析器
 * （几百 KB + 一次全量 parse），而模型实际吐的 90% 就是这四样。
 * 真需要渲染表格/公式时再换，不要一开始就为"完备"付代价。
 */
@Composable
fun MarkdownLite(text: String) {
    val blocks = remember(text) { parseBlocks(text) }
    Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
        blocks.forEach { b ->
            when (b) {
                is Block.Code -> CodeBlock(b.lang, b.code)
                is Block.Paragraph -> Text(
                    inlineish(b.text),
                    style = MaterialTheme.typography.bodyLarge,
                    color = MaterialTheme.colorScheme.onSurface,
                )
                is Block.Bullet -> Row {
                    Text("·  ", color = MaterialTheme.colorScheme.primary,
                        style = MaterialTheme.typography.bodyLarge)
                    Text(inlineish(b.text), style = MaterialTheme.typography.bodyLarge,
                        color = MaterialTheme.colorScheme.onSurface)
                }
            }
        }
    }
}

private sealed interface Block {
    data class Paragraph(val text: String) : Block
    data class Bullet(val text: String) : Block
    data class Code(val lang: String, val code: String) : Block
}

private fun parseBlocks(src: String): List<Block> {
    val out = mutableListOf<Block>()
    val lines = src.replace("\r\n", "\n").split("\n")
    var i = 0
    val buf = StringBuilder()

    fun flush() {
        if (buf.isNotBlank()) out.add(Block.Paragraph(buf.toString().trim()))
        buf.clear()
    }

    while (i < lines.size) {
        val ln = lines[i]
        if (ln.trimStart().startsWith("```")) {
            flush()
            val lang = ln.trim().removePrefix("```").trim()
            val code = StringBuilder()
            i++
            while (i < lines.size && !lines[i].trimStart().startsWith("```")) {
                code.appendLine(lines[i]); i++
            }
            i++                              // 跳过收尾的 ```
            out.add(Block.Code(lang, code.toString().trimEnd()))
            continue
        }
        if (ln.trimStart().startsWith("- ") || ln.trimStart().startsWith("* ")) {
            flush()
            out.add(Block.Bullet(ln.trim().substring(2)))
            i++
            continue
        }
        if (ln.isBlank()) { flush(); i++; continue }
        if (buf.isNotEmpty()) buf.append("\n")
        buf.append(ln)
        i++
    }
    flush()
    return out
}

/** 把 **粗体** 和 `代码` 变成 AnnotatedString。粗体真加粗，行内代码只换等宽+底色。 */
@Composable
private fun inlineish(s: String) = androidx.compose.ui.text.buildAnnotatedString {
    var i = 0
    while (i < s.length) {
        when {
            s.startsWith("**", i) -> {
                val end = s.indexOf("**", i + 2)
                if (end < 0) { append(s.substring(i)); return@buildAnnotatedString }
                pushStyle(androidx.compose.ui.text.SpanStyle(fontWeight = FontWeight.Bold))
                append(s.substring(i + 2, end))
                pop()
                i = end + 2
            }
            s[i] == '`' -> {
                val end = s.indexOf('`', i + 1)
                if (end < 0) { append(s.substring(i)); return@buildAnnotatedString }
                pushStyle(
                    androidx.compose.ui.text.SpanStyle(
                        fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace,
                        fontSize = 13.sp,
                        background = Color(0x22FFFFFF),
                    )
                )
                append(s.substring(i + 1, end))
                pop()
                i = end + 1
            }
            else -> { append(s[i]); i++ }
        }
    }
}

@Composable
private fun CodeBlock(lang: String, code: String) {
    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(8.dp))
            .background(Color(0xFF0A0D11))
            .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(8.dp)),
    ) {
        Row(
            Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surfaceVariant)
                .padding(horizontal = 10.dp, vertical = 5.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                if (lang.isBlank()) "code" else lang,
                fontSize = 10.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.weight(1f),
            )
            Text("复制", fontSize = 10.sp, color = MaterialTheme.colorScheme.primary)
        }
        Text(
            code,
            style = MonoStyle,
            modifier = Modifier.padding(horizontal = 11.dp, vertical = 9.dp),
        )
    }
}

@Composable
private fun TypingDots() {
    Row(verticalAlignment = Alignment.CenterVertically) {
        CircularProgressIndicator(
            Modifier.size(11.dp), strokeWidth = 1.5.dp,
            color = MaterialTheme.colorScheme.primary,
        )
        Spacer(Modifier.width(7.dp))
        Text("生成中", fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

/* --------------------------------- 输入区 --------------------------------- */

@Composable
private fun Composer(draft: String, onDraft: (String) -> Unit, onSend: () -> Unit) {
    val can = draft.isNotBlank()
    Column(
        Modifier.fillMaxWidth()
            .background(MaterialTheme.colorScheme.surface)
            .imePadding(),
    ) {
        HorizontalDivider(color = MaterialTheme.colorScheme.outline)
        Row(
            Modifier.fillMaxWidth().padding(start = 6.dp, end = 8.dp, top = 7.dp, bottom = 8.dp),
            verticalAlignment = Alignment.Bottom,
        ) {
            IconButton(onClick = {}) {
                Icon(Icons.Filled.Add, "附件",
                    tint = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            TextField(
                value = draft,
                onValueChange = onDraft,
                modifier = Modifier.weight(1f),
                placeholder = {
                    Text("说点什么，或让它动手", fontSize = 14.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                },
                maxLines = 5,
                shape = RoundedCornerShape(20.dp),
                textStyle = MaterialTheme.typography.bodyLarge,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Default),
                colors = TextFieldDefaults.colors(
                    focusedContainerColor = MaterialTheme.colorScheme.surfaceVariant,
                    unfocusedContainerColor = MaterialTheme.colorScheme.surfaceVariant,
                    focusedIndicatorColor = Color.Transparent,
                    unfocusedIndicatorColor = Color.Transparent,
                ),
            )
            Spacer(Modifier.width(7.dp))
            Surface(
                color = if (can) MaterialTheme.colorScheme.primary
                else MaterialTheme.colorScheme.surfaceVariant,
                shape = CircleShape,
                modifier = Modifier.size(40.dp).clickable(enabled = can) { onSend() },
            ) {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Icon(
                        Icons.Filled.ArrowUpward, "发送",
                        tint = if (can) MaterialTheme.colorScheme.onPrimary
                        else MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.size(19.dp),
                    )
                }
            }
        }
    }
}

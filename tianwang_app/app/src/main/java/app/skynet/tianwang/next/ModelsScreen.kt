package app.skynet.tianwang.next

import android.content.Context
import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Cloud
import androidx.compose.material.icons.filled.Memory
import androidx.compose.material.icons.filled.Download
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 模型页 —— 这是「三个融合」最该被看见的那一页。
 *
 * 🔴 上游两家的病根就在这一页：
 *   pocketpal 这一页只有 GGUF 文件列表，云端是**不存在**的分类；
 *   Operit 这一页只有 API 端点，本地模型是塞在"工具"里的旁枝。
 *   所以在这里，本地和云端**共用一张列表、同一套参数面板、同一个"设为当前"**，
 *   区别只体现在左上角那个图标和一句副标题上。
 *   用户不需要先决定"我要用哪种范式"，他只需要挑一个模型。
 */
@Composable
fun ModelsScreen(
    models: List<ModelProfile>,
    currentId: String,
    onPick: (ModelProfile) -> Unit,
    onChanged: () -> Unit = {},
) {
    var gateways by remember { mutableStateOf<List<Gateway>>(emptyList()) }
    var gwErr by remember { mutableStateOf("") }
    LaunchedEffect(Unit) {
        if (Net.hasConfig()) {
            runCatching { Net.getProviders() }
                .onSuccess { gateways = it }
                .onFailure { gwErr = it.message ?: "拉取失败" }
        }
    }
    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(14.dp),
        verticalArrangement = Arrangement.spacedBy(9.dp),
    ) {
        item("local-head") {
            SectionHead(
                icon = { Icon(Icons.Filled.Memory, null, Modifier.size(14.dp),
                    tint = StatusColors.ok) },
                title = "本地引擎",
                sub = "llama.cpp / GGUF · 断网也能跑 · 数据不出手机",
            )
        }
        items(models.filter { it.kind == BackendKind.LOCAL }, key = { it.id }) {
            ModelRow(it, it.id == currentId) { onPick(it) }
        }
        // 🔴 「+」放在本地组末尾而不是标题栏（2026-10-26）：
        //    标题栏的加号有歧义（加本地还是加云端？），而这里语义天然清楚——
        //    紧跟在本地模型列表后面，就是「再添一个本地模型」。
        item("add-local") {
            AddModelCard(onChanged)
        }

        item("cloud-head") {
            Spacer(Modifier.height(8.dp))
            SectionHead(
                icon = { Icon(Icons.Filled.Cloud, null, Modifier.size(14.dp),
                    tint = MaterialTheme.colorScheme.primary) },
                title = "云端引擎",
                sub = "OpenAI 兼容端点 · 长上下文 · 需要联网",
            )
        }
        // 🔴 P1 判据：这里要能看见至少 2 个网关的真实状态（活/死、延迟、模型数）。
        // 数据来自云侧 /api/providers，不是写死的 demo。
        item("gateways") {
            if (gwErr.isNotEmpty()) {
                Text(gwErr, fontSize = 11.sp, color = MaterialTheme.colorScheme.error)
            } else if (gateways.isEmpty()) {
                Text("（未检测到网关 —— 云侧未启动，或本机还没在「设置」连上）",
                    fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            } else {
                // 🔴 Compose 铁律：forEach 的 lambda 不是 @Composable 上下文，
                // 不能在里面直接调自由组合函数 GatewayRow。用 for 循环（纯控制流），
                // 循环体落在 Column 的可组合作用域里，调用才合法。
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    for (gw in gateways) {
                        GatewayRow(gw)
                    }
                }
            }
        }
        items(models.filter { it.kind == BackendKind.CLOUD }, key = { it.id }) {
            ModelRow(it, it.id == currentId) { onPick(it) }
        }

        item("tail") {
            Spacer(Modifier.height(10.dp))
            Text(
                "两条腿都要。日常与敏感内容走本地，重活走云端——" +
                    "这不是二选一的取舍，是同一台机器上两种预算。",
                fontSize = 11.5.sp,
                lineHeight = 18.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun SectionHead(icon: @Composable () -> Unit, title: String, sub: String) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        icon()
        Spacer(Modifier.width(7.dp))
        Text(title, fontSize = 13.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Spacer(Modifier.width(8.dp))
        Text(sub, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun ModelRow(m: ModelProfile, active: Boolean, onClick: () -> Unit) {
    val local = m.kind == BackendKind.LOCAL
    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(MaterialTheme.colorScheme.surface)
            .border(
                if (active) 1.5.dp else 1.dp,
                if (active) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.outline,
                RoundedCornerShape(12.dp),
            )
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 11.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                Modifier.size(7.dp).clip(CircleShape).background(
                    when {
                        !local -> MaterialTheme.colorScheme.primary
                        m.down -> StatusColors.ok
                        else -> StatusColors.idle
                    }
                )
            )
            Spacer(Modifier.width(9.dp))
            Text(
                m.label, fontSize = 14.sp, fontWeight = FontWeight.Medium,
                color = MaterialTheme.colorScheme.onSurface, modifier = Modifier.weight(1f),
            )
            if (active) {
                Text("当前", fontSize = 10.5.sp, color = MaterialTheme.colorScheme.primary)
                Spacer(Modifier.width(8.dp))
            }
            Text(
                if (local) "LOCAL" else "CLOUD",
                fontSize = 9.5.sp,
                fontFamily = FontFamily.Monospace,
                color = if (local) StatusColors.ok else MaterialTheme.colorScheme.primary,
                modifier = Modifier
                    .clip(RoundedCornerShape(4.dp))
                    .background(MaterialTheme.colorScheme.surfaceVariant)
                    .padding(horizontal = 5.dp, vertical = 2.dp),
            )
        }

        if (m.note.isNotBlank()) {
            Text(
                m.note, fontSize = 11.5.sp, lineHeight = 17.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 16.dp, top = 5.dp),
            )
        }

        if (local && m.sizeMb > 0) {
            Spacer(Modifier.height(9.dp))
            // 参数与「带不带得动」都按本机实测推，不写死 ctx
            val plan = if (m.down) {
                remember(m.id) { runCatching { LocalBackend.previewPlan(m) }.getOrNull() }
            } else null

            if (m.down) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        "${m.sizeMb} MB · ctx ${plan?.ctx ?: 0} · 线程 ${plan?.threads ?: 0}",
                        fontSize = 10.5.sp, fontFamily = FontFamily.Monospace,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Spacer(Modifier.weight(1f))
                    Text(
                        when {
                            plan == null -> "参数未知"
                            plan.fits -> "已就绪"
                            else -> "带不动"
                        },
                        fontSize = 10.5.sp,
                        color = when {
                            plan == null -> StatusColors.idle
                            plan.fits -> StatusColors.ok
                            else -> StatusColors.fail
                        },
                    )
                }
                if (plan != null && !plan.fits) {
                    Spacer(Modifier.height(4.dp))
                    Text(
                        "⚠ ${plan.verdict}",
                        fontSize = 10.5.sp, lineHeight = 15.sp,
                        color = StatusColors.fail,
                        modifier = Modifier.padding(start = 16.dp),
                    )
                }
            } else {
                // 🔴 旧版这里画了一条写死 62% 的进度条 —— 那是纯装饰，
                // 会让用户以为真在下载。没下载器就老实说文件不在。
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        "文件不在 · 推上去或重扫",
                        fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Spacer(Modifier.weight(1f))
                    Text("未就绪", fontSize = 10.5.sp, color = StatusColors.idle)
                }
            }
        }

        if (!local && m.target.isNotBlank()) {
            Spacer(Modifier.height(7.dp))
            Text(
                m.target,
                fontSize = 10.sp, fontFamily = FontFamily.Monospace,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
            )
        }
    }
}

/** 一个网关的真实状态卡（来自云侧 /api/providers）。活/死用色点 + 文案双重表达。 */
@Composable
private fun GatewayRow(gw: Gateway) {
    val alive = gw.status == "活"
    Row(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surface)
            .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(10.dp))
            .padding(horizontal = 12.dp, vertical = 9.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(Modifier.size(7.dp).clip(CircleShape)
            .background(if (alive) StatusColors.ok else StatusColors.fail))
        Spacer(Modifier.width(9.dp))
        Column(Modifier.weight(1f)) {
            Text(
                gw.id.ifBlank { gw.endpoint }, fontSize = 13.sp,
                fontWeight = FontWeight.Medium, color = MaterialTheme.colorScheme.onSurface,
            )
            val sub = buildList {
                if (gw.endpoint.isNotBlank()) add(gw.endpoint)
                if (gw.modelCount > 0) add("${gw.modelCount} 模型")
                if (gw.latencyMs > 0) add("${gw.latencyMs}ms")
            }.joinToString(" · ")
            if (sub.isNotBlank()) {
                Text(
                    sub, fontSize = 10.5.sp, fontFamily = FontFamily.Monospace,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        Text(
            gw.status, fontSize = 11.sp,
            color = if (alive) StatusColors.ok else StatusColors.fail,
        )
    }
}

/** 导入进度。null = 空闲。[total] < 0 表示系统没告诉大小（部分 content provider 如此）。 */
private data class ImportState(
    val name: String = "",
    val done: Long = 0,
    val total: Long = -1,
    val err: String = "",
    val done100: Boolean = false,
)

/**
 * 「＋ 添加本地模型」。
 *
 * 🔴 为什么走系统文件选择器而不是自己写下载器（2026-10-26）：
 *   模型从哪来是**用户的事** —— 网盘、聊天软件收到的、电脑 adb push 的、浏览器下的。
 *   自己写下载器只能覆盖其中一条，还得内置一堆模型 URL（那就是写死）。
 *   SAF 选择器一条覆盖全部，且不需要存储权限。
 *
 * 选完不是直接信任：拷完要验 GGUF 魔数。600MB 拷了十分钟、到加载时才炸，
 * 是最难查的那类故障，宁可拷完立刻判死。
 */
@Composable
private fun AddModelCard(onChanged: () -> Unit) {
    val scope = rememberCoroutineScope()
    var st by remember { mutableStateOf<ImportState?>(null) }
    // "*/*" 而不是 "application/octet-stream"：各家文件管理器对 .gguf 的 MIME
    // 判定不一致，限制太死会选不到文件。格式在拷之前自己校验。
    val picker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri: Uri? ->
        if (uri == null) return@rememberLauncherForActivityResult
        scope.launch {
            st = ImportState(name = displayName(App.ctx, uri), total = -1)
            val r = importGguf(App.ctx, uri) { d, t ->
                st = st?.copy(done = d, total = t)
            }
            r.onSuccess { f ->
                st = ImportState(name = f.name, done = f.length(), total = f.length(), done100 = true)
                onChanged()
            }.onFailure { e ->
                st = ImportState(err = e.message ?: "导入失败")
            }
        }
    }

    val s0 = st
    val busy = s0 != null && !s0.done100 && s0.err.isEmpty()
    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(MaterialTheme.colorScheme.surface)
            .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(12.dp))
            .clickable(enabled = !busy) { picker.launch(arrayOf("*/*")) }
            .padding(horizontal = 12.dp, vertical = 13.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Filled.Add, null, Modifier.size(16.dp),
                tint = MaterialTheme.colorScheme.primary)
            Spacer(Modifier.width(8.dp))
            Text(
                when {
                    s0 == null -> "添加本地模型"
                    busy -> "正在导入…"
                    s0.err.isNotEmpty() -> "导入失败，点此重试"
                    else -> "已添加，再点可继续添加"
                },
                fontSize = 14.sp, fontWeight = FontWeight.Medium,
                color = when {
                    s0 != null && s0.err.isNotEmpty() -> MaterialTheme.colorScheme.error
                    s0 != null && s0.done100 -> StatusColors.ok
                    else -> MaterialTheme.colorScheme.onSurface
                },
                modifier = Modifier.weight(1f),
            )
        }

        val s = st
        if (s == null) {
            Text(
                "选一个 .gguf 文件（0.6B 约 600MB，1.7B 约 1GB）。" +
                    "也可以从电脑 adb push 到模型目录，进这一页会自动重扫。",
                fontSize = 10.5.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 24.dp, top = 4.dp),
            )
        } else if (s.err.isNotEmpty()) {
            Text(
                s.err, fontSize = 10.5.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.error,
                modifier = Modifier.padding(start = 24.dp, top = 4.dp),
            )
        } else {
            Text(
                s.name, fontSize = 10.5.sp, fontFamily = FontFamily.Monospace,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 24.dp, top = 3.dp),
                maxLines = 1,
            )
            Spacer(Modifier.height(7.dp))
            val frac = if (s.total > 0) (s.done.toFloat() / s.total).coerceIn(0f, 1f)
                       else if (s.done100) 1f else 0f
            if (s.done100) {
                LinearProgressIndicator(progress = { 1f },
                    Modifier.fillMaxWidth().height(4.dp).padding(start = 24.dp))
            } else {
                // total 未知时用不确定态，绝不画一条假装有进度的条
                if (s.total > 0) {
                    LinearProgressIndicator(progress = { frac },
                        Modifier.fillMaxWidth().height(4.dp).padding(start = 24.dp))
                } else {
                    LinearProgressIndicator(
                        Modifier.fillMaxWidth().height(4.dp).padding(start = 24.dp))
                }
            }
            Text(
                if (s.total > 0) "${mb(s.done)} / ${mb(s.total)} MB"
                else "${mb(s.done)} MB（总大小未知）",
                fontSize = 10.sp, fontFamily = FontFamily.Monospace,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 24.dp, top = 5.dp),
            )
        }
    }
}

private fun mb(b: Long): String = "%.0f".format(b / 1048576.0)

/** 流式拷贝到模型目录，边拷边报进度。不把 600MB 读进内存。 */
private suspend fun importGguf(
    ctx: Context,
    uri: Uri,
    onProgress: (Long, Long) -> Unit,
): Result<File> = withContext(Dispatchers.IO) {
    try {
        val dir = File(Demo.modelDir)
        if (!dir.isDirectory && !dir.mkdirs()) {
            return@withContext Result.failure(
                IllegalStateException("建不了模型目录：${dir.absolutePath}"))
        }

        val display = displayName(ctx, uri)
        if (!display.endsWith(".gguf", true)) {
            return@withContext Result.failure(
                IllegalStateException("只能导入 .gguf 文件（选到的是「$display」）"))
        }
        val base = display.removeSuffix(".gguf").removeSuffix(".GGUF")
            .ifBlank { "model-${System.currentTimeMillis()}" }

        // 重名不覆盖：同名不同量化是常事（Q4_K_M / Q8_0），覆盖等于悄悄换了模型
        var target = File(dir, "$base.gguf")
        var n = 1
        while (target.exists()) target = File(dir, "$base-${n++}.gguf")

        val total = runCatching {
            ctx.contentResolver.openAssetFileDescriptor(uri, "r")?.use { it.length } ?: -1L
        }.getOrDefault(-1L)

        val input = ctx.contentResolver.openInputStream(uri)
            ?: return@withContext Result.failure(IllegalStateException("打不开这个文件"))

        var done = 0L
        var lastTick = 0L
        input.use { ins ->
            target.outputStream().use { out ->
                val buf = ByteArray(1 shl 20)
                while (true) {
                    val k = ins.read(buf)
                    if (k < 0) break
                    out.write(buf, 0, k)
                    done += k
                    // 每 16MB 报一次：太密会让主线程一直忙着重组界面
                    if (done - lastTick >= 16L * 1024 * 1024) {
                        lastTick = done
                        onProgress(done, total)
                    }
                }
            }
        }

        // 魔数校验：坏文件现在就丢，别留到加载时炸
        val magic = ByteArray(4)
        var head = ""
        runCatching { target.inputStream().use { it.read(magic) }; head = String(magic) }
        if (head != "GGUF") {
            target.delete()
            return@withContext Result.failure(
                IllegalStateException("不是 GGUF 文件（头部不是 GGUF 魔数），已丢弃"))
        }
        onProgress(done, total)
        Result.success(target)
    } catch (e: Throwable) {
        Result.failure(e)
    }
}

/** 从 content:// 拿真实文件名。拿不到就用 URI 末段兜底。 */
private fun displayName(ctx: Context, uri: Uri): String {
    runCatching {
        ctx.contentResolver.query(uri, null, null, null, null)?.use { c ->
            if (c.moveToFirst()) {
                val i = c.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
                if (i >= 0) c.getString(i) else null
            } else null
        }
    }.getOrNull()?.takeIf { it.isNotBlank() }?.let { return it }
    return uri.lastPathSegment?.substringAfterLast('/')?.substringAfterLast(':') ?: "model.gguf"
}

package app.skynet.tianwang.next

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
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
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch

/**
 * 本机出图：提示词 → 手机自己跑 diffusion → 图片就在这一页。
 *
 * 引擎、模型、QNN 库全在这台手机里，界面上不给「去连谁」的入口。
 * 目录/端口/类型是可改的数据（换机、换模型位置都能在界面上改），不是代码常量。
 */
@Composable
fun ImageScreen(
    autoSelftest: Boolean = false,
    autoPrompt: String = "",
) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var models by remember { mutableStateOf<List<ImageEngine.ImageModel>>(emptyList()) }
    var model by remember { mutableStateOf("") }
    var size by remember { mutableStateOf(512 to 512) }
    var prompt by remember { mutableStateOf("") }
    var negative by remember { mutableStateOf("") }
    var steps by remember { mutableStateOf("20") }
    var cfg by remember { mutableStateOf("7") }
    var seed by remember { mutableStateOf("-1") }
    var busy by remember { mutableStateOf(false) }
    var status by remember { mutableStateOf("") }
    var err by remember { mutableStateOf("") }
    var saved by remember { mutableStateOf("") }
    var bitmap by remember { mutableStateOf<androidx.compose.ui.graphics.ImageBitmap?>(null) }
    var lastBytes by remember { mutableStateOf<ByteArray?>(null) }
    var showCfg by remember { mutableStateOf(false) }
    var rootText by remember { mutableStateOf("") }
    var portText by remember { mutableStateOf("") }
    var npu by remember { mutableStateOf(true) }

    fun scan() {
        models = ImageEngine.scan(ctx)
        if (model.isBlank() || models.none { it.name == model }) {
            model = models.firstOrNull()?.name ?: ""
        }
        val sizes = models.firstOrNull { it.name == model }?.sizes ?: listOf(512 to 512)
        if (size !in sizes) size = sizes.first()
        rootText = ImageEngine.root(ctx).absolutePath
        portText = ImageEngine.port(ctx).toString()
        npu = ImageEngine.type(ctx) != "sd15cpu"
    }

    fun generate(checkOnly: Boolean, promptOverride: String? = null) {
        if (busy) return
        if (models.isEmpty()) {
            err = "本机还没找到出图模型。确认引擎资源目录里有 models/<名字>/unet.bin。"
            android.util.Log.i("TianwangImage", "自检失败：本机模型 0 个 · ${ImageEngine.root(ctx)}")
            return
        }
        val p = (promptOverride ?: prompt).trim()
        if (!checkOnly && p.isBlank()) {
            err = "先写一句提示词"
            return
        }
        busy = true
        err = ""
        saved = ""
        status = "正在启动本机引擎（首次要加载约 1.2G 权重，20~30 秒）…"
        scope.launch {
            val started = ImageEngine.ensureStarted(ctx, model, size.first, size.second)
            if (started.isFailure) {
                err = "引擎没起来：${started.exceptionOrNull()?.message}\n${ImageEngine.lastLog().takeLast(300)}"
                android.util.Log.i("TianwangImage", "引擎启动失败 ${err.take(300)}")
                status = ""
                busy = false
                return@launch
            }
            android.util.Log.i("TianwangImage", "引擎就绪 ${ImageEngine.status(ctx)}")
            if (checkOnly) {
                status = "引擎自检通过 · ${ImageEngine.status(ctx)}"
                busy = false
                return@launch
            }
            status = "正在出图…"
            val r = ImageEngine.generate(
                ctx = ctx,
                prompt = p,
                negative = negative.trim(),
                w = size.first, h = size.second,
                steps = steps.toIntOrNull() ?: 20,
                cfg = cfg.toFloatOrNull() ?: 7f,
                seed = seed.toLongOrNull() ?: -1,
            ) { status = it }
            r.onSuccess { bytes ->
                lastBytes = bytes
                bitmap = ImageEngine.decode(bytes)
                status = if (bitmap == null) "引擎回了数据，但解不出图片" else
                    "出好了 · ${bytes.size / 1024} KB · ${size.first}×${size.second}"
                android.util.Log.i("TianwangImage", "出图成功 ${bytes.size} 字节 · ${status}")
            }.onFailure { e ->
                err = "出图失败：${e.message}"
                android.util.Log.i("TianwangImage", "出图失败 ${err.take(300)}")
                status = ""
            }
            busy = false
        }
    }

    // intent 自检：不用点屏（HyperOS 拦 adb input），一条命令就能验引擎/出图。
    // scan() 和自检必须在同一个 effect 里：分开的话第二个 effect 读到的还是
    // 首帧那个空的 models，会误报「本机模型 0 个」。
    LaunchedEffect(Unit) {
        scan()
        if (autoSelftest) generate(true)
        else if (autoPrompt.isNotBlank()) {
            prompt = autoPrompt
            generate(false, autoPrompt)
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState())
            .padding(horizontal = 16.dp, vertical = 12.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Text("本机出图", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Text(
            "引擎与模型都在这台手机里 · ${models.size} 个底模 · " +
                if (ImageEngine.hasRuntime(ctx)) "QNN 库已就位" else "QNN 库未找到",
            fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        OutlinedTextField(
            prompt, { prompt = it },
            modifier = Modifier.fillMaxWidth(), minLines = 2,
            label = { Text("画面提示词") },
            placeholder = { Text("a lighthouse at dawn, cinematic lighting") },
        )
        OutlinedTextField(
            negative, { negative = it },
            modifier = Modifier.fillMaxWidth(), singleLine = true,
            label = { Text("不想要什么（可留空）") },
            placeholder = { Text("blurry, lowres, watermark") },
        )

        if (models.isNotEmpty()) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("底模", fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.width(8.dp))
                Row(Modifier.weight(1f)) {
                    models.forEach { m ->
                        TextButton(
                            onClick = {
                                model = m.name
                                if (size !in m.sizes) size = m.sizes.first()
                            },
                            enabled = !busy,
                        ) {
                            Text(m.name.take(14), fontSize = 11.sp,
                                color = if (m.name == model) MaterialTheme.colorScheme.primary
                                else MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                }
            }
            val sizes = models.firstOrNull { it.name == model }?.sizes ?: listOf(512 to 512)
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("尺寸", fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.width(8.dp))
                sizes.take(7).forEach { s ->
                    TextButton(onClick = { size = s }, enabled = !busy) {
                        Text("${s.first}×${s.second}", fontSize = 11.sp,
                            color = if (s == size) MaterialTheme.colorScheme.primary
                            else MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        } else {
            Text("本机模型目录：${ImageEngine.root(ctx).absolutePath}/models",
                fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }

        Row(verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(steps, { steps = it }, Modifier.weight(1f), singleLine = true,
                label = { Text("步数") })
            Spacer(Modifier.width(8.dp))
            OutlinedTextField(cfg, { cfg = it }, Modifier.weight(1f), singleLine = true,
                label = { Text("CFG") })
            Spacer(Modifier.width(8.dp))
            OutlinedTextField(seed, { seed = it }, Modifier.weight(1f), singleLine = true,
                label = { Text("种子 -1 随机") })
        }

        Row(verticalAlignment = Alignment.CenterVertically) {
            Button(onClick = { generate(false) }, enabled = !busy && models.isNotEmpty()) {
                Text(if (busy) "处理中…" else "出图", fontSize = 13.sp)
            }
            Spacer(Modifier.width(8.dp))
            TextButton(onClick = { generate(true) }, enabled = !busy && models.isNotEmpty()) {
                Text("引擎自检", fontSize = 12.sp)
            }
            Spacer(Modifier.weight(1f))
            TextButton(onClick = { showCfg = !showCfg }) { Text("引擎设置", fontSize = 12.sp) }
        }

        if (showCfg) {
            HorizontalDivider()
            Text("资源目录 / 端口 / 算力类型（可改，改完重新自检）",
                fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            OutlinedTextField(rootText, { rootText = it }, Modifier.fillMaxWidth(), singleLine = true,
                label = { Text("引擎资源目录") })
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(portText, { portText = it }, Modifier.weight(1f), singleLine = true,
                    label = { Text("端口") })
                Spacer(Modifier.width(8.dp))
                TextButton(onClick = { npu = !npu }) {
                    Text(if (npu) "NPU" else "CPU", fontSize = 12.sp)
                }
                Spacer(Modifier.width(8.dp))
                Button(onClick = {
                    ImageEngine.setRoot(ctx, rootText)
                    portText.toIntOrNull()?.let { ImageEngine.setPort(ctx, it) }
                    ImageEngine.setType(ctx, if (npu) "sd15npu" else "sd15cpu")
                    scan()
                    status = "已保存，点「引擎自检」确认"
                }) { Text("保存", fontSize = 12.sp) }
            }
            Text("当前：${ImageEngine.status(ctx)}", fontSize = 10.5.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }

        if (status.isNotBlank()) {
            Box(Modifier.fillMaxWidth().clip(RoundedCornerShape(8.dp))
                .background(MaterialTheme.colorScheme.surfaceVariant).padding(10.dp)) {
                Text(status, fontSize = 11.5.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        if (err.isNotBlank()) {
            Text(err, fontSize = 11.sp, color = MaterialTheme.colorScheme.error)
        }

        bitmap?.let { bmp ->
            Image(
                bmp, "生成的图片",
                Modifier.fillMaxWidth().aspectRatio(size.first.toFloat() / size.second.toFloat())
                    .clip(RoundedCornerShape(10.dp)),
                contentScale = ContentScale.Fit,
            )
            Row(verticalAlignment = Alignment.CenterVertically) {
                TextButton(onClick = {
                    lastBytes?.let { bytes ->
                        ImageEngine.save(ctx, bytes).onSuccess { saved = "已保存到 $it" }
                            .onFailure { err = "保存失败：${it.message}" }
                    }
                }) { Text("保存到相册", fontSize = 12.sp) }
                TextButton(onClick = { bitmap = null; lastBytes = null; saved = "" }) {
                    Text("清掉", fontSize = 12.sp)
                }
            }
        }
        if (saved.isNotBlank()) Text(saved, fontSize = 11.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(20.dp))
    }
}

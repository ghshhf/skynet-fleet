package app.skynet.tianwang.next

import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Assignment
import androidx.compose.material.icons.filled.Build
import androidx.compose.material.icons.filled.ChatBubbleOutline
import androidx.compose.material.icons.filled.Devices
import androidx.compose.material.icons.filled.Memory
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * 手机端第二代骨架。
 *
 * 结构上跟老工程最大的不同：**不是一个 Activity 手搓所有界面**。
 * 老的那份 MainActivity 2200 行，加一个功能要在一个文件里翻十几个方法；
 * 这里按屏切文件，一屏一件事，加功能是加文件而不是加分支。
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Net.init(applicationContext)
        // 模型目录必须由系统给：`/sdcard/...` 硬编码在 targetSdk 35 下读不到
        // 自己的 Android/data（File.exists() 恒 false ⇒ UI 谎报「没下载完」）。
        Demo.modelDir = java.io.File(getExternalFilesDir(null), "models").absolutePath
        Log.i("TianwangModels", "dir=${Demo.modelDir}")
        Demo.models.forEach { m ->
            Log.i("TianwangModels", "${m.label} down=${m.down} exists=${java.io.File(m.target).exists()} ${m.target}")
        }
        enableEdgeToEdge()
        setContent {
            TianwangTheme {
                App()
            }
        }
        if (intent?.getBooleanExtra("selftest", false) == true) {
            runSelfTest(intent?.getStringExtra("stmodel") ?: "1.7b")
        }
    }

    /**
     * 本地推理自检：`adb shell am start -n app.skynet.tianwang.next/.MainActivity --ez selftest true`
     * （换 4B：再加 `--es stmodel 4b`）。结果全进 logcat，TAG = `Selftest`。
     *
     * 为什么要这个入口：HyperOS 把 `adb shell input` 封了（`INJECT_EVENTS` 权限），
     * 没法从外面点界面。「装了能启动」也不等于「推理真通」，所以必须让 App
     * 自己走一遍加载 + 生成，把首字延迟、耗时、输出都写出来。
     */
    private fun runSelfTest(which: String) {
        Thread {
            val tag = "Selftest"
            try {
                if (!LlamaNative.ensureLoaded()) {
                    Log.e(tag, "FAIL native 库没加载上（libfleet_llama.so 缺或 ABI 不符）")
                    return@Thread
                }
                val want = if (which.contains("4b", true)) "4B" else "1.7B"
                val m = Demo.models.firstOrNull {
                    it.kind == BackendKind.LOCAL && it.down && it.label.contains(want, true)
                }
                if (m == null) {
                    Log.e(tag, "FAIL 没有已就绪的 $want（dir=${Demo.modelDir}）")
                    return@Thread
                }
                Log.i(tag, "START ${m.label} ctx=${m.ctx} path=${m.target}")

                val t0 = System.currentTimeMillis()
                val h = LlamaNative.nativeLoadModel(m.target, m.ctx, 6)
                val loadMs = System.currentTimeMillis() - t0
                Log.i(tag, "LOAD handle=$h 耗时=${loadMs}ms")
                if (h == 0L) {
                    Log.e(tag, "FAIL 加载返回 0（模型损坏 / 内存不足 / 格式不认）")
                    return@Thread
                }

                val prompt = "你好，用一句话介绍你自己。"
                var firstMs = -1L
                var chars = 0
                val tGen = System.currentTimeMillis()
                val out = LlamaNative.nativeChatMessages(h, arrayOf("user"), arrayOf(prompt), 96) { s ->
                    if (firstMs < 0) firstMs = System.currentTimeMillis() - tGen
                    chars += s.length
                }
                val genMs = System.currentTimeMillis() - tGen
                Log.i(tag, "PROMPT=$prompt")
                Log.i(tag, "OUT=[$out]")
                Log.i(tag, "STAT first=${firstMs}ms total=${genMs}ms chars=$chars")
                Log.i(tag, if (out.isNotBlank()) "PASS 本地推理真出字" else "FAIL 输出为空")
                LlamaNative.nativeFreeModel(h)
            } catch (e: Throwable) {
                Log.e(tag, "FAIL 异常 ${e.javaClass.simpleName}: ${e.message}")
            }
        }.start()
    }
}

private enum class Tab(val label: String) {
    CHAT("对话"), MODELS("模型"), TOOLS("工具箱"), TASKS("任务"), FLEET("编队"), SETTINGS("设置")
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun App() {
    var tab by remember { mutableStateOf(Tab.CHAT) }
    // 默认选「这台手机真跑得动」的那个：1.7B 是普适甜点——本机（K70E/天玑8300）
    // 实测 9.8 tok/s，4B 只有 2.9 tok/s（首字 4.2s），明显卡手。
    // 强机（另一台能跑 4B 的）在模型页手动切，不靠这里猜。
    // 顺序：1.7B → 任一就绪 → 第一个占位。
    var current by remember {
        mutableStateOf(
            Demo.models.firstOrNull { it.down && it.label.contains("1.7B") }
                ?: Demo.models.firstOrNull { it.down }
                ?: Demo.models.first()
        )
    }
    var sheet by remember { mutableStateOf(false) }
    val sheetState = rememberModalBottomSheetState()
    val scope = rememberCoroutineScope()

    val msgs = remember { mutableStateListOf<ChatMsg>().apply { addAll(Demo.seedMessages()) } }
    val sessions = remember { mutableStateListOf<Session>().apply { addAll(Demo.sessions) } }
    var busy by remember { mutableStateOf(false) }

    fun send(text: String) {
        if (busy) return
        msgs.add(ChatMsg(role = ChatMsg.Role.USER, text = text))
        val replyId = java.util.UUID.randomUUID().toString()
        msgs.add(ChatMsg(id = replyId, role = ChatMsg.Role.ASSISTANT, streaming = true))
        busy = true
        if (current.kind == BackendKind.LOCAL) {
            // 真本地链路：llama.cpp 已编进 app（libfleet_llama.so），
            // 模型在 /sdcard/tianwang/models/，推理全程不出手机。
            scope.launch {
                val backend = LocalBackend()
                val history = msgs.filter { it.id != replyId }   // 含刚发的 user 消息
                backend.chat(current, history, onDelta = { d ->
                    update(msgs, replyId) { it.copy(text = it.text + d) }
                }).onSuccess { full ->
                    update(msgs, replyId) {
                        it.copy(text = full.ifEmpty { it.text }, streaming = false)
                    }
                    busy = false
                }.onFailure { e ->
                    update(msgs, replyId) {
                        it.copy(failed = e.message ?: "本地推理失败", streaming = false)
                    }
                    busy = false
                }
            }
    }
        else if (Net.hasConfig()) {
            // 真链路：手机 → 云侧 SSE → 电脑侧 agentloop 真跑（工具 + 回答都是真的）
            val steps = androidx.compose.runtime.mutableStateListOf<ToolCall>()
            scope.launch {
                Net.streamAgent(text,
                    onStep = { tc ->
                        val i = steps.indexOfFirst { it.id == tc.id }
                        if (i >= 0) steps[i] = tc else steps.add(tc)
                        update(msgs, replyId) { it.copy(tools = steps.toList()) }
                    },
                    onAnswer = { ans, finalSteps ->
                        update(msgs, replyId) { it.copy(text = ans, tools = finalSteps, streaming = false) }
                        busy = false
                    },
                    onError = { e ->
                        update(msgs, replyId) { it.copy(failed = e, streaming = false) }
                        busy = false
                    })
            }
        }
        else {
                // 未连云侧：用假流式把「输入 → 工具 → 回答」这条链路走通
            scope.launch {
                val steps = listOf(
                    "fleet.nodes" to "{\"detail\":true}",
                    "model.route" to "{\"prefer\":\"${current.kind}\"}",
                )
                val done = mutableListOf<ToolCall>()
                delay(280)
                steps.forEachIndexed { i, (n, a) ->
                    val running = ToolCall(name = n, args = a, state = ToolCall.State.RUNNING)
                    done.add(running)
                    update(msgs, replyId) { it.copy(tools = done.toList()) }
                    delay(420 + i * 160L)
                    done[done.lastIndex] = running.copy(
                        state = ToolCall.State.OK,
                        result = if (n == "fleet.nodes")
                            "2 台在线 / 共 ${Demo.nodes.size} 台"
                        else "已选 ${current.label}",
                        ms = 300 + i * 120L,
                    )
                    update(msgs, replyId) { it.copy(tools = done.toList()) }
                }
                val body = "收到。当前走 **${current.label}**" +
                    (if (current.kind == BackendKind.LOCAL) "（本地，不出手机）" else "（云端）") +
                    "。\n\n这是离线演示回答——在「设置」里连上云侧后，上面这几张卡片会变成真的工具调用。"
                body.split(" ").forEach { w ->
                    update(msgs, replyId) { it.copy(text = it.text + w + " ") }
                    delay(55)
                }
                update(msgs, replyId) { it.copy(streaming = false) }
                busy = false
            }
        }
    }

    Scaffold(
        modifier = Modifier.fillMaxSize().statusBarsPadding(),
        containerColor = MaterialTheme.colorScheme.background,
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(
                            "天网 Next",
                            fontSize = 16.sp, fontWeight = FontWeight.SemiBold,
                            color = MaterialTheme.colorScheme.onSurface,
                        )
                        Text(
                            when (tab) {
                                Tab.CHAT -> msgs.lastOrNull { it.role == ChatMsg.Role.USER }?.text
                                    ?.take(20) ?: "新对话"
                                Tab.MODELS -> "本地 + 云端，一个列表"
                                Tab.TOOLS -> "电脑侧真实能力"
                                Tab.TASKS -> "项目整体进度"
                                Tab.FLEET -> "我在编队里的位置"
                                Tab.SETTINGS -> "会改变行为的开关"
                            },
                            fontSize = 11.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            maxLines = 1,
                        )
                    }
                },
                navigationIcon = {
                    if (tab == Tab.CHAT) {
                        IconButton(onClick = { sheet = true }) {
                            Icon(Icons.Filled.Menu, "会话",
                                tint = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                },
                actions = {
                    if (tab == Tab.CHAT) {
                        IconButton(onClick = {
                            sessions.add(0, Session(title = "新会话"))
                            msgs.clear()
                        }) {
                            Icon(Icons.Filled.Add, "新会话",
                                tint = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.background,
                ),
            )
            HorizontalDivider(color = MaterialTheme.colorScheme.outline)
        },
        bottomBar = {
            Column(Modifier.navigationBarsPadding()) {
                NavigationBar(
                    containerColor = MaterialTheme.colorScheme.surface,
                    tonalElevation = 0.dp,
                ) {
                    Tab.entries.forEach { t ->
                        NavigationBarItem(
                            selected = tab == t,
                            onClick = { tab = t },
                            icon = {
                                Icon(
                                    when (t) {
                                        Tab.CHAT -> Icons.Filled.ChatBubbleOutline
                                        Tab.MODELS -> Icons.Filled.Memory
                                        Tab.TOOLS -> Icons.Filled.Build
                                        Tab.TASKS -> Icons.Filled.Assignment
                                        Tab.FLEET -> Icons.Filled.Devices
                                        Tab.SETTINGS -> Icons.Filled.Settings
                                    },
                                    t.label,
                                )
                            },
                            label = { Text(t.label, fontSize = 10.5.sp) },
                            colors = NavigationBarItemDefaults.colors(
                                selectedIconColor = MaterialTheme.colorScheme.primary,
                                selectedTextColor = MaterialTheme.colorScheme.primary,
                                unselectedIconColor = MaterialTheme.colorScheme.onSurfaceVariant,
                                unselectedTextColor = MaterialTheme.colorScheme.onSurfaceVariant,
                                indicatorColor = MaterialTheme.colorScheme.surfaceVariant,
                            ),
                        )
                    }
                }
            }
        },
    ) { pad ->
        Box(Modifier.fillMaxSize().padding(pad)) {
            when (tab) {
                Tab.CHAT -> ChatScreen(
                    msgs = msgs,
                    model = current,
                    onPickModel = { tab = Tab.MODELS },
                    onSend = { send(it) },
                )
                Tab.MODELS -> ModelsScreen(Demo.models, current.id) { current = it }
                Tab.TOOLS -> ToolsScreen()
                Tab.TASKS -> TasksScreen()
                Tab.FLEET -> FleetScreen(Demo.nodes)
                Tab.SETTINGS -> SettingsScreen()
            }
        }
    }

    if (sheet) {
        ModalBottomSheet(
            onDismissRequest = { sheet = false },
            sheetState = sheetState,
            containerColor = MaterialTheme.colorScheme.surface,
        ) {
            Column(Modifier.fillMaxWidth().padding(horizontal = 14.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("会话", fontSize = 13.sp, fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.onSurface,
                        modifier = Modifier.weight(1f))
                    Text("${sessions.size} 个", fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Spacer(Modifier.height(10.dp))
                LazyColumn(verticalArrangement = Arrangement.spacedBy(7.dp)) {
                    items(sessions, key = { it.id }) { s ->
                        Row(
                            Modifier.fillMaxWidth()
                                .clip(RoundedCornerShape(10.dp))
                                .background(MaterialTheme.colorScheme.surfaceVariant)
                                .clickable { sheet = false }
                                .padding(horizontal = 12.dp, vertical = 11.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Column(Modifier.weight(1f)) {
                                Text(s.title, fontSize = 13.sp,
                                    color = MaterialTheme.colorScheme.onSurface)
                                Text("${s.tokens} tokens", fontSize = 10.5.sp,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                            Spacer(Modifier.width(8.dp))
                        }
                    }
                }
                Spacer(Modifier.height(24.dp))
            }
        }
    }
}

/** 原地改列表里某条消息。原型用它模拟流式；真链路也走同一个入口。 */
private fun update(list: MutableList<ChatMsg>, id: String, f: (ChatMsg) -> ChatMsg) {
    val i = list.indexOfFirst { it.id == id }
    if (i >= 0) list[i] = f(list[i])
}

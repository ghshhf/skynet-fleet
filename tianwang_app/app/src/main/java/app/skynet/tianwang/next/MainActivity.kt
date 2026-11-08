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
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Assignment
import androidx.compose.material.icons.filled.Build
import androidx.compose.material.icons.filled.ChatBubbleOutline
import androidx.compose.material.icons.filled.Devices
import androidx.compose.material.icons.filled.Explore
import androidx.compose.material.icons.filled.Memory
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Person
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Bookmark
import androidx.compose.material.icons.filled.ThumbUp
import androidx.compose.material.icons.filled.Label
import androidx.compose.material.icons.filled.PhoneAndroid
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.NavigationDrawerItem
import androidx.compose.material3.rememberDrawerState
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.TextStyle
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
        App.bind(this)
        Net.init(applicationContext)
        // debug 构建才用得到：生成/复用真机验证用的随机 token（release 里无人读它）。
        runCatching { DebugBridge.ensureToken(this) }
        // 手机端爬虫后台常驻：每 30 分钟自动抓一轮全球资源，不靠用户下拉刷新。
        runCatching { CrawlScheduler.ensure(this) }
        // 电脑上已有凭据按「端点列表」格式一次性交给手机：只落本机，接完即删源件，日志不写内容。
        // 之后加端点在设置页手动完成，不再需要改代码或重装。
        val dropped = LlmConfig.importDropped(applicationContext)
        if (dropped > 0) {
            Log.i("TianwangLlm", "已导入 $dropped 条本机大模型端点")
        }
        // 上次若开着「手机入网」，进程重启后自己接回中继；不要求用户每次手动重连。
        if (Prefs.mcpEnabled && Prefs.mcpConfigured()) PhoneLinkService.start(this)
        if (Prefs.ddnsEnabled) PhoneLinkService.start(this)
        // 出声（总纲第 1 条空白）。默认关，界面上那个喇叭点了才念。
        Speech.init(applicationContext)
        // 可选的局域网节点配置可由 adb 一次性注入，免得每次装机都要在手机上手动敲一遍：
        //   adb shell am start -n app.skynet.tianwang.next/.MainActivity \
        //       --es agentUrl http://127.0.0.1:9124 --es agentToken <token>
        intent?.getStringExtra("agentUrl")?.takeIf { it.isNotBlank() }?.let { u ->
            Net.saveConfig(u, intent.getStringExtra("agentToken") ?: "")
            Log.i("TianwangNet", "已由 intent 写入云侧配置: $u")
        }
        // 模型目录必须由系统给：`/sdcard/...` 硬编码在 targetSdk 35 下读不到
        // 自己的 Android/data（File.exists() 恒 false ⇒ UI 谎报「没下载完」）。
        Demo.modelDir = java.io.File(getExternalFilesDir(null), "models").absolutePath
        Log.i("TianwangModels", "dir=${Demo.modelDir} 本机 GGUF ${ModelCatalog.local().size} 个")
        val found = ModelCatalog.local()
        Log.i("TianwangModels", "扫描到 ${found.size} 个 GGUF")
        found.forEach { Log.i("TianwangModels", "${it.label} ${it.sizeMb}MB ${it.target}") }
        enableEdgeToEdge()
        // 出声自检：HyperOS 把 `adb shell input` 封了（INJECT_EVENTS），
        // 点不了界面上那个喇叭，所以给一条命令让 App 自己开开关 + 念一句。
        //   adb shell am start -n app.skynet.tianwang.next/.MainActivity --ez speaktest true
        // 结果全进 logcat，TAG=TianwangSpeech：init / lang / speak rc 各一行。
        if (intent?.getBooleanExtra("speaktest", false) == true) {
            Speech.setEnabled(true)   // 自检会顺手把开关打开（持久化），验完记得再关
            Speech.speak("这是一句测试。天网手机端第一次开口，能听到就说明链路通了。")
            Log.i("TianwangSpeech",
                "speaktest 已触发 enabled=${Speech.enabled()} status=${Speech.status()}")
        }
        setContent {
            TianwangTheme {
                App()
            }
        }
        if (intent?.getBooleanExtra("selftest", false) == true) {
            runSelfTest(intent?.getStringExtra("stmodel") ?: "")
        }
    }

    /**
     * 本地推理自检：`adb shell am start -n app.skynet.tianwang.next/.MainActivity --ez selftest true`
     * （指定某个模型：`--es stmodel 1.7b`，不指定就取扫到的第一个）。结果全进 logcat，TAG = `Selftest`。
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
                val all = ModelCatalog.local()
                if (all.isEmpty()) {
                    Log.e(tag, "FAIL 模型目录里没有 GGUF（dir=${Demo.modelDir}）")
                    return@Thread
                }
                // 🔴 不指定就挑「这台机器带得动」里最大的那个：
                //    无脑取最大的会拿一个注定缺页到超时的模型开局（4B / 可用 3.4G 实测首字 20 秒）。
                val m = if (which.isBlank()) {
                    val ok = all.filter { Dev.plan(this, it.fileMb, Prefs.mode, it).fits }
                    ok.maxByOrNull { it.sizeMb } ?: all.minByOrNull { it.sizeMb }!!
                } else {
                    all.firstOrNull { it.label.contains(which, true) } ?: all.minByOrNull { it.sizeMb }!!
                }

                val dev = Dev.profile(this)
                Log.i(tag, "DEVICE ${dev.summary()}")
                val plan = Dev.plan(this, m.fileMb, Prefs.mode, m)
                Log.i(tag, "PLAN ${plan.mode.label} threads=${plan.threads} " +
                    "ctx=${plan.ctx} gpu=${plan.gpuPercent}% | ${plan.reason}")
                Log.i(tag, "START ${m.label} ${m.sizeMb}MB path=${m.target}")

                val t0 = System.currentTimeMillis()
                val h = LlamaNative.nativeLoadModel(m.target, plan.ctx, plan.threads, plan.gpuPercent)
                val loadMs = System.currentTimeMillis() - t0
                Log.i(tag, "LOAD handle=$h 耗时=${loadMs}ms")
                if (h == 0L) {
                    Log.e(tag, "FAIL 加载返回 0（模型损坏 / 内存不足 / 格式不认）")
                    return@Thread
                }

                val prompt = "你好，用一句话介绍你自己。"
                var chars = 0
                val tGen = System.currentTimeMillis()
                val out = LlamaNative.nativeChatMessages(
                    h, arrayOf("user"), arrayOf(prompt), 96, Prefs.noThink, "",
                ) { s -> chars += s.length }
                val genMs = System.currentTimeMillis() - tGen
                val st = LlamaNative.lastStats()
                Log.i(tag, "PROMPT=$prompt")
                Log.i(tag, "OUT=[$out]")
                Log.i(tag, "STAT first=${st?.firstMs ?: -1}ms total=${st?.totalMs ?: genMs}ms " +
                    "tokens=${st?.tokens ?: -1} speed=${String.format("%.2f", st?.tokPerSec ?: 0.0)}tok/s chars=$chars")
                Log.i(tag, if (out.isNotBlank()) "PASS 本地推理真出字" else "FAIL 输出为空")

                // 第二轮：验证 KV 前缀复用是不是真生效（同前缀 + 追加一句，应几乎不花 prefill）
                val t2 = System.currentTimeMillis()
                val out2 = LlamaNative.nativeChatMessages(
                    h,
                    arrayOf("user", "assistant", "user"),
                    arrayOf(prompt, out, "再用两个字总结。"),
                    32, Prefs.noThink, "",
                ) { }
                val st2 = LlamaNative.lastStats()
                Log.i(tag, "TURN2 total=${System.currentTimeMillis() - t2}ms " +
                    "first=${st2?.firstMs ?: -1}ms tokens=${st2?.tokens ?: -1} out2=[$out2]")
                LlamaNative.nativeFreeModel(h)
            } catch (e: Throwable) {
                Log.e(tag, "FAIL 异常 ${e.javaClass.simpleName}: ${e.message}")
            }
        }.start()
    }
}

private enum class Tab(val label: String) {
    CHAT("对话"), RES("资源"), TOOLS("工具箱"), ME("我")
}

/**
 * 二级页：从底栏收进抽屉的低频页 + 「我」页里点开的内容页。
 * 底栏只放 4 个「每天要用」的，其余一律点一下再进 —— 这是国内外大厂的
 * 共同做法，也是 Material 官方 3–5 个顶级入口的上限。
 */
private enum class Overlay(val title: String, val sub: String) {
    MODELS("模型与网关", "本地 GGUF + 云端网关"),
    TASKS("任务进度", "项目整体进度"),
    FLEET("编队节点", "我在编队里的位置"),
    FAVORITES("我的收藏", "收藏过的资源卡片"),
    LIKED("我赞过的", "点过赞的资源卡片"),
    FOCUS("关注主题", "决定信息流里先看到什么"),
    SETTINGS("设置", "会改变行为的开关"),
    DEVICE("设备与权限", "无障碍 / 通知 / 手机入网"),
    ABOUT("关于与版本", "版本与后端信息"),
    IMAGE("本机出图", "手机自己跑 diffusion · 提示词出片"),
    CLOUDDRIVE("网盘聚合", "对等节点 · 集中各网盘"),
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun App() {
    var tab by remember { mutableStateOf(Tab.CHAT) }
    // 一条命令直达二级页 / 自动自检：HyperOS 拦了 adb input（INJECT_EVENTS），
    // 点不了屏，所以留 intent 入口给机主和验收用，跟已有的 speaktest 一个路子。
    //   adb shell am start -n app.skynet.tianwang.next/.MainActivity \
    //       --es tw_page image --ez tw_selftest true
    //   adb shell am start -n app.skynet.tianwang.next/.MainActivity \
    //       --es tw_page image --es tw_prompt "a lighthouse at dawn"
    val launchIntent = (androidx.compose.ui.platform.LocalContext.current
        as? android.app.Activity)?.intent
    val launchImage = launchIntent?.getStringExtra("tw_page") == "image"
    val launchSelftest = launchIntent?.getBooleanExtra("tw_selftest", false) == true
    val launchPrompt = launchIntent?.getStringExtra("tw_prompt") ?: ""
    // 验收用：启动即展开左侧抽屉，便于在不模拟点击的机器上直接截图核对历史布局。
    //   adb shell am start -n app.skynet.tianwang.next/.MainActivity --ez tw_drawer true
    val launchDrawer = launchIntent?.getBooleanExtra("tw_drawer", false) == true
    // 二级页 + 全局左侧抽屉。overlay 非空时顶栏变返回箭头，底栏保持不变。
    var overlay by remember { mutableStateOf<Overlay?>(if (launchImage) Overlay.IMAGE else null) }
    val drawerState = rememberDrawerState(if (launchDrawer) DrawerValue.Open else DrawerValue.Closed)
    // 默认选「这台手机真跑得动」的那个：1.7B 是普适甜点——本机（K70E/天玑8300）
    // 实测 9.8 tok/s，4B 只有 2.9 tok/s（首字 4.2s），明显卡手。
    // 强机（另一台能跑 4B 的）在模型页手动切，不靠这里猜。
    // 顺序：1.7B → 任一就绪 → 第一个占位。
    // 🔴 模型列表不再写死（2026-10-25）：扫目录拿本地 GGUF，问云侧拿网关。
    //    一台没装模型的手机就真的显示「没有模型」，而不是编三个文件名出来。
    val models = remember { mutableStateListOf<ModelProfile>() }
    var current by remember { mutableStateOf<ModelProfile?>(null) }
    // 历史对话直接列在左侧抽屉里，不再从主页面中间弹面板（2026-11-07 主人）。
    val scope = rememberCoroutineScope()
    // 她出不出声。初值从存储读——上次开过的这次还开着，不是每次启动都重置。
    var speakOn by remember { mutableStateOf(Speech.enabled()) }
    // LocalContext 只能在 composable 上下文取，不能挪进 onToggleSpeak 那种普通 lambda
    // （踩过：@Composable invocations can only happen from the context of a @Composable function）
    val ctx = androidx.compose.ui.platform.LocalContext.current

    // 启动后台把首页 20 条的封面 / 摘要预先备好，进资源页直接出图，不等网络。
    // 单独一条协程，不跟下面的模型初始化串行。
    LaunchedEffect(Unit) { runCatching { ResourceApi.prefetchFirstPage() } }
    // 验收用：启动直接落在资源页，方便核对首屏预热效果。
    //   adb shell am start -n app.skynet.tianwang.next/.MainActivity --es tw_tab res
    LaunchedEffect(Unit) {
        if (launchIntent?.getStringExtra("tw_tab") == "res") tab = Tab.RES
    }

    LaunchedEffect(Unit) {
        val local = ModelCatalog.local()
        models.clear()
        models.addAll(local)
        // 默认选本机跑得动又够用的：1.7B 优先（真机实测 9.8 tok/s），
        // 没装就挑最小那个；上次选过的一律沿用上次。
        val remembered = Prefs.lastModelId
        val preferred = local.firstOrNull { it.id.contains("1.7B", true) }
            ?: local.minByOrNull { it.sizeMb }
        current = local.firstOrNull { it.id == remembered } ?: preferred
        // 手机拿本机密钥直连厂商：每一条手动添加的端点都在模型页有一条，选哪条用哪把钥匙。
        val directs = LlmConfig.cloudModels()
        models.addAll(directs)
        if (current == null || current?.id == remembered) {
            current = local.firstOrNull { it.id == remembered }
                ?: preferred ?: directs.firstOrNull()
        }
        // 对等节点是可选加分项：登记过才拉网关，没登记本机直连照常能聊。
        if (Net.hasConfig()) {
            runCatching { Net.getProviders() }.onSuccess { gws ->
                val cloud = ModelCatalog.cloud(gws, Net.loadConfig().token)
                models.addAll(cloud)
                // 🔴 2026-10-31：只有云侧、没装本地 GGUF 的手机会永远卡在「未选模型」。
                //    上面那句 current 只从 local 里挑，local 为空就是 null；云端模型虽然
                //    进了列表，却没人把它设成当前项。实测这台手机就是这样：配置注入成功、
                //    /api/agent 链路也通，顶栏还写着「未选模型 点这里去挑一个」，纯 UI 假故障。
                if (current == null && cloud.isNotEmpty()) {
                    current = cloud.firstOrNull { it.id == remembered } ?: cloud.first()
                }
            }
        }
    }

    // 🔴 每次进「模型」页都重扫目录（2026-10-26）：
    //    模型是 adb push / 删除出来的，进程不重启就永远看不到变化 ——
    //    删掉的文件还挂在列表里点得动，新推的模型要杀进程才出现，这是实测撞到的。
    //    重扫只在没在推理时做，避免把正在加载的模型踢掉。
    LaunchedEffect(overlay) {
        if (overlay != Overlay.MODELS) return@LaunchedEffect
        val local = ModelCatalog.local()
        val ids = local.map { it.id }.toSet()
        // 本地项整段换新；云端项保留（重扫不该把网关丢掉）
        val cloud = models.filter { it.kind != BackendKind.LOCAL }
        models.clear()
        models.addAll(local)
        models.addAll(cloud)
        // 当前选中的模型文件没了 ⇒ 自动落到「跑得动的最大那个」，而不是死抱着一个不存在的
        val cur = current
        if (cur != null && cur.kind == BackendKind.LOCAL && cur.id !in ids) {
            current = local.filter { Dev.plan(App.ctx, it.fileMb, Prefs.mode, it).fits }
                .maxByOrNull { it.sizeMb }
                ?: local.minByOrNull { it.sizeMb }
        }
    }

    // 空对话起步：假对话比空更糟，它让人以为「刚才那个动作真的做过」。
    val msgs = remember { mutableStateListOf<ChatMsg>() }
    // 🔴 历史从磁盘读（2026-10-26）：以前这里是 Demo.sessions = emptyList()，
    //    于是「会话」抽屉永远空、杀进程就全丢。现在落盘，重开还在。
    val sessions = remember { mutableStateListOf<Session>().apply { addAll(SessionStore.load()) } }
    var currentSessionId by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }

    /**
     * 把当前对话写回会话列表并落盘。
     *
     * 每次回答结束后调 —— 不做「退出时统一保存」，因为手机上进程随时会被系统杀掉，
     * 那时候已经来不及了。宁可每个回复都写一次（几 KB 的 JSON，代价可以忽略）。
     */
    fun persist() {
        val live = msgs.filter { !it.streaming && (it.text.isNotBlank() || it.tools.isNotEmpty()) }
        if (live.isEmpty()) return
        val sid = currentSessionId ?: java.util.UUID.randomUUID().toString()
        currentSessionId = sid
        val firstUser = live.firstOrNull { it.role == ChatMsg.Role.USER }?.text.orEmpty()
        val title = firstUser.replace('\n', ' ').trim().take(24).ifBlank { "新会话" }
        val s = Session(
            id = sid,
            title = title,
            msgs = live.toList(),
            modelId = current?.id ?: "",
            at = live.last().let { if (it.at > 0) it.at else System.currentTimeMillis() },
        )
        val i = sessions.indexOfFirst { it.id == sid }
        if (i >= 0) sessions[i] = s else sessions.add(0, s)
        // 最近说话的排最前 —— 抽屉里第一眼就是刚才那段
        val ordered = sessions.sortedByDescending { it.at }
        sessions.clear()
        sessions.addAll(ordered)
        SessionStore.save(ordered)
    }

    fun send(text: String) {
        val model = current
        if (busy || model == null) return
        msgs.add(ChatMsg(role = ChatMsg.Role.USER, text = text))
        val replyId = java.util.UUID.randomUUID().toString()
        msgs.add(ChatMsg(id = replyId, role = ChatMsg.Role.ASSISTANT, streaming = true))
        busy = true
        if (model.kind == BackendKind.LOCAL) {
            // 真本地链路：llama.cpp 已编进 app（libfleet_llama.so），推理全程不出手机。
            scope.launch {
                val started = System.currentTimeMillis()
                val history = msgs.filter { it.id != replyId }   // 含刚发的 user 消息
                val steps = mutableStateListOf<ToolCall>()
                // 通道在线才给本地模型工具协议：没通道时它只会拿到一串「没开」，
                // 白白烧 token，还会把回答带偏。
                val run = if (model.tools && PhoneSelf.connected()) {
                    LocalAgent.run(
                        profile = model,
                        history = history,
                        tools = PhoneMcp.toolList(),
                        allowActions = Prefs.mcpAllowActions,
                        execute = { n, a -> PhoneMcp.executeTool(n, a) },
                        onDelta = { d ->
                            update(msgs, replyId) { it.copy(text = it.text + d) }
                        },
                        onTool = { tc ->
                            val i = steps.indexOfFirst { it.id == tc.id }
                            if (i >= 0) steps[i] = tc else steps.add(tc)
                            update(msgs, replyId) { it.copy(tools = steps.toList()) }
                        },
                    )
                } else {
                    LocalBackend.chat(model, history, onDelta = { d ->
                        update(msgs, replyId) { it.copy(text = it.text + d) }
                    })
                }
                run.onSuccess { full ->
                    val st = LlamaNative.lastStats()
                    val cost = System.currentTimeMillis() - started
                    // 念的是"最终落下那句"：full 为空时答案是流式期间攒的 text
                    val said = full.ifEmpty { msgs.firstOrNull { it.id == replyId }?.text ?: "" }
                    update(msgs, replyId) {
                        it.copy(
                            text = full.ifEmpty { it.text },
                            streaming = false,
                            // 速度不靠感觉：首字 / 总耗时 / tok-s 全部实测
                            stats = st ?: GenStats(-1, cost, 0),
                        )
                    }
                    busy = false
                    persist()
                    Speech.speak(said)
                }.onFailure { e ->
                    update(msgs, replyId) {
                        it.copy(failed = e.message ?: "本地推理失败", streaming = false)
                    }
                    busy = false
                    persist()
                }
            }
        }
        else if (model.id.startsWith("llm-direct:") ||
            (!Net.hasConfig() && LlmConfig.get().ready)) {
            // 手机拿本机密钥直连厂商：不经过电脑，也不需要任何节点在线。
            val profile = if (model.id.startsWith("llm-direct:")) model
            else LlmConfig.cloudModel()
            if (profile == null) {
                update(msgs, replyId) {
                    it.copy(failed = "还没填好大模型 API 配置", streaming = false)
                }
                busy = false
            } else {
                scope.launch {
                    val history = msgs.filter { it.id != replyId }
                    CloudBackend.chat(
                        profile, history,
                        onDelta = { d -> update(msgs, replyId) { it.copy(text = it.text + d) } },
                        onTool = {},
                    ).onSuccess { full ->
                        update(msgs, replyId) {
                            it.copy(text = full.ifEmpty { it.text }, streaming = false)
                        }
                        busy = false
                        persist()
                        Speech.speak(full)
                    }.onFailure { e ->
                        update(msgs, replyId) {
                            it.copy(failed = e.message ?: "直连失败", streaming = false)
                        }
                        busy = false
                    }
                }
            }
        }
        else if (Net.hasConfig()) {
            // 可选：登记过的对等节点，手机 → 节点 SSE → agentloop（工具 + 回答都是真的）
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
                        persist()
                        Speech.speak(ans)
                    },
                    onError = { e ->
                        update(msgs, replyId) { it.copy(failed = e, streaming = false) }
                        busy = false
                        persist()
                    })
            }
        }
        else {
            // 既没把密钥交给手机、也没登记可选节点：说清楚缺的是哪一样
            update(msgs, replyId) {
                it.copy(
                    failed = "去「设置」把大模型 API 密钥交给手机；或登记可选的局域网节点",
                    streaming = false,
                )
            }
            busy = false
        }
    }

    ModalNavigationDrawer(
        drawerState = drawerState,
        drawerContent = {
            ModalDrawerSheet {
                AppDrawer(
                    sessions = sessions,
                    currentSessionId = currentSessionId,
                    onSelect = { s ->
                        // 点历史 = 先保住当前这段，再把那段装进来；抽屉当场收起。
                        persist()
                        msgs.clear()
                        msgs.addAll(s.msgs)
                        currentSessionId = s.id
                        s.modelId.takeIf { it.isNotBlank() }?.let { mid ->
                            models.firstOrNull { it.id == mid }?.let {
                                current = it
                                Prefs.lastModelId = it.id
                            }
                        }
                        tab = Tab.CHAT
                        scope.launch { drawerState.close() }
                    },
                    onNewChat = {
                        scope.launch { drawerState.close() }
                        overlay = null
                        persist()
                        msgs.clear()
                        currentSessionId = null
                        tab = Tab.CHAT
                    },
                    onOpen = { ov ->
                        scope.launch { drawerState.close() }
                        overlay = ov
                    },
                )
            }
        },
    ) {
    Scaffold(
        modifier = Modifier.fillMaxSize().statusBarsPadding(),
        containerColor = MaterialTheme.colorScheme.background,
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(
                            overlay?.title ?: "天网 Next",
                            fontSize = 16.sp, fontWeight = FontWeight.SemiBold,
                            color = MaterialTheme.colorScheme.onSurface,
                        )
                        Text(
                            overlay?.sub ?: when (tab) {
                                Tab.CHAT -> msgs.lastOrNull { it.role == ChatMsg.Role.USER }?.text
                                    ?.take(20) ?: "新对话"
                                Tab.RES -> "按内容分类的信息流"
                                Tab.TOOLS -> "常用动作，点一下就做"
                                Tab.ME -> "我的内容与设置"
                            },
                            fontSize = 11.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            maxLines = 1,
                        )
                    }
                },
                navigationIcon = {
                    val ov = overlay
                    if (ov != null) {
                        IconButton(onClick = { overlay = null }) {
                            Icon(Icons.Filled.ArrowBack, "返回",
                                tint = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    } else {
                        IconButton(onClick = { scope.launch { drawerState.open() } }) {
                            Icon(Icons.Filled.Menu, "菜单",
                                tint = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                },
                actions = {
                    if (tab == Tab.CHAT && overlay == null) {
                        IconButton(onClick = {
                            // 新对话 = 先把当前这段存好再清空。
                            // 旧版只 add 一个空 Session，当前对话直接蒸发（第三条 bug 的另一半）。
                            persist()
                            msgs.clear()
                            currentSessionId = null
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
                                        Tab.RES -> Icons.Filled.Explore
                                        Tab.TOOLS -> Icons.Filled.Build
                                        Tab.ME -> Icons.Filled.Person
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
            when (val ov = overlay) {
                null -> when (tab) {
                Tab.CHAT -> ChatScreen(
                    msgs = msgs,
                    model = current,
                    busy = busy,
                    onPickModel = { overlay = Overlay.MODELS },
                    onSend = { send(it) },
                    onStop = { LocalBackend.abort() },
                    onOpenSettings = { overlay = Overlay.SETTINGS },
                    speakOn = speakOn,
                    onToggleSpeak = {
                        speakOn = Speech.toggle()
                        // 开了却两条路都不通：当场弹出来。否则「开关是开的、就是没声」
                        // 会被当成 bug 查半天。判据用 isUsable() 而不是比对状态字符串——
                        // 状态里带「开 · 云端合成」这类后缀，字符串比较会一直判真。
                        if (speakOn && !Speech.isUsable()) {
                            android.widget.Toast.makeText(ctx, Speech.status(), android.widget.Toast.LENGTH_LONG).show()
                        }
                    },
                )
                Tab.RES -> ResourcesScreen()
                Tab.TOOLS -> ToolsScreen(
                    onNewChat = {
                        persist()
                        msgs.clear()
                        currentSessionId = null
                        tab = Tab.CHAT
                    },
                    onOpenSettings = { overlay = Overlay.SETTINGS },
                    onOpenDevice = { overlay = Overlay.DEVICE },
                    onOpenAbout = { overlay = Overlay.ABOUT },
                    onOpenImage = { overlay = Overlay.IMAGE },
                    speakOn = speakOn,
                    onToggleSpeak = { speakOn = Speech.toggle() },
                    onOpenCloudDrive = { overlay = Overlay.CLOUDDRIVE },
                )
                Tab.ME -> MineScreen(
                    onFavorites = { overlay = Overlay.FAVORITES },
                    onLiked = { overlay = Overlay.LIKED },
                    onFocus = { overlay = Overlay.FOCUS },
                    onSettings = { overlay = Overlay.SETTINGS },
                    onDevice = { overlay = Overlay.DEVICE },
                    onAbout = { overlay = Overlay.ABOUT },
                )
                }
                Overlay.MODELS -> ModelsScreen(models, current?.id ?: "",
                    onPick = {
                        current = it
                        Prefs.lastModelId = it.id
                    },
                    onChanged = {
                        val local = ModelCatalog.local()
                        val cloud = models.filter { it.kind != BackendKind.LOCAL }
                        models.clear()
                        models.addAll(local)
                        models.addAll(cloud)
                    })
                Overlay.TASKS -> TasksScreen()
                Overlay.FLEET -> FleetScreen()
                Overlay.SETTINGS -> SettingsScreen()
                Overlay.DEVICE -> PhoneSelfPane()
                Overlay.FAVORITES -> MineListScreen("favorite")
                Overlay.LIKED -> MineListScreen("like")
                Overlay.FOCUS -> MineFocusScreen()
                Overlay.ABOUT -> MineAboutScreen()
                Overlay.IMAGE -> ImageScreen(
                    autoSelftest = launchSelftest,
                    autoPrompt = launchPrompt,
                )
                Overlay.CLOUDDRIVE -> CloudDriveScreen(onBack = { overlay = null })
            }
        }
    }
    }
}

/**
 * 左侧抽屉（点 ☰ 或侧滑）：历史对话就在侧边，不再从主页面中间弹面板。
 *
 * 参考主流对话 App 的排法：顶部「新对话」+ 搜索，中间按今天 / 昨天 / 7 天内
 * 分组的历史列表，点一条直接切过去并收起抽屉；低频页放列表末尾跟着滚，
 * 不占固定高度，历史再少也不会把列表挤没。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AppDrawer(
    sessions: List<Session>,
    currentSessionId: String?,
    onSelect: (Session) -> Unit,
    onNewChat: () -> Unit,
    onOpen: (Overlay) -> Unit,
) {
    var query by remember { mutableStateOf("") }
    Column(
        Modifier.fillMaxWidth()
            .statusBarsPadding()
            .navigationBarsPadding()
            .padding(horizontal = 12.dp, vertical = 14.dp),
    ) {
        Text("天网 Next", fontSize = 17.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Text("本机节点 · 历史对话", fontSize = 11.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(14.dp))

        // 新对话置顶：主流 AI App 的第一入口，不用再点进二级页。
        NavigationDrawerItem(
            label = { Text("新对话", fontSize = 13.sp, fontWeight = FontWeight.SemiBold) },
            selected = false,
            onClick = onNewChat,
            icon = { Icon(Icons.Filled.Add, null, modifier = Modifier.size(20.dp)) },
            modifier = Modifier.padding(bottom = 8.dp),
        )

        // 搜索：标题 + 消息正文都搜（历史多了以后，翻是翻不动的）
        Box(
            Modifier.fillMaxWidth()
                .clip(RoundedCornerShape(22.dp))
                .background(MaterialTheme.colorScheme.surfaceVariant)
                .padding(horizontal = 14.dp, vertical = 10.dp),
        ) {
            if (query.isEmpty()) {
                Text("搜索对话…", fontSize = 13.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            BasicTextField(
                value = query,
                onValueChange = { query = it },
                singleLine = true,
                textStyle = TextStyle(
                    fontSize = 13.sp,
                    color = MaterialTheme.colorScheme.onSurface),
                cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
                modifier = Modifier.fillMaxWidth(),
            )
        }
        Spacer(Modifier.height(10.dp))

        val q = query.trim()
        val visible = if (q.isEmpty()) sessions else sessions.filter { s ->
            s.title.contains(q, true) || s.msgs.any { it.text.contains(q, true) }
        }
        LazyColumn(
            Modifier.weight(1f),
            verticalArrangement = Arrangement.spacedBy(2.dp),
        ) {
            item { DrawerGroup("历史对话 · ${sessions.size} 段") }
            when {
                sessions.isEmpty() -> item {
                    Text("还没有历史 —— 说过的话会自动存下来，重开 App 也还在。",
                        fontSize = 11.5.sp, lineHeight = 17.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(start = 8.dp, top = 6.dp))
                }
                visible.isEmpty() -> item {
                    Text("没搜到「$q」。", fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(start = 8.dp, top = 6.dp))
                }
                else -> items(historyRows(visible), key = { it.key }) { row ->
                    val h = row.header
                    if (h != null) {
                        Text(h, fontSize = 11.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(start = 8.dp, top = 12.dp, bottom = 2.dp))
                    } else {
                        val s = row.session!!
                        val active = s.id == currentSessionId
                        NavigationDrawerItem(
                            label = {
                                Text(s.title, fontSize = 13.sp, maxLines = 1,
                                    color = if (active) MaterialTheme.colorScheme.primary
                                    else MaterialTheme.colorScheme.onSurface)
                            },
                            selected = active,
                            onClick = { onSelect(s) },
                            icon = {
                                Icon(Icons.Filled.ChatBubbleOutline, null,
                                    modifier = Modifier.size(18.dp))
                            },
                            badge = {
                                if (active) Text("当前", fontSize = 10.sp,
                                    color = MaterialTheme.colorScheme.primary)
                            },
                            modifier = Modifier.padding(bottom = 2.dp),
                        )
                    }
                }
            }
            item {
                Spacer(Modifier.height(14.dp))
                DrawerGroup("从底栏收进来")
            }
            item { DrawerItem(Icons.Filled.Memory, "模型与网关", "本地 GGUF / 云侧") { onOpen(Overlay.MODELS) } }
            item { DrawerItem(Icons.Filled.Assignment, "任务进度", "项目整体进度") { onOpen(Overlay.TASKS) } }
            item { DrawerItem(Icons.Filled.Devices, "编队节点", "我在编队里的位置") { onOpen(Overlay.FLEET) } }
            item { DrawerGroup("设备") }
            item { DrawerItem(Icons.Filled.PhoneAndroid, "设备与权限", "授权与入网") { onOpen(Overlay.DEVICE) } }
            item { Spacer(Modifier.height(16.dp)) }
        }
    }
}

@Composable
private fun DrawerGroup(t: String) {
    Text(t, fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(start = 8.dp, bottom = 6.dp))
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DrawerItem(icon: ImageVector, label: String, sub: String, onClick: () -> Unit) {
    NavigationDrawerItem(
        label = { Text(label, fontSize = 13.sp) },
        selected = false,
        onClick = onClick,
        icon = { Icon(icon, null, modifier = Modifier.size(20.dp)) },
        badge = {
            if (sub.isNotBlank()) {
                Text(sub, fontSize = 10.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        },
        modifier = Modifier.padding(bottom = 4.dp),
    )
}

/** 原地改列表里某条消息。原型用它模拟流式；真链路也走同一个入口。 */
private fun update(list: MutableList<ChatMsg>, id: String, f: (ChatMsg) -> ChatMsg) {
    val i = list.indexOfFirst { it.id == id }
    if (i >= 0) list[i] = f(list[i])
}

/** 抽屉里的一行：要么是组头（小灰字），要么是一条会话。 */
private data class HistoryRow(val key: String, val header: String?, val session: Session?)

/**
 * 会话按时间分组（2026-10-26 主人给的参考排版）：
 * 今天 / 昨天 / 7 天内，再往后按月（2026年9月）。组内保持时间倒序。
 *
 * 之前的排版是每条都带卡片底色和「N 条 · 时间」元信息，历史一多就糊成一片；
 * 参考主流对话 App 的历史列表：组头小灰字、条目只有标题，干净得多。
 */
private fun historyRows(list: List<Session>): List<HistoryRow> {
    val rows = ArrayList<HistoryRow>(list.size + 4)
    var last = ""
    val nowMid = midnight(System.currentTimeMillis())
    for (s in list) {   // 传入前已按 at 降序
        val diff = if (s.at <= 0) Long.MAX_VALUE else (nowMid - midnight(s.at)) / 86_400_000L
        val g = when {
            diff <= 0L -> "今天"
            diff == 1L -> "昨天"
            diff <= 7L -> "7 天内"
            else -> {
                val t = java.util.Calendar.getInstance().apply { timeInMillis = s.at }
                "%d年%d月".format(
                    t.get(java.util.Calendar.YEAR),
                    t.get(java.util.Calendar.MONTH) + 1)
            }
        }
        if (g != last) {
            rows.add(HistoryRow(key = "h-${rows.size}-$g", header = g, session = null))
            last = g
        }
        rows.add(HistoryRow(key = "s-${s.id}", header = null, session = s))
    }
    return rows
}

/** 当天零点：分组按「天」算，不按 24 小时滚窗 —— 昨天就是昨天，不看几点。 */
private fun midnight(at: Long): Long =
    java.util.Calendar.getInstance().apply {
        timeInMillis = at
        set(java.util.Calendar.HOUR_OF_DAY, 0)
        set(java.util.Calendar.MINUTE, 0)
        set(java.util.Calendar.SECOND, 0)
        set(java.util.Calendar.MILLISECOND, 0)
    }.timeInMillis

package app.skynet.tianwang.next

import android.content.Context
import android.content.Intent
import android.provider.Settings
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
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 首次使用引导 + 能力就绪清单。
 *
 * 为什么要有：这台手机装了 64 个工具、3 个常驻服务、7 类权限，但装完直接落到
 * 四个 Tab —— 人不知道该开什么、也不知道哪些能力现在根本调不动。
 * 对照 DSH Go 的产品形态（欢迎 → 运行时 → 连接 → 权限 → 完成，带进度），
 * 补的是「让一堆能力在手机上真的可用」这一层，不是再加一个能力。
 *
 * 纪律：
 * - 每一步的「可用/待授权」只读现成的 [Perms.snapshot]，不另造一套权限判定。
 * - 可选步骤永远不当成门槛（对等直连、外部 MCP、Shizuku 都是加分项）。
 * - 跳过也算走完，不反复拦人。
 */
data class ReadyStep(
    val id: String,
    val title: String,
    val desc: String,
    val ready: Boolean,
    val optional: Boolean,
    val next: String,
    /** 系统设置页标识；空表示只需在 App 内自行前往。 */
    val setting: String = "",
)

object ReadyCheck {
    const val SET_ACCESSIBILITY = "accessibility"
    const val SET_NOTIFICATION = "notification"

    /**
     * 纯函数：环境状态全由参数传入，不碰 Android，方便以后单独验各种组合。
     * [required] 只数非可选步骤，避免「没接第二台手机」被算成没配好。
     */
    fun steps(
        env: PermEnv,
        hasModel: Boolean,
        mcpConfigured: Boolean,
        externalMcp: Int,
    ): List<ReadyStep> = listOf(
        ReadyStep(
            "model", "能开口：一个能用的模型", "本机 GGUF，或自己登记的云端 / 厂商直连端点",
            hasModel, false, "进「模型与网关」挑一个；没装模型就真显示没有，不编文件名出来",
        ),
        ReadyStep(
            "ui", "能动手：读屏 / 点击 / 输入", "走无障碍通道，全程不出本机",
            env.accessibilityConnected, false,
            if (env.accessibilityEnabled) "已授权但服务没连上：退出设置页等 1–2 秒或重开 App"
            else "系统设置 → 无障碍 → 天网 Next",
            SET_ACCESSIBILITY,
        ),
        ReadyStep(
            "notif", "能读通知：验证码 / 消息", "通知使用权，缺了就如实报「没开」",
            env.notificationGranted, true,
            "系统设置 → 通知使用权 → 勾选「天网 Next」", SET_NOTIFICATION,
        ),
        ReadyStep(
            "lan", "对等直连：手机之间互调", "自己起服务端，不经过任何中心节点",
            env.lanRunning, true, "进「设备与权限」开直连，或用 self.lan.start",
        ),
        ReadyStep(
            "mcp", "远程入网：/pair 中继", "手机与 PC 各自出站连，中继只盲转发字节",
            mcpConfigured, true, "进「设备与权限 → 入网设置」填中继地址 / 房间 / 密钥",
        ),
        ReadyStep(
            "fed", "外部 MCP 联邦", "把别处的工具接进本机工具面",
            externalMcp > 0, true, "进「设备与权限」或工具箱添加外部 MCP 服务器",
        ),
        ReadyStep(
            "shizuku", "进阶通道：Shizuku / 设备所有者", "shell、静默授权、系统策略",
            env.shizukuAuthorized || env.deviceOwner, true,
            "装 Shizuku 并授权；设备所有者需一条 adb 命令，见 self.owner.provision",
        ),
    )

    /** 就绪百分比只按必做项算，且不算「跳过」的项。 */
    fun percent(steps: List<ReadyStep>): Int {
        val required = steps.filter { !it.optional }
        if (required.isEmpty()) return 100
        return required.count { it.ready } * 100 / required.size
    }

    /** 把标识翻成系统设置 Intent；返回 null 表示这一步要在 App 内自己走。 */
    fun settingsIntent(setting: String): Intent? = when (setting) {
        SET_ACCESSIBILITY -> Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
        SET_NOTIFICATION -> Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
        else -> null
    }
}

/** 本机是不是已经有能跑的模型：本地 GGUF + 自己登记的直连端点，都不依赖别人开机。 */
internal fun onboardingHasModel(): Boolean = runCatching {
    ModelCatalog.local().isNotEmpty() || LlmConfig.cloudModels().isNotEmpty()
}.getOrDefault(false)

@Composable
fun OnboardingScreen(onDone: () -> Unit) {
    val ctx = LocalContext.current
    var page by remember { mutableStateOf(0) }
    var env by remember { mutableStateOf(Perms.snapshot(ctx)) }
    var steps by remember { mutableStateOf(emptyList<ReadyStep>()) }
    var message by remember { mutableStateOf("") }

    // 每次回到清单页重读一次真机状态：从系统设置返回时，勾过的权限要立刻显出来。
    LaunchedEffect(page) {
        if (page == 1) {
            env = Perms.snapshot(ctx)
            steps = ReadyCheck.steps(
                env = env,
                hasModel = onboardingHasModel(),
                mcpConfigured = Prefs.mcpConfigured(),
                externalMcp = runCatching { McpServers.cachedTools().size }.getOrDefault(0),
            )
        }
    }

    Column(
        Modifier.fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .padding(horizontal = 20.dp, vertical = 26.dp),
    ) {
        Text("天网 Next", fontSize = 22.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Text("先把这台手机配成能用的对等节点", fontSize = 12.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(16.dp))
        ProgressStrip(page = page, total = 3)
        Spacer(Modifier.height(18.dp))

        when (page) {
            0 -> WelcomePage(
                onStart = { page = 1 },
                onSkip = { Prefs.onboardingDone = true; onDone() },
            )
            1 -> ChecklistPage(
                ctx = ctx, steps = steps, percent = ReadyCheck.percent(steps),
                message = message,
                onRefresh = {
                    env = Perms.snapshot(ctx)
                    steps = ReadyCheck.steps(
                        env = env,
                        hasModel = onboardingHasModel(),
                        mcpConfigured = Prefs.mcpConfigured(),
                        externalMcp = runCatching { McpServers.cachedTools().size }.getOrDefault(0),
                    )
                },
                onOpenSetting = { setting ->
                    val i = ReadyCheck.settingsIntent(setting)
                    if (i != null) {
                        runCatching { ctx.startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
                            .onFailure { message = "打不开系统设置：${it.message ?: ""}" }
                    } else {
                        message = "这一步在 App 里完成：${steps.firstOrNull { it.setting == setting }?.next ?: ""}"
                    }
                },
                onNext = { page = 2 },
            )
            else -> DonePage(
                steps = steps,
                onEnter = { Prefs.onboardingDone = true; onDone() },
            )
        }
    }
}

@Composable
private fun ProgressStrip(page: Int, total: Int) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(5.dp)) {
        for (i in 0 until total) {
            Box(
                Modifier.weight(1f).height(4.dp)
                    .clip(RoundedCornerShape(2.dp))
                    .background(
                        if (i <= page) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.surfaceVariant,
                    ),
            )
        }
    }
}

@Composable
private fun WelcomePage(onStart: () -> Unit, onSkip: () -> Unit) {
    Column(Modifier.fillMaxSize()) {
        Text("这台手机自己就是完整节点", fontSize = 18.sp,
            fontWeight = FontWeight.SemiBold, color = MaterialTheme.colorScheme.onSurface)
        Spacer(Modifier.height(14.dp))
        for ((t, d) in listOf(
            "本机就能跑" to "本地 GGUF 推理、资源库、出图、无障碍操控都在手机里，不用等电脑开机。",
            "联网是为了对等" to "局域网直连、/pair 中继、外部 MCP 让节点之间互相调用；没有任何一台是中心。",
            "权限由你给" to "默认只读。操作类能力要你显式打开；安全模式可一键停掉全部远控。",
        )) {
            Column(
                Modifier.fillMaxWidth().padding(bottom = 12.dp)
                    .clip(RoundedCornerShape(10.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .padding(13.dp),
            ) {
                Text(t, fontSize = 14.sp, fontWeight = FontWeight.Medium,
                    color = MaterialTheme.colorScheme.onSurface)
                Spacer(Modifier.height(4.dp))
                Text(d, fontSize = 11.5.sp, lineHeight = 17.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        Spacer(Modifier.weight(1f))
        Button(onClick = onStart, modifier = Modifier.fillMaxWidth()) {
            Text("看一下这台手机还差什么")
        }
        TextButton(onClick = onSkip, modifier = Modifier.fillMaxWidth()) {
            Text("跳过，直接用")
        }
    }
}

@Composable
private fun ChecklistPage(
    ctx: Context,
    steps: List<ReadyStep>,
    percent: Int,
    message: String,
    onRefresh: () -> Unit,
    onOpenSetting: (String) -> Unit,
    onNext: () -> Unit,
) {
    val readyCount = steps.count { it.ready }
    Column(Modifier.fillMaxSize()) {
        Text("能力就绪 $readyCount / ${steps.size} · 必做项 $percent%",
            fontSize = 16.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Text("必做的只有「能开口」和「能动手」；其余都是加分项，缺了不影响本机照常用。",
            fontSize = 11.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(10.dp))
        if (message.isNotBlank()) {
            Text(message, fontSize = 11.sp, color = MaterialTheme.colorScheme.error)
            Spacer(Modifier.height(6.dp))
        }
        LazyColumn(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(7.dp)) {
            items(steps, key = { it.id }) { s ->
                StepCard(s, onClick = {
                    if (!s.ready && s.setting.isNotBlank()) onOpenSetting(s.setting)
                })
            }
            item {
                TextButton(onClick = onRefresh, modifier = Modifier.fillMaxWidth()) {
                    Text("重新检测（从系统设置回来点这里）")
                }
            }
        }
        Button(onClick = onNext, modifier = Modifier.fillMaxWidth()) { Text("完成设置") }
    }
}

@Composable
private fun StepCard(s: ReadyStep, onClick: () -> Unit) {
    val tone = when {
        s.ready -> MaterialTheme.colorScheme.primary
        s.optional -> MaterialTheme.colorScheme.onSurfaceVariant
        else -> MaterialTheme.colorScheme.error
    }
    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surface)
            .clickable(enabled = !s.ready && s.setting.isNotBlank(), onClick = onClick)
            .padding(12.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                Modifier.size(7.dp).clip(RoundedCornerShape(4.dp)).background(tone),
            )
            Spacer(Modifier.width(9.dp))
            Text(s.title, fontSize = 13.5.sp, fontWeight = FontWeight.Medium,
                color = MaterialTheme.colorScheme.onSurface, modifier = Modifier.weight(1f))
            Text(
                when {
                    s.ready -> "可用"
                    s.optional -> "可选"
                    else -> "待开通"
                },
                fontSize = 10.5.sp, color = tone,
            )
        }
        Spacer(Modifier.height(4.dp))
        Text(s.desc, fontSize = 11.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        if (!s.ready) {
            Spacer(Modifier.height(3.dp))
            Text("→ ${s.next}", fontSize = 10.5.sp, lineHeight = 15.sp, color = tone)
        }
    }
}

@Composable
private fun DonePage(steps: List<ReadyStep>, onEnter: () -> Unit) {
    val ready = steps.count { it.ready }
    val pending = steps.filter { !it.ready && !it.optional }
    Column(Modifier.fillMaxSize()) {
        Text("配好了，开工", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Spacer(Modifier.height(10.dp))
        Text("当前 $ready / ${steps.size} 项可用。剩下的随时能在抽屉里补，不会拦着你用。",
            fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(12.dp))
        if (pending.isNotEmpty()) {
            Text("还差这些必做项：", fontSize = 12.sp,
                color = MaterialTheme.colorScheme.onSurface)
            for (s in pending) {
                Text("· ${s.title} → ${s.next}", fontSize = 11.sp, lineHeight = 17.sp,
                    color = MaterialTheme.colorScheme.error)
            }
        } else {
            Text("必做项已全部就绪。", fontSize = 12.sp,
                color = MaterialTheme.colorScheme.primary)
        }
        Spacer(Modifier.height(12.dp))
        Text("想再看这份清单：抽屉 →「首次引导」。想一键停掉所有远控：抽屉 →「安全模式」。",
            fontSize = 11.sp, lineHeight = 17.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.weight(1f))
        Button(onClick = onEnter, modifier = Modifier.fillMaxWidth()) { Text("进入天网") }
    }
}

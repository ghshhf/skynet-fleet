package app.skynet.tianwang.next

import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 设置族：对照 DSH Go 拆得很细的设置面 —— interface / privacy / notification /
 * voice / media / performance / reply_prefs / language / safe_mode / background_guide。
 *
 * 天网原来只有一个 SettingsScreen 大杂烩。这里按「一组开关 = 一页」拆开，
 * 每档开关都落 [KB]，页面之间不互相覆盖。开关只改本机标记，不谎称联动到
 * 系统权限或云端（缺权限的项在 sub 里直说下一步）。
 */

/** 一个开关项：key 存 KB，label/sub 给人看。 */
data class ToggleSpec(val key: String, val label: String, val sub: String, val def: Boolean = false)

object SettingGroups {
    val interfacePage = listOf(
        ToggleSpec("ui_fluid", "流式动画", "回复逐字出现时的过渡", true),
        ToggleSpec("ui_codewrap", "代码块自动换行", "长代码不横向滚动"),
        ToggleSpec("ui_process_fold", "折叠过程", "把工具调用过程收成一行，对照 DSH Go 的 turn_process", true),
        ToggleSpec("ui_thinking", "显示思考摘要", "在回复上方留一条 thinking pill"),
        ToggleSpec("ui_clock", "显示实时时钟", "顶栏显示当前时间"),
        ToggleSpec("ui_density", "紧凑布局", "横屏/平板时收紧留白"),
    )
    val privacyPage = listOf(
        ToggleSpec("priv_stats", "匿名使用统计", "默认关闭；采集范围见帮助页", false),
        ToggleSpec("priv_mask", "隐藏敏感值", "回包里的 key/token 打码", true),
        ToggleSpec("priv_clip", "允许读剪贴板", "关掉后不再读系统剪贴板", false),
        ToggleSpec("priv_notif_read", "允许读通知", "需要通知使用权；缺权去「设备与权限」", false),
    )
    val notifyPage = listOf(
        ToggleSpec("ntf_done", "任务完成提醒", "一轮回复跑完时提醒", true),
        ToggleSpec("ntf_ask", "需要确认时提醒", "工具卡等你点头时提醒", true),
        ToggleSpec("ntf_fail", "失败提醒", "执行失败或断线时提醒", true),
        ToggleSpec("ntf_bg", "后台常驻提示", "前台服务被系统限制时提示"),
    )
    val voicePage = listOf(
        ToggleSpec("voi_input", "语音输入", "长按说话转文字"),
        ToggleSpec("voi_auto", "自动朗读回复", "回复完成后念出来"),
        ToggleSpec("voi_local", "优先本地识别", "有本地模型时先走本机"),
    )
    val mediaPage = listOf(
        ToggleSpec("med_preview", "图片预览", "分享/生成的图先看再发", true),
        ToggleSpec("med_autosave", "自动保存图片", "把收到的图落到本机目录"),
        ToggleSpec("med_compress", "压缩再传", "大图先压后传，省流量", true),
    )
    val performancePage = listOf(
        ToggleSpec("perf_low", "低占用优先", "少起常驻、少叠并发", true),
        ToggleSpec("perf_prefetch", "资源预取", "Wi-Fi 下提前取下一步要用的资源"),
        ToggleSpec("perf_gpufallback", "GPU 失败自动回 CPU", "本地推理显卡不可用时降级", true),
        ToggleSpec("perf_anim", "高质量动效", "关掉可降低老机型卡顿", true),
    )
    val replyPrefs = listOf(
        ToggleSpec("rp_nothink", "默认不展开思考", "新会话默认关闭思考链"),
        ToggleSpec("rp_zh", "默认中文回复", "跟随系统语言之外固定中文", true),
        ToggleSpec("rp_cite", "回复带来源", "联网回答附出处"),
    )
    val safeMode = listOf(
        ToggleSpec("safe_readonly", "全局只读", "所有写操作一律拦下，对照 DSH Go 的 read_only_bar"),
        ToggleSpec("safe_stopwatcher", "停止监视器", "异常时一键停下远控，对照 phone_stop_watcher", true),
        ToggleSpec("safe_confirm3", "三档确认", "敏感动作默认交还给人，对照 hold_to_confirm", true),
    )
    val webMode = listOf(
        ToggleSpec("web_local", "本机网页模式", "在 App 内打开本机网页版，对照 local_web_screen", true),
        ToggleSpec("web_guard", "网页跳转守卫", "外链先问再跳，对照 web_view_guard", true),
        ToggleSpec("web_remember", "记住上次模式", "下次打开接着上次用的原生/网页"),
    )
}

/** 通用开关页：一个 spec 列表 = 一页，改动即时写 KB。 */
@Composable
fun ToggleGroupScreen(title: String, sub: String, items: List<ToggleSpec>) {
    var tick by remember { mutableStateOf(0) }
    val state = remember(tick) {
        items.associate { it.key to (KB.str("set_" + it.key, if (it.def) "1" else "0") == "1") }
    }
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item {
            FeatureHeader(title, sub)
            Spacer(Modifier.height(8.dp))
        }
        item {
            FeatureCard {
                items.forEach { s ->
                    FeatureToggle(s.label, s.sub, state[s.key] ?: s.def) { v ->
                        KB.setStr("set_" + s.key, if (v) "1" else "0")
                        tick++
                    }
                }
            }
            Spacer(Modifier.height(10.dp))
        }
        item {
            Text("开关只写本机配置。涉及系统权限的项（通知/剪贴板/悬浮）需在「设备与权限」里另行授权，这里不代申请。",
                fontSize = 10.5.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(20.dp))
        }
    }
}

@Composable fun InterfaceSettingsScreen() = ToggleGroupScreen(
    "界面与交互", "流式、代码块、过程折叠、思考摘要等显示偏好。", SettingGroups.interfacePage)
@Composable fun PrivacySettingsScreen() = ToggleGroupScreen(
    "隐私与分享", "统计、打码、剪贴板与通知读取，默认最保守。", SettingGroups.privacyPage)
@Composable fun NotificationSettingsScreen() = ToggleGroupScreen(
    "通知设置", "哪类事件该提醒你，逐档开关。", SettingGroups.notifyPage)
@Composable fun VoiceSettingsScreen() = ToggleGroupScreen(
    "语音设置", "语音输入、朗读、本地识别优先。", SettingGroups.voicePage)
@Composable fun MediaSettingsScreen() = ToggleGroupScreen(
    "媒体设置", "图片/视频的预览、保存与压缩策略。", SettingGroups.mediaPage)
@Composable fun PerformanceSettingsScreen() = ToggleGroupScreen(
    "性能", "低占用优先、预取、GPU 回退与动效档位。", SettingGroups.performancePage)
@Composable fun ReplyPrefsScreen() = ToggleGroupScreen(
    "回复偏好", "思考链、语言、引用来源的默认行为。", SettingGroups.replyPrefs)
@Composable fun SafeExtrasScreen() = ToggleGroupScreen(
    "安全附加", "只读、停止监视器、三档确认等兜底开关。", SettingGroups.safeMode)
@Composable fun WebModeScreen() = ToggleGroupScreen(
    "网页模式", "本机网页版与外链守卫，对照 DSH Go 的原生/网页一键切换。", SettingGroups.webMode)

/** 语言：单选，写 KB。对照 DSH Go 的「中文，也说 English」。 */
@Composable
fun LanguageScreen() {
    var lang by remember { mutableStateOf(KB.str("set_lang", "zh")) }
    val options = listOf("zh" to "简体中文", "en" to "English", "auto" to "跟随系统")
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("语言", "界面语言，默认跟随系统。切换后立即写本机配置。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureCard {
                options.forEach { (k, label) ->
                    FeatureActions((if (lang == k) "● " else "○ ") + label to {
                        lang = k; KB.setStr("set_lang", k)
                    })
                }
                Spacer(Modifier.height(4.dp))
                Text("当前：${options.firstOrNull { it.first == lang }?.second ?: "简体中文"}",
                    fontSize = 11.sp, color = MaterialTheme.colorScheme.primary)
            }
            Spacer(Modifier.height(20.dp))
        }
    }
}

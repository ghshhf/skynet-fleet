package app.skynet.tianwang.next

import androidx.compose.foundation.layout.Column
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
 * 桌宠：对照 DSH Go 的「Deepy 和鲸娘，陪你干活」（敲代码 / 完成 / 等你确认 / 睡觉）。
 *
 * 它是产品形态的一部分，不是装饰：干活时给状态、要确认时举手、App 外也能跟到桌面。
 * 天网的做法是把「状态台词 + 形象 + 是否悬浮」做成可配置项，落到本机；
 * 悬浮窗需要 SYSTEM_ALERT_WINDOW，本轮只记录开关与状态，不申请系统权限、不常驻进程。
 *
 * 状态由 [EventCenter] 那类本机事件驱动会更活，先给可手动切换的四态与台词编辑，
 * 后续接事件总线即可复用同一套形象。
 */
object PetConfig {
    const val K_ENABLED = "pet_enabled"
    const val K_SKIN = "pet_skin"
    const val K_OVERLAY = "pet_overlay"
    const val K_LINE_WORK = "pet_line_work"
    const val K_LINE_DONE = "pet_line_done"
    const val K_LINE_ASK = "pet_line_ask"
    const val K_LINE_SLEEP = "pet_line_sleep"

    val skins = listOf("whale" to "鲸娘", "deepy" to "Deepy", "minimal" to "极简光点")

    /** 四态：key 存台词、label 显示、def 是没改过时的默认台词。 */
    data class PetState(val key: String, val label: String, val def: String)

    val states = listOf(
        PetState(K_LINE_WORK, "干活中", "我在敲，稍等一下～"),
        PetState(K_LINE_DONE, "已完成", "搞定啦，来看看结果！"),
        PetState(K_LINE_ASK, "等你确认", "这步要你点头哦"),
        PetState(K_LINE_SLEEP, "待机", "没事我就先眯一会儿…"),
    )

    fun line(key: String): String {
        val def = states.firstOrNull { it.key == key }?.def ?: ""
        return KB.str(key, def)
    }
}

@Composable
fun PetsScreen() {
    var enabled by remember { mutableStateOf(KB.str(PetConfig.K_ENABLED, "0") == "1") }
    var overlay by remember { mutableStateOf(KB.str(PetConfig.K_OVERLAY, "0") == "1") }
    var skin by remember { mutableStateOf(KB.str(PetConfig.K_SKIN, "whale")) }
    var state by remember { mutableStateOf(PetConfig.K_LINE_WORK) }
    var tick by remember { mutableStateOf(0) }
    val lines = remember(tick) {
        PetConfig.states.associate { it.key to KB.str(it.key, it.def) }
    }

    LazyColumn(
        Modifier.fillMaxSize().padding(14.dp),
    ) {
        item {
            FeatureHeader("桌宠", "陪你干活：给状态、要确认时举手、待机时安静。形象/台词/悬浮都能改，配置只存本机。")
            Spacer(Modifier.height(8.dp))
        }
        item {
            FeatureCard {
                FeatureToggle("启用桌宠", "关掉后所有状态展示与悬浮都不再出现", enabled) {
                    enabled = it; KB.setStr(PetConfig.K_ENABLED, if (it) "1" else "0")
                }
                FeatureToggle("App 外显示", "需要悬浮窗权限；本轮只记开关，不主动申请系统权限", overlay) {
                    overlay = it; KB.setStr(PetConfig.K_OVERLAY, if (it) "1" else "0")
                }
            }
            Spacer(Modifier.height(10.dp))
        }
        item {
            FeatureSection("形象")
            FeatureCard {
                PetConfig.skins.forEach { (key, label) ->
                    FeatureActions(label to {
                        skin = key; KB.setStr(PetConfig.K_SKIN, key)
                    })
                }
                Spacer(Modifier.height(4.dp))
                Text("当前：${PetConfig.skins.firstOrNull { it.first == skin }?.second ?: "鲸娘"}",
                    fontSize = 11.sp, color = MaterialTheme.colorScheme.primary)
            }
            Spacer(Modifier.height(10.dp))
        }
        item {
            FeatureSection("状态与台词")
            Text("点状态切到它，台词改完即时存。预览跟着当前状态变。",
                fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(6.dp))
        }
        PetConfig.states.forEach { st ->
            item {
                FeatureCard(highlighted = state == st.key, onClick = { state = st.key }) {
                    Text(st.label, fontSize = 12.5.sp,
                        color = if (state == st.key) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurface)
                    Spacer(Modifier.height(4.dp))
                    var text by remember(st.key) { mutableStateOf(lines[st.key] ?: st.def) }
                    FeatureField(
                        value = text,
                        onChange = {
                            text = it
                            KB.setStr(st.key, it)
                        },
                        label = "${st.label} 时说什么",
                        singleLine = false,
                    )
                }
                Spacer(Modifier.height(8.dp))
            }
        }
        item {
            FeatureSection("预览")
            FeatureCard {
                val skinName = PetConfig.skins.firstOrNull { it.first == skin }?.second ?: "鲸娘"
                val label = PetConfig.states.firstOrNull { it.key == state }?.label ?: "干活中"
                FeatureKV("形象", skinName)
                FeatureKV("状态", label)
                Spacer(Modifier.height(6.dp))
                Column {
                    Text("「 ${lines[state].orEmpty()} 」", fontSize = 13.sp,
                        color = MaterialTheme.colorScheme.onSurface)
                }
                Spacer(Modifier.height(4.dp))
                Text(if (enabled) "桌宠已启用${if (overlay) " · 悬浮开关已打开（待授权）" else ""}"
                else "桌宠当前未启用",
                    fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.height(20.dp))
        }
    }
}

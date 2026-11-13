package app.skynet.tianwang.next

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
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 外观：主题色 + 深浅模式。
 *
 * 对照 DSH Go 的「六套主题色 / 跟随壁纸 / 自选颜色 / 四款桌面图标」：
 * 它把外观当成产品的一部分，而不是开发时写死在代码里的常量。
 * 天网原来 [TianwangTheme] 只能「跟随系统深浅」，颜色一套钉死（青）。
 *
 * 纪律：只换 [ColorScheme] 的语义槽位，不动业务状态色（[StatusColors] 里的
 * 运行/成功/失败各自有含义，不能跟着主题变色，否则「成功」会变成别的颜色）。
 * 桌面图标涉及 launcher 别名与重启桌面，本轮不做 —— 不为了凑数加半成品。
 */
enum class Accent(
    val key: String,
    val label: String,
    val color: Color,
    val container: Color,
) {
    CYAN("cyan", "青", Color(0xFF4DD0E1), Color(0xFF1F3A40)),
    BLUE("blue", "蓝", Color(0xFF64B5F6), Color(0xFF1B3049)),
    PURPLE("purple", "紫", Color(0xFFB388FF), Color(0xFF2E2145)),
    GREEN("green", "绿", Color(0xFF66BB6A), Color(0xFF1D3A25)),
    ORANGE("orange", "橙", Color(0xFFFFB74D), Color(0xFF42301A)),
    ROSE("rose", "玫", Color(0xFFF06292), Color(0xFF42203A)),
}

object Appearance {
    const val MODE_SYSTEM = 0
    const val MODE_DARK = 1
    const val MODE_LIGHT = 2

    /**
     * 主题存在 SharedPreferences 里，但 SharedPreferences 不会触发 Compose 重组。
     * 这个计数器是给 [TianwangTheme] 订阅用的：改一次颜色就 +1，界面立刻换，
     * 不用杀进程重开（那样「六套主题色」看着像假的）。
     */
    var revision by mutableStateOf(0)
        private set

    fun current(): Accent =
        Accent.entries.firstOrNull { it.key == Prefs.accentKey } ?: Accent.CYAN

    fun set(a: Accent) {
        Prefs.accentKey = a.key
        revision++
    }

    fun mode(): Int = Prefs.darkMode

    fun setMode(m: Int) {
        Prefs.darkMode = m
        revision++
    }

    fun useDark(systemDark: Boolean): Boolean = when (Prefs.darkMode) {
        MODE_DARK -> true
        MODE_LIGHT -> false
        else -> systemDark
    }

    /** 在既有两套色板上只改主色相关槽位，其余布局色保持不变。 */
    fun scheme(dark: Boolean): ColorScheme {
        // 读一次 revision，让调用方（TianwangTheme）订阅到设置变化。
        @Suppress("UNUSED_VARIABLE") val rev = revision
        val a = current()
        val base = if (dark) DarkScheme else LightScheme
        return base.copy(
            primary = a.color,
            onPrimary = if (dark) Color(0xFF00201F) else Color.White,
            primaryContainer = a.container,
            onPrimaryContainer = a.color,
            secondary = a.color,
        )
    }
}

@Composable
fun AppearanceScreen() {
    var accent by remember { mutableStateOf(Appearance.current()) }
    var mode by remember { mutableStateOf(Appearance.mode()) }

    LazyColumn(
        Modifier.fillMaxSize().padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            Text("外观", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
                color = MaterialTheme.colorScheme.onSurface)
            Text("六套主题色 + 深浅模式。只换主色，不改运行/成功/失败这三种业务状态色。",
                fontSize = 11.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        item {
            Text("主题色", fontSize = 12.5.sp, fontWeight = FontWeight.Medium,
                color = MaterialTheme.colorScheme.onSurface)
            Spacer(Modifier.height(7.dp))
            for (row in Accent.entries.chunked(3)) {
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    for (a in row) {
                        AccentChip(
                            a = a,
                            selected = a == accent,
                            modifier = Modifier.weight(1f),
                            onClick = {
                                accent = a
                                Appearance.set(a)
                            },
                        )
                    }
                    // 最后一行不满 3 个时补空位，避免剩余卡片被拉宽。
                    repeat(3 - row.size) { Spacer(Modifier.weight(1f)) }
                }
                Spacer(Modifier.height(8.dp))
            }
        }
        item {
            Text("深浅", fontSize = 12.5.sp, fontWeight = FontWeight.Medium,
                color = MaterialTheme.colorScheme.onSurface)
            Spacer(Modifier.height(7.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf(
                    Appearance.MODE_SYSTEM to "跟随系统",
                    Appearance.MODE_DARK to "始终深色",
                    Appearance.MODE_LIGHT to "始终浅色",
                ).forEach { (m, label) ->
                    Button(
                        onClick = { mode = m; Appearance.setMode(m) },
                        modifier = Modifier.weight(1f),
                        colors = if (mode == m) ButtonDefaults.buttonColors()
                        else ButtonDefaults.buttonColors(
                            containerColor = MaterialTheme.colorScheme.surfaceVariant,
                            contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
                        ),
                    ) { Text(label, fontSize = 11.sp) }
                }
            }
        }
        item {
            Spacer(Modifier.height(4.dp))
            Text("预览", fontSize = 12.5.sp, fontWeight = FontWeight.Medium,
                color = MaterialTheme.colorScheme.onSurface)
            Column(
                Modifier.fillMaxWidth()
                    .clip(RoundedCornerShape(10.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .padding(13.dp),
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(
                        Modifier.size(14.dp).clip(RoundedCornerShape(4.dp))
                            .background(accent.color),
                    )
                    Spacer(Modifier.size(8.dp))
                    Text("当前：${accent.label}", fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurface)
                }
                Spacer(Modifier.height(8.dp))
                Box(
                    Modifier.fillMaxWidth()
                        .clip(RoundedCornerShape(8.dp))
                        .background(accent.container)
                        .padding(10.dp),
                ) {
                    Text("主色用在按钮、选中态与状态强调上", fontSize = 11.5.sp,
                        color = accent.color)
                }
            }
        }
        item {
            Text("切换后立即生效，设置存在手机本地。桌面图标样式涉及 launcher 别名，本轮不做。",
                fontSize = 10.5.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun AccentChip(
    a: Accent,
    selected: Boolean,
    modifier: Modifier,
    onClick: () -> Unit,
) {
    Column(
        modifier
            .clip(RoundedCornerShape(9.dp))
            .background(MaterialTheme.colorScheme.surface)
            .border(
                width = if (selected) 1.5.dp else 1.dp,
                color = if (selected) a.color else MaterialTheme.colorScheme.outline,
                shape = RoundedCornerShape(9.dp),
            )
            .clickable(onClick = onClick)
            .padding(10.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Box(
            Modifier.size(26.dp).clip(RoundedCornerShape(7.dp)).background(a.color),
        )
        Spacer(Modifier.height(6.dp))
        Text(a.label, fontSize = 11.sp,
            color = if (selected) a.color else MaterialTheme.colorScheme.onSurface)
        if (selected) Text("已选", fontSize = 9.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

package app.skynet.tianwang.next

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

/**
 * 配色：深色是主场景（天网本来就常在夜里跑），浅色只是跟随系统时的兜底。
 *
 * 为什么颜色写死在代码里、不读 `values/colors.xml`：
 * 两套色板要在 Compose 里互相引用（气泡底 = 面板底再亮一档），写 XML 就得来回跳；
 * 而且 Material3 的 scheme 本身就是 Kotlin 对象，硬拆成资源反而多一层。
 */

private val Ink = Color(0xFF0E1116)          // 全局底
private val Panel = Color(0xFF161B22)        // 卡片/面板
private val PanelUp = Color(0xFF1E252E)      // 面板上再浮一层（气泡、输入框）
private val Line = Color(0xFF2A323C)         // 分隔线
private val Cyan = Color(0xFF4DD0E1)         // 主色（状态、强调）
private val CyanDim = Color(0xFF1F3A40)
private val Amber = Color(0xFFE8B44A)        // 工具运行中
private val Red = Color(0xFFE05A5A)          // 失败
private val Green = Color(0xFF57C08A)        // 成功
private val TextMain = Color(0xFFE6EDF3)
private val TextDim = Color(0xFF8B98A5)

private val DarkScheme = darkColorScheme(
    primary = Cyan,
    onPrimary = Color(0xFF00201F),
    primaryContainer = CyanDim,
    onPrimaryContainer = Cyan,
    secondary = Amber,
    onSecondary = Color(0xFF2A1E00),
    background = Ink,
    onBackground = TextMain,
    surface = Panel,
    onSurface = TextMain,
    surfaceVariant = PanelUp,
    onSurfaceVariant = TextDim,
    outline = Line,
    outlineVariant = Line,
    error = Red,
    onError = Color.White,
)

private val LightScheme = lightColorScheme(
    primary = Color(0xFF00707C),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFB8ECF2),
    onPrimaryContainer = Color(0xFF00201F),
    background = Color(0xFFF7F9FA),
    onBackground = Color(0xFF14181D),
    surface = Color.White,
    onSurface = Color(0xFF14181D),
    surfaceVariant = Color(0xFFEDF1F4),
    onSurfaceVariant = Color(0xFF5A6570),
    outline = Color(0xFFD2D9DF),
    error = Color(0xFFB3261E),
)

/** 语义色。Compose 的 ColorScheme 槽位是有限的，工具状态这种业务色单列一份。 */
object StatusColors {
    val running = Amber
    val ok = Green
    val fail = Red
    val idle = TextDim
}

val MonoStyle = TextStyle(
    fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace,
    fontSize = 12.sp,
    lineHeight = 17.sp,
    color = TextMain,
)

private val AppTypography = Typography(
    titleLarge = TextStyle(fontSize = 19.sp, fontWeight = FontWeight.SemiBold),
    titleMedium = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold),
    bodyLarge = TextStyle(fontSize = 15.sp, lineHeight = 22.sp),
    bodyMedium = TextStyle(fontSize = 13.5.sp, lineHeight = 20.sp),
    labelSmall = TextStyle(fontSize = 11.sp, fontWeight = FontWeight.Medium),
)

@Composable
fun TianwangTheme(dark: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    // 原型阶段：默认走深色（见上），跟随系统只在系统为亮色时切浅色。
    MaterialTheme(
        colorScheme = if (dark) DarkScheme else LightScheme,
        typography = AppTypography,
        content = content,
    )
}

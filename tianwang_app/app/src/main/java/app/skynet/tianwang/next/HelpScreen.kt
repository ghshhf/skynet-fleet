package app.skynet.tianwang.next

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 内置使用指南：离线可查，答案只写这台 App 里真实存在的行为。
 *
 * 对照 DSH Go 的 docs/guide（12 篇用户指南）：产品做到一定体量之后，
 * 「功能在哪、为什么这样」必须留在手机里，不能只写在仓库 README。
 * 天网原来关于页只有四行版本信息。
 *
 * 纪律：每条答案都能指回一个真实入口或代码事实；不写「以后会支持」，
 * 不写没实现的承诺（桌面图标、原生插件卡、常驻视听都没做，这里就不提）。
 */
object HelpBook {
    data class Q(val q: String, val a: String, val where: String)

    val items: List<Q> = listOf(
        Q(
            "必须开着电脑才能用吗？",
            "不用。本地 GGUF 推理、资源库 SQLite、本机出图、无障碍读屏点击都在手机里跑；" +
                "手机自己还会起局域网服务端。别的节点只是可选共享来源，调不到照常可用。",
            "模型与网关 / 资源 / 工具箱",
        ),
        Q(
            "为什么强调联网？",
            "对等节点之间要互相调用：局域网直连、/pair 中继、外部 MCP 都要网络。" +
                "「不依赖某台电脑先开机」和「不联网」是两回事，别搞混。",
            "设备与权限 / 全部能力",
        ),
        Q(
            "怎么让 AI 读屏、点按、输入？",
            "开启无障碍服务；默认只读，操作类工具还要在入网设置里打开「允许操作」。" +
                "缺权时工具会直接报「没开」并给下一步，不会重试或伪造结果。",
            "设备与权限 / 权限矩阵",
        ),
        Q(
            "入网 /pair 是什么？",
            "手机和 PC 各自出站连同一个中继，配对后中继只盲转发字节，不依赖 adb/VPN/sshd。" +
                "三处配置（地址 / 房间 / 密钥）必须一致，密钥只存本机。",
            "设备与权限 → 入网设置",
        ),
        Q(
            "两台手机怎么直连？",
            "self.lan.start 起本机服务端，对端用 self.lan.call 直调；口令即授权。" +
                "再配 DDNS，IPv6 换前缀后对端用域名仍能找到你。",
            "全部能力 → 局域网直连 / 动态域名",
        ),
        Q(
            "外部 MCP 是干什么的？",
            "把别处 MCP 服务器的工具接进本机工具面，工具表和本机能力共用一份，" +
                "不会各说各话。失败会隔离并退避 60 秒。",
            "工具箱 / 全部能力 → 外部 MCP",
        ),
        Q(
            "怕别人远程操作这台手机怎么办？",
            "顶栏盾牌 → 安全模式。一键断开入网、停局域网服务与前台壳、停 DDNS、收回动作权限，" +
                "并把结果逐条列出来。总闸不会自动恢复，必须手动关。",
            "安全模式",
        ),
        Q(
            "诊断包会不会泄露密钥？",
            "只收集状态，不读会话正文、资源内容、网盘目录；导出前整段脱敏，" +
                "中继地址 / 房间 / token / 密钥 / 长十六进制串一律抹掉，可先在页面里核一遍。",
            "诊断与导出",
        ),
        Q(
            "聊天记录能带走吗？",
            "会话自动存在手机本地，重开 App 也还在。对话回放页能只读重看、复制或分享整段文字；" +
                "导出同样过脱敏，且不含图片文件内容。",
            "对话回放",
        ),
        Q(
            "别的 App 分享过来的文字在哪？",
            "系统分享里选「天网 Next」，内容进分享收件箱，只存在手机本地。" +
                "不会自动发给模型、不会自动联网，由你决定复制 / 转发 / 删除。",
            "分享收件箱",
        ),
        Q(
            "这台手机到底会哪些能力？",
            "全部能力页直接读 tools/list，按域摊开，能搜名字和说明、看参数、" +
                "复制工具名；会改状态的工具单独标红。",
            "全部能力 / 权限矩阵",
        ),
        Q(
            "没装模型会怎样？",
            "模型页会真的显示「没有模型」，不会编三个文件名出来。" +
                "可以推一个 GGUF 到本机模型目录，或在模型页登记云端 / 厂商直连端点。",
            "模型与网关",
        ),
        Q(
            "主题能换吗？",
            "六套主题色 + 跟随系统 / 始终深 / 始终浅，切换立即生效，设置存本机。" +
                "运行 / 成功 / 失败三种业务状态色不跟着变，免得含义被改掉。桌面图标样式本轮没有。",
            "外观",
        ),
        Q(
            "第一次装完不知道该开什么？",
            "首次引导会把「能开口」「能动手」列为必做，其余都标成可选加分项；" +
                "从系统设置回来点「重新检测」就刷新。跳过也算走完，不会每次启动都拦。",
            "首次引导",
        ),
    )

    fun search(q: String): List<Q> {
        val s = q.trim()
        if (s.isEmpty()) return items
        return items.filter { it.q.contains(s, true) || it.a.contains(s, true) || it.where.contains(s, true) }
    }
}

@Composable
fun HelpScreen() {
    var q by remember { mutableStateOf("") }
    var opened by remember { mutableStateOf("") }
    val list = HelpBook.search(q)

    Column(Modifier.fillMaxSize().padding(14.dp)) {
        Text("使用指南", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Text("离线可查，只写这台 App 里真实存在的行为。共 ${HelpBook.items.size} 条。",
            fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(10.dp))
        BasicTextField(
            value = q,
            onValueChange = { q = it },
            singleLine = true,
            textStyle = TextStyle(fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurface),
            cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
            modifier = Modifier.fillMaxWidth()
                .clip(RoundedCornerShape(9.dp))
                .background(MaterialTheme.colorScheme.surface)
                .padding(horizontal = 12.dp, vertical = 10.dp),
            decorationBox = { inner ->
                if (q.isEmpty()) Text("搜问题，例如 密钥 / 直连 / 主题",
                    fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                inner()
            },
        )
        Spacer(Modifier.height(8.dp))
        Text("命中 ${list.size} 条", fontSize = 10.5.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(4.dp))
        LazyColumn(verticalArrangement = Arrangement.spacedBy(6.dp)) {
            items(list, key = { it.q }) { item ->
                val open = opened == item.q
                Column(
                    Modifier.fillMaxWidth()
                        .clip(RoundedCornerShape(9.dp))
                        .background(MaterialTheme.colorScheme.surface)
                        .clickable { opened = if (open) "" else item.q }
                        .padding(11.dp),
                ) {
                    Row {
                        Text(item.q, fontSize = 12.5.sp, fontWeight = FontWeight.Medium,
                            color = MaterialTheme.colorScheme.onSurface,
                            modifier = Modifier.weight(1f))
                        Text(if (open) "−" else "+", fontSize = 14.sp,
                            color = MaterialTheme.colorScheme.primary)
                    }
                    if (open) {
                        Spacer(Modifier.height(6.dp))
                        Text(item.a, fontSize = 11.5.sp, lineHeight = 17.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Spacer(Modifier.height(5.dp))
                        Text("入口：${item.where}", fontSize = 10.5.sp,
                            color = MaterialTheme.colorScheme.primary)
                    }
                }
            }
        }
    }
}

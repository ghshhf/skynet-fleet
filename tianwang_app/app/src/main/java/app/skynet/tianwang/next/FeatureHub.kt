package app.skynet.tianwang.next

import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 能力工作台：把「对照 DSH Go 补齐的一批产品模块」按板块集中到一个入口。
 *
 * 为什么要有这一页：底栏只放 4 个常用 Tab，二级页藏在抽屉里，加得越多越难找。
 * 这一页是它们的目录 —— 分板块列出、标一句定位，点一下直接进对应 Overlay。
 * 对照 DSH Go 的 all_features_page / feature_catalog：功能多了要有索引，不是堆完就完。
 */
data class HubEntry(val label: String, val sub: String, val overlay: String)
data class HubGroup(val title: String, val entries: List<HubEntry>)

object FeatureHub {
    // overlay 字符串与 MainActivity 的 Overlay 名对应，跳转在调用处用 onOpen 接。
    val groups = listOf(
        HubGroup("代码与运行时（自包含 Linux）", listOf(
            HubEntry("代码工作台", "写简单代码 · 跑脚本 · 看输出", "WORKBENCH"),
            HubEntry("小应用工坊", "手机里写 HTML/JS 应用并本机运行", "MINIAPP"),
            HubEntry("自主任务", "手机自己按步骤跑完一组动作", "TASKAGENT"),
            HubEntry("工作区文件", "本地代码 / 产物 / 导出给对端", "WORKSPACE"),
            HubEntry("Linux 运行时", "Alpine 用户态 · PRoot · 安装与状态", "RUNTIME"),
            HubEntry("运行历史", "每次执行的回执、回看与重跑", "CODEHISTORY"),
            HubEntry("代码编辑器", "行号着色 · 查找替换 · 多文件", "CODEEDITOR"),
            HubEntry("会话终端", "连续命令 · 历史回溯 · 快捷条", "SETERMINAL"),
            HubEntry("工具链自检", "逐条查可用命令 · apk 安装", "TOOLCHAIN"),
            HubEntry("项目脚手架", "静态站点 / Python / Node / Shell 模板", "SCAFFOLD"),
        )),
        HubGroup("陪伴与角色", listOf(
            HubEntry("桌宠", "形象 / 四态台词 / 悬浮开关", "PETS"),
            HubEntry("酒馆 · 角色卡", "角色名 / 设定 / 开场白", "COL_CHARACTERS"),
            HubEntry("酒馆 · 世界书", "关键词触发的背景设定", "COL_LOREBOOKS"),
            HubEntry("酒馆 · 正则脚本", "文本替换规则", "COL_REGEX"),
            HubEntry("快捷回复", "常用话术一键插入", "COL_QUICK_REPLIES"),
        )),
        HubGroup("智能体与协作", listOf(
            HubEntry("Bot 与智能体", "名字 / 人设 / 模型 / 启停", "COL_BOTS"),
            HubEntry("群聊房间", "多 Bot 协作的房间", "COL_ROOMS"),
            HubEntry("Agent 团队", "席位 / 职责 / 上级", "COL_TEAM"),
            HubEntry("技能与预设", "Agent 预设 / 斜杠命令", "COL_SKILLS"),
            HubEntry("插件与工具卡", "已装 / 可用的原生工具卡", "COL_PLUGINS"),
        )),
        HubGroup("任务与自动化", listOf(
            HubEntry("日程与定时任务", "cron + 动作登记（不自动执行）", "COL_SCHEDULES"),
            HubEntry("作业队列", "长任务登记与状态", "COL_JOBS"),
            HubEntry("应用规则", "手机操控的分级 allow/ask/deny", "COL_RULES"),
            HubEntry("工作区", "目录登记与用途", "COL_WORKSPACES"),
            HubEntry("运行记录", "长跑输入与结果", "COL_RUNS"),
            HubEntry("检查报告", "审查结论与建议", "COL_REPORTS"),
        )),
        HubGroup("会话与内容", listOf(
            HubEntry("会话文件夹", "会话归类", "COL_FOLDERS"),
            HubEntry("书签", "长对话位置标记", "COL_BOOKMARKS"),
            HubEntry("对话分支", "从某条消息分叉", "COL_BRANCHES"),
            HubEntry("回复预设", "提示词 / 参数组合", "COL_PRESETS"),
            HubEntry("人格面具", "说话风格切换", "COL_PERSONAS"),
            HubEntry("社区论坛", "本地发帖 / 看帖", "COL_FORUM"),
            HubEntry("分享链接", "托管链接登记", "COL_LINKS"),
            HubEntry("分享卡", "一段结论做成纯文本卡片", "SHARECARD"),
            HubEntry("分享码", "配置编解码", "SHARECODE"),
            HubEntry("终端", "手机 shell 命令 + 历史", "TERMINAL"),
            HubEntry("终端快捷命令", "常用命令一键填", "TERMKEYS"),
        )),
        HubGroup("接入与系统", listOf(
            HubEntry("模型提供方", "API 地址登记（不存密钥）", "COL_PROVIDERS"),
            HubEntry("订阅与用量", "套餐与额度", "COL_SUBSCRIPTIONS"),
            HubEntry("网络详情", "地址 / 直连 / DDNS", "NETDETAIL"),
            HubEntry("权限引导", "通道与缺权明细", "ACCESSWALK"),
            HubEntry("验证", "口令自检", "VERIFY"),
            HubEntry("许可与致谢", "开源组件清单", "CREDITS"),
            HubEntry("更新", "版本与本次变更", "UPDATE"),
        )),
        HubGroup("对话增强", listOf(
            HubEntry("斜杠命令", "/ 命令面板 + 自定义", "SLASH"),
            HubEntry("消息快捷操作", "长按消息能做什么", "MSGACTIONS"),
            HubEntry("目标与待办", "本轮目标 + 待办勾选", "GOALTODO"),
            HubEntry("富文本渲染预览", "代码块/表格/数学/diff", "RENDER"),
            HubEntry("过程轨迹", "动作序列逐步看", "TRAJECTORY"),
            HubEntry("产物", "链接与报告聚合", "ARTIFACT"),
        )),
        HubGroup("交互工具", listOf(
            HubEntry("骰子", "真随机数 + 历史", "DICE"),
            HubEntry("故事书", "逐页翻看", "STORYBOOK"),
            HubEntry("论坛 · 发帖与搜索", "写帖 + 过滤", "FORUMCOMPOSE"),
            HubEntry("设置搜索", "一次搜遍开关与模块", "SETSEARCH"),
            HubEntry("快捷面板", "下拉快捷项定制", "QUICKPANEL"),
        )),
        HubGroup("设置族", listOf(
            HubEntry("界面与交互", "流式 / 代码块 / 过程折叠", "SET_INTERFACE"),
            HubEntry("隐私与分享", "统计 / 打码 / 读取权限", "SET_PRIVACY"),
            HubEntry("通知设置", "完成 / 确认 / 失败提醒", "SET_NOTIFY"),
            HubEntry("语音设置", "输入 / 朗读 / 本地优先", "SET_VOICE"),
            HubEntry("媒体设置", "预览 / 保存 / 压缩", "SET_MEDIA"),
            HubEntry("性能", "低占用 / 预取 / GPU 回退", "SET_PERF"),
            HubEntry("回复偏好", "思考 / 语言 / 引用", "SET_REPLY"),
            HubEntry("安全附加", "只读 / 停止监视 / 三档确认", "SET_SAFE"),
            HubEntry("网页模式", "本机网页版 + 外链守卫", "SET_WEB"),
            HubEntry("语言", "中文 / English / 跟随系统", "LANG"),
        )),
        HubGroup("信息", listOf(
            HubEntry("账户与本机", "身份卡 + 本机名", "ACCOUNT"),
            HubEntry("更新日志", "这一版加了什么", "CHANGELOG"),
            HubEntry("用量与状态", "工具数 / 名册 / 直连快照", "USAGE"),
        )),
        HubGroup("手机实体工具", listOf(
            HubEntry("系统体检", "CPU/内存/电池/存储/屏幕", "SYSINFO"),
            HubEntry("文件管理器", "浏览 / 查看 / 删除 / 复制路径", "FILEMAN"),
            HubEntry("网络诊断", "连通性 / 网关 / DNS / 接口", "NETDIAG"),
            HubEntry("进程查看", "运行中进程 · 按内存排序 · 结束", "PROC"),
            HubEntry("剪贴板", "读取 / 写入 / 本地历史", "CLIPBOARD"),
            HubEntry("本机日志", "logcat 抓取 / 过滤 / 保存", "LOGCAT"),
            HubEntry("应用列表", "已装第三方应用 · 点一下复制包名", "APPLIST"),
            HubEntry("命令收藏", "常用 shell 命令本机存储 · 一键执行", "CMDFAV"),
            HubEntry("存储分析", "目录体积 / 文件数 · 清本应用缓存", "STORAGE"),
            HubEntry("本机属性", "getprop 构建 / 运行环境 / 网络", "PROPS"),
            HubEntry("传感器清单", "机身传感器类型 / 供应商 / 量程", "SENSORS"),
            HubEntry("电量与电池", "电量 / 健康 / 温度 / 电压", "BATTERY"),
            HubEntry("网络流量", "/proc/net/dev 各接口收发", "TRAFFIC"),
            HubEntry("磁盘与挂载", "df -h 各分区用量", "DISK"),
            HubEntry("字体清单", "/system/fonts 字体文件", "FONTS"),
            HubEntry("应用权限自查", "本应用危险权限授予状态", "PERMS"),
        )),
    )

    val count: Int get() = groups.sumOf { it.entries.size }
}

/**
 * @param onOpen 传入 Overlay 名字符串，由 MainActivity 映射到枚举。
 */
@Composable
fun FeatureHubScreen(onOpen: (String) -> Unit) {
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item {
            FeatureHeader("能力工作台",
                "按板块索引这台手机新增的 ${FeatureHub.count} 个功能模块。对照 DSH Go 的 all_features：功能多了要有目录，不是堆完就算。")
            Spacer(Modifier.height(6.dp))
            Text("每一点都能进去增删改，数据只存本机。", fontSize = 10.5.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(10.dp))
        }
        FeatureHub.groups.forEach { g ->
            item {
                FeatureSection("${g.title}（${g.entries.size}）")
            }
            g.entries.chunked(2).forEach { row ->
                item {
                    androidx.compose.foundation.layout.Row(
                        Modifier.fillMaxWidth(),
                        horizontalArrangement = androidx.compose.foundation.layout.Arrangement.spacedBy(8.dp),
                    ) {
                        row.forEach { e ->
                            androidx.compose.foundation.layout.Box(Modifier.weight(1f)) {
                                FeatureCard(onClick = { onOpen(e.overlay) }) {
                                    Text(e.label, fontSize = 12.5.sp,
                                        color = MaterialTheme.colorScheme.onSurface)
                                    Spacer(Modifier.height(3.dp))
                                    Text(e.sub, fontSize = 10.sp, lineHeight = 14.sp,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                                }
                            }
                        }
                        if (row.size == 1) {
                            Spacer(Modifier.weight(1f))
                        }
                    }
                    Spacer(Modifier.height(8.dp))
                }
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}

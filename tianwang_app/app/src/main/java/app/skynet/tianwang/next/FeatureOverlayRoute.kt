package app.skynet.tianwang.next

import androidx.compose.runtime.Composable

/**
 * 成批补齐模块的集中路由。
 *
 * MainActivity 的 Overlay 是本文件 private，这里不引用它的类型，只按枚举名
 * 字符串分发 —— 加一个模块 = 枚举加一项 + 这里加一个分支，MainActivity 的
 * when 不用再动。onOpen(String) 让 [FeatureHubScreen] 的目录卡片能跳到任意模块。
 */
@Composable
fun FeatureOverlayRoute(name: String, onOpen: (String) -> Unit) {
    when (name) {
        "HUB" -> FeatureHubScreen(onOpen)
        "PETS" -> PetsScreen()
        "TERMINAL" -> TerminalScreen()
        "SLASH" -> SlashPaletteScreen()
        "MSGACTIONS" -> MessageActionsScreen()
        "GOALTODO" -> GoalTodoScreen()
        "SHARECARD" -> ShareCardScreen()
        "ACCOUNT" -> AccountScreen()
        "CHANGELOG" -> ChangelogScreen()
        "USAGE" -> UsageScreen()
        "LANG" -> LanguageScreen()

        "COL_BOTS" -> CollectionScreen(Collections.bots)
        "COL_ROOMS" -> CollectionScreen(Collections.rooms)
        "COL_TEAM" -> CollectionScreen(Collections.team)
        "COL_PLUGINS" -> CollectionScreen(Collections.plugins)
        "COL_SKILLS" -> CollectionScreen(Collections.skills)
        "COL_SCHEDULES" -> CollectionScreen(Collections.schedules)
        "COL_JOBS" -> CollectionScreen(Collections.jobs)
        "COL_FORUM" -> CollectionScreen(Collections.forum)
        "COL_CHARACTERS" -> CollectionScreen(Collections.characters)
        "COL_LOREBOOKS" -> CollectionScreen(Collections.lorebooks)
        "COL_REGEX" -> CollectionScreen(Collections.regex)
        "COL_QUICK_REPLIES" -> CollectionScreen(Collections.quickReplies)
        "COL_RULES" -> CollectionScreen(Collections.rules)
        "COL_LINKS" -> CollectionScreen(Collections.links)
        "COL_WORKSPACES" -> CollectionScreen(Collections.workspaces)

        "SET_INTERFACE" -> InterfaceSettingsScreen()
        "SET_PRIVACY" -> PrivacySettingsScreen()
        "SET_NOTIFY" -> NotificationSettingsScreen()
        "SET_VOICE" -> VoiceSettingsScreen()
        "SET_MEDIA" -> MediaSettingsScreen()
        "SET_PERF" -> PerformanceSettingsScreen()
        "SET_REPLY" -> ReplyPrefsScreen()
        "SET_SAFE" -> SafeExtrasScreen()
        "SET_WEB" -> WebModeScreen()

        "COL_FOLDERS" -> CollectionScreen(Collections.folders)
        "COL_BOOKMARKS" -> CollectionScreen(Collections.bookmarks)
        "COL_BRANCHES" -> CollectionScreen(Collections.branches)
        "COL_PRESETS" -> CollectionScreen(Collections.presets)
        "COL_PERSONAS" -> CollectionScreen(Collections.personas)
        "COL_PROVIDERS" -> CollectionScreen(Collections.providers)
        "COL_SUBSCRIPTIONS" -> CollectionScreen(Collections.subscriptions)
        "COL_REPORTS" -> CollectionScreen(Collections.reports)
        "COL_RUNS" -> CollectionScreen(Collections.runs)
        "SHARECODE" -> ShareCodeScreen()
        "NETDETAIL" -> NetworkDetailScreen()
        "ACCESSWALK" -> AccessWalkScreen()
        "VERIFY" -> VerifyScreen()
        "TERMKEYS" -> TerminalKeysScreen()
        "CREDITS" -> CreditsScreen()
        "UPDATE" -> UpdateScreen()

        "RENDER" -> RenderShowcaseScreen()
        "TRAJECTORY" -> TrajectoryScreen()
        "ARTIFACT" -> ArtifactScreen()
        "DICE" -> DiceScreen()
        "STORYBOOK" -> StorybookScreen()
        "FORUMCOMPOSE" -> ForumComposerScreen()
        "SETSEARCH" -> SettingsSearchScreen()
        "QUICKPANEL" -> QuickPanelScreen()

        // 第四批：自包含代码运行时
        "WORKBENCH" -> CodeWorkbenchScreen(onOpen)
        "MINIAPP" -> MiniAppScreen()
        "TASKAGENT" -> TaskAgentScreen(onOpen)
        "WORKSPACE" -> WorkspaceScreen(onOpen)
        "RUNTIME" -> RuntimeSetupScreen()
        "CODEHISTORY" -> CodeHistoryScreen(onOpen)

        // 第五批：IDE 层
        "CODEEDITOR" -> CodeEditorScreen(onOpen)
        "SETERMINAL" -> SessionTerminalScreen()
        "TOOLCHAIN" -> ToolchainScreen(onOpen)
        "SCAFFOLD" -> ScaffoldScreen(onOpen)

        // 未知名字不白屏：退回工作台目录，人还能从这再点进去。
        else -> FeatureHubScreen(onOpen)
    }
}

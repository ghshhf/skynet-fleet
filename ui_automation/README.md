# 能力提案：`ui_automation`（无障碍跳广告 / 通用 UI 自动化）

> 分支 `feat/accessibility-adskip` · 源自 `docs/李跳跳拆解.md`
> 状态：提案 + 最小实现（未合入 main）

## 0. 一句话

把李跳跳的「规则驱动无障碍点击」抽象成编队的一个**节点能力** `ui_automation`，并补上它缺的两块：**规则共享中继**（不再靠单个作者维护就停更）和**视觉兜底**（图标无文字时截图问 AI）。

## 1. 为什么做（不是复刻，是补短板）

李跳跳的架构是对的（无障碍节点树 + 规则匹配 + 点击），死在**分发模型**：规则静态、作者 2023 年停更、目标 App 持续变形 → 规则全过期。编队能补：

| 李跳跳短板 | 本能力补法 |
|---|---|
| 规则静态、停更即废 | 规则存 L3 共享上下文 `adskip.rules`，带版本与回滚；任一设备更新，全编队即时生效 |
| 单机各填各的 | 第三原则：手机/Commander 反向推送规则与引擎到 Android 节点 |
| 图标无文字抓不到 | **视觉兜底**：匹配失败时截图 → 远程 AI 节点（`rfc/remote-ai-service`）识别关闭按钮 → 回坐标点击 |
| 无远端算力 | 匹配本身极轻；只有视觉兜底才调算力，且走已有 `pick_node` 路由 |

## 2. 两种落地形态（按设备能力选）

1. **Phone-native**：Android 设备上装一个薄 AccessibilityService App，规则从编队拉、点击在自己进程内完成。优点：不依赖 adb、后台常驻、无需电脑。代价：要维护一个极小的 Android App。
2. **Host-adb**（本分支最小实现，Windows-first 立即可用）：编队里的一台 Windows/PC 节点通过 `adb`（USB 或无线调试）操作 Android 设备：`uiautomator dump` 拿节点树 → 规则匹配 → `input tap` 点击。优点：零 Android 开发，复用现有 Windows 节点与 adb 工具箱（第三原则）。

> 建议先跑 ②（能立刻验证），验证规则中继价值后再做 ①（体验更好）。

## 3. 规则格式（兼容李跳跳，扩两个字段）

```json
{
  "version": 3,
  "rules": [
    {
      "packageName": "com.xxx.video",
      "activity": "*.SplashActivity",
      "triggers": [
        {"name": "跳过广告", "action": "click",
         "text": ["跳过广告", "跳过", "5s后跳过"],
         "textRegex": "跳过|skip|关闭广告",
         "desc": ["关闭"],
         "resourceId": ["img_close"],
         "needVision": false},
        {"name": "图标关闭", "action": "click",
         "resourceId": [], "needVision": true}
      ]
    }
  ]
}
```

新增：`textRegex`（正则兜底文案漂移）、`needVision`（标了就走视觉兜底）。

## 4. 流程

```
拉规则(L3 adskip.rules, 带版本)
   │
adb shell uiautomator dump → /sdcard/window.xml → pull → 解析节点树
   │
按 packageName 取规则 → 依次匹配 text / textRegex / desc / resourceId
   │ 命中 → adb shell input tap <bounds 中心>
   │ 未命中 且 needVision → 截图 → AI 节点识别关闭按钮 → tap
   ▼
命中即停；多轮重试（开屏广告常有 2-3 秒延迟出现）
```

## 5. 最小实现

`ui_automation.py`（本目录）：
- `dump_nodes(serial)` — adb uiautomator dump + pull + XML 解析
- `match(nodes, rule)` — text/regex/desc/resourceId 匹配，返回 bounds 中心
- `tap(serial, x, y)` — adb input tap
- `skip_ad(serial, package, rules, ...)` — 多轮重试主循环
- `vision_fallback(serial, prompt)` — 截图→远程 AI 节点→坐标（钩子，需 AI 节点在线）

## 6. 合规边界

- 仅本人设备、本人账号；用户主动授予无障碍/adb 调试权限才生效。
- 不绕过付费、不用于刷量/抢票/他人设备。
- 默认不启用，作为可选能力注册。

## 7. 与主线关系

主线没有 UI 自动化这一层；这是**新增能力维度**，不动主干。它天然复用已有的：L3 共享上下文（存规则）、远程 AI（视觉兜底）、能力注册表（声明 `ui_automation`）。

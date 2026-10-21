# 图形版视觉验收工具（uicheck）

开发期工具，**不进产品**（产品是纯手写 HTML/CSS/JS，零构建、断网可用）。
本目录只是给「界面长什么样」提供**可判定**的证据。

## 为什么需要它

以前验收界面靠人眼描述 + 一张拍出来的图，结果是：
- 改完只能「感觉比上次好」，**没有回归基线** ⇒ 改 A 弄坏 B 查不出来；
- 「看起来清楚」不等于「对比度达标」（暗色主题尤其容易翻车）；
- 「点了没反应」靠记忆判断 ⇒ 死链会悄悄回来。

## 三个脚本

| 脚本 | 干什么 | 命令 | 退出码 |
|---|---|---|---|
| `shot.js` | Playwright + Chromium 以 1440×900 **真渲染**截图；可选与基线**像素级**对比 | `node tools/uicheck/shot.js --out baseline`<br>`node tools/uicheck/shot.js --out current --diff` | 0 通过 / 1 超阈值或报错 |
| `a11y.js` | axe-core 扫 WCAG 2.0/2.1 A+AA，报**真实对比度比值** | `node tools/uicheck/a11y.js --theme light` | 0 无 serious / 1 有 |
| `clickaudit.js` | 真浏览器里**点一遍每个可点元素**，找出点了没反应的（死链） | `node tools/uicheck/clickaudit.js --json out.json` | 0 无死链 / 1 有 |

## 依赖

`node_modules` 在仓库外（`D:/tools/uitools/node_modules`），本目录做了软链接。
浏览器内核在 `C:/Users/admin/AppData/Local/ms-playwright/`。
换机器要重装：

```
cd D:/tools/uitools && npm i playwright pixelmatch pngjs axe-core
npx playwright install chromium
```

## 三条纪律（沿用仓库老规矩）

1. **门禁红必须意味着代码坏了** —— 三个脚本都起真本地服务（127.0.0.1 + 随机端口），
   不依赖外网、不依赖本机有没有装 pywebview。
2. **不许假「可用」** —— 拿不到值要写明原因，不许给假的 0 / 空表。
3. **只看图不算验收** —— 截图是**证据**，不是结论；结论要有数字（差异百分比 / 对比度 / 死链数）。

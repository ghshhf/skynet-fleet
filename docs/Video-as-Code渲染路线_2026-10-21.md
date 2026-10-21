# Video as Code：把「代码出片」接进本仓（2026-10-21）

> 一句话：**让本仓用写界面的那套东西（HTML/CSS/JS）直接出 MP4**，
> 不另学一套图形语言，也不必再依赖有问题的 Python 像素引擎。
>
> 状态：**已落地并通过门禁**。能力 `render.html`（74 项能力之一），
> 门禁 `evals/t23_html_render.py`（18 项断言），全量门禁 **54/54 通过**。

---

## 一、为什么换路线

本仓原有一条渲染线（`render.styles` / `render.frame` / `render.reel`），
用的是开源项目 **RuiC-motion-reel**（Python + numpy + Pillow 逐帧画像素）。
2026-10-21 实测暴露两个问题：

| 问题 | 实测数据 |
|---|---|
| 太慢 | 15 秒片子（450 帧）独占 CPU 约 **47 分钟**；被别的任务抢 CPU 会掉到 2 小时以上 |
| 与本仓技术栈脱节 | 本仓产品（桌面图形版）本身就是**纯手写 HTML/CSS/JS、零构建、断网可用**。用 Python 画图等于**同一个产品里维护两套渲染语言** |

于是转向 **Video as Code** 这个已经成型的品类。核实过的一线项目：

| 项目 | 归属 | 许可 | 核心 |
|---|---|---|---|
| **HyperFrames** | HeyGen 开源 | Apache-2.0 | HTML+CSS+GSAP，21 个 agent skills，`/hyperframes` 路由技能，Node 22+，puppeteer + ffmpeg |
| Remotion | Remotion | source-available（商用需读许可） | React + TypeScript |
| Motion Canvas | 社区 | MIT | TypeScript + Canvas |
| Generative Manim | 社区 | 开源 | Python + Manim |

**内核全都一样**：

```
可寻址动画  →  确定性逐帧  →  ffmpeg 合成
```

所以本仓不必引入 Remotion（React + 整套构建链）也不必引入 HyperFrames（21 个技能 + 它自己的 CLI 约定）——
**直接用裸 HTML + 浏览器内核 + ffmpeg**，正好是本仓已有的三样东西。

---

## 二、落地的东西

| 文件 | 作用 |
|---|---|
| `fleet/kinds/render_html.py` | Python 适配层：探环境、找 node/Playwright/ffmpeg、跑后端、搬产物 |
| `fleet/kinds/htmlrender.js` | Node 后端：确定性 lint → 逐帧截图 → ffmpeg 合成 |
| `fleet/kinds/templates/reel_demo.html` | 四场景样例模板（同时是「怎么写一条片」的说明书） |
| `fleet/kinds/render.py` | 能力入口 `render.html`（`template` / `src` / `html` 三个互斥入口） |
| `fleet/catalog.py` | 能力声明 `render.html`（副作用 + `approval="ask"`） |
| `evals/t23_html_render.py` | 门禁：18 项断言 |

### 能力签名

```
render.html(
  html | template | src,     # 三个互斥入口，template= 内置模板名
  outdir,                     # 必填，片子落在哪
  name="reel", width=1920, height=1080, fps=30, duration=5.0,
  keep_frames=False
) -> {ok, mp4, bytes, frames, seconds, width, height, fps, lint, engine:"html"}
```

`template` 传不存在的名字时，**报错并列出现成的模板名**（不静默失败）。

---

## 三、确定性是怎么做到的

不按真实时间播放动画，而是**手动拨时间**：

1. 页面加载完，用 `document.getAnimations()` 拿到全部动画实例；
2. 全部 `pause()` 住；
3. 渲染第 f 帧时，把每个动画的 `currentTime` 统一设成 `f / fps * 1000` 毫秒；
4. 截图。

于是**同一份 HTML 每次渲染出的每一帧都一样** ⇒ 片子能进 git、能 diff、能做像素回归。

### 由此带来的三条硬约束（写 HTML 的人必须遵守）

| 禁止 | 为什么 | 怎么被拦住 |
|---|---|---|
| 取当前时刻的函数（墙上时钟） | 每次渲出来画面不一样 | 静态 lint 拒绝，退出码 3 |
| 不播种的随机数 | 同上 | 同上 |
| 渲染时发网络请求拿视觉状态 | 同上 | 同上 |

lint 结果写在产物的 `report.json` 里，**命中就拒绝渲染并说明是哪一条**（不静默出片）。

> 🔴 **实踩的坑**：早期 lint 只剥 JS 注释，没剥 HTML 注释（`<!-- -->`），
> 结果**模板自己的说明书里写了「不许用墙上时钟」就被当成真代码拒了**——自己的模板渲不出来（假红）。
> 现在 lint 剥三种注释。

---

## 四、写片子的三条实操要点

1. **只用 CSS animation**（Web Animations API 能把它变成可寻址的钟）。
   不要用 `setTimeout` / `requestAnimationFrame` 驱动——那些靠真实时间走，逐帧渲染时拨不动。
2. **`display` 不会随 animation 变**，只有 `opacity` / `transform` 会。
   所以场景切换要靠 opacity 动画，**不要靠 `display:none` 切换**。
3. **场景之间的内部动画要对齐**：给场景一个 `--s`（第几个场景），
   内部元素的 `animation-delay` 用 `calc(var(--s) * <场景时长> + <自己的偏移>)`。

第 2 点是实踩出来的：最初用 `.scene.on { display:block }` + opacity 动画，
结果 4 个场景里有 3 个没出现——**因为 display 根本不随 animation 变**。

---

## 五、性能（2026-10-21 本机实测）

| 方案 | 450 帧（15 秒片子） |
|---|---|
| **HTML 后端**（Playwright + Chromium + ffmpeg） | **约 1.1 分钟**（6.6 帧/秒） |
| Python 像素引擎（RuiC-motion-reel） | 约 **47 分钟**（被抢 CPU 时更差） |

**快约 40 倍，且不占 GPU、不需要装任何新东西**（Chromium 和 ffmpeg 本机都有）。

本机环境：12 逻辑核 / 8 物理核；Playwright 在 `D:\tools\uitools\node_modules`；
ffmpeg 在 `D:\tools\ffmpeg\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe`。

---

## 六、验收证据

门禁 `evals/t23_html_render.py` 五段共 18 项断言：

1. **环境**：缺 node / Playwright / Chromium ⇒ 明确 skip 并说清缺什么（不假装成功）
2. **真出片**：MP4 存在且非空；帧数 = 时长×帧率；尺寸参数对；
   用 ffmpeg 自己读回分辨率 / 片长 / 帧率（不信参数，信产物）
3. **确定性**：同一份 HTML 渲两次**逐字节一致**
4. **确定性 lint**：用了墙上时钟的 HTML 被拒绝，且拒绝时说清原因
5. **模板入口**：`template="reel_demo"` 真出片；不存在的模板报错并列出现成的
6. **注册表**：`render.html` 可见、是副作用、要审批；**没有闸门时 fail-closed 拒绝**

端到端手工验收（走注册表 + 审批闸门）：**225 帧 / 7.5 秒 / 1920×1080 / 30fps / 249KB MP4**，
抽帧确认 4 个场景全部正确（标题飞入+下划线、数据条增长、点阵铺开、文字幕）。

---

## 七、边界与未做

- **本仓只提供「HTML → MP4」这一段**。不提供时间线编辑器、不提供 React/组件生态、
  不提供模板市场——那些是 HyperFrames / Remotion 的活，本仓不重复造。
- **`render.reel`（Python 引擎那条线）仍在**，`render.html` 是**新增的第四条能力**，
  两者并存。Python 引擎之所以保留：它的风格牌（16 张）有自己的审美，不是 HTML/CSS 能立刻替代的。
- **整条片子还没渲完**（等用户升级那个开源项目后再定）。
- 引擎调度、样式统一、音频轨**都还没做**——先保证「能出一条可复现的片」，再谈别的。

---

## 八、与「一切皆 MCP」的关系

`render.html` 是**副作用能力 + `approval="ask"`**，所以：

- 走 GUI / CLI / MCP 三条路径，**都过同一道注册表闸门**；
- 在 **MCP（stdio）路径上默认不导出**——stdio 没有 GUI 弹窗可等，等不到人就是没人授权（fail-closed）。
  门禁 `t14` 断言「有审批档的 `render_html` 不在 MCP 工具表里」。

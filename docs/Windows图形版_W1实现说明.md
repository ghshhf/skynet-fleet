# Windows 图形版 · W1 实现说明

> W1 = 第一个**可运行骨架**。目标不是做全，是把「第三个显影面」的骨架立起来：
> 一个真能开起来的本地窗口 + 一个真能调设备的后端 + 一份不依赖外网的前端。
> 交互、审批弹窗、语音、附件、多卡切换都是 W2 之后的事，本文末尾写清楚边界。

---

## 一、三层架构

```
┌───────────────────────────────────────────────────────────────────┐
│  ① 壳（Windows 窗口）        fleet/gui/app.py                    │
│  ─ 只干三件事：拉服务 / 交令牌 / 开窗                              │
│  ─ 壳里**不写任何能力逻辑**                                        │
│  ─ pywebview（WebView2）；没装就明说「装它：pip install pywebview」 │
└───────────────────────────┬───────────────────────────────────────┘
                            │ 打开 http://127.0.0.1:<随机端口>/index.html#t=<令牌>&theme=light
                            │（令牌走 **URL 片段**：片段不随 HTTP 请求发出）
                            ▼
┌───────────────────────────────────────────────────────────────────┐
│  ② 本地后端（唯一能力代理层）  fleet/gui/server.py                │
│  ─ stdlib http.server，绑 127.0.0.1 + **随机端口**（port=0）      │
│  ─ 每个 API 请求校验 X-Fleet-Token 头（一次性令牌，只存内存）      │
│  ─ **一切能力调用走 registry.call**：闸门在服务端，前端绕不过去    │
│  ─ /api/chat 用 SSE 真流式：AI 的 _on_line 一行行回流，边收边写    │
└───────────────────────────┬───────────────────────────────────────┘
                            │ HTTP + JSON / SSE
                            ▼
┌───────────────────────────────────────────────────────────────────┐
│  ③ 前端（本地 Web UI）        ui/index.html + ui/app.js + app.css│
│  ─ 纯原生 JS + CSS：无框架、无 CDN、无 npm、无外部字体             │
│  ─ 断网可用，也不外传任何东西                                       │
│  ─ 界面照抄 WorkBuddy 桌面端三栏布局（明暗双主题）                   │
│  ─ 消息流真接 /api/chat 的 SSE：边收边渲染                          │
│  ─ 前端**不直接碰设备**，能力动作一律 POST /api/call                │
└───────────────────────────────────────────────────────────────────┘
```

**为什么要有第 ② 层**：浏览器里没有 Python，能力调用必须经过某个进程；
但「能点鼠标键盘的 HTTP 接口」是一个新引入的攻击面，所以这层自己给自己上了三道锁
（回环地址、随机端口、一次性令牌），并且把所有能力收口到 `registry.call`——
与命令行/T 版共用同一张注册表、同一套审批语义。

---

## 二、API 契约

所有 API 都是 JSON。**每个请求都必须带 `X-Fleet-Token` 头**，否则 401 且带原因。

| 方法 | 路径 | 请求体 | 答复 | 备注 |
|---|---|---|---|---|
| GET | `/api/health` | — | `{ok, devices, caps_count, backend, backends, backend_reason, persona, uptime_s, calls}` | 启动自检；`backend_reason` 说明为什么没有后端 |
| GET | `/api/caps` | — | `{ok, count, caps:[{name,title,params,side_effect,destructive,note,approval,domain,devices}], domains:{domain:[name,…]}}` | 能力清单，按 domain 分组；`approval` ∈ `auto/ask/deny` |
| GET | `/api/devices` | — | `{ok, count, devices:[{name,kind,via,ok,reason,caps}]}` | 真可用性（`ok=false` 时 `reason` 必填） |
| GET | `/api/persona` | — | `{ok, card:{name,spec,creator}, card_loaded, state}` | 当前角色卡 + PersonaState 快照 |
| GET | `/api/cards` | — | `{ok, cards:[{file,ok,name,spec,error}]}` | 卡库清单（坏卡也列出来，带 `error`） |
| POST | `/api/call` | `{cap, args, device?}` | `DeviceResult`：`{ok,cap,device,data,err,code,ms,side_effect,via}` | 走 `registry.call`；`code` ∈ `ok/unavailable/denied/timeout/error/unsupported` |
| POST | `/api/chat` | `{prompt}` | **SSE**：`text/event-stream` | 事件三型：`{type:"line",text}` / `{type:"done",result}` / `{type:"error",err}` |
| POST | `/api/card/use` | `{file}` | `{ok, card, card_loaded, state}` | 切卡（写 `cards/active.json`） |

静态资源（**不需要** token）：`GET /`、`GET /index.html`、`GET /app.js`、`GET /app.css`。
白名单只有这三个文件名，不做路径拼接 —— 防目录穿越。

错误形状统一为 `{ok:false, err:"<为什么>"}`，HTTP 状态码 400/401/404/500 各自对应
「参数不对 / 没鉴权 / 没这个接口 / 后端出错了」。

---

## 三、安全边界

### 1. 一次性 token

- 启动时 `secrets.token_urlsafe(32)` 生成，**只活在本进程内存里**，不落盘。
- 壳通过 **URL 片段**（`#t=…`）交给页面：片段不会随 HTTP 请求发出，
  因此既不会进服务端访问日志，也不会进任何 `Referer` 头。
- 页面读出来只存在 JS 内存变量里，**不写 localStorage**（落盘等于把凭据留在盘上）。
- 每个 API 请求带 `X-Fleet-Token`；服务端用 `secrets.compare_digest` 比对（常数时间，
  防时序侧信道）。
- 401 一律带明确原因（缺头 / 令牌不对），不许静默。

> token 与 `local.env` 里的 AI 凭据是**两回事**：`local.env` 是后端连 AI 用的，
> 已在 `.gitignore` 里；本 token 根本不落盘，进程一退就没了。

### 2. 为什么必须是随机端口 + 回环地址

- 绑 `0.0.0.0` 等于把这台机器变成「局域网里谁都能指挥的遥控器」——
  能点鼠标键盘的接口绝不能出本机。
- 端口固定（8000/8080…）既会被同机别的程序撞上，也方便外部扫描猜。
- `port=0` 让操作系统分配，系统会挑一个当前空闲的高位端口。

### 3. 为什么前端不许直接调设备

浏览器里根本没有 Python，前端想调设备只能再开后门（进程外调脚本、跑本地 socket
等等）。那等于把注册表、审批语义、能力白名单全绕过去——CLI 有审批、窗口没有，
就出现「同一个动作，命令行要人点头，窗口里悄悄就做了」。

所以：**前端只能 `POST /api/call`**，由后端 `registry.call(cap, device, args, gate)`。
闸门在服务端，前端绕不过去。这条也是 T5 门禁的精神延续。

### 4. 闸门现状（W1 的真实状态，不是兜底）

- `POST /api/call` 用的 `gate` 默认是 `None` → 按注册表 fail-closed 纪律，
  **一切有副作用的能力一律 denied**，答复里写明「需要审批闸门（没注入 gate 一律拒绝）」。
  审批弹窗是 W2 的事；在那之前，先不许动手。
- `POST /api/chat` 用一个**窄闸门** `chat_gate`：只放行 `ai.chat` 一项。
  为什么需要它：`ai.chat` 在 catalog 里是 `side_effect=True`（真连后端、真烧 token），
  按纪律没有闸门必被拒；而「用户在界面上敲了字点了发送」本身就是授权，
  所以给它一个只认 `ai.chat` 的窄口子，别的能力仍然一律拒。
- 只读能力（`ai.list`、`host.monitor`…）不需要闸门，照常走。

### 5. 人格来源

`/api/chat` 的 system prompt 由 **`persona_card.build_system(card, state=…, extra=…)`** 拼，
不另造一套：

- 角色卡（`active_card()`）给底色：她是谁、什么性格、什么情境。
- `PersonaState`（`persona.py`）给当天的体温：情绪、口癖、执念。
- 世界书（`match_world_book(card, text)`）按关键词注入。

没有选中卡时 system prompt 是**空串**（界面据此知道「现在是没有人格的裸助手」），
不拿一个默认人设糊弄过去。

---

## 四、怎么跑

### 装依赖

```bash
pip install pywebview          # 图形窗口需要；本机 WebView2 运行时 Windows 11 自带
```

没装 pywebview 时程序会**明确告诉你**并退出（不会双击没反应）：

```
没装 pywebview，图形窗口起不来。
装它：pip install pywebview
（只想跑后端服务本体的话加 --no-window）
原始错误：No module named 'webview'
```

### 开窗口

```bash
cd D:/code/device-fleet
python -m fleet.gui.app
```

### 只跑后端（无 GUI 环境 / 自动化）

```bash
python -m fleet.gui.app --no-window
```

它会打印两行关键信息：

```
[fleet-gui] 本地服务已起：http://127.0.0.1:54321
[fleet-gui] 页面地址（片段里是一次性令牌）：http://127.0.0.1:54321/index.html#t=XXXX&theme=light
```

### 手动打接口（令牌从上面那行 URL 的 `#t=` 后面取）

```bash
curl -s http://127.0.0.1:54321/api/health -H "X-Fleet-Token: XXXX"
curl -s http://127.0.0.1:54321/api/caps   -H "X-Fleet-Token: XXXX"
curl -N -s http://127.0.0.1:54321/api/chat -H "X-Fleet-Token: XXXX" \
     -H "Content-Type: application/json" -d '{"prompt":"你好"}'
```

### 跑门禁

```bash
python evals/t7_gui_backend.py       # W1 专用门禁（104 项，离线可复现）
python run_selftests.py              # 全量
python run_selftests.py t7           # 只跑含 t7 的
```

---

## 五、门禁 `evals/t7_gui_backend.py` 钉死了什么

全离线、可复现：AI 后端打桩（**只打桩网络层**，聊天链路本身全真），
角色卡与 PersonaState 放临时目录，设备清单用临时账本。不碰真屏幕、不连网、不点真鼠标。

| 组 | 钉住的事 |
|---|---|
| ① token 鉴权 | 缺头 → 401 带原因；错 token → 401 带原因；对 token → 200；token 不出现在任何答复里 |
| ② 绑定 | 只绑 127.0.0.1（`server_address` 也是回环）；端口随机（两次启动不同） |
| ③ API 契约 | health/caps/devices/persona/cards 各自的字段都在；cap 名是字符串；设备不可用时带原因 |
| ④ SSE 真流式 | 裸 socket 逐块读：**首个 line 事件与 done 事件之间必须拉开时间差**（攒完再发 → 间隔≈0，立刻红）；事件顺序 line→done |
| ⑤ registry 闸门 | 无 gate → 副作用能力 denied 带原因；只读能力不受影响；未登记能力 404；缺字段 400 |
| ⑥ 人格 | system prompt 里含当前角色卡的名字（证明走的是 `build_system`） |
| ⑦ 前端 | 三个文件存在且非空；无 CDN/外链/绝对 URL；只走本地相对路径；`app.js` 用到的 id 在 `index.html` 里都有；`node --check` 过；假 DOM 真跑一遍 `app.js`，验令牌真的流进了请求头 |
| ⑧ 静态纪律 | `server.py` 绑 127.0.0.1、代码里无 `0.0.0.0`、用 `secrets`+`compare_digest`、走 `registry.call`、用 stdlib `http.server`、不读 `local.env`；`local.env` 在 `.gitignore` 里；目录穿越被挡 |

**「门禁红 = 代码坏了」做过变异验证**（改坏 → 立刻红 → 改回 → 绿）：

| 变异 | 结果 |
|---|---|
| token 校验改成永远通过 | 红 3 项 |
| SSE 改成攒完再一次性发 | 红 1 项（时间差 ≈ 0.001s） |
| `app.js` 里真加一个 CDN `fetch` | 红 2 项 |
| 后端默认 host 改成 `0.0.0.0` | 红 2 项 |
| `/api/call` 绕过闸门直接放行 | 红 5 项 |
| 前端 token 被加工（`TOKEN.replace(...)`） | 红 1 项 |

---

## 六、W1 的已知限制（都是「还没做」，不是「坏了」）

1. **没有审批弹窗**。`/api/call` 在没注入闸门时一律 denied —— 这是 W1 的**真实状态**，
   不是兜底。W2 接审批弹窗后，这里换成弹窗回调。
2. **语音是 UI 占位**。底部麦克风按钮点了会显示「语音还没接上（W4 才录）」，
   不录音、不上传。语音链路计划在 W4。
3. **附件（＋）与浏览权限（⊘）是死的**。W3 才开。
4. **会话列表只在内存里**。关掉窗口就没了，没有落盘、没有多窗口共享。
5. **只有浅色是主色，深色是等价实现**。主题切换已通（`data-theme` 切换 CSS 变量），
   但没做过逐像素校色。
6. **没有托盘、没有单实例**。开两个窗口就是两个服务、两个令牌，互不相干。
7. **模型选择器只读**。点击只是把 `/api/health` 里的后端清单念给你听，还不能切。
8. **pywebview 依赖 WebView2**。Win10 上可能要单独装 Microsoft Edge WebView2 Runtime。
9. **后端进程与窗口同生共死**。窗口关了，服务也停（`finally: srv.stop()`）。

---

## 七、文件清单

| 文件 | 作用 |
|---|---|
| `fleet/gui/__init__.py` | 包声明 |
| `fleet/gui/server.py` | 本地后端：路由、鉴权、闸门转发、SSE 流式 |
| `fleet/gui/app.py` | 窗口壳：起服务 → 注入令牌 → 开 pywebview 窗口 |
| `ui/index.html` | 三栏骨架（照抄 `ui/w0_layout.html` 的视觉基准） |
| `ui/app.css` | 样式：明暗双主题 + W0 布局 + 能力面板/结果条/提示条 |
| `ui/app.js` | 逻辑：token、SSE 渲染、能力面板、设备/会话列表、主题 |
| `evals/t7_gui_backend.py` | W1 门禁（104 项） |
| `evals/t7_front_smoke.js` | 前端运行时冒烟（假 DOM 真跑 `app.js`） |
| `docs/Windows图形版_W1实现说明.md` | 本文 |

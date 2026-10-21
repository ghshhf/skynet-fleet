# Windows 图形版 · W2 审批弹窗

> 状态：已实现并接入门禁 `evals/t9_approval.py`（全量 48/48）。
> 依赖 W1（`docs/Windows图形版_W1实现说明.md`）——本地后端 + 三栏 UI + 一次性令牌。

## 一句话

W1 的时候 `/api/call` 因为**没配闸门**，一切有副作用的能力一律 `denied`（带明确原因）。
那是**故意的 fail-closed 真实状态，不是 bug**。W2 补上的是：需要审批的能力会**挂起**，
等主人在弹窗里点确认，点确认才执行。

## 1. 架构

```
浏览器 UI (ui/app.js)
   │  ① 拉 /api/approvals  → 界面上显示「哪些能力在等审批」
   │  ② 点「执行」 → POST /api/call（这趟请求会**挂在闸门上**）
   │  ③ 主人点确认 → POST /api/approval/respond  {request_id, approve, double, session}
   ▼
本地后端 (fleet/gui/server.py)  ← 只绑 127.0.0.1 + 随机端口 + 一次性 token
   │
   ├─ ApprovalBroker (fleet/gui/approval.py)   ← 登记 + 等待 + 送达
   │      gate(cap, dev, args, level) → (True/False, reason)
   │      每次调用各有各的 threading.Event；等待发生在锁外
   ▼
注册表 (fleet/core/registry.py)                ← **判定权在这里**
   needs_approval = cap.side_effect or cap.approval == "ask"
   level = 2 if (cap.destructive or cap.approval == "ask") else 1
   approval="deny" → 直接拒绝（数据文件改不了）
```

### 判定权为什么必须在注册表层

「这项能力要不要审批、审批几级、破坏性要不要双确认」全部由 `Registry.call` 算，
**只在这一层算**。UI 换个前端、手敲一条 HTTP，都绕不过去——因为闸门是
`registry.call` 必经之路上的一个环节，不在 UI 里。

对应地，审批中枢（`fleet/gui/approval.py`）**不重做**这个判定：闸门被调用这件事本身
就说明「注册表已经判定要审批了」。它只做三件事：登记一条待审批记录、把这次调用挂起、
把主人的表态一次性送到正在等的那次调用。

前端（`ui/app.js`）只负责一件事：把「用户点了确认」这个信号回传。
它**不判**要不要审批、危不危险 —— 界面上的「破坏性 / 二级审批」徽标全部来自后端推来的清单。

### 为什么是「挂起」而不是「二次提交」

挂起式：点击 → 后端在闸门上停住 → 用户确认 → **同一次调用**继续往下走。

如果做成「第一次点下去返回 denied，第二次带上票据再提交」，
那么重复提交就会导致副作用**落地两次**（点两次鼠标、发两条消息）。
挂起式从结构上杜绝了这件事：一次调用只有一次落地机会。

## 2. API 契约

### `GET /api/approvals`

列出当前在等主人确认的清单。

```json
{
  "ok": true,
  "enabled": true,
  "count": 1,
  "pending": [
    {
      "request_id": "6f1c…（uuid4 hex，唯一）",
      "cap": "host.input.click",
      "title": "点一下",
      "device": "pc:local",
      "level": 1,
      "destructive": false,
      "args_preview": "x=100, y=200",
      "tag": "call-3",
      "created_at": 1760000000.0,
      "expires_in": 118.4,
      "state": "pending"
    }
  ],
  "reason": ""
}
```

- `enabled=false` 表示这个服务没注入审批中枢（有副作用的能力会一律被注册表拒成 `denied`），
  此时 `reason` 里写清为什么是空的 —— 不静默返回空数组让人误以为「没人等审批」。
- `tag` 是调用方自己生成的自标签，用来把待审批条目认领回自己那次调用；只回显，不参与判定。
- `args_preview` 是给人看的参数摘要（截断到 400 字符）。

### `POST /api/approval/respond`

```json
{ "request_id": "6f1c…", "approve": true, "double": false,
  "session": "ui-abc123", "reason": "" }
```

| 情况 | HTTP | 说明 |
|---|---|---|
| 缺 `request_id` | 400 | 带明确原因 |
| 缺 `approve` / 不是布尔 | 400 | 带明确原因 |
| 成功送达 | 200 | `{"ok":true,"decision":"approved"}` |
| `request_id` 不认识 | 409 | 「没有这条审批请求（编号不认识）」 |
| 已结束（用过 / 超时 / 已拒） | 409 | 「这条审批已经结束了，不能重复回应」 |
| 跨会话 | 409 | 「这条审批不属于当前会话」 |
| 破坏性但没给 `double=true` | 409 | 「破坏性操作需要二次确认（double=true）」 |

### 鉴权口径

- **没带** `X-Fleet-Token` → **401**（调用方忘了加头，是 bug）
- **带了但不对** → **403**（有人拿着旧令牌来敲，是可疑行为）

两种都带明确原因，比对一律走 `secrets.compare_digest`（常数时间，防时序侧信道）。
> W1 时错 token 也是 401；W2 拆成 401/403，因为这两种事的处置不一样。
> 对应的 `evals/t7_gui_backend.py` 断言已同步更新。

### `POST /api/call` 的变化

多了两个**只用于归属与回显**的可选字段：

```json
{ "cap": "…", "args": {}, "device": "", "tag": "call-3", "session": "ui-abc123" }
```

调用本身不变，仍然走 `registry.call`。`tag` / `session` **不参与判定**。

## 3. 关键性质

| 性质 | 怎么保证的 |
|---|---|
| 没确认就不执行 | 闸门返回 `(False, reason)`，注册表直接 `denied`，设备一次都没被碰过 |
| 点确认才执行 | `Event.set()` 唤醒挂起的那次调用，同一次调用继续往下走 |
| 授权一次性 | 送达即从 `_pending` 摘除并写进「已结束」账本（`_closed`，上限 256 条）；同一 `request_id` 第二次回应被拒 |
| 不可跨会话重放 | 待审批记录带 `session`；别的会话拿同一个 `request_id` 回应 → 409 |
| 超时 = 拒绝 | 等待超时即 `denied`，理由写明「审批超时未确认（等了 N 秒没有人点确认，这次就不做）」；请求不会永久挂死 |
| 并发不互堵 | 每次调用各有各的 `Event`；等待发生在 `self._lk` 锁外；HTTP 层是 `ThreadingHTTPServer`（每请求一线程） |
| 破坏性双确认 | `cap.destructive` 时要求 `double=true`，**判定在审批中枢**，不靠 UI 自觉 |
| 判定绕不过去 | 判定在 `Registry.call` 里，闸门是必经之路 |

## 4. 安全边界

- 服务只绑 `127.0.0.1` + 随机端口（`port=0`），局域网里别的机器连不进来。
- 一次性 token 由 `secrets.token_urlsafe(32)` 生成，只活在本进程内存里，
  通过 URL **片段**交给页面（片段不随 HTTP 请求发出 ⇒ 不进服务端日志、不进 Referer）。
- token 只在页面内存变量里，**不写 localStorage**；不落盘、不入库。
- 前端无 CDN 外链、无框架、无 npm 包；只走相对路径 `/api/…`。
- 前端**不直接调设备**：一切能力动作都 POST `/api/call`，由后端走 `registry.call`。
- 失败一律带明确原因（`code` + `err`），不静默降级、不假装成功。

## 5. 怎么跑

### 起图形版（带审批）

```bash
D:/tools/python/python.exe -m fleet.gui.app
```

窗口里点「能力面板 → 执行」触发一个有副作用的能力 → 界面弹出审批框 →
点「确认执行」才真的执行。破坏性能力要点两次。

### 只跑后端（不开窗）

```bash
D:/tools/python/python.exe -m fleet.gui.app --no-window
```

打印出来的地址里带一次性 token（片段形式）。

### 跑门禁

```bash
D:/tools/python/python.exe evals/t9_approval.py     # 只跑这一条
D:/tools/python/python.exe run_selftests.py         # 全量（48 项）
```

> 用 `D:/tools/python/python.exe`，**不要**用系统 `python`（WindowsApps 存根会静默 exit=49）。

t9 门禁**完全离线**：假设备、假注册表，网络 I/O 一律假掉 —— 绝不真连设备、
绝不真开麦、绝不真调 LLM。门禁红 = 代码坏了。

### 门禁覆盖的形状

`evals/t9_approval.py` 覆盖：

1. 没点确认 → 设备一次都没被碰过；拒绝 → `denied` 且理由原样带回
2. 点确认 → 成功执行，且只执行一次
3. 同一个 `request_id` 第二次回应 → 被拒（一次性授权）
4. 破坏性能力：`double=true` 才放行；不给 → 拒
5. 超时 → `denied`，理由含「审批超时未确认」
6. 两个请求同时在等审批 → 互不阻塞，各自独立回应
7. 不带 token → 401；带错 token → 403；带对 → 200
8. HTTP 层：跨会话回应 → 409 且不生效；重复回应 → 409
9. 静态断言：判定权在注册表层、审批中枢不重做判定、UI 不自判、前端不碰设备

## 6. 改了哪些文件

| 文件 | 改动 |
|---|---|
| `fleet/gui/approval.py` | **新增**。审批中枢 `ApprovalBroker`（登记 / 挂起 / 送达 / 一次性 / 超时 / 会话绑定） |
| `fleet/gui/server.py` | 注入审批中枢；新增 `GET /api/approvals`、`POST /api/approval/respond`；鉴权拆 401/403；`/api/call` 透传 `tag`/`session` |
| `fleet/gui/app.py` | 图形版启动时注入 `ApprovalBroker()`（有副作用的能力因此会弹窗而不是被拒） |
| `ui/index.html` | 待审批计数徽标 + 审批弹窗骨架 |
| `ui/app.js` | 拉待审批清单、显示弹窗、确认/拒绝回传、破坏性二次确认、能力面板「执行」按钮 |
| `ui/app.css` | 审批弹窗与徽标样式 |
| `evals/t9_approval.py` | **新增**。W2 门禁（完全离线） |
| `evals/t7_gui_backend.py` | 错 token 的断言从 401 改为 403（与新鉴权口径对齐） |
| `run_selftests.py` | 接入 `eval_t9_approval`（全量 47 → 48 项） |

**没有**改 `fleet/catalog.py`（另一个 agent 正在改，会冲突）。本任务不需要新增能力登记 ——
W2 只加审批链路，不动能力清单。

## 7. 已知限制

1. **审批超时默认 120 秒**（`ApprovalBroker.DEFAULT_TIMEOUT`）。
   超时即拒绝，理由写明「审批超时未确认」。这个值目前是常量，
   没有做成命令行参数。
2. **同一个页面会话只认自己发起的审批**。`session` 由前端在页面加载时生成
   （内存里，刷新即换）；别的会话拿着同一个 `request_id` 回应会被拒。
   代价是：如果用户在两个窗口同时开同一个页面，第二个窗口看不到第一个窗口发起的审批
   （`GET /api/approvals` 返回的是全局清单，但**回应**会因会话不匹配被拒）——
   这一点目前没有做「谁来接管」的界面。
3. **`tag` 目前只是回显**。前端用它把待审批条目认领回自己那次调用，
   但没有做「只显示属于我的那条」的过滤。
4. **破坏性双确认靠前端点两次 + 后端要求 `double=true` 双重把关**。
   前端点两次是体验，后端要求 `double=true` 才是安全边界 —— 后者不可绕过。
5. **审批中枢的「已结束」账本上限 256 条**（`CLOSED_LEDGER_MAX`）。
   超出的旧记录会被挤掉，此时重复回应会得到「编号不认识」而不是「已经结束了」——
   两种都拒绝，只是理由文案不同。
6. **W1 的 `fleet/gui/server.py` 里 `FleetServer(approval=...)` 默认是 `None`**，
   也就是不注入审批中枢时行为与 W1 完全一致（有副作用的能力一律 `denied`）。
   `fleet/gui/app.py`（真正的图形版入口）才注入。
7. **审批等待期间那次 HTTP 连接是挂着的**。`ThreadingHTTPServer` 每请求一线程，
   所以不堵其它请求；但挂着的连接会占一个线程，浏览器刷新会留下一个等服务端超时的连接。
8. **没有做审批历史留档**。已结束的审批只在内存账本里（上限 256 条），
   进程一关就没了，也不落盘。

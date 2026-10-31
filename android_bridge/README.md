# 编队桥（fleet-bridge）

手机侧唯一常驻组件：**29 KB**，一个无障碍服务 + 一个前台服务，无界面、无第三方库、不读通知、不联网。

## 为什么需要它

小米 / 红米（MIUI / HyperOS）把 `adb shell input`、`monkey`、`uiautomator`
的注入权限单独锁在开发者选项的「USB 调试（安全设置）」后面，而这个开关要求
**登录小米账号 + 插入 SIM 卡**。没插卡的手机就永远点不动屏幕。

实测过、都不行的路（2026-10-20，Redmi K60 / Android 15 / HyperOS 3）：

| 尝试 | 结果 |
|---|---|
| `input tap/swipe/keyevent` | `SecurityException: INJECT_EVENTS` |
| `monkey`（含 monkey script） | 同样 `INJECT_EVENTS`，走的是同一个 `injectInputEvent` |
| `sendevent /dev/input/event7` | `Permission denied`：SELinux `Enforcing`，shell 域不能写 input 设备 |
| `setprop persist.security.adbinput 1` | `Failed to set property`，只有 root 能写 persist |
| `settings put secure enabled_accessibility_services` | 缺 `WRITE_SECURE_SETTINGS`，MIUI 从 shell 移除了该权限 |

唯一没被堵死的是无障碍：`AccessibilityService.dispatchGesture` 走的是
`AccessibilityInputFilter`，**不经过 `InputManager.injectInputEvent` 那道检查**。
所以只要用户手动授权一次，点击 / 滑动 / 输入全部可用。

## 编译

```bash
bash build.sh              # 复用 android_bridge/.token
bash build.sh --new-token  # 轮换令牌（之后手机上要重装）
bash build.sh --install    # 编译 + 装到当前连着的手机 + 拉起前台服务
```

产物 `build/fleet-bridge.apk`，令牌在 `build/token.txt`（同时固化在 `.token`）。
两者都不入库。

## 安装与授权

MIUI 的「USB 安装」也锁着，`adb install` 会报
`INSTALL_FAILED_USER_RESTRICTED: Install canceled by user`。三条路，只有第三条通
（2026-11-01 全试过）：

```bash
# ✅ 路 3：分步 session。快捷命令 pm install 会带"需要用户确认"的标志，分步不带。
#    全程不碰屏幕，也不看 ICP 备案弹窗的脸色。build.sh --install 走的就是这条。
adb push build/fleet-bridge.apk /data/local/tmp/fb.apk
adb shell pm install-create -r -t -S $(stat -c%s build/fleet-bridge.apk)   # → [会话号]
adb shell pm install-write -S <字节> <会话号> base /data/local/tmp/fb.apk
adb shell pm install-commit <会话号>

# ❌ 路 1：临时放开 Shell 的安装权限 —— 这代 ROM 上仍被 USER_RESTRICTED 挡
adb shell cmd appops set --uid com.android.shell REQUEST_INSTALL_PACKAGES allow

# ❌ 路 2：推到 Download 用文件管理器装 —— 会卡在 ICP 备案弹窗，
#    而点「继续安装」需要无障碍，无障碍正是要装的东西（卸载后必死）
adb push build/fleet-bridge.apk /sdcard/Download/
```

授权无障碍（**必须手动**，这是安全设计，命令改不动）：

设置 → 无障碍 → 已下载的服务 → 「编队桥」→ 打开

> **能不能不手动？** 不能。`settings put secure` 与 `pm grant` 在这代 ROM 上都被拒
> （`WRITE_SECURE_SETTINGS` 从 shell 收走了），而「USB 调试（安全设置）」要求登录
> 小米账号 + 插 SIM 卡。**覆盖安装不会丢授权**（同一把 keystore），只有卸载才会。
> 所以 `keystore.jks` 丢了 = 必须卸载重装 = 必须人再点一次。

## 控制协议

手机侧只监听 `127.0.0.1:8765`，电脑侧用 `adb forward` 建隧道，端口不外露到局域网。

```
电脑 127.0.0.1:18765  --adb forward-->  手机 127.0.0.1:8765
```

### 三层存活，必须分开问（2026-11-01）

端口原来开在无障碍服务里，于是「端口通不通」同时承载了两件事，坏起来却完全不是一回事：

| 现象 | 真正的意思 | 修法 |
|---|---|---|
| 端口连不上 | 进程没了 | `am start` 拉活（`bridge_wake`） |
| 端口通、`A11Y` 报 `no-a11y` | 进程活着，无障碍授权被撤 | **只能人去设置里点一次** |
| `A11Y` 回 `OK a11y ready` | 真能用 | — |

现在端口开在 `BridgeKeepAliveService`（前台服务，`specialUse` 类型、`START_STICKY`），
无障碍服务只负责注入身份。进程被回收后系统会把前台服务重建回来，端口跟着回来；
无障碍也被重绑，通常不需要人管。

> 为什么非得分家：`settings get secure enabled_accessibility_services` 里那行
> **在进程被回收之后照样在**。只看它 = 把最坏的情况判成最好的情况。

每条连接第一行必须是 `AUTH <令牌>`，之后一行一条指令、一行一条回执：

| 指令 | 说明 |
|---|---|
| `PING` | 回 `PONG`。**只证明进程活着**，不证明能点 |
| `A11Y` | 回 `OK a11y ready` / `ERR no-a11y …`。问的是「能不能点」，和 `PING` 互补 |
| `TAP x y [ms]` | 点按，默认 55ms |
| `SWIPE x1 y1 x2 y2 [ms]` | 滑动，默认 300ms |
| `LONG x y [ms]` | 长按，默认 650ms |
| `TEXT 内容` | 写入当前焦点输入框（中文可用，走 `ACTION_SET_TEXT`） |
| `BACK` / `HOME` / `RECENTS` / `NOTIFY` | 全局动作 |
| `FOCUS` | 读当前焦点控件与文字，确认输入框在哪 |

驱动层已封装，日常不用手写协议：

```bash
python android_device.py cap                 # 看两条注入通道哪条通
python android_device.py tap "7"             # 按屏幕文字点（桥自动接管）
python android_device.py bridge "TAP 243 2133"
```

## 边界

- **覆盖安装不丢授权**（同一把 keystore），卸载才会。所以 `keystore.jks` 一旦丢失，
  代价就是「卸载 → 重装 → 人去设置里再点一次」。这个文件**不在 git 里**，要自己备份。
- 用户关掉无障碍即彻底失效——这是有意的。
- 前台服务的通知（「编队桥运行中」）不能划走。它就是"桥还在不在"的唯一人眼可见信号：
  通知没了 = 进程没了。
- 前台服务类型用 `specialUse` 而不是 `dataSync`：Android 14+ 给 `dataSync` 按了
  「每 24 小时 6 小时」的配额，跑满就再也起不来。对一个要求「一直在」的桥来说那是定时炸弹。
- 不做截图：截屏走 `screencap` 更省事，没必要在服务里碰硬件缓冲区。

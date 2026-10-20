# 编队桥（fleet-bridge）

手机侧唯一常驻组件：**16 KB**，无界面、无第三方库、不读通知、不联网。

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
```

产物 `build/fleet-bridge.apk`，令牌在 `build/token.txt`（同时固化在 `.token`）。
两者都不入库。

## 安装与授权

MIUI 的「USB 安装」也锁着，`adb install` 会报
`INSTALL_FAILED_USER_RESTRICTED`。两条路：

```bash
# 路 1：临时放开 Shell 的安装权限（不需要 SIM 卡）
adb shell cmd appops set --uid com.android.shell REQUEST_INSTALL_PACKAGES allow
adb install -r build/fleet-bridge.apk

# 路 2：把 APK 推到手机，用文件管理器手动装
adb push build/fleet-bridge.apk /sdcard/Download/
```

授权无障碍（**必须手动**，这是安全设计，命令改不动）：

设置 → 无障碍 → 已下载的服务 → 「编队桥」→ 打开

## 控制协议

手机侧只监听 `127.0.0.1:8765`，电脑侧用 `adb forward` 建隧道，端口不外露到局域网。

```
电脑 127.0.0.1:18765  --adb forward-->  手机 127.0.0.1:8765
```

每条连接第一行必须是 `AUTH <令牌>`，之后一行一条指令、一行一条回执：

| 指令 | 说明 |
|---|---|
| `PING` | 回 `PONG` |
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

- 重装 APK 后无障碍会解绑，需要再授权一次；令牌因此默认复用，不让它变成每次都要重来。
- 服务只在无障碍被系统绑定时活着，用户关掉无障碍即彻底失效——这是有意的。
- 不做截图：截屏走 `screencap` 更省事，没必要在服务里碰硬件缓冲区。

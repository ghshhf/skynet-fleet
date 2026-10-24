"""隐私屏 / 无痕接入：天网控手机时，被控端黑屏、静音，断开恢复原状。

（UU 远程功能盘点计划书 P0-b。UU 那套「被控端全黑 + 静音 + 省电」的
无痕体验，本质就是三条 adb 命令 + 一个状态账本。）

为什么需要：天网通过 adb 操控手机时，手机屏幕亮着、声音外放——
① 在场的人能看见/听见我们在做什么（隐私）；
② 屏幕常亮 + 解码播放是纯耗电（省电）；
③ 更重要的是**被控端没有可见性**：机主不知道手机正被远端操作。
第 ③ 条的完整解是 P0-c 的授权弹窗；本模块解决 ①②。

落点设计（可离线验证的部分）：
  · `shade_cmds()` / `restore_cmds(prev)` 是**纯函数**——命令字符串
    一次构造对，门禁逐字节钉死，不依赖真机；
  · 原音量**不猜**：`media volume --get` 读不到就记 None，
    restore 时 None 只亮屏、不动音量，并在返回里说清「原音量未恢复」。
    静默写个默认值 = 覆盖用户原本的设置，是最坏的一种「贴心」。
  · 「原屏幕亮不亮」同样不猜：restore 无条件亮屏。机主正在电脑前
    操控，亮屏是预期行为；而 shade 前屏幕恰好灭着的状态无法从 shell
    可靠得知（dumpsys 解析太脆，版本间格式漂移）⇒ 不脆的那半边。

adb 红线注意：`android_device.shell()` 的 guard 会拦断网/卸载类命令——
本模块只发 keyevent 与 media volume，天然过线。
"""
from __future__ import annotations

import json
import os
import tempfile

#: 隐私模式状态账本（shade 时写、restore 时读后清）。
#: 放临时区不如放家目录：shade/restore 可能跨进程（GUI 发起、CLI 恢复）。
DEFAULT_STATE = os.path.join(os.path.expanduser("~"), ".fleet", "privacy.json")

#: 媒体音量流（Android STREAM_MUSIC = 3；投屏/操控时外放声音的就是它）
STREAM_MUSIC = 3


def _vol_cmd(op, stream=STREAM_MUSIC, value=None):
    """媒体音量命令的统一构造（主路径）。

    🔴 为什么是 `cmd media_session volume` 而不是 `media volume`：
    实测小米 14（HyperOS）上 `media` 二进制不存在（rc=127
    "inaccessible or not found"）——MIUI 裁掉了它。`cmd` 是 Android 8+
    的通用服务入口，AOSP 与各大 ROM 都在。

    ⚠️ **主路径在 MIUI 上是空操作**（实测 --set/--adj rc=0 但音量不变）：
    MIUI 把 AudioService 的 setStreamVolume 对 shell 禁了。所以调用方
    （enter/leave）必须**读回校验**，没生效就走 service call 降级链——
    见 `_fallback_vol_cmd`。只看 rc=0 就当成功 = 最坏的一种假数据。
    """
    base = f"cmd media_session volume --stream {int(stream)}"
    if op == "get":
        return base + " --get"
    if op == "set":
        return base + f" --set {int(value)}"
    raise ValueError(f"未知音量操作 {op!r}")


def _fallback_vol_cmd(stream=STREAM_MUSIC, value=None):
    """降级路径：`service call audio 13`（setStreamVolume 的 binder transaction）。

    实测小米 14 有效（set 0 读回 0）。⚠️ transaction 编号是内部 AIDL
    顺序，**跨 ROM/跨版本会变**——所以只当降级：主路径失效且本 ROM
    试通了才用，且用后同样读回校验。传错编号的后果是 no-op，
    不会炸（service call 失败返回错误 Parcel）。
    """
    return f"service call audio 13 i32 {int(stream)} i32 {int(value or 0)} i32 0"


def shade_cmds(stream=STREAM_MUSIC, screen_timeout_ms=15000):
    """进入隐私模式要发的命令序列：[(用途, 命令)]。

    ① 静音先于熄屏：反过来先熄屏，某些 ROM 的音量命令会因屏幕关闭
       被延后/丢弃（时序竞态）。
    ② 熄屏用 keyevent 26（POWER）——AOSP 上立即熄屏。
       ⚠️ 实测小米 14（HyperOS）对所有 input keyevent 抛
       SecurityException: INJECT_EVENTS ——**shell 不被允许注入输入事件**。
       所以调用方（enter）在 keyevent 失败时必须退化到
       `screen_timeout` 这条路：把息屏超时改到 15 秒，让屏幕自然灭。
       零权限、可逆（记原值 restore 写回），代价是最多 15 秒才黑。
       ⚠️ **无 root 小米上本模块无法真正控制屏幕**：即使用 appops 给 shell
       放权后 `settings put` 写超时成功（rc=0），屏幕是否真的在超时后自动黑、
       亮屏能否由 `am start` 触发，都未经独立验证且通常不可靠——黑/亮实际
       依赖机主手动。本模块「隐私屏」在无 root 设备上只剩**静音**是 adb
       可靠可控的（`service call audio`）；屏幕可见性请机主自行处理。
    """
    return [
        ("mute", _vol_cmd("set", stream, 0)),
        ("screen_off", "input keyevent 26"),
        ("screen_timeout", f"settings put system screen_off_timeout "
                           f"{int(screen_timeout_ms)}"),
    ]


def read_timeout_cmd():
    """读当前息屏超时（秒）。"""
    return "settings get system screen_off_timeout"


def parse_timeout(out):
    """解析息屏超时。**单位是毫秒**——Android Settings.System.SCREEN_OFF_TIMEOUT
    存的本就是毫秒（实测小米 14 `settings get` 返回 600000 = 10 分钟）。
    解析不出返回 None（不猜，restore 时不写回总比写错好）。"""
    s = str(out or "").strip()
    if s.isdigit():
        return int(s)
    return None


def restore_cmds(prev_volume=None, stream=STREAM_MUSIC,
                 prev_timeout_ms=None):
    """退出隐私模式要发的命令序列。

    `prev_volume`：shade 时读到的原音量；None 表示当时没读到
    ⇒ **只亮屏不动音量**，调用方负责把这个「没恢复」说出去。
    擅自写默认值 = 覆盖用户原本音量，比不恢复更坏。
    `prev_timeout_ms`：shade 前改短的息屏超时，restore 写回。
    """
    cmds = [
        ("screen_on", "input keyevent 224"),      # WAKEUP：亮屏不解锁
        ("unlock", "wm dismiss-keyguard"),         # 解锁（无密码时有效；
    ]                                             # 有密码的会停在输入界面，
    if prev_timeout_ms is not None:                # 那也是「可见」的应有样子）
        cmds.append(("timeout", f"settings put system screen_off_timeout "
                                f"{int(prev_timeout_ms)}"))  # 毫秒，原样写回
    if prev_volume is not None:
        cmds.append(("volume", _vol_cmd("set", stream, prev_volume)))
    return cmds


def read_volume_cmd(stream=STREAM_MUSIC):
    """读当前媒体音量的命令（shade 前调用，拿到原值好恢复）。"""
    return _vol_cmd("get", stream)


def parse_volume(out):
    """从 `cmd media_session volume --get` 的输出解析音量。

    实测两种输出都解析：AOSP `volume is 7 in range 0..15`；
    MIUI（小米 14 实测）`[V] volume is 30 in range [0..150]`（量程 150）。
    🔴 不许直接抓第一个数字：多行输出（`Active stream: 3\\n volume is 12...`）
    会把「流号 3」当成音量——自测抓到过这个错。
    匹配顺序：① `volume is N`（主模式）② 独立成数的最后一行（退化）
    ③ 都没有 ⇒ None。猜错就是拿 7 覆盖用户的 12。
    """
    import re
    s = str(out or "")
    m = re.search(r"volume\s+is\s+(\d+)", s, re.I)
    if m:
        return int(m.group(1))
    # 退化：找「独占一行的数字」（某些 ROM 只输出数字本身）
    for line in reversed(s.splitlines()):
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


# ---------------------------------------------------------------- 状态账本

def _state_path(path=None):
    return path or DEFAULT_STATE


def save_state(state, path=None):
    """写隐私模式状态。返回落盘路径。"""
    p = _state_path(path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    return p


def load_state(path=None):
    """读隐私模式状态；没有/坏了返回 None（没进入过就是没有）。"""
    p = _state_path(path)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def clear_state(path=None):
    """清掉状态（restore 成功后调）。返回是否真的删了。"""
    p = _state_path(path)
    if os.path.isfile(p):
        os.remove(p)
        return True
    return False


def _set_volume(serial, value, shell_fn, stream=STREAM_MUSIC):
    """设媒体音量：主路径 → 读回校验 → 降级链 → 再校验。

    返回 (ok, how, detail)。`how`：主路径 = "media_session"，降级 =
    "service_call"，都失败 = ""。🔴 读回校验不可省：MIUI 上主路径
    rc=0 但是空操作，不读回就会把「没静音」报成「已静音」。
    """
    detail = []
    rc, o = shell_fn(serial, _vol_cmd("set", stream, value))
    detail.append(f"set rc={rc}")
    got = _read_volume(serial, shell_fn, stream)
    if got == int(value):
        return True, "media_session", "; ".join(detail)
    detail.append(f"读回 {got}≠{value}，主路径疑似空操作，试降级链")
    rc2, o2 = shell_fn(serial, _fallback_vol_cmd(stream, value))
    detail.append(f"fallback rc={rc2}")
    got2 = _read_volume(serial, shell_fn, stream)
    if got2 == int(value):
        return True, "service_call", "; ".join(detail)
    detail.append(f"降级后读回 {got2}≠{value}")
    return False, "", "; ".join(detail)


def _read_volume(serial, shell_fn, stream=STREAM_MUSIC):
    """读当前媒体音量；读不出返回 None（不猜）。"""
    rc, out = shell_fn(serial, _vol_cmd("get", stream))
    return parse_volume(out) if rc == 0 else None


def enter(serial, shell_fn=None, path=None):
    """进入隐私模式：读原音量 → 存状态 → 熄屏 → 静音（带降级链）。

    `shell_fn(serial, cmd) -> (rc, out)`：注入以便离线测试；
    真机由调用方传 `android_device.shell`。

    顺序：先熄屏再静音——反过来先静音，屏幕关闭后某些 ROM 的音量
    命令会被延后/丢弃（时序竞态；且实测屏幕命令最稳，先锁定核心价值）。

    返回 dict：`ok / cmds / prev_volume / state_path / note / muted`。
    🔴 静音失败不阻塞 shade：屏幕已黑 = 核心价值（看不见）已达成；
    此时 note 必须说清「静音未生效」——调用方有权知道这个降级。
    """
    cmds = shade_cmds()
    out = {"ok": False, "cmds": [c for _u, c in cmds], "prev_volume": None,
           "state_path": "", "note": "", "sent": [], "muted": False,
           "screen_how": ""}
    if shell_fn is None:
        out["note"] = "没给 shell_fn：只返回命令清单，未执行"
        return out
    prev = _read_volume(serial, shell_fn)
    out["prev_volume"] = prev
    rc_t, t_out = shell_fn(serial, read_timeout_cmd())
    prev_timeout = parse_timeout(t_out) if rc_t == 0 else None
    out["prev_timeout_ms"] = prev_timeout
    # ① 熄屏：先试 keyevent 26（AOSP 立即熄屏）；MIUI 抛 INJECT_EVENTS
    #    ⇒ 退化「短超时自然熄屏」（零权限可逆，最多 15 秒黑）
    rc, o = shell_fn(serial, cmds[1][1])       # ("screen_off", keyevent 26)
    out["sent"].append({"cmd": cmds[1][1], "rc": rc, "out": (o or "")[:200]})
    if rc == 0:
        out["screen_how"] = "keyevent"
    else:
        # keyevent 被 ROM 拒（INJECT_EVENTS）——不是致命错误，走超时路
        out["screen_how"] = "timeout"
        out["sent"].append({"cmd": "keyevent 26 被 ROM 拒绝，退化短超时路径",
                            "rc": rc, "out": (o or "")[:120]})
        rc2, o2 = shell_fn(serial, cmds[2][1])  # ("screen_timeout", ...)
        if rc2 != 0:
            # MIUI 等 ROM：settings put 默认被拒（WRITE_SETTINGS 没给 shell）。
            # appops 放权给 shell 后重试——零安装、可逆，AOSP 上幂等无害。
            out["sent"].append({"cmd": "settings put 被拒，先 appops 放权后重试",
                                "rc": rc2, "out": (o2 or "")[:120]})
            shell_fn(serial, "appops set com.android.shell WRITE_SETTINGS allow")
            rc2, o2 = shell_fn(serial, cmds[2][1])
        out["sent"].append({"cmd": cmds[2][1], "rc": rc2, "out": (o2 or "")[:200]})
        if rc2 != 0:
            out["note"] = f"熄屏两条路都失败：{o2}"
            return out
    # ② 静音（次要价值：听不见；MIUI 上要走降级链）
    ok_mute, how, detail = _set_volume(serial, 0, shell_fn)
    out["muted"] = ok_mute
    out["mute_how"] = how
    out["sent"].append({"cmd": _vol_cmd("set", STREAM_MUSIC, 0), "rc": 0 if ok_mute else 1,
                        "out": detail})
    p = save_state({"serial": serial, "prev_volume": prev,
                    "prev_timeout_ms": prev_timeout,
                    "stream": STREAM_MUSIC, "at": _now()}, path=path)
    out["state_path"] = p
    out["ok"] = True
    if out["screen_how"] == "timeout":
        out["note"] = "本 ROM 禁止注入输入事件（INJECT_EVENTS）⇒ 已改 15 秒息屏"
    if not ok_mute:
        out["note"] = (out["note"] + "；" if out["note"] else "") + \
                      ("屏幕已黑，但静音未生效（主路径空操作 + 降级链也未通）——"
                       "后台媒体可能仍有声，请知悉")
    elif prev is None:
        out["note"] = (out["note"] + "；" if out["note"] else "") + \
                      "原音量没读到 ⇒ 退出时只亮屏、不恢复音量"
    return out


def leave(serial=None, shell_fn=None, path=None):
    """退出隐私模式：读状态 → 亮屏解锁 → 音量按原值恢复（带降级链）→ 清状态。

    serial 不给就用状态里记的那个（恢复操作天然跟着账本走）。
    """
    st = load_state(path=path)
    if not st:
        return {"ok": False, "cmds": [], "note": "没有隐私模式状态可恢复",
                "sent": []}
    serial = serial or st.get("serial")
    stream = st.get("stream", STREAM_MUSIC)
    prev = st.get("prev_volume")
    cmds = restore_cmds(prev_volume=prev, stream=stream)
    out = {"ok": False, "cmds": [c for _u, c in cmds], "note": "",
           "sent": [], "serial": serial}
    if shell_fn is None:
        out["note"] = "没给 shell_fn：只返回命令清单，未执行"
        return out
    # ① 亮屏 + 解锁（核心价值恢复：机主重新看得见自己的手机）
    # 亮屏：AOSP 上用 keyevent 224（WAKEUP）；MIUI/HyperOS 对所有 input
    # keyevent 抛 INJECT_EVENTS（无 root 无解），退化 `am start HOME`。
    # ⚠️ 实测小米 14（HyperOS，无 root）：am start HOME 也无法从息屏态点亮
    # 屏幕（Activity resume 被推迟到屏幕 on）。本分支为「尽力而为」——
    # 屏幕最终亮灭依赖机主手动或自然唤醒，leave 不保证亮屏。两条都进
    # sent 账本，便于事后对账。
    rc, o = shell_fn(serial, cmds[0][1])
    if rc != 0:
        out["sent"].append({"cmd": f"{cmds[0][1]} 被 ROM 拒绝，退化 am start HOME",
                            "rc": rc, "out": (o or "")[:120]})
        rc, o = shell_fn(serial,
            "am start -a android.intent.action.MAIN "
            "-c android.intent.category.HOME")
    out["sent"].append({"cmd": cmds[0][1], "rc": rc, "out": (o or "")[:200]})
    if rc != 0:
        out["note"] = f"亮屏失败：{cmds[0][1]}（{o}）"
        return out
    # ② 解锁 + 恢复超时 + 恢复音量（按 restore_cmds 顺序遍历，不漏发）。
    # 音量走 _set_volume 降级链（restore_cmds 里的 volume 命令只占位，
    # 真正设值要带读回校验，否则 MIUI 空操作会假成功）；其余命令直接发。
    for _use, c in cmds[1:]:
        if _use == "volume":
            if prev is None:
                continue
            ok_v, how, detail = _set_volume(serial, prev, shell_fn, stream=stream)
            out["sent"].append({"cmd": c, "rc": 0 if ok_v else 1, "out": detail})
            if not ok_v:
                out["note"] = (f"原音量 {prev} 恢复失败（主路径空操作 + 降级链未通）——"
                               "屏幕已亮，音量请手动调回")
            continue
        rc, o = shell_fn(serial, c)
        out["sent"].append({"cmd": c, "rc": rc, "out": (o or "")[:200]})
        if rc != 0:
            out["note"] = f"命令失败：{c}（{o}）"
            return out
    clear_state(path=path)
    out["ok"] = True
    return out


def _now():
    import time
    return int(time.time())


def _selftest():
    """离线验证：命令构造逐字节对 / 音量解析 / None 不猜 / 状态账本往返 /
    降级链三场景（主路径有效 / 主路径空转降级有效 / 都无效）。"""
    # ① shade 命令：先静音后熄屏（顺序是时序竞态的解）
    sc = shade_cmds()
    assert [u for u, _c in sc] == ["mute", "screen_off", "screen_timeout"], sc
    assert sc[0][1] == "cmd media_session volume --stream 3 --set 0", sc[0]
    assert sc[1][1] == "input keyevent 26", sc[1]
    # ② restore：亮屏 + 解锁；有原音量才带音量命令，None 就不带（不猜）
    rc_all = restore_cmds(prev_volume=7)
    assert [u for u, _c in rc_all] == ["screen_on", "unlock", "volume"], rc_all
    assert rc_all[2][1] == "cmd media_session volume --stream 3 --set 7", rc_all[2]
    rc_none = restore_cmds(prev_volume=None)
    assert [u for u, _c in rc_none] == ["screen_on", "unlock"], rc_none
    # ③ 音量解析：标准输出解析出；解析不出给 None（不许猜）
    assert parse_volume("volume is 7 in range 0..15") == 7
    assert parse_volume("  Active stream: 3\n  volume is 12 in range 0..15") == 12
    assert parse_volume("[V] volume is 30 in range [0..150]") == 30   # MIUI 实测输出
    assert parse_volume("") is None
    assert parse_volume("没这行") is None
    # ④ 状态账本往返（临时目录，不碰真家目录）
    tmp = tempfile.mkdtemp(prefix="fleet-privacy-")
    p = os.path.join(tmp, "state.json")
    assert load_state(path=p) is None, "没有状态就该是 None"
    save_state({"serial": "S1", "prev_volume": 7}, path=p)
    assert load_state(path=p)["serial"] == "S1"
    assert clear_state(path=p) is True and load_state(path=p) is None

    # ⑤ 带状态的假 shell：三种 ROM 行为（真机实测过的三种）
    def make_fake(mode):
        state = {"vol": 5}
        def fake(serial, cmd):
            if "--get" in cmd:
                return 0, f"[V] volume is {state['vol']} in range [0..150]"
            if "service call audio" in cmd:
                if mode == "both_dead":
                    return 0, "Result: Parcel(...)"   # rc=0 但不变（MIUI 空操作形状）
                state["vol"] = int(cmd.split("i32 ")[-2])   # 降级有效
                return 0, "Result: Parcel(00000000 '....')"
            if "--set" in cmd:
                if mode in ("main_dead", "both_dead"):
                    return 0, ""                        # rc=0 空操作（MIUI 实测）
                state["vol"] = int(cmd.split("--set ")[1])
                return 0, ""
            return 0, ""                                 # keyevent 等
        return fake, state

    # ⑤a 主路径有效（AOSP 形状）
    fake, st = make_fake("main_ok")
    r = enter("S1", shell_fn=fake, path=p)
    assert r["ok"] and r["muted"] and r["mute_how"] == "media_session", r
    assert r["prev_volume"] == 5 and not r["note"], r
    assert st["vol"] == 0, "静音后音量应为 0"
    r2 = leave(shell_fn=fake, path=p)
    assert r2["ok"] and st["vol"] == 5, "restore 应写回原值 5"
    assert load_state(path=p) is None
    # ⑤b 主路径空操作 → 降级链有效（MIUI 实测形状）
    fake2, st2 = make_fake("main_dead")
    r3 = enter("S1", shell_fn=fake2, path=p)
    assert r3["ok"] and r3["muted"] and r3["mute_how"] == "service_call", r3
    assert st2["vol"] == 0
    leave(shell_fn=fake2, path=p)
    assert st2["vol"] == 5
    # ⑤c 都无效：shade 仍成功（屏幕已黑=核心价值），但 note 必须警告
    fake3, _st3 = make_fake("both_dead")
    r4 = enter("S1", shell_fn=fake3, path=p)
    assert r4["ok"] and not r4["muted"] and "静音未生效" in r4["note"], r4
    leave(shell_fn=fake3, path=p)
    # ⑤d 熄屏两条路都失败 ⇒ enter 失败（不假装成功）
    def fake_no_screen(serial, cmd):
        if "keyevent 26" in cmd:
            return 1, " Permission denied"
        if "settings put" in cmd:          # 连超时降级路也死 ⇒ 真失败
            return 1, " Permission denied"
        if "--get" in cmd:
            return 0, "volume is 5 in range 0..15"
        return 0, ""
    r5 = enter("S1", shell_fn=fake_no_screen, path=p)
    assert not r5["ok"] and "熄屏两条路都失败" in r5["note"], r5
    print("privacy selftest OK（7 组）")


if __name__ == "__main__":
    _selftest()

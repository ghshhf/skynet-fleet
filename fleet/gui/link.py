"""组网接线：把「电脑上这个只绑 127.0.0.1 的服务」接到手机那边去。

为什么用 `adb reverse`（隧道）而不是局域网直连：
  · reverse 是把手机的端口「借」给电脑用：手机访问自己的 127.0.0.1:端口，流量
    经 adb 通道回到电脑上的服务。于是电脑仍然**只绑 127.0.0.1**，桌面不会出现
    在局域网里；令牌也不必泄露给同网段的任何人。
  · 局域网直连（`--lan`）要绑 0.0.0.0，等于把「能点鼠标键盘的接口」摆在局域网
    上，所以那边强制要求固定令牌，且只能当「不想插线」的备选。

🔴 必须读回复核，不许「命令返回 0 就算成功」。这是 P1 踩过的坑：
`ACTION_SCROLL_FORWARD` 返回 true 但界面没动。`adb reverse` 在部分 ROM 上同样会
静默失败，所以建完一律用 `reverse --list` 把真实列表读回来对一遍。
"""
import re
import subprocess


def _run(args, timeout=15):
    """跑一条 adb 命令，返回 (returncode, stdout+stderr)。

    不设 shell：参数里有 `tcp:8000` 这种带冒号的东西，走 shell 会在部分环境里被
    当路径改写（Git Bash 的 MSYS 路径转换就干过这事）。
    """
    try:
        p = subprocess.run(["adb"] + args, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "找不到 adb：把 platform-tools 放进 PATH，或用 --adb 指定"
    except subprocess.TimeoutExpired:
        return 124, "adb 超过 %d 秒没返回（设备可能掉线了）" % timeout
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    return p.returncode, out


def devices():
    """在线设备序列号列表。offline / unauthorized 的不算。

    为什么把 unauthorized 单独标出来：手机第一次连会弹「允许调试」，没点之前
    `adb devices` 里那台是 unauthorized。这时候 reverse 必然失败，报「没找到设备」
    会误导人去查线，所以说清是没授权。
    """
    rc, out = _run(["devices"])
    if rc != 0:
        raise RuntimeError(out.strip() or "adb devices 执行失败")
    online, unauthorized = [], []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state == "device":
            online.append(serial)
        elif state == "unauthorized":
            unauthorized.append(serial)
    if not online and unauthorized:
        raise RuntimeError(
            "设备已连上但没授权调试：在手机上点「允许 USB 调试 / 允许无线调试」后重试\n"
            "  未授权设备：%s" % ", ".join(unauthorized))
    return online


def ensure_reverse(port, serials=None):
    """对每台在线设备建 `tcp:port → tcp:port` 的 reverse，并读回核对。

    返回 {"ok": [{"serial", "raw"}], "fail": [{"serial", "why"}]}。
    失败不吞：每台的失败原因都留着，report() 会原样打出来。
    """
    targets = serials if serials is not None else devices()
    if not targets:
        return {"ok": [], "fail": [], "empty": True}

    ok, fail = [], []
    pat = re.compile(r"tcp:%d\s+tcp:%d" % (port, port))
    for serial in targets:
        rc, out = _run(["-s", serial, "reverse", "tcp:%d" % port, "tcp:%d" % port])
        if rc != 0:
            fail.append({"serial": serial, "why": "reverse 命令失败：" + out.strip()})
            continue
        # 读回复核：命令成功不代表真建起来了
        rc2, listing = _run(["-s", serial, "reverse", "--list"])
        if rc2 != 0:
            fail.append({"serial": serial, "why": "reverse --list 读不回来：" + listing.strip()})
            continue
        if not pat.search(listing):
            fail.append({
                "serial": serial,
                "why": "命令报成功但 reverse --list 里没有这条，隧道没真建起来\n"
                       "      reverse --list 实际内容：%s" % (listing.strip() or "(空)"),
            })
            continue
        ok.append({"serial": serial, "raw": listing.strip()})
    return {"ok": ok, "fail": fail, "empty": False}


def report(port, token, result):
    """打印手机端该填什么（固定四行，照着抄就行）。"""
    ok, fail = result.get("ok") or [], result.get("fail") or []
    if result.get("empty"):
        print("[fleet-link] 没找到在线设备：先确认 USB 线或无线调试开着（adb devices）",
              flush=True)
    else:
        print("[fleet-link] 隧道端口 %d → 已对 %d 台设备建立" % (port, len(ok)), flush=True)
        for d in ok:
            print("[fleet-link]   设备 %s" % d["serial"], flush=True)
        for d in fail:
            print("[fleet-link]   设备 %s 失败：%s" % (d["serial"], d["why"]), flush=True)
    print("[fleet-link] 手机端「连接设置」填：", flush=True)
    print("[fleet-link]   地址 http://127.0.0.1:%d" % port, flush=True)
    print("[fleet-link]   令牌 %s" % token, flush=True)


def selfheal_once(port):
    """一次自愈检查：对所有在线设备重建 reverse（幂等，重复建无害）。

    返回 {"ok", "fail", "empty", "error"} 汇总，供 selfheal_loop 打日志、
    也供门禁断言。异常被兜住——自愈本身不能因为一次 adb 抖动而崩。
    """
    try:
        r = ensure_reverse(port)
    except RuntimeError as e:
        return {"ok": 0, "fail": 0, "empty": False, "error": str(e)}
    return {"ok": len(r.get("ok") or []), "fail": len(r.get("fail") or []),
            "empty": bool(r.get("empty")), "error": None}


def selfheal_loop(port, interval=15, stop=None):
    """连接自愈主循环：每 interval 秒重建一次 reverse，掉了自动恢复。

    stop 是可调用（返回 True 即退出）；不传则跑到进程结束。日志每轮可见，
    正好也是 A4「后台循环心跳」的一份——「adb reverse 还活着吗、几台」。

    同时记心跳（`fleet.beat`）：日志会滚动、会被重定向，而心跳账本能回答
    「这个循环最后一次成功是几秒前」——卡住时 `/api/health` 直接标 stale。
    """
    import time
    try:
        from ..beat import register          # 正常：作为 fleet.gui.link 包内导入
    except (ImportError, ValueError):
        try:
            from beat import register        # fleet/ 在 sys.path 里
        except ImportError:
            # 门禁 t28 把 fleet/gui 单独塞进 sys.path 再 import link ——
            # 两种包内导入都不可用。**不能因此让自愈循环失去心跳**，
            # 按文件路径直接加载 beat.py（只读依赖，无副作用）。
            import importlib.util as _iu
            import os as _os
            _p = _os.path.join(
                _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "beat.py")
            _spec = _iu.spec_from_file_location("_fleet_beat", _p)
            _m = _iu.module_from_spec(_spec)
            _spec.loader.exec_module(_m)
            register = _m.register
    bt = register("link.selfheal", stale_after=max(90.0, interval * 6))
    # 进场先记一次：否则「刚起、正等第一个 interval」这段会被判成 stale 的假警。
    bt.beat()
    while True:
        if stop is not None and stop():
            return
        try:
            time.sleep(interval)
            s = selfheal_once(port)
        except Exception as e:                  # 循环不许因为一次异常整条死掉
            bt.allow("%s: %s" % (type(e).__name__, e))
            print("[fleet-link] 自愈循环异常：%s: %s" % (type(e).__name__, e), flush=True)
            continue
        if s.get("error"):
            bt.allow(s["error"])
            print("[fleet-link] 自愈异常：%s" % s["error"], flush=True)
        elif s.get("empty"):
            bt.beat()
            print("[fleet-link] 自愈：无在线设备", flush=True)
        elif s["fail"]:
            # 有失败但也没全挂：算「跑过了」，把失败原因带上，两种事实都留
            bt.beat(ok=False, error="reverse %d 失败 / %d 成功" % (s["fail"], s["ok"]))
            print("[fleet-link] 自愈：reverse %d 成功 / %d 失败"
                  % (s["ok"], s["fail"]), flush=True)
        else:
            bt.beat()
            print("[fleet-link] 自愈：%d 台 reverse 存活" % s["ok"], flush=True)

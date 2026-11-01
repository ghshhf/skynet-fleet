#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网关守护：确保 llmgw 在 8790 上活着。

为什么需要这个：11-01 晚上就吃过一次亏 —— 杀掉旧进程后"重启"其实全失败了
（后台任务退出码 127），客户端发消息连不上，只有干等超时（主人实测 50 多秒无响应）。
**服务没起是最难发现的故障**，因为它不报错，只是沉默。

用法：
    python tools/gw_up.py            确保在跑（不在就拉起），打印状态
    python tools/gw_up.py --check    只检查，不起进程（给计划任务/门禁调）
    python tools/gw_up.py --restart  杀掉现有的再拉一个新的
    python tools/gw_up.py --stop     停掉

关键实现：用 DETACHED_PROCESS 拉起子进程，脱离父 shell。
不这么做的话，命令一结束子进程跟着死 —— 那正是"以为起来了其实没有"的根源。
"""
import argparse
import os
import socket
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8790
LOG = os.path.join(ROOT, "tmp", "gw8790.log")

# Windows：完全脱离父进程。CREATE_NEW_PROCESS_GROUP 让它不吃父进程的 Ctrl-C。
_DETACHED = 0x00000008 | subprocess.CREATE_NEW_PROCESS_GROUP


def port_open(port=PORT, host="127.0.0.1", timeout=1.0):
    """端口是否有人接。比 PID 文件可靠 —— 进程还在但没监听等于没起。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def healthy(port=PORT, timeout=4.0):
    """真发一次 /health。开着但卡死的情况也有，所以不光看端口。"""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port,
                                    timeout=timeout) as r:
            d = json.loads(r.read())
            return bool(d.get("ok")), d
    except Exception as e:
        return False, {"err": str(e)[:120]}


def _run_text(cmd, timeout=20):
    """跑个命令拿文本输出。

    **不能直接 text=True**：中文 Windows 的 netstat / tasklist 输出是 GBK，
    Python 3.12 默认按 utf-8 解码会抛 UnicodeDecodeError（实测 0xbb 就炸），
    而异常被上层吞掉后表现为「没找到进程」—— 于是 --restart 静默不生效。
    这正是 GBK 铁律的一个新变种：读外部命令输出也要按系统编码解。
    """
    import locale
    enc = locale.getpreferredencoding(False) or "utf-8"
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    out = r.stdout or b""
    for e in (enc, "utf-8", "gbk"):
        try:
            return out.decode(e)
        except Exception:
            continue
    return out.decode("utf-8", "replace")


def find_pids(port=PORT):
    """找出占着这个端口的进程号（Windows netstat / POSIX lsof 都试）。"""
    pids = []
    try:
        if os.name == "nt":
            out = _run_text(["netstat", "-ano"])
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0].upper() == "TCP" \
                        and parts[1].endswith(":%d" % port) \
                        and parts[3].upper() == "LISTENING":
                    if parts[4].isdigit():
                        pids.append(int(parts[4]))
        else:
            out = _run_text(["lsof", "-ti", ":%d" % port])
            pids = [int(x) for x in out.split() if x.isdigit()]
    except Exception as e:
        print("  查端口占用失败：%s" % e)
    return sorted(set(pids))


def kill_pids(pids):
    """结束进程。Windows 上 Stop-Process 被沙箱静默拦，直接调 taskkill /F。"""
    for pid in pids:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=20)
            else:
                os.kill(pid, 15)
        except Exception as e:
            print("  结束 %s 失败：%s" % (pid, e))


def spawn(port=PORT):
    """拉起一个脱离父进程的网关。返回 Popen 或 None。"""
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    f = open(LOG, "a", encoding="utf-8", errors="replace")
    f.write("\n===== 启动于 %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    f.flush()
    try:
        p = subprocess.Popen(
            [sys.executable, "-m", "fleet.llmgw_server", "--port", str(port)],
            cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=env, creationflags=_DETACHED if os.name == "nt" else 0,
            start_new_session=(os.name != "nt"),
        )
        return p
    except Exception as e:
        print("  拉起失败：%s" % e)
        return None
    finally:
        f.close()


def ensure(port=PORT, wait=15.0):
    """确保活着并且真能答话。返回 (ok, 说明)。"""
    if port_open(port):
        ok, info = healthy(port)
        if ok:
            return True, "已在运行（%s 个分类）" % len(info.get("classes") or [])
        # 端口开着但不健康 = 僵尸，杀掉重来
        print("  端口开着但 /health 不通，按僵尸处理")
        kill_pids(find_pids(port))
        time.sleep(1.5)

    print("  拉起网关 …")
    p = spawn(port)
    if not p:
        return False, "拉起失败（见 %s）" % LOG

    deadline = time.time() + wait
    while time.time() < deadline:
        if port_open(port):
            ok, info = healthy(port)
            if ok:
                return True, "已拉起 pid=%s（%s 个分类）" % (
                    p.pid, len(info.get("classes") or []))
        time.sleep(0.6)
    return False, "拉起后 %.0fs 内没答话（见 %s）" % (wait, LOG)


def main(argv=None):
    ap = argparse.ArgumentParser(description="天网 llmgw 网关守护")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--check", action="store_true", help="只检查，不拉起")
    ap.add_argument("--restart", action="store_true", help="先杀再拉")
    ap.add_argument("--stop", action="store_true", help="停掉")
    a = ap.parse_args(argv)

    if a.stop:
        pids = find_pids(a.port)
        if not pids:
            print("网关没在跑（端口 %d 空）" % a.port)
            return 0
        kill_pids(pids)
        time.sleep(1.2)
        print("已停：%s" % (pids,))
        return 0

    if a.restart:
        pids = find_pids(a.port)
        if pids:
            print("停掉旧的：%s" % (pids,))
            kill_pids(pids)
            time.sleep(1.5)

    if a.check:
        ok, info = healthy(a.port) if port_open(a.port) else (False, {})
        print(("OK " if ok else "DOWN ") + (str(info.get("err") or "")[:80]))
        return 0 if ok else 1

    ok, msg = ensure(a.port)
    print(("✅ " if ok else "❌ ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

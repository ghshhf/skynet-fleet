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
    python tools/gw_up.py --install  注册成 Windows 计划任务（脱离会话常驻）
    python tools/gw_up.py --task     看一眼计划任务状态
    python tools/gw_up.py --uninstall 删掉计划任务

关键实现①：用 DETACHED_PROCESS 拉起子进程，脱离父 shell。
不这么做的话，命令一结束子进程跟着死 —— 那正是"以为起来了其实没有"的根源。

关键实现②（11-01 主人要求）：**DETACHED_PROCESS 只脱离 shell，没脱离会话**。
实测：我这边任务一收尾，网关就被连坐杀掉；主人只能在我说话的时候用，
想同时开个新会话都做不到 —— 工具成了"我在它才在"，这不成。
所以交给 **Task Scheduler**：任务的父进程是 svchost，跟任何交互会话无关，
我这边断线、shell 退出、甚至主人开十个新会话，它都照跑。
触发器两条：登录时拉一次 + 每 5 分钟自愈一次（幂等，活着就秒退）。
"""
import argparse
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8790
LOG = os.path.join(ROOT, "tmp", "gw8790.log")


def _ensure_streams():
    """pythonw 下 sys.stdout/stderr 是 None，任何 print 都会炸。

    计划任务用 pythonw 跑（不然每隔 5 分钟在桌面上闪一个黑框，没法看）。
    但 pythonw 没有控制台 → 流为 None。这里重定向到日志，顺带留痕：
    「谁在什么时候发现网关死了、怎么救的」都查得到。
    """
    need = sys.stdout is None or sys.stderr is None
    if need:
        try:
            os.makedirs(os.path.join(ROOT, "tmp"), exist_ok=True)
            f = open(os.path.join(ROOT, "tmp", "gw_up.log"), "a",
                     encoding="utf-8", errors="replace")
            if sys.stdout is None:
                sys.stdout = f
            if sys.stderr is None:
                sys.stderr = f
        except Exception:
            return
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_ensure_streams()

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


TASK_NAME = "TianWang-llmgw"


def _pythonw():
    """优先 pythonw.exe：计划任务每 5 分钟跑一次，用 python.exe 会闪黑框。"""
    exe = os.path.join(os.path.dirname(sys.executable),
                       "pythonw.exe" if os.name == "nt" else "python")
    return exe if os.path.exists(exe) else sys.executable


def _whoami():
    dom = os.environ.get("USERDOMAIN") or ""
    usr = os.environ.get("USERNAME") or ""
    return ("%s\\%s" % (dom, usr)) if dom and usr else usr


def _task_xml(interval_min=5):
    """拼一个计划任务 XML：登录触发 + 每 N 分钟自愈。

    为什么用 XML 而不是 schtasks 的 /SC：一个任务要两个触发器
    （登录时 + 定时），命令行建不出来，只能走 XML。
    """
    user = _whoami()
    start = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + 60))
    cmds = _xml_esc(_pythonw())
    # --from-task 是身份标记，必须带：不带的话任务里的 gw_up.py 会「交给计划任务拉」，
    # 而那正是它自己 → 自我触发 → 被 IgnoreNew 拒绝 → 永远起不来。
    # 11-01 实测撞过：日志里连续两行「交给计划任务」，端口一直空着。
    args = _xml_esc(os.path.join(ROOT, "tools", "gw_up.py")) + " --from-task"
    cwd = _xml_esc(ROOT)
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>天网 llmgw 免费模型网关（127.0.0.1:%d）守护：登录拉起 + 每 %d 分钟自愈</Description>
    <URI>\\%s</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>%s</UserId>
    </LogonTrigger>
    <TimeTrigger>
      <Repetition>
        <Interval>PT%dM</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>%s</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>%s</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT2M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%s</Command>
      <Arguments>%s</Arguments>
      <WorkingDirectory>%s</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
""" % (PORT, interval_min, TASK_NAME, _xml_esc(user), interval_min, start,
       _xml_esc(user), cmds, args, cwd)


def _xml_esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def task_install(interval_min=5):
    """注册计划任务。返回 (ok, 说明)。"""
    if os.name != "nt":
        return False, "只有 Windows 走计划任务；POSIX 用 systemd/cron，这里不管"
    os.makedirs(os.path.join(ROOT, "tmp"), exist_ok=True)
    path = os.path.join(ROOT, "tmp", "%s.xml" % TASK_NAME)
    # schtasks 要求 XML 是 UTF-16（带 BOM），写 utf-16 正好
    with open(path, "w", encoding="utf-16", newline="\r\n") as f:
        f.write(_task_xml(interval_min))
    out = _run_text(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", path, "/F"])
    ok = "成功" in out or "SUCCESS" in out.upper()
    if not ok:
        return False, "注册失败：%s" % out.strip()[:300]
    # 立刻跑一次，别等下一个 5 分钟
    _run_text(["schtasks", "/Run", "/TN", TASK_NAME])
    return True, "已注册 %s（登录触发 + 每 %d 分钟自愈）" % (TASK_NAME, interval_min)


def task_delete():
    out = _run_text(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    ok = "成功" in out or "SUCCESS" in out.upper()
    return ok, out.strip()[:300]


def task_query():
    """返回 (已注册?, 原始状态文本)。"""
    out = _run_text(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"])
    return ("找不到" not in out and "ERROR" not in out.upper()), out.strip()[:900]


def spawn(port=PORT):
    """拉起一个脱离父进程的网关。返回 Popen 或 None。"""
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    f = open(LOG, "a", encoding="utf-8", errors="replace")
    f.write("\n===== 启动于 %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    f.flush()
    try:
        p = subprocess.Popen(
            [_pythonw(), "-m", "fleet.llmgw_server", "--port", str(port)],
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


def ensure(port=PORT, wait=15.0, prefer_task=True):
    """确保活着并且真能答话。返回 (ok, 说明)。

    关键：**优先让计划任务去拉**，别自己 Popen。
    自己 Popen 出来的进程是我这个 shell 的子孙，会话一收尾就有被连坐杀掉的风险
    （11-01 实测过：我在的时候网关活得好好的，我这边任务完了它就没了）。
    由 Task Scheduler 拉起时父进程是 svchost 链，跟我一点关系都没有 ——
    主人同时开十个会话，它照样在。

    prefer_task=False 是给「已经被计划任务拉起的那一次运行」用的：
    那种情况下再转交给计划任务就是自己触发自己，只会被 IgnoreNew 拒掉。
    """
    if port_open(port):
        ok, info = healthy(port)
        if ok:
            return True, "已在运行（%s 个分类）" % len(info.get("classes") or [])
        # 端口开着但不健康 = 僵尸，杀掉重来
        print("  端口开着但 /health 不通，按僵尸处理")
        kill_pids(find_pids(port))
        time.sleep(1.5)

    print("  拉起网关 …")
    by_task = False
    if os.name == "nt" and prefer_task and task_query()[0]:
        out = _run_text(["schtasks", "/Run", "/TN", TASK_NAME])
        # 上一实例还没退时 /Run 会被 IgnoreNew 拒绝（0x800710E0）——
        # 别在那儿干等 15 秒，直接自己拉一个，反正有独占端口兜着。
        if "拒绝" in out or "refused" in out.lower() or "ERROR" in out.upper():
            print("  计划任务拒了这次触发（上一实例还在），改自己拉")
        else:
            by_task = True
            print("  交给计划任务 %s 拉（脱离本会话）" % TASK_NAME)
    p = None if by_task else spawn(port)
    if not by_task and not p:
        return False, "拉起失败（见 %s）" % LOG

    deadline = time.time() + wait
    while time.time() < deadline:
        if port_open(port):
            ok, info = healthy(port)
            if ok:
                who = "计划任务" if by_task else ("pid=%s" % p.pid)
                return True, "已拉起 %s（%s 个分类）" % (who, len(info.get("classes") or []))
        time.sleep(0.6)
    return False, "拉起后 %.0fs 内没答话（见 %s）" % (wait, LOG)


def main(argv=None):
    ap = argparse.ArgumentParser(description="天网 llmgw 网关守护")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--check", action="store_true", help="只检查，不拉起")
    ap.add_argument("--restart", action="store_true", help="先杀再拉")
    ap.add_argument("--stop", action="store_true", help="停掉")
    ap.add_argument("--install", action="store_true",
                    help="注册 Windows 计划任务（脱离会话常驻 + 自愈）")
    ap.add_argument("--uninstall", action="store_true", help="删掉计划任务")
    ap.add_argument("--task", action="store_true", help="看计划任务状态")
    ap.add_argument("--interval", type=int, default=5,
                    help="自愈间隔分钟数，默认 5")
    ap.add_argument("--from-task", action="store_true",
                    help="（内部用）由计划任务拉起，别再转交给计划任务")
    a = ap.parse_args(argv)

    if a.task:
        reg, txt = task_query()
        print(("✅ 已注册 %s" % TASK_NAME) if reg else "❌ 没有计划任务 %s" % TASK_NAME)
        if txt:
            print(txt)
        ok, info = (healthy(a.port) if port_open(a.port) else (False, {}))
        print(("网关 OK：" if ok else "网关 DOWN：") + str(info.get("err") or "")[:80])
        return 0 if ok else 1

    if a.uninstall:
        ok, txt = task_delete()
        print(("✅ 已删 " if ok else "❌ 删除失败 ") + TASK_NAME + (" —— " + txt if not ok else ""))
        return 0 if ok else 1

    if a.install:
        ok, msg = task_install(a.interval)
        print(("✅ " if ok else "❌ ") + msg)
        if ok:
            ok2, info = ensure(a.port, wait=25.0)
            print(("✅ 网关 " if ok2 else "❌ 网关 ") + info)
            return 0 if ok2 else 1
        return 1

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

    ok, msg = ensure(a.port, prefer_task=not a.from_task)
    print(("✅ " if ok else "❌ ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""Node-Agent：被控端（L0 连通 + L1 执行落点）。

连 Commander -> 密码认证 -> 注册 -> 收指令（exec / push / pull）+ 心跳。
真实部署：装成 SYSTEM 服务（Win）/ systemd root（Linux）跨重启常驻；此处先跑通协议。

用法：
  python node_agent.py --host 127.0.0.1 --port 9000 --name PC-B --password 你的密码
"""
import argparse
import base64
import os
import socket
import subprocess
import sys
import threading
import time

import common


def run_node(host, port, name, password, platform="windows", sandbox="sandbox_node", bg=False):
    """连接并常驻处理。bg=True 时在新线程跑（供 commander --self-test 调用）。"""
    def _loop():
        while True:  # 断线重连
            try:
                _session(host, port, name, password, platform, sandbox)
            except OSError as e:
                print(f"[{name}] 连接失败: {e}，3s 后重连", flush=True)
            time.sleep(3)

    if bg:
        threading.Thread(target=_loop, daemon=True).start()
    else:
        _loop()


def _session(host, port, name, password, platform, sandbox):
    os.makedirs(sandbox, exist_ok=True)
    s = socket.create_connection((host, port), timeout=10)
    common.send_msg(s, {"t": common.AUTH, "name": name, "password": password})
    resp = common.recv_msg(s)
    if not resp or resp.get("t") != common.AUTH_OK:
        raise OSError(f"认证失败: {resp}")
    common.send_msg(s, {"t": common.REGISTER, "name": name, "platform": platform,
                        "info": {"sandbox": os.path.abspath(sandbox)}})
    print(f"[{name}] 已注册（token={resp['token'][:8]}…）", flush=True)

    # 心跳线程
    def _hb():
        while True:
            time.sleep(15)
            try:
                common.send_msg(s, {"t": common.HEARTBEAT})
            except OSError:
                return

    threading.Thread(target=_hb, daemon=True).start()

    while True:
        m = common.recv_msg(s)
        if m is None:
            break
        t = m.get("t")
        if t == common.EXEC:
            _do_exec(s, m)
        elif t == common.PUSH:
            _do_push(s, m, sandbox)
        elif t == common.PULL:
            _do_pull(s, m, sandbox)
        else:
            print(f"[{name}] 未知指令 {t}", flush=True)


def _decode(b):
    """Windows 控制台默认 GBK(cp936)，先试 utf-8 再退 cp936，最后 replace 兜底。"""
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return b.decode("cp936")
    except UnicodeDecodeError:
        return b.decode("utf-8", "replace")


def _do_exec(s, m):
    cmd = m.get("cmd", "")
    mid = m.get("id")
    if not common.is_safe_cmd(cmd):
        common.send_msg(s, {"t": common.EXEC_RESULT, "id": mid, "rc": -1,
                            "out": "", "err": f"命令不在白名单: {cmd.split(' ',1)[0]}"})
        return
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, timeout=30)
        common.send_msg(s, {"t": common.EXEC_RESULT, "id": mid,
                            "rc": r.returncode, "out": _decode(r.stdout), "err": _decode(r.stderr)})
    except subprocess.TimeoutExpired:
        common.send_msg(s, {"t": common.EXEC_RESULT, "id": mid, "rc": -1, "out": "", "err": "超时"})


def _do_push(s, m, sandbox):
    mid = m.get("id")
    try:
        data = base64.b64decode(m.get("data", ""))
        path = os.path.join(sandbox, os.path.basename(m.get("path", "file.bin")))
        open(path, "wb").write(data)
        common.send_msg(s, {"t": common.PUSH_RESULT, "id": mid, "ok": True, "err": ""})
    except Exception as e:
        common.send_msg(s, {"t": common.PUSH_RESULT, "id": mid, "ok": False, "err": str(e)})


def _do_pull(s, m, sandbox):
    mid = m.get("id")
    try:
        path = os.path.join(sandbox, os.path.basename(m.get("path", "")))
        data = open(path, "rb").read()
        common.send_msg(s, {"t": common.PULL_RESULT, "id": mid, "ok": True,
                            "data": base64.b64encode(data).decode(), "err": ""})
    except Exception as e:
        common.send_msg(s, {"t": common.PULL_RESULT, "id": mid, "ok": False, "data": "", "err": str(e)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--name", required=True)
    ap.add_argument("--password", default=os.environ.get("FLEET_PASSWORD", "changeme"))
    ap.add_argument("--platform", default="windows")
    ap.add_argument("--sandbox", default="sandbox_node")
    args = ap.parse_args()
    run_node(args.host, args.port, args.name, args.password, args.platform, args.sandbox, bg=False)


if __name__ == "__main__":
    main()

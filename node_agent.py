"""Node-Agent：被控端（L0 连通 + L1 执行落点 + 能力上报 + 上下文读写 + 会话模式）。

连 Commander -> 密码认证 -> 注册(带 capabilities) -> 收指令 + 主动读写共享上下文。
真实部署：装成 SYSTEM 服务（Win）/ systemd root（Linux）跨重启常驻；--session 为便携模式（网吧等）。

用法：
  python node_agent.py --host 127.0.0.1 --port 9000 --name PC-B --password 你的密码
  python node_agent.py ... --session            # 便携模式，不安装，下机即清
  python node_agent.py ... --cap-req '{"cap":"phone.screen.on","reason":"想玩游戏"}'   # 她的节点演示
"""
import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time

import common

PENDING = {}
_PENDING_LK = threading.Lock()
_ID = threading.Lock()
_counter = 0


def _next_id(prefix):
    global _counter
    with _ID:
        _counter += 1
        return f"{prefix}-{_counter}"


def _store_pending(mid):
    ev = threading.Event()
    with _PENDING_LK:
        PENDING[mid] = {"ev": ev, "slot": {}}
    return ev


def _resolve_pending(mid, payload):
    with _PENDING_LK:
        p = PENDING.get(mid)
        if p:
            p["slot"].update(payload)
            p["ev"].set()


def _send_wait(s, msg, timeout=20):
    mid = msg.get("id") or _next_id("c")
    msg["id"] = mid
    ev = _store_pending(mid)
    try:
        common.send_msg(s, msg)
    except OSError:
        return None
    if not ev.wait(timeout):
        return None
    with _PENDING_LK:
        return PENDING.pop(mid, {"slot": {}})["slot"]


def run_node(host, port, name, password, platform="windows",
             caps=None, sandbox="sandbox_node", bg=False, cap_req=None, session=False,
             models=None, gpu=False, ram_gb=0):
    caps = caps or ["exec", "push", "pull"]
    if isinstance(caps, str):
        caps = caps.split(",")
    if models and "compute" not in caps:
        caps = caps + ["compute"]
    if isinstance(models, str):
        models = models.split(",")

    def _loop():
        while True:
            try:
                _session(host, port, name, password, platform, caps, sandbox, cap_req,
                         session, models, gpu, ram_gb)
            except OSError as e:
                print(f"[{name}] 连接失败: {e}，3s 后重连", flush=True)
            time.sleep(3)

    if bg:
        threading.Thread(target=_loop, daemon=True).start()
    else:
        _loop()


def _session(host, port, name, password, platform, caps, sandbox, cap_req,
             session, models, gpu, ram_gb):
    os.makedirs(sandbox, exist_ok=True)
    s = socket.create_connection((host, port), timeout=10)
    common.send_msg(s, {"t": common.AUTH, "name": name, "password": password})
    resp = common.recv_msg(s)
    if not resp or resp.get("t") != common.AUTH_OK:
        raise OSError(f"认证失败: {resp}")
    common.send_msg(s, {"t": common.REGISTER, "name": name, "platform": platform,
                        "capabilities": caps,
                        "info": {"sandbox": os.path.abspath(sandbox), "session": session,
                                 "compute": {"cpu": os.cpu_count() or 0, "gpu": gpu,
                                             "ram_gb": ram_gb, "models": models or []}}})
    print(f"[{name}] 已注册（token={resp['token'][:8]}…）caps={caps}", flush=True)

    if cap_req:
        try:
            cap_req = json.loads(cap_req) if isinstance(cap_req, str) else cap_req
        except Exception:
            cap_req = {"cap": "phone.screen.on", "reason": "想玩游戏"}
        common.send_msg(s, {"t": common.CAP_REQ, "cap": cap_req.get("cap"),
                            "reason": cap_req.get("reason", "")})
        print(f"[{name}] 已发送能力请求: {cap_req}", flush=True)

    # 心跳
    def _hb():
        while True:
            time.sleep(15)
            try:
                common.send_msg(s, {"t": common.HEARTBEAT})
            except OSError:
                return

    threading.Thread(target=_hb, daemon=True).start()

    # 读线程（处理 commander 下发的指令/上下文回执/授权）
    stop = threading.Event()

    def _read():
        while not stop.is_set():
            try:
                m = common.recv_msg(s)
            except OSError:
                break
            if m is None:
                break
            _dispatch(s, m, name, sandbox)

    th = threading.Thread(target=_read, daemon=True)
    th.start()

    # 她的节点：演示跨设备读共享上下文（证明 L3 一体）
    if "persona" in caps or cap_req:
        time.sleep(0.3)
        r = _send_wait(s, {"t": common.CTX_PUT, "ns": "her", "key": "mood_now", "val": "今天想撒娇"})
        print(f"[{name}] 写共享上下文 her/mood_now -> {r}", flush=True)
        r = _send_wait(s, {"t": common.CTX_GET, "ns": "her", "key": "game_lastnight"})
        print(f"[{name}] 跨设备读到: {r}", flush=True)

    try:
        while not stop.is_set():
            time.sleep(1)
    finally:
        stop.set()
        th.join(timeout=2)


def _dispatch(s, m, name, sandbox):
    t = m.get("t")
    if t == common.EXEC:
        _do_exec(s, m)
    elif t == common.PUSH:
        _do_push(s, m, sandbox)
    elif t == common.PULL:
        _do_pull(s, m, sandbox)
    elif t == common.QUEUE:
        for c in m.get("cmds", []):
            print(f"[{name}] 执行重放命令: {c['cmd']}", flush=True)
            _do_exec(s, {"id": c["id"], "cmd": c["cmd"]})
    elif t == common.COMPUTE:
        _do_compute(s, m)
    elif t == common.CAP_GRANT:
        print(f"[{name}] ✓ 收到授权 {m.get('cap')}（限 {m.get('session_min')}min，token={m.get('token','')[:8]}）", flush=True)
    elif t == common.CAP_DENY:
        print(f"[{name}] ✗ 能力请求被拒: {m.get('cap')}（{m.get('reason')}）", flush=True)
    elif t in (common.CTX_PUT_RESULT, common.CTX_GET_RESULT, common.CTX_SEARCH_RESULT, common.CTX_DEL_RESULT):
        _resolve_pending(m.get("id"), m)
    else:
        print(f"[{name}] 未知指令 {t}", flush=True)


def _decode(b):
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


def _do_compute(s, m):
    """共享算力：Commander 路由来的计算任务，在本节点跑本地推理。"""
    mid = m.get("id")
    try:
        from compute_runner import run
        out = run(m.get("model"), m.get("prompt", ""))
        common.send_msg(s, {"t": common.COMPUTE_RESULT, "id": mid, "ok": True,
                            "out": out, "model": m.get("model")})
    except Exception as e:
        common.send_msg(s, {"t": common.COMPUTE_RESULT, "id": mid, "ok": False,
                            "out": "", "err": str(e)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--name", required=True)
    ap.add_argument("--password", default=os.environ.get("FLEET_PASSWORD", "changeme"))
    ap.add_argument("--platform", default="windows")
    ap.add_argument("--caps", default="exec,push,pull")
    ap.add_argument("--sandbox", default="sandbox_node")
    ap.add_argument("--session", action="store_true", help="便携模式（网吧等），下机即清")
    ap.add_argument("--cap-req", default=None, help='她的节点：能力请求 JSON，如 {"cap":"phone.screen.on","reason":"想玩游戏"}')
    ap.add_argument("--models", default=None, help="本节点可跑的本地模型，逗号分隔，如 polaris-0.4b,qwen3-4b")
    ap.add_argument("--gpu", action="store_true", help="本节点有 GPU（路由优先）")
    ap.add_argument("--ram", type=int, default=0, help="本节点内存 GB（路由参考）")
    args = ap.parse_args()
    run_node(args.host, args.port, args.name, args.password, args.platform,
             args.caps, args.sandbox, bg=False, cap_req=args.cap_req, session=args.session,
             models=args.models, gpu=args.gpu, ram_gb=args.ram)


if __name__ == "__main__":
    main()

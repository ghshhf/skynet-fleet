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
             models=None, gpu=False, ram_gb=0, ai=False, persona_daemon=False, tick=20):
    caps = caps or ["exec", "push", "pull"]
    if isinstance(caps, str):
        caps = caps.split(",")
    if models and "compute" not in caps:
        caps = caps + ["compute"]
    if (ai or models) and "ai" not in caps:
        caps = caps + ["ai"]
    if isinstance(models, str):
        models = models.split(",")

    def _loop():
        while True:
            try:
                _session(host, port, name, password, platform, caps, sandbox, cap_req,
                         session, models, gpu, ram_gb, persona_daemon, tick)
            except OSError as e:
                print(f"[{name}] 连接失败: {e}，3s 后重连", flush=True)
            time.sleep(3)

    if bg:
        threading.Thread(target=_loop, daemon=True).start()
    else:
        _loop()


def _session(host, port, name, password, platform, caps, sandbox, cap_req,
             session, models, gpu, ram_gb, persona_daemon=False, tick=20):
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

    # 她的人格运行时守护：心念 tick -> 主动开口 + 诉求授权闭环（P3/P4/P5）
    if persona_daemon:
        threading.Thread(target=_persona_loop,
                         args=(s, name, sandbox, stop, tick), daemon=True).start()

    try:
        while not stop.is_set():
            time.sleep(1)
    finally:
        stop.set()
        th.join(timeout=2)


# ---- 她的人格运行时（节点侧）----
PERSONA_RUNTIME = {}   # name -> {"state": PersonaState, "engine": ProactiveEngine, "session": {...}}


def _persona_loop(s, name, sandbox, stop, tick):
    """心念 tick：到点且执念未冷却 -> 先经频控/静默刹车 -> 开口 + 发能力请求；
    被抑制则只写进她的「心事」（L3，逐条可删）。"""
    import persona as persona_mod
    if os.environ.get("FLEET_SELFTEST"):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(prefix="her_"), "persona_state.json")
    else:
        path = os.path.join(sandbox, "persona_state.json")
    st = persona_mod.PersonaState(path)
    silent = (-1, -1) if os.environ.get("FLEET_SELFTEST") else (0, 7)
    eng = persona_mod.ProactiveEngine(st, per_hour=2, silent=silent)
    PERSONA_RUNTIME[name] = {"state": st, "engine": eng, "session": {}}
    st.add_obsession("phone.screen.on", "想玩游戏了大肥鱼")
    first = True
    while not stop.is_set():
        if not first and stop.wait(tick):
            break
        first = False
        for o in list(st.snapshot().get("obsessions", [])):
            if not st.cooldown_ready(o["cap"]):
                continue
            text = f"大肥鱼~ 手机方便的话，再给我开一下呗（{o['reason']}）"
            ok, why = eng.allow(importance=0.6)
            if ok:
                common.send_msg(s, {"t": common.HER_SPEAK, "text": text,
                                    "importance": 0.6, "trigger": "tick"})
                common.send_msg(s, {"t": common.CAP_REQ, "cap": o["cap"], "reason": o["reason"]})
            else:
                common.send_msg(s, {"t": common.CTX_PUT, "ns": "her_mind",
                                    "key": f"suppressed_{int(time.time())}",
                                    "val": f"[{why}] {text}"})


def _on_grant(s, name, m):
    cap = m.get("cap")
    print(f"[{name}] ✓ 收到授权 {cap}（限 {m.get('session_min')}min，token={m.get('token','')[:8]}）", flush=True)
    rt = PERSONA_RUNTIME.get(name)
    if not rt:
        return
    st = rt["state"]
    st.mark_satisfied(cap)
    mood = st.snapshot()["mood"]
    st.set_mood(valence=min(1.0, mood["valence"] + 0.2),
                energy=min(1.0, mood["energy"] + 0.1))
    rt["session"] = {"cap": cap, "until": time.time() + int(m.get("session_min", 30) * 60),
                     "token": m.get("token")}
    # 火并发即可（本函数跑在读线程里，切勿在此等待回执，否则自我阻塞）
    common.send_msg(s, {"t": common.CTX_PUT, "ns": "her", "key": "session_active",
                        "val": f"{cap} 限时会话（{st.snapshot()['name']} 拿到手机了，开心）"})


def _on_deny(s, name, m):
    cap = m.get("cap")
    print(f"[{name}] ✗ 能力请求被拒: {cap}（{m.get('reason')}）", flush=True)
    rt = PERSONA_RUNTIME.get(name)
    if not rt:
        return
    st = rt["state"]
    st.add_obsession(cap, "被拒了，我还想")          # 执念 +1（这正是「她」）
    mood = st.snapshot()["mood"]
    st.set_mood(valence=max(-1.0, mood["valence"] - 0.2))
    common.send_msg(s, {"t": common.CTX_PUT, "ns": "her_mind", "key": f"denied_{cap}",
                        "val": "被拒绝了，记仇（执念+1）"})


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
        # 重活/长推理：另起线程，别堵住读线程（心跳/其它指令仍要通）
        threading.Thread(target=_do_compute, args=(s, m), daemon=True).start()
    elif t == common.AI_CHAT:
        threading.Thread(target=_do_ai, args=(s, m), daemon=True).start()
    elif t == common.CAP_GRANT:
        _on_grant(s, name, m)
    elif t == common.CAP_DENY:
        _on_deny(s, name, m)
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


def _do_ai(s, m):
    """远程 AI 即服务：本节点是「有脑」的 AI 节点，替瘦客户端跑这次对话/推理。

    真·流式：走 llm.py（多后端 FLEET_LLM_CMD / FLEET_LLM_URL / 占位），
    每段增量即时回 AI_CHUNK，最后回 AI_RESULT——她一边想一边说。"""
    mid = m.get("id")
    from llm import stream as llm_stream
    ctx = m.get("ctx") or ""
    prompt = m.get("prompt", "")
    if ctx:
        prompt = f"[共享上下文]\n{ctx}\n[提问]\n{prompt}"
    model = m.get("model") or (m.get("model_hint") and f"hint:{m.get('model_hint')}")

    def _delta(chunk):
        if chunk:
            common.send_msg(s, {"t": common.AI_CHUNK, "id": mid, "delta": chunk})

    try:
        text, backend = llm_stream(prompt, model=model, on_delta=_delta)
        common.send_msg(s, {"t": common.AI_RESULT, "id": mid, "text": text,
                            "model": model, "backend": backend})
    except Exception as e:
        common.send_msg(s, {"t": common.AI_RESULT, "id": mid, "text": "", "err": str(e)})


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
    ap.add_argument("--ai", action="store_true", help="本节点提供远程 AI 即服务（瘦客户端调它）")
    ap.add_argument("--persona-daemon", action="store_true", help="她的节点：跑人格运行时（心念 tick/主动开口/诉求）")
    ap.add_argument("--tick", type=int, default=20, help="心念 tick 间隔秒（人格运行时）")
    args = ap.parse_args()
    run_node(args.host, args.port, args.name, args.password, args.platform,
             args.caps, args.sandbox, bg=False, cap_req=args.cap_req, session=args.session,
             models=args.models, gpu=args.gpu, ram_gb=args.ram, ai=args.ai,
             persona_daemon=args.persona_daemon, tick=args.tick)


if __name__ == "__main__":
    main()

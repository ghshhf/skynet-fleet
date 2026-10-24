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

import capabilities
import common
import image_gen_runner
import video_gen_runner
from fleet.skynet_relay import get_relay

_DEBUG = bool(os.environ.get("FLEET_DEBUG"))

# 停机开关：让后台节点停止重连（干净下机）。见 stop_all()。
_STOP = threading.Event()
# 按名字停机的集合：只停指定节点，不影响其它节点（见 stop_node）。
_STOPPED_NAMES = set()
_STOP_NAMES_LK = threading.Lock()

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
             models=None, gpu=False, ram_gb=0, relay_port=None, relay_lan=False,
             relay_token=""):
    caps = caps or ["exec", "push", "pull"]
    if isinstance(caps, str):
        caps = caps.split(",")
    # 本机若指定 --relay-port，先启动 SkyNet relay，并把 skynet.relay 能力上报给 commander
    relay = None
    if relay_port:
        try:
            bind_host = "0.0.0.0" if relay_lan else "127.0.0.1"
            # 🔴 --lan 没给 token 时**自动生成**一枚，而不是裸奔绑 0.0.0.0。
            # relay 侧已 fail-closed（非回环 + 无 token 直接拒起），这里补上
            # 「不用主人记参数也能安全开」的那一半：生成 → 落盘（save_relay_state
            # 会写进 ~/.fleet/relay.json）→ 大声打出来，供他机 `--relay-token` 使用。
            if relay_lan and not relay_token:
                from fleet.skynet_relay import new_token
                relay_token = new_token()
            relay = get_relay(host=bind_host, token=relay_token)
            relay.port = int(relay_port)
            actual = relay.start(host=bind_host, token=relay_token)
            if "skynet.relay" not in caps:
                caps.append("skynet.relay")
            if relay_lan:
                print(f"[{name}] SkyNet relay 已启动 {bind_host}:{actual}（--lan，已强制 token 鉴权；skynet.relay 能力已上报）", flush=True)
                print(f"[{name}] relay token（他机用 --relay-token 填这串）= {relay_token}", flush=True)
                print(f"[{name}] token 已存 ~/.fleet/relay.json；换 token 就删掉它重开", flush=True)
            else:
                print(f"[{name}] SkyNet relay 已启动 127.0.0.1:{actual}（skynet.relay 能力已上报）", flush=True)
        except Exception as e:
            print(f"[{name}] SkyNet relay 启动失败（不影响上线）：{type(e).__name__}: {e}", flush=True)
            relay = None
    if models and "compute" not in caps:
        caps = caps + ["compute"]
    if isinstance(models, str):
        models = models.split(",")

    def _loop():
        while not _STOP.is_set():
            with _STOP_NAMES_LK:
                if name in _STOPPED_NAMES:
                    break
            try:
                _session(host, port, name, password, platform, caps, sandbox, cap_req,
                         session, models, gpu, ram_gb, relay)
            except OSError as e:
                if _STOP.is_set():
                    break
                print(f"[{name}] 连接失败: {e}，3s 后重连", flush=True)
            # 停机时别再等满 3 秒
            if _STOP.wait(3):
                break
            with _STOP_NAMES_LK:
                if name in _STOPPED_NAMES:
                    break

    if bg:
        threading.Thread(target=_loop, daemon=True).start()
    else:
        _loop()


def stop_all():
    """让所有后台节点停止重连（干净下机）。

    为什么需要（2026-10-21 真事故的连带发现）：临时节点「下机即清」这条语义
    **没法验证**——node_agent 的重连循环是无限的，测试想模拟「拔线」，
    客户端 3 秒后就重连并同名接管，于是测的其实是「重连」而不是「下机」。
    真实部署里也缺一个「让这台节点干净下线」的手段。
    """
    _STOP.set()


def stop_node(name):
    """只让**指定**的节点停止重连。

    为什么不能只用 stop_all()：自检里 PC-A / Box / Her-Phone 等节点在后面阶段还要用，
    全局停机会把它们一起停掉 ⇒ 后面阶段全崩（实测「统一意图编排」阶段就报 probe 失败）。
    """
    _STOPPED_NAMES.add(name)


def reset_stop():
    """解除停机（给自检在多个阶段之间复位用）。"""
    _STOP.clear()
    _STOPPED_NAMES.clear()


def _session(host, port, name, password, platform, caps, sandbox, cap_req,
             session, models, gpu, ram_gb, relay=None):
    os.makedirs(sandbox, exist_ok=True)
    # 🔴 建连超时与读超时是两回事，**必须分开**（2026-10-21 真踩）：
    #   create_connection(timeout=N) 留下的 timeout 会**一直留在 socket 上**，
    #   于是每 N 秒没收到任何消息就抛 socket.timeout。而心跳是 **15 秒**一次
    #   —— 比超时长 ⇒ 每个节点**必然**每 ~13 秒断开重连一次（与网络无关）。
    #   后果链：周期性重连 → 同名覆盖服务端注册表 → 旧连接的 finally 找不到自己
    #   → session 清理被整段跳过 → 临时节点「下机即清」失效（钥匙留在门上）。
    # 双保险（两道都做，缺一道就复发）：
    #   ① 建连超时取 30 秒（> 心跳 15s），即使后面那行 settimeout 被删也不会周期性重连；
    #   ② 建连后立刻回到阻塞模式，读超时交给心跳/看门狗判断，不靠 socket 报。
    s = socket.create_connection((host, port), timeout=30)
    s.settimeout(None)
    common.send_msg(s, {"t": common.AUTH, "name": name, "password": password})
    resp = common.recv_msg(s)
    if not resp or resp.get("t") != common.AUTH_OK:
        raise OSError(f"认证失败: {resp}")
    common.send_msg(s, {"t": common.REGISTER, "name": name, "platform": platform,
                        "capabilities": caps,
                        # 不只报能力名，还报「说明书」（参数 schema）→ 编排才知道怎么调
                        "capability_specs": capabilities.describe(caps),
                        "info": {"sandbox": os.path.abspath(sandbox), "session": session,
                                 "relay_port": (relay.port if relay and relay.running else None),
                                 "relay_token": (relay.token if relay and relay.running else None),
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
            if _DEBUG:
                print(f"[nd:{name}] 收到 t={m.get('t')} id={m.get('id')}", flush=True)
            try:
                _dispatch(s, m, name, sandbox)
            except Exception as e:
                # 单条消息处理失败不许杀死整条连接：否则会话变僵尸——
                # 对端还以为在线（sendall 成功），实际再没人 recv，表现为「永远等到超时」。
                print(f"[{name}] 处理 {m.get('t')} 出错: {e}", flush=True)
        stop.set()      # 读线程结束 = 会话结束，让主循环退出并触发重连（自愈）

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
    elif t == common.IMAGE_GEN:
        _do_image_gen(s, m)
    elif t == common.VIDEO_GEN:
        _do_video_gen(s, m)
    elif t == common.MEMORY_SYNC:
        _do_memory_sync(s, m)
    elif t == common.UI_SKIP:
        _do_ui_skip(s, m)
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
    reason = common.cmd_reject_reason(cmd)
    if reason:
        # 理由分层（不在白名单 / 含 shell 元字符 / 空命令）——白名单是安全边界，
        # 拒绝原因要写清楚，否则对端只看到「rc=-1」无从排查
        common.send_msg(s, {"t": common.EXEC_RESULT, "id": mid, "rc": -1,
                            "out": "", "err": reason})
        return
    t0 = time.time()
    if _DEBUG:
        print(f"[nd] exec 收到 id={mid} cmd={cmd!r}", flush=True)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, timeout=30)
        if _DEBUG:
            print(f"[nd] exec 完成 id={mid} rc={r.returncode} 用时{time.time()-t0:.1f}s", flush=True)
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


def _do_image_gen(s, m, backend=None):
    """安卓出图：在本节点挂载的手机上跑 SD，把 PNG 路径回传 PC。

    后端可注入（测试用）；默认走 image_gen_runner 的 android_bridge 后端。
    任何失败都回一张带原因的 err 回执，不许静默交空（与 exec/compute 同纪律）。
    """
    mid = m.get("id")
    try:
        res = image_gen_runner.run(
            m.get("prompt", ""), neg=m.get("neg", ""), size=m.get("size", "512x512"),
            steps=int(m.get("steps", 20)), seed=int(m.get("seed", -1)),
            cfg=float(m.get("cfg", 7.0)), model=m.get("model"), backend=backend)
        common.send_msg(s, {"t": common.IMAGE_GEN_RESULT, "id": mid,
                            "ok": res["ok"], "path": res.get("path", ""),
                            "err": res.get("err", "")})
    except Exception as e:
        common.send_msg(s, {"t": common.IMAGE_GEN_RESULT, "id": mid, "ok": False,
                            "path": "", "err": str(e)})


def _do_video_gen(s, m, backend=None):
    """视频生成（P3 壳 + 远端路由）：在本节点/挂载的视频后端上出一段视频。

    与 _do_image_gen 同构：参数规整 + 回执路径 + 任何失败都带原因回执，
    不许静默交空。默认后端（无 FLEET_VIDEO_BACKEND）如实说「未配置」。
    """
    mid = m.get("id")
    try:
        res = video_gen_runner.run(
            m.get("prompt", ""), duration=m.get("duration", 3), fps=m.get("fps", 24),
            size=m.get("size", "512x512"), seed=int(m.get("seed", -1)),
            model=m.get("model"), backend=backend)
        common.send_msg(s, {"t": common.VIDEO_GEN_RESULT, "id": mid,
                            "ok": res["ok"], "path": res.get("path", ""),
                            "err": res.get("err", "")})
    except Exception as e:
        common.send_msg(s, {"t": common.VIDEO_GEN_RESULT, "id": mid, "ok": False,
                            "path": "", "err": str(e)})


_DEFAULT_UI_RULES = [{
    "packageName": "*",
    "triggers": [
        {"name": "跳过", "action": "click", "text": ["跳过广告", "跳过"],
         "textRegex": r"跳过|skip", "desc": ["关闭"]},
    ],
}]



def _do_memory_sync(s, m):
    """处理 commander 派发的 memory_sync：export/import 本节点记忆。"""
    mid = m.get("id")
    try:
        from fleet.memory_store import get_store
        store = get_store()
        op = (m.get("op") or "export").lower()
        if op == "import":
            items = m.get("items") or []
            n = store.import_memories(items)
            common.send_msg(s, {"t": common.MEMORY_SYNC_RESULT, "id": mid,
                                "ok": True, "count": n})
        else:
            items = store.export_all()
            common.send_msg(s, {"t": common.MEMORY_SYNC_RESULT, "id": mid,
                                "ok": True, "items": items, "count": len(items)})
    except Exception as e:
        common.send_msg(s, {"t": common.MEMORY_SYNC_RESULT, "id": mid,
                            "ok": False, "err": str(e)})


def _do_ui_skip(s, m, skip_fn=None):
    """UI 自动化：在本节点挂载的 Android 设备上按规则跳过广告。

    skip_fn 可注入（测试用），默认调 ui_automation.skip_ad。
    无 adb / 无设备时返回 ok=False + err，不崩。
    """
    mid = m.get("id")
    # 规则来源：控制面下发的规则库版本 > 本节点内置默认（离线兜底，保证无控制面也能跑）
    ver = m.get("rules_version")
    head = f"规则来源=控制面 v{ver}" if ver else "规则来源=节点内置默认"
    log = []
    try:
        if skip_fn is None:
            ua_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_automation")
            if ua_dir not in sys.path:
                sys.path.insert(0, ua_dir)
            import ui_automation as _ua
            skip_fn = _ua.skip_ad
        rules = m.get("rules") or _DEFAULT_UI_RULES
        head += f"（{len(rules)} 条）"
        ok, log = skip_fn(m.get("serial"), m.get("package", ""), rules,
                          rounds=int(m.get("rounds", 6)),
                          use_vision=bool(m.get("use_vision", False)))
        common.send_msg(s, {"t": common.UI_SKIP_RESULT, "id": mid, "ok": bool(ok),
                            "log": [head] + list(log)[-19:], "err": "",
                            "rules_version": ver or 0})
    except Exception as e:
        common.send_msg(s, {"t": common.UI_SKIP_RESULT, "id": mid, "ok": False,
                            "log": [head], "err": str(e), "rules_version": ver or 0})


def _selftest():
    """离线验证 UI_SKIP 能力接线（无需真机/adb）。"""
    s = _FakeSock()
    _do_ui_skip(s, {"id": "u1", "package": "com.x", "rounds": 1},
                skip_fn=lambda serial, pkg, rules, rounds=6, use_vision=False:
                (True, ["hit 跳过"]))
    msgs = _read_msgs(s)
    assert msgs and msgs[0]["t"] == common.UI_SKIP_RESULT and msgs[0]["ok"] is True, msgs
    assert msgs[0]["id"] == "u1", msgs
    assert msgs[0]["log"][0].startswith("规则来源="), msgs   # 首行固定为规则来源
    assert msgs[0]["log"][-1] == "hit 跳过", msgs

    # 控制面下发规则版本时，回执要能证明「用的是哪一版规则」
    s3 = _FakeSock()
    _do_ui_skip(s3, {"id": "u3", "package": "com.x", "rules_version": 7,
                     "rules": [{"packageName": "*", "triggers": [{"name": "跳过", "text": ["跳过"]}]}]},
                skip_fn=lambda serial, pkg, rules, rounds=6, use_vision=False:
                (True, ["hit 跳过"]))
    m3 = _read_msgs(s3)[0]
    assert m3["rules_version"] == 7 and "v7" in m3["log"][0], m3

    s2 = _FakeSock()

    def _boom(*a, **k):
        raise RuntimeError("adb 不存在")

    _do_ui_skip(s2, {"id": "u2", "package": "com.x"}, skip_fn=_boom)
    msgs2 = _read_msgs(s2)
    assert msgs2 and msgs2[0]["ok"] is False and "adb" in msgs2[0]["err"], msgs2

    ua_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_automation")
    if ua_dir not in sys.path:
        sys.path.insert(0, ua_dir)
    import ui_automation as ua
    nodes = [{"text": "跳过 3s", "desc": "", "rid": "", "package": "com.x",
              "cls": "", "clickable": True, "bounds": (10, 10, 20, 20)}]
    n, c = ua.match(nodes, _DEFAULT_UI_RULES[0]["triggers"][0])
    assert n and c == (15, 15), (n, c)

    # exec 白名单收紧：三层各自要能拒（不真执行，只验判定）
    R = common.cmd_reject_reason
    assert R("echo 你好") is None, "白名单内命令应放行"
    assert R("") is not None, "空命令应拒"
    assert R("   ") is not None, "空白命令应拒"
    assert R("rm -rf /") is not None, "不在白名单应拒"
    assert R("echo ok & del x") is not None, "链式命令（&）应拒"
    assert R("echo ok | more") is not None, "管道应拒"
    assert R("type a.txt > b.txt") is not None, "重定向应拒"
    assert R("echo `whoami`") is not None, "反引号应拒"
    assert R("echo $HOME") is not None, "变量展开应拒"
    assert R("echo a\nwhoami") is not None, "换行注入应拒"
    assert R("python -c print(1)") is not None, "解释器默认应拒（等于任意代码执行）"
    os.environ["FLEET_ALLOW_INTERPRETER"] = "1"
    try:
        assert R("python --version") is None, "显式开关后解释器应放行"
        assert R("python -c a & b") is not None, "放行解释器也不能放行链式"
    finally:
        os.environ.pop("FLEET_ALLOW_INTERPRETER", None)

    # 走一遍真实回执路径：拒绝理由要写给人看，不能只给 rc=-1
    se = _FakeSock()
    _do_exec(se, {"id": "x1", "cmd": "echo ok & whoami"})
    me = _read_msgs(se)[0]
    assert me["rc"] == -1 and "元字符" in me["err"], me

    print("[selftest] UI_SKIP 正常路径回执 OK")
    print("[selftest] UI_SKIP 异常路径 ok=False+err OK")
    print("[selftest] 默认规则可被 ui_automation.match 消费 OK")
    print("[selftest] exec 白名单：链式/管道/重定向/反引号/变量/换行全拒 OK")
    print("[selftest] exec 解释器默认拒，FLEET_ALLOW_INTERPRETER=1 才放行 OK")

    # ---- image.gen 能力接线（路线甲·N+2）：参数解析 + 回执路径 + 空 prompt 拒 ----
    def _fake_ok(prompt, neg, size, steps, seed, cfg, model):
        return f"/sdcard/Pictures/fleet/{seed or 0}.png"

    s4 = _FakeSock()
    _do_image_gen(s4, {"id": "g1", "prompt": "一只猫", "size": "512x512", "steps": 4, "seed": 7},
                  backend=_fake_ok)
    m4 = _read_msgs(s4)[0]
    assert m4["t"] == common.IMAGE_GEN_RESULT and m4["ok"] is True, m4
    assert m4["id"] == "g1", m4
    assert m4["path"] == "/sdcard/Pictures/fleet/7.png", m4

    def _fake_fail(prompt, neg, size, steps, seed, cfg, model):
        raise RuntimeError("手机没电了")

    s5 = _FakeSock()
    _do_image_gen(s5, {"id": "g5", "prompt": "一只狗"}, backend=_fake_fail)
    m5 = _read_msgs(s5)[0]
    assert m5["ok"] is False and "手机没电" in m5["err"], m5

    s6 = _FakeSock()
    _do_image_gen(s6, {"id": "g6", "prompt": "  "})
    m6 = _read_msgs(s6)[0]
    assert m6["ok"] is False and "prompt" in m6["err"], m6

    # 默认后端未配置时必须明确报错（不许静默交空），且走回执通道
    se = _FakeSock()
    _do_image_gen(se, {"id": "ge", "prompt": "猫"})
    me = _read_msgs(se)[0]
    assert me["ok"] is False and ("未配置" in me["err"] or "未实现" in me["err"]), me

    print("[selftest] image.gen 正常/异常/空prompt/未配置后端 全过")

    # ---- video.gen 能力接线（P3 壳 + 远端路由）：与 image.gen 同构的回执路径 ----
    def _v_ok(prompt, duration, fps, size, seed, model):
        return f"/sdcard/Movies/fleet/{seed or 0}.mp4"

    s7 = _FakeSock()
    _do_video_gen(s7, {"id": "v1", "prompt": "海浪", "duration": 3, "fps": 24, "seed": 9},
                  backend=_v_ok)
    m7 = _read_msgs(s7)[0]
    assert m7["t"] == common.VIDEO_GEN_RESULT and m7["ok"] is True, m7
    assert m7["path"] == "/sdcard/Movies/fleet/9.mp4", m7

    def _v_fail(prompt, duration, fps, size, seed, model):
        raise RuntimeError("视频后端没电了")

    s8 = _FakeSock()
    _do_video_gen(s8, {"id": "v8", "prompt": "日落"}, backend=_v_fail)
    m8 = _read_msgs(s8)[0]
    assert m8["ok"] is False and "视频后端没电" in m8["err"], m8

    s9 = _FakeSock()
    _do_video_gen(s9, {"id": "v9", "prompt": "  "})
    m9 = _read_msgs(s9)[0]
    assert m9["ok"] is False and "prompt" in m9["err"], m9

    # 默认后端：必须在「有后端」与「没后端」两种环境都说得清。
    # 2026-10-23 更正：这条断言原写成「必然报未配置」，只在没装 ffmpeg 的机器上成立；
    # 本机装了 ffmpeg 就走 pc_video_backend 的占位路线（真出片），于是门禁在
    # 有 ffmpeg 的机器上恒红——那是**环境差异**，不是代码坏了。按环境分支断言。
    import shutil as _sh
    _video_backend = bool(os.environ.get("FLEET_VIDEO_CLI")
                          or os.environ.get("FLEET_FFMPEG")
                          or _sh.which("ffmpeg"))
    se2 = _FakeSock()
    _do_video_gen(se2, {"id": "ve", "prompt": "云"})
    me2 = _read_msgs(se2)[0]
    if _video_backend:
        assert me2["ok"] is True and me2["path"], me2
    else:
        assert me2["ok"] is False and "未配置" in me2["err"], me2

    print("[selftest] video.gen 正常/异常/空prompt/未配置后端 全过")

    # ---- 跨设备记忆同步（P5c）：export/import 回执路径 ----
    import os as _os, tempfile as _tf
    _dbp = _os.path.join(_tf.gettempdir(), f"fleet_ms_{_os.getpid()}.db")
    for _f in (_dbp, _dbp+"-journal", _dbp+"-wal", _dbp+"-shm"):
        if _os.path.exists(_f): _os.remove(_f)
    _os.environ["FLEET_MEMORY"] = "1"
    from fleet.memory_store import MemoryStore
    # _do_memory_sync 走全局 get_store()，count 不固定；只验回执形状 + 去重语义
    sx = _FakeSock()
    _do_memory_sync(sx, {"id": "ms1", "op": "export"})
    mx = _read_msgs(sx)[0]
    assert mx["t"] == common.MEMORY_SYNC_RESULT and mx["ok"] is True, mx
    assert isinstance(mx["items"], list) and mx["count"] == len(mx["items"]), mx
    c0 = mx["count"]
    # import 同内容应全部跳过（去重）
    sx2 = _FakeSock()
    _do_memory_sync(sx2, {"id": "ms2", "op": "import", "items": mx["items"]})
    mx2 = _read_msgs(sx2)[0]
    assert mx2["ok"] is True and mx2["count"] == 0, mx2
    # import 全新内容应新增 1
    sx3 = _FakeSock()
    _do_memory_sync(sx3, {"id": "ms3", "op": "import", "items": [
        {"role": "assistant", "content": "selftest-unique-" + str(c0),
         "tags": "selftest", "ts": 1.0}]})
    mx3 = _read_msgs(sx3)[0]
    assert mx3["ok"] is True and mx3["count"] == 1, mx3
    print("[selftest] memory.sync export/import 去重回执 OK")

    print("[selftest] PASS")


class _FakeSock:
    def __init__(self):
        self.buf = b""

    def sendall(self, b):
        self.buf += b


def _read_msgs(sock):
    """解析 _FakeSock 累积的长度前缀帧。"""
    import struct
    out = []
    buf = sock.buf
    while len(buf) >= 4:
        n = struct.unpack(">I", buf[:4])[0]
        if len(buf) < 4 + n:
            break
        out.append(json.loads(buf[4:4 + n].decode("utf-8")))
        buf = buf[4 + n:]
    return out


def _scan_local_models():
    """扫描本地 models 目录，返回模型名列表（供 info.models 上报）。"""
    import os as _os
    d = _os.environ.get("FLEET_MODELS_DIR") or _os.path.join(
        _os.path.expanduser("~/.fleet"), "models")
    exts = (".gguf", ".safetensors", ".bin", ".ckpt", ".pt", ".pth")
    out = []
    if _os.path.isdir(d):
        for root, _, files in _os.walk(d):
            for f in files:
                if f.lower().endswith(exts):
                    out.append(f)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="离线自测 UI_SKIP 接线（不连网）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--name", default="", help="节点名（非 selftest 必填）")
    ap.add_argument("--password", default=os.environ.get("FLEET_PASSWORD", "changeme"))
    ap.add_argument("--platform", default="windows")
    ap.add_argument("--caps", default="exec,push,pull")
    ap.add_argument("--sandbox", default="sandbox_node")
    ap.add_argument("--session", action="store_true", help="便携模式（网吧等），下机即清")
    ap.add_argument("--cap-req", default=None, help='她的节点：能力请求 JSON，如 {"cap":"phone.screen.on","reason":"想玩游戏"}')
    ap.add_argument("--models", default=None, help="本节点可跑的本地模型，逗号分隔，如 polaris-0.4b,qwen3-4b")
    ap.add_argument("--gpu", action="store_true", help="本节点有 GPU（路由优先）")
    ap.add_argument("--ram", type=int, default=0, help="本节点内存 GB（路由参考）")
    ap.add_argument("--relay-port", type=int, default=None,
                    help="启动 SkyNet relay 并监听此端口（供他机经端口转发取页）")
    ap.add_argument("--lan", action="store_true",
                    help="relay 绑 0.0.0.0（局域网其他节点可借本机出口）；必须配 --relay-token")
    ap.add_argument("--relay-token", default=os.environ.get("FLEET_RELAY_TOKEN", ""),
                    help="--lan 模式下的共享 token，拦住局域网误连/扫描")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return
    if args.models is None:
        scanned = _scan_local_models()
        if scanned:
            args.models = ",".join(scanned)
    if not args.name:
        raise SystemExit("--name 必填（或使用 --selftest）")
    run_node(args.host, args.port, args.name, args.password, args.platform,
             args.caps, args.sandbox, bg=False, cap_req=args.cap_req, session=args.session,
             models=args.models, gpu=args.gpu, ram_gb=args.ram, relay_port=args.relay_port,
             relay_lan=args.lan, relay_token=args.relay_token)


if __name__ == "__main__":
    main()

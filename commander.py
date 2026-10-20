"""Commander：统一调用控制面（L0 连通 + L1 执行枢纽 + L2 路由 + L3 上下文 + 她路由）。

- 节点密码认证后注册，获得会话令牌；注册带 capabilities[]；
- 维持节点表 + 心跳；离线命令进队列，上线自动重放（Phase 4 体验）；
- 持有 SharedContext（L3 跨设备记忆，海马体）；节点经协议读写；
- 配对设备一次性令牌（卸载闸门 ②）；她的能力请求路由到用户批准（P4）。
- 运维侧 REPL：nodes / exec / broadcast / push / pull / ctx_* / token / cap / queue。

用法：
  python commander.py                      # 服务 + REPL（本机控他机）
  python commander.py --self-test          # 本地回路自检（模拟多节点，不碰真机）
  python commander.py --password 你的密码 --host 0.0.0.0 --port 9000
"""
import argparse
import base64
import hashlib
import os
import socket
import sys
import threading
import time

import audit
import capabilities
import common
import llm
import memory as mem_mod

REGISTRY = common.LockedDict()          # name -> {sock, token, platform, info, capabilities, last_hb}
PENDING = {}                             # msg id -> {ev, slot}
_PENDING_LK = threading.Lock()
_ID = threading.Lock()
_counter = 0

OFFLINE_QUEUE = {}                       # name -> [{id, cmd}]  （离线排队，上线重放）
_QUEUE_LK = threading.Lock()

TOKENS = {}                              # token -> {node, purpose, exp}
_TOKEN_LK = threading.Lock()

THIN_WAITERS = {}                        # ai req id -> {conn, deadline}  （瘦客户端流式回传）
_THIN_LK = threading.Lock()

CTX = None                               # SharedContext（懒初始化）
SELF_TEST = False


def _ctx():
    global CTX
    if CTX is None:
        CTX = mem_mod.SharedContext(os.environ.get("FLEET_CTX", "fleet_context.db"))
    return CTX


def _next_id(prefix):
    global _counter
    with _ID:
        _counter += 1
        return f"{prefix}-{_counter}"


def _free_port(preferred):
    """从 preferred 起找一个本机空闲端口（自测用，避免与既有服务抢端口）。"""
    for p in range(preferred, preferred + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as t:
            try:
                t.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0


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


def send_to_node(name, msg, timeout=30):
    node = REGISTRY.with_lock(lambda d: d.get(name))
    if not node:
        return {"ok": False, "err": f"节点 {name} 不在线", "offline": True}
    mid = msg.get("id") or _next_id("m")
    msg["id"] = mid
    ev = _store_pending(mid)
    try:
        common.send_msg(node["sock"], msg)
    except OSError as e:
        with _PENDING_LK:
            PENDING.pop(mid, None)
        return {"ok": False, "err": f"发送失败: {e}"}
    if not ev.wait(timeout):
        with _PENDING_LK:
            PENDING.pop(mid, None)
        return {"ok": False, "err": "等待节点回执超时"}
    with _PENDING_LK:
        slot = PENDING.pop(mid, {"slot": {}})["slot"]
    return {"ok": True, **slot}


def _drop_node(name):
    REGISTRY.with_lock(lambda d: d.pop(name, None))


def _enqueue(name, cmd):
    """离线排队（Phase 4）。"""
    with _QUEUE_LK:
        OFFLINE_QUEUE.setdefault(name, []).append({"id": _next_id("q"), "cmd": cmd})


def _replay_queue(name):
    """节点上线时，把排队命令重放过去（透明上下线）。"""
    with _QUEUE_LK:
        q = OFFLINE_QUEUE.pop(name, [])
    if q:
        send_to_node(name, {"t": common.QUEUE, "cmds": q})
        print(f"[*] 节点 {name} 上线，重放 {len(q)} 条离线命令", flush=True)


def _issue_token(name, purpose="uninstall"):
    tok = os.urandom(8).hex()
    with _TOKEN_LK:
        TOKENS[tok] = {"node": name, "purpose": purpose, "exp": time.time() + 600}
    return tok


def _handle_node(conn, password):
    try:
        auth = common.recv_msg(conn)
        if not auth or auth.get("t") != common.AUTH:
            return
        if auth.get("password") != password:
            common.send_msg(conn, {"t": common.AUTH_FAIL, "reason": "密码错误"})
            return
        token = f"tok-{os.urandom(6).hex()}"
        common.send_msg(conn, {"t": common.AUTH_OK, "token": token})
        reg = common.recv_msg(conn)
        # 瘦客户端：不是节点，是来调 AI 服务的（RFC remote-ai-service）
        if reg and reg.get("t") == common.AI_CHAT:
            _serve_thin_client(conn, reg)
            return
        if not reg or reg.get("t") != common.REGISTER:
            return
        name = reg.get("name")
        REGISTRY.with_lock(lambda d: d.__setitem__(name, {
            "sock": conn, "token": token,
            "platform": reg.get("platform", "?"),
            "info": reg.get("info", {}),
            "capabilities": reg.get("capabilities", []),
            "capability_specs": reg.get("capability_specs", {}),
            "last_hb": time.time(),
        }))
        print(f"[+] 节点上线: {name} ({reg.get('platform')}) caps={reg.get('capabilities')}", flush=True)
        audit.log("node.up", node=name, platform=reg.get("platform"),
                  caps=reg.get("capabilities"))
        _replay_queue(name)  # 上线即补离线任务
        while True:
            m = common.recv_msg(conn)
            if m is None:
                break
            t = m.get("t")
            if t == common.HEARTBEAT:
                REGISTRY.with_lock(lambda d: d.get(name) and d[name].__setitem__("last_hb", time.time()))
            elif t in (common.EXEC_RESULT, common.PUSH_RESULT, common.PULL_RESULT,
                       common.COMPUTE_RESULT):
                _resolve_pending(m.get("id"), m)
            elif t == common.AI_CHUNK:
                _forward_ai(m)   # 她一边想一边说：增量即时转发给消瘦客户端
            elif t == common.AI_RESULT:
                _resolve_pending(m.get("id"), m)
                _forward_ai(m)
            elif t == common.HER_SPEAK:
                print(f"\n[她·主动] {m.get('text')}"
                      f"（重要度 {m.get('importance')}，触发 {m.get('trigger')}）", flush=True)
                audit.log("her.speak", node=name, text=m.get("text"),
                          importance=m.get("importance"))
            elif t == common.CAPABILITIES:
                def _upd(d):
                    if d.get(name):
                        d[name]["capabilities"] = m.get("caps", [])
                        if m.get("specs"):
                            d[name]["capability_specs"] = m["specs"]
                REGISTRY.with_lock(_upd)
            elif t == common.CTX_PUT:
                r = _ctx().put(m.get("ns"), m.get("key"), m.get("val"))
                common.send_msg(conn, {"t": common.CTX_PUT_RESULT, "id": m.get("id"), **r})
            elif t == common.CTX_GET:
                r = _ctx().get(m.get("ns"), m.get("key"))
                common.send_msg(conn, {"t": common.CTX_GET_RESULT, "id": m.get("id"), **r})
            elif t == common.CTX_SEARCH:
                r = _ctx().search(m.get("ns"), m.get("query", ""), m.get("k", 10))
                common.send_msg(conn, {"t": common.CTX_SEARCH_RESULT, "id": m.get("id"), "rows": r})
            elif t == common.CTX_DEL:
                r = _ctx().delete(m.get("ns"), m.get("key"))
                common.send_msg(conn, {"t": common.CTX_DEL_RESULT, "id": m.get("id"), **r})
            elif t == common.TOKEN_REQUEST:
                tok = _issue_token(name, m.get("purpose", "uninstall"))
                common.send_msg(conn, {"t": common.TOKEN_RESULT, "id": m.get("id"), "token": tok})
                print(f"[*] 节点 {name} 请求一次性令牌（{m.get('purpose')}），已下发: {tok}", flush=True)
                audit.log("token.issue", node=name, purpose=m.get("purpose"))
            elif t == common.CAP_REQ:
                _route_capability_request(name, m, conn)
            else:
                print(f"[?] 节点 {name} 未知消息: {t}", flush=True)
    except OSError:
        pass
    finally:
        nm = REGISTRY.with_lock(lambda d: next((k for k, v in d.items() if v["sock"] is conn), None))
        if nm:
            _drop_node(nm)
            print(f"[-] 节点下线: {nm}", flush=True)
            audit.log("node.down", node=nm)


def _route_capability_request(name, m, conn):
    """P4：她想用某设备=一次能力请求。默认须用户批准；自检自动批准。"""
    cap, reason = m.get("cap"), m.get("reason", "")
    if SELF_TEST:
        common.send_msg(conn, {"t": common.CAP_GRANT, "cap": cap,
                                "session_min": 30, "token": os.urandom(6).hex()})
        print(f"[她] {name} 请求 {cap}（{reason}）→ 自检自动批准", flush=True)
        audit.log("her.grant", node=name, cap=cap, reason=reason, auto=True)
        return
    print(f"\n[她] {name} 想用 {cap}：{reason}", flush=True)
    print(f"      批准? y / 其它拒绝（拒绝后她的执念+1）", flush=True)
    try:
        ans = input("     approve> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "n"
    if ans == "y":
        common.send_msg(conn, {"t": common.CAP_GRANT, "cap": cap,
                                "session_min": 30, "token": os.urandom(6).hex()})
        print(f"      ✓ 已授权 {cap}（限 30min）", flush=True)
        audit.log("her.grant", node=name, cap=cap, reason=reason)
    else:
        common.send_msg(conn, {"t": common.CAP_DENY, "cap": cap, "reason": "用户拒绝"})
        print(f"      ✗ 已拒绝；她的执念+1", flush=True)
        audit.log("her.deny", node=name, cap=cap, reason=reason)


def serve(host, port, password):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(32)
    print(f"[*] Commander 监听 {host}:{port}（密码认证已启用）", flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=_handle_node, args=(conn, password), daemon=True).start()


# ---------- 运维侧命令 ----------
def _cmd_nodes():
    rows = REGISTRY.with_lock(lambda d: [(k, v["platform"], v["capabilities"],
                                          round(time.time() - v["last_hb"], 1))
                                         for k, v in d.items()])
    if not rows:
        print("（无在线节点）")
    for k, p, caps, age in rows:
        print(f"  {k:12} {p:8} caps={caps} 心跳 {age}s 前")


def _cmd_caps():
    """能力自描述总览：编队里出现过的能力 + 各自一句话 + 谁能干（编排的发现面）。"""
    nodes = REGISTRY.with_lock(lambda d: list(d.items()))
    agg = {}
    for nm, v in nodes:
        for c in v.get("capabilities", []):
            agg.setdefault(c, []).append(nm)
    if not agg:
        print("（无在线节点）")
        return
    for c in sorted(agg):
        s = capabilities.spec_of(c)
        ps = ", ".join(p["name"] for p in s["params"]) or "—"
        print(f"  {c:10} {s['title']:12} 参数[{ps}]  能干: {', '.join(agg[c])}")


def _cmd_can(cap):
    """发现：谁会干这件事 + 怎么调（参数 schema）——编排据此「按谁能干」选点。"""
    s = capabilities.spec_of(cap)
    print(f"  {cap}: {s['title']} —— {s['desc']}")
    for p in s["params"]:
        req = "必填" if p.get("required") else "可选"
        print(f"    - {p['name']}:{p['type']}（{req}）{p['desc']}")
    ns = REGISTRY.with_lock(lambda d: [k for k, v in d.items() if cap in v.get("capabilities", [])])
    print(f"  能干此事的节点: {', '.join(ns) if ns else '（无）'}")


def _cmd_exec(name, cmd):
    r = send_to_node(name, {"t": common.EXEC, "cmd": cmd})
    if not r.get("ok"):
        if r.get("offline"):
            _enqueue(name, cmd)
            print(f"  节点离线，已排队（上线自动重放）：{cmd}")
            audit.log("exec.queued", node=name, cmd=cmd)
            return
        print(f"  失败: {r.get('err')}")
        audit.log("exec", node=name, cmd=cmd, ok=False, err=r.get("err"))
        return
    print(f"  退出码={r.get('rc')}")
    if r.get("out"):
        print(r["out"].rstrip())
    if r.get("err"):
        print("[stderr]", r["err"].rstrip())
    audit.log("exec", node=name, cmd=cmd, rc=r.get("rc"))


def _cmd_push(name, localpath):
    try:
        data = open(localpath, "rb").read()
    except OSError as e:
        print(f"  读本地文件失败: {e}")
        return
    r = send_to_node(name, {"t": common.PUSH, "path": os.path.basename(localpath),
                            "data": base64.b64encode(data).decode()})
    print(f"  {'OK' if r.get('ok') else '失败: ' + str(r.get('err'))}")


def _cmd_pull(name, remotepath, outpath):
    r = send_to_node(name, {"t": common.PULL, "path": remotepath})
    if not r.get("ok"):
        print(f"  失败: {r.get('err')}")
        return
    open(outpath, "wb").write(base64.b64decode(r["data"]))
    print(f"  已拉取 -> {outpath} ({len(r['data'])}B base64)")


def _cmd_broadcast(cmd):
    names = REGISTRY.with_lock(lambda d: list(d.keys()))
    if not names:
        print("（无在线节点）")
        return
    for n in names:
        print(f"-- -> {n} --", flush=True)
        _cmd_exec(n, cmd)


def _ctx_put(ns, key, val):
    print("  ", _ctx().put(ns, key, val))


def _ctx_get(ns, key):
    print("  ", _ctx().get(ns, key))


def _ctx_search(ns, q):
    for row in _ctx().search(ns, q):
        print(f"  {row['key']}: {row['val'][:60]}")


def _ctx_del(ns, key):
    print("  ", _ctx().delete(ns, key))


def _cmd_token(name):
    tok = _issue_token(name, "uninstall")
    print(f"  一次性令牌（配对设备经手机下发）：{tok}  （10min 有效）")
    print(f"  卸载/停止时：install_windows.py --uninstall --name {name} --token {tok}")


def pick_node(cap="compute", prefer_gpu=False):
    """共享算力路由（§九 dev_pick）：选「谁能干」的节点。
    优先满足 cap；prefer_gpu 时优先有 GPU 的；否则选内存最大。"""
    nodes = REGISTRY.with_lock(lambda d: list(d.items()))

    def _can(v):
        return cap in v.get("capabilities", [])

    cands = [(k, v) for k, v in nodes if _can(v)]
    if not cands:
        return None
    if prefer_gpu:
        g = [x for x in cands if x[1].get("info", {}).get("compute", {}).get("gpu")]
        if g:
            cands = g
    cands.sort(key=lambda x: x[1].get("info", {}).get("compute", {}).get("ram_gb", 0), reverse=True)
    return cands[0][0]


def _cache_key(prompt, model):
    return hashlib.sha256(f"{model or ''}|{prompt}".encode("utf-8")).hexdigest()[:16]


def _cmd_compute(prompt, model=None, prefer_gpu=False):
    """共享算力：把计算任务路由到最合适的节点执行（她三段式 ③ 重活）。
    结果进共享上下文缓存（compute_cache），全编队复用，不重复算（更加一步共享）。
    """
    ck = _cache_key(prompt, model)
    hit = _ctx().get("compute_cache", ck)
    if hit.get("ok"):
        print(f"  ✓ 算力池缓存命中（{ck}）：{hit['val'][:80]}")
        return
    target = pick_node("compute", prefer_gpu)
    if not target:
        print("  无具备 compute 能力的在线节点（留待接入电脑/盒子）。")
        return
    r = send_to_node(target, {"t": common.COMPUTE, "prompt": prompt, "model": model})
    if not r.get("ok"):
        print(f"  路由 {target} 失败: {r.get('err')}")
        return
    print(f"  → 路由到 {target}（model={r.get('model')}）")
    print("  ", r.get("out") or r.get("err"))
    _ctx().put("compute_cache", ck, r.get("out") or r.get("err") or "")


def _compute_nodes():
    return REGISTRY.with_lock(lambda d: [(k, v) for k, v in d.items()
                                         if "compute" in v.get("capabilities", [])])


def _cmd_computepool():
    ns = _compute_nodes()
    if not ns:
        print("（算力池为空）")
        return
    for k, v in ns:
        c = v.get("info", {}).get("compute", {})
        print(f"  {k:12} gpu={c.get('gpu')} ram={c.get('ram_gb')}GB models={c.get('models')}")


def _cmd_computemap(prompts, model=None, prefer_gpu=False):
    """共享算力池：一批提示词按容量分发到多个算力节点，并发执行并聚合结果。"""
    items = [p.strip() for p in prompts.split(";") if p.strip()]
    nodes = _compute_nodes()
    if not nodes:
        print("  算力池为空，无法分发。")
        return
    # 路由偏好：优先 GPU 节点放前面
    if prefer_gpu:
        nodes.sort(key=lambda x: not x[1].get("info", {}).get("compute", {}).get("gpu"))
    assign = {n[0]: [] for n in nodes}
    for i, p in enumerate(items):
        assign[nodes[i % len(nodes)][0]].append(p)

    results = {}
    lk = threading.Lock()

    def _run(node, plist):
        outs = []
        for p in plist:
            r = send_to_node(node, {"t": common.COMPUTE, "prompt": p, "model": model})
            outs.append(f"    [{node}] {p} -> " + (r.get("out") or r.get("err") or "?"))
        with lk:
            results[node] = outs

    ths = [threading.Thread(target=_run, args=(n, pl)) for n, pl in assign.items() if pl]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    for n, pl in assign.items():
        if pl:
            print(f"-- 节点 {n} 承接 {len(pl)} 个任务 --", flush=True)
            for line in results.get(n, []):
                print(line)


def _cmd_queue(name):
    with _QUEUE_LK:
        q = OFFLINE_QUEUE.get(name, [])
    print(f"  {name} 离线队列: {len(q)} 条" + ("" if not q else " -> " + ", ".join(c['cmd'] for c in q)))


def _cmd_audit(n=20):
    rows = audit.tail(n)
    print(f"  审计流水 {audit.path()}（共 {audit.count()} 条，显示最近 {len(rows)}）")
    for r in rows:
        ts = time.strftime("%H:%M:%S", time.localtime(r.get("ts", 0)))
        extra = " ".join(f"{k}={v}" for k, v in r.items() if k not in ("ts", "kind"))
        print(f"    {ts}  {r.get('kind'):12} {extra}")


# ---------- 远程 AI 即服务（RFC remote-ai-service 合入主线）----------
def pick_ai_node(model_hint=None):
    """按档位选 AI 节点：gpu 优先有 GPU；4b 优先带 4b 级权重；否则内存最大。"""
    nodes = REGISTRY.with_lock(lambda d: list(d.items()))
    cands = [(k, v) for k, v in nodes if common.AI_CAP in v.get("capabilities", [])]
    if not cands:
        return None

    def _comp(v):
        return v.get("info", {}).get("compute", {})

    if model_hint == "gpu":
        g = [x for x in cands if _comp(x[1]).get("gpu")]
        if g:
            cands = g
    elif model_hint == "4b":
        m = [x for x in cands if any("4b" in s for s in _comp(x[1]).get("models", []))]
        if m:
            cands = m
    cands.sort(key=lambda x: _comp(x[1]).get("ram_gb", 0), reverse=True)
    return cands[0][0]


def _relay_ai_to_node(prompt, ctx_ns=None, model_hint=None, timeout=60):
    """把 AI 请求路由到「谁能干 AI」的节点，附带上 L3 共享上下文（跨设备不重复说）。"""
    if model_hint in ("", "None"):
        model_hint = None
    if model_hint not in common.MODEL_HINTS:
        model_hint = None
    target = pick_ai_node(model_hint)
    if not target:
        audit.log("ai.route", ok=False, err="no_ai_node", hint=model_hint)
        return {"ok": False, "err": "no_ai_node"}
    ctx = ""
    if ctx_ns:
        rows = _ctx().recent(ctx_ns, 5)
        if rows:
            ctx = "\n".join(f"{r['key']}: {r['val']}" for r in rows)
    r = send_to_node(target, {"t": common.AI_CHAT, "prompt": prompt, "ctx_ns": ctx_ns,
                              "model_hint": model_hint, "ctx": ctx}, timeout=timeout)
    audit.log("ai.route", node=target, hint=model_hint, ctx_ns=ctx_ns, ok=bool(r.get("ok")))
    if not r.get("ok"):
        return {"ok": False, "err": r.get("err")}
    return {"ok": True, "text": r.get("text", ""), "model": r.get("model"), "node": target}


def _thin_watchdog(mid, timeout):
    """瘦客户端兜底：AI 节点长时间不回就报超时并释放连接，别让它挂在墙上。"""
    time.sleep(timeout)
    with _THIN_LK:
        w = THIN_WAITERS.pop(mid, None)
    if not w:
        return
    try:
        common.send_msg(w["conn"], {"t": common.AI_RESULT, "id": mid, "text": "",
                                    "err": "AI 节点响应超时"})
    except OSError:
        pass
    try:
        w["conn"].close()
    except OSError:
        pass
    audit.log("ai.timeout", id=mid)


def _forward_ai(m):
    """AI 节点回吐的 AI_CHUNK / AI_RESULT -> 转发给正在等待的瘦客户端。
    AI_RESULT 到达即关闭该连接（一问一答，收完就走）。"""
    mid = m.get("id")
    with _THIN_LK:
        w = THIN_WAITERS.get(mid)
    if not w:
        return False
    try:
        common.send_msg(w["conn"], m)
    except OSError:
        pass
    if m.get("t") == common.AI_RESULT:
        with _THIN_LK:
            THIN_WAITERS.pop(mid, None)
        try:
            w["conn"].close()
        except OSError:
            pass
    return True


def _serve_thin_client(conn, m, timeout=120):
    """瘦客户端通道：客户端无权重，经 Commander 路由到 AI 节点；**真·流式**回吐。

    登记 req id -> conn 的等待表后即返回(不关闭连接)，AI 节点的每段 AI_CHUNK
    由 _handle_node 线程经 _forward_ai 即时转发；AI_RESULT 到达才收尾关闭。
    """
    mid = m.get("id") or _next_id("ai")
    hint = m.get("model_hint") if m.get("model_hint") in common.MODEL_HINTS else None
    target = pick_ai_node(hint)
    if not target:
        common.send_msg(conn, {"t": common.AI_RESULT, "id": mid, "text": "",
                               "err": "no_ai_node"})
        audit.log("ai.route", ok=False, err="no_ai_node", hint=hint)
        try:
            conn.close()
        except OSError:
            pass
        return
    ctx = ""
    if m.get("ctx_ns"):
        rows = _ctx().recent(m.get("ctx_ns"), 5)
        if rows:
            ctx = "\n".join(f"{r['key']}: {r['val']}" for r in rows)
    with _THIN_LK:
        THIN_WAITERS[mid] = {"conn": conn, "deadline": time.time() + timeout}
    node = REGISTRY.with_lock(lambda d: d.get(target))
    try:
        common.send_msg(node["sock"], {"t": common.AI_CHAT, "id": mid,
                                       "prompt": m.get("prompt", ""),
                                       "ctx_ns": m.get("ctx_ns"), "ctx": ctx,
                                       "model_hint": hint, "stream": True})
    except OSError as e:
        with _THIN_LK:
            THIN_WAITERS.pop(mid, None)
        try:
            common.send_msg(conn, {"t": common.AI_RESULT, "id": mid, "text": "",
                                   "err": f"转发 AI 节点失败: {e}"})
            conn.close()
        except OSError:
            pass
        return
    audit.log("ai.route", node=target, hint=hint, ctx_ns=m.get("ctx_ns"), stream=True)
    threading.Thread(target=_thin_watchdog, args=(mid, timeout), daemon=True).start()


def _cmd_ai(prompt, model_hint=None):
    r = _relay_ai_to_node(prompt, model_hint=model_hint)
    if not r.get("ok"):
        print(f"  AI 服务失败: {r.get('err')}")
        return
    print(f"  → {r['node']} (model={r.get('model')}, hint={model_hint or 'auto'})")
    print("  ", r["text"])


def _cmd_ainodes():
    ns = REGISTRY.with_lock(lambda d: [(k, v) for k, v in d.items()
                                       if common.AI_CAP in v.get("capabilities", [])])
    if not ns:
        print("（AI 池为空）")
        return
    for k, v in ns:
        c = v.get("info", {}).get("compute", {})
        print(f"  {k:12} gpu={c.get('gpu')} ram={c.get('ram_gb')}GB models={c.get('models')}")


def _cmd_aihelp():
    print("  她（本地推理）在哪个节点跑，用户就在哪个节点问；瘦客户端只发 prompt：")
    print("    ai <提示词>           # 自动路由到「谁能干 AI」的节点")
    print("    ai gpu <提示词>       # 指定档位 small / 4b / gpu")
    print("    ainodes               # 看 AI 池（带权重/算力画像的节点）")


def repl():
    print("cmd> nodes | exec <节点> <命令> | broadcast <命令> | push <节点> <文件> | pull <节点> <远程> [输出]")
    print("      | ctx_put <ns> <key> <值> | ctx_get <ns> <key> | ctx_search <ns> <查询> | ctx_del <ns> <key>")
    print("      | token <节点> | queue <节点> | compute <提示词> | computemap <p1;p2;...> | computepool | quit")
    print("      | ai [small|4b|gpu] <提示词> | ainodes | aihelp   # 远程 AI 即服务（瘦客户端调她）")
    print("      | audit [n]   # 看控制面审计流水（谁在什么时候调了什么）")
    print("      | caps | can <能力>   # 能力自描述：看编队能做什么、怎么调（编排发现面）")
    while True:
        try:
            line = input("cmd> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        op, rest = (line.split(" ", 1) + ["", ""])[:2]
        op = op.lower()
        if op == "quit":
            break
        elif op == "nodes":
            _cmd_nodes()
        elif op == "exec":
            sp = rest.split(" ", 1)
            if len(sp) == 2:
                _cmd_exec(sp[0], sp[1])
        elif op == "broadcast":
            if rest:
                _cmd_broadcast(rest)
        elif op == "push":
            sp = rest.split(" ", 1)
            if len(sp) == 2:
                _cmd_push(sp[0], sp[1])
        elif op == "pull":
            sp = rest.split(" ", 2)
            if len(sp) >= 2:
                _cmd_pull(sp[0], sp[1], sp[2] if len(sp) >= 3 else sp[1].split("/")[-1])
        elif op == "ctx_put":
            sp = rest.split(" ", 2)
            if len(sp) == 3:
                _ctx_put(sp[0], sp[1], sp[2])
        elif op == "ctx_get":
            sp = rest.split(" ", 1)
            if len(sp) == 2:
                _ctx_get(sp[0], sp[1])
        elif op == "ctx_search":
            sp = rest.split(" ", 1)
            if len(sp) == 2:
                _ctx_search(sp[0], sp[1])
        elif op == "ctx_del":
            sp = rest.split(" ", 1)
            if len(sp) == 2:
                _ctx_del(sp[0], sp[1])
        elif op == "token":
            if rest:
                _cmd_token(rest)
        elif op == "queue":
            if rest:
                _cmd_queue(rest)
        elif op == "compute":
            if rest:
                _cmd_compute(rest)
        elif op == "computemap":
            if rest:
                _cmd_computemap(rest)
        elif op == "computepool":
            _cmd_computepool()
        elif op == "ai":
            if rest:
                sp = rest.split(" ", 1)
                hint = sp[0] if sp[0] in common.MODEL_HINTS else None
                _cmd_ai(sp[1] if hint else rest, hint)
        elif op == "ainodes":
            _cmd_ainodes()
        elif op == "aihelp":
            _cmd_aihelp()
        elif op == "caps":
            _cmd_caps()
        elif op == "can":
            if rest:
                _cmd_can(rest)
        elif op == "audit":
            _cmd_audit(int(rest) if rest.isdigit() else 20)
        else:
            print("  未知命令")


def self_test(host, port, password):
    """本地回路自检：起服务 + 多节点，证明身体 Phase 0–4 + 她授权闭环 + 反向推送 + 共享算力 + 远程 AI 即服务。"""
    global SELF_TEST
    SELF_TEST = True
    os.environ["FLEET_CTX"] = ":memory:"
    os.environ["FLEET_SELFTEST"] = "1"   # 关掉她的静默时段刹车，让自检能演示主动开口
    import tempfile
    audit.configure(os.path.join(tempfile.mkdtemp(prefix="fleet_audit_"), "audit.jsonl"))
    import node_agent
    import bootstrap as boot
    import remote_ai_client
    port = _free_port(port)
    boot_port = _free_port(9100)
    threading.Thread(target=serve, args=(host, port, password), daemon=True).start()
    time.sleep(0.4)

    # 先写共享上下文，确保她的节点注册后能跨设备读到（L3 一体）
    _ctx_put("her", "game_lastnight", "昨晚那局还没玩完，说到第3关")

    # 两台 Windows 常驻节点
    node_agent.run_node(host, port, "PC-A", password, "windows",
                         caps=["exec", "push", "pull", "ctx"], sandbox="sandbox_A", bg=True)
    # 她的节点（手机占位）：带 persona 守护 + AI 能力，演示 P4 闭环 + 远程 AI 即服务
    node_agent.run_node(host, port, "Her-Phone", password, "android",
                         caps=["exec", "ctx", "persona", "ai"], sandbox="sandbox_her", bg=True,
                         models="polaris-0.4b,qwen3-4b", gpu=False, ram_gb=16,
                         persona_daemon=True, tick=4)
    # 算力盒子（常驻家中枢候选）：上报 GPU + 本地模型，承接重活/大模型（她三段式 ③）
    node_agent.run_node(host, port, "Box", password, "android",
                         caps=["exec", "ctx", "compute"], sandbox="sandbox_box", bg=True,
                         models="polaris-0.4b,qwen3-4b", gpu=True, ram_gb=8)
    for _ in range(50):
        if REGISTRY.with_lock(lambda d: len(d)) >= 3:
            break
        time.sleep(0.1)

    print("\n=== 自检：本机经密码控制另一台(PC-A) ===", flush=True)
    _cmd_nodes()
    _cmd_exec("PC-A", "echo 由Commander控制")
    _cmd_exec("PC-A", "hostname")
    _cmd_exec("PC-A", "whoami")

    print("\n=== 能力自描述（节点上报说明书，供统一意图编排「发现」）===", flush=True)
    _cmd_caps()
    _cmd_can("exec")

    print("\n=== L3 共享上下文（跨设备记忆，海马体）===", flush=True)
    _ctx_search("her", "昨晚")
    _ctx_get("her", "game_lastnight")
    print("  （Her-Phone 节点在注册时已跨设备读到 her/game_lastnight，见其上线日志）")

    print("\n=== 离线队列 + 上线重放（Phase 4 透明上下线）===", flush=True)
    _enqueue("PC-Ghost", "echo 上线重放OK")
    print("  （PC-Ghost 尚未上线，命令已排队）", flush=True)
    node_agent.run_node(host, port, "PC-Ghost", password, "windows",
                         caps=["exec"], sandbox="sandbox_ghost", bg=True)
    time.sleep(1.0)  # 等其上线->重放

    print("\n=== 反向推送 bootstrap（第三原则，loopback 校验）===", flush=True)
    srv = boot.start_server(".", host, boot_port)
    b = boot.fetch_bytes(f"http://{host}:{boot_port}", "node_agent.py")
    local = open("node_agent.py", "rb").read()
    print(f"  bootstrap 分发 node_agent.py: {len(b)}B，与本地一致={b == local}")
    srv.shutdown()

    print("\n=== 配对设备一次性令牌（卸载闸门 ②）===", flush=True)
    _cmd_token("PC-A")

    print("\n=== 共享算力路由（§九 dev_pick：选「谁能干」的节点跑重活）===", flush=True)
    _cmd_compute("帮我写段按修改时间排序的 Python", prefer_gpu=True)
    print("\n=== 共享算力池：批量分发 + 跨设备结果缓存 ===", flush=True)
    _cmd_computepool()
    _cmd_computemap("解释什么是注意力机制;用一句话总结Transformer;给个快速排序的Python", prefer_gpu=True)

    print("\n=== 远程 AI 即服务（RFC 合入：瘦客户端不装权重，经 Commander 调她）===", flush=True)
    print(f"  本地推理后端: {llm.backend()}  （设 FLEET_LLM_CMD / FLEET_LLM_URL 即接真模型）")
    _cmd_ainodes()
    _cmd_ai("大肥鱼，昨晚那局还玩不玩？", model_hint="small")
    chunks = []

    def _on_delta(d):
        chunks.append(d)
        print(d, end="", flush=True)
    print("  [瘦客户端·流式] ", end="", flush=True)
    text, model, err = remote_ai_client.ask(host, port, password,
                                            "帮我用一句话总结，我这个编队现在有几台设备",
                                            ctx_ns="her", model_hint="4b", on_delta=_on_delta)
    print(f"\n  [瘦客户端] 收到 {len(chunks)} 段流式增量，文本长度={len(text or '')} model={model} err={err}")

    time.sleep(1.5)  # 等她的心念 tick 触发主动开口 + 授权闭环
    print("\n=== 她的人格运行时（心念 tick / 主动开口 / 诉求授权闭环）===", flush=True)
    print("  （见上方 [她·主动] 与 [她] 授权日志；她的执念/心情落 persona_state.json，心事落 L3）")

    print("\n=== 控制面审计（统一调用面留痕，可观测/可追责）===", flush=True)
    _cmd_audit(12)

    print("\n=== 自检通过：L0/L1/L2/L3 + 她（授权+人格运行时）+ 反向推送 + 共享算力 + 远程 AI 即服务(流式) + 审计 ===", flush=True)
    os._exit(0)


def main():
    global CTX
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--password", default=os.environ.get("FLEET_PASSWORD", "changeme"))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test(args.host, args.port, args.password)
    else:
        threading.Thread(target=serve, args=(args.host, args.port, args.password), daemon=True).start()
        repl()


if __name__ == "__main__":
    main()

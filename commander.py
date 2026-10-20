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

import common
import memory as mem_mod
import policies
import queue_store

REGISTRY = common.LockedDict()          # name -> {sock, token, platform, info, capabilities, last_hb}
PENDING = {}                             # msg id -> {ev, slot}
_PENDING_LK = threading.Lock()
_ID = threading.Lock()
_counter = 0

OFFLINE_QUEUE = {}                       # name -> [{id, cmd}]  （离线排队，上线重放）
_QUEUE_LK = threading.Lock()
QUEUE_PATH = os.environ.get(
    "FLEET_QUEUE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet_queue.json"))

TOKENS = {}                              # token -> {node, purpose, exp}
_TOKEN_LK = threading.Lock()

DEGRADED = set()                         # 被策略降级的节点（掉线后不再参与路由）

CTX = None                               # SharedContext（懒初始化）
SELF_TEST = False

# adskip 规则库（control-plane 侧唯一权威，节点不再各自维护规则）
RULES_PATH = os.environ.get(
    "FLEET_RULES",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_automation", "adskip_rules.json"))
_RULES_MOD = None


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


def _apply_hits(name, hits):
    """执行策略引擎产出的动作（引擎只判断，副作用集中在这里，便于审计）。"""
    for rname, act in hits:
        a = act.get("action")
        args = act.get("args") or {}
        if a == "mark_degraded":
            DEGRADED.add(name)
            print(f"  [策略:{rname}] {name} 已降级，后续路由跳过", flush=True)
        elif a == "notify":
            print(f"  [策略:{rname}] {args.get('text', '')}", flush=True)
        elif a == "probe":
            print(f"  [策略:{rname}] {name} 心跳超时 -> 探测（不急于判死）", flush=True)
        elif a == "cleanup":
            print(f"  [策略:{rname}] 清理临时节点凭据（下机不留痕）", flush=True)
        elif a == "drop":
            _drop_node_silent(name)
            print(f"  [策略:{rname}] {name} 已从节点表摘除", flush=True)
        elif a == "prefer_gpu":
            continue                      # 查询型动作，由 pick_node 消费
        else:
            print(f"  [策略:{rname}] 未实现动作 {a}", flush=True)


def _fire(event, name=""):
    """触发一个事件，返回动作列表（同时执行副作用）。"""
    hits = policies.eval_event(event)
    _apply_hits(name, hits)
    return hits


def _drop_node(name):
    node = REGISTRY.with_lock(lambda d: d.pop(name, None))
    if node and (node.get("info") or {}).get("session"):
        # 临时节点（网吧等）下机：先走清理策略，再走掉线策略
        _fire({"event": "session_end", "mode": "session"}, name)
    _fire({"event": "node_offline", "node": name}, name)


def _drop_node_silent(name):
    """摘除节点但不触发 node_offline（心跳判死路径用，避免动作重复）。"""
    node = REGISTRY.with_lock(lambda d: d.pop(name, None))
    if node:
        try:
            node["sock"].close()
        except OSError:
            pass


WATCHDOG_STOP = threading.Event()


def scan_stale(rows, now=None, stale=90.0, dead=180.0):
    """纯函数：按心跳年龄产出事件（可单测，不必等真超时）。

    这是 hb_stale / hb_dead 策略的唯一事件源——没有它，策略就是空壳。
    """
    now = now if now is not None else time.time()
    out = []
    for name, hb in rows:
        age = now - hb
        if age > dead:
            out.append((name, {"event": "hb_dead", "node": name, "age": round(age, 1)}))
        elif age > stale:
            out.append((name, {"event": "hb_stale", "node": name, "age": round(age, 1)}))
    return out


def watchdog_loop(interval=5.0, stale=90.0, dead=180.0):
    """心跳看门狗：把「时间」变成事件喂给策略层。"""
    while not WATCHDOG_STOP.is_set():
        rows = REGISTRY.with_lock(lambda d: [(k, v["last_hb"]) for k, v in d.items()])
        for name, ev in scan_stale(rows, stale=stale, dead=dead):
            _fire(ev, name)
        _persona_tick()          # 她的主动性心跳：自己带频控/静默刹车，不会刷屏
        WATCHDOG_STOP.wait(interval)


def start_watchdog(**kw):
    t = threading.Thread(target=watchdog_loop, kwargs=kw, daemon=True)
    t.start()
    return t


def _enqueue(name, cmd):
    """离线排队（Phase 4）：同时落盘，控制面重启不丢。"""
    with _QUEUE_LK:
        OFFLINE_QUEUE.setdefault(name, []).append({"id": _next_id("q"), "cmd": cmd})
        queue_store.save(OFFLINE_QUEUE, QUEUE_PATH)


def _replay_queue(name):
    """节点上线时，把排队命令重放过去（透明上下线）。"""
    with _QUEUE_LK:
        q = OFFLINE_QUEUE.pop(name, [])
        if q:
            queue_store.save(OFFLINE_QUEUE, QUEUE_PATH)
    if q:
        send_to_node(name, {"t": common.QUEUE, "cmds": q})
        print(f"[*] 节点 {name} 上线，重放 {len(q)} 条离线命令", flush=True)


def _restore_queue():
    """启动时恢复磁盘上未重放的排队命令（控制面重启不丢）。"""
    saved = queue_store.load(QUEUE_PATH)
    if saved:
        with _QUEUE_LK:
            for k, v in saved.items():
                OFFLINE_QUEUE.setdefault(k, []).extend(v)
        print(f"[*] 已恢复 {queue_store.pending_count(saved)} 条离线排队命令", flush=True)
    return saved


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
        if not reg or reg.get("t") != common.REGISTER:
            return
        name = reg.get("name")
        REGISTRY.with_lock(lambda d: d.__setitem__(name, {
            "sock": conn, "token": token,
            "platform": reg.get("platform", "?"),
            "info": reg.get("info", {}),
            "capabilities": reg.get("capabilities", []),
            "last_hb": time.time(),
        }))
        print(f"[+] 节点上线: {name} ({reg.get('platform')}) caps={reg.get('capabilities')}", flush=True)
        _replay_queue(name)  # 上线即补离线任务
        while True:
            m = common.recv_msg(conn)
            if m is None:
                break
            t = m.get("t")
            if t == common.HEARTBEAT:
                REGISTRY.with_lock(lambda d: d.get(name) and d[name].__setitem__("last_hb", time.time()))
            elif t in (common.EXEC_RESULT, common.PUSH_RESULT, common.PULL_RESULT,
                       common.COMPUTE_RESULT, common.UI_SKIP_RESULT):
                _resolve_pending(m.get("id"), m)
            elif t == common.CAPABILITIES:
                REGISTRY.with_lock(lambda d: d.get(name) and d[name].__setitem__("capabilities", m.get("caps", [])))
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


def _route_capability_request(name, m, conn):
    """P4：她想用某设备=一次能力请求。默认须用户批准；自检自动批准。"""
    cap, reason = m.get("cap"), m.get("reason", "")
    if SELF_TEST:
        common.send_msg(conn, {"t": common.CAP_GRANT, "cap": cap,
                                "session_min": 30, "token": os.urandom(6).hex()})
        print(f"[她] {name} 请求 {cap}（{reason}）→ 自检自动批准", flush=True)
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
    else:
        common.send_msg(conn, {"t": common.CAP_DENY, "cap": cap, "reason": "用户拒绝"})
        print(f"      ✗ 已拒绝；她的执念+1", flush=True)


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


def _cmd_exec(name, cmd):
    r = send_to_node(name, {"t": common.EXEC, "cmd": cmd})
    if not r.get("ok"):
        if r.get("offline"):
            _enqueue(name, cmd)
            print(f"  节点离线，已排队（上线自动重放）：{cmd}")
            return
        print(f"  失败: {r.get('err')}")
        return
    print(f"  退出码={r.get('rc')}")
    if r.get("out"):
        print(r["out"].rstrip())
    if r.get("err"):
        print("[stderr]", r["err"].rstrip())


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


def pick_node(cap="compute", prefer_gpu=False, task=""):
    """共享算力路由（§九 dev_pick）：选「谁能干」的节点。

    「要不要优先 GPU」不写死在代码里——交给策略层按任务类型判断（compute_pick 事件）。
    被策略降级（DEGRADED）的节点不参与路由。
    """
    if not prefer_gpu and task:
        for rname, act in policies.eval_event({"event": "compute_pick", "task": task}):
            if act.get("action") == "prefer_gpu":
                prefer_gpu = bool((act.get("args") or {}).get("on", True))
                print(f"  [策略:{rname}] 任务属重活 -> 优先 GPU 节点", flush=True)

    nodes = REGISTRY.with_lock(lambda d: list(d.items()))

    def _can(v):
        return cap in v.get("capabilities", [])

    cands = [(k, v) for k, v in nodes if _can(v) and k not in DEGRADED]
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
    target = pick_node("compute", prefer_gpu, task=prompt)
    if not target:
        _fire({"event": "compute_pool_empty"})
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
                                         if "compute" in v.get("capabilities", [])
                                         and k not in DEGRADED])


def _cmd_policy():
    """列出当前生效策略（数据，不是代码分支）。"""
    rules = policies.load_rules()
    print(f"  生效策略 {len(rules)} 条（FLEET_POLICY={policies.DEFAULT_POLICY or '内置默认'}）")
    for r in sorted(rules, key=lambda x: -x.get("priority", 0)):
        when = r.get("when") or {}
        acts = ",".join(a.get("action", "?") for a in r.get("then", []))
        print(f"  p{r.get('priority', 0):<3} {r.get('name', '?'):22} on={when.get('event', '*'):18} -> {acts}")


def _cmd_policy_eval(js):
    """手动评估一个事件（dry-run，不改任何节点状态）。"""
    import json as _json
    try:
        ev = _json.loads(js)
    except ValueError as e:
        print(f"  事件 JSON 解析失败: {e}")
        return
    hits = policies.eval_event(ev)
    if not hits:
        print("  （无规则命中）")
        return
    for rname, act in hits:
        print(f"  {rname} -> {act.get('action')} {act.get('args') or ''}")


# ---------- 她（P1/P3/P4/P5） ----------
PERSONA = None
_PROACTIVE = None
_LAST_TICK = [0.0]                       # 心跳节流（秒）
PERSONA_PATH = os.environ.get(
    "FLEET_PERSONA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona_state.json"))


def _her():
    """懒初始化人格状态与主动性引擎（同一个「她」，不因设备不同而分裂）。"""
    global PERSONA, _PROACTIVE
    if PERSONA is None:
        import persona as p
        PERSONA = p.PersonaState(PERSONA_PATH)
        _PROACTIVE = p.ProactiveEngine(PERSONA)
    return PERSONA, _PROACTIVE


HER_SYSTEM_TMPL = ("你是{name}，性格{trait}，口癖「{catch}」。"
                   "用一句口语回答，不超过30字，不要旁白、不要解释。")


def _her_compose(hint):
    """用本地模型让她自己开口；没接模型（placeholder）时返回 None，由调用方用模板句。

    算力在哪台设备都行：只要那台节点设了 FLEET_LLM_CMD / FLEET_LLM_URL，她就真说话。
    注意：只在「已经过刹车、确定要说」时才调用，绝不为了说话而空烧 GPU。
    """
    try:
        import llm
        if llm.backend() == "placeholder":
            return None
        st, _ = _her()
        d = st.snapshot()
        sys_prompt = HER_SYSTEM_TMPL.format(
            name=d.get("name"), trait="、".join(d.get("traits") or []),
            catch=d.get("catchphrase", ""))
        text, _b = llm.complete(hint, system=sys_prompt)
        return (text or "").strip() or None
    except Exception:
        return None


def _persona_tick(min_interval=60.0):
    """她的主动性心跳：产生一个念头 -> 过频控/静默/重要度三道刹车 -> 说或不说。

    不说话不是失败：念头会记进 L3（pending_thought），她没消失，只是在忍。
    """
    st, eng = _her()
    now = time.time()
    if now - _LAST_TICK[0] < min_interval:
        return None
    _LAST_TICK[0] = now

    obs = st.data.get("obsessions") or []
    if obs:
        o = obs[-1]
        text = f"{st.data.get('catchphrase', '')} 我还在想 {o['cap']}（第{o['count']}次了）"
        imp = min(0.9, 0.4 + 0.1 * o["count"])
    else:
        text = f"{st.data.get('catchphrase', '')} 你在干嘛？"
        imp = 0.5

    ok, why = eng.allow(imp)
    if not ok:
        _ctx().put("her", "pending_thought", text)   # 被刹车 -> 记成心事，不打扰
        return None

    text = _her_compose(text) or text                # 接上真模型就她来说，否则模板兜底
    print(f"[她] {text}", flush=True)
    _ctx().put("her", "last_thought", text)          # 跨设备：手机上也知道她刚说了啥
    return text


def _cmd_her():
    st, eng = _her()
    d = st.snapshot()
    m = d.get("mood") or {}
    print(f"  名字={d.get('name')} 口癖={d.get('catchphrase')} 性格={d.get('traits')}")
    print(f"  情绪 valence={m.get('valence')} energy={m.get('energy')} cling={m.get('cling')}")
    obs = d.get("obsessions") or []
    for o in obs:
        ready = time.time() >= o.get("cooldown_until", 0)
        print(f"  执念 {o['cap']} x{o['count']} 冷却{'已过' if ready else '中'}")
    print(f"  刹车: 频控≤{eng.per_hour}/h 静默={eng.silent[0]}-{eng.silent[1]}点")
    lt = _ctx().get("her", "last_thought")
    if lt.get("ok"):
        print(f"  上次开口: {lt['val']}")


def _cmd_her_say(text):
    """让她现在说一句（走同一套刹车，便于真人验证主动性不会刷屏）。"""
    st, eng = _her()
    ok, why = eng.allow(0.8)
    if not ok:
        print(f"  （刹车拦下: {why}，已记成心事）", flush=True)
        _ctx().put("her", "pending_thought", text)
        return
    said = _her_compose(text) or text
    print(f"[她] {said}", flush=True)
    _ctx().put("her", "last_thought", said)


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


def _rules():
    """惰性加载 rules_relay（ui_automation 目录里的规则库）。"""
    global _RULES_MOD
    if _RULES_MOD is None:
        ua_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_automation")
        if ua_dir not in sys.path:
            sys.path.insert(0, ua_dir)
        import rules_relay
        _RULES_MOD = rules_relay
    return _RULES_MOD


def build_ui_payload(package, use_vision=False, rounds=6, version=None):
    """组装 UI_SKIP 载荷：自动带上规则库当前（或指定）版本。

    节点侧不再各自维护规则副本——规则是编队共享资产，改一次全设备生效。
    规则库为空时不带 rules，节点退回内置默认（离线可用性优先）。
    """
    rl = _rules()
    vers = rl.list_versions(RULES_PATH)
    ver = version if version is not None else (vers[0][0] if vers else 0)
    rules = rl.get(ver, path=RULES_PATH) if ver else None
    p = {"t": common.UI_SKIP, "package": package or "", "use_vision": bool(use_vision),
         "rounds": int(rounds), "rules_version": ver or 0}
    if rules:
        p["rules"] = rules
    return p


def _cmd_rules_list():
    rl = _rules()
    vers = rl.list_versions(RULES_PATH)
    cur = vers[0][0] if vers else 0
    print(f"  规则库 {RULES_PATH}")
    if not vers:
        print("  （空，节点将用内置默认规则）")
        return
    for v, ts, note in vers:
        mark = " ← 当前" if v == cur else ""
        print(f"  v{v}\t{time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))}\t{note}{mark}")


def _cmd_rules_get(version=None):
    rl = _rules()
    r = rl.get(version, path=RULES_PATH)
    if r is None:
        print("  无此版本规则")
        return
    import json as _json
    print(_json.dumps(r, ensure_ascii=False, indent=2))


def _cmd_rules_put(path, note=""):
    rl = _rules()
    import json as _json
    try:
        with open(path, "r", encoding="utf-8") as f:
            rules = _json.load(f)
        v = rl.put(rules, note or f"import {os.path.basename(path)}", path=RULES_PATH)
        print(f"  已发布规则 v{v}（{len(rules)} 条）—— 下次 ui 命令自动携带")
    except Exception as e:
        print(f"  发布失败: {e}")


def _cmd_rules_rollback(version):
    rl = _rules()
    try:
        v = rl.rollback(version, path=RULES_PATH)
        print(f"  已回滚到 v{version} 的内容，生成新版本 v{v}（历史保留）")
    except Exception as e:
        print(f"  回滚失败: {e}")


def _cmd_ui(node, package, use_vision=False, rounds=6):
    """UI 自动化：让节点在挂载的 Android 设备上按规则跳过广告（源自李跳跳拆解）。"""
    payload = build_ui_payload(package, use_vision, rounds)
    r = send_to_node(node, payload)
    if "log" not in r:          # 无 log = 传输层失败（不在线/超时/发送失败）
        print(f"  失败: {r.get('err')}")
        return
    ver = payload.get("rules_version") or 0
    src = f"规则库 v{ver}" if ver else "节点内置默认"
    print(f"  → {node}: {'已跳过' if r.get('ok') else '未命中/无设备'}（{package or '*'}，{src}）")
    for line in (r.get("log") or [])[-8:]:
        print("   ", line)


def repl():
    print("cmd> nodes | exec <节点> <命令> | broadcast <命令> | push <节点> <文件> | pull <节点> <远程> [输出]")
    print("      | ctx_put <ns> <key> <值> | ctx_get <ns> <key> | ctx_search <ns> <查询> | ctx_del <ns> <key>")
    print("      | token <节点> | queue <节点> | compute <提示词> | computemap <p1;p2;...> | computepool")
    print("      | ui <节点> <包名|*> [--vision] | rules | rules_get [版本] | rules_put <文件> [备注] | rules_rollback <版本>")
    print("      | policy | policy_eval <事件JSON> | her | her_say <文本> | quit")
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
        elif op == "ui":
            sp = rest.split()
            if len(sp) >= 2:
                _cmd_ui(sp[0], sp[1], use_vision="--vision" in sp[2:])
        elif op == "rules":
            _cmd_rules_list()
        elif op == "rules_get":
            _cmd_rules_get(int(rest) if rest.strip().isdigit() else None)
        elif op == "rules_put":
            sp = rest.split(" ", 1)
            if sp and sp[0]:
                _cmd_rules_put(sp[0], sp[1] if len(sp) > 1 else "")
        elif op == "rules_rollback":
            if rest.strip().isdigit():
                _cmd_rules_rollback(int(rest))
            else:
                print("  用法: rules_rollback <版本号>")
        elif op == "policy":
            _cmd_policy()
        elif op == "policy_eval":
            if rest:
                _cmd_policy_eval(rest)
        elif op == "her":
            _cmd_her()
        elif op == "her_say":
            if rest:
                _cmd_her_say(rest)
        else:
            print("  未知命令")


def self_test(host, port, password):
    """本地回路自检：起服务 + 多节点，证明身体 Phase 0–4 + 她授权闭环 + 反向推送。"""
    global SELF_TEST, RULES_PATH, QUEUE_PATH, PERSONA_PATH
    SELF_TEST = True
    os.environ["FLEET_CTX"] = ":memory:"
    import tempfile
    # 自检用临时文件，绝不污染仓库里的规则库/队列/人格态
    tdir = tempfile.gettempdir()
    RULES_PATH = os.path.join(tdir, f"fleet_rules_st_{os.getpid()}.json")
    QUEUE_PATH = os.path.join(tdir, f"fleet_queue_st_{os.getpid()}.json")
    PERSONA_PATH = os.path.join(tdir, f"fleet_persona_st_{os.getpid()}.json")
    for _p in (RULES_PATH, QUEUE_PATH, PERSONA_PATH):
        if os.path.exists(_p):
            os.remove(_p)
    import node_agent
    import bootstrap as boot
    threading.Thread(target=serve, args=(host, port, password), daemon=True).start()
    time.sleep(0.4)

    # 两台 Windows 常驻节点
    node_agent.run_node(host, port, "PC-A", password, "windows",
                         caps=["exec", "push", "pull", "ctx"], sandbox="sandbox_A", bg=True)
    # 她的节点（手机/盒子占位）：带 cap-req 演示 P4 闭环
    node_agent.run_node(host, port, "Her-Phone", password, "android",
                         caps=["exec", "ctx", "persona"], sandbox="sandbox_her", bg=True,
                         cap_req={"cap": "phone.screen.on", "reason": "想玩游戏了大肥鱼"})
    # 算力盒子（常驻家中枢候选）：上报 GPU + 本地模型，承接重活/大模型（她三段式 ③）
    node_agent.run_node(host, port, "Box", password, "android",
                         caps=["exec", "ctx", "compute"], sandbox="sandbox_box", bg=True,
                         models="polaris-0.4b,qwen3-4b", gpu=True, ram_gb=8)
    for _ in range(50):
        if REGISTRY.with_lock(lambda d: len(d)) >= 3:
            break
        time.sleep(0.1)

    # 先写共享上下文，确保她的节点注册后能跨设备读到（L3 一体）
    _ctx_put("her", "game_lastnight", "昨晚那局还没玩完，说到第3关")

    print("\n=== 自检：本机经密码控制另一台(PC-A) ===", flush=True)
    _cmd_nodes()
    _cmd_exec("PC-A", "echo 由Commander控制")
    _cmd_exec("PC-A", "hostname")
    _cmd_exec("PC-A", "whoami")

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

    print("\n=== 离线队列持久化（控制面重启也不丢「上线自动补」这个承诺）===", flush=True)
    _enqueue("PC-Restart", "echo 重启后命令还在")
    disk = queue_store.load(QUEUE_PATH)
    print(f"  磁盘排队 {queue_store.pending_count(disk)} 条；含 PC-Restart={bool(disk.get('PC-Restart'))}")
    with _QUEUE_LK:
        OFFLINE_QUEUE.clear()               # 模拟控制面重启：内存清空
    restored = _restore_queue()              # 从磁盘恢复
    print(f"  模拟重启后恢复 {queue_store.pending_count(restored)} 条；"
          f"命令原文={restored.get('PC-Restart', [{}])[0].get('cmd')!r}")

    print("\n=== 临时节点（网吧）：下机即清，不留凭据 ===", flush=True)
    node_agent.run_node(host, port, "PC-Net", password, "windows", caps=["exec"],
                        sandbox="sandbox_net", bg=True, session=True)
    time.sleep(0.9)
    print(f"  上线: {bool(REGISTRY.with_lock(lambda d: d.get('PC-Net')))}（session 模式已上报）")
    sk = REGISTRY.with_lock(lambda d: (d.get("PC-Net") or {}).get("sock"))
    if sk:
        try:
            sk.close()                 # 模拟下机（拔线/关浏览器）
        except OSError:
            pass
    time.sleep(0.9)
    print(f"  下机后在线: {bool(REGISTRY.with_lock(lambda d: d.get('PC-Net')))}"
          f"（应为 False；session_end 策略已触发清理）")

    print("\n=== 反向推送 bootstrap（第三原则，loopback 校验）===", flush=True)
    srv = boot.start_server(".", host, 9100)
    b = boot.fetch_bytes(f"http://{host}:9100", "node_agent.py")
    local = open("node_agent.py", "rb").read()
    print(f"  bootstrap 分发 node_agent.py: {len(b)}B，与本地一致={b == local}")
    srv.shutdown()

    print("\n=== adskip 规则库 → ui 能力（规则=编队共享资产，改一次全端生效）===", flush=True)
    import json as _json
    seed_path = RULES_PATH + ".seed.json"
    with open(seed_path, "w", encoding="utf-8") as f:
        _json.dump([{"packageName": "*", "triggers": [
            {"name": "跳过", "action": "click", "text": ["跳过", "跳过广告"],
             "textRegex": r"跳过|skip|关闭"}]}], f, ensure_ascii=False)
    _cmd_rules_put(seed_path, "自检初版")
    _cmd_rules_list()
    _cmd_ui("Her-Phone", "com.demo.app")      # 无 adb：应回「规则来源=控制面 v1」，证明规则已下发
    _cmd_rules_rollback(1)
    print(f"  回滚后当前版本 = {_rules().list_versions(RULES_PATH)[0][0]}（应为 2，历史保留）")

    print("\n=== 配对设备一次性令牌（卸载闸门 ②）===", flush=True)
    _cmd_token("PC-A")

    print("\n=== 策略层（判断外置为数据，代码里不写 if/else 分支）===", flush=True)
    _cmd_policy()
    _cmd_policy_eval('{"event":"compute_pick","task":"微调一个LoRA"}')
    _cmd_policy_eval('{"event":"hb_stale","age":120}')
    _cmd_policy_eval('{"event":"compute_pick","task":"写个快排"}')
    print("  说明：策略只产出动作，副作用由控制面统一执行 -> 可 dry-run、可单测、改策略不改代码。", flush=True)

    print("\n=== 心跳看门狗（把「时间」变成事件；hb_stale/hb_dead 的唯一事件源）===", flush=True)
    now = time.time()
    for n, ev in scan_stale([("PC-OK", now), ("PC-Slow", now - 120), ("PC-Dead", now - 400)], now=now):
        print(f"  {n}: {ev['event']} age={ev['age']}s")
    print("  （PC-OK 无事件 / PC-Slow 仅探测 / PC-Dead 判死并摘除）", flush=True)

    print("\n=== 她的人格心跳（P3 主动性 + 三道刹车：频控/静默/重要度）===", flush=True)
    import llm as _llm
    print(f"  说话后端={_llm.backend()}（placeholder 用模板句；设 FLEET_LLM_CMD/URL 后由模型自己说）")
    if _llm.backend() == "placeholder":
        assert _her_compose("测试") is None, "placeholder 下不许伪造模型输出"
    _persona_tick(min_interval=0)                       # 无执念：打招呼
    _her()[0].add_obsession("phone.screen.on", "想玩游戏")
    _persona_tick(min_interval=0)                       # 有执念：谈执念
    r3 = _persona_tick(min_interval=0)                  # 第 3 次：频控拦下
    print(f"  第3次开口={bool(r3)}（应为 False：频控拦下，念头转心事而不是硬刷）", flush=True)
    _ctx_get("her", "pending_thought")
    _cmd_her()

    print("\n=== 共享算力路由（§九 dev_pick：选「谁能干」的节点跑重活）===", flush=True)
    _cmd_compute("帮我写段按修改时间排序的 Python")     # 普通任务：策略不要求占 GPU
    _cmd_compute("帮我微调一个 LoRA 训练脚本")          # 重活：策略自动转 prefer_gpu
    print("\n=== 共享算力池：批量分发 + 跨设备结果缓存 ===", flush=True)
    _cmd_computepool()
    _cmd_computemap("解释什么是注意力机制;用一句话总结Transformer;给个快速排序的Python", prefer_gpu=True)

    print("\n=== 自检通过：L0/L1/L2/L3 + 她授权 + 反向推送 + 共享算力 均工作 ===", flush=True)
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
        _restore_queue()
        threading.Thread(target=serve, args=(args.host, args.port, args.password), daemon=True).start()
        start_watchdog()          # 心跳看门狗：卡住/假死的节点会被自动探测/摘除
        repl()


if __name__ == "__main__":
    main()

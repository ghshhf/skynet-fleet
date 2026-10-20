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
import hmac
import json
import os
import socket
import sys
import threading
import time

import accounts
import arbiter
import common
import enroll
import memory as mem_mod
import netpath
import policies
import queue_store
import registry

# 节点表：在线在内存，历史在磁盘。重启后历史还在——「谁来过、它是什么」
# 不该每天从零认识一遍（但历史绝不冒充在线，见 registry.py）。
NODES_PATH = os.environ.get(
    "FLEET_NODES_PATH",
    os.path.join(os.path.expanduser("~"), ".fleet", "nodes.json"))

REGISTRY = registry.NodeTable(NODES_PATH)   # name -> {sock, token, platform, info, capabilities, last_hb}
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

# 入网凭据账本（PSK 哈希 -> 绑定的节点名）。扫码入网后节点用它认证，撤销也在这里。
ENROLL_LEDGER = os.environ.get(
    "FLEET_ENROLL_LEDGER",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet_enroll.json"))

DEGRADED = set()                         # 被策略降级的节点（掉线后不再参与路由）
DEBUG = bool(os.environ.get("FLEET_DEBUG"))   # FLEET_DEBUG=1：打印通信收发与超时

SERVE_HOST, SERVE_PORT = "127.0.0.1", 9000    # 监听地址（扫码入网要把它写进载荷）

CTX = None                               # SharedContext（懒初始化）
SELF_TEST = False

# adskip 规则库（control-plane 侧唯一权威，节点不再各自维护规则）
RULES_PATH = os.environ.get(
    "FLEET_RULES",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_automation", "adskip_rules.json"))
_RULES_MOD = None

# 发现账本：局域网里「看见过」的设备。**不等于入网**，只是记下来供人过目。
DISCOVERY_PATH = os.environ.get(
    "FLEET_DISCOVERY_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "discovered.json"))

# 扩散扫描账本：整个网段的在场设备（含多网段）。与发现账本分开存，
# 因为一个是「精细指纹」、一个是「全段底数」，混在一起会看不出哪条路生效。
SWEEP_PATH = os.environ.get(
    "FLEET_SWEEP_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "swept.json"))

# ---- 多控制面仲裁（一个意图绝不割裂）----
# 全编队只有一个领袖控制面能发令；其它待机（只读 REPL），领袖死了备用自动接管。
# 共享状态文件路径在 serve()/self_test() 里定（自检要指到临时文件，不碰真实文件）。
ARBITER_PATH = arbiter.default_path()
ARBITER_ID = os.environ.get("FLEET_COMMANDER_ID") or f"{socket.gethostname()}-{os.getpid()}"
ARBITER_RANK = int(os.environ.get("FLEET_RANK", str(arbiter.DEFAULT_RANK)) or arbiter.DEFAULT_RANK)


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
    t0 = time.time()
    node = REGISTRY.with_lock(lambda d: d.get(name))
    if not node:
        return {"ok": False, "err": f"节点 {name} 不在线", "offline": True}
    mid = msg.get("id") or _next_id("m")
    msg["id"] = mid
    ev = _store_pending(mid)
    if DEBUG:
        try:
            _fd = node["sock"].fileno()
        except OSError:
            _fd = "closed"
        print(f"[dbg] -> {name} {msg.get('t')} mid={mid} fd={_fd}", flush=True)
    try:
        common.send_msg(node["sock"], msg)
        if DEBUG:
            print(f"[dbg] .. 已写出 {name} mid={mid} ({time.time()-t0:.2f}s)", flush=True)
    except OSError as e:
        with _PENDING_LK:
            PENDING.pop(mid, None)
        return {"ok": False, "err": f"发送失败: {e}"}
    if not ev.wait(timeout):
        with _PENDING_LK:
            PENDING.pop(mid, None)
        if DEBUG:
            print(f"[dbg] {name} {msg.get('t')} mid={mid} 超时 {time.time() - t0:.1f}s", flush=True)
        return {"ok": False, "err": "等待节点回执超时"}
    with _PENDING_LK:
        slot = PENDING.pop(mid, {"slot": {}})["slot"]
    return {"ok": True, **slot}


def _session_cleanup(name):
    """临时节点（网吧/借用设备）下机：把这个节点的一切「能再进来」的东西清掉。

    为什么必须真干而不是打一行日志：临时节点的意义就是「下机不留痕」。
    留一枚凭据在账本里，等于把那台机器的钥匙挂在门上——机器换人用，
    下一个人照样能认证进来。清理项：
      ① 入网凭据（PSK）——撤了它才真的进不来；
      ② 卸载/配对令牌（内存）——同机会话内签发的令牌一并作废；
      ③ 该节点的离线队列——临时机不会「下次上线」，留着就是永久垃圾命令；
      ④ 写流水审计——清了几样要有据可查（E-7 可逐条删）。

    诚实边界：L3 里它写过的上下文**不清**（那是编队的记忆，属于她/用户，
    不属于那台机器）；节点自己沙箱目录里的文件也不清（不在本进程权限范围内，
    由节点的 --session 模式自己负责）。
    """
    out = {"psk": 0, "tokens": 0, "queued": 0}
    try:
        out["psk"] = enroll.revoke(ENROLL_LEDGER, name)
    except enroll.EnrollError as e:
        print(f"  [清理] 入网账本不可用，跳过 PSK 撤销：{e}", flush=True)
    with _TOKEN_LK:
        gone = [t for t, v in TOKENS.items() if v.get("node") == name]
        for t in gone:
            TOKENS.pop(t, None)
        out["tokens"] = len(gone)
    with _QUEUE_LK:
        q = OFFLINE_QUEUE.pop(name, [])
        if q:
            queue_store.save(OFFLINE_QUEUE, QUEUE_PATH)
        out["queued"] = len(q)
    # 清完留痕：以后要查「那台机器到底清没清」只看这一条
    try:
        _journal().log(
            "task", name,
            f"临时节点下机清理：凭据 {out['psk']} 枚 / 令牌 {out['tokens']} 个 / "
            f"丢弃排队命令 {out['queued']} 条")
    except Exception:
        pass
    return out


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
            out = _session_cleanup(name)
            print(f"  [策略:{rname}] 临时节点凭据已清："
                  f"凭据 {out['psk']} / 令牌 {out['tokens']} / 排队 {out['queued']}", flush=True)
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


# ---- 常驻自转：没人敲命令，它也自己看一眼 ----

SENSE_STOP = threading.Event()


def _sense_loop(interval=1800.0):
    """周期感知：没人敲命令，天网也自己看一眼身边。

    为什么必须常驻做：人是会动的——出门、回家、朋友来访、新设备拆箱，这些变化
    只有连续采样才看得见；手敲一次 nearby 只是抓了个瞬间，而单次快照的价值很低。
    变化写进对话流水，她才有「今天楼下多了三辆车」这种话可说。

    只写变化摘要，不写完整设备清单：清单留在本地指纹文件里（那是 E-7 意义上
    可逐条删的数据），流水里只留一句人话。
    """
    import perceive as pv
    while not SENSE_STOP.is_set():
        if SENSE_STOP.wait(interval):
            break
        try:
            out = pv.snapshot()
            if (out.get("trend") or {}).get("changed"):
                line = pv.look(out)
                print(f"[感知] {line}", flush=True)
                _journal().log("see", "commander", line, topic="nearby")
        except Exception as e:            # 感知挂了不许弄死常驻主服务
            print(f"[感知] 本轮失败（主服务照常）：{e}", flush=True)


def _sense_start(interval):
    SENSE_STOP.clear()
    t = threading.Thread(target=_sense_loop, args=(interval,), daemon=True)
    t.start()
    print(f"[感知] 周期感知已开：每 {int(interval)}s 看一眼身边（0 表示关）", flush=True)
    return t


def _sense_stop():
    SENSE_STOP.set()


# ---- 多控制面仲裁：发令闸门 ----

GATED_OPS = ("exec", "broadcast", "push", "pull", "compute", "computemap", "ui",
             "rules_put", "rules_rollback", "enroll", "enroll_revoke", "token",
             "intent", "cap批准", "discover", "sweep")


def _gate(op, role=None):
    """发令类命令的统一闸门：**先查分权，再查仲裁**。

    顺序不能反：没权限就不该发令，跟本机是不是领袖无关。分号即使占了领袖位，
    「exec」也该被拒；反过来，有权限但本机在待机，就得等领袖退位。
    两道闸的失败语义也不同——分权是**安全**（拒绝即拒绝，不看运气），
    仲裁是**协同**（状态文件坏了就放行，不能被一份坏文件把单机摁死）。

    角色来源：登录令牌（第 3 步接 CLI 后由令牌带入）；当前单主号在役，
    未显式传入时回落 `FLEET_ROLE`（默认 owner）。分号显式传 `role="sub"`。
    """
    role = role if role is not None else os.environ.get("FLEET_ROLE", "owner")
    if not accounts.can(role, op):
        print(f"[权限] {accounts.deny_reason(role, op)}", flush=True)
        return False
    try:
        st = arbiter.status(ARBITER_PATH)
    except Exception:
        return True
    leader = st.get("leader")
    if not leader or leader.get("id") == ARBITER_ID:
        return True
    print(f"[仲裁] 本机待机中：领袖是 {leader.get('id')}（rank {leader.get('rank')}），"
          f"「{op}」已拒绝。查看用 arbiter；领袖退出后本机自动接管。", flush=True)
    return False


def _arbiter_loop(interval=arbiter.CLAIM_INTERVAL):
    """领袖续期线程：活着就周期性声明；被杀则声明过期，备用按 TTL 接管。"""
    while True:
        try:
            arbiter.claim(ARBITER_PATH, ARBITER_ID, ARBITER_RANK)
        except Exception as e:
            print(f"[仲裁] 续期失败（不影响本机发令，仅影响待机判定）：{e}", flush=True)
        time.sleep(interval)


def _cmd_arbiter():
    st = arbiter.status(ARBITER_PATH)
    leader = st.get("leader")
    me = next((c for c in st.get("claims", []) if c.get("id") == ARBITER_ID), None)
    if leader:
        who = "本机（领袖）" if leader.get("id") == ARBITER_ID else f"{leader.get('id')}（本机待机）"
        print(f"  领袖: {leader.get('id')} rank={leader.get('rank')} -> {who}")
    else:
        print("  领袖: 无（下一轮声明后产生）")
    if me:
        print(f"  本机: id={ARBITER_ID} rank={me.get('rank')} since={me.get('since')}")
    else:
        print(f"  本机: id={ARBITER_ID} rank={ARBITER_RANK}（尚未声明）")
    for c in st.get("claims", []):
        if c is not leader:
            print(f"  在册: {c.get('id')} rank={c.get('rank')}")


def _enqueue(name, cmd):
    """离线排队（Phase 4）：同时落盘，控制面重启不丢。"""
    with _QUEUE_LK:
        OFFLINE_QUEUE.setdefault(name, []).append({"id": _next_id("q"), "cmd": cmd})
        queue_store.save(OFFLINE_QUEUE, QUEUE_PATH)
    # 排队就是一笔没闭环的账：节点上线补发后才消（_replay_queue 里 resolve）
    _journal().log("task", name, f"节点离线，已排队：{cmd}",
                   topic=_followups().queue_topic(name, cmd))


def _replay_queue(name, sock=None):
    """节点上线时把排队命令补发过去（透明上下线）。

    关键：**不能等回执**。重放在 `_handle_node` 的读循环启动之前执行，
    若在此同步等回执就会把读循环堵死（回执到了没人读），每次上线白等 30 秒，
    期间该节点所有回执全部丢失。重放是「补发」，回执由节点各自执行后异步产生。
    """
    with _QUEUE_LK:
        q = OFFLINE_QUEUE.pop(name, [])
        if q:
            queue_store.save(OFFLINE_QUEUE, QUEUE_PATH)
    if not q:
        return
    if sock is None:
        sock = REGISTRY.with_lock(lambda d: (d.get(name) or {}).get("sock"))
    if sock is None:
        # 节点已走：任务塞回队列，下次上线再补
        with _QUEUE_LK:
            OFFLINE_QUEUE.setdefault(name, []).extend(q)
            queue_store.save(OFFLINE_QUEUE, QUEUE_PATH)
        return
    try:
        common.send_msg(sock, {"t": common.QUEUE, "cmds": q})
    except OSError:
        with _QUEUE_LK:
            OFFLINE_QUEUE.setdefault(name, []).extend(q)
            queue_store.save(OFFLINE_QUEUE, QUEUE_PATH)
        return
    print(f"[*] 节点 {name} 上线，重放 {len(q)} 条离线命令", flush=True)
    # 补发成功 = 这笔账清了（闭环留痕，她不再追问）
    for it in q:
        _followups().resolve(_journal(), _her()[0],
                             _followups().queue_topic(name, it.get("cmd", "")))


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


def _auth_ok(name, pw, global_pw):
    """认证两路：①全局安装密码 ②扫码入网的节点凭据（绑死节点名）。

    凭据走 `enroll` 账本而不是内存里的 `TOKENS`——TOKENS 是「卸载令牌」，10 分钟过期；
    节点凭据必须扛得住重启，两者寿命不同，不能混用一份表。
    """
    if pw and global_pw and hmac.compare_digest(pw.encode("utf-8"), global_pw.encode("utf-8")):
        return True
    try:
        if enroll.check_psk(ENROLL_LEDGER, pw, name):
            enroll.mark_used(ENROLL_LEDGER, pw)
            return True
    except enroll.EnrollError as e:
        # 账本坏了就只认全局密码，不因读不到账本而放行任何凭据（fail-closed）
        print(f"[!] 入网凭据账本不可用，本轮仅接受全局密码：{e}", flush=True)
    return False


def _handle_node(conn, password):
    try:
        auth = common.recv_msg(conn)
        if not auth or auth.get("t") != common.AUTH:
            return
        if not _auth_ok(auth.get("name"), auth.get("password"), password):
            common.send_msg(conn, {"t": common.AUTH_FAIL, "reason": "密码错误或入网凭据无效"})
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
        # 记进「来过」的历史（凭据字段由 registry 白名单剔掉，密码不会落盘）
        REGISTRY.observe(name, reg, force=True)
        if DEBUG:
            try:
                print(f"[dbg] 注册 {name} fd={conn.fileno()}", flush=True)
            except OSError:
                pass
        _replay_queue(name, conn)  # 上线即补离线任务（不阻塞读循环）
        while True:
            m = common.recv_msg(conn)
            if m is None:
                break
            t = m.get("t")
            if t == common.HEARTBEAT:
                REGISTRY.with_lock(lambda d: d.get(name) and d[name].__setitem__("last_hb", time.time()))
                REGISTRY.observe(name, {})      # 只刷新 last_seen（内部节流写盘）
            elif t in (common.EXEC_RESULT, common.PUSH_RESULT, common.PULL_RESULT,
                       common.COMPUTE_RESULT, common.UI_SKIP_RESULT):
                if DEBUG:
                    print(f"[dbg] <- {name} {t} id={m.get('id')}", flush=True)
                _resolve_pending(m.get("id"), m)
            elif t == common.CAPABILITIES:
                REGISTRY.with_lock(lambda d: d.get(name) and d[name].__setitem__("capabilities", m.get("caps", [])))
                REGISTRY.observe(name, {"capabilities": m.get("caps", [])})
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
    if not _gate("cap批准"):
        # 待机控制面不批能力：两张皮授权比不授权更糟（主面不认，她会以为到手了）
        common.send_msg(conn, {"t": common.CAP_DENY, "cap": cap, "reason": "本机非领袖控制面"})
        _journal().log("her", name, f"想用 {cap}：{reason}（待机控制面，未批）",
                       topic=_followups().her_topic(cap))
        return
    try:
        ans = input("     approve> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "n"
    if ans == "y":
        common.send_msg(conn, {"t": common.CAP_GRANT, "cap": cap,
                                "session_min": 30, "token": os.urandom(6).hex()})
        print(f"      ✓ 已授权 {cap}（限 30min）", flush=True)
        _followups().resolve(_journal(), _her()[0], _followups().her_topic(cap))
    else:
        common.send_msg(conn, {"t": common.CAP_DENY, "cap": cap, "reason": "用户拒绝"})
        print(f"      ✗ 已拒绝；她的执念+1", flush=True)
        # 被拒 = 她欠一次「还想用」：留一笔 her 账，她过会儿会自己起话
        _journal().log("her", name, f"想用 {cap}：{reason}（被拒绝）",
                       topic=_followups().her_topic(cap))


def serve(host, port, password):
    global SERVE_HOST, SERVE_PORT
    SERVE_HOST, SERVE_PORT = host, port
    # 监听走路径层：host 给 0.0.0.0/空 时自动双栈（v4+v6），本机的公网 IPv6 才用得上。
    # 原来写死 AF_INET，等于把运营商给的公网 v6 白扔——跨网络只能借道第三方。
    srv, note = netpath.listen(host, port)
    show = host or "0.0.0.0"
    print(f"[*] Commander 监听 {show}:{port}（{note}，密码认证已启用）", flush=True)
    if note.startswith("双栈"):
        for r in netpath.addresses():
            if r["scope"] in ("public", "lan") and not r["temp"]:
                print(f"    可达：{r['addr']}（{netpath.SCOPE_CN[r['scope']]}）", flush=True)
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


def _cmd_seen(n=20):
    """谁来过：重启也不忘的设备账。

    在线看内存（socket 断了就是断了），来过看磁盘（历史）。两者刻意分开显示——
    把历史当在线，nodes 里就会站一排连不上的幽灵，策略层也会以为编队还在。
    """
    import perceive as pv
    rows = REGISTRY.seen()
    live = set(REGISTRY.with_lock(lambda d: list(d.keys())))
    if not rows:
        print("  （还没有任何节点来过；等一个节点注册后这里就有账了）")
        return rows
    print(f"  来过 {len(rows)} 台（历史落盘在 {NODES_PATH}）：")
    for r in rows[:max(int(n or 20), 1)]:
        mark = "在线" if r["name"] in live else "离线"
        caps = ",".join(r.get("capabilities") or []) or "-"
        age = time.time() - (r.get("last_seen") or 0)
        print(f"  {r['name']:12} {mark}  {(r.get('platform') or '?'):8} caps={caps:<24}"
              f" 最后见 {pv.ago(age)}前")
    return rows


def _cmd_exec(name, cmd):
    if not _gate("exec"):
        return
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
    if not _gate("push"):
        return
    try:
        data = open(localpath, "rb").read()
    except OSError as e:
        print(f"  读本地文件失败: {e}")
        return
    r = send_to_node(name, {"t": common.PUSH, "path": os.path.basename(localpath),
                            "data": base64.b64encode(data).decode()})
    print(f"  {'OK' if r.get('ok') else '失败: ' + str(r.get('err'))}")


def _cmd_pull(name, remotepath, outpath):
    if not _gate("pull"):
        return
    r = send_to_node(name, {"t": common.PULL, "path": remotepath})
    if not r.get("ok"):
        print(f"  失败: {r.get('err')}")
        return
    open(outpath, "wb").write(base64.b64decode(r["data"]))
    print(f"  已拉取 -> {outpath} ({len(r['data'])}B base64)")


def _cmd_broadcast(cmd):
    if not _gate("broadcast"):
        return
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
    if not _gate("token"):
        return
    tok = _issue_token(name, "uninstall")
    print(f"  一次性令牌（配对设备经手机下发）：{tok}  （10min 有效）")
    print(f"  卸载/停止时：install_windows.py --uninstall --name {name} --token {tok}")


def _advertise():
    """载荷里写「节点该往哪儿连」的地址。

    监听 0.0.0.0 时绝不能把 0.0.0.0 写进二维码——节点没法连。用 FLEET_ADVERTISE
    指定真实可达地址（如 Tailscale IP / 局域网 IP），没设则退回监听地址。
    """
    adv = os.environ.get("FLEET_ADVERTISE")
    if adv:
        h, _, p = adv.partition(":")
        return (h or SERVE_HOST), (int(p) if p.isdigit() else SERVE_PORT)
    return SERVE_HOST, SERVE_PORT


def _cmd_enroll(name, ttl=enroll.DEFAULT_TTL):
    """签发一张入网二维码载荷，并把节点凭据登记进账本。返回载荷文本（供自检复用）。"""
    if not _gate("enroll"):
        return None
    host, port = _advertise()
    payload = enroll.issue(name, platform="windows", host=host, port=port, ttl=ttl)
    uri = enroll.encode(payload)
    enroll.register_psk(ENROLL_LEDGER, payload["k"], name, exp=0)
    print(f"  入网载荷（{ttl}s 内有效、只能兑换一次）：")
    print(f"  {uri}")
    print(f"  {enroll.describe(payload)}")
    print(f"  目标机器上：install_windows.py --enroll \"{uri}\"")
    print("  手机侧把它渲成二维码即可；凭据长期有效（扛重启），换机/送人时用 enroll_revoke 收权")
    return uri


def _cmd_enroll_list():
    rows = enroll.list_psks(ENROLL_LEDGER)
    if not rows:
        print("（还没有扫码入网的节点）")
    for r in rows:
        used = time.strftime("%m-%d %H:%M", time.localtime(r["used_at"])) if r["used_at"] else "未使用"
        exp = time.strftime("%m-%d %H:%M", time.localtime(r["exp"])) if r["exp"] else "长期"
        print(f"  {r['node']:12} 凭据={r['key']}… 过期={exp} 首次使用={used}")


def _cmd_enroll_revoke(name):
    if not _gate("enroll_revoke"):
        return
    n = enroll.revoke(ENROLL_LEDGER, name)
    print(f"  已撤销 {name} 的 {n} 枚凭据（该节点下次重连即认证失败）")


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
    if not _gate("compute"):
        return
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
_RECALL = None
_FOLLOWUPS = None
JOURNAL = None
_LAST_TICK = [0.0]                       # 心跳节流（秒）
PERSONA_PATH = os.environ.get(
    "FLEET_PERSONA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona_state.json"))
JOURNAL_PATH = os.environ.get(
    "FLEET_JOURNAL", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet_journal.db"))


def _her():
    """懒初始化人格状态与主动性引擎（同一个「她」，不因设备不同而分裂）。"""
    global PERSONA, _PROACTIVE
    if PERSONA is None:
        import persona as p
        PERSONA = p.PersonaState(PERSONA_PATH)
        _PROACTIVE = p.ProactiveEngine(PERSONA)
    return PERSONA, _PROACTIVE


def _recall():
    """「她记得」：从 L3 唤醒往事（延迟导入，保持 CLI 冷启动轻）。"""
    global _RECALL
    if _RECALL is None:
        import recall as r
        _RECALL = r
    return _RECALL


def _followups():
    """「她会跟进」：从对话流水抽未闭环项（延迟导入）。"""
    global _FOLLOWUPS
    if _FOLLOWUPS is None:
        import followups as f
        _FOLLOWUPS = f
    return _FOLLOWUPS


def _journal():
    """对话流水（append-only，敏感信息打码）。没有它，「提取未完成任务」是无米之炊。"""
    global JOURNAL
    if JOURNAL is None:
        import journal as jmod
        JOURNAL = jmod.Journal(JOURNAL_PATH)
    return JOURNAL


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

    念头优先级：**没闭环的账 > 记得的往事 > 执念 > 打招呼**。前者是她「起话题」，
    中者是「接回忆」；两者都只引用流水/库里的原文，没接模型就用模板句，绝不编造。
    不说话不是失败：念头会记进 L3（pending_thought），她没消失，只是在忍。
    """
    st, eng = _her()
    now = time.time()
    if now - _LAST_TICK[0] < min_interval:
        return None
    _LAST_TICK[0] = now

    fu = _followups().pick(_journal(), st, now=now)
    rcall = _recall()
    mems = rcall.pick(_ctx(), st, now=now)
    obs = st.data.get("obsessions") or []
    if fu:
        text = _followups().template_line(fu, st.data.get("catchphrase", ""))
        hint = _followups().prompt_hint(fu) + "\n" + text
        imp = 0.75                                   # 欠账比寒暄重要，但仍过频控/静默
    elif mems:
        text = rcall.template_line(mems, st.data.get("catchphrase", ""))
        hint = rcall.prompt_hint(mems) + "\n" + text
        imp = 0.7                                    # 往事比寒暄重要，但仍过频控/静默
    elif obs:
        o = obs[-1]
        text = f"{st.data.get('catchphrase', '')} 我还在想 {o['cap']}（第{o['count']}次了）"
        hint = text
        imp = min(0.9, 0.4 + 0.1 * o["count"])
    else:
        text = f"{st.data.get('catchphrase', '')} 你在干嘛？"
        hint = text
        imp = 0.5

    ok, why = eng.allow(imp)
    if not ok:
        _ctx().put("her", "pending_thought", text)   # 被刹车 -> 记成心事，不打扰
        return None

    text = _her_compose(hint) or text                # 接上真模型就她来说，否则模板兜底
    print(f"[她] {text}", flush=True)
    _ctx().put("her", "last_thought", text)          # 跨设备：手机上也知道她刚说了啥
    _journal().log("her", "her", text)               # 她的话也进流水（追问的证据链上游）
    if fu:
        fu[0]["said"] = text                         # 记她实际说的话，不是模板
        _followups().mark_asked(st, _journal(), fu, now=now)   # 追问记账：24h 内不重复
    if mems:
        rcall.mark_recalled(st, _ctx(), mems, now=now)   # 说过就记账，N 天内不重复
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
    mems = _recall().candidates(_ctx())
    lr = _ctx().get("her", "last_recall")
    print(f"  记得的往事 {len(mems)} 件" + (f"（最近提过: {lr['val']}）" if lr.get("ok") else ""))
    pend = _followups().extract(_journal())
    print(f"  没闭环的账 {len(pend)} 笔（her_followups 看明细）")


def _cmd_her_followups():
    """列出 24h 内没闭环的事——「她该起的话题」清单，人可核对。"""
    items = _followups().extract(_journal())
    if not items:
        print("  （没有未闭环的事：流水里 24h 窗口内没有开着的账）")
        return
    for it in items:
        print(f"  [{it['when']}|{it['kind']}|x{it['count']}] {it['topic']}")
        print(f"      流水 #{it['evidence_id']}：{it['body']}")
    print("  用法: done <topic> 标记这件事已完成（清了账，她就不再追问）")


def _cmd_done(topic):
    """人工闭环：有些事是在系统外做完的（你手动备份完了），得有个口子告诉系统。"""
    if not topic:
        print("  用法: done <topic>（topic 见 her_followups）")
        return
    n = _followups().resolve(_journal(), _her()[0], topic)
    if n:
        print(f"  已闭环「{topic}」（清掉 {n} 条开着的记录）")
    else:
        print(f"  「{topic}」没有开着的记录")


def _cmd_journal(n=20):
    """回看最近的对话流水（她起话题的依据都在这里）。"""
    rows = _journal().recent(limit=n)
    if not rows:
        print("  （流水还是空的）")
        return
    for r in rows:
        mark = "○" if r["status"] == "open" else "●"
        t = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
        topic = f" @{r['topic']}" if r["topic"] else ""
        print(f"  {mark} #{r['id']:<4} {t} {r['kind']:4} {r['actor']}{topic}: {r['body']}")


def _cmd_her_memory():
    """把她能回忆起来的往事列出来——人可据此核对「她记得」是不是真的。"""
    st, _ = _her()
    rows = _recall().candidates(_ctx(), k=10)
    if not rows:
        print("  （暂无往事：L3 里没有 12h–72h 的条目）")
    for it in rows:
        flag = "已提过" if _recall().recalled_key(it) in (st.data.get("recalled") or {}) else "未提"
        print(f"  [{it['when']}|{flag}] {it['ns']}/{it['key']} = {it['val']}")
    print("  说明：只取 12h–72h 的条目，跳过她自己的元状态与技术缓存；说过的 3 天内不再提。")


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
    _journal().log("her", "her", said)


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
    if not _gate("computemap"):
        return
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
    if not _gate("rules_put"):
        return
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
    if not _gate("rules_rollback"):
        return
    rl = _rules()
    try:
        v = rl.rollback(version, path=RULES_PATH)
        print(f"  已回滚到 v{version} 的内容，生成新版本 v{v}（历史保留）")
    except Exception as e:
        print(f"  回滚失败: {e}")


def _cmd_ui(node, package, use_vision=False, rounds=6):
    """UI 自动化：让节点在挂载的 Android 设备上按规则跳过广告（源自李跳跳拆解）。"""
    if not _gate("ui"):
        return
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


def _cmd_sweep(save=True):
    """扩散扫描：把「ARP 表里有的」扩成「整个网段在场的」。

    与 discover 的分工（两个不是重复的）：
      discover = 在**已经知道 IP** 的设备上做精细指纹（组播/型号/banner）
      sweep    = 先把**整个网段**的人找出来（ICMP 全扫 + NBT 名字 + 多网段）
    实际用的时候先 sweep 拿到底数，再 discover 补型号。
    铁律：只探测、不入网；不发应用层指令、不试口令。
    """
    if not _gate("sweep"):
        return None
    import sweep as sw
    print("  扩散扫描中（ICMP 全段 + TCP + NBT + mDNS + SSDP）…", flush=True)
    r = sw.sweep(verbose=False)
    print(sw.format_table(r))
    print()
    for line in r.get("profile") or []:
        print("  ▪", line)
    for n in r.get("notes", []):
        print("  ·", n)
    w = r.get("wifi") or {}
    if w.get("networks"):
        print(f"  周边 WiFi {len(w['networks'])} 个")
    elif w.get("note"):
        print("  ·", w["note"])
    if save:
        try:
            with open(SWEEP_PATH, "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False, indent=2)
            print(f"  已记入扫描账本 {SWEEP_PATH}（{r.get('count', 0)} 台）")
        except OSError as e:
            print(f"  账本写入失败: {e}")
    return r


def _cmd_discover(save=True, do_ports=True):
    """主动发现局域网设备：自己不报名也要把它们找出来。

    三条路：组播询问（SSDP/mDNS，设备自报型号最准）+ 邻居表/OUI（零打扰）
    + 特征端口组合（海康 9000+554+23+8080 这类招牌组合）。
    铁律：**只探测、不入网**。发现结果进账本，要变成节点仍需在设备上装 agent 并入网。
    """
    if not _gate("discover"):
        return None
    import discovery as disc
    print("  扫描中（组播 + 邻居表 + 端口指纹）…", flush=True)
    r = disc.discover(do_ports=do_ports)
    print(disc.format_table(r))
    if save:
        try:
            with open(DISCOVERY_PATH, "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False, indent=2)
            print(f"  已记入发现账本 {DISCOVERY_PATH}（{r.get('count', 0)} 台）")
        except OSError as e:
            print(f"  账本写入失败: {e}")
    print("  发现 != 入网：这些设备只是「看见了」，要纳入编队得在它上面装 agent 再 enroll。")
    return r


def _show_action(r, act=""):
    """动作回执：只读的铺开细节，动手的给一句人话。"""
    if not r or not r.get("ok"):
        err = (r or {}).get("err") or "失败"
        print(f"  ✗ {err}")
        see = (r or {}).get("see")
        if see:
            print("    界面上有：" + "、".join(str(x) for x in see[:12]))
        return
    if act in ("screen", "see", "look"):
        print(f"  前台 {r.get('foreground') or '?'}")
        labels = r.get("labels") or []
        print(f"  可见文字 {len(labels)} 条：")
        for x in labels[:24]:
            print("    ·", x)
        if len(labels) > 24:
            print(f"    …还有 {len(labels) - 24} 条（phone tree 看全）")
    elif act == "tree":
        print(f"  控件 {r.get('count', 0)} 个：")
        print(r.get("text") or "")
    elif act == "apps":
        apps = r.get("apps") or []
        print(f"  第三方应用 {len(apps)} 个：" + "、".join(apps[:20])
              + ("…" if len(apps) > 20 else ""))
    elif act == "fg":
        print(f"  前台 {r.get('foreground') or '?'}")
    elif act == "notify":
        items = r.get("items") or []
        print(f"  通知 {len(items)} 条（只读，没点开）：")
        for x in items:
            print(f"    · {x.get('pkg')}: {x.get('title')}")
        if not items:
            print("    （没读到——可能确实没有，也可能系统限制了 dumpsys）")
    elif act == "tap":
        how = "（只算落点，没点）" if r.get("dry_run") else (
            "界面变了" if r.get("changed") else "界面没变，可能点到死区")
        print(f"  ✓ 点了「{r.get('hit') or r.get('hit') or ''}」{how}  ({r.get('x')},{r.get('y')})")
    else:
        body = {k: v for k, v in r.items() if k not in ("pattern", "nodes", "text")}
        print("  ✓ " + json.dumps(body, ensure_ascii=False))


def _cmd_nearby(only=""):
    """身边全景（三层一起看）：IP 层 + 无线层 + 蓝牙层。

    为什么不是「又一个扫描命令」：sweep 只看得见跟本机说过话的 IP，discover 只在已知
    IP 上补型号，手机天线补的是电脑根本看不见的那半边（电脑射频被自己的热点占了）。
    三层拼起来才是「此刻身边有什么」。而单次扫描的信息量很低——真正有用的是**变化**：
    谁第一次出现、谁连续几次不见了。所以每次都比对指纹并发一句话。
    """
    if not _gate("nearby"):
        return None
    import perceive as pv
    only = (only or "").strip().lower()
    if only and only not in pv.LAYERS:
        print(f"  未知层 {only}（lan / wifi / bt）")
        return None
    pick = only in pv.LAYERS
    print("  三层扫描中（IP 全段 + 手机 WiFi 天线 + 手机蓝牙天线）…", flush=True)
    out = pv.snapshot(with_lan=(not pick) or only == "lan",
                      with_wifi=(not pick) or only == "wifi",
                      with_bt=(not pick) or only == "bt")
    print(pv.report(out))
    print()
    line = pv.look(out)
    print("  一句话：" + line)
    if out.get("hist_file"):
        print(f"  指纹已留 {out['hist_file']}（下次自动比变化）")
    if (out.get("trend") or {}).get("changed"):
        # 变化进流水：她以后才能说出「你回来的时候楼下多了三辆车」这种话
        _journal().log("see", "commander", line, topic="nearby")
    return out


def _cmd_caps():
    """能力矩阵：现在能感知什么、能动手什么，不能的为什么不能。"""
    import perceive as pv
    print("  能力自检（真连一次手机，约几秒）…", flush=True)
    rows = pv.caps(probe=True)
    print(pv.format_caps(rows))
    return rows


def _cmd_phone(act, arg="", arg2="", dry_run=False):
    """让手机看/动手。只读动作直接放行，会改状态的动作（SIDE_EFFECT）过闸。"""
    if not act:
        print("  用法：phone screen|tree|apps|fg | open <包名> | tap <屏幕上的字> [--dry-run]"
              " | type <文本> | back | home | shot [路径] | wait <字> [秒]")
        return None
    import perceive as pv
    act = act.strip().lower()
    if act in pv.SIDE_EFFECT and not _gate("phone"):
        return None
    r = pv.phone(act, arg, arg2, dry_run=dry_run)
    _show_action(r, act)
    return r


def _cmd_ir(spec, send=False):
    """红外。默认只算序列不发——真发射会把电器打开或关掉，得人明确加 --send。"""
    import perceive as pv
    if send and not _gate("ir"):
        return None
    r = pv.ir(spec, send=send)
    if not r.get("ok"):
        print(f"  ✗ {r.get('err')}")
        return r
    if spec.strip() in ("", "info"):
        print("  " + (r.get("out") or "（没回执）"))
        return r
    print(f"  协议 {r.get('proto')} 载波 {r.get('freq')}Hz 共 {r.get('segments')} 段")
    if r.get("sent"):
        print(f"  ✓ 已发射：{r.get('out') or ''}")
    else:
        print("  （未发射，加 --send 才真发）")
    return r


def _cmd_phone_line(rest):
    """把一行 REPL 拆成「动作 + 参数」。

    除 wait 外整段余文都算一个参数——`type 12 * 34` 这种带空格的输入不能被拆散；
    wait 的第二个值才是超时秒数（`wait 结果 15`）。
    """
    rest = (rest or "").strip()
    dry = "--dry-run" in rest.split()
    if dry:
        i = rest.find("--dry-run")
        rest = (rest[:i] + rest[i + len("--dry-run"):]).strip()
    sp = rest.split(" ", 1)
    act = (sp[0] if sp else "").strip().lower()
    tail = sp[1] if len(sp) > 1 else ""
    arg, arg2 = tail, ""
    if act == "wait" and tail:
        head, _, last = tail.rpartition(" ")
        if head and last.strip().isdigit():
            arg, arg2 = head, last.strip()
    return _cmd_phone(act, arg, arg2, dry_run=dry)


def _cmd_net(port=None):
    """本机能从哪些地址被连到，节点该往哪连。

    为什么单独一条命令：跨网络直连的前提是「知道该给节点填哪个 host」。这台机器
    同时有内网 v4、公网 v6、热点网关和一堆 fe80，凭印象填十有八九是错的（填成
    fe80 就只能同链路、填成临时地址会随隐私扩展轮换而失效）。
    """
    import netpath as np
    rows = np.addresses()
    print("【本机地址】按「外人能不能连进来」排序：")
    print(np.fmt(rows))
    print()
    print(f"【连法建议】控制面端口 {port or SERVE_PORT}")
    for line in np.hint(port or SERVE_PORT):
        print(line)
    return rows


def _cmd_intent(text):
    """统一意图编排（L2 本命位）：一句话 -> 多步计划 -> 带依赖执行 -> 失败逆序回滚。

    与 broadcast 的区别：扇出是「一条命令发多台」，编排是「一个意图拆多步、步间有依赖，
    某步失败就把已完成的步按逆序补偿」——这才叫一个系统在动。
    """
    if not _gate("intent"):
        return
    import orchestrator as orch
    nodes = REGISTRY.with_lock(lambda d: list(d.keys()))
    topic = _followups().task_topic(text)
    plan = orch.parse_intent(text, nodes=nodes, pick=lambda cap: pick_node(cap, task=text))
    if not plan:
        print("  没解析出计划（试试：巡检 / 备份 / 对 PC-A 执行 hostname）")
        # 解析不出计划也是一笔账：她回头可以问「要不要换个说法」
        _journal().log("task", "commander", f"意图没解析出计划：{text}", topic=topic)
        return
    print(f"  计划 {len(plan)} 步：")
    for s in plan:
        dep = f"  after={s['after']}" if s.get("after") else ""
        print(f"    {s['id']:10} {s['desc']:18} node={s.get('node')}{dep}")

    def _exec(node, cmd):
        return send_to_node(node, {"t": common.EXEC, "cmd": cmd})

    def _log(kind, step, r):
        mark = {"ok": "✓", "fail": "✗", "rollback": "↩"}.get(kind, "·")
        body = (r.get("out") or r.get("err") or "").strip().replace("\n", " ")[:60]
        print(f"    {mark} {step['id']}: {body}  [rc={r.get('rc')} ok={r.get('ok')}]")

    res = orch.run_plan(plan, _exec, _log)
    state = "成功" if res["ok"] else f"失败于 {res.get('failed')}"
    print(f"  结果: {state}；已完成 {res.get('done')}；回滚 {res.get('rolled_back')}")
    if res["ok"]:
        _followups().resolve(_journal(), _her()[0], topic)      # 成了 -> 账清了
    else:
        # 失败要留一笔账：她 24h 内可能起话问「还作数吗」
        _journal().log("task", "commander",
                       f"意图失败于 {res.get('failed')}，已完成 {res.get('done')}：{text}",
                       topic=topic)


def repl():
    print("cmd> nodes | exec <节点> <命令> | broadcast <命令> | push <节点> <文件> | pull <节点> <远程> [输出]")
    print("      | ctx_put <ns> <key> <值> | ctx_get <ns> <key> | ctx_search <ns> <查询> | ctx_del <ns> <key>")
    print("      | token <节点> | queue <节点> | compute <提示词> | computemap <p1;p2;...> | computepool")
    print("      | enroll <节点> [有效期秒] | enroll_list | enroll_revoke <节点>   # 扫码入网（离线验签）")
    print("      | ui <节点> <包名|*> [--vision] | rules | rules_get [版本] | rules_put <文件> [备注] | rules_rollback <版本>")
    print("      | policy | policy_eval <事件JSON> | her | her_memory | her_followups | her_say <文本> | intent <一句话> | quit")
    print("      | discover [--no-ports]  # 自己扫局域网、自己认设备（只探测不入网）")
    print("      | sweep                 # 扩散扫描：全段 ICMP + 多网段 + NBT 名字 + 环境画像")
    print("      | nearby [lan|wifi|bt]   # 身边全景：IP+无线+蓝牙三层，自动比上次谁进谁走")
    print("      | caps                   # 能力矩阵：能感知/能动手什么，不能的为什么")
    print("      | phone screen|tree|apps|open <包名>|tap <字>|type <文本>|back|home|shot|wait <字> [秒]")
    print("      | ir info | ir nec:0x04:0x08 [--send]   # 红外，默认只算序列不发射")
    print("      | net [端口]              # 本机能从哪些地址被连到（公网 v6 优先）+ 连法建议")
    print("      | seen [n]                # 谁来过：控制面重启也不忘的设备账（历史绝不冒充在线）")
    print("      | done <topic>   # 告诉系统这件事做完了（清账，她不再追问）")
    print("      | journal [n]    # 回看对话流水（她起话题的依据）")
    print("      | arbiter        # 多控制面仲裁：谁是领袖、本机是领袖还是待机")
    while True:
        try:
            line = input("cmd> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        # 你说的话也进流水：她起话题时要能引用「你昨天说过什么」
        _journal().log("user", "user", line)
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
        elif op == "enroll":
            sp = rest.split()
            if sp:
                ttl = int(sp[1]) if len(sp) > 1 and sp[1].isdigit() else enroll.DEFAULT_TTL
                _cmd_enroll(sp[0], ttl)
        elif op == "enroll_list":
            _cmd_enroll_list()
        elif op == "enroll_revoke":
            if rest:
                _cmd_enroll_revoke(rest.strip())
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
        elif op == "sweep":
            _cmd_sweep()
        elif op == "discover":
            _cmd_discover(do_ports="--no-ports" not in rest.split())
        elif op == "nearby":
            _cmd_nearby(rest.strip())
        elif op == "caps":
            _cmd_caps()
        elif op == "phone":
            _cmd_phone_line(rest)
        elif op == "ir":
            _cmd_ir(rest.replace("--send", "").strip(),
                    send="--send" in rest.split())
        elif op == "net":
            _cmd_net(int(rest) if rest.strip().isdigit() else None)
        elif op == "seen":
            _cmd_seen(int(rest) if rest.strip().isdigit() else 20)
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
        elif op == "her_memory":
            _cmd_her_memory()
        elif op == "her_followups":
            _cmd_her_followups()
        elif op == "done":
            _cmd_done(rest.strip())
        elif op == "journal":
            _cmd_journal(int(rest) if rest.strip().isdigit() else 20)
        elif op == "her_say":
            if rest:
                _cmd_her_say(rest)
        elif op == "arbiter":
            _cmd_arbiter()
        elif op == "intent":
            if rest:
                _cmd_intent(rest)
        else:
            print("  未知命令")


def self_test(host, port, password):
    """本地回路自检：起服务 + 多节点，证明身体 Phase 0–4 + 她授权闭环 + 反向推送。"""
    global SELF_TEST, RULES_PATH, QUEUE_PATH, PERSONA_PATH, ENROLL_LEDGER, JOURNAL_PATH
    global ARBITER_PATH, SWEEP_PATH, DISCOVERY_PATH, NODES_PATH, REGISTRY
    SELF_TEST = True
    os.environ["FLEET_CTX"] = ":memory:"
    # 自检不碰 ~/.fleet 下的真实密钥：给本进程一份固定测试密钥
    os.environ["FLEET_ENROLL_SECRET"] = "commander-selftest-secret"
    import tempfile
    # 自检用临时文件，绝不污染仓库里的规则库/队列/人格态/入网账本/对话流水
    tdir = tempfile.gettempdir()
    RULES_PATH = os.path.join(tdir, f"fleet_rules_st_{os.getpid()}.json")
    QUEUE_PATH = os.path.join(tdir, f"fleet_queue_st_{os.getpid()}.json")
    PERSONA_PATH = os.path.join(tdir, f"fleet_persona_st_{os.getpid()}.json")
    ENROLL_LEDGER = os.path.join(tdir, f"fleet_enroll_st_{os.getpid()}.json")
    JOURNAL_PATH = os.path.join(tdir, f"fleet_journal_st_{os.getpid()}.db")
    # 仲裁状态也走临时文件：自检里要模拟「另一台更高 rank 的控制面」
    ARBITER_PATH = os.path.join(tdir, f"fleet_arbiter_st_{os.getpid()}.json")
    # 扫描/发现账本同样不能落到仓库里（一次自检就能写出一份真设备清单）
    SWEEP_PATH = os.path.join(tdir, f"fleet_sweep_st_{os.getpid()}.json")
    DISCOVERY_PATH = os.path.join(tdir, f"fleet_discovered_st_{os.getpid()}.json")
    # 节点表也走临时文件：一次自检不该往 ~/.fleet 留下一堆假节点历史
    NODES_PATH = os.path.join(tdir, f"fleet_nodes_st_{os.getpid()}.json")
    REGISTRY = registry.NodeTable(NODES_PATH, interval=0)
    for _p in (RULES_PATH, QUEUE_PATH, PERSONA_PATH, ENROLL_LEDGER, JOURNAL_PATH,
               ARBITER_PATH, SWEEP_PATH, DISCOVERY_PATH, NODES_PATH):
        if os.path.exists(_p):
            os.remove(_p)
    import node_agent
    import bootstrap as boot
    if DEBUG:
        # 调试用：周期 dump 所有线程栈，定位「帧已写出但节点没收到」这类卡死
        import faulthandler
        _fh = open(os.path.join(tdir, f"fleet_stacks_{os.getpid()}.txt"), "a", encoding="utf-8")
        faulthandler.dump_traceback_later(45, repeat=True, file=_fh)
        print(f"[dbg] 线程栈会写到 {_fh.name}", flush=True)
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

    print("\n=== 多控制面仲裁（一个意图绝不割裂）===", flush=True)
    # 单控制面常态：claim 即成领袖，零配置
    r = arbiter.claim(ARBITER_PATH, ARBITER_ID, ARBITER_RANK)
    assert r["is_leader"], "单控制面应当即领袖"
    print(f"  本机 {ARBITER_ID} rank={ARBITER_RANK} -> 领袖（单控制面零配置）")
    _cmd_exec("PC-A", "echo 领袖发的令")
    # 模拟第二台控制面（rank 更高）：本机应变待机，发令被拒
    phantom = f"{ARBITER_ID}-phantom"
    arbiter.claim(ARBITER_PATH, phantom, rank=999)
    _cmd_exec("PC-A", "echo 待机面不该发出这条")
    _cmd_arbiter()
    # 幽灵退出（释放让位）：本机立即恢复发令，不等 TTL
    arbiter.release(ARBITER_PATH, phantom)
    _cmd_exec("PC-A", "echo 接管后恢复发令")

    print("\n=== 分权（主号可发令，分号只能被控）===", flush=True)
    # 闸门顺序：先分权后仲裁。分号即使本机是领袖也必须被拒。
    a_sub = _gate("exec", role="sub")
    a_owner = _gate("exec", role="owner")
    a_ghost = _gate("exec", role="ghost")
    print(f"  分号发 exec -> {a_sub}（应 False）")
    print(f"  主号发 exec -> {a_owner}（应 True）")
    print(f"  未知角色   -> {a_ghost}（应 False，fail-closed）")
    assert a_sub is False and a_owner is True and a_ghost is False
    # 权限矩阵必须覆盖闸门清单：漏一个动作就等于给自己留了一道后门
    leaked = [o for o in GATED_OPS if accounts.can("sub", o)]
    print(f"  分号还能触碰的闸门动作: {leaked}（应为空）")
    assert not leaked, f"权限矩阵漏了这些动作：{leaked}"
    print(f"  分号可见范围: {accounts.nodes_of.__name__} 只返回自己名下节点"
          f"（主号返回 None = 全部）")

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
    # 预置「能再进来」的东西：一枚入网凭据 + 一条排队命令 + 一枚卸载令牌。
    # 不预置就分不清「清理跑过了」和「本来就没东西可清」。
    _psk = "psk-" + os.urandom(6).hex()
    enroll.register_psk(ENROLL_LEDGER, _psk, "PC-Net")
    _enqueue("PC-Net", "echo 网吧那台不会真的回来了")
    _net_tok = _issue_token("PC-Net", "uninstall")
    print(f"  预置：凭据 1 枚 / 排队 1 条 / 令牌 1 个 -> 下机应全清")
    sk = REGISTRY.with_lock(lambda d: (d.get("PC-Net") or {}).get("sock"))
    if sk:
        try:
            sk.close()                 # 模拟下机（拔线/关浏览器）
        except OSError:
            pass
    time.sleep(0.9)
    print(f"  下机后在线: {bool(REGISTRY.with_lock(lambda d: d.get('PC-Net')))}"
          f"（应为 False；session_end 策略已触发清理）")
    left_psk = [r for r in enroll.list_psks(ENROLL_LEDGER) if r["node"] == "PC-Net"]
    with _QUEUE_LK:
        left_q = len(OFFLINE_QUEUE.get("PC-Net", []))
    with _TOKEN_LK:
        left_tok = sum(1 for v in TOKENS.values() if v.get("node") == "PC-Net")
    print(f"  清理结果：凭据余 {len(left_psk)} / 排队余 {left_q} / 令牌余 {left_tok}"
          f"（应全 0）")
    assert not left_psk and not left_q and not left_tok, (left_psk, left_q, left_tok)
    _audit = [r for r in _journal().recent(hours=1, kinds=("task",))
              if "临时节点下机清理" in (r.get("body") or "")]
    print(f"  审计留痕：流水里有 {len(_audit)} 条清理记录（E-7 可逐条删）")
    assert _audit, "清理必须留痕，否则事后无法核对到底清没清"

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

    print("\n=== 扫码入网（一次性载荷 / 离线验签 / 凭据绑节点）===", flush=True)
    uri = _cmd_enroll("PC-QR", ttl=600)
    pay = enroll.verify(uri)                     # 离线验签：不联网、不回调
    print(f"  离线验签通过: 节点={pay['n']} 中心={pay['h']}:{pay['o']} 载荷{len(uri)}字符", flush=True)

    # 改一个字节 -> 签名失配（不信任载荷自述的任何字段）
    _bs = uri.index("p=") + 2
    _bad = uri[:_bs] + ("B" if uri[_bs] != "B" else "C") + uri[_bs + 1:]
    try:
        enroll.verify(_bad)
        raise AssertionError("篡改过的载荷竟然验过了")
    except enroll.EnrollError as _e:
        print(f"  篡改一个字符 -> {_e.reason}（应为 bad_sig）", flush=True)

    # 认证两路：凭据能上、凭据不能跨节点、乱猜的不行
    def _try_auth(nm, pw):
        _s = socket.create_connection((host, port), timeout=5)
        try:
            common.send_msg(_s, {"t": common.AUTH, "name": nm, "password": pw})
            _r = common.recv_msg(_s)
            return (_r or {}).get("t")
        finally:
            _s.close()

    psk = pay["k"]
    print(f"  凭据认证 PC-QR -> {_try_auth('PC-QR', psk)}（应 auth_ok）")
    print(f"  同凭据冒充 PC-Fake -> {_try_auth('PC-Fake', psk)}（应 auth_fail：凭据绑节点名）")
    print(f"  乱猜凭据 -> {_try_auth('PC-QR', '00' * 16)}（应 auth_fail）")

    # 真节点持凭据注册，能接命令（不是只过握手）
    node_agent.run_node(host, port, "PC-QR", psk, "windows", caps=["exec"],
                        sandbox="sandbox_qr", bg=True)
    for _ in range(30):
        if REGISTRY.with_lock(lambda d: "PC-QR" in d):
            break
        time.sleep(0.1)
    _cmd_exec("PC-QR", "echo 扫码入网节点已受控")
    _cmd_enroll_list()
    _cmd_enroll_revoke("PC-QR")
    print(f"  撤销后 PC-QR 重连 -> {_try_auth('PC-QR', psk)}（应 auth_fail：收权即断）")

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

    print("\n=== 她记得（隔日 + 换设备，能主动带出昨天的具体事）===", flush=True)
    # 隔离变量：前面的离线队列演示欠下了账（那是下面一段的测试对象），
    # 先闭环掉，否则这段测的就是「未完成项」而不是「回忆」。
    for _t in [r["topic"] for r in _journal().open_topics(now=time.time())]:
        _followups().resolve(_journal(), _her()[0], _t)
    rcall = _recall()
    _now = time.time()
    # 造一条「昨天」的往事 + 一条「刚发生」的噪声，证明只回忆隔日的、不当场复读
    _ctx().put("her", "game_lastnight", "昨晚那局还没玩完，说到第3关", ts=_now - 26 * 3600)
    _ctx().put("her", "just_minutes", "刚喝了口水", ts=_now - 300)
    _cmd_her_memory()
    _mems = rcall.pick(_ctx(), _her()[0], now=_now)
    print(f"  可提往事 {len(_mems)} 件（应 1 件：刚发生的不算回忆）", flush=True)
    assert [m["key"] for m in _mems] == ["game_lastnight"], _mems
    _PROACTIVE.sent_times = []            # 仅为演示重置额度：频控本身已在上一段验证过
    _said = _persona_tick(min_interval=0)
    print(f"  她带出的往事 = {_said!r}（模板只引用库里原文，没模型也不编）", flush=True)
    assert _said and "昨晚那局还没玩完" in _said, _said
    _again = rcall.pick(_ctx(), _her()[0], now=time.time())
    print(f"  立刻再说同一件 = {bool(_again)}（应为 False：3 天内不重复）", flush=True)
    assert "game_lastnight" not in [m["key"] for m in _again], _again
    print(f"  跨设备可见 last_recall = {(_ctx().get('her', 'last_recall') or {}).get('val')!r}",
          flush=True)

    print("\n=== 她会跟进没完成的事（24h 对话流水 -> 主动起话头）===", flush=True)
    # 一笔两小时前失败的意图（同一 topic 试了两次，应合并成一件）
    _topic = _followups().task_topic("备份")
    _journal().log("task", "PC-A", "意图『备份』失败：type 不存在的文件", topic=_topic,
                   ts=time.time() - 2 * 3600)
    _journal().log("task", "PC-A", "意图『备份』又失败：磁盘满", topic=_topic,
                   ts=time.time() - 1 * 3600)
    _cmd_her_followups()
    _items = _followups().pick(_journal(), _her()[0])
    print(f"  可起的账 {len(_items)} 件（同一 topic 两次失败应合并成 1 件）", flush=True)
    assert len(_items) == 1 and _items[0]["count"] == 2, _items
    _PROACTIVE.sent_times = []            # 同上：本段只验证「起话」，频控已在别段验证
    _said2 = _persona_tick(min_interval=0)
    print(f"  她起的话 = {_said2!r}", flush=True)
    assert _said2 and "备份" in _said2, _said2
    _reask = _followups().pick(_journal(), _her()[0])
    print(f"  立刻再起同一笔 = {bool(_reask)}（应为 False：24h 内不重复追问）", flush=True)
    assert not _reask, _reask
    _cmd_done(_topic)                     # 人告诉系统这事做完了 -> 账清了
    _left = _followups().extract(_journal())
    print(f"  闭环后剩余未完成项 = {len(_left)}（这笔不再出现）", flush=True)
    assert all(i["topic"] != _topic for i in _left), _left
    _cmd_journal(6)

    print("\n=== 共享算力路由（§九 dev_pick：选「谁能干」的节点跑重活）===", flush=True)
    _cmd_compute("帮我写段按修改时间排序的 Python")     # 普通任务：策略不要求占 GPU
    _cmd_compute("帮我微调一个 LoRA 训练脚本")          # 重活：策略自动转 prefer_gpu
    print("\n=== 共享算力池：批量分发 + 跨设备结果缓存 ===", flush=True)
    _cmd_computepool()
    _cmd_computemap("解释什么是注意力机制;用一句话总结Transformer;给个快速排序的Python", prefer_gpu=True)

    print("\n=== 统一意图编排（一句话 -> 多步带依赖 -> 失败逆序回滚）===", flush=True)
    _cmd_intent("巡检")
    import orchestrator as _orch
    bad = [{"id": "a", "desc": "先做一步", "cap": "exec", "node": "PC-A",
            "args": {"cmd": "echo step-a"},
            "rollback": {"cap": "exec", "node": "PC-A", "args": {"cmd": "echo 已回滚 a"}}},
           {"id": "b", "desc": "故意失败", "cap": "exec", "node": "PC-A",
            "args": {"cmd": "type 不存在的文件_xyz.txt"}, "after": ["a"]}]
    res = _orch.run_plan(bad, lambda n, c: send_to_node(n, {"t": common.EXEC, "cmd": c}),
                         lambda k, s, r: print(
                             f"    [{k}] {s['id']} rc={r.get('rc')} ok={r.get('ok')} "
                             f"err={(r.get('err') or '')[:40]!r}", flush=True))
    print(f"  故意失败计划 -> failed={res['failed']} 回滚={res['rolled_back']}"
          f"（回滚非空=补偿到位）", flush=True)

    print("\n=== 自检通过：L0/L1/L2/L3 + 她授权 + 反向推送 + 共享算力 均工作 ===", flush=True)
    os._exit(0)


def main():
    global CTX
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--password", default=os.environ.get("FLEET_PASSWORD", "changeme"))
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--daemon", action="store_true",
                    help="常驻模式：只跑服务/看门狗/仲裁/周期感知，不要交互 REPL")
    ap.add_argument("--sense", type=int, default=1800,
                    help="常驻时周期感知的间隔秒（0=关；默认 1800）")
    args = ap.parse_args()
    if args.self_test:
        self_test(args.host, args.port, args.password)
    elif args.daemon:
        _restore_queue()
        threading.Thread(target=serve, args=(args.host, args.port, args.password),
                         daemon=True).start()
        start_watchdog()          # 看门狗同时驱动策略与她的人格心跳
        _arbiter_start()
        if args.sense > 0:
            _sense_start(args.sense)
        print("[*] 常驻模式：没有 REPL。看日志 / 用另一台机器的控制面连过来。", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\n[*] 收到中断，让位退出", flush=True)
        finally:
            _sense_stop()
            _arbiter_stop()
    else:
        _restore_queue()
        threading.Thread(target=serve, args=(args.host, args.port, args.password), daemon=True).start()
        start_watchdog()          # 心跳看门狗：卡住/假死的节点会被自动探测/摘除
        _arbiter_start()          # 多控制面仲裁：本机开始竞选领袖（单机即领袖）
        repl()
        _arbiter_stop()           # 正常退出：主动让位，备用控制面立即接管（不等 TTL）


def _arbiter_start():
    try:
        arbiter.claim(ARBITER_PATH, ARBITER_ID, ARBITER_RANK)
    except Exception as e:
        print(f"[仲裁] 首次声明失败（不影响本机使用）：{e}", flush=True)
    t = threading.Thread(target=_arbiter_loop, daemon=True)
    t.start()
    return t


def _arbiter_stop():
    try:
        arbiter.release(ARBITER_PATH, ARBITER_ID)
        print(f"[仲裁] 已让位（{ARBITER_ID}），备用控制面可立即接管", flush=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()

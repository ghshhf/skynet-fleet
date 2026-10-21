#!/usr/bin/env python
"""t11：节点生命周期门禁（2026-10-21 真事故的离线复现）。

**这个门禁是为了钉死一个真实安全缺陷**（不是功能测试）：

  ① 清理被整段跳过（安全缺陷）
     `commander._handle_node` 的 finally 用 `v["sock"] is conn` 找自己的节点名。
     而 `node_agent.run_node` 的重连循环是无限的（断线 3s 后**同名**重连），
     `_handle_node` 注册时按名覆盖 ⇒ 旧连接断开时注册表里那名字的 sock 已是新连接的
     ⇒ `nm=None` ⇒ **整个 `_drop_node` → `session_end` 策略 → `_session_cleanup` 被跳过**。
     `_session_cleanup` 干的是「临时节点下机即清」（撤 PSK / 作废卸载令牌 / 丢离线队列）。
     ⇒ 临时节点断线重连时「钥匙留在门上」。

  ② `_drop_node` 误杀新连接
     `d.pop(name)` 不校验身份 ⇒ 按名字 pop 会把活得好好的新连接一起摘掉（幽灵节点）。

  ③ 客户端 socket 超时 < 心跳间隔 ⇒ 必然周期性重连
     `socket.create_connection(..., timeout=10)` 的 timeout 会**留在 socket 上**，
     而心跳是 15 秒一次 ⇒ 每个节点**必然**每 ~13 秒断开重连一次（与网络无关）。
     这就是「同名重连」不是罕见情况而是**常态**的原因。

  ④ 自检用固定 sleep 等上线 ⇒ 机器一慢就打出假阴性。

**全离线**：本门禁不开真端口、不发真密码、不启动真节点。
只调用真代码（`commander._drop_node` / `node_agent` 的超时设置），
但把它们作用在**隔离的假注册表**上，不碰真机、不碰真节点。
"""
import os
import sys
import socket
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import commander          # noqa: E402
import node_agent         # noqa: E402

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def _fresh_registry():
    """换一份隔离的注册表，别碰真节点表。"""
    tdir = tempfile.mkdtemp(prefix="fleet_t11_")
    import registry
    commander.REGISTRY = registry.NodeTable(os.path.join(tdir, "nodes.json"), interval=0)
    return commander.REGISTRY


# ---------------------------------------------------------------- ①② 世代语义

@check("同名重连：旧世代的收尾不摘新世代的节点")
def t_reconnect_not_dropped():
    reg = _fresh_registry()
    gen_old = commander._next_gen()
    gen_new = commander._next_gen()
    reg.with_lock(lambda d: d.__setitem__("N1", {
        "sock": None, "token": "t1", "gen": gen_old,
        "platform": "windows", "info": {"session": True},
        "capabilities": ["exec"], "last_hb": time.time()}))
    # 同名重连：新世代接管
    reg.with_lock(lambda d: d.__setitem__("N1", {
        "sock": None, "token": "t2", "gen": gen_new,
        "platform": "windows", "info": {"session": True},
        "capabilities": ["exec"], "last_hb": time.time()}))
    # 旧连接这时才走 finally
    dropped = commander._drop_node("N1", expect_gen=gen_old)
    assert dropped is None, "旧世代的收尾不该摘掉新世代的节点"
    assert reg.with_lock(lambda d: d.get("N1")), "新连接被旧世代误杀了"
    assert reg.with_lock(lambda d: d["N1"]["gen"]) == gen_new, "条目被旧世代覆盖了"
    assert reg.with_lock(lambda d: d["N1"]["token"]) == "t2", "token 不是新连接的"


@check("同名重连：旧世代的收尾不触发 session 清理（节点还在线）")
def t_reconnect_no_cleanup():
    reg = _fresh_registry()
    gen_old = commander._next_gen()
    gen_new = commander._next_gen()
    reg.with_lock(lambda d: d.__setitem__("N2", {
        "sock": None, "token": "t1", "gen": gen_old,
        "platform": "windows", "info": {"session": True},
        "capabilities": ["exec"], "last_hb": time.time()}))
    reg.with_lock(lambda d: d.__setitem__("N2", {
        "sock": None, "token": "t2", "gen": gen_new,
        "platform": "windows", "info": {"session": True},
        "capabilities": ["exec"], "last_hb": time.time()}))
    # 记下清理前的账本状态
    import enroll
    tdir = tempfile.mkdtemp(prefix="fleet_t11_ledger_")
    led = os.path.join(tdir, "ledger.json")
    psk = "psk-" + os.urandom(6).hex()
    enroll.register_psk(led, psk, "N2")
    commander.ENROLL_LEDGER = led
    before = len([r for r in enroll.list_psks(led) if r["node"] == "N2"])
    assert before == 1, "前置：应有一枚凭据"
    # 旧世代收尾
    commander._drop_node("N2", expect_gen=gen_old)
    after = len([r for r in enroll.list_psks(led) if r["node"] == "N2"])
    assert after == 1, "节点还在线却把凭据清了（重连 ≠ 下机）——钥匙被误撤"


@check("真下机：当前世代的收尾触发清理（PSK/令牌/队列全清）")
def t_real_offline_cleans():
    reg = _fresh_registry()
    import enroll
    tdir = tempfile.mkdtemp(prefix="fleet_t11_ledger_")
    led = os.path.join(tdir, "ledger.json")
    commander.ENROLL_LEDGER = led
    gen = commander._next_gen()
    reg.with_lock(lambda d: d.__setitem__("N3", {
        "sock": None, "token": "t1", "gen": gen,
        "platform": "windows", "info": {"session": True},
        "capabilities": ["exec"], "last_hb": time.time()}))
    psk = "psk-" + os.urandom(6).hex()
    enroll.register_psk(led, psk, "N3")
    tok = commander._issue_token("N3", "uninstall")
    with commander._QUEUE_LK:
        commander.OFFLINE_QUEUE["N3"] = [{"id": "x", "cmd": "echo hi"}]
    assert len([r for r in enroll.list_psks(led) if r["node"] == "N3"]) == 1
    # 真下机
    commander._drop_node("N3", expect_gen=gen)
    assert not reg.with_lock(lambda d: d.get("N3")), "下机后节点还挂在注册表里"
    left_psk = [r for r in enroll.list_psks(led) if r["node"] == "N3"]
    assert not left_psk, "下机了凭据却没清（钥匙留在门上）: %r" % left_psk
    with commander._TOKEN_LK:
        left_tok = sum(1 for v in commander.TOKENS.values() if v.get("node") == "N3")
    assert not left_tok, "下机了令牌却没清"
    with commander._QUEUE_LK:
        left_q = len(commander.OFFLINE_QUEUE.get("N3", []))
    assert not left_q, "下机了离线队列却没清"


@check("清理不可重放：重复清理不报错、不留残留")
def t_cleanup_idempotent():
    reg = _fresh_registry()
    import enroll
    tdir = tempfile.mkdtemp(prefix="fleet_t11_ledger_")
    led = os.path.join(tdir, "ledger.json")
    commander.ENROLL_LEDGER = led
    gen = commander._next_gen()
    reg.with_lock(lambda d: d.__setitem__("N4", {
        "sock": None, "token": "t1", "gen": gen,
        "platform": "windows", "info": {"session": True},
        "capabilities": ["exec"], "last_hb": time.time()}))
    psk = "psk-" + os.urandom(6).hex()
    enroll.register_psk(led, psk, "N4")
    commander._drop_node("N4", expect_gen=gen)
    # 再清一次：不该抛异常，也不该报错说「节点不存在」
    r = commander._drop_node("N4", expect_gen=gen)
    assert r is None, "重复清理应安静返回 None"
    assert not [x for x in enroll.list_psks(led) if x["node"] == "N4"]


@check("_drop_node 不带 expect_gen 时不会误杀（向后兼容仍按名摘）")
def t_drop_without_gen():
    reg = _fresh_registry()
    gen = commander._next_gen()
    reg.with_lock(lambda d: d.__setitem__("N5", {
        "sock": None, "token": "t1", "gen": gen,
        "platform": "windows", "info": {"session": False},
        "capabilities": ["exec"], "last_hb": time.time()}))
    # 不传 expect_gen => 按名摘（心跳判死路径用这个）
    node = commander._drop_node("N5")
    assert node is not None, "不传 expect_gen 时应能摘掉"
    assert not reg.with_lock(lambda d: d.get("N5"))


@check("世代号严格递增且唯一")
def t_gen_monotonic():
    seen = set()
    last = 0
    for _ in range(200):
        g = commander._next_gen()
        assert g > last, "世代号必须递增"
        assert g not in seen, "世代号不能重复"
        seen.add(g)
        last = g


# ---------------------------------------------------------------- ③ 超时 vs 心跳

@check("客户端 socket 建连后必须脱离建连超时（否则必然周期性重连）")
def t_client_socket_not_timed_out():
    """🔴 这条是最根本的一条：timeout 留在 socket 上 + 心跳 15s > 超时 10s
    ⇒ 每个节点**必然**每 ~13 秒断开重连一次 ⇒ 同名重连是常态不是意外。

    用 AST 检查而不是字符串窗口（字符串窗口会被注释长度影响，误报）。
    """
    import ast
    src = open(os.path.join(ROOT, "node_agent.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    # 找所有 create_connection 调用，看它所在函数里有没有紧跟的 settimeout(None)
    fns = [n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    target = None
    for fn in fns:
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "create_connection"):
                target = fn
                break
        if target:
            break
    assert target is not None, "node_agent 里找不到 socket.create_connection 调用"
    # 该函数内所有 socket.settimeout(None) 调用
    has_none = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "settimeout" and n.args
        and isinstance(n.args[0], ast.Constant) and n.args[0].value is None
        for n in ast.walk(target))
    assert has_none, (
        "node_agent 建连后必须 settimeout(None)："
        "create_connection 的 timeout 会留在 socket 上，"
        "而心跳 15s > 超时 10s ⇒ 必然周期性重连")


@check("心跳间隔短于客户端可能保留的读超时（源头约束）")
def t_heartbeat_shorter_than_timeout():
    import ast
    src = open(os.path.join(ROOT, "node_agent.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    # 找心跳线程：里面有 time.sleep(N) 紧跟 common.send_msg(..., HEARTBEAT)
    hbs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "sleep" and node.args \
                and isinstance(node.args[0], ast.Constant):
            hbs.append(node.args[0].value)
    # 心跳用的 sleep 是 15；读超时若存在必须比它小
    assert 15 in hbs, "找不到心跳间隔 15 秒的定义（源码结构变了？）"
    # 建连超时的常量
    conn_to = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_connection"):
            for kw in node.keywords:
                if kw.arg == "timeout" and isinstance(kw.value, ast.Constant):
                    conn_to = kw.value.value
    assert conn_to is not None, "找不到 create_connection 的 timeout 参数"
    assert conn_to >= 15, (
        "建连超时 %ds 必须 >= 心跳间隔 15s："
        "create_connection 的 timeout 会留在 socket 上，"
        "比心跳短就必然周期性重连" % conn_to)


# ---------------------------------------------------------------- ④ 等待方式

@check("自检等待节点上线用轮询而不是固定 sleep")
def t_wait_online_exists():
    assert hasattr(commander, "_wait_online"), "commander 缺少 _wait_online（轮询等待）"
    src = open(os.path.join(ROOT, "commander.py"), encoding="utf-8").read()
    # 自检里不许再出现「起节点 + 固定 sleep」的组合
    for bad in ('"PC-Ghost", password, "windows", caps=["exec"],\n'
                '                         sandbox="sandbox_ghost", bg=True)\n    time.sleep(',
                'sandbox="sandbox_net", bg=True, session=True)\n    time.sleep('):
        assert bad not in src, "自检里还有「起节点 + 固定 sleep」的写法（会有假阴性）"
    assert "sandbox_net" in src and "_wait_online(" in src, "PC-Net 应改用 _wait_online"


@check("_wait_online 等不到会返回 False 而不是抛异常")
def t_wait_online_returns_bool():
    _fresh_registry()
    t0 = time.time()
    r = commander._wait_online("NO-SUCH-NODE", timeout=0.3, poll=0.02)
    assert r is False, "等不到应返回 False"
    assert time.time() - t0 < 3.0, "超时参数不生效（等了太久）"


@check("node_agent 有按名字停机的手段（能干净模拟下机）")
def t_stop_node_exists():
    assert hasattr(node_agent, "stop_node"), "node_agent 缺少 stop_node（按名字停机）"
    assert hasattr(node_agent, "reset_stop"), "node_agent 缺少 reset_stop"
    # 全局停机会误伤其它节点（自检里 PC-A/Box 后面还要用）⇒ 必须能按名字停
    assert "stop_node" in open(os.path.join(ROOT, "node_agent.py"),
                                encoding="utf-8").read()


@check("stop_node 只停指定的节点，不影响别的")
def t_stop_node_scoped():
    node_agent.reset_stop()
    try:
        node_agent.stop_node("ONLY-THIS")
        with node_agent._STOP_NAMES_LK:
            assert "ONLY-THIS" in node_agent._STOPPED_NAMES
            assert "OTHER-NODE" not in node_agent._STOPPED_NAMES
    finally:
        node_agent.reset_stop()


# ------------------------------------------- ⑤ 并发串味（2026-10-21 偶发红的真根因）

@check("自检用随机端口（固定端口在并发下会串味）")
def t_selftest_random_port():
    """🔴 这是「偶发红」的**真根因**，不是「等得不够久」。

    实测：四个自检实例并发跑，三个报「PC-Ghost 在 10 秒内没有上线」。
    链子是：固定端口 9000 → Windows 的 SO_REUSEADDR 允许第二个进程绑同一端口
    且**抢走连接** → 后启动实例的节点注册进了先启动那个的注册表
    → 自己进程永远等不到 ⇒ 条件永不成立（不是等待不够久）。

    所以门禁要求：自检起服务必须用 port=0（系统随机分配），
    且必须从套接字取回真实端口再拿去连节点。
    """
    import ast
    src = open(os.path.join(ROOT, "commander.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "self_test"), None)
    assert fn is not None, "找不到 self_test"
    zero_port = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "listen" and len(n.args) >= 2
        and isinstance(n.args[1], ast.Constant) and n.args[1].value == 0
        for n in ast.walk(fn))
    assert zero_port, "自检必须用 port=0 起服务（固定端口会串味）"
    real_port = any(
        isinstance(n, ast.Attribute) and n.attr == "getsockname"
        for n in ast.walk(fn))
    assert real_port, "自检必须从套接字取回真实端口再拿它起节点"


@check("Windows 上监听不得用 SO_REUSEADDR（会抢连接）")
def t_no_reuseaddr_on_windows():
    """Windows 的 SO_REUSEADDR 语义与 Linux 不同：允许第二个进程绑同一端口，
    且新 socket 抢走后来者的连接。Linux 上不是这个行为。

    实测过：两个 socket 都设 SO_REUSEADDR，第二个 bind 同端口**成功**；
    换成 SO_EXCLUSIVEADDRUSE，第二个 bind 正确报 10048。

    端口重复是调用方的错，让它响 —— 绝不静默抢。
    """
    import ast
    src = open(os.path.join(ROOT, "netpath.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "listen"), None)
    assert fn is not None, "找不到 netpath.listen"

    # 找所有 setsockopt(SO_*) 调用，逐个看它所在分支有没有被 IS_WIN 守住
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "setsockopt"):
            continue
        sockopt = n.args[1] if len(n.args) > 1 else None
        if not isinstance(sockopt, ast.Attribute):
            continue
        if sockopt.attr == "SO_EXCLUSIVEADDRUSE":
            continue
        if sockopt.attr != "SO_REUSEADDR":
            continue
        # 这个 SO_REUSEADDR 必须在「非 Windows」分支里：
        # 既可能在 `if not IS_WIN:` 的函数体内，也可能在 `if IS_WIN: ... else:` 的 orelse 里。
        guarded = False
        for parent in ast.walk(fn):
            if not isinstance(parent, ast.If):
                continue
            test = ast.dump(parent.test)
            is_win_test = "IS_WIN" in test
            # 精确判断：它到底在 body（True 分支）还是 orelse（False 分支）
            in_body = any(sub is n for stmt in parent.body for sub in ast.walk(stmt))
            in_else = any(sub is n for stmt in parent.orelse
                           for sub in ast.walk(stmt))
            if not (in_body or in_else):
                continue
            if is_win_test:
                # if IS_WIN: ... -> 函数体是 Windows 分支（不该有 REUSEADDR）
                # else 分支（orelse）是非 Windows 分支（可以有）
                if in_else:
                    guarded = True
            else:
                # if not IS_WIN: ... -> 函数体是非 Windows 分支（可以有）
                if in_body:
                    guarded = True
            if guarded:
                break
        assert guarded, (
            "SO_REUSEADDR 必须只出现在「非 Windows」分支里："
            "Windows 上它会让第二个进程绑上同一端口并抢走连接")
    # 至少得有一处用独占
    assert any(
        isinstance(n, ast.Attribute) and n.attr == "SO_EXCLUSIVEADDRUSE"
        for n in ast.walk(fn)), "netpath.listen 必须用 SO_EXCLUSIVEADDRUSE"


@check("两个监听套接字在 Windows 上不能绑同一端口（抢连接复现）")
def t_exclusive_bind_real():
    """直接复现那个语义差异 —— 这条是行为断言，不是看代码。"""
    import netpath
    if not netpath.IS_WIN:
        return
    a, _ = netpath.listen("127.0.0.1", 0)
    port = a.getsockname()[1]
    try:
        b, _ = netpath.listen("127.0.0.1", port)
        b.close()
        a.close()
        raise AssertionError(
            "第二个监听竟能绑上同一端口 %d ⇒ 会抢连接（Windows 上必须独占）" % port)
    except OSError:
        pass
    finally:
        try:
            a.close()
        except OSError:
            pass


def main():
    ok = 0
    bad = 0
    for name, fn in CHECKS:
        try:
            fn()
            print("  ok %s" % name)
            ok += 1
        except AssertionError as e:
            print("  x  %s —— %s" % (name, e))
            bad += 1
        except Exception as e:
            print("  x  %s —— 意外异常: %r" % (name, e))
            bad += 1
    print("t11 node_lifecycle: %s" % ("PASS" if bad == 0 else "FAIL"))
    print("  %d 项断言，%d 通过 / %d 失败" % (ok + bad, ok, bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

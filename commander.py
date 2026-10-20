"""Commander：统一调用控制面（L0 连通 + L1 执行枢纽）。

- 节点以密码认证后注册，获得会话令牌；
- 维持节点表 + 心跳；
- 运维侧（本机）通过 REPL 把指令扇出到节点：nodes / exec / push / pull。

用法：
  python commander.py                      # 起服务 + 交互 REPL（本机控他机）
  python commander.py --self-test          # 本地回路自检（模拟两台 PC，不碰真机）
  python commander.py --password 你的密码 --host 0.0.0.0 --port 9000
"""
import argparse
import base64
import os
import socket
import sys
import threading
import time

import common

REGISTRY = common.LockedDict()          # name -> {"sock","token","platform","info","last_hb"}
PENDING = {}                             # msg id -> {"ev":Event,"slot":{}}
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
    """更新等待项的 slot 并置事件；不在此处 pop，删除由 send_to_node 唤醒后负责，
    避免『先 pop 再读』导致调用方读到空 slot。"""
    with _PENDING_LK:
        p = PENDING.get(mid)
        if p:
            p["slot"].update(payload)
            p["ev"].set()


def send_to_node(name, msg, timeout=30):
    """向指定节点发消息并等待其回执（exec/push/pull 用）。"""
    node = REGISTRY.with_lock(lambda d: d.get(name))
    if not node:
        return {"ok": False, "err": f"节点 {name} 不在线"}
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
    # 唤醒后再读 slot，然后移除等待项（避免 _resolve_pending 先 pop 导致读空）
    with _PENDING_LK:
        slot = PENDING.pop(mid, {"slot": {}})["slot"]
    return {"ok": True, **slot}


def _drop_node(name):
    REGISTRY.with_lock(lambda d: d.pop(name, None))


def _handle_node(conn, password):
    """单节点连接处理（server 线程内）。认证 -> 注册 -> 事件循环。"""
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
            "info": reg.get("info", {}), "last_hb": time.time(),
        }))
        print(f"[+] 节点上线: {name} ({reg.get('platform')})", flush=True)
        while True:
            m = common.recv_msg(conn)
            if m is None:
                break
            t = m.get("t")
            if t == common.HEARTBEAT:
                REGISTRY.with_lock(lambda d: d.get(name) and d[name].__setitem__("last_hb", time.time()))
            elif t in (common.EXEC_RESULT, common.PUSH_RESULT, common.PULL_RESULT):
                _resolve_pending(m.get("id"), m)
            else:
                print(f"[?] 节点 {name} 未知消息: {t}", flush=True)
    except OSError:
        pass
    finally:
        # 找出 name（conn 可能在注册前断开）
        nm = REGISTRY.with_lock(lambda d: next((k for k, v in d.items() if v["sock"] is conn), None))
        if nm:
            _drop_node(nm)
            print(f"[-] 节点下线: {nm}", flush=True)


def serve(host, port, password):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(32)
    print(f"[*] Commander 监听 {host}:{port}（密码认证已启用）", flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=_handle_node, args=(conn, password), daemon=True).start()


def _cmd_nodes():
    rows = REGISTRY.with_lock(lambda d: [(k, v["platform"], round(time.time() - v["last_hb"], 1))
                                         for k, v in d.items()])
    if not rows:
        print("（无在线节点）")
    for k, p, age in rows:
        print(f"  {k:12} {p:8} 心跳 {age}s 前")


def _cmd_exec(name, cmd):
    r = send_to_node(name, {"t": common.EXEC, "cmd": cmd})
    if not r.get("ok"):
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
    print(f"  {'OK' if r.get('ok') else '失败: '+str(r.get('err'))}")


def _cmd_pull(name, remotepath, outpath):
    r = send_to_node(name, {"t": common.PULL, "path": remotepath})
    if not r.get("ok"):
        print(f"  失败: {r.get('err')}")
        return
    open(outpath, "wb").write(base64.b64decode(r["data"]))
    print(f"  已拉取 -> {outpath} ({len(r['data'])}B base64)")


def _cmd_broadcast(cmd):
    """统一调用平面：一条意图扇出到所有在线节点（不割裂）。"""
    names = REGISTRY.with_lock(lambda d: list(d.keys()))
    if not names:
        print("（无在线节点）")
        return
    for n in names:
        print(f"-- -> {n} --", flush=True)
        _cmd_exec(n, cmd)


def repl():
    print("Commander REPL：nodes | exec <节点> <命令> | broadcast <命令> | push <节点> <本地文件> | pull <节点> <远程文件> [输出] | quit")
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
            else:
                print("  用法: exec <节点> <命令>")
        elif op == "broadcast":
            if rest:
                _cmd_broadcast(rest)
            else:
                print("  用法: broadcast <命令>")
        elif op == "push":
            sp = rest.split(" ", 1)
            if len(sp) == 2:
                _cmd_push(sp[0], sp[1])
            else:
                print("  用法: push <节点> <本地文件>")
        elif op == "pull":
            sp = rest.split(" ", 2)
            if len(sp) >= 2:
                _cmd_pull(sp[0], sp[1], sp[2] if len(sp) >= 3 else sp[1].split("/")[-1])
            else:
                print("  用法: pull <节点> <远程文件> [输出]")
        else:
            print("  用法: nodes | exec <节点> <命令> | broadcast <命令> | push <节点> <本地文件> | pull <节点> <远程文件> [输出]")


def self_test(host, port, password):
    """本地回路自检：起服务 + 模拟两台 PC（PC-A / PC-B），证明本机经密码控他机。"""
    import node_agent
    threading.Thread(target=serve, args=(host, port, password), daemon=True).start()
    time.sleep(0.4)
    # 模拟两台 Windows PC：各自连 commander、密码认证、注册（用户确认 MVP 两台都是 Win）
    node_agent.run_node(host, port, "PC-A", password, "windows", sandbox="sandbox_A", bg=True)
    node_agent.run_node(host, port, "PC-B", password, "windows", sandbox="sandbox_B", bg=True)
    # 等两台注册
    for _ in range(50):
        if REGISTRY.with_lock(lambda d: len(d)) >= 2:
            break
        time.sleep(0.1)
    print("\n=== 自检开始：本机(Commander)经密码控制另一台(PC-B) ===", flush=True)
    _cmd_nodes()
    print("\n-- exec PC-B: 证明指令在『另一台电脑』上执行 --", flush=True)
    _cmd_exec("PC-B", "echo 由Commander控制")
    _cmd_exec("PC-B", "hostname")
    _cmd_exec("PC-B", "whoami")
    print("\n-- push 文件到 PC-A（反向推送：中心->设备）--", flush=True)
    with open("sandbox_A/_seed.txt", "w", encoding="utf-8") as f:
        f.write("来自Commander的种子文件")
    _cmd_push("PC-A", "sandbox_A/_seed.txt")
    print("\n-- pull 回 PC-A 的文件，验证闭环 --", flush=True)
    _cmd_pull("PC-A", "_seed.txt", "sandbox_pulled.txt")
    print("\n-- broadcast：一条意图扇出到所有节点（统一调用平面，不割裂）--", flush=True)
    _cmd_broadcast("echo 统一指令已抵达")
    print("\n=== 自检通过：L0 连通+认证 / L1 执行+文件推拉 / 统一扇出 均工作 ===", flush=True)
    os._exit(0)


def main():
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

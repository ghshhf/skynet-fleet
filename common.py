"""设备编队：传输协议与公共原语（stdlib-only，跨平台）。

消息 = 4 字节大端长度前缀 + UTF-8 JSON。
所有消息是 dict，含字段 "t" 表示类型。
"""
import json
import socket
import struct
import threading

# ---- 消息类型（L0/L1 基础）----
AUTH = "auth"                 # node -> commander: {name, password}
AUTH_OK = "auth_ok"           # commander -> node:  {token}
AUTH_FAIL = "auth_fail"       # commander -> node:  {reason}
REGISTER = "register"         # node -> commander: {name, platform, info, capabilities[]}
HEARTBEAT = "hb"              # node -> commander
EXEC = "exec"                 # commander -> node: {id, cmd}
EXEC_RESULT = "exec_result"   # node -> commander: {id, rc, out, err}
PUSH = "push"                 # commander -> node: {id, path, data(b64)}
PUSH_RESULT = "push_result"   # node -> commander: {id, ok, err}
PULL = "pull"                 # commander -> node: {id, path}
PULL_RESULT = "pull_result"   # node -> commander: {id, ok, data(b64), err}

# ---- L1 能力注册表 ----
CAPABILITIES = "capabilities" # node -> commander: 上报/更新能力列表

# ---- L2 离线队列（Phase 4 体验）----
QUEUE = "queue"              # commander -> node（上线重放）: {cmds:[{id, cmd}]}

# ---- L3 共享上下文（TinyContext 式）----
CTX_PUT = "ctx_put"
CTX_PUT_RESULT = "ctx_put_result"
CTX_GET = "ctx_get"
CTX_GET_RESULT = "ctx_get_result"
CTX_SEARCH = "ctx_search"
CTX_SEARCH_RESULT = "ctx_search_result"
CTX_DEL = "ctx_del"
CTX_DEL_RESULT = "ctx_del_result"

# ---- 配对设备一次性令牌（卸载闸门 ②）----
TOKEN_REQUEST = "token_req"   # node/install -> commander: {purpose, node}
TOKEN_RESULT = "token_res"    # commander -> 请求方: {token}（也经手机下发）

# ---- 共享算力（§九 dev_pick 路由选「谁能干」；§五 电脑=她的手跑重活）----
COMPUTE = "compute"           # commander -> node: {id, prompt, model?}
COMPUTE_RESULT = "compute_result"  # node -> commander: {id, ok, out, model}

# ---- UI 自动化（无障碍跳广告 / 通用 UI 动作，源自李跳跳拆解）----
UI_SKIP = "ui_skip"                 # commander -> node: {id, package, serial?, rounds?, use_vision?, rules?}
UI_SKIP_RESULT = "ui_skip_result"   # node -> commander: {id, ok, log[], err}

# ---- 她 P4 能力请求 + 授权门 ----
CAP_REQ = "cap_req"           # node(她) -> commander: {cap, reason}
CAP_GRANT = "cap_grant"       # commander -> node(她): {cap, session_min, token}
CAP_DENY = "cap_deny"         # commander -> node(她): {cap, reason}

HEADER = 4  # 长度前缀字节数

# 演示用执行白名单（真实部署须收紧 + 走 Tailscale）
EXEC_ALLOWLIST = {
    "echo", "ping", "ver", "systeminfo", "dir", "ls", "whoami",
    "hostname", "python", "type", "cat", "ipconfig", "tasklist", "date", "time",
    "set", "path",
}


_SEND_LOCKS = {}
_SEND_LK = threading.Lock()


def _lock_for(sock):
    """每条连接一把写锁。

    为什么必须有：心跳线程与执行线程会并发往**同一个 socket** 写。若不加锁，
    两个帧的「长度头 + 载荷」会交错，对端解析随即错位——表现为随机的
    「等待节点回执超时」，甚至整条连接后续全废。这类 bug 偶发、难复现，
    却会让所有超时判定（心跳判死、编排回滚）建立在假信号上。
    """
    key = id(sock)
    with _SEND_LK:
        lk = _SEND_LOCKS.get(key)
        if lk is None:
            lk = _SEND_LOCKS[key] = threading.Lock()
        return lk


def send_msg(sock, obj):
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    frame = struct.pack(">I", len(payload)) + payload
    with _lock_for(sock):
        sock.sendall(frame)          # 一次写入整帧，不留半帧窗口


def recv_msg(sock):
    """读取一条消息；连接关闭返回 None。"""
    hdr = _recv_exact(sock, HEADER)
    if hdr is None:
        return None
    n = struct.unpack(">I", hdr)[0]
    body = _recv_exact(sock, n)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def is_safe_cmd(cmd):
    base = cmd.strip().split(" ", 1)[0].lower() if cmd.strip() else ""
    return base in EXEC_ALLOWLIST


class LockedDict(dict):
    """带锁的节点表，供 server 线程与运维线程并发访问。"""

    def __init__(self):
        super().__init__()
        self._lk = threading.Lock()

    def with_lock(self, fn):
        with self._lk:
            return fn(self)

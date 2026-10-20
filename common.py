"""设备编队：传输协议与公共原语（stdlib-only，跨平台）。

消息 = 4 字节大端长度前缀 + UTF-8 JSON。
所有消息是 dict，含字段 "t" 表示类型。
"""
import json
import socket
import struct
import threading
import weakref

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

# ---- 她 P4 能力请求 + 授权门 ----
CAP_REQ = "cap_req"           # node(她) -> commander: {cap, reason}
CAP_GRANT = "cap_grant"       # commander -> node(她): {cap, session_min, token}
CAP_DENY = "cap_deny"         # commander -> node(她): {cap, reason}

# ---- 远程 AI 即服务（RFC remote-ai-service 合入主线）----
# 瘦客户端不持有权重，只把 prompt + 上下文命名空间发到编队内的 AI 节点，收回文本。
AI_CHAT = "ai.chat"           # client/thin -> commander: {prompt, ctx_ns?, model_hint?}
AI_CHUNK = "ai.chunk"          # ai-node -> client: {id, delta}（流式）
AI_RESULT = "ai.result"        # ai-node -> client: {id, text, model, err?}

# 她的主动问候（心念 tick 到点，经 Commander 转呈用户）
HER_SPEAK = "her_speak"       # node(她) -> commander: {text, importance, trigger}

# AI 能力名 + 档位 -> 路由器偏好
AI_CAP = "ai"
MODEL_HINTS = ("small", "4b", "gpu")

HEADER = 4  # 长度前缀字节数

# 演示用执行白名单（真实部署须收紧 + 走 Tailscale）
EXEC_ALLOWLIST = {
    "echo", "ping", "ver", "systeminfo", "dir", "ls", "whoami",
    "hostname", "python", "type", "cat", "ipconfig", "tasklist", "date", "time",
    "set", "path",
}


def send_msg(sock, obj):
    """发送一条消息。按 socket 串行化，避免多线程（心跳/读线程/忙时）并发写同一连接造成帧错乱。"""
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    with _send_lock(sock):
        sock.sendall(struct.pack(">I", len(payload)))
        sock.sendall(payload)


_SEND_LOCKS = weakref.WeakKeyDictionary()
_SEND_LOCKS_LK = threading.Lock()


def _send_lock(sock):
    with _SEND_LOCKS_LK:
        lk = _SEND_LOCKS.get(sock)
        if lk is None:
            lk = threading.Lock()
            _SEND_LOCKS[sock] = lk
        return lk


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

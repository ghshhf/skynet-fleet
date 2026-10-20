"""设备编队：传输协议与公共原语（stdlib-only，跨平台）。

消息 = 4 字节大端长度前缀 + UTF-8 JSON。
所有消息是 dict，含字段 "t" 表示类型。
"""
import json
import socket
import struct
import threading

# ---- 消息类型 ----
AUTH = "auth"                 # node -> commander: {name, password}
AUTH_OK = "auth_ok"           # commander -> node:  {token}
AUTH_FAIL = "auth_fail"       # commander -> node:  {reason}
REGISTER = "register"         # node -> commander: {name, platform, info}
HEARTBEAT = "hb"              # node -> commander
EXEC = "exec"                 # commander -> node: {id, cmd}
EXEC_RESULT = "exec_result"   # node -> commander: {id, rc, out, err}
PUSH = "push"                 # commander -> node: {id, path, data(b64)}
PUSH_RESULT = "push_result"   # node -> commander: {id, ok, err}
PULL = "pull"                 # commander -> node: {id, path}
PULL_RESULT = "pull_result"   # node -> commander: {id, ok, data(b64), err}
RESULT = "result"             # commander -> 本地运维CLI: {ok, data, err}

HEADER = 4  # 长度前缀字节数

# 演示用执行白名单（真实部署须收紧 + 走 Tailscale）
EXEC_ALLOWLIST = {
    "echo", "ping", "ver", "systeminfo", "dir", "ls", "whoami",
    "hostname", "python", "type", "cat", "ipconfig", "tasklist", "date", "time",
}


def send_msg(sock, obj):
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sock.sendall(struct.pack(">I", len(payload)))
    sock.sendall(payload)


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

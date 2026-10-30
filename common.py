"""设备编队：传输协议与公共原语（stdlib-only，跨平台）。

消息 = 4 字节大端长度前缀 + UTF-8 JSON。
所有消息是 dict，含字段 "t" 表示类型。
"""
import json
import os
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

# ---- D1 控制面对外协议（客户端侧，不是节点）----
# 为什么要有：`shell.exec` / `file.push` / `file.pull` 这类「对别的机器动手」的
# 能力，实现不在调用方进程里，而在**控制面持有的节点 socket** 上。没有这条协议，
# 任何非 commander 进程（比如桌面 GUI）想说「让 N1 跑这条命令」时，
# 唯一诚实的回答只能是「做不到」——所以要么补这条协议，要么别登记这些能力。
#   客户端 = 和节点走同一道 AUTH（同一把密码），但**注册的是角色不是身份**：
#   节点发 REGISTER 进编队，客户端发 CLIENT 只要一个请求通道。
CLIENT = "client"             # 客户端 -> commander: {}（AUTH 之后发，声明「我是客户端」）
CLIENT_REQ = "client_req"     # 客户端 -> commander: {id, op, node, ...}
CLIENT_RES = "client_res"     # commander -> 客户端: {id, ok, data|err}
# op 全集（少一个就必须报错，不能默默当成别的）
OP_NODES = "nodes"            # 列出在线节点（只读）
OP_EXEC = "exec"              # node + cmd
OP_PUSH = "push"              # node + path + data(b64)
OP_PULL = "pull"              # node + path

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

# ---- 安卓出图节点 fleet 化（路线甲 · N+2 实质落地）：PC 派发 -> 手机出图 -> PNG 回传 ----
IMAGE_GEN = "image_gen"                 # commander -> node: {id, prompt, neg?, size?, steps?, seed?, cfg?, model?}
IMAGE_GEN_RESULT = "image_gen_result"   # node -> commander: {id, ok, path?, err?}

# ---- 跨设备记忆同步（P5c）----
MEMORY_SYNC = "memory_sync"              # commander -> node: {id, op: export|import, items?}
MEMORY_SYNC_RESULT = "memory_sync_result" # node -> commander: {id, ok, items?, count?, err?}

# ---- 视频生成（P3 壳 + 远端路由）：本地算力撑不住，壳登记 + 路由到 PC/云端节点 ----
VIDEO_GEN = "video_gen"                 # commander -> node: {id, prompt, duration?, fps?, size?, seed?, model?, motion?, gen_fps?, prompts?}
VIDEO_GEN_RESULT = "video_gen_result"   # node -> commander: {id, ok, path?, err?}

# ---- 漫剧 / 分镜成片（画布 + 多镜头 + 字幕 + 配音 + 转场）----
VIDEO_STORY = "video_story"             # commander -> node: {id, board}  board 见 fleet/storyboard.py
VIDEO_STORY_RESULT = "video_story_result"  # node -> commander: {id, ok, path?, shots?, err?}

# ---- 她 P4 能力请求 + 授权门 ----
CAP_REQ = "cap_req"           # node(她) -> commander: {cap, reason}
CAP_GRANT = "cap_grant"       # commander -> node(她): {cap, session_min, token}
CAP_DENY = "cap_deny"         # commander -> node(她): {cap, reason}

# ---- 账号与分权（主号/分号，见 accounts.py）----
LOGIN = "login"               # client -> commander: {user, password}
LOGIN_RESULT = "login_res"    # commander -> client: {ok, token, role, exp, err}
GRANT_MAKE = "grant_make"     # owner -> commander: {target, kind}
GRANT_RESULT = "grant_res"    # commander -> owner: {code, uri, exp, err}
GRANT_REDEEM = "grant_redeem"  # client -> commander: {code | uri}
GRANT_REDEEM_RESULT = "grant_redeem_res"  # commander -> client: {ok, target, role, err}

HEADER = 4  # 长度前缀字节数

# 演示用执行白名单（真实部署须收紧 + 走 Tailscale）
EXEC_ALLOWLIST = {
    "echo", "ping", "ver", "systeminfo", "dir", "ls", "whoami",
    "hostname", "type", "cat", "ipconfig", "tasklist", "date", "time",
    "set", "path",
}

# 解释器 = 任意代码执行，默认**不进**白名单：要跑脚本先显式
# FLEET_ALLOW_INTERPRETER=1（并接受「等于把节点交给命令方」这个后果）。
INTERPRETERS = {"python", "python3", "py", "powershell", "pwsh", "cmd", "bash", "sh"}

# 元字符一律拒：节点侧 exec 走 shell=True，一个 & | ; > 就能链到白名单外的命令
# （`echo ok & del x` 这种 base 是 echo、却能干任何事的组合，是白名单最大的洞）。
SHELL_META = set("&|;<>`$\n\r")


def cmd_reject_reason(cmd):
    """exec 前置检查：返回 None 放行，否则返回拒绝理由（给人看的）。

    三层：① 空命令 ② base 不在白名单（解释器另走显式开关）
    ③ 参数里带 shell 元字符（防链式命令）。
    诚实边界：白名单内的 type/dir/cat 本来就能读服务账号能读的一切——
    L1 的边界是安装闸（管理员启动），不是这份名单；名单防的是「借白名单命令
    跳出去干别的」，不是「白名单命令本身别干它该干的事」。
    """
    if not cmd or not cmd.strip():
        return "空命令"
    base = cmd.strip().split(" ", 1)[0].lower()
    if base in EXEC_ALLOWLIST:
        pass
    elif base in INTERPRETERS and os.environ.get("FLEET_ALLOW_INTERPRETER") == "1":
        pass
    else:
        return f"命令不在白名单: {base}"
    bad = sorted(c for c in SHELL_META if c in cmd)
    if bad:
        return f"含 shell 元字符 {bad}（禁止链式命令）"
    return None


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
    return cmd_reject_reason(cmd) is None


class LockedDict(dict):
    """带锁的节点表，供 server 线程与运维线程并发访问。"""

    def __init__(self):
        super().__init__()
        self._lk = threading.Lock()

    def with_lock(self, fn):
        with self._lk:
            return fn(self)

"""D1 控制面客户端（fleet.ctrl_client）—— 让「非 commander 进程」也能对编队发令。

为什么必须存在
--------------
`shell.exec` / `file.push` / `file.pull` 的实现不在调用方进程里，而在**控制面
（commander）持有的节点 socket** 上。桌面 GUI 是另一个进程，它想让 N1 跑一条
命令时，只有两条路：
  ① 拿到控制面的对外协议（本文件 + commander 的 CLIENT 角色）——真代发；
  ② 老老实实说「做不到」——摘掉登记，不留空壳。
总纲 P1-9 选的是 ①。

协议（与 common.CLIENT* 一一对应）
----------------------------------
    connect → AUTH{name,password} → AUTH_OK → CLIENT
    → CLIENT_REQ{id,op,node,...} → CLIENT_RES{id,ok,data|err}

纪律
----
· **每个请求一条连接**：控制面不加会话状态，客户端也不用维护心跳/重连状态机，
  调用方拿到就是拿到了，失败带原因（不做后台偷偷重试，静默重试会掩盖故障）。
· **失败必须能分辨**：`offline=True` 是「节点不在线」（去叫它上线），
  其他是「通道问题/命令失败」（看 err）。混成一句「失败」等于让调用方瞎猜。
· 密码只从环境/显式参数取，**不落盘、不进日志**。
"""
import os
import socket
import sys
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import common  # noqa: E402

# 🔴 不把 host/port 冻成模块常量：常量在 import 时就定型，调用方随后改
# 环境变量（FLEET_HOST/FLEET_PORT）或测试切端口都不生效——现象是「明明配了
# 却连 9000」。改成每次现读。
def default_host():
    return os.environ.get("FLEET_HOST") or "127.0.0.1"


def default_port():
    try:
        return int(os.environ.get("FLEET_PORT") or 9000)
    except ValueError:
        return 9000
# 密码只认环境变量：写进代码/命令行就是把它留在 ps 和 shell 历史里。
PASS_ENVS = ("FLEET_PASS", "FLEET_PASSWORD", "FLEET_CTRL_PASS")


class ControlError(Exception):
    """控制面不可用 / 认证失败 / 节点不在线 / 节点报错 —— 都带可读原因。"""

    def __init__(self, msg, offline=False):
        super().__init__(msg)
        self.offline = bool(offline)


def password_from_env():
    for k in PASS_ENVS:
        v = os.environ.get(k)
        if v:
            return v
    return ""


class ControlClient:
    """控制面的一个瘦客户端。用完即弃（每次请求一条连接）。"""

    def __init__(self, host=None, port=None, password=None,
                 name="fleet-client", timeout=60.0):
        self.host = host or default_host()
        self.port = int(port or default_port())
        self.password = password if password is not None else password_from_env()
        self.name = name
        self.timeout = float(timeout)

    # ---- 内部：建一条已认证的连接 ----
    def _open(self):
        if not self.password:
            raise ControlError(
                "没有控制面密码：设环境变量 FLEET_PASS 后再试（控制面只认密码，"
                "不认『本机就免认证』——那样任何本机进程都能指挥编队）")
        try:
            conn = socket.create_connection((self.host, self.port), timeout=8.0)
        except OSError as e:
            raise ControlError("连不上控制面 %s:%s（%s）—— 先起 commander"
                               % (self.host, self.port, e))
        conn.settimeout(self.timeout)
        try:
            common.send_msg(conn, {"t": common.AUTH, "name": self.name,
                                   "password": self.password})
            r = common.recv_msg(conn)
            if not r or r.get("t") != common.AUTH_OK:
                raise ControlError("控制面认证失败：%s"
                                   % ((r or {}).get("reason") or "没收到 AUTH_OK"))
            common.send_msg(conn, {"t": common.CLIENT})
        except ControlError:
            conn.close()
            raise
        except OSError as e:
            conn.close()
            raise ControlError("认证阶段断线：%s" % e)
        return conn

    def request(self, op, **kw):
        """发一个请求，返回 data（dict）。失败抛 ControlError（带 offline 标记）。"""
        rid = uuid.uuid4().hex[:12]
        conn = self._open()
        try:
            msg = {"t": common.CLIENT_REQ, "id": rid, "op": op}
            msg.update(kw)
            common.send_msg(conn, msg)
            r = common.recv_msg(conn)
            if not r or r.get("t") != common.CLIENT_RES:
                raise ControlError("控制面没按协议回 client_res：%r" % (r,))
            if r.get("id") != rid:
                raise ControlError("回执 id 对不上（发 %s 收 %s）" % (rid, r.get("id")))
            if not r.get("ok"):
                raise ControlError(r.get("err") or "控制面拒绝（没说原因）",
                                   offline=bool(r.get("offline")))
            return r.get("data") or {}
        except OSError as e:
            raise ControlError("与控制面通信中断：%s" % e)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # ---- 便捷动作 ----
    def nodes(self):
        return self.request(common.OP_NODES).get("nodes") or []

    def nodes_detail(self):
        """名字 → {platform, peer, caps}。

        `peer` 是**真实来源地址**：跨网段排查第一问就是「它从哪连来的」，
        只有名字时无法区分「它连了我」和「它自己连自己」。
        """
        return self.request(common.OP_NODES).get("detail") or {}

    def exec(self, node, cmd, timeout=None):
        """在节点上执行命令（白名单由节点侧把关）。返回 {rc, out, err}。"""
        return self.request(common.OP_EXEC, node=node, cmd=cmd, timeout=timeout)

    def push(self, node, path, data_b64, timeout=None):
        """把 base64 内容写到节点上的 path。

        ⚠️ 节点侧会**沙箱化**：只取你给的 basename，落到它的沙箱目录里，
        目录部分被丢弃。所以返回的 `path` 才是**真实落盘路径**，
        别拿你自己传的路径去找文件（2026-10-25 实测踩过：传 tmp\\a.txt，
        回 ok，去 tmp 看是空的，文件在 sandbox_node\\a.txt）。
        """
        return self.request(common.OP_PUSH, node=node, path=path,
                            data=data_b64, timeout=timeout)

    def pull(self, node, path, timeout=None):
        """从节点上读 path 的 base64 内容（同样只认沙箱里的 basename）。"""
        return self.request(common.OP_PULL, node=node, path=path, timeout=timeout)


def available(host=None, port=None):
    """控制面是否可达（只探端口，不认证）—— 用于 probe 与「诚实报错」文案。"""
    h = host or default_host()
    p = int(port or default_port())
    try:
        s = socket.create_connection((h, p), timeout=0.4)
    except OSError:
        return False
    s.close()
    return True


if __name__ == "__main__":       # 手工探活用：python -m fleet.ctrl_client
    print("控制面 %s:%s 可达=%s，密码在环境里=%s"
          % (default_host(), default_port(), available(), bool(password_from_env())))

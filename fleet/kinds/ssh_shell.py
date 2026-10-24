"""远程 shell 通道：把「别人的机器」当成设备来管。

为什么它算设备而不是一个函数：仓库里所有「能对外做什么」的东西都归设备层
（本机 / 手机 / 扫描器 / 编队节点 / 万维网）。一台只会听 22 端口的服务器，
和一台只会发红外的手机没有本质区别——都是「有的能力现在能用，有的不能用，
问一句才知道」。所以 ssh 走后门进来，也走同一条路：注册表 → 闸门 → 结果信封。

两条能力，故意只有两条：

  ssh.banner   只连 TCP，读第一行 `SSH-2.0-xxx`。
               这是**真的** SSH——不是「22 端口开着就算」：2200 端口上跑 SMTP 的机器
               多得是，把 banner 读出来才敢说是 sshd。纯 socket，不需要客户端。
  ssh.exec     真执行，走系统 ssh 客户端（BatchMode=yes + ConnectTimeout）。
               绝不自己实现 SSH 协议——那是密码学工程，抄一半比不抄更危险。

诚实性纪律（这文件最容易违反的三条）：

1. **probe 只回答「我这台 adapter 现在能不能干活」**，不是「目标机通不通」。
   目标机是参数，不是设备属性；把目标机写进 probe 会让「换一台机器」变成「换设备」。
2. **客户端不在就整台下线**，不做「banner 还能用所以算可用」的自我安慰——
   主职是执行，执行不了就是不可用，原因写清。宁可整台红，不给半绿。
3. **密码永不进命令行**。`ssh` 的密码只能交互输入，把密码塞进 argv 会被
   `ps` / 任务管理器看见，也会进 shell 历史。本模块只支持密钥/agent，
   要密码就如实拒绝并告诉你去配密钥——这不是缺陷，是边界。
"""
import os
import re
import shutil
import socket
import subprocess
import time

from ..core import BaseDevice, DeviceResult as R

#: banner 只认这一行；sshd 允许前置若干行（RFC 4253 说服务端可以在版本行前发别的行）
_BANNER_RE = re.compile(r"^SSH-(\d+\.\d+)-(\S+)", re.M)

#: 常见客户端位置：PATH 之外，Windows 的可选功能与 Git for Windows 都要捞一遍
_SSH_CANDIDATES = (
    r"C:\Windows\System32\OpenSSH\ssh.exe",
    r"C:\Program Files\Git\usr\bin\ssh.exe",
    r"C:\Program Files (x86)\Git\usr\bin\ssh.exe",
    "/usr/bin/ssh",
    "/usr/local/bin/ssh",
)


def find_ssh():
    """找到能用的 ssh 客户端，返回 (路径, "") 或 (None, 原因)。"""
    p = shutil.which("ssh")
    if p:
        return p, ""
    for c in _SSH_CANDIDATES:
        if os.path.isfile(c):
            return c, ""
    return None, ("没找到 ssh 客户端：Windows 装「可选功能 → OpenSSH 客户端」，"
                  "或装 Git for Windows，或 Linux 上 apt install openssh-client")


class SshShell(BaseDevice):
    kind = "ssh"
    via = "ssh"

    def __init__(self, name="ssh:shell", **kw):
        super().__init__(name, via="ssh", **kw)
        self.meta.setdefault(
            "note", "只连你拥有或获授权的机器；密码不进命令行，只支持密钥/agent")

    def probe(self):
        """客户端在不在。目标机通不通是参数的事，不在这里判。

        注意把 `find_ssh()` 的**路径**收成 bool：probe 的契约是 (bool, 原因)，
        直接把路径漏出去的话，注册表判「可用」时看的是「非空」，一旦有人
        写成 `if ok is True` 就全线误判。
        """
        cli, why = find_ssh()
        return bool(cli), why

    def caps(self):
        return ("ssh.banner", "ssh.exec")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "ssh.banner":
            return self._banner(args)
        if cap == "ssh.exec":
            return self._exec(args)
        return super().invoke(cap, args)

    # ---------------- 只读：真读 banner ----------------

    def _banner(self, args):
        host = (args.get("host") or "").strip()
        if not host:
            return R.error("ssh.banner", "缺 host", device=self.name, via=self.via)
        port = int(args.get("port") or 22)
        ms = int(args.get("timeout_ms") or 1500)
        t0 = time.time()
        # 「连不上」和「连上了不说话」必须分开报——第一版把 create_connection 的
        # 超时和 recv 的超时塞进同一个 except，结果 22 端口根本没应答，却报出
        # 「连上了但不说话」。这两句对排查的价值差一个量级，不能混。
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(ms / 1000.0)
            s.connect((host, port))
        except socket.timeout:
            s.close()
            return R.timeout(
                "ssh.banner",
                f"{host}:{port} 的 TCP 握手 {ms}ms 没完成（不是「连上了不说话」）。"
                f"本机实测：Windows 上连已关端口常是静默丢包而不返 RST，"
                f"所以「超时」不等于「没有服务」", device=self.name,
                ms=(time.time() - t0) * 1000, via=self.via)
        except OSError as e:
            s.close()
            return R.unavailable("ssh.banner", f"{host}:{port} 连不上：{e}",
                                 device=self.name, ms=(time.time() - t0) * 1000,
                                 via=self.via)
        try:
            s.settimeout(ms / 1000.0)
            buf = b""
            # 读到版本行或读满 1KB 就停：sshd 有时先发几行注释
            while len(buf) < 1024:
                chunk = s.recv(256)
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf and _BANNER_RE.search(buf.decode("latin-1")):
                    break
        except socket.timeout:
            return R.timeout("ssh.banner", f"{host}:{port} 连上了但不说话（{ms}ms）",
                             device=self.name, ms=(time.time() - t0) * 1000,
                             via=self.via)
        except OSError as e:
            return R.unavailable("ssh.banner", f"{host}:{port} 读 banner 出错：{e}",
                                 device=self.name, ms=(time.time() - t0) * 1000,
                                 via=self.via)
        finally:
            try:
                s.close()
            except OSError:
                pass

        text = buf.decode("latin-1")
        m = _BANNER_RE.search(text)
        took = (time.time() - t0) * 1000
        if not m:
            # 端口开着但不是 SSH——这条比「连不上」更值钱，必须显式区分
            return R.success("ssh.banner", self.name, via=self.via, ms=took, data={
                "host": host, "port": port, "is_ssh": False,
                "first_line": text.splitlines()[0][:120] if text else "",
                "verdict": "端口开着，但没吐 SSH 版本行 —— 不是 sshd",
            })
        return R.success("ssh.banner", self.name, via=self.via, ms=took, data={
            "host": host, "port": port, "is_ssh": True,
            "proto": m.group(1), "software": m.group(2),
            "banner": m.group(0)[:160], "verdict": "确认是 sshd",
        })

    # ---------------- 副作用：真执行 ----------------

    def _exec(self, args):
        cli, why = find_ssh()
        if not cli:
            return R.unavailable("ssh.exec", why, device=self.name, via=self.via)
        host = (args.get("host") or "").strip()
        cmd = args.get("cmd") or ""
        if not host or not cmd:
            return R.error("ssh.exec", "缺 host 或 cmd", device=self.name, via=self.via)
        if args.get("password"):
            # 不接密码不是省事，是纪律：argv 里的密码等于公开
            return R.denied("ssh.exec",
                            "不收密码：ssh 的密码只能交互输入，塞进命令行会被 ps 看见。"
                            "请配密钥或 ssh-agent", device=self.name, via=self.via)

        port = int(args.get("port") or 22)
        user = (args.get("user") or "").strip()
        timeout = float(args.get("timeout") or 20)
        target = f"{user}@{host}" if user else host
        argv = [cli, "-p", str(port),
                "-o", "BatchMode=yes",                    # 绝不卡在密码提示上等死
                "-o", f"ConnectTimeout={max(1, int(timeout / 2) or 1)}",
                "-o", "StrictHostKeyChecking=accept-new",  # 首次自动记住，之后变了才拦
                "-o", "LogLevel=ERROR",
                target, "--"]                             # `--` 之后一律当命令，不给注入留口子
        # cmd 走 stdin 交给远端 shell：避免本地 shell 二次解析（引号地狱 + 注入）
        argv += ["sh -s"]
        t0 = time.time()
        try:
            p = subprocess.run(argv, input=cmd.encode("utf-8"), capture_output=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            return R.timeout("ssh.exec", f"{target} 执行超过 {timeout}s",
                             device=self.name, ms=(time.time() - t0) * 1000,
                             via=self.via)
        took = (time.time() - t0) * 1000
        out = p.stdout.decode("utf-8", "replace")[:20000]
        err = p.stderr.decode("utf-8", "replace")[:4000]
        if p.returncode != 0:
            # 认证失败 / 主机不可达都是 255；要和「远端命令返回非 0」分开
            if p.returncode == 255:
                return R.unavailable("ssh.exec",
                                     f"{target} 连不上或认证失败：{err.strip() or '无输出'}",
                                     device=self.name, ms=took, via=self.via,
                                     data={"stdout": out, "stderr": err})
            return R.error("ssh.exec", f"远端返回 {p.returncode}：{err.strip()[:300]}",
                           device=self.name, ms=took, via=self.via,
                           data={"stdout": out, "stderr": err, "rc": p.returncode})
        return R.success("ssh.exec", self.name, via=self.via, ms=took, side_effect=True,
                         data={"target": target, "stdout": out, "stderr": err, "rc": 0})

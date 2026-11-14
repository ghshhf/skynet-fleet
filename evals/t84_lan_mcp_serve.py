#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T84 电脑侧局域网控制面（lan_mcp_server）：能不能真被手机连上、口令管不管用。

这条线最容易糊弄的地方是「本机自测全绿、手机一接就死」——因为三处都只在
跨设备时才暴露：

  ① **绑定 `::` 但不关 IPV6_V6ONLY**。Windows 上 AF_INET6 默认 V6ONLY=1，
     绑 :: 之后 IPv4 一律连不进来，而本机用 `::1` 测又是通的。手机主路是
     IPv4 兜底（没有公网 v6），于是「自测通过、手机连接超时」。
     本门禁用 **127.0.0.1（纯 v4）**连一次，专门锁这一格。
  ② **口令**。错口令必须被拒且要回话，不能静默断开（静默会让手机误判成
     网络问题，去换地址重试）。
  ③ **mDNS 广播里不能带口令**。把 token 塞 TXT 等于在同一 Wi-Fi 里公开，
     谁都能连；手机端 LanDiscovery 的注释也写着这一条。同时地址只该播
     「默认路由所在网段」——多播一块虚拟网卡地址，手机会先试它再超时回落。

全程回环，端口用 0 让系统分配，不碰 18765，不影响正在跑的那份服务。
"""
import json
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import lan_mcp_server as LMS      # noqa: E402

FAIL = []
TOKEN = "t84-test-token-0123456789abcdef"


def check(ok, msg):
    if ok:
        print("  \u2713 " + msg)
    else:
        print("  \u2717 " + msg)
        FAIL.append(msg)


def _line(sock, buf):
    while b"\n" not in buf[0]:
        c = sock.recv(65536)
        if not c:
            return None
        buf[0] += c
    line, rest = buf[0].split(b"\n", 1)
    buf[0] = rest
    return line.decode("utf-8", "replace")


def _hello(host, port, token):
    s = socket.create_connection((host, port), timeout=15)
    buf = [b""]
    s.sendall((json.dumps({"lan": "hello", "token": token}) + "\n").encode())
    first = _line(s, buf)
    return s, buf, (json.loads(first) if first else None)


def main():
    srv = LMS.LanMcpServer(token=TOKEN, port=0, readonly=False)
    if not srv.start():
        print("T84 红：服务起不来：%s" % srv.last_error)
        return 1
    port = srv.bound_port
    try:
        # ① 纯 IPv4 连接：绑 :: 却没关 V6ONLY 的话，这一格必红
        s4 = None
        try:
            s4, buf4, hello4 = _hello("127.0.0.1", port, TOKEN)
            check(hello4 is not None and hello4.get("lan") == "ok",
                  "IPv4 能连上并握手成功（IPV6_V6ONLY 已关）")
            caps = (hello4 or {}).get("caps") or 0
            check(caps > 0, "握手回执带能力数（%s 项）" % caps)
        except OSError as e:
            check(False, "IPv4 连不上（%s）：绑 :: 时没关 IPV6_V6ONLY" % e)
        finally:
            if s4:
                s4.close()

        # ② 口令错必须被拒，而且要回话
        s5, _, bad = _hello("127.0.0.1", port, "wrong-token")
        s5.close()
        check(bad is not None and bad.get("lan") == "error",
              "错口令被拒并回了明确的 error（不静默断开）")

        # ③ 真调一次能力：证明不止能握手，还能执行
        s6, buf6, _ = _hello("127.0.0.1", port, TOKEN)
        try:
            req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "host.monitor", "arguments": {}}}
            s6.sendall((json.dumps(req) + "\n").encode())
            line = _line(s6, buf6)
            r = json.loads(line) if line else {}
            text = ""
            for c in (r.get("result") or {}).get("content") or []:
                text += c.get("text", "")
            check("ok" in text and "cpu" in text,
                  "tools/call 真执行了（host.monitor 回显含 cpu）")
        finally:
            s6.close()

        # ④ mDNS 广播：不能带口令；地址只能是默认路由所在网段
        try:
            from zeroconf import Zeroconf, ServiceBrowser
        except Exception:
            print("  - 跳过 mDNS 检查（未装 zeroconf）")
        else:
            import time
            hits = []

            class L:
                def add_service(self, zc, t, n):
                    info = zc.get_service_info(t, n)
                    if info:
                        hits.append(info)

                def remove_service(self, *a):
                    pass

                def update_service(self, *a):
                    pass

            zc = Zeroconf()
            try:
                ServiceBrowser(zc, LMS.SERVICE_TYPE, L())
                zc2 = LMS._maybe_mdns(port, TOKEN)
                time.sleep(6)
                # 只认广播了本测试端口的那个实例。不过滤的话，本机正在跑的生产
                # 实例会被扫到，测试就拿别人的广播当自己的（假绿）。
                mine = [i for i in hits if i.port == port]
                props = {}
                for info in mine:
                    props.update({k.decode(): v.decode()
                                  for k, v in (info.properties or {}).items()})
                check(bool(mine), "mDNS 广播能被发现（端口 %d 的实例）" % port)
                check("token" not in props,
                      "广播里没有口令字段（只有 id/name/v）")
                for k in ("id", "name", "v"):
                    check(k in props, "广播带 %s 字段（手机端按这三个认对端）" % k)
                # 地址只该是默认路由所在网段：多播一块虚拟网卡地址会让手机先超时
                import socket as _sk
                prim = ""
                try:
                    _s = _sk.socket(_sk.AF_INET, _sk.SOCK_DGRAM)
                    _s.connect(("223.5.5.5", 53))
                    prim = _s.getsockname()[0]
                    _s.close()
                except OSError:
                    prim = ""
                if prim and mine:
                    prefix = prim.rsplit(".", 1)[0] + "."
                    bad = []
                    for info in mine:
                        for a in info.addresses or []:
                            ip = ".".join(str(b) for b in a)
                            if not ip.startswith(prefix) and not ip.startswith("127."):
                                bad.append(ip)
                    check(not bad, "广播地址只在默认路由网段（越界的：%s）" % (bad or "无"))
                if zc2:
                    zc2.close()
            finally:
                zc.close()
    finally:
        srv.stop()

    print()
    if FAIL:
        print("T84 红：%d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("T84 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())

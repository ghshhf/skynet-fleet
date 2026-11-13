#!/usr/bin/env python3
"""天网·电脑侧局域网客户端（电脑控手机直连）。

对应手机端的 LanServer：手机开 self_lan_start 后，电脑用这个脚本按同一协议
（hello 带 token → tools/call 一行 JSON-RPC）连上去，调手机的 self.* 工具。

这是「手机控电脑」之外的另一半：**电脑控手机**，也走局域网直连，不经过云中继。

依赖：纯标准库（socket / json / argparse）。IPv6 字面量自动加方括号，
链路本地 IPv6 必须带 %网卡，地址解析复用 Python 自带逻辑。

用法：
    python lan_client.py 192.168.1.7:18765 <token> self_status
    python lan_client.py fe80::1%wlan0:18765 <token> self_ui_dump '{"limit":30}'
    python lan_client.py [2408:xxxx]:18765 <token> self_ui_tap '{"query":"设置"}'
    python lan_client.py 192.168.1.7:18765 <token> --probe        # 只握手，看对端自述
    python lan_client.py 192.168.1.7:18765 <token> --speed 8      # 测速 8MB

注意：写操作（self_ui_tap / self_ui_text / self_lan_* 等）会真的在手机上执行，
回包丢了也可能已经执行——和手机端「写操作失败不换机重试」是同一原则。
"""
import argparse
import json
import socket
import sys
import time


def _parse_target(raw: str):
    raw = raw.strip()
    host = raw
    port = 18765
    if raw.startswith("["):
        end = raw.find("]")
        host = raw[1:end]
        rest = raw[end + 1:]
        if rest.startswith(":"):
            port = int(rest[1:])
    elif raw.count(":") == 1:
        h, p = raw.rsplit(":", 1)
        host, port = h, int(p)
    return host, port


def _resolve(host, port, per_addr_timeout=4.0, connect_timeout=8.0):
    # getaddrinfo 给 v4/v6 候选；逐地址试，避免一个坏地址拖死。
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception as e:
        raise RuntimeError(f"解析不了地址 {host}: {e}")
    errs = []
    for info in infos:
        af, socktype, _, _, sa = info
        s = None
        try:
            s = socket.socket(af, socktype)
            s.settimeout(min(connect_timeout, per_addr_timeout))
            s.connect(sa)
            return s
        except Exception as e:
            errs.append(str(e))
            if s:
                try:
                    s.close()
                except Exception:
                    pass
    raise RuntimeError("；".join(errs) or "连接失败")


def _read_line(sock):
    buf = bytearray()
    while True:
        b = sock.recv(1)
        if not b:
            return buf.decode("utf-8", "replace") if buf else None
        if b[0] == 0x0A:
            return buf.decode("utf-8", "replace")
        if len(buf) < 256 * 1024:
            buf += b


def _write_line(sock, s):
    sock.sendall((s + "\n").encode("utf-8"))


def hello(sock, token):
    _write_line(sock, json.dumps({"lan": "hello", "token": token}))
    line = _read_line(sock)
    if not line:
        raise RuntimeError("对端没回 hello（连接被关）")
    j = json.loads(line)
    if j.get("lan") != "ok":
        raise RuntimeError(j.get("err", "对端拒绝连接"))
    return j


def call(target, token, tool, args=None, timeout=30.0):
    host, port = _parse_target(target)
    s = _resolve(host, port)
    s.settimeout(timeout)
    try:
        h = hello(s, token)
        tools = h.get("tools", [])
        req = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}},
        }
        _write_line(s, json.dumps(req, ensure_ascii=True))
        line = _read_line(s)
        if not line:
            raise RuntimeError("对端没回包（连接被关）")
        return h, json.loads(line)
    finally:
        try:
            s.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="天网电脑侧局域网客户端（控手机）")
    ap.add_argument("target", help="对端地址：<IPv4> / [<IPv6>]:端口 / fe80::1%wlan0:18765")
    ap.add_argument("token", help="手机 self_lan_start 回执里的完整口令")
    ap.add_argument("tool", nargs="?", default=None, help="要调的工具名，如 self_status")
    ap.add_argument("args", nargs="?", default="{}", help="工具参数 JSON，默认 {}")
    ap.add_argument("--timeout", type=float, default=30.0, help="总超时秒数")
    ap.add_argument("--probe", action="store_true", help="只握手，打印对端自述（工具表等）")
    ap.add_argument("--speed", type=int, default=0, help="测速：传 MB 数（1–64）")
    args = ap.parse_args()

    host, port = _parse_target(args.target)
    s = None
    try:
        s = _resolve(host, port)
        s.settimeout(args.timeout)
        h = hello(s, args.token)
        if args.probe or (args.tool is None and args.speed == 0):
            print(json.dumps(h, ensure_ascii=False, indent=2))
            return 0
        if args.speed:
            mb = max(1, min(64, args.speed))
            n = mb * 1024 * 1024
            _write_line(s, json.dumps({"lan": "speed", "token": args.token,
                                       "bytes": n, "dir": "up"}))
            buf = bytearray()
            started = time.time()
            remain = n
            while remain > 0:
                chunk = s.recv(min(65536, remain))
                if not chunk:
                    break
                remain -= len(chunk)
            tail = _read_line(s)
            ms = (time.time() - started) * 1000.0
            print(f"测速：{mb}MB 上行 {ms/1000.0:.2f}s，" +
                  f"{n*8/ms/1e6:.1f} Mbps" + (f" | {tail}" if tail else ""))
            return 0
        try:
            argobj = json.loads(args.args) if args.args.strip() else {}
        except Exception as e:
            print(f"args 不是合法 JSON：{e}", file=sys.stderr)
            return 2
        req = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": args.tool, "arguments": argobj},
        }
        _write_line(s, json.dumps(req, ensure_ascii=True))
        line = _read_line(s)
        if not line:
            print("对端没回包（连接被关）", file=sys.stderr)
            return 1
        j = json.loads(line)
        # 压成一句人话，和手机侧 summarizeMcpReply 同口径
        if "error" in j:
            print(f"对端错误：{j['error'].get('message','未知')}", file=sys.stderr)
            return 1
        result = j.get("result", {})
        content = result.get("content", [])
        out = []
        for item in content:
            if item.get("type") == "text":
                out.append(item.get("text", ""))
            elif item.get("type") == "image":
                out.append(f"[图片 {item.get('mimeType','image/*')}，"
                           f"{len(item.get('data',''))} 字符 base64]")
        print("\n".join(out) if out else json.dumps(j, ensure_ascii=False))
        return 0
    except Exception as e:
        print(f"失败：{e}", file=sys.stderr)
        return 1
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())

"""天网·跨机 relay 客户端（在他机/局域网另一台电脑上跑，验证连通与代取）。

用法：
  python relay_client.py <host:port> <token> <url>
  python relay_client.py 192.168.1.50:9798 mytoken https://example.com
  python relay_client.py 127.0.0.1:9798 "" https://example.com   # 本机、无 token

协议（与 fleet.skynet_relay 的 SkyNetRelay 兼容）：
  发一行 JSON  {"type":"task","id","kind":"fetch","url","token?"}
  收一行 JSON  {"type":"result","id","ok","status","body_b64","text","error"}

连不上 / 超时 / unauthorized 都会明确打印，不静默失败。
零第三方依赖（纯 stdlib）。
"""
import json
import socket
import sys


def fetch(host, port, url, token="", timeout=25):
    rid = "cli-%d" % (int(__import__("time").time() * 1000) % 100000)
    req = {"type": "task", "id": rid, "kind": "fetch", "url": url}
    if token:
        req["token"] = token
    s = socket.create_connection((host, int(port)), timeout=timeout)
    try:
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    if not buf:
        return {"ok": False, "error": "无回执（对端可能已掉线或未启动）"}
    try:
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8", "ignore"))
    except Exception as e:
        return {"ok": False, "error": "回执解析失败: %s" % e}


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    hp, token, url = sys.argv[1], sys.argv[2], sys.argv[3]
    if ":" not in hp:
        print("host:port 格式不对：%s" % hp)
        return 2
    host, port = hp.rsplit(":", 1)
    try:
        r = fetch(host, port, url, token)
    except OSError as e:
        print("连接失败：%s" % e)
        print("（检查：对端 relay 是否在跑、端口是否放行、host:port 是否正确）")
        return 1
    if not r.get("ok"):
        print("取页失败：%s" % r.get("error", "未知"))
        return 1
    text = r.get("text", "") or ""
    print("status=%s chars=%d" % (r.get("status", 0), len(text)))
    print("-" * 40)
    print(text[:4000])
    if len(text) > 4000:
        print("…(截断，共 %d 字)" % len(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())

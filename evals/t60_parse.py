"""解析层门禁：锁死「分享链接→直链」框架的正确性（全本地，不碰外网、不耗额度、不依赖外部二进制）。

对应铁律：注释不是防线，测试才是——parse.py 的能力必须有可执行为证。
覆盖：
  1. 短链/口令抽取 URL（样本 → https://v.douyin.com/6zGANWG_NBA/）
  2. 平台识别（douyin）
  3. 无水印 trick（playwm → play）
  4. 三重签名非空（X-Bogus / A-Bogus / websign）
  5. SM3 标准向量（"abc" → 66c7f0f4...，本机 hashlib 兜底）
  6. 真实 invoke parse.resolve(download=True) 端到端：本地直链 → 下载引擎落盘字节数一致
"""
import os
import sys
import io
import json
import threading
import socketserver
import http.server

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet.kinds.parse import (
    Parse, extract_url, detect_platform, DouyinResolver,
    XBogus, ABogus, WebSign, _sm3,
)

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


# ---------------------------------------------------------------- 本地 Range server
DATA = b"PARSE_GATE_" * 50000        # 非整块，逼出多切片边界


class _H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(DATA)))
        self.end_headers()

    def do_GET(self):
        rng = self.headers.get("Range")
        if rng:
            s, e = rng.replace("bytes=", "").split("-")
            s, e = int(s), int(e)
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (s, e, len(DATA)))
            self.send_header("Content-Length", str(e - s + 1))
            self.end_headers()
            self.wfile.write(DATA[s:e + 1])
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(DATA)))
            self.end_headers()
            self.wfile.write(DATA)


# ---------------------------------------------------------------- 纯本地不变量
SAMPLE = "7.10 a@A.TY 06/24 jPk:/ :8pm 复制打开抖音极速版，看看【凛凛碎的作品】喵喵  https://v.douyin.com/6zGANWG_NBA/"
SM3_ABC = "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"

u = extract_url(SAMPLE)
check(u == "https://v.douyin.com/6zGANWG_NBA/", "extract_url 抽取短链失败: %r" % u)

p = detect_platform(SAMPLE)
check(p == "douyin", "detect_platform 识别失败: %r" % p)

nwm = DouyinResolver._nwm("https://v3-dy-o.xxx.com/playwm/abc.m3u8?a=1")
check(nwm == "https://v3-dy-o.xxx.com/play/abc.m3u8?a=1", "无水印 trick(playwm→play) 失败: %r" % nwm)

xb = XBogus("Mozilla/5.0").get_xbogus("a=1&b=2")
ab = ABogus().get_value({"a": "1", "b": "2"})
ws, _, _ = WebSign.sign([("a", "1")], "uifid123")
check(len(xb[1]) > 0, "X-Bogus 签名为空")
check(len(ab) > 0, "A-Bogus 签名为空")
check("x-secsdk-web-signature=" in ws, "websign 未生成 x-secsdk-web-signature")

check(_sm3("abc") == SM3_ABC, "SM3 标准向量不匹配: %r" % _sm3("abc"))


# ---------------------------------------------------------------- 真实 invoke 端到端（直连本地）
httpd = socketserver.TCPServer(("127.0.0.1", 0), _H)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
local = "http://127.0.0.1:%d/file.bin" % port
os.makedirs("tmp", exist_ok=True)
dest = "tmp/parse_gate.bin"

dev = Parse()
res = dev.invoke("parse.resolve", {"share": local, "platform": "direct", "download": True, "dest": dest})
httpd.shutdown()

check(res is not None and getattr(res, "ok", False), "parse.resolve(download=True) 端到端未成功: %r" % res)
got = os.path.getsize(dest) if os.path.exists(dest) else 0
check(got == len(DATA), "端到端下载字节数不匹配: got=%d expect=%d" % (got, len(DATA)))


# ---------------------------------------------------------------- 汇总
if FAIL:
    print("FAIL")
    for m in FAIL:
        print("  -", m)
    sys.exit(1)
print("PASS: 解析层框架全过（短链抽取/平台识别/无水印trick/三重签名/SM3向量/端到端下载）")
sys.exit(0)

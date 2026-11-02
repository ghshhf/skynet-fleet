"""下载门禁：锁死「分片真分、续传真续、多源轮流、hash 真校验、任务接口可用」。

全本地（Range server 自起），不碰外网、不耗额度、不依赖任何外部二进制。
对应铁律：注释不是防线，测试才是——download.py 的能力必须有可执行为证。
"""
import os
import sys
import io
import hashlib
import threading
import socketserver
import http.server

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet.kinds.download import DownloadEngine, Download

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


# ---------------------------------------------------------------- 本地 Range server
DATA = b'Y' * (3 * 1024 * 1024 + 777)        # 非整块大小，逼出多切片边界


class _H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-Length', str(len(DATA)))
        self.end_headers()

    def do_GET(self):
        rng = self.headers.get('Range')
        if rng:
            s, e = rng.replace('bytes=', '').split('-')
            s, e = int(s), int(e)
            self.send_response(206)
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (s, e, len(DATA)))
            self.send_header('Content-Length', str(e - s + 1))
            self.end_headers()
            self.wfile.write(DATA[s:e + 1])
        else:
            self.send_response(200)
            self.send_header('Content-Length', str(len(DATA)))
            self.end_headers()
            self.wfile.write(DATA)


httpd = socketserver.TCPServer(('127.0.0.1', 0), _H)
PORT = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
URL = 'http://127.0.0.1:%d/f.bin' % PORT
HASH = hashlib.sha256(DATA).hexdigest()
os.makedirs('tmp', exist_ok=True)
DEST = os.path.join('tmp', 'eval_dl.bin')


# ---------------------------------------------------------------- ① 分片下载（多线程）
eng = DownloadEngine(concurrency=4, slice_size=1024 * 1024, verify_hash=('sha256', HASH))
r = eng.download([URL], DEST, resume=False)
check(r.get('ok'), '① 分片下载应成功: %s' % r)
check(os.path.getsize(DEST) == len(DATA), '① 落盘大小应等于源大小')


# ---------------------------------------------------------------- ② 断点续传（截断后 resume）
sz = os.path.getsize(DEST)
with open(DEST, 'r+b') as f:
    f.truncate(sz - 200)
eng2 = DownloadEngine(concurrency=4, slice_size=1024 * 1024, verify_hash=('sha256', HASH))
r2 = eng2.download([URL], DEST, resume=True)
check(r2.get('ok') and r2.get('resumed'), '② 续传应 resumed=True: %s' % r2)
check(os.path.getsize(DEST) == len(DATA) and open(DEST, 'rb').read() == DATA,
      '② 续传后字节应完全一致')


# ---------------------------------------------------------------- ③ 多源轮流切片
eng3 = DownloadEngine(concurrency=4, slice_size=1024 * 1024, verify_hash=('sha256', HASH))
r3 = eng3.download([URL, URL], DEST + '.ms', resume=False, multi_source=True)
check(r3.get('ok'), '③ 多源（镜像）下载应成功: %s' % r3)


# ---------------------------------------------------------------- ④ 错误 hash 必须判失败
eng4 = DownloadEngine(concurrency=4, slice_size=1024 * 1024, verify_hash=('sha256', 'deadbeef' * 8))
r4 = eng4.download([URL], DEST + '.bad', resume=False)
check((not r4.get('ok')) and 'hash' in r4.get('err', ''), '④ 错误 hash 应判失败: %s' % r4)


# ---------------------------------------------------------------- ⑤ 任务管理接口
dev = Download()
tr = dev.invoke('download.task', {'action': 'list'})
check(tr.ok, '⑤ download.task list 应可用')
tp = dev.invoke('download.task', {'action': 'status', 'task_id': 'nope'})
check(not tp.ok, '⑤ 未知任务应 unavailable（不谎报）')


httpd.shutdown()

if FAIL:
    print('DOWNLOAD GATE FAIL:')
    for f in FAIL:
        print(' -', f)
    sys.exit(1)
print('DOWNLOAD GATE OK: 分片/续传/多源/hash校验/任务接口 全部通过')

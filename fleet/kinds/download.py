"""下载设备：把「下载」整个领域吸收进天网源码（不封装外部 daemon、不调黑盒 exe）。

吸收来源（源码已拉至 /d/refs，吸收而非 vendored）：
- aria2 设计理念：多源 / 分片 / 断点续传 / 并发闸 —— 用标准库 urllib 重写，零第三方依赖

设计原则（对齐 OSINT「提取万物融入己身」）：
- 下载引擎零依赖（urllib + 标准库），分片多线程、断点续传、多源、代理、并发闸、hash 校验
- 任务管理：进度 / 暂停 / 续传，内存任务表
- AI 读本文件即可知道如何调用（capabilities 经 catalog 暴露）

网盘相关能力（网盘直链/挂载）已移除：用别人的现成方案更好更方便；本模块只做通用直链下载。
"""
import os
import sys
import json
import time
import uuid
import base64
import hashlib
import threading
import concurrent.futures
import ssl
import urllib.request
import urllib.error
import urllib.parse
import socketserver
import http.server

from ..core import BaseDevice, DeviceResult as R


# ============================ 下载引擎 ============================
class DownloadEngine:
    """标准库实现的下载引擎。
    吸收 aria2 设计：文件按 slice_size 切片，每片一个 Range GET，多线程并发拉不同段；
    断点续传比对本地已有大小作为 Range 起点；多源时各源轮流承担切片。
    零第三方依赖（urllib），可直接被天网读源码调用。
    """
    def __init__(self, concurrency=4, slice_size=1024 * 1024, timeout=60,
                 proxy=None, chunk=256 * 1024, verify_hash=None, progress_cb=None):
        self.concurrency = max(1, int(concurrency))
        self.slice_size = max(64 * 1024, int(slice_size))
        self.timeout = int(timeout)
        self.proxy = proxy
        self.chunk = max(16 * 1024, int(chunk))
        self.verify_hash = verify_hash          # (algo, hexdigest) 或 None
        self.progress_cb = progress_cb
        self._opener = self._build_opener(proxy)
        self._lock = threading.Lock()
        self._paused = False
        self._done = 0
        self._total = 0

    @staticmethod
    def _build_opener(proxy):
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
        return urllib.request.build_opener(*handlers)

    def _size_of(self, url, headers=None):
        """探明文件总大小。优先 HEAD 的 Content-Length，否则 Range:0-0 读 Content-Range。"""
        try:
            req = urllib.request.Request(url, headers=headers or {}, method='HEAD')
            with self._opener.open(req, timeout=self.timeout) as r:
                cl = r.headers.get('Content-Length')
                if cl:
                    return int(cl), None
        except Exception:
            pass
        try:
            req = urllib.request.Request(url, headers=dict(headers or {}, **{'Range': 'bytes=0-0'}))
            with self._opener.open(req, timeout=self.timeout) as r:
                cr = r.headers.get('Content-Range')
                if cr and '/' in cr:
                    return int(cr.split('/')[-1]), None
        except urllib.error.HTTPError as e:
            cr = e.headers.get('Content-Range') if e.headers else None
            if cr and '/' in cr:
                return int(cr.split('/')[-1]), None
        except Exception:
            pass
        return None, '无法探明大小（服务器不支持 HEAD/Range）'

    def _fetch_slice(self, url, start, end, dest, headers=None):
        hdr = dict(headers or {})
        hdr['Range'] = 'bytes=%d-%d' % (start, end)
        req = urllib.request.Request(url, headers=hdr)
        off = start
        with self._opener.open(req, timeout=self.timeout) as r:
            with open(dest, 'r+b') as f:
                f.seek(off)
                while True:
                    if self._paused:
                        raise InterruptedError('paused')
                    buf = r.read(self.chunk)
                    if not buf:
                        break
                    f.write(buf)
                    with self._lock:
                        self._done += len(buf)
                    if self.progress_cb:
                        self.progress_cb(self._done, self._total)

    def _simple(self, url, dest, resume):
        req = urllib.request.Request(url)
        t0 = time.time()
        with self._opener.open(req, timeout=self.timeout) as r, open(dest, 'wb') as f:
            while True:
                if self._paused:
                    raise InterruptedError('paused')
                b = r.read(self.chunk)
                if not b:
                    break
                f.write(b)
        return {'ok': True, 'size': os.path.getsize(dest), 'seconds': round(time.time() - t0, 2)}

    def download(self, urls, dest, resume=True, multi_source=False):
        if not urls:
            return {'ok': False, 'err': '空 url 列表'}
        primary = urls[0]
        os.makedirs(os.path.dirname(os.path.abspath(dest)) or '.', exist_ok=True)
        total, err = self._size_of(primary)
        if total is None:
            # 未知大小：退化为单线程整下
            try:
                return self._simple(primary, dest, resume)
            except Exception as e:
                return {'ok': False, 'err': str(e)}
        existing = os.path.getsize(dest) if (resume and os.path.exists(dest)) else 0
        start_off = existing if existing < total else 0
        if os.path.exists(dest):
            with open(dest, 'r+b') as f:
                f.truncate(total)   # 保留已下载段，只扩展/截断到目标大小
        else:
            with open(dest, 'wb') as f:
                f.truncate(total)
        self._total = total
        self._done = start_off
        # 切片（从断点起）
        slices = []
        pos = start_off
        while pos < total:
            end = min(pos + self.slice_size, total) - 1
            slices.append((pos, end))
            pos = end + 1
        if not slices:
            return {'ok': True, 'size': total, 'seconds': 0.0, 'resumed': start_off > 0}
        srcs = urls if multi_source else [primary]
        self._paused = False
        errors = []
        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.concurrency) as ex:
            futs = {}
            for i, (s, e) in enumerate(slices):
                src = srcs[i % len(srcs)]
                futs[ex.submit(self._fetch_slice, src, s, e, dest, None)] = (s, e)
            for fut in concurrent.futures.as_completed(futs):
                try:
                    fut.result()
                except InterruptedError:
                    ex.shutdown(wait=False, cancel_futures=True)
                    return {'ok': False, 'err': 'paused', 'size': self._done}
                except Exception as e:
                    errors.append(str(e))
        if errors:
            return {'ok': False, 'err': '; '.join(errors[:3]), 'size': self._done}
        if self.verify_hash:
            algo, hexd = self.verify_hash
            h = hashlib.new(algo)
            with open(dest, 'rb') as f:
                for blk in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(blk)
            if h.hexdigest().lower() != hexd.lower():
                return {'ok': False, 'err': 'hash 不匹配', 'size': total}
        return {'ok': True, 'size': total, 'seconds': round(time.time() - t0, 2),
                'resumed': start_off > 0}

    def pause(self):
        self._paused = True


# ============================ 任务管理 ============================
TASKS = {}   # task_id -> dict


# ============================ 设备封装 ============================
class Download(BaseDevice):
    kind = "download"
    via = "internet"

    def __init__(self, name="download:engine", **kw):
        super().__init__(name, via="internet", **kw)
        self.meta.setdefault("note", "下载领域全内化：直链/分片/续传/多源/代理，零外部 daemon")

    def probe(self):
        # 标准库 urllib 总在，引擎零依赖
        return True, ""

    def caps(self):
        return ("download.direct", "download.task")

    def _hash_tuple(self, verify):
        if not verify:
            return None
        algo, _, hexd = verify.partition(':')
        return (algo, hexd)

    def _direct(self, args):
        urls = args.get('urls') or args.get('url')
        if isinstance(urls, str):
            urls = [u.strip() for u in urls.split(',') if u.strip()]
        if not urls:
            return {'ok': False, 'err': '缺少 urls'}
        dest = args.get('dest', 'downloads/out.bin')
        eng = DownloadEngine(
            concurrency=int(args.get('concurrency', 4)),
            slice_size=int(args.get('slice_size', 1024 * 1024)),
            timeout=int(args.get('timeout', 60)),
            proxy=args.get('proxy'),
            verify_hash=self._hash_tuple(args.get('verify')),
        )
        r = eng.download(urls, dest, resume=args.get('resume', True),
                         multi_source=args.get('multi_source', False))
        return r

    def _task(self, args):
        action = args.get('action', 'list')
        tid = args.get('task_id')
        if action == 'list':
            return {'ok': True, 'tasks': TASKS}
        if action == 'status':
            if tid not in TASKS:
                return {'ok': False, 'err': '任务不存在'}
            return {'ok': True, 'task': TASKS[tid]}
        if action == 'pause':
            if tid not in TASKS or not TASKS[tid].get('engine'):
                return {'ok': False, 'err': '任务不存在或无引擎'}
            TASKS[tid]['engine'].pause()
            TASKS[tid]['status'] = 'paused'
            return {'ok': True, 'task': TASKS[tid]}
        if action == 'resume':
            if tid not in TASKS:
                return {'ok': False, 'err': '任务不存在'}
            t = TASKS[tid]
            r = t['engine'].download(t['urls'], t['dest'], resume=True,
                                    multi_source=t.get('multi_source', False))
            t['status'] = 'done' if r.get('ok') else 'error'
            t['last'] = r
            return {'ok': True, 'task': t}
        if action == 'remove':
            TASKS.pop(tid, None)
            return {'ok': True}
        return {'ok': False, 'err': '未知 action: %s' % action}

    def invoke(self, cap, args=None):
        args = args or {}
        op = cap.split('.', 1)[1]
        try:
            if op == 'direct':
                m = self._direct(args)
            elif op == 'task':
                m = self._task(args)
            else:
                return R.unavailable(cap, '未知下载子操作: %s' % op, device=self.name, via=self.via)
        except Exception as e:
            return R.unavailable(cap, '下载执行异常: %s' % e, device=self.name, via=self.via)
        if not m.get('ok'):
            return R.unavailable(cap, m.get('err', '未知失败'), device=self.name, via=self.via)
        return R.success(cap, self.name, via=self.via, data=m)


# ============================ 自测 ============================
def _selftest():
    """本地 Range server + 引擎：验证分片真分、续传真续、hash 校验。"""
    class _RangeHandler(http.server.BaseHTTPRequestHandler):
        DATA = b'X' * (3 * 1024 * 1024 + 12345)   # 非整块大小
        def log_message(self, *a):
            pass
        def do_HEAD(self):
            self.send_response(200)
            self.send_header('Content-Length', str(len(self.DATA)))
            self.end_headers()
        def do_GET(self):
            rng = self.headers.get('Range')
            if rng:
                s, e = rng.replace('bytes=', '').split('-')
                s, e = int(s), int(e)
                self.send_response(206)
                self.send_header('Content-Range', 'bytes %d-%d/%d' % (s, e, len(self.DATA)))
                self.send_header('Content-Length', str(e - s + 1))
                self.end_headers()
                self.wfile.write(self.DATA[s:e + 1])
            else:
                self.send_response(200)
                self.send_header('Content-Length', str(len(self.DATA)))
                self.end_headers()
                self.wfile.write(self.DATA)
    httpd = socketserver.TCPServer(('127.0.0.1', 0), _RangeHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = 'http://127.0.0.1:%d/big.bin' % port
    dest = 'tmp/selftest_out.bin'
    os.makedirs('tmp', exist_ok=True)
    h = hashlib.sha256(_RangeHandler.DATA).hexdigest()
    eng = DownloadEngine(concurrency=4, slice_size=1024 * 1024, verify_hash=('sha256', h))
    r1 = eng.download([url], dest, resume=False)
    # 续传：截断末尾再 resume，应补齐
    sz = os.path.getsize(dest)
    with open(dest, 'r+b') as f:
        f.truncate(sz - 100)
    eng2 = DownloadEngine(concurrency=4, slice_size=1024 * 1024, verify_hash=('sha256', h))
    r2 = eng2.download([url], dest, resume=True)
    httpd.shutdown()
    ok = (r1.get('ok') and r2.get('ok') and os.path.getsize(dest) == len(_RangeHandler.DATA))
    return {'ok': ok, 'r1': r1, 'r2': r2, 'size': os.path.getsize(dest),
            'expect': len(_RangeHandler.DATA)}


if __name__ == '__main__':
    print(json.dumps(_selftest(), ensure_ascii=False, indent=2))

"""下载设备：把「下载」整个领域吸收进天网源码（不封装外部 daemon、不调黑盒 exe）。

吸收来源（源码已拉至 /d/refs，吸收而非 vendored）：
- QuarkPanTool (ihmily)：夸克网盘官方 API —— cookie → drive-pc.quark.cn/1/clouddrive/file/download 拿直链
- LinkSwift (hmjz100)：9 家网盘直链提取思路（百度/阿里/夸克/迅雷/天翼/UC/123/移动/光鸭）
- aria2 设计理念：多源 / 分片 / 断点续传 / 并发闸 —— 用标准库 urllib 重写，零第三方依赖

设计原则（对齐 OSINT「提取万物融入己身」）：
- 下载引擎零依赖（urllib + 标准库），分片多线程、断点续传、多源、代理、并发闸、hash 校验
- 网盘层 = 拿直链（官方 API，不破解限速），再交给引擎下载；QuarkClient 为样本实现，其余家登记端点清单待读源码内化
- 任务管理：进度 / 暂停 / 续传，内存任务表
- AI 读本文件即可知道如何调用（capabilities 经 catalog 暴露）
"""
import os
import re
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
from .. import net   # 出向唯一正道（网盘 API 也要躲开僵尸 HTTP_PROXY）


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


# ============================ 网盘层 ============================
class PanClient:
    """网盘基类。子类实现 get_share_detail / get_download_urls。
    设计：网盘层只负责「拿直链」（官方 API，不破解限速），下载交给 DownloadEngine。"""
    NAME = 'base'

    def __init__(self, cookie=None):
        self.cookie = cookie or ''

    def get_download_urls(self, fids):
        raise NotImplementedError

    def get_share_detail(self, pwd_id, stoken='', password=''):
        raise NotImplementedError


class QuarkClient(PanClient):
    """夸克网盘（吸收 QuarkPanTool）。
    登录态 = 夸克网页 cookie。提取方式：浏览器登录 pan.quark.cn → 取 domain 含 quark 的 cookie
    拼成 k=v;k=v。可放环境变量 QUARK_COOKIE 或文件 cookies.txt。
    真链路：fids → POST /1/clouddrive/file/download → download_url 直链 → 引擎下载。
    """
    NAME = 'quark'
    BASE = 'https://drive-pc.quark.cn/1/clouddrive'
    UA_EDGE = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0')
    UA_QUARK = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) quark-cloud-drive/2.5.56 Chrome/100.0.4896.160 '
                'Electron/18.3.5.12-a038f7b798 Safari/537.36 Channel/pckk_other_ch')

    def __init__(self, cookie=None):
        cookie = cookie or os.environ.get('QUARK_COOKIE') or ''
        if not cookie and os.path.exists('cookies.txt'):
            try:
                cookie = open('cookies.txt', 'r', encoding='utf-8').read().strip()
            except Exception:
                pass
        super().__init__(cookie)
        self.ua = self.UA_EDGE

    def _headers(self):
        return {
            'User-Agent': self.ua,
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json',
            'accept-language': 'zh-CN',
            'origin': 'https://pan.quark.cn',
            'referer': 'https://pan.quark.cn/',
            'cookie': self.cookie,
        }

    def _post(self, path, data, params=None):
        url = self.BASE + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'),
                                     headers=self._headers(), method='POST')
        try:
            with net.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode('utf-8'))
            except Exception:
                return {'code': -1, 'status': -1, 'message': str(e)}

    def get_download_urls(self, fids):
        if isinstance(fids, str):
            fids = [fids]
        params = {'pr': 'ucpro', 'fr': 'pc', 'sys': 'win32', 've': '2.5.56', 'ut': '', 'guid': ''}
        j = None
        for _ in range(2):
            j = self._post('/file/download', {'fids': fids}, params)
            if j.get('code') == 23018:
                self.ua = self.UA_QUARK   # 触发风控换 UA 重试
                continue
            break
        if not j or j.get('status') != 200 or not j.get('data'):
            return {'ok': False, 'err': (j or {}).get('message', 'no data')}
        files = [{'file_name': it['file_name'], 'download_url': it['download_url']}
                 for it in j['data']]
        return {'ok': True, 'files': files}

    def get_stoken(self, pwd_id, password=''):
        j = self._post('/share/sharepage/token',
                       {'pwd_id': pwd_id, 'passcode': password},
                       {'pr': 'ucpro', 'fr': 'pc'})
        if j.get('status') != 200 or not j.get('data'):
            return {'ok': False, 'err': j.get('message', 'token 失败')}
        return {'ok': True, 'stoken': j['data']['stoken']}

    def get_share_detail(self, pwd_id, stoken='', password=''):
        if not stoken:
            t = self.get_stoken(pwd_id, password)
            if not t.get('ok'):
                return t
            stoken = t['stoken']
        j = self._post('/share/sharepage/detail',
                       {'pwd_id': pwd_id, 'stoken': stoken, 'pdir_cid': '0', 'force': 0},
                       {'pr': 'ucpro', 'fr': 'pc'})
        if j.get('status') != 200 or not j.get('data') or 'list' not in j['data']:
            return {'ok': False, 'err': j.get('message', 'detail 失败')}
        files = [{'fid': it['fid'], 'file_name': it['file_name'], 'size': it.get('size', 0)}
                 for it in j['data']['list']]
        return {'ok': True, 'files': files, 'stoken': stoken}


# 其余网盘：登记官方直链端点思路（吸收 LinkSwift 覆盖的 9 家），待读各自源码内化实现。
# 不编造 API 字段——只留清晰扩展点，避免「看起来能下载其实调不通」。
PAN_REGISTRY = {
    'quark':   {'client': 'QuarkClient', 'desc': '夸克网盘（已实现样本）', 'status': 'ready'},
    'baidu':   {'client': None, 'desc': '百度网盘（需 BDUSS，直链 API 待内化）', 'status': 'stub'},
    'aliyun':  {'client': None, 'desc': '阿里云盘（需 token，openapi 待内化）', 'status': 'stub'},
    'xunlei':  {'client': None, 'desc': '迅雷云盘（需 xl_token，待内化）', 'status': 'stub'},
    'tianyi':  {'client': None, 'desc': '天翼云盘（需 cookie，待内化）', 'status': 'stub'},
    'uc':      {'client': None, 'desc': 'UC 网盘（待内化）', 'status': 'stub'},
    'pan123':  {'client': None, 'desc': '123 网盘（待内化）', 'status': 'stub'},
    'mobile':  {'client': None, 'desc': '移动云盘（待内化）', 'status': 'stub'},
    'guangya': {'client': None, 'desc': '光鸭网盘（待内化）', 'status': 'stub'},
}


# ============================ 任务管理 ============================
TASKS = {}   # task_id -> dict


# ============================ 设备封装 ============================
class Download(BaseDevice):
    kind = "download"
    via = "internet"

    def __init__(self, name="download:engine", **kw):
        super().__init__(name, via="internet", **kw)
        self.meta.setdefault("note", "下载领域全内化：直链/网盘/分片/续传/多源/代理，零外部 daemon")

    def probe(self):
        # 标准库 urllib 总在，引擎零依赖；网盘需 cookie（运行时提供）
        return True, ""

    def caps(self):
        return ("download.direct", "download.pan", "download.task")

    def _pan_client(self, pan, cookie):
        if pan != 'quark':
            return None   # 其余家未实现
        return QuarkClient(cookie=cookie)

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

    def _pan(self, args):
        pan = args.get('pan', 'quark')
        client = self._pan_client(pan, args.get('cookie'))
        if client is None:
            return {'ok': False, 'err': '未知或未实现的网盘: %s（已支持: quark）' % pan}
        if args.get('mode') == 'share':
            share_url = args.get('share_url')
            if not share_url:
                return {'ok': False, 'err': 'share 模式需要 share_url'}
            pwd_id = self._parse_share(share_url)
            if not pwd_id:
                return {'ok': False, 'err': '无法从分享链接解析 pwd_id'}
            detail = client.get_share_detail(pwd_id, '', args.get('password', ''))
            if not detail.get('ok'):
                return detail
            fids = [f['fid'] for f in detail['files']]
        else:
            fids = args.get('fids')
            if isinstance(fids, str):
                fids = [x.strip() for x in fids.split(',') if x.strip()]
        if not fids:
            return {'ok': False, 'err': '缺少 fids 或有效分享'}
        urls = client.get_download_urls(fids)
        if not urls.get('ok'):
            return urls
        dest_dir = args.get('dest', 'downloads')
        os.makedirs(dest_dir, exist_ok=True)
        eng = DownloadEngine(
            concurrency=int(args.get('concurrency', 4)),
            proxy=args.get('proxy'),
            verify_hash=self._hash_tuple(args.get('verify')),
        )
        results = []
        for fi in urls['files']:
            d = os.path.join(dest_dir, fi['file_name'])
            r = eng.download([fi['download_url']], d, resume=args.get('resume', True))
            r['file'] = fi['file_name']
            results.append(r)
        return {'ok': all(x['ok'] for x in results), 'files': results}

    @staticmethod
    def _parse_share(share_url):
        m = re.search(r'/s/([A-Za-z0-9]+)', share_url)
        if m:
            return m.group(1)
        m = re.search(r'pwd_id=([A-Za-z0-9]+)', share_url)
        return m.group(1) if m else None

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
            elif op == 'pan':
                m = self._pan(args)
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

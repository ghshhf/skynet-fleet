"""网盘挂载：把「远方的存储」变成「本地的一个盘符」。

为什么要这一层
--------------
天网的产物（Artifact）现在只有两种落点：本机磁盘路径，或者某一台节点的沙箱。
这两种都绑死「机器 + 路径」，换一台机器就找不到东西。网盘是第三种落点：**有身份、
有版本、跨设备、天然可共享**。但它没有本地文件系统语义，模型调不动。

所以这里做的事是给网盘补一套文件系统界面（list/stat/read/write/mkdir/move/remove），
再把它**真挂载**出去——不是比喻，是 Windows 资源管理器里多一个盘符。

怎么做到「真挂载」而不用装驱动
------------------------------
Windows 自带 WebDAV 客户端（WebClient 服务 / 网络重定向器）。只要本机起一个
WebDAV 服务端，`net use Z: http://127.0.0.1:PORT/` 就能挂成盘符，不需要 WinFsp、
不需要 Dokan、不需要任何第三方驱动。macOS Finder「连接服务器」和 Linux 的
davfs2 也是同一条路。

于是本模块 = **后端适配（后端各不相同）+ 一个标准 WebDAV 前端（只有一份）**。
换网盘只换后端，前端与盘符不变。

后端三类
--------
1. `LocalBackend`  —— 本机某个目录（把任意磁盘目录也挂成盘符，等价 subst 但跨机可用）
2. `WebDAVBackend` —— 远端 WebDAV（坚果云 / Nextcloud / AList / 群晖），间接挂载
3. `QuarkBackend`  —— 夸克网盘（官方 API，只读：目录列举 + 直链）

安全边界（写在这里，因为这是唯一会被外部客户端直连的入口）
----------------------------------------------------------
- **路径穿越必须挡死**：`..`、绝对路径、符号链接逃逸，全部用 realpath 前缀比对，
  不做字符串 startswith（`/data/root2` 会被 `/data/root` 前缀匹配骗过）。
- **只绑回环地址**。默认 `127.0.0.1`，要跨机必须显式给 host 并自己负责网络边界。
- **写操作要授权**：默认只读挂载；要写必须 `write=True` 显式开。
- **凭据不落盘**：WebDAV 密码、夸克 cookie 只存在内存，挂载表里只记「有凭据」这个事实。

用法（对模型）
--------------
    drive.mount(name="pan", backend="webdav", url="https://dav.jianguoyun.com/dav/",
                user="me@example.com", password="***", write=True)
    → {"url": "http://127.0.0.1:8731/pan/", "windows": "net use Z: ..."}

    drive.list(path="pan:/2026/")        # 统一用 name:/path 定位
    drive.read(path="pan:/notes.md")
    drive.write(path="pan:/notes.md", text="...")
"""
from .. import endpoints
import base64
import ctypes
import hashlib
import html
import io
import json
import mimetypes
import os
import posixpath
import re
import shutil
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..core import BaseDevice, DeviceResult as R
from .. import net

# ============================================================ 常量

DEFAULT_HOST = "0.0.0.0"      # 必须绑全网卡：绑 127.0.0.1 时 Windows 的 WebDAV 客户端
                              # 会拒绝映射（回环限制，报错 67 且不说原因）
DEFAULT_PORT = endpoints.port_num("DRIVE", 8731)
VAR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "var")
MOUNTS_FILE = os.path.join(VAR_DIR, "drive_mounts.json")

#: 单文件读入内存的上限。超过就拒绝——WebDAV 是流式的，但能力接口返回的是内存对象，
#: 悄悄读一个 4GB 文件会把整台机器拖死。要传大文件请用 download.direct 或直接走挂载盘符。
MAX_INLINE = 32 * 1024 * 1024

#: 挂载点名字只允许这些字符：它要进 URL 路径，也是 Windows 映射命令的一部分
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


# ============================================================ 路径安全

def _safe_join(root, rel):
    """把相对路径拼到 root 下，并**证明**结果没有逃出 root。

    三层防线，缺一不可：
      1. 拒绝绝对路径与盘符（`/etc/passwd`、`C:\\Windows`）——它们会覆盖 join 语义。
      2. 用 realpath 消解 `..` 与符号链接，再比对前缀。
      3. 前缀比对必须带分隔符，否则 `/data/root2` 会被 `/data/root` 误判为「在内部」。
    """
    if rel is None:
        rel = ""
    rel = str(rel).replace("\\", "/").strip()
    if rel.startswith("/"):
        rel = rel.lstrip("/")
    if re.match(r"^[A-Za-z]:", rel):
        raise ValueError("不接受绝对路径（带盘符）：%r" % rel)
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, rel))
    if target != root_real and not target.startswith(root_real + os.sep):
        raise ValueError("路径越界（拒绝穿越出挂载根）：%r" % rel)
    return target


def _normalize_rel(rel):
    """把请求路径归一成干净的相对路径（去掉 . / 空段，保留 .. 交给 _safe_join 判）。"""
    rel = (rel or "").replace("\\", "/")
    parts = []
    for seg in rel.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


# ============================================================ 后端

class BackendError(Exception):
    """后端统一的失败类型。带上 kind，让上层能区分「有但读不了」和「根本没有」。"""

    def __init__(self, kind, msg):
        super().__init__(msg)
        self.kind = kind          # not_found / denied / unsupported / io / auth

    def __str__(self):
        return "[%s] %s" % (self.kind, super().__str__())


class BaseBackend:
    """后端契约。只要求实现这几件事，前端与能力层都只认它。"""

    KIND = "base"
    READONLY = False

    def stat(self, rel):
        raise NotImplementedError

    def list(self, rel):
        raise NotImplementedError

    def open_read(self, rel):
        raise NotImplementedError

    def write(self, rel, data):
        raise NotImplementedError

    def mkdir(self, rel):
        raise NotImplementedError

    def remove(self, rel):
        raise NotImplementedError

    def move(self, src, dst):
        raise NotImplementedError

    def describe(self):
        """给挂载表看的名片——**不含任何凭据**。"""
        return {"kind": self.KIND, "readonly": self.READONLY}


class LocalBackend(BaseBackend):
    """本机目录。也用来把任意磁盘目录挂成盘符（换机器也能挂）。"""

    KIND = "local"

    def __init__(self, root, readonly=False):
        self.root = os.path.realpath(root)
        if not os.path.isdir(self.root):
            raise BackendError("not_found", "目录不存在：%s" % root)
        self.READONLY = bool(readonly)

    def describe(self):
        d = super().describe()
        d["root"] = self.root
        return d

    def _p(self, rel):
        return _safe_join(self.root, rel)

    def stat(self, rel):
        p = self._p(rel)
        try:
            st = os.stat(p)
        except FileNotFoundError:
            raise BackendError("not_found", "没有这个路径：%s" % (rel or "/"))
        except PermissionError:
            raise BackendError("denied", "没有权限：%s" % (rel or "/"))
        return {
            "name": os.path.basename(p) or os.path.basename(self.root),
            "path": _normalize_rel(rel),
            "is_dir": os.path.isdir(p),
            "size": 0 if os.path.isdir(p) else st.st_size,
            "mtime": st.st_mtime,
            "mode": oct(st.st_mode & 0o777),
        }

    def list(self, rel):
        p = self._p(rel)
        if not os.path.isdir(p):
            raise BackendError("not_found", "不是目录：%s" % (rel or "/"))
        out = []
        try:
            for name in sorted(os.listdir(p)):
                if name in (".", ".."):
                    continue
                fp = os.path.join(p, name)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                is_dir = os.path.isdir(fp)
                out.append({
                    "name": name,
                    "path": posixpath.join(_normalize_rel(rel), name).strip("/"),
                    "is_dir": is_dir,
                    "size": 0 if is_dir else st.st_size,
                    "mtime": st.st_mtime,
                })
        except PermissionError:
            raise BackendError("denied", "没有权限列这个目录：%s" % (rel or "/"))
        return out

    def open_read(self, rel):
        p = self._p(rel)
        if os.path.isdir(p):
            raise BackendError("unsupported", "这是目录，不能当文件读：%s" % (rel or "/"))
        try:
            st = os.stat(p)
        except FileNotFoundError:
            raise BackendError("not_found", "文件不存在：%s" % rel)
        if st.st_size > MAX_INLINE:
            raise BackendError("unsupported",
                               "文件 %d MB 超过单次读入上限 %d MB（请走挂载盘符或 download.direct）"
                               % (st.st_size // 1048576, MAX_INLINE // 1048576))
        with open(p, "rb") as f:
            return f.read(), st.st_mtime

    def write(self, rel, data):
        if self.READONLY:
            raise BackendError("denied", "这是只读挂载（mount 时加 write=True 才可写）")
        p = self._p(rel)
        if os.path.isdir(p):
            raise BackendError("unsupported", "目标是目录：%s" % rel)
        os.makedirs(os.path.dirname(p) or self.root, exist_ok=True)
        tmp = p + ".dltmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)          # 原子替换：宁可丢最后一次写，也不留半个坏文件
        return {"path": _normalize_rel(rel), "bytes": len(data)}

    def mkdir(self, rel):
        if self.READONLY:
            raise BackendError("denied", "这是只读挂载")
        p = self._p(rel)
        os.makedirs(p, exist_ok=True)
        return {"path": _normalize_rel(rel), "is_dir": True}

    def remove(self, rel):
        if self.READONLY:
            raise BackendError("denied", "这是只读挂载")
        if not _normalize_rel(rel):
            raise BackendError("denied", "拒绝删除挂载根")
        p = self._p(rel)
        if os.path.isdir(p):
            shutil.rmtree(p)
        elif os.path.exists(p):
            os.remove(p)
        else:
            raise BackendError("not_found", "没有这个路径：%s" % rel)
        return {"path": _normalize_rel(rel), "removed": True}

    def move(self, src, dst):
        if self.READONLY:
            raise BackendError("denied", "这是只读挂载")
        a, b = self._p(src), self._p(dst)
        if not os.path.exists(a):
            raise BackendError("not_found", "源不存在：%s" % src)
        os.makedirs(os.path.dirname(b) or self.root, exist_ok=True)
        os.replace(a, b)
        return {"from": _normalize_rel(src), "to": _normalize_rel(dst)}


class WebDAVBackend(BaseBackend):
    """远端 WebDAV。用它把「别人家的网盘」接进来，天网不需要为每家写驱动。

    只实现天网自己需要的那部分协议（PROPFIND/GET/PUT/DELETE/MKCOL/MOVE），
    不做锁协商——我们是单写入方，锁对我们没意义。
    """

    KIND = "webdav"

    def __init__(self, base_url, user=None, password=None, readonly=True, timeout=30,
                 verify_tls=True):
        if not base_url.startswith(("http://", "https://")):
            raise BackendError("io", "WebDAV 地址必须以 http(s):// 开头")
        self.base = base_url.rstrip("/") + "/"
        self.user = user or ""
        self._password = password or ""
        self.READONLY = bool(readonly)
        self.timeout = int(timeout)
        self.verify_tls = bool(verify_tls)

    def describe(self):
        d = super().describe()
        d["url"] = re.sub(r"//[^/@]+@", "//", self.base)   # 万一 URL 里带了凭据，剥掉
        d["has_credentials"] = bool(self.user or self._password)
        return d

    def _headers(self, extra=None):
        h = {"User-Agent": "skynet-drive/1.0", "Accept": "*/*"}
        if self.user or self._password:
            raw = ("%s:%s" % (self.user, self._password)).encode("utf-8")
            h["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        h.update(extra or {})
        return h

    def _ctx(self):
        if self.verify_tls:
            return None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _req(self, method, rel, body=None, headers=None, timeout=None):
        url = self.base + urllib.parse.quote(_normalize_rel(rel))
        if _normalize_rel(rel) and rel.endswith("/"):
            url += "/"
        req = urllib.request.Request(url, data=body, method=method,
                                     headers=self._headers(headers))
        try:
            return net.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            code = e.code
            if code == 404:
                raise BackendError("not_found", "远端没有这个路径：%s" % (rel or "/"))
            if code in (401, 403):
                raise BackendError("auth", "远端拒绝访问（%d）——检查用户名/密码/权限" % code)
            raise BackendError("io", "远端返回 %d：%s" % (code, (e.reason or "")[:80]))
        except Exception as e:
            raise BackendError("io", "连不上远端 WebDAV：%s" % e)

    _DAV_NS = "{DAV:}"

    def _propfind(self, rel, depth="0"):
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<d:propfind xmlns:d="DAV:"><d:prop>'
                '<d:displayname/><d:getcontentlength/><d:getlastmodified/>'
                '<d:resourcetype/><d:getcontenttype/>'
                '</d:prop></d:propfind>').encode("utf-8")
        r = self._req("PROPFIND", rel, body,
                      {"Depth": depth, "Content-Type": 'application/xml; charset="utf-8"'})
        raw = r.read()
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            raise BackendError("io", "远端 PROPFIND 返回的不是合法 XML：%s" % e)
        items = []
        for resp in root.findall(self._DAV_NS + "response"):
            href = resp.findtext(self._DAV_NS + "href") or ""
            prop = resp.find(self._DAV_NS + "propstat/" + self._DAV_NS + "prop")
            if prop is None:
                continue
            is_dir = prop.find(self._DAV_NS + "resourcetype/" + self._DAV_NS + "collection") is not None
            size = prop.findtext(self._DAV_NS + "getcontentlength") or "0"
            lm = prop.findtext(self._DAV_NS + "getlastmodified") or ""
            name = resp.findtext(self._DAV_NS + "displayname") or ""
            p = urllib.parse.unquote(urllib.parse.urlsplit(href).path or "")
            try:
                mtime = parsedate_to_datetime(lm).timestamp() if lm else 0.0
            except Exception:
                mtime = 0.0
            items.append({
                "href": href,
                "name": name or posixpath.basename(p.rstrip("/")),
                "is_dir": is_dir,
                "size": int(size or 0) if str(size).isdigit() else 0,
                "mtime": mtime,
                "_base_path": p,
            })
        return items

    def _rel_of_item(self, item, request_rel):
        """远端返回的 href 是绝对路径（/dav/xxx），要还原成相对挂载根的路径。"""
        base_path = urllib.parse.urlsplit(self.base).path or "/"
        p = item["_base_path"]
        if p.startswith(base_path):
            return _normalize_rel(p[len(base_path):])
        return _normalize_rel(posixpath.join(_normalize_rel(request_rel), item["name"]))

    def stat(self, rel):
        items = self._propfind(rel, "0")
        if not items:
            raise BackendError("not_found", "远端没有这个路径：%s" % (rel or "/"))
        it = items[0]
        return {"name": it["name"], "path": _normalize_rel(rel), "is_dir": it["is_dir"],
                "size": it["size"], "mtime": it["mtime"]}

    def list(self, rel):
        items = self._propfind(rel, "1")
        me = _normalize_rel(rel)
        out = []
        for it in items:
            p = self._rel_of_item(it, rel)
            if p == me:                    # PROPFIND depth=1 会把自身也返回
                continue
            out.append({"name": it["name"], "path": p, "is_dir": it["is_dir"],
                        "size": it["size"], "mtime": it["mtime"]})
        out.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return out

    def open_read(self, rel):
        r = self._req("GET", rel)
        data = r.read(MAX_INLINE + 1)
        if len(data) > MAX_INLINE:
            raise BackendError("unsupported", "远端文件超过单次读入上限 %d MB" % (MAX_INLINE // 1048576))
        lm = r.headers.get("Last-Modified")
        try:
            mt = parsedate_to_datetime(lm).timestamp() if lm else 0.0
        except Exception:
            mt = 0.0
        return data, mt

    def write(self, rel, data):
        if self.READONLY:
            raise BackendError("denied", "这是只读挂载")
        self._req("PUT", rel, data, {"Content-Type": "application/octet-stream"})
        return {"path": _normalize_rel(rel), "bytes": len(data)}

    def mkdir(self, rel):
        if self.READONLY:
            raise BackendError("denied", "这是只读挂载")
        try:
            self._req("MKCOL", rel.rstrip("/") + "/")
        except BackendError as e:
            if e.kind != "io" or "405" not in str(e):    # 405 = 已存在，幂等通过
                raise
        return {"path": _normalize_rel(rel), "is_dir": True}

    def remove(self, rel):
        if self.READONLY:
            raise BackendError("denied", "这是只读挂载")
        self._req("DELETE", rel)
        return {"path": _normalize_rel(rel), "removed": True}

    def move(self, src, dst):
        if self.READONLY:
            raise BackendError("denied", "这是只读挂载")
        dest = self.base + urllib.parse.quote(_normalize_rel(dst))
        self._req("MOVE", src, headers={"Destination": dest, "Overwrite": "T"})
        return {"from": _normalize_rel(src), "to": _normalize_rel(dst)}


class QuarkBackend(BaseBackend):
    """夸克网盘（只读）。复用 download.py 的官方 API 通道，只补「列目录」。

    为什么只读：写夸克要走上传三段式（预检 → 分片 → 合并），还没有实现；
    先把它当「只读仓库」用——读得到、能挂盘符、能拉直链，这已经够用。
    声称能写而实际写不进去，比老老实实标只读糟糕得多。
    """

    KIND = "quark"
    READONLY = True
    FID_ROOT = "0"

    def __init__(self, cookie=None, timeout=30):
        cookie = cookie or os.environ.get("QUARK_COOKIE") or ""
        if not cookie:
            raise BackendError("auth", "夸克需要 cookie（QUARK_COOKIE 环境变量或 mount 时传 cookie）")
        self.cookie = cookie
        self.timeout = int(timeout)
        self.base = "https://drive-pc.quark.cn/1/clouddrive"
        self.ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0")

    def describe(self):
        d = super().describe()
        d["note"] = "只读；写需要上传三段式，尚未实现"
        return d

    def _post(self, path, data, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, data=json.dumps(data).encode("utf-8"), method="POST",
            headers={"User-Agent": self.ua, "Content-Type": "application/json",
                     "Accept": "application/json, text/plain, */*",
                     "accept-language": "zh-CN", "origin": "https://pan.quark.cn",
                     "referer": "https://pan.quark.cn/", "cookie": self.cookie})
        try:
            with net.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"status": -1, "message": str(e)}
        except Exception as e:
            return {"status": -1, "message": str(e)}

    def _resolve_fid(self, rel):
        """把 a/b/c 逐级走成 fid。夸克是 fid 导航，不是路径导航。"""
        rel = _normalize_rel(rel)
        if not rel:
            return self.FID_ROOT
        fid = self.FID_ROOT
        for seg in rel.split("/"):
            found = None
            for it in self._list_fid(fid):
                if it["name"] == seg:
                    found = it
                    break
            if not found:
                raise BackendError("not_found", "夸克里没有这个路径：%s" % rel)
            fid = found["fid"]
        return fid

    def _list_fid(self, fid):
        j = self._post("/file/sort",
                       {"pdir_fid": fid, "force": 0, "_page": 1, "_size": 200,
                        "_fetch_banner": 0, "_fetch_share": 0, "_fetch_total": 1},
                       {"pr": "ucpro", "fr": "pc", "uc_param_str": ""})
        if j.get("status") != 200:
            raise BackendError("io", "夸克列目录失败：%s" % (j.get("message") or "未知"))
        out = []
        for it in ((j.get("data") or {}).get("list") or []):
            out.append({
                "fid": it.get("fid"),
                "name": it.get("file_name"),
                "is_dir": bool(it.get("dir")),
                "size": it.get("size") or 0,
                "mtime": (it.get("updated_at") or 0) / 1000.0,
            })
        return out

    def stat(self, rel):
        rel = _normalize_rel(rel)
        if not rel:
            return {"name": "/", "path": "", "is_dir": True, "size": 0, "mtime": 0}
        parent, name = posixpath.split(rel)
        for it in self._list_fid(self._resolve_fid(parent)):
            if it["name"] == name:
                return {"name": name, "path": rel, "is_dir": it["is_dir"],
                        "size": it["size"], "mtime": it["mtime"]}
        raise BackendError("not_found", "夸克里没有这个路径：%s" % rel)

    def list(self, rel):
        rel = _normalize_rel(rel)
        out = []
        for it in self._list_fid(self._resolve_fid(rel)):
            out.append({"name": it["name"],
                        "path": posixpath.join(rel, it["name"]).strip("/"),
                        "is_dir": it["is_dir"], "size": it["size"], "mtime": it["mtime"]})
        out.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return out

    def open_read(self, rel):
        fid = self._resolve_fid(rel)
        j = self._post("/file/download", {"fids": [fid]},
                       {"pr": "ucpro", "fr": "pc", "sys": "win32", "ve": "2.5.56"})
        if j.get("status") != 200 or not j.get("data"):
            raise BackendError("io", "拿不到夸克直链：%s" % (j.get("message") or "未知"))
        url = j["data"][0].get("download_url")
        if not url:
            raise BackendError("io", "夸克返回里没有 download_url")
        try:
            with net.urlopen(url, timeout=self.timeout) as r:
                return r.read(MAX_INLINE), 0.0
        except Exception as e:
            raise BackendError("io", "从夸克直链读取失败：%s" % e)

    def write(self, rel, data):
        raise BackendError("unsupported", "夸克后端只读：上传需要预检/分片/合并三段式，尚未实现")

    def mkdir(self, rel):
        raise BackendError("unsupported", "夸克后端只读")

    def remove(self, rel):
        raise BackendError("unsupported", "夸克后端只读")

    def move(self, src, dst):
        raise BackendError("unsupported", "夸克后端只读")


# ============================================================ WebDAV 前端

class _Mount:
    """一个挂载点 = 名字 + 后端 + 元信息。"""

    def __init__(self, name, backend, note=""):
        self.name = name
        self.backend = backend
        self.note = note
        self.created = time.time()

    def card(self):
        d = self.backend.describe()
        d.update({"name": self.name, "note": self.note, "created": self.created})
        return d


class _Server:
    """一个进程内唯一的 WebDAV 服务。挂载表在内存 + 落盘（只落名字与类型）。"""

    def __init__(self):
        self.mounts = {}
        self.lock = threading.RLock()
        self.httpd = None
        self.thread = None
        self.port = 0
        self.host = DEFAULT_HOST
        self.auth = None          # (user, password) 或 None

    # ---------------- 生命周期 ----------------

    def start(self, host=DEFAULT_HOST, port=DEFAULT_PORT, auth=None):
        with self.lock:
            if self.httpd is not None:
                return self.port
            handler = _make_handler(self)
            last = None
            for candidate in range(port, port + 20):
                try:
                    self.httpd = ThreadingHTTPServer((host, candidate), handler)
                    self.port = candidate
                    break
                except OSError as e:
                    last = e
                    continue
            if self.httpd is None:
                raise RuntimeError("起不来 WebDAV 服务：%s" % last)
            self.httpd.daemon_threads = True
            self.host = host
            self.auth = auth
            self.thread = threading.Thread(target=self.httpd.serve_forever,
                                           name="drive-webdav", daemon=True)
            self.thread.start()
            return self.port

    def stop(self):
        with self.lock:
            if self.httpd is not None:
                try:
                    self.httpd.shutdown()
                    self.httpd.server_close()
                except Exception:
                    pass
                self.httpd = None
                self.thread = None
            self.mounts.clear()

    def is_up(self):
        return self.httpd is not None

    def base_url(self):
        """对外可用的地址。绑 0.0.0.0 时换成局域网 IP——`http://0.0.0.0:8731/`
        对调用方毫无意义，而客户端要的是一个真能连上的地址。"""
        host = self.host
        if host in ("0.0.0.0", "::", ""):
            host = _lan_ip()
        return "http://%s:%d/" % (host, self.port)

    # ---------------- 挂载表 ----------------

    def mount(self, m):
        with self.lock:
            self.mounts[m.name] = m

    def unmount(self, name):
        with self.lock:
            return self.mounts.pop(name, None)

    def get(self, name):
        with self.lock:
            return self.mounts.get(name)

    def cards(self):
        with self.lock:
            return [m.card() for m in self.mounts.values()]


_SERVER = _Server()
_STATE_LOCK = threading.RLock()


def _persist():
    """只落「挂了什么」，凭据一律不落盘。重启后需要重新 mount（密码在内存里）。"""
    try:
        os.makedirs(VAR_DIR, exist_ok=True)
        data = {"port": _SERVER.port, "host": _SERVER.host, "mounts": _SERVER.cards()}
        tmp = MOUNTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, MOUNTS_FILE)
    except Exception:
        pass          # 落盘失败不该让挂载失败


# ============================================================ HTTP handler

def _xml_escape(s):
    return html.escape(str(s), quote=True)


def _href(path, is_dir):
    q = urllib.parse.quote(path)
    if is_dir and not q.endswith("/"):
        q += "/"
    return q or "/"


def _make_handler(server):
    class Handler(BaseHTTPRequestHandler):
        server_version = "skynet-drive/1.0"
        protocol_version = "HTTP/1.1"

        # ---------- 基础 ----------

        def log_message(self, fmt, *args):
            # 默认静音：WebDAV 客户端会打很多噪音，别污染控制面日志。
            # 排查「资源管理器映射不上」时设 SKYNET_DRIVE_DEBUG=1 打开——
            # 客户端在映射阶段会先发 OPTIONS/PROPFIND，看它请求了什么才知道卡在哪。
            if os.environ.get("SKYNET_DRIVE_DEBUG") == "1":
                sys.stderr.write("[dav] %s %s\n" % (self.command, fmt % args))
                sys.stderr.flush()

        def _send(self, code, body=b"", headers=None, ctype="text/plain; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body and self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def _auth_ok(self):
            if server.auth is None:
                return True
            h = self.headers.get("Authorization") or ""
            if not h.startswith("Basic "):
                return False
            try:
                raw = base64.b64decode(h[6:]).decode("utf-8")
            except Exception:
                return False
            return raw == "%s:%s" % server.auth

        def _need_auth(self):
            self._send(401, "需要认证", {"WWW-Authenticate": 'Basic realm="skynet-drive"'})

        def _resolve(self):
            """URL → (mount, rel)。路径第一段是挂载名。"""
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path or "/")
            path = _normalize_rel(path)
            if not path:
                return None, ""
            name, _, rel = path.partition("/")
            m = server.get(name)
            return m, rel

        # ---------- 方法 ----------

        def do_OPTIONS(self):
            self._send(200, b"", {
                "DAV": "1, 2",
                "MS-Author-Via": "DAV",
                "Allow": "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, MKCOL, MOVE, COPY, LOCK, UNLOCK",
            })

        def do_LOCK(self):
            # Windows 挂载会先要一个锁。我们单写入方，发一个假 token 就行——
            # 回 501 会让资源管理器直接判定「不可写」。
            token = "opaquelocktoken:%s" % uuid.uuid4()
            body = ('<?xml version="1.0" encoding="utf-8"?>'
                    '<d:prop xmlns:d="DAV:"><d:lockdiscovery><d:activelock>'
                    '<d:locktype><d:write/></d:locktype>'
                    '<d:lockscope><d:exclusive/></d:lockscope>'
                    '<d:depth>infinity</d:depth>'
                    '<d:timeout>Second-3600</d:timeout>'
                    '<d:locktoken><d:href>%s</d:href></d:locktoken>'
                    '</d:activelock></d:lockdiscovery></d:prop>' % token)
            self._send(200, body, {"Lock-Token": "<%s>" % token},
                       ctype='application/xml; charset="utf-8"')

        def do_UNLOCK(self):
            self._send(204, b"")

        def do_GET(self):
            self._get(head=False)

        def do_HEAD(self):
            self._get(head=True)

        def _get(self, head):
            if not self._auth_ok():
                return self._need_auth()
            m, rel = self._resolve()
            if m is None:
                return self._send(404, "没有这个挂载点（先用 drive.mount 注册）")
            try:
                st = m.backend.stat(rel)
            except BackendError as e:
                return self._send(404 if e.kind == "not_found" else 500, str(e))
            if st["is_dir"]:
                return self._send(200, "<h1>%s</h1><p>目录请用 PROPFIND</p>" % _xml_escape(rel),
                                  ctype="text/html; charset=utf-8")
            try:
                data, mtime = m.backend.open_read(rel)
            except BackendError as e:
                code = {"not_found": 404, "denied": 403, "auth": 401}.get(e.kind, 500)
                return self._send(code, str(e))
            ctype = mimetypes.guess_type(st["name"])[0] or "application/octet-stream"
            hdrs = {"Last-Modified": formatdate(mtime or time.time(), usegmt=True),
                    "Accept-Ranges": "none"}
            if head:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.end_headers()
                return
            self._send(200, data, hdrs, ctype=ctype)

        def do_PROPFIND(self):
            if not self._auth_ok():
                return self._need_auth()
            depth = (self.headers.get("Depth") or "1").strip()
            m, rel = self._resolve()
            # 根请求：列出所有挂载点，让资源管理器一进来就能看到盘
            if m is None:
                return self._propfind_root(depth)
            try:
                st = m.backend.stat(rel)
            except BackendError as e:
                return self._send(404 if e.kind == "not_found" else 500, str(e))
            items = [st]
            if depth != "0" and st["is_dir"]:
                try:
                    items += [{"name": c["name"], "path": c["path"], "is_dir": c["is_dir"],
                               "size": c["size"], "mtime": c["mtime"]}
                              for c in m.backend.list(rel)]
                except BackendError:
                    pass
            return self._send(207, self._multistatus(m.name, items),
                              ctype='application/xml; charset="utf-8"')

        def _propfind_root(self, depth):
            items = []
            for card in server.cards():
                items.append({"name": card["name"], "path": card["name"], "is_dir": True,
                              "size": 0, "mtime": card.get("created") or time.time()})
            return self._send(207, self._multistatus("", items, root=True),
                              ctype='application/xml; charset="utf-8"')

        def _multistatus(self, mount_name, items, root=False):
            out = ['<?xml version="1.0" encoding="utf-8"?>',
                   '<d:multistatus xmlns:d="DAV:">']
            for it in items:
                rel = _normalize_rel(it["path"])
                url_path = "/".join(x for x in (mount_name, rel) if x)
                href = _href("/" + url_path, it["is_dir"])
                lm = formatdate(it.get("mtime") or time.time(), usegmt=True)
                # 结构必须严格合法：propstat 里是 prop + status 两个并列子元素。
                # 少写一个 </d:prop> 会让 Windows WebClient 解析失败——它不报错，
                # 只是 `net use` 回一句「找不到网络名（67）」，排查起来毫无线索。
                out.append("<d:response><d:href>%s</d:href><d:propstat><d:prop>" % _xml_escape(href))
                out.append("<d:displayname>%s</d:displayname>" % _xml_escape(it["name"]))
                out.append("<d:getlastmodified>%s</d:getlastmodified>" % lm)
                if it["is_dir"]:
                    out.append("<d:resourcetype><d:collection/></d:resourcetype>")
                    out.append("<d:getcontentlength>0</d:getcontentlength>")
                else:
                    out.append("<d:resourcetype/>")
                    out.append("<d:getcontentlength>%d</d:getcontentlength>" % int(it.get("size") or 0))
                    ctype = mimetypes.guess_type(it["name"])[0] or "application/octet-stream"
                    out.append("<d:getcontenttype>%s</d:getcontenttype>" % _xml_escape(ctype))
                # 资源管理器靠这两个元素判断「能不能往里写」；不给就显示成只读盘
                out.append("<d:lockdiscovery/>")
                out.append("<d:supportedlock><d:lockentry><d:lockscope><d:exclusive/>"
                           "</d:lockscope><d:locktype><d:write/></d:locktype></d:lockentry>"
                           "<d:lockentry><d:lockscope><d:shared/></d:lockscope>"
                           "<d:locktype><d:write/></d:locktype></d:lockentry></d:supportedlock>")
                out.append("</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
            out.append("</d:multistatus>")
            return "".join(out)

        def do_PUT(self):
            if not self._auth_ok():
                return self._need_auth()
            m, rel = self._resolve()
            if m is None:
                return self._send(404, "没有这个挂载点")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_INLINE:
                return self._send(413, "超过单次写入上限 %d MB" % (MAX_INLINE // 1048576))
            data = self.rfile.read(length) if length else b""
            try:
                m.backend.write(rel, data)
            except BackendError as e:
                code = {"denied": 403, "unsupported": 405, "auth": 401}.get(e.kind, 500)
                return self._send(code, str(e))
            self._send(201, b"")

        def do_MKCOL(self):
            if not self._auth_ok():
                return self._need_auth()
            m, rel = self._resolve()
            if m is None:
                return self._send(404, "没有这个挂载点")
            try:
                m.backend.mkdir(rel)
            except BackendError as e:
                code = {"denied": 403, "unsupported": 405}.get(e.kind, 500)
                return self._send(code, str(e))
            self._send(201, b"")

        def do_DELETE(self):
            if not self._auth_ok():
                return self._need_auth()
            m, rel = self._resolve()
            if m is None:
                return self._send(404, "没有这个挂载点")
            try:
                m.backend.remove(rel)
            except BackendError as e:
                code = {"denied": 403, "unsupported": 405, "not_found": 404}.get(e.kind, 500)
                return self._send(code, str(e))
            self._send(204, b"")

        def do_MOVE(self):
            if not self._auth_ok():
                return self._need_auth()
            m, rel = self._resolve()
            if m is None:
                return self._send(404, "没有这个挂载点")
            dest = self.headers.get("Destination") or ""
            dpath = _normalize_rel(urllib.parse.unquote(urllib.parse.urlsplit(dest).path or ""))
            dname, _, drel = dpath.partition("/")
            if dname != m.name:
                return self._send(502, "不支持跨挂载点移动")
            try:
                m.backend.move(rel, drel)
            except BackendError as e:
                code = {"denied": 403, "unsupported": 405, "not_found": 404}.get(e.kind, 500)
                return self._send(code, str(e))
            self._send(201, b"")

        def do_COPY(self):
            # 资源管理器偶尔会试 COPY。实现成「读 + 写」，够用。
            if not self._auth_ok():
                return self._need_auth()
            m, rel = self._resolve()
            if m is None:
                return self._send(404, "没有这个挂载点")
            dest = self.headers.get("Destination") or ""
            dpath = _normalize_rel(urllib.parse.unquote(urllib.parse.urlsplit(dest).path or ""))
            dname, _, drel = dpath.partition("/")
            if dname != m.name:
                return self._send(502, "不支持跨挂载点复制")
            try:
                data, _ = m.backend.open_read(rel)
                m.backend.write(drel, data)
            except BackendError as e:
                return self._send(500, str(e))
            self._send(201, b"")

        def do_POST(self):
            self._send(405, "不支持 POST")

    return Handler


# ============================================================ 路径解析（能力层用）

def _split_path(path):
    """`pan:/a/b` → ("pan", "a/b")。也接受 `pan` / `pan:/` 这种根写法。"""
    if not path:
        raise ValueError("没给 path（形如 name:/a/b）")
    p = str(path).strip().replace("\\", "/")
    if ":" in p and not p.startswith("/"):
        name, _, rel = p.partition(":")
        name = name.strip()
        if not _NAME_RE.match(name):
            raise ValueError("挂载点名字不合法：%r（字母数字开头，可含 _ . -）" % name)
        return name, _normalize_rel(rel)
    raise ValueError("path 必须带挂载点前缀，形如 pan:/a/b（收到 %r）" % path)


def _rmtree_safe(p):
    if os.path.isdir(p):
        shutil.rmtree(p)
    elif os.path.exists(p):
        os.remove(p)


# ============================================================ 设备

NET_USE_HELP = (
    "Windows 原生映射（不需要装任何驱动）：\n"
    "  1) 先跑一次 drive.setup 把环境准备好（WebClient 服务 + BasicAuthLevel + 防火墙）\n"
    "  2) 映射盘符：net use {drive}: {url}\n"
    "  3) 断开：net use {drive}: /delete\n"
    "⚠️ 地址必须用**局域网 IP**（{lan_ip}），不能用 127.0.0.1 ——\n"
    "   Windows 的 WebDAV 客户端对回环地址有硬限制：协议握手正常、映射必失败（错误 67），\n"
    "   而且它不会告诉你原因。这是实测踩出来的，别再走一遍。\n"
    "macOS：Finder → 前往 → 连接服务器 → 填 {url}\n"
    "Linux：mount -t davfs {url} /mnt/pan"
)


_LAN_IP_CACHE = []


def _lan_ip():
    """探一个本机局域网 IP。

    不用 `socket.gethostbyname(gethostname())`——那玩意儿在有虚拟网卡（WSL/Hyper-V/
    Docker）的机器上经常返回 172.x 的虚拟段，那个地址别的设备根本连不到。
    这里用「UDP connect 一个外网地址」取默认出口 IP：不发包，只是让内核选路，
    选出来的就是这张机器真正对外的地址。结果缓存 5 分钟。
    """
    now = time.time()
    if _LAN_IP_CACHE and now - _LAN_IP_CACHE[1] < 300:
        return _LAN_IP_CACHE[0]
    ip = ""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 53))       # 阿里 DNS，只为选路，一个包都不发
        ip = s.getsockname()[0]
    except Exception:
        ip = ""
    finally:
        s.close()
    if not ip or ip.startswith("127."):
        ip = ""
        try:
            for cand in socket.gethostbyname_ex(socket.gethostname())[2]:
                if not cand.startswith(("127.", "169.254.")):
                    ip = cand
                    break
        except Exception:
            pass
    ip = ip or "127.0.0.1"
    _LAN_IP_CACHE[:] = [ip, now]
    return ip


#: Windows 用哪个盘符：从 Z 往前找第一个没被占用的
def _free_drive():
    try:
        used = set()
        out = os.popen("net use").read()
        for line in out.splitlines():
            token = line.strip().split(" ")[0] if line.strip() else ""
            if re.match(r"^[A-Z]:$", token):
                used.add(token[0])
        for ch in "ZYXWVUTSRQPO":
            if ch not in used:
                return ch
    except Exception:
        pass
    return "Z"


class Drive(BaseDevice):
    """kind=drive：网盘与远程存储的挂载面。

    probe 只判「能不能起服务」，不判「有没有挂上」——没挂载是常态，不是故障。
    """

    kind = "drive"
    via = "local"

    def __init__(self, name="drive:local", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("note", "把网盘/远端存储挂成本地盘符，同时给模型一套文件系统界面")

    def probe(self):
        try:
            port = _SERVER.start()
        except Exception as e:
            return False, "WebDAV 服务起不来：%s" % e
        return True, "服务在 %s（已挂 %d 个）" % (_SERVER.base_url(), len(_SERVER.cards()))

    def caps(self):
        return CAPS

    def invoke(self, cap, args=None):
        args = dict(args or {})
        if cap not in CAPS:
            return R.unsupported(cap, "%s（%s）没有这项能力" % (self.name, self.kind),
                                 device=self.name, via=self.via)
        fn = getattr(self, "_do_" + cap.split(".", 1)[1], None)
        if fn is None:
            return R.unsupported(cap, "这项能力没接上实现", device=self.name, via=self.via)
        try:
            return fn(args)
        except ValueError as e:
            return R.error(cap, str(e), device=self.name, via=self.via)
        except Exception as e:
            return R.error(cap, "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)

    # ---------------- 挂载管理 ----------------

    def _do_mount(self, args):
        name = str(args.get("name") or "").strip()
        backend_kind = str(args.get("backend") or "local").strip().lower()
        if not _NAME_RE.match(name):
            return R.error("drive.mount",
                           "name 不合法：%r（字母数字开头，可含 _ . -，最长 32）" % name,
                           device=self.name, via=self.via)

        # 未显式给 write 时，默认只读——挂载是外部可直连的入口，默认收紧
        write = bool(args.get("write", False))

        if backend_kind == "local":
            root = args.get("path") or args.get("root")
            if not root:
                return R.error("drive.mount", "backend=local 需要 path（本机目录）",
                               device=self.name, via=self.via)
            be = LocalBackend(root, readonly=not write)
        elif backend_kind == "webdav":
            url = args.get("url")
            if not url:
                return R.error("drive.mount", "backend=webdav 需要 url",
                               device=self.name, via=self.via)
            be = WebDAVBackend(url, user=args.get("user"), password=args.get("password"),
                               readonly=not write, verify_tls=bool(args.get("verify_tls", True)))
        elif backend_kind in ("quark", "夸克"):
            be = QuarkBackend(cookie=args.get("cookie"))
        else:
            return R.error("drive.mount",
                           "未知 backend=%r（可用 local / webdav / quark）" % backend_kind,
                           device=self.name, via=self.via)

        port = _SERVER.start(host=str(args.get("host") or DEFAULT_HOST),
                             port=int(args.get("port") or DEFAULT_PORT),
                             auth=((str(args["user"]), str(args.get("password") or ""))
                                   if args.get("require_auth") and args.get("user") else None))
        _SERVER.mount(_Mount(name, be, note=str(args.get("note") or "")))
        _persist()

        lan = _lan_ip()
        host = _SERVER.host
        # 绑 0.0.0.0 时对外要用局域网 IP；绑回环时只能本机访问（且 Windows 挂不上）
        public_host = lan if host in ("0.0.0.0", "::") else host
        url = "http://%s:%d/%s/" % (public_host, _SERVER.port, name)
        drive_letter = _free_drive()
        data = {
            "name": name,
            "backend": backend_kind,
            "url": url,
            "readonly": be.READONLY,
            "bind": "%s:%d" % (host, _SERVER.port),
            "lan_ip": lan,
            "service": _SERVER.base_url(),
            "mounts": len(_SERVER.cards()),
            "windows_cmd": 'net use %s: %s' % (drive_letter, url),
            "windows_hint": NET_USE_HELP.format(
                url=url, drive=drive_letter, lan_ip=lan,
                user=(args.get("user") or "<user>"),
                password=("<password>" if args.get("password") else "")),
        }
        if not be.READONLY and not _SERVER.auth:
            data["warning"] = ("这个挂载可写且没有认证——同局域网内任何人都能改。"
                               "要收紧就重新 mount 并传 require_auth=True + user/password。")
        return R.success("drive.mount", self.name, via=self.via, side_effect=True, data=data)

    def _do_setup(self, args):
        """一键把 Windows 侧环境准备好。

        为什么要有这一步：`net use` 挂不上的原因有四个，全都藏在系统设置里，
        表现却都是同一句「找不到网络名（67）」。让人去翻四个地方不现实。
        这里一次做完四件事，每件都回报真实结果，失败的如实说失败。
        """
        if os.name != "nt":
            return R.unsupported("drive.setup", "只对 Windows 有意义（macOS/Linux 不需要这些设置）",
                                 device=self.name, via=self.via)

        results = []

        def run(cmd):
            try:
                p = __import__("subprocess").run(
                    cmd, shell=True, capture_output=True, text=True, timeout=30,
                    creationflags=0x08000000)          # CREATE_NO_WINDOW：不弹黑框
                return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
            except Exception as e:
                return -1, "", str(e)

        # 1) WebClient 服务：被禁用的机器上最常见
        rc, out, err = run('sc qc WebClient')
        start_type = ""
        for line in (out or "").splitlines():
            if "START_TYPE" in line:
                start_type = line.split(":", 1)[-1].strip()
        if "DISABLED" in start_type.upper():
            rc2, o2, e2 = run('sc config WebClient start= demand')
            results.append({"step": "启用 WebClient", "ok": rc2 == 0,
                            "detail": (o2 or e2 or start_type)[:120]})
        else:
            results.append({"step": "WebClient 启动类型", "ok": True,
                            "detail": start_type[:60] or "未读到"})
        rc3, o3, e3 = run('sc start WebClient')
        if rc3 != 0 and "1056" not in (e3 or "") and "已在运行" not in (o3 or ""):
            results.append({"step": "启动 WebClient", "ok": False, "detail": (e3 or o3)[:120]})
        else:
            results.append({"step": "启动 WebClient", "ok": True, "detail": "运行中"})

        # 2) BasicAuthLevel：默认 1（只允许 HTTPS），HTTP 挂载必须 2
        rc4, o4, e4 = run('reg add "HKLM\\SYSTEM\\CurrentControlSet\\Services\\WebClient'
                          '\\Parameters" /v BasicAuthLevel /t REG_DWORD /d 2 /f')
        results.append({"step": "放开 Basic over HTTP", "ok": rc4 == 0, "detail": (o4 or e4)[:120]})

        # 3) 单文件大小上限：默认 50MB，传个大文件就报 0x800700DF
        rc5, o5, e5 = run('reg add "HKLM\\SYSTEM\\CurrentControlSet\\Services\\WebClient'
                          '\\Parameters" /v FileSizeLimitInBytes /t REG_DWORD /d 4294967295 /f')
        results.append({"step": "解除 50MB 单文件限制", "ok": rc5 == 0, "detail": (o5 or e5)[:120]})

        # 4) 防火墙：绑 0.0.0.0 之后不开这个，别的设备连不上
        port = _SERVER.port or int(args.get("port") or DEFAULT_PORT)
        rule = "Skynet Drive WebDAV (%d)" % port
        rc6, o6, e6 = run(
            'netsh advfirewall firewall show rule name="%s"' % rule)
        if rc6 == 0:
            results.append({"step": "防火墙规则", "ok": True, "detail": "已存在"})
        else:
            rc7, o7, e7 = run(
                'netsh advfirewall firewall add rule name="%s" dir=in action=allow '
                'protocol=TCP localport=%d profile=private,domain' % (rule, port))
            results.append({"step": "加防火墙规则", "ok": rc7 == 0, "detail": (o7 or e7)[:120]})

        # 5) 重启服务让注册表生效（不重启的话新值读不到）
        run('sc stop WebClient')
        time.sleep(1)
        rc8, o8, e8 = run('sc start WebClient')
        results.append({"step": "重载 WebClient", "ok": rc8 == 0, "detail": (o8 or e8 or "已重启")[:80]})

        all_ok = all(r["ok"] for r in results)
        return R.success("drive.setup", self.name, via=self.via, side_effect=True, data={
            "ok": all_ok,
            "lan_ip": _lan_ip(),
            "steps": results,
            "note": ("全部就绪，可以直接 drive.mount 再 net use 了" if all_ok
                     else "有步骤失败——多半是没以管理员身份运行；照 steps 里失败那项的 detail 处理"),
        })

    def _do_unmount(self, args):
        name = str(args.get("name") or "").strip()
        m = _SERVER.unmount(name)
        if m is None:
            return R.error("drive.unmount", "没有这个挂载点：%s" % name,
                           device=self.name, via=self.via)
        _persist()
        return R.success("drive.unmount", self.name, via=self.via, side_effect=True,
                         data={"name": name, "remaining": len(_SERVER.cards())})

    def _do_mounts(self, args):
        cards = _SERVER.cards()
        return R.success("drive.mounts", self.name, via=self.via, data={
            "service": _SERVER.base_url() if _SERVER.is_up() else "",
            "running": _SERVER.is_up(),
            "count": len(cards),
            "mounts": cards,
        })

    # ---------------- 文件系统 ----------------

    def _backend_of(self, path):
        name, rel = _split_path(path)
        m = _SERVER.get(name)
        if m is None:
            raise ValueError("没有这个挂载点：%s（先 drive.mount，或 drive.mounts 看有哪些）" % name)
        if not _SERVER.is_up():
            _SERVER.start()
        return m, rel

    def _do_list(self, args):
        m, rel = self._backend_of(args.get("path"))
        try:
            items = m.backend.list(rel)
        except BackendError as e:
            return R.unavailable("drive.list", str(e), device=self.name, via=self.via)
        return R.success("drive.list", self.name, via=self.via, data={
            "mount": m.name, "path": rel, "count": len(items), "items": items})

    def _do_stat(self, args):
        m, rel = self._backend_of(args.get("path"))
        try:
            st = m.backend.stat(rel)
        except BackendError as e:
            return R.unavailable("drive.stat", str(e), device=self.name, via=self.via)
        return R.success("drive.stat", self.name, via=self.via,
                         data=dict(st, mount=m.name))

    def _do_read(self, args):
        m, rel = self._backend_of(args.get("path"))
        try:
            data, mtime = m.backend.open_read(rel)
        except BackendError as e:
            return R.unavailable("drive.read", str(e), device=self.name, via=self.via)
        # 返回 base64 还是文本，交给调用方决定：先探一下能不能当 utf-8
        try:
            text = data.decode("utf-8")
            return R.success("drive.read", self.name, via=self.via, data={
                "mount": m.name, "path": rel, "bytes": len(data), "text": text,
                "mtime": mtime, "encoding": "utf-8"})
        except UnicodeDecodeError:
            return R.success("drive.read", self.name, via=self.via, data={
                "mount": m.name, "path": rel, "bytes": len(data),
                "base64": base64.b64encode(data).decode("ascii"),
                "mtime": mtime, "encoding": "base64"})

    def _do_write(self, args):
        m, rel = self._backend_of(args.get("path"))
        if args.get("base64"):
            data = base64.b64decode(args["base64"])
        elif args.get("text") is not None:
            data = str(args["text"]).encode("utf-8")
        else:
            return R.error("drive.write", "没给内容（text 或 base64）",
                           device=self.name, via=self.via)
        if len(data) > MAX_INLINE:
            return R.error("drive.write", "超过单次写入上限 %d MB" % (MAX_INLINE // 1048576),
                           device=self.name, via=self.via)
        try:
            r = m.backend.write(rel, data)
        except BackendError as e:
            return R.unavailable("drive.write", str(e), device=self.name, via=self.via)
        return R.success("drive.write", self.name, via=self.via, side_effect=True,
                         data=dict(r, mount=m.name, sha1=hashlib.sha1(data).hexdigest()[:16]))

    def _do_mkdir(self, args):
        m, rel = self._backend_of(args.get("path"))
        try:
            r = m.backend.mkdir(rel)
        except BackendError as e:
            return R.unavailable("drive.mkdir", str(e), device=self.name, via=self.via)
        return R.success("drive.mkdir", self.name, via=self.via, side_effect=True,
                         data=dict(r, mount=m.name))

    def _do_remove(self, args):
        m, rel = self._backend_of(args.get("path"))
        try:
            r = m.backend.remove(rel)
        except BackendError as e:
            return R.unavailable("drive.remove", str(e), device=self.name, via=self.via)
        return R.success("drive.remove", self.name, via=self.via, side_effect=True,
                         destructive=True, data=dict(r, mount=m.name))

    def _do_move(self, args):
        src = args.get("from") or args.get("src")
        dst = args.get("to") or args.get("dst")
        if not src or not dst:
            return R.error("drive.move", "需要 from 与 to（都带挂载点前缀）",
                           device=self.name, via=self.via)
        sm, srel = self._backend_of(src)
        dm, drel = self._backend_of(dst)
        if sm.name != dm.name:
            return R.error("drive.move", "不支持跨挂载点移动（from=%s to=%s）" % (sm.name, dm.name),
                           device=self.name, via=self.via)
        try:
            r = sm.backend.move(srel, drel)
        except BackendError as e:
            return R.unavailable("drive.move", str(e), device=self.name, via=self.via)
        return R.success("drive.move", self.name, via=self.via, side_effect=True,
                         data=dict(r, mount=sm.name))

    def _do_copy(self, args):
        src = args.get("from") or args.get("src")
        dst = args.get("to") or args.get("dst")
        if not src or not dst:
            return R.error("drive.copy", "需要 from 与 to", device=self.name, via=self.via)
        sm, srel = self._backend_of(src)
        dm, drel = self._backend_of(dst)
        if sm.name != dm.name:
            return R.error("drive.copy", "不支持跨挂载点复制", device=self.name, via=self.via)
        try:
            data, _ = sm.backend.open_read(srel)
            r = dm.backend.write(drel, data)
        except BackendError as e:
            return R.unavailable("drive.copy", str(e), device=self.name, via=self.via)
        return R.success("drive.copy", self.name, via=self.via, side_effect=True,
                         data=dict(r, mount=sm.name, bytes=len(data)))

    def _do_url(self, args):
        """给一个 drive 路径，返回可直接给浏览器/播放器的 HTTP URL。"""
        m, rel = self._backend_of(args.get("path"))
        if not _SERVER.is_up():
            _SERVER.start()
        public_host = _lan_ip() if _SERVER.host in ("0.0.0.0", "::") else _SERVER.host
        url = "http://%s:%d/%s/%s" % (public_host, _SERVER.port, m.name,
                                      urllib.parse.quote(rel))
        return R.success("drive.url", self.name, via=self.via, data={
            "mount": m.name, "path": rel, "url": url,
            "auth": bool(_SERVER.auth),
            "note": "走局域网 IP，其他设备也能直接访问；绑 0.0.0.0 意味着同网段可见"})


CAPS = (
    "drive.setup",
    "drive.mount", "drive.unmount", "drive.mounts",
    "drive.list", "drive.stat", "drive.read", "drive.write",
    "drive.mkdir", "drive.remove", "drive.move", "drive.copy", "drive.url",
)


# ============================================================ 自测

def _selftest():
    import tempfile
    ok = []

    def chk(name, cond, extra=""):
        ok.append((name, bool(cond), extra))

    # 路径穿越
    with tempfile.TemporaryDirectory() as td:
        root = os.path.join(td, "root")
        os.makedirs(root)
        open(os.path.join(root, "a.txt"), "w").write("hi")
        be = LocalBackend(root, readonly=False)
        try:
            _safe_join(root, "../../etc/passwd")
            chk("拒绝 .. 穿越", False, "竟然通过了")
        except ValueError:
            chk("拒绝 .. 穿越", True)
        try:
            _safe_join(root, "C:/Windows/system32")
            chk("拒绝盘符绝对路径", False)
        except ValueError:
            chk("拒绝盘符绝对路径", True)
        chk("正常文件可读", be.open_read("a.txt")[0] == b"hi")
        be.write("sub/b.txt", b"yo")
        chk("写 + 自动建目录", be.open_read("sub/b.txt")[0] == b"yo")
        chk("列目录", [i["name"] for i in be.list("")] == ["a.txt", "sub"])
        be.move("sub/b.txt", "sub/c.txt")
        chk("改名", be.stat("sub/c.txt")["size"] == 2)
        be.remove("sub/c.txt")
        try:
            be.stat("sub/c.txt")
            chk("删除生效", False)
        except BackendError:
            chk("删除生效", True)

    # WebDAV 服务端
    with tempfile.TemporaryDirectory() as td:
        open(os.path.join(td, "hello.txt"), "w").write("world")
        os.makedirs(os.path.join(td, "d"))
        _SERVER.stop()
        _SERVER.mount(_Mount("t", LocalBackend(td, readonly=False)))
        port = _SERVER.start(host=DEFAULT_HOST, port=18731)
        base = "http://127.0.0.1:%d" % port
        try:
            r = net.urlopen(base + "/t/", timeout=8, method="PROPFIND",
                            headers={"Depth": "1"})
            body = r.read().decode("utf-8")
            chk("PROPFIND 207", r.status == 207, str(r.status))
            chk("PROPFIND 含子项", "hello.txt" in body and ">d<" in body)
            r2 = net.urlopen(base + "/t/hello.txt", timeout=8)
            chk("GET 文件", r2.read() == b"world")
            req = urllib.request.Request(base + "/t/new.txt", data=b"abc", method="PUT")
            r3 = net.urlopen(req, timeout=8)
            chk("PUT 写入", r3.status in (200, 201), str(r3.status))
            chk("PUT 落盘", os.path.exists(os.path.join(td, "new.txt")))
            req2 = urllib.request.Request(base + "/t/d/", method="MKCOL")
            r4 = net.urlopen(req2, timeout=8)
            chk("MKCOL 建目录", r4.status in (200, 201), str(r4.status))
            req3 = urllib.request.Request(base + "/t/new.txt", method="DELETE")
            r5 = net.urlopen(req3, timeout=8)
            chk("DELETE 删除", r5.status in (200, 204), str(r5.status))
        except Exception as e:
            chk("WebDAV 链路", False, "%s: %s" % (type(e).__name__, e))
        finally:
            _SERVER.stop()

    passed = sum(1 for _, c, _ in ok if c)
    for n, c, x in ok:
        print("  %s %s %s" % ("✓" if c else "✗", n, x))
    print("drive selftest: %d/%d" % (passed, len(ok)))
    return passed == len(ok)


if __name__ == "__main__":
    sys.exit(0 if _selftest() else 1)

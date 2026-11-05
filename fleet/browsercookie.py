"""从 Chromium 系浏览器 profile 里解出 cookie —— 只为「同一 Windows 用户下自己的浏览器」。

为什么能解
----------
Chromium 的 cookie 加密是**故意设计成同一用户可解的**：
`Local State` 里存一个 AES key，这个 key 本身用 Windows DPAPI 加密；
DPAPI 的作用范围就是「本机 + 本 Windows 账户」。所以同一账户下，
浏览器加密对我们是透明的——这不是漏洞，是 DPAPI 的既定语义。

反过来，换机器、换 Windows 用户、重装系统，全都解不开。

支持范围
--------
- `v10` / `v11`：AES-256-GCM，key 来自 Local State（Chrome、Edge、Electron 系）
- `v20`：Chrome 127+ 的 app-bound encryption，key 绑在 elevation service 上，
  普通手段解不开，直接报错，不假装成功
- 无前缀：老版本整体 DPAPI 加密

用法
----
    from fleet import browsercookie
    ck = browsercookie.read(os.path.expanduser("~/AppData/Local/app/session"), host="x.com")
    # -> {"auth_token": "...", "ct0": "..."}  或者带属性的完整列表

    browsercookie.to_header(ck)   # -> "auth_token=...; ct0=..."
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import tempfile

from . import vault

__all__ = ["read", "read_full", "to_header", "find_profile", "KNOWN_PROFILES"]


# 本机常见 Chromium 系浏览器 profile（都含 Network/Cookies + Local State）
_USER_HOME = os.path.join(os.environ.get("SystemDrive", "C:") + "\\", "Users",
                          os.environ.get("USERNAME") or os.environ.get("USER") or "")
KNOWN_PROFILES = {
    "workbuddy": os.path.join(_USER_HOME, ".workbuddy", "app", "session"),
    "chrome": os.path.join(_USER_HOME, "AppData", "Local", "Google", "Chrome", "User Data", "Default"),
    "edge": os.path.join(_USER_HOME, "AppData", "Local", "Microsoft", "Edge", "User Data", "Default"),
}


def find_profile(hint: str = None) -> str:
    """找一个可用的 profile 目录。hint 可以是键名，也可以是路径。"""
    if hint and os.path.isdir(hint):
        return hint
    if hint and hint.lower() in KNOWN_PROFILES:
        p = KNOWN_PROFILES[hint.lower()]
        if os.path.isdir(p):
            return p
    for name, p in KNOWN_PROFILES.items():
        if os.path.isdir(p):
            return p
    raise FileNotFoundError("没找到任何已知的 Chromium profile 目录")


def _local_state(profile: str) -> str:
    """Local State 可能在 profile 内，也可能在上一级（Chrome 的 Default 就是这种情况）。"""
    for p in (os.path.join(profile, "Local State"),
              os.path.join(os.path.dirname(profile), "Local State")):
        if os.path.isfile(p):
            return p
    raise FileNotFoundError("找不到 Local State：%s" % profile)


def _cookies_db(profile: str) -> str:
    for p in (os.path.join(profile, "Network", "Cookies"),
              os.path.join(profile, "Cookies")):
        if os.path.isfile(p):
            return p
    raise FileNotFoundError("找不到 Cookies 数据库：%s" % profile)


def _aes_key(profile: str) -> bytes:
    with open(_local_state(profile), encoding="utf-8") as f:
        ls = json.load(f)
    enc = ls.get("os_crypt", {}).get("encrypted_key")
    if not enc:
        raise ValueError("Local State 里没有 os_crypt.encrypted_key（可能未启用 cookie 加密）")
    raw = base64.b64decode(enc)
    if raw[:5] != b"DPAPI":
        raise ValueError("密钥前缀不是 DPAPI，而是 %r，不认识" % raw[:5])
    # Chromium 的 DPAPI 用空 entropy；保险箱自己用的是固定 entropy，必须区分
    return vault.unprotect(raw[5:], entropy=b"")


def _decrypt(enc: bytes, key: bytes) -> str:
    if not enc:
        return ""
    tag = enc[:3]
    if tag in (b"v10", b"v11"):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if not key:
            raise ValueError("没有 AES key，解不了 v10 cookie")
        nonce, ct = enc[3:15], enc[15:]
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8", "replace")
    if tag == b"v20":
        raise ValueError("v20 = Chrome app-bound encryption，key 绑在 elevation service 上，"
                         "本模块不解，也不假装解开了")
    # 老格式：整段 DPAPI
    return vault.unprotect(enc, entropy=b"").decode("utf-8", "replace")


def read_full(profile: str = None, host: str = None, names=None) -> list:
    """返回 cookie 列表，每项含 name/value/domain/path/expires/secure/http_only。

    host     域名子串过滤，如 "x.com"
    names    cookie 名白名单，如 ["auth_token", "ct0"]
    """
    profile = find_profile(profile)
    key = _aes_key(profile)

    # Cookies 被浏览器占用（WAL），复制一份再读，绝不改动原库
    src = _cookies_db(profile)
    tmpdir = tempfile.mkdtemp(prefix="wbck_")
    dst = os.path.join(tmpdir, "Cookies")
    try:
        shutil.copy2(src, dst)
        for suffix in ("-wal", "-shm"):
            if os.path.isfile(src + suffix):
                shutil.copy2(src + suffix, dst + suffix)

        con = sqlite3.connect(dst)
        con.text_factory = bytes
        sql = ("SELECT host_key, name, value, encrypted_value, path, expires_utc, "
               "is_secure, is_httponly FROM cookies")
        where, args = [], []
        if host:
            where.append("host_key LIKE ?")
            args.append("%" + host + "%")
        if names:
            where.append("name IN (%s)" % ",".join("?" * len(names)))
            args.extend(names)
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = con.execute(sql, args).fetchall()
        con.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    out, failed = [], 0
    for host_key, name, value, enc, path, expires, secure, http_only in rows:
        name = name.decode() if isinstance(name, bytes) else name
        host_key = host_key.decode() if isinstance(host_key, bytes) else host_key
        path = path.decode() if isinstance(path, bytes) else path
        val = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        if not val and enc:
            try:
                val = _decrypt(enc, key)
            except Exception:
                failed += 1
                continue
        out.append(dict(name=name, value=val, domain=host_key, path=path,
                        expires=expires, secure=bool(secure),
                        http_only=bool(http_only)))
    if failed:
        out.append(dict(name="__failed__", value=str(failed), domain="", path=""))
    return out


def read(profile: str = None, host: str = None, names=None) -> dict:
    """返回 {name: value} 简化字典。"""
    items = read_full(profile, host=host, names=names)
    return {c["name"]: c["value"] for c in items if c["name"] != "__failed__"}


def to_header(cookies) -> str:
    """{name: value} 或 read_full 的列表 → Cookie 请求头字符串。"""
    if isinstance(cookies, dict):
        return "; ".join("%s=%s" % (k, v) for k, v in cookies.items())
    return "; ".join("%s=%s" % (c["name"], c["value"]) for c in cookies
                     if c["name"] != "__failed__")


def _main(argv):
    import sys
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        print("可用 profile：")
        for name, p in KNOWN_PROFILES.items():
            print("  %-10s %s  %s" % (name, "OK " if os.path.isdir(p) else "-- ", p))
        return 0
    cmd = argv[0]
    if cmd == "list":
        profile, host = None, None
        for a in argv[1:]:
            if a.startswith("host="):
                host = a[5:]
            else:
                profile = a
        items = read_full(profile, host=host)
        for c in items:
            if c["name"] == "__failed__":
                print("  !! 解密失败 %s 条" % c["value"])
                continue
            shown = c["value"][:14] + ("…" if len(c["value"]) > 14 else "")
            print("  %-22s %-28s %s" % (c["name"], c["domain"], shown))
        print("共 %d 条" % len([c for c in items if c["name"] != "__failed__"]))
        return 0
    if cmd == "get":
        # get <name> [host=...] [profile]
        name = argv[1]
        host = next((a[5:] for a in argv[2:] if a.startswith("host=")), None)
        profile = next((a for a in argv[2:] if not a.startswith("host=")), None)
        v = read(profile, host=host, names=[name]).get(name)
        print(v if v else "(没找到)")
        return 0 if v else 1
    print("未知命令：%s" % cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))

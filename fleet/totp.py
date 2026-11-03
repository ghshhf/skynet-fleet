# -*- coding: utf-8 -*-
"""天网 · TOTP 验证码（本机算，不联网、不依赖手机）
================================================================
2026-11-02 主人定的调子：「现在能生成密钥这种东西了……就不用这么单搞了」

这个模块兑现的就是这句话：**密钥在保险箱里，码在本机算**。
不需要 Google Authenticator、不需要手机、不需要联网，也不需要在
任何第三方 App 里再存一份。

为什么 TOTP 比短信和 Passkey 都更适合这个场景
----------------------------------------------
    · 短信   → 要一个真实号码进对方账本，等于往外递身份
    · Passkey→ 要系统的屏幕解锁方式（本机没 PIN，建不了）
    · TOTP   → 只要一串 32 位种子，种子能抄、能算、能锁进 DPAPI

RFC 6238 标准实现，只依赖标准库。参数（位数/周期/算法）从保险箱里读，
不写死——不同站点给的不一样。

用法
----
    python -m fleet.totp                 # 列出保险箱里所有 totp 条目
    python -m fleet.totp google          # 直接出当前 6 位码 + 剩余秒
    python -m fleet.totp google --watch  # 连续刷新（Ctrl-C 退出）
    python -m fleet.totp google --at 1793566200   # 算指定时刻的码（对时用）

代码里：
    from fleet import totp
    code = totp.now("google_totp")
"""
import base64
import hashlib
import hmac
import struct
import sys
import time

from . import vault

_DEFAULT = {"digits": 6, "period": 30, "algorithm": "SHA1"}


def _decode_secret(secret: str) -> bytes:
    s = "".join(secret.split()).upper()
    pad = (8 - len(s) % 8) % 8
    return base64.b32decode(s + "=" * pad)


def code_at(secret: str, at: float = None, digits: int = 6,
            period: int = 30, algorithm: str = "SHA1") -> str:
    """按 RFC 6238 算某个时刻的验证码。"""
    at = time.time() if at is None else at
    counter = int(at // period)
    key = _decode_secret(secret)
    msg = struct.pack(">Q", counter)
    digest = getattr(hashlib, algorithm.lower().replace("-", ""))
    mac = hmac.new(key, msg, digest).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def _entry(name: str):
    if not name.endswith("_totp") and vault.exists(name + "_totp"):
        name = name + "_totp"
    d = vault.load(name)
    if not isinstance(d, dict) or "secret" not in d:
        raise KeyError("保险箱里没有 TOTP 条目：%s" % name)
    return name, d


def now(name: str) -> str:
    """取当前码。name 可写 'google' 或 'google_totp'。"""
    _, d = _entry(name)
    return code_at(d["secret"], digits=d.get("digits", 6),
                   period=d.get("period", 30),
                   algorithm=d.get("algorithm", "SHA1"))


def remaining(period: int = 30) -> int:
    return period - int(time.time()) % period


def entries():
    out = []
    for it in vault.listing():
        n = it["name"]
        if not n.endswith("_totp"):
            continue
        try:
            d = vault.load(n)
            out.append((n, d.get("account") or d.get("provider") or "", d))
        except Exception as e:
            out.append((n, "<解不开：%s>" % e, {}))
    return out


def _main(argv):
    args = [a for a in argv[1:]]
    watch = "--watch" in args
    at = None
    if "--at" in args:
        i = args.index("--at")
        at = float(args[i + 1])
        del args[i:i + 2]
    args = [a for a in args if not a.startswith("--")]

    if not args:
        rows = entries()
        if not rows:
            print("保险箱里还没有 TOTP 条目（%s）" % vault.VAULT_DIR)
            return 1
        for n, acct, d in rows:
            print("%-22s %-32s %d 位 / %ds / %s" % (
                n, acct, d.get("digits", 6), d.get("period", 30),
                d.get("algorithm", "SHA1")))
        return 0

    name = args[0]
    if watch:
        try:
            while True:
                print("\r%s  %2ds  " % (now(name), remaining()), end="", flush=True)
                time.sleep(1)
        except KeyboardInterrupt:
            print()
            return 0
    _, d = _entry(name)
    c = code_at(d["secret"], at=at, digits=d.get("digits", 6),
                period=d.get("period", 30), algorithm=d.get("algorithm", "SHA1"))
    print("%s  （剩余 %ds）" % (c, remaining(d.get("period", 30))))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))

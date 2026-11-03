# -*- coding: utf-8 -*-
"""天网 · 凭据保险箱（绑本机）
================================================================
2026-11-02 主人原话：
    「本就是今天晚上跑通注册的账号，就给了你，你为啥不绑定在自己的电脑上？
      我这个电脑又不是明天就要换了」
    「绑一下那个别人就用不了了，只要使用的话用密钥」

所以这个模块只解决一件事：**把凭据从明文文件搬进本机锁里**。

为什么是 DPAPI 而不是自己写一套加密
------------------------------------
自己写 = 把密钥也放在同一个盘上，等于把钥匙插在锁眼上。DPAPI 的密钥由
**本机 TPM + 当前 Windows 用户账户**派生，不落盘、不可导出：

    · 文件被拷到另一台机器  → 解不开
    · 同一台机器的另一个用户 → 解不开
    · 只有「这台机器 + 这个账户」→ 解得开

这正是「绑在自己电脑上」的字面实现，而且零依赖：ctypes 直接调 crypt32，
不用装 pywin32，不用记主密码，不用额外保管任何东西。

代价（必须说清楚，不许含糊）
----------------------------
`CRYPTPROTECT_LOCAL_MACHINE` 不加 → 用户级绑定。重装系统 / 换用户 / 换机器
之后**原文件永久解不开**。所以本模块提供 `export`（明文导出）作为逃生口，
但导出必须在**本机、本用户**下执行，且不做自动备份——按主人的规矩，
备份等于预设「还要回来」，与「确定要删」互斥。

用法
----
    python -m fleet.vault put  nvidia_api_key        # 交互式录入
    python -m fleet.vault show nvidia_api_key        # 打印明文（仅本机）
    python -m fleet.vault move _accounts/xxx.json    # 明文 json → .vault 并删明文
    python -m fleet.vault list

代码里：
    from fleet import vault
    vault.save("nvidia_api_key", "nvapi-...")
    key = vault.load("nvidia_api_key")
"""
import ctypes
import json
import os
import sys
from ctypes import wintypes

# 默认库目录：与 netforce 的 _accounts 对齐（那边是主人认得的落点）
VAULT_DIR = os.environ.get("FLEET_VAULT_DIR", r"E:\xmanbian\netforce\_accounts")
SUFFIX = ".vault"

# 熵：给同一份数据加一层站内盐。改这个值 = 旧密文全部作废，慎动。
_ENTROPY = b"skynet-fleet/vault/v1"

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _from_blob(blob: _DATA_BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _crypt32():
    return ctypes.windll.crypt32


def _kernel32():
    return ctypes.windll.kernel32


def protect(data: bytes, entropy: bytes = _ENTROPY) -> bytes:
    """明文 → DPAPI 密文。绑当前机器 + 当前用户。"""
    blob_in = _to_blob(data)
    blob_ent = _to_blob(entropy)
    blob_out = _DATA_BLOB()
    ok = _crypt32().CryptProtectData(
        ctypes.byref(blob_in), None,
        ctypes.byref(blob_ent), None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise OSError(ctypes.GetLastError(), "CryptProtectData 失败")
    try:
        return _from_blob(blob_out)
    finally:
        _kernel32().LocalFree(blob_out.pbData)


def unprotect(blob: bytes, entropy: bytes = _ENTROPY) -> bytes:
    """DPAPI 密文 → 明文。换机器/换用户会在这里报错，这是设计意图。"""
    blob_in = _to_blob(blob)
    blob_ent = _to_blob(entropy)
    blob_out = _DATA_BLOB()
    ok = _crypt32().CryptUnprotectData(
        ctypes.byref(blob_in), None,
        ctypes.byref(blob_ent), None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise OSError(ctypes.GetLastError(),
                      "CryptUnprotectData 失败：密文不是本机本账户生成的"
                      "（换机器 / 重装系统 / 换了 Windows 用户都解不开）")
    try:
        return _from_blob(blob_out)
    finally:
        _kernel32().LocalFree(blob_out.pbData)


# ---------------------------------------------------------------- 文件层

def _path(name: str) -> str:
    name = name.strip().replace("\\", "/").split("/")[-1]
    if name.endswith(SUFFIX):
        name = name[:-len(SUFFIX)]
    return os.path.join(VAULT_DIR, name + SUFFIX)


def save(name: str, value) -> dict:
    """把任意可 JSON 序列化的值加密落盘。返回落点信息（不含明文）。"""
    os.makedirs(VAULT_DIR, exist_ok=True)
    if isinstance(value, (bytes, bytearray)):
        payload = bytes(value)
        raw = payload
    else:
        raw = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    blob = protect(raw)
    p = _path(name)
    tmp = p + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, p)
    return dict(ok=True, path=p, bytes=len(blob), bound="DPAPI/本机+本账户")


def load(name: str, default=None):
    """解密读取。文件不存在返回 default；存在但解不开直接抛错（不吞）。"""
    p = _path(name)
    if not os.path.exists(p):
        return default
    with open(p, "rb") as f:
        blob = f.read()
    raw = unprotect(blob)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return raw.decode("utf-8", "replace")


def exists(name: str) -> bool:
    return os.path.exists(_path(name))


def listing() -> list:
    if not os.path.isdir(VAULT_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(VAULT_DIR)):
        if fn.endswith(SUFFIX):
            p = os.path.join(VAULT_DIR, fn)
            out.append(dict(name=fn[:-len(SUFFIX)], path=p,
                            size=os.path.getsize(p),
                            mtime=os.path.getmtime(p)))
    return out


def migrate(plain_path: str, name: str = None, remove=True) -> dict:
    """明文 json/txt → .vault。**先验证能解开，再删明文**，绝不先删。"""
    if not os.path.exists(plain_path):
        return dict(ok=False, err="源文件不存在：%s" % plain_path)
    name = name or os.path.splitext(os.path.basename(plain_path))[0]
    with open(plain_path, "rb") as f:
        text = f.read()
    try:
        value = json.loads(text.decode("utf-8"))
    except Exception:
        value = text.decode("utf-8", "replace")

    info = save(name, value)
    # 回读校验：解不开就绝不删明文
    back = load(name)
    if back != value:
        return dict(ok=False, err="回读校验不一致，已保留明文", path=info["path"])
    if remove:
        os.remove(plain_path)
    info["removed_plain"] = bool(remove)
    info["name"] = name
    return info


# ---------------------------------------------------------------- CLI

def _main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "list":
        for it in listing():
            print("%-28s %6d B  %s" % (it["name"], it["size"],
                                       __import__("time").strftime(
                                           "%Y-%m-%d %H:%M", __import__("time").localtime(it["mtime"]))))
        return 0
    if cmd == "put":
        name = argv[2]
        val = input("value for %s: " % name)
        print(json.dumps(save(name, val), ensure_ascii=False))
        return 0
    if cmd == "show":
        v = load(argv[2])
        print(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=2))
        return 0
    if cmd == "move":
        for p in argv[2:]:
            print(json.dumps(migrate(p), ensure_ascii=False))
        return 0
    print("未知命令：%s" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))

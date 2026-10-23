"""adb 封装：把「引号地狱」关在一个地方。

为什么要有它：真机操作里最难的不是 adb 本身，是三层壳叠加
（PowerShell → adb → sh → termux）。任何一层多一个引号就变成
`syntax error`，而报错信息永远指向最内层，排查成本极高。

本模块的规矩：
  * 一律走 argv 数组调 adb，不拼 shell 字符串（除了明确要 shell 的 sh()）
  * 要跑多行脚本，先落到 /data/local/tmp 再执行，绝不内联
  * Termux 的脚本用 `run-as com.termux` 跑，环境变量按 Termux 的规矩摆好
    （PREFIX/HOME/LD_LIBRARY_PATH/PATH 少一个，curl 就会报 not found）
  * 返回值统一 (rc, out, err)，out 去掉 \\r

命令行用法：
    python tools/adbx.py devices
    python tools/adbx.py sh "getprop ro.product.model"
    python tools/adbx.py termux "curl -sI https://hf-mirror.com | head -3"
    python tools/adbx.py termux @path/to/probe.sh     # 从文件读脚本（推荐）

带 @ 的写法是为了绕开「外层壳再把脚本二次解析」这类没法调的问题：
脚本一旦含引号/分号/换行，命令行传参就有一半概率被吃掉。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

ADB = os.environ.get("ADB") or "adb"
SERIAL = os.environ.get("FLEET_ANDROID_SERIAL") or ""

TERMUX_PKG = "com.termux"
_TERMUX_PREFIX = f"/data/user/0/{TERMUX_PKG}/files/usr"
_TERMUX_HOME = f"/data/user/0/{TERMUX_PKG}/files/home"

# Termux 里任何二进制都依赖这套环境，少一个就 "inaccessible or not found"。
# 注意：这里一律写绝对路径，不写 $PREFIX。原因是整串最后是作为
# `run-as ... sh -c "..."` 传过去的，双引号内的 $PREFIX 会在「执行前」被
# 外层 sh 展开（那时还是空），于是 LD_LIBRARY_PATH=/lib、curl 直接 not found。
_TERMUX_ENV = (
    f"export PREFIX={_TERMUX_PREFIX}; "
    f"export HOME={_TERMUX_HOME}; "
    f"export LD_LIBRARY_PATH={_TERMUX_PREFIX}/lib; "
    f"export PATH={_TERMUX_PREFIX}/bin:/system/bin:/system/xbin; "
    "export LANG=en_US.UTF-8; "
    f"export TMPDIR={_TERMUX_PREFIX}/tmp; "
)


def _base() -> list:
    return [ADB] + (["-s", SERIAL] if SERIAL else [])


def run(args, timeout=120, stdin_bytes=None):
    """跑一条 adb 命令，返回 (rc, stdout, stderr)。stdout 已去掉 \\r。"""
    try:
        p = subprocess.run(_base() + list(args), capture_output=True,
                           timeout=timeout, input=stdin_bytes)
    except FileNotFoundError:
        return -2, "", f"找不到 adb（ADB 环境变量={ADB}）"
    except subprocess.TimeoutExpired:
        return -3, "", f"adb 超时 {timeout}s: {' '.join(args)[:120]}"
    out = (p.stdout or b"").decode("utf-8", "replace").replace("\r\n", "\n")
    err = (p.stderr or b"").decode("utf-8", "replace").replace("\r\n", "\n")
    return p.returncode, out, err


def sh(cmd, timeout=120):
    """在设备 shell 里跑一条命令（单条，不拼引号）。"""
    return run(["shell", cmd], timeout=timeout)


def push(local, remote, timeout=300):
    return run(["push", local, remote], timeout=timeout)


def pull(remote, local, timeout=300):
    return run(["pull", remote, local], timeout=timeout)


def exec_out(args, dst=None, timeout=300):
    """二进制安全地把设备上的文件取回来。

    为什么不能用 `adb shell cat`：adb shell 会走 pty，把 \\n 变成 \\r\\n，
    一个 1MB 的 mp4 取回来会多出几千字节、sha 对不上、播放器直接打不开。
    症状极像「文件损坏」，其实只是传输层动了字节。exec-out 专为二进制设计：
    不分配 pty、不做换行转换。

    传 dst=None 时返回 bytes（小文件用）。
    """
    cmd = _base() + ["exec-out"] + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -3, b"", b"exec-out timeout"
    if dst:
        with open(dst, "wb") as f:
            f.write(p.stdout or b"")
        return p.returncode, (p.stdout or b""), (p.stderr or b"")
    return p.returncode, (p.stdout or b""), (p.stderr or b"")


def pull_termux(remote, local, timeout=300):
    """从 Termux 的私有目录取文件（shell 读不了，必须 run-as），二进制安全。"""
    return exec_out(["run-as", TERMUX_PKG, "cat", remote], dst=local, timeout=timeout)


def devices():
    rc, out, err = run(["devices"])
    rows = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        rows.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return rc, rows, err


def push_script(text: str, remote="/data/local/tmp/_adbx.sh"):
    """把脚本文本推到设备（先写本机临时文件再 push，避免 stdin 编码问题）。"""
    fd, tmp = tempfile.mkstemp(suffix=".sh")
    with os.fdopen(fd, "wb") as f:
        f.write(text.replace("\r\n", "\n").encode("utf-8"))
    try:
        rc, out, err = push(tmp, remote)
        if rc != 0:
            return rc, out, err
        sh(f"chmod 755 {remote}")
        return 0, out, err
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def shell_script(text: str, remote="/data/local/tmp/_adbx.sh", timeout=300):
    """推脚本 + 以设备 shell 身份执行。"""
    rc, out, err = push_script(text, remote)
    if rc != 0:
        return rc, out, err
    return sh(f"sh {remote}", timeout=timeout)


def termux(text: str, remote="/data/local/tmp/_adbx_tx.sh", timeout=600):
    """推脚本 + 以 Termux 身份执行（带 Termux 环境）。"""
    rc, out, err = push_script(text, remote)
    if rc != 0:
        return rc, out, err
    wrapper = (f'run-as {TERMUX_PKG} sh -c '
               f'"{_TERMUX_ENV}sh {remote}"')
    return run(["shell", wrapper], timeout=timeout)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    mode = argv[1]
    if mode == "devices":
        rc, rows, err = devices()
        for s, st in rows:
            print(f"{s}\t{st}")
        if err.strip():
            sys.stderr.write(err)
        return 0 if rows else 1
    if len(argv) < 3:
        print(__doc__)
        return 2
    body = " ".join(argv[2:])
    if body.startswith("@"):                 # 从文件读，躲开多层壳的二次解析
        with open(body[1:], encoding="utf-8") as f:
            body = f.read()
    if mode == "sh":
        rc, out, err = shell_script(body)
    elif mode == "termux":
        rc, out, err = termux(body)
    else:
        print(f"未知模式 {mode}")
        return 2
    if out:
        sys.stdout.write(out)
    if err.strip():
        sys.stderr.write(err)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))

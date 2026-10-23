"""P2 传模型兜底：把 PC 上的模型推到已连接的安卓手机。

流程：
  1. 读 fleet/model_catalog.py 的 CATALOG（或 tianwang_android 侧镜像）；
  2. 对每个档位，按约定本地路径查找模型文件（FLEET_MODELS_DIR / --src）；
  3. adb push 到手机 /sdcard/Android/data/app.skynet.tianwang/files/models/；
  4. chmod 777（sd.cpp mmap 需要可读）；
  5. 上报 node_agent info.models（由 node_agent._scan_local_models 扫描手机侧目录，
     PC 侧只负责推送）。

未连 adb / 无模型时如实说，不静默交空。
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_REMOTE_DIR = "/sdcard/Android/data/app.skynet.tianwang/files/models"
DEFAULT_SRC_DIR = os.environ.get("FLEET_MODELS_DIR") or os.path.join(
    os.path.expanduser("~/.fleet"), "models")

MODEL_EXTS = (".gguf", ".safetensors", ".bin", ".ckpt", ".pt", ".pth")


def _adb(adb_bin, *args, timeout=120):
    cmd = [adb_bin, *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"adb 执行失败：{type(e).__name__}: {e}")
    if r.returncode != 0:
        raise RuntimeError(f"adb 失败（rc={r.returncode}）：{r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def find_local_models(src_dir):
    """扫描 src_dir，返回 {filename: fullpath}。"""
    out = {}
    if not os.path.isdir(src_dir):
        return out
    for root, _, files in os.walk(src_dir):
        for f in files:
            if f.lower().endswith(MODEL_EXTS):
                out[f] = os.path.join(root, f)
    return out


def push_one(adb_bin, local_path, remote_dir=DEFAULT_REMOTE_DIR):
    """push 单个文件并 chmod 777，返回 remote 路径。"""
    name = os.path.basename(local_path)
    remote = remote_dir.rstrip("/") + "/" + name
    _adb(adb_bin, "push", local_path, remote)
    _adb(adb_bin, "shell", "chmod", "777", remote)
    return remote


def push_all(adb_bin="adb", src_dir=DEFAULT_SRC_DIR, remote_dir=DEFAULT_REMOTE_DIR,
             dry_run=False):
    """扫描 src_dir 并推送全部模型。返回 list[dict] 结果。"""
    locals_ = find_local_models(src_dir)
    results = []
    if not locals_:
        return results
    # 确保远端目录存在
    if not dry_run:
        try:
            _adb(adb_bin, "shell", "mkdir", "-p", remote_dir)
        except RuntimeError:
            pass
    for name, path in locals_.items():
        if dry_run:
            results.append({"name": name, "local": path, "remote": None, "ok": True, "dry": True})
            continue
        try:
            remote = push_one(adb_bin, path, remote_dir)
            results.append({"name": name, "local": path, "remote": remote, "ok": True})
        except RuntimeError as e:
            results.append({"name": name, "local": path, "remote": None, "ok": False, "err": str(e)})
    return results


def main():
    ap = argparse.ArgumentParser(description="推送 PC 模型到安卓手机")
    ap.add_argument("--adb", default="adb", help="adb 可执行文件")
    ap.add_argument("--src", default=DEFAULT_SRC_DIR, help="本地模型目录")
    ap.add_argument("--remote", default=DEFAULT_REMOTE_DIR, help="手机目标目录")
    ap.add_argument("--dry-run", action="store_true", help="只列出不推送")
    args = ap.parse_args()

    res = push_all(adb_bin=args.adb, src_dir=args.src, remote_dir=args.remote,
                   dry_run=args.dry_run)
    if not res:
        print(f"未在 {args.src} 找到模型文件（扩展名 {MODEL_EXTS}）")
        return 1
    ok = sum(1 for r in res if r["ok"])
    print(f"推送 {ok}/{len(res)} 成功")
    for r in res:
        status = "OK" if r["ok"] else "FAIL"
        print(f"  [{status}] {r['name']} -> {r.get('remote') or r.get('err')}")
    return 0 if ok == len(res) else 2


if __name__ == "__main__":
    # 自测：mock adb
    import tempfile
    d = tempfile.mkdtemp()
    mdir = os.path.join(d, "models")
    os.makedirs(mdir)
    # 创建假模型文件
    open(os.path.join(mdir, "tiny.gguf"), "w").write("x")
    open(os.path.join(mdir, "readme.txt"), "w").write("x")

    # mock adb 脚本
    mock = os.path.join(d, "mock_adb.py")
    with open(mock, "w") as f:
        f.write("import sys\n"
                "args=sys.argv[1:]\n"
                "if args[:2]==['push']:\n"
                "    open(args[2].replace('/sdcard/','/tmp/'),'w').write('ok') if False else None\n"
                "    print('1 file pushed')\n"
                "elif args[:2]==['shell','chmod']:\n"
                "    print('')\n"
                "elif args[:2]==['shell','mkdir']:\n"
                "    print('')\n"
                "else:\n"
                "    print('ok')\n")
    mock_exe = os.path.join(d, "mock_adb.bat" if os.name == "nt" else "mock_adb.sh")
    with open(mock_exe, "w") as f:
        if os.name == "nt":
            f.write(f'@echo off\nD:/tmp/pyembed/python.exe "{mock}" %*\n')
        else:
            f.write(f'#!/bin/sh\npython3 "{mock}" "$@"\n')
    if os.name != "nt":
        os.chmod(mock_exe, 0o755)

    res = push_all(adb_bin=mock_exe, src_dir=mdir, remote_dir="/sdcard/models", dry_run=False)
    assert len(res) == 1, res
    assert res[0]["ok"] is True, res
    assert res[0]["name"] == "tiny.gguf", res
    # dry-run
    res2 = push_all(adb_bin=mock_exe, src_dir=mdir, dry_run=True)
    assert res2[0].get("dry") is True, res2
    print("push_models 自测：扫描/推送/mock-adb/去除非模型扩展名 OK")

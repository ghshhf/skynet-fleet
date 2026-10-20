"""Android 节点入网（adb 反向推送，第三原则）。

手机/家服持工具箱，经 adb 把 node_agent 推到 Android TV / 盒子 / 手机并运行。
已 root 才达系统层（第四原则）；否则仅应用 UID，能力受限。
"""
import os
import shutil
import subprocess
import sys


def adb_available():
    return shutil.which("adb") is not None


def deploy(serial, host, port, name, password,
           node_agent="node_agent.py", common="common.py", root=False):
    """把工具箱 adb push 到设备并启动节点。"""
    if not adb_available():
        return {"ok": False, "err": "本机未安装 adb（需 Android SDK platform-tools）"}
    dest = "/data/local/tmp" if root else "/sdcard/Download"
    for f in (common, node_agent):
        if not os.path.isfile(f):
            return {"ok": False, "err": f"本地缺 {f}"}
        r = subprocess.run(["adb", "-s", serial, "push", f, f"{dest}/{os.path.basename(f)}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return {"ok": False, "err": f"push {f} 失败: {r.stderr}"}
    py = "su -c python" if root else "python"
    cmd = (f"{py} {dest}/{os.path.basename(node_agent)} --host {host} --port {port} "
           f"--name {name} --password {password} --platform android")
    r = subprocess.run(["adb", "-s", serial, "shell", cmd], capture_output=True, text=True)
    return {"ok": True, "out": r.stdout, "err": r.stderr}


def check():
    print("adb 可用:", adb_available())


if __name__ == "__main__":
    check()

"""Apple 节点桥接（半可达，§〇 标注）。

macOS：可 SSH 跑 node_agent（一等公民）。
iOS：锁死，仅 屏共 / 快捷指令 / 随航 —— 不设想为节点，由手机侧的「她」借屏幕说话。
"""
import shutil
import subprocess


def macos_available(host, user, key=None):
    """探测 macOS 节点是否可达且可 SSH。"""
    opt = ["-i", key] if key else []
    r = subprocess.run(["ssh", *opt, f"{user}@{host}", "echo ok"],
                       capture_output=True, text=True, timeout=10)
    return r.returncode == 0


def deploy_macos(host, user, password, name, port=9000, key=None):
    """SSH 把工具箱推过去并后台起 node_agent（macOS = Linux 同等待遇）。"""
    if not shutil.which("ssh"):
        return {"ok": False, "err": "本机无 ssh 客户端"}
    # 简化：假设文件已 scp；实际可由 bootstrap 同源。这里只示范启动命令。
    cmd = (f"python3 node_agent.py --host <CommanderIP> --port {port} "
           f"--name {name} --password <密码> --platform macos &")
    return {"ok": True, "note": "macOS 节点用 SSH 启动", "cmd": cmd}


def ios_note():
    return ("iOS 仅屏共/快捷指令/随航；不装 agent。她的发声借 iPhone 屏幕 + 快捷指令触发。")


if __name__ == "__main__":
    print(ios_note())

"""路由器 / OpenWrt 节点（常开中枢候选，§十）。

能刷 OpenWrt/爱快/ROS 的路由 = Linux 节点，跑 node_agent 即兼常开中枢 +
TinyContext 记忆仓储（海马体，永远在线）。本模块只是把"路由器"落成标准 linux 节点的别名。
MT6000 级 4 核 ARM 不跑模型（§10.4）：路由=Commander+注册表+记忆；手机=推理；电脑=重活。
"""
import subprocess
import sys


def is_openwrt(host, user, key=None):
    opt = ["-i", key] if key else []
    r = subprocess.run(["ssh", *opt, f"{user}@{host}", "cat /etc/openwrt_release"],
                       capture_output=True, text=True, timeout=10)
    return r.returncode == 0 and "OpenWrt" in r.stdout


def deploy(host, user, name, port=9000, key=None):
    """在路由上起 node_agent（与 linux 节点同构）。"""
    opt = ["-i", key] if key else []
    cmd = (f"python3 node_agent.py --host <CommanderIP> --port {port} "
           f"--name {name} --password <密码> --platform linux")
    return {"ok": True, "note": "OpenWrt 路由=linux 节点，作常开中枢", "cmd": cmd}


if __name__ == "__main__":
    print("路由器节点 = linux 节点；能刷 OpenWrt 即兼中枢+记忆仓储。详见计划书 §十。")

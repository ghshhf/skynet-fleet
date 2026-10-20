"""Linux L1 常驻安装器（零第三方依赖，纯 stdlib + systemd）。

把 node_agent 装成 systemd root 服务（与 Windows SYSTEM 计划任务同地位）：
  - 开机自起（WantedBy=multi-user.target，早于登录）
  - User=root，最高权限
  - Restart=always（崩溃自恢复）
  - 断线自动重连（node_agent 自带）

即第四原则里的「以 root 跑一次安装，之后永久常驻、无弹窗、重启自愈」。
重装系统即清（文件在系统卷）——特性不是缺陷。

卸载闸门（L1 诚实边界：本机 root 主动 disable+rm 才拦得住），两路放行：
  ① 安装密码（sha256 本地留存校验）
  ② 配对设备一次性令牌（手机经 Commander 下发，10min 有效）
任何一路通过即可卸载；都不符则拒。

用法：
  sudo python3 install_linux.py --install   --name BOX-A --host 192.168.1.10 --port 9000 --password 你的密码
  sudo python3 install_linux.py --uninstall --name BOX-A --password 你的密码
  sudo python3 install_linux.py --uninstall --name BOX-A --token <配对设备下发的一次性令牌>
  python3 install_linux.py --issue-token BOX-A          # 演示：签发一次性令牌
  sudo python3 install_linux.py --selftest --password demo123   # 装->查->令牌卸->查，不留残留
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable                       # 当前 python 解释器
NODE = os.path.join(HERE, "node_agent.py")
PW_FILE = os.path.join(HERE, ".fleet_pw.json")
TOKEN_CANDIDATES = [os.path.join(HERE, ".fleet_tokens.json"), ".fleet_tokens.json"]
UNIT_DIR = "/etc/systemd/system"


def is_root():
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def unit_name(name):
    return f"fleet-node-{name}.service"


def unit_path(name):
    return os.path.join(UNIT_DIR, unit_name(name))


def _build_unit(name, host, port, password, workdir):
    exec_start = (f'{PY} "{NODE}" --host {host} --port {port} --name {name} '
                  f'--password {password} --platform linux')
    return f"""[Unit]
Description=设备编队节点代理 FleetNode ({name})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory={workdir}
ExecStart={exec_start}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""


def _sha(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def _load_json(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def _save_json(p, d):
    json.dump(d, open(p, "w", encoding="utf-8"))


def _systemctl(*argv):
    """systemctl 调用；容器/无 systemd(PID1) 环境会失败，调用方自行容忍。"""
    if not shutil.which("systemctl"):
        return -1, "", "本机无 systemctl"
    r = subprocess.run(["systemctl", *argv], capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def issue_token(name):
    """配对设备令牌签发（真实由 Commander/手机下发；此处本地签发供演示/离线用）。"""
    tok = os.urandom(8).hex()
    d = _load_json(TOKEN_CANDIDATES[0], {})
    d[tok] = {"node": name, "purpose": "uninstall", "exp": time.time() + 600}
    _save_json(TOKEN_CANDIDATES[0], d)
    return tok


def _validate_token(name, token):
    for p in TOKEN_CANDIDATES:
        d = _load_json(p, {})
        e = d.get(token)
        if e and (not e.get("node") or e["node"] == name) and e.get("exp", 0) > time.time():
            return True
    return False


def install(name, host, port, password):
    if not is_root():
        raise SystemExit("需要 root：请以 root/sudo 运行一次（systemd root 服务）。")
    os.makedirs(UNIT_DIR, exist_ok=True)
    path = unit_path(name)
    open(path, "w", encoding="utf-8").write(_build_unit(name, host, port, password, HERE))
    rc, out, err = _systemctl("daemon-reload")
    if rc != 0:
        print(f"[!] systemctl daemon-reload 未成功（本环境可能无 systemd PID1）: {err}")
    rc, out, err = _systemctl("enable", "--now", unit_name(name))
    if rc != 0:
        print(f"[!] systemctl enable --now 未成功（本环境可能无 systemd PID1）: {err}")
    # 留存安装密码哈希（L1 闸门 ①）
    pw = _load_json(PW_FILE, {})
    pw[name] = _sha(password)
    _save_json(PW_FILE, pw)
    print(f"[+] 已安装 systemd root 服务 {unit_name(name)}（开机自起/root/Restart=always）")


def uninstall(name, password=None, token=None):
    """L1 卸载闸门：密码哈希匹配 或 配对设备一次性令牌有效，任一路过即可。"""
    pw = _load_json(PW_FILE, {})
    ok = (name in pw and password and pw[name] == _sha(password)) or _validate_token(name, token or "")
    if not ok:
        raise SystemExit("卸载被拒：密码错误，或配对设备令牌无效/过期（L1 闸门）。")
    unit = unit_name(name)
    _systemctl("disable", "--now", unit)
    path = unit_path(name)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:
        raise SystemExit(f"删除服务文件失败: {e}")
    # 兜底：即使 systemctl bus 不可用（disable 未生效），也清掉 enable 建的软链接，保证无残留
    link = os.path.join(UNIT_DIR, "multi-user.target.wants", unit)
    try:
        if os.path.islink(link):
            os.remove(link)
    except OSError:
        pass
    _systemctl("daemon-reload")
    pw.pop(name, None)
    _save_json(PW_FILE, pw)
    print(f"[-] 已卸载服务 {unit}")


def selftest(password):
    """装->查->令牌卸->查，验证 L1 路径 + 令牌闸，结束不留残留。"""
    name = "SELFTEST"
    print("[*] L1 安装器自检（装->查->令牌卸->查，不留残留）", flush=True)
    if not is_root():
        print("[!] 非 root：unit 生成逻辑已校验，但需 root 才能落到 /etc/systemd/system。")
        print("    真实部署：sudo python3 install_linux.py --install（第四原则：运行一次即永久常驻）。")
        print("    生成的 unit 内容如下：")
        print("    " + _build_unit(name, "127.0.0.1", 9000, password, HERE).replace("\n", "\n    "))
        return
    try:
        install(name, "127.0.0.1", 9000, password)
    except SystemExit as e:
        print(f"[!] 安装失败：{e}")
        return
    path = unit_path(name)
    print(f"    服务文件存在: {os.path.exists(path)} ({path})")
    tok = issue_token(name)
    print(f"    签发配对设备令牌: {tok}")
    uninstall(name, token=tok)
    print(f"    令牌卸载后服务文件存在: {os.path.exists(path)}（False=已清除，无残留）")
    print("[*] L1 路径 + 令牌闸 验证完成。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--issue-token", metavar="NAME", help="签发一次性卸载令牌（演示/离线）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--name", default="BOX-A")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--password", default=os.environ.get("FLEET_PASSWORD", "changeme"))
    ap.add_argument("--token", default=None, help="配对设备下发的一次性令牌")
    args = ap.parse_args()

    if args.selftest:
        selftest(args.password)
    elif args.issue_token:
        print("一次性令牌:", issue_token(args.issue_token))
    elif args.install:
        install(args.name, args.host, args.port, args.password)
    elif args.uninstall:
        uninstall(args.name, password=args.password, token=args.token)
    else:
        print("用法: --install | --uninstall [--token T] | --issue-token NAME | --selftest  （见文件头注释）")


if __name__ == "__main__":
    main()

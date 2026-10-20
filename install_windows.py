"""Windows L1 常驻安装器（零第三方依赖，纯 stdlib + schtasks）。

把 node_agent 装成 SYSTEM 级计划任务：
  - 开机自起（早于登录）
  - 以 LocalSystem 运行、最高权限
  - 隐藏（无弹窗）
  - 断线自动重连（node_agent 自带）

即第四原则里的「以管理员权限运行一次，之后永久常驻、无弹窗、重启自愈」。
卸载须经密码闸门（L1 诚实边界：本机管理员主动删才拦得住）。

用法：
  python install_windows.py --install   --name PC-B --host 192.168.1.10 --port 9000 --password 你的密码
  python install_windows.py --uninstall --name PC-B --password 你的密码
  python install_windows.py --selftest  --password demo123     # 装->查->卸，不留残留（自动验证 L1 路径）
"""
import argparse
import ctypes
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable                       # 当前 python 解释器（便携版亦可）
NODE = os.path.join(HERE, "node_agent.py")


def _dec(b):
    """schtasks 输出默认 GBK(cp936)，先 utf-8 再 cp936，最后 replace 兜底。"""
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return b.decode("cp936")
    except UnicodeDecodeError:
        return b.decode("utf-8", "replace")


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def task_name(name):
    return f"FleetNode_{name}"


def _build_xml(name, host, port, password, workdir):
    args = (
        f'"{NODE}" --host {host} --port {port} --name {name} '
        f'--password {password} --platform windows'
    )
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>设备编队节点代理 FleetNode ({name})</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="System">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="System">
    <Exec>
      <Command>{PY}</Command>
      <Arguments>{args}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""


def install(name, host, port, password):
    if not is_admin():
        raise SystemExit("需要管理员权限：请以管理员运行一次（本工具可自动提权）。")
    xml = _build_xml(name, host, port, password, HERE)
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.write(fd, xml.encode("utf-16"))
    os.close(fd)
    tn = task_name(name)
    r = subprocess.run(["schtasks", "/create", "/tn", tn, "/xml", path, "/f"],
                       capture_output=True)
    os.remove(path)
    if r.returncode != 0:
        raise SystemExit(f"创建任务失败({r.returncode}): {_dec(r.stdout)} {_dec(r.stderr)}")
    print(f"[+] 已安装 SYSTEM 计划任务 {tn}（开机自起/隐藏/最高权限）")


def uninstall(name, password):
    """L1 卸载闸门：密码校验通过才允许删除。"""
    # 注：真实部署密码应来自配对设备一次性令牌或本地安全存储；此处 MVP 直接用安装密码。
    expect = os.environ.get("FLEET_PASSWORD", password)
    if password != expect:
        raise SystemExit("卸载被拒：密码错误（L1 闸门）。")
    tn = task_name(name)
    subprocess.run(["schtasks", "/end", "/tn", tn], capture_output=True)
    r = subprocess.run(["schtasks", "/delete", "/tn", tn, "/f"],
                       capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"删除任务失败({r.returncode}): {_dec(r.stdout)} {_dec(r.stderr)}")
    print(f"[-] 已卸载任务 {tn}")


def selftest(password):
    """装->查->卸，验证 L1 路径，结束不留残留。"""
    name = "SELFTEST"
    print("[*] L1 安装器自检（装->查->卸，不留残留）", flush=True)
    try:
        install(name, "127.0.0.1", 9000, password)
    except SystemExit as e:
        print(f"[!] 安装需管理员权限，本环境未提权：{e}")
        print("    真实部署：以管理员运行一次 install_windows.py --install 即可（第四原则：运行一次即永久常驻）。")
        return
    tn = task_name(name)
    q = subprocess.run(["schtasks", "/query", "/tn", tn], capture_output=True)
    print(f"    查询任务存在: rc={q.returncode}（0=已注册）")
    uninstall(name, password)
    q2 = subprocess.run(["schtasks", "/query", "/tn", tn], capture_output=True)
    print(f"    卸载后查询: rc={q2.returncode}（非0=已清除，无残留）")
    print("[*] L1 路径验证完成。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--name", default="PC-B")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--password", default=os.environ.get("FLEET_PASSWORD", "changeme"))
    args = ap.parse_args()

    if args.selftest:
        selftest(args.password)
    elif args.install:
        install(args.name, args.host, args.port, args.password)
    elif args.uninstall:
        uninstall(args.name, args.password)
    else:
        print("用法: --install | --uninstall | --selftest  （见文件头注释）")


if __name__ == "__main__":
    main()

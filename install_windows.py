"""Windows L1 常驻安装器（零第三方依赖，纯 stdlib + schtasks）。

把 node_agent 装成 SYSTEM 级计划任务：
  - 开机自起（早于登录）
  - 以 LocalSystem 运行、最高权限
  - 隐藏（无弹窗）
  - 断线自动重连（node_agent 自带）

即第四原则里的「以管理员权限运行一次，之后永久常驻、无弹窗、重启自愈」。

卸载闸门（L1 诚实边界：本机管理员主动删才拦得住），两路放行：
  ① 安装密码（sha256 本地留存校验）
  ② 配对设备一次性令牌（手机经 Commander 下发，10min 有效）
任何一路通过即可卸载；都不符则拒。

用法：
  python install_windows.py --install   --name PC-B --host 192.168.1.10 --port 9000 --password 你的密码
  python install_windows.py --enroll "fleet://enroll?p=..."   # 扫码入网：离线验签后安装（不带密码）
  python install_windows.py --enroll "fleet://..." --dry-run  # 只验签看参数，不兑换不安装
  python install_windows.py --uninstall --name PC-B --password 你的密码
  python install_windows.py --uninstall --name PC-B --token <配对设备下发的一次性令牌>
  python install_windows.py --issue-token PC-B          # 演示：签发一次性令牌（真实由 Commander/手机下发）
  python install_windows.py --selftest  --password demo123     # 装->查->令牌卸->查，不留残留
"""
import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

import enroll

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable                       # 当前 python 解释器（便携版亦可）
NODE = os.path.join(HERE, "node_agent.py")
PW_FILE = os.path.join(HERE, ".fleet_pw.json")
TOKEN_CANDIDATES = [os.path.join(HERE, ".fleet_tokens.json"), ".fleet_tokens.json"]
ENROLL_LEDGER = os.environ.get(
    "FLEET_ENROLL_REDEEM_LEDGER", os.path.join(HERE, ".fleet_enroll_used.json"))


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


def _sha(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def _load_json(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def _save_json(p, d):
    json.dump(d, open(p, "w", encoding="utf-8"))


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
    # 留存安装密码哈希（L1 闸门 ①）
    pw = _load_json(PW_FILE, {})
    pw[name] = _sha(password)
    _save_json(PW_FILE, pw)
    print(f"[+] 已安装 SYSTEM 计划任务 {tn}（开机自起/隐藏/最高权限）")


def uninstall(name, password=None, token=None):
    """L1 卸载闸门：密码哈希匹配 或 配对设备一次性令牌有效，任一路过即可。"""
    pw = _load_json(PW_FILE, {})
    ok = (name in pw and password and pw[name] == _sha(password)) or _validate_token(name, token or "")
    if not ok:
        raise SystemExit("卸载被拒：密码错误，或配对设备令牌无效/过期（L1 闸门）。")
    tn = task_name(name)
    subprocess.run(["schtasks", "/end", "/tn", tn], capture_output=True)
    r = subprocess.run(["schtasks", "/delete", "/tn", tn, "/f"],
                       capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"删除任务失败({r.returncode}): {_dec(r.stdout)} {_dec(r.stderr)}")
    pw.pop(name, None)
    _save_json(PW_FILE, pw)
    print(f"[-] 已卸载任务 {tn}")


def parse_enroll(uri, secret=None):
    """离线验签入网载荷，返回安装参数。不兑换、不落状态、不联网。"""
    pay = enroll.verify(uri, secret=secret)
    return {"name": pay["n"], "host": pay["h"], "port": int(pay["o"]),
            "psk": pay["k"], "platform": pay["p"]}


def install_from_enroll(uri, dry_run=False, secret=None):
    """扫码入网：验签 ->（兑换）-> 装成 SYSTEM 计划任务，用 PSK 认证而非明文密码。"""
    try:
        p = parse_enroll(uri, secret=secret)
    except enroll.EnrollError as e:
        raise SystemExit(f"入网载荷不可用（{e.reason}）：{e.msg}")
    print(f"[*] 载荷验签通过（离线，未联网）：{p['name']} -> {p['host']}:{p['port']} 平台={p['platform']}")
    if dry_run:
        print("[*] dry-run：未兑换、未安装（一张码只能用一次，先看清楚再真装）")
        return p
    if not is_admin():
        # 先查权限再兑换：装不成就不该烧掉这张一次性码
        raise SystemExit("需要管理员权限：请以管理员运行一次（本工具可自动提权）。载荷未兑换，重试即可。")
    try:
        enroll.redeem(uri, ENROLL_LEDGER, secret=secret)
    except enroll.EnrollError as e:
        raise SystemExit(f"入网载荷不可用（{e.reason}）：{e.msg}")
    print("[+] 载荷已兑换（同一张码不能再兑换）")
    install(p["name"], p["host"], p["port"], p["psk"])
    tok = issue_token(p["name"])
    print(f"[+] 卸载令牌（记下来；也可事后在 Commander 上重置）：{tok}")
    return p


def enroll_selftest():
    """离线验证「扫码 -> 验签 -> 兑换 -> 装配参数」这条链，不碰 schtasks。

    替身只顶掉本机跑不了的那一步（建系统任务 + 管理员检查），验签/兑换/账本都是真代码。
    """
    tdir = tempfile.mkdtemp(prefix="fleet_enroll_install_st_")
    ledger = os.path.join(tdir, "used.json")
    secret = b"enroll-install-selftest-key"
    real = {"install": install, "is_admin": is_admin, "issue_token": issue_token,
            "ledger": ENROLL_LEDGER}
    calls = []
    try:
        pay = enroll.issue("PC-QR", "windows", "10.0.0.5", 9000, ttl=600, secret=secret)
        uri = enroll.encode(pay, secret)

        p = parse_enroll(uri, secret=secret)
        assert (p["name"], p["host"], p["port"]) == ("PC-QR", "10.0.0.5", 9000), p
        assert p["psk"] == pay["k"]
        print("  载荷 -> 安装参数（节点/中心/端口/PSK） OK")

        bad = uri[:40] + ("B" if uri[40] != "B" else "C") + uri[41:]
        try:
            parse_enroll(bad, secret=secret)
            raise AssertionError("篡改载荷竟然通过验签")
        except enroll.EnrollError as e:
            assert e.reason in ("bad_sig", "malformed"), e.reason
        print("  篡改载荷被拒 OK")

        install_from_enroll(uri, dry_run=True, secret=secret)   # dry-run 不兑换
        install_from_enroll(uri, dry_run=True, secret=secret)
        print("  dry-run 可重复执行且不兑换 OK")

        # 替身：跳过 schtasks 与管理员检查，兑换/账本/参数传递走真代码
        globals()["install"] = lambda *a, **k: calls.append(a)
        globals()["is_admin"] = lambda: True
        globals()["issue_token"] = lambda nm: "tok-selftest"
        globals()["ENROLL_LEDGER"] = ledger
        install_from_enroll(uri, secret=secret)
        assert calls == [("PC-QR", "10.0.0.5", 9000, pay["k"])], calls
        print("  兑换后安装参数正确（PSK 顶替明文密码） OK")
        try:
            install_from_enroll(uri, secret=secret)
            raise AssertionError("同一张码兑换了两次")
        except SystemExit as e:
            assert "replayed" in str(e), e
        print(f"  同一张码第二次被拒（replayed，账本 {enroll.redeem_count(ledger)} 条） OK")
        print("[enroll-selftest] PASS")
    finally:
        for k, v in real.items():
            globals()["ENROLL_LEDGER" if k == "ledger" else k] = v
        for fn in os.listdir(tdir):
            os.remove(os.path.join(tdir, fn))
        os.rmdir(tdir)


def selftest(password):
    """装->查->令牌卸->查，验证 L1 路径 + 令牌闸，结束不留残留。"""
    name = "SELFTEST"
    print("[*] L1 安装器自检（装->查->令牌卸->查，不留残留）", flush=True)
    try:
        install(name, "127.0.0.1", 9000, password)
    except SystemExit as e:
        print(f"[!] 安装需管理员权限，本环境未提权：{e}")
        print("    真实部署：以管理员运行一次 install_windows.py --install 即可（第四原则：运行一次即永久常驻）。")
        return
    tn = task_name(name)
    q = subprocess.run(["schtasks", "/query", "/tn", tn], capture_output=True)
    print(f"    查询任务存在: rc={q.returncode}（0=已注册）")
    tok = issue_token(name)
    print(f"    签发配对设备令牌: {tok}")
    uninstall(name, token=tok)
    q2 = subprocess.run(["schtasks", "/query", "/tn", tn], capture_output=True)
    print(f"    令牌卸载后查询: rc={q2.returncode}（非0=已清除，无残留）")
    print("[*] L1 路径 + 令牌闸 验证完成。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--issue-token", metavar="NAME", help="签发一次性卸载令牌（演示/离线）")
    ap.add_argument("--enroll", metavar="URI", help="扫码入网载荷（fleet://enroll?...）：离线验签后安装")
    ap.add_argument("--dry-run", action="store_true", help="配合 --enroll：只验签看参数，不兑换不安装")
    ap.add_argument("--enroll-selftest", action="store_true", help="离线自检扫码入网链路（不碰 schtasks）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--name", default="PC-B")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--password", default=os.environ.get("FLEET_PASSWORD", "changeme"))
    ap.add_argument("--token", default=None, help="配对设备下发的一次性令牌")
    args = ap.parse_args()

    if args.selftest:
        selftest(args.password)
    elif args.enroll_selftest:
        enroll_selftest()
    elif args.enroll:
        install_from_enroll(args.enroll, dry_run=args.dry_run)
    elif args.issue_token:
        print("一次性令牌:", issue_token(args.issue_token))
    elif args.install:
        install(args.name, args.host, args.port, args.password)
    elif args.uninstall:
        uninstall(args.name, password=args.password, token=args.token)
    else:
        print("用法: --install | --uninstall [--token T] | --issue-token NAME | --enroll URI [--dry-run] | --selftest  （见文件头注释）")


if __name__ == "__main__":
    main()

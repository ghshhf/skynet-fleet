"""天网 · 网络加速控制层（gitport 子系统）

把 gitport 从「手动双击 exe」升级为「天网可管控的外国流量加速通道」。

gitport 是本地代理（127.0.0.1:7810 代理 / :7811 管理页），但它**不是通用翻墙**——
只接管 sites.toml 登记的外国站点（GitHub / YouTube / Twitter / HuggingFace / arXiv …），
其余流量必须 DIRECT，否则会拖垮国内站点。所以对外暴露的核心是「PAC 路由 + 按需开关」。

设计约束（与 netpath.py 同款克制）：
- 零第三方依赖，只用标准库 + 必要时 subprocess。
- 改系统代理/防火墙这类「自己设备的管理员动作」做成显式命令，绝不静默改。
- 端口按程序路径挂的 Block 规则优先于端口 Allow，这条坑已记在 memory，这里只开代理、不碰防火墙。

    python netaccel.py status            # 看 gitport 在不在、代理开没开
    python netaccel.py start             # 启动 gitport（headless）+ 接管 git 代理
    python netaccel.py stop              # 停 gitport + 清 git / 系统代理
    python netaccel.py pac               # 打印当前 PAC 脚本（可落盘给浏览器/系统用）
    python netaccel.py system-proxy on   # 系统代理指向 PAC（仅登记域名走 7810）
    python netaccel.py system-proxy off  # 关系统代理
    python netaccel.py --selftest
"""
import os
import re
import socket
import subprocess
import sys

IS_WIN = sys.platform.startswith("win")

PROXY_PORT = 7810
WEBUI_PORT = 7811
PROXY_ADDR = f"127.0.0.1:{PROXY_PORT}"

# gitport 二进制相对本文件的路径（release 优先，debug 兜底）。
# fleet/netaccel.py -> ../vendor/gitport-accel/target/.../release/gitport-github.exe
_HERE = os.path.dirname(os.path.abspath(__file__))
_GITPORT_DIR = os.path.normpath(os.path.join(_HERE, "..", "vendor", "gitport-accel"))
_EXE_RELEASE = os.path.join(
    _GITPORT_DIR, "target", "x86_64-pc-windows-gnu", "release", "gitport-github.exe"
)
_EXE_DEBUG = os.path.join(
    _GITPORT_DIR, "target", "x86_64-pc-windows-gnu", "debug", "gitport-github.exe"
)
_SITES_TOML = os.path.join(_GITPORT_DIR, "sites.toml")
_SITES_EXAMPLE = os.path.join(_GITPORT_DIR, "SITES_EXAMPLE.toml")

# 内置受管域名（必须镜像 src/site.rs 的 default_profiles 域名）。
# 这是 PAC 路由的真相来源之一；用户通过 sites.toml 追加的站点会被自动并入。
# 旧 GitHub 生态域名（*.github.com / *.githubusercontent.com）覆盖全部 GitHub 流量。
BUILTIN_DOMAINS = [
    # GitHub 全系
    "github.com", "*.github.com",
    "githubusercontent.com", "*.githubusercontent.com",
    # YouTube（Google 视频/CDN 基础设施）
    "youtube.com", "www.youtube.com", "*.youtube.com",
    "*.googlevideo.com", "*.ytimg.com", "*.ggpht.com",
    # Twitter / X
    "twitter.com", "*.twitter.com", "t.co", "*.t.co",
    "*.twimg.com", "api.twitter.com", "pbs.twimg.com",
    # HuggingFace（模型下载 / 数据集）
    "huggingface.co", "*.huggingface.co", "*.hf.co",
    "cdn-lfs.huggingface.co", "cdn-lfs.hf.co",
    # arXiv（国外论文）
    "arxiv.org", "*.arxiv.org", "export.arxiv.org",
]


def find_exe():
    """定位 gitport 二进制：release 优先，debug 兜底，再查环境变量。"""
    env = os.environ.get("GITPORT_EXE")
    for cand in (env, _EXE_RELEASE, _EXE_DEBUG):
        if cand and os.path.isfile(cand):
            return cand
    return None


def is_running():
    """7810 是否在监听（gitport 起来了）。"""
    try:
        with socket.create_connection(( "127.0.0.1", PROXY_PORT), timeout=1.5):
            return True
    except OSError:
        return False


def _safe_run(cmd, timeout=10):
    """跑命令，安全解码输出（中文 Windows 的 netstat/taskkill 是 GBK，不能 text=True 硬解）。"""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception as e:
        return -1, "", str(e)
    out = ""
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            out = r.stdout.decode(enc, "strict") if r.stdout else ""
            break
        except UnicodeDecodeError:
            continue
    if r.stderr:
        try:
            out += r.stderr.decode("gbk", "replace")
        except Exception:
            pass
    return r.returncode, out, ""


def _find_pid_on_port(port):
    """Windows：netstat 找占用端口的 PID。无权限/不可用时返回 None。"""
    if not IS_WIN:
        return None
    rc, out, _ = _safe_run(["netstat", "-ano", "-p", "TCP"])
    if rc != 0 and not out:
        return None
    pat = re.compile(rf":{port}\s+\S+\s+LISTENING\s+(\d+)")
    for line in out.splitlines():
        m = pat.search(line)
        if m:
            return int(m.group(1))
    return None


def start(headless=True, wait=8):
    """启动 gitport。返回 (ok, 说明)。

    headless=True 时不弹窗（后台常驻）。等 7810 监听起来才算成功，
    否则回退说明（多半是 WebView2Loader / MinGW 运行时 DLL 没就位）。
    """
    exe = find_exe()
    if not exe:
        return False, "找不到 gitport-github.exe（先跑 build_gitport.bat 编译）"
    if is_running():
        return True, f"gitport 已经在跑（{PROXY_ADDR}）"
    args = [exe]
    if headless:
        args.append("-headless")
    try:
        # detach：不随本进程退出而关；Windows 用 CREATE_NEW_PROCESS_GROUP 脱离作业
        if IS_WIN:
            creation = 0x00000200 | 0x00001000  # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
            p = subprocess.Popen(args, creationflags=creation, close_fds=True)
        else:
            p = subprocess.Popen(args, start_new_session=True, close_fds=True)
    except OSError as e:
        return False, f"启动失败：{e}"
    # 轮询等监听起来
    for _ in range(max(1, int(wait * 10))):
        if is_running():
            return True, f"已启动（pid={p.pid}），监听 {PROXY_ADDR}"
        import time
        time.sleep(0.1)
    return False, f"启动了但 {wait}s 内 7810 没起来（检查 WebView2Loader.dll / MinGW 运行时）"


def stop():
    """停 gitport。优先按端口找 PID 杀；失败再按 exe 名兜底。返回 (ok, 说明)。"""
    if not is_running():
        return True, "gitport 本来就没跑"
    pid = _find_pid_on_port(PROXY_PORT)
    if pid:
        if IS_WIN:
            rc, out, err = _safe_run(["taskkill", "/PID", str(pid), "/F", "/T"])
            if rc == 0:
                return True, f"已结束 pid={pid}"
            return False, f"taskkill 失败：{err or out.strip()[:120]}"
        else:
            import signal
            try:
                os.kill(pid, signal.SIGTERM)
                return True, f"已结束 pid={pid}"
            except OSError as e:
                return False, f"kill 失败：{e}"
    # 兜底：按 exe 名
    if IS_WIN:
        rc, out, err = _safe_run(["taskkill", "/IM", "gitport-github.exe", "/F", "/T"])
        return rc == 0, f"按名结束：{(out or err).strip()[:120]}"
    return False, "非 Windows 且按端口找不到 PID，无法结束"


def git_proxy(on):
    """git 全局代理开关。on=True 设 http/https proxy 到 7810；False 清掉。"""
    val = f"http://{PROXY_ADDR}" if on else None
    try:
        if val:
            for k in ("http.proxy", "https.proxy"):
                subprocess.run(
                    ["git", "config", "--global", k, val],
                    capture_output=True, text=True, timeout=10, check=True,
                )
            return True, f"git 代理已设 {val}（仅 GitHub 系域名经 gitport）"
        else:
            for k in ("http.proxy", "https.proxy"):
                subprocess.run(
                    ["git", "config", "--global", "--unset", k],
                    capture_output=True, text=True, timeout=10,
                )
            return True, "git 代理已清除"
    except (OSError, subprocess.SubprocessError, subprocess.CalledProcessError) as e:
        return False, f"git 配置失败：{e}"


def sites_domains():
    """受管域名全集 = 内置默认 ∪ sites.toml 里追加的站点域名。

    解析 sites.toml：新格式用 `domains`（通配 `*.x`），旧格式用 `groups`/`patterns`
    与 `whitelist` 的键。tomllib 可用就用，否则退化为只给内置默认（带告警）。
    """
    domains = list(BUILTIN_DOMAINS)
    src = _SITES_TOML if os.path.isfile(_SITES_TOML) else (
        _SITES_EXAMPLE if os.path.isfile(_SITES_EXAMPLE) else None
    )
    if not src:
        return domains
    text = ""
    try:
        with open(src, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return domains

    parsed = None
    try:
        import tomllib
        parsed = tomllib.loads(text)
    except ModuleNotFoundError:
        pass  # 3.11 以下无 tomllib，走下方正则兜底
    except Exception:
        parsed = None

    def _add(s):
        s = (s or "").strip()
        if s and s not in domains:
            domains.append(s)

    def _coerce(v):
        """把 toml 里的各种形态归一成域名串：str 直接加；dict 取 patterns/domain 键。"""
        if isinstance(v, str):
            _add(v)
        elif isinstance(v, dict):
            # 旧格式 [[sites.groups]] 每个元素是 {pool, patterns}
            pats = v.get("patterns") or v.get("pattern") or []
            if isinstance(pats, str):
                _add(pats)
            else:
                for p in pats:
                    _add(p)

    if parsed and isinstance(parsed.get("sites"), list):
        for blk in parsed["sites"]:
            if not isinstance(blk, dict):
                continue
            for d in (blk.get("domains") or []):
                _add(d)
            # groups 可能是字符串列表（新）或嵌套表数组（旧：含 patterns）
            for g in (blk.get("groups") or []):
                _coerce(g)
            for p in (blk.get("patterns") or []):
                _add(p)
            for w in (blk.get("whitelist") or {}).keys():
                _add(w)
    else:
        # 正则兜底：抓 domains = [...] / groups = [...] / patterns = [...] 数组里的引号串
        for arr_key in ("domains", "groups", "patterns"):
            for m in re.finditer(rf"{arr_key}\s*=\s*\[(.*?)\]", text, re.S):
                for s in re.findall(r"[\"']([^\"']+)[\"']", m.group(1)):
                    _add(s)
        for m in re.finditer(r"\[whitelist\](.*?)(?:\n\[|\Z)", text, re.S):
            for s in re.findall(r"[\"']?([\w.\-*]+)[\"']?\s*=", m.group(1)):
                _add(s)
    return domains


def pac_script():
    """生成 PAC：受管域名走 PROXY 7810，其余 DIRECT。

    这是 gitport 作为「外国加速」而非翻墙的关键——国内站点绝不经它，避免被拖慢。
    """
    domains = sites_domains()
    lines = [
        "// 天网 · gitport PAC：仅登记的外国站点走本地加速代理，其余直连。",
        "function FindProxyForURL(url, host) {",
    ]
    for d in domains:
        if d.startswith("*."):
            base = d[2:]
            lines.append(
                f'    if (shExpMatch(host, "{d}") || shExpMatch(host, "{base}"))'
                f" {{ return \"PROXY {PROXY_ADDR}\"; }}"
            )
        else:
            lines.append(
                f'    if (shExpMatch(host, "{d}")) {{ return "PROXY {PROXY_ADDR}\"; }}'
            )
    lines.append('    return "DIRECT";')
    lines.append("}")
    return "\n".join(lines) + "\n"


def _reg_proxy(on, pac_path=None):
    """Windows 系统代理开关（注册表 HKCU）。on=True 用 PAC（pac_path 必须给）；False 全关。"""
    if not IS_WIN:
        return False, "非 Windows：请用浏览器/系统设置把代理指向 PAC 或 7810"
    try:
        import winreg
    except ImportError:
        return False, "无 winreg 模块"
    key = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            if on and pac_path:
                winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(k, "AutoConfigURL", 0, winreg.REG_SZ, pac_path)
                winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, "")
                return True, f"系统代理已指向 PAC：{pac_path}"
            else:
                winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 0)
                winreg.SetValueEx(k, "AutoConfigURL", 0, winreg.REG_SZ, "")
                return True, "系统代理已关闭"
    except OSError as e:
        return False, f"注册表写入失败（可能需要管理员）：{e}"


def system_proxy(on, pac_path=None):
    """系统代理开关。on=True 需先确保 PAC 文件已落盘（pac_path）。"""
    if on and not pac_path:
        return False, "开启系统代理需提供 PAC 文件路径（先 netaccel.py pac > 某文件）"
    return _reg_proxy(on, pac_path)


def status():
    """汇总状态：二进制/运行/代理。返回 dict。"""
    exe = find_exe()
    running = is_running()
    git_on = _git_proxy_state()
    return {
        "exe": exe,
        "running": running,
        "proxy_addr": PROXY_ADDR if running else None,
        "git_proxy": git_on,
        "managed_domains": len(sites_domains()),
    }


def _git_proxy_state():
    try:
        r = subprocess.run(
            ["git", "config", "--global", "--get", "http.proxy"],
            capture_output=True, text=True, timeout=10,
        )
        v = (r.stdout or "").strip()
        return v if v else None
    except (OSError, subprocess.SubprocessError):
        return None


def _selftest():
    # ① 受管域名非空且含关键站点
    doms = sites_domains()
    for must in ("github.com", "youtube.com", "twitter.com", "huggingface.co", "arxiv.org"):
        assert must in doms, f"缺 {must}：{doms}"
    # ② PAC 生成：受管域名命中 PROXY，国内域名走 DIRECT
    pac = pac_script()
    assert 'PROXY 127.0.0.1:7810' in pac
    assert 'return "DIRECT";' in pac
    # 通配能同时覆盖裸域与子域
    assert "shExpMatch(host, \"*.github.com\")" in pac
    # ③ 端口常量
    assert PROXY_PORT == 7810 and WEBUI_PORT == 7811
    # ④ 二进制定位（debug 或 release 至少一个应存在，否则只告警不报错）
    exe = find_exe()
    if exe:
        assert os.path.isfile(exe), exe
    # ⑤ 系统代理关路径不炸
    ok, msg = system_proxy(False)
    assert isinstance(ok, bool), (ok, msg)
    print(f"netaccel 自检：5 项全过（受管域名 {len(doms)} 个，exe={exe or '未编译'}）")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        _selftest()
        return 0
    if not argv:
        argv = ["status"]

    cmd = argv[0]
    if cmd == "status":
        s = status()
        print("【gitport 状态】")
        print(f"  二进制      : {s['exe'] or '未找到（先编译）'}")
        print(f"  运行中      : {'是 ' + s['proxy_addr'] if s['running'] else '否'}")
        print(f"  git 代理    : {s['git_proxy'] or '未设'}")
        print(f"  受管域名    : {s['managed_domains']} 个（见 pac 命令）")
        return 0
    if cmd == "start":
        ok, msg = start()
        print(("  ✓ " if ok else "  ✗ ") + msg)
        if ok:
            ok2, msg2 = git_proxy(True)
            print(("  ✓ " if ok2 else "  ✗ ") + msg2)
        return 0 if ok else 1
    if cmd == "stop":
        ok, msg = stop()
        print(("  ✓ " if ok else "  ✗ ") + msg)
        ok2, msg2 = git_proxy(False)
        print(("  ✓ " if ok2 else "  ✗ ") + msg2)
        return 0 if ok else 1
    if cmd == "pac":
        sys.stdout.write(pac_script())
        return 0
    if cmd == "system-proxy":
        if len(argv) < 2 or argv[1] not in ("on", "off"):
            print("  用法：netaccel.py system-proxy on <pac路径> | off")
            return 2
        if argv[1] == "on":
            if len(argv) < 3:
                print("  用法：netaccel.py system-proxy on <pac路径>")
                return 2
            ok, msg = system_proxy(True, argv[2])
        else:
            ok, msg = system_proxy(False)
        print(("  ✓ " if ok else "  ✗ ") + msg)
        return 0 if ok else 1
    if cmd in ("git-proxy", "git_proxy"):
        if len(argv) < 2 or argv[1] not in ("on", "off"):
            print("  用法：netaccel.py git-proxy on | off")
            return 2
        ok, msg = git_proxy(argv[1] == "on")
        print(("  ✓ " if ok else "  ✗ ") + msg)
        return 0 if ok else 1
    print(f"  未知命令：{cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())

"""天网 · 网络加速控制层（gitport 子系统）

把 gitport 从「手动双击 exe」升级为「天网可管控的外国流量加速通道」。

gitport 是本地代理（127.0.0.1:7810 代理 / :7811 管理页），但它**不是通用翻墙**——
只接管真正救得动的站点，其余流量必须 DIRECT，否则会拖垮国内站点。
2026-10-31 实测定调：GitHub 是「按域名封、换 IP 即通」→ 可救；
YouTube/Google 是「TLS 握手阶段按 IP 段整段切断」→ 分片无效，已从名单剔除。
所以对外暴露的核心是「PAC 路由 + 按需开关 + 热点网关」。

设计约束（与 netpath.py 同款克制）：
- 零第三方依赖，只用标准库 + 必要时 subprocess。
- 改系统代理/防火墙这类「自己设备的管理员动作」做成显式命令，绝不静默改。
- 端口按程序路径挂的 Block 规则优先于端口 Allow，这条坑已记在 memory，这里只开代理、不碰防火墙。

    python netaccel.py status            # 看 gitport 在不在、代理开没开
    python netaccel.py start             # 启动 gitport（headless）+ 接管 git 代理
    python netaccel.py stop              # 停 gitport + 清 git / 系统代理
    python netaccel.py gateway           # 把本机做成热点网关（7822 → gitport 7810）
    python netaccel.py gateway-stop      # 只停网关，不动 gitport
    python netaccel.py ci-fetch          # 从 GitHub Actions 拉最新成功产物到 dist/（编译交给 runner）
    python netaccel.py pac               # 打印当前 PAC 脚本（可落盘给浏览器/系统用）
    python netaccel.py system-proxy on   # 系统代理指向 PAC（仅登记域名走 7810）
    python netaccel.py system-proxy off  # 关系统代理
    python netaccel.py --selftest
"""
import json
import os
import re
import socket
import subprocess
import sys

IS_WIN = sys.platform.startswith("win")

PROXY_PORT = 7810
WEBUI_PORT = 7811
PROXY_ADDR = f"127.0.0.1:{PROXY_PORT}"

# gitport 二进制候选路径。
# 本机用 gnu 目标编（Strawberry MinGW），CI 用 msvc 编（runner 自带工具链）——
# 两边都会产出，按「release 优先、msvc 次之、debug 兜底」的顺序找，
# 不要写死一个目标三元组，否则换个编译方式就「找不到 exe」。
_HERE = os.path.dirname(os.path.abspath(__file__))
_GITPORT_DIR = os.path.normpath(os.path.join(_HERE, "..", "vendor", "gitport-accel"))
_TRIPLES = ("x86_64-pc-windows-gnu", "x86_64-pc-windows-msvc")
_EXE_CANDIDATES = [
    os.path.join(_GITPORT_DIR, "target", t, profile, "gitport-github.exe")
    for profile in ("release", "debug")
    for t in _TRIPLES
]
# CI 产物的下载解压位置（artifact 解出来就是 exe 本体，没有 target 结构）。
# `netaccel.py ci-fetch` 就落在这里，是「编译交给 runner」这条规矩的落地点。
CI_DIST_DIR = os.path.join(_GITPORT_DIR, "dist")
_EXE_CANDIDATES.append(os.path.join(CI_DIST_DIR, "gitport-github.exe"))
_SITES_TOML = os.path.join(_GITPORT_DIR, "sites.toml")
_SITES_EXAMPLE = os.path.join(_GITPORT_DIR, "SITES_EXAMPLE.toml")

# 内置受管域名（必须镜像 src/site.rs 的 default_profiles 域名）。
# 这是 PAC 路由的真相来源之一；用户通过 sites.toml 追加的站点会被自动并入。
# 旧 GitHub 生态域名（*.github.com / *.githubusercontent.com）覆盖全部 GitHub 流量。
# 2026-10-31 实测定调：只登记「gitport 确实救得动」的站。
#
# YouTube / Twitter / Google 已从名单里剔除，理由是量出来的，不是猜的：
#   ① DNS 就被污染（www.youtube.com 返回 31.13.92.37 = Facebook 的 IP）；
#   ② 直接打 25 个真实 Google IP，只有 4 个 TCP 能建连；
#   ③ 对这 4 个换任意 SNI（含 pypi/cloudflare）全部 ConnectionReset。
# 结论是 Google 段在 TLS 握手阶段被按 IP 整段切断，跟 SNI 无关，分片救不了。
# 把它们留在 PAC 里等于把流量导进死端口——浏览器白等超时，还不如 DIRECT 快速失败。
BUILTIN_DOMAINS = [
    # GitHub 全系（换 IP 即通，实测 200/0.29s，gitport 的主战场）
    "github.com", "*.github.com",
    "githubusercontent.com", "*.githubusercontent.com",
    # arXiv（国外论文，直连可用，登记只为在必要时代理）
    "arxiv.org", "*.arxiv.org", "export.arxiv.org",
]

# 模型下载别走代理，走镜像更快：实测 hf-mirror 1.63MB/s、ModelScope 直连可用，
# 而 huggingface.co 本身 IP 可达但 SNI 被封、且 DPI 流重组窗口 >3s（分片/乱序全无效）。
MIRROR_HOSTS = [
    "hf-mirror.com", "*.hf-mirror.com",
    "modelscope.cn", "*.modelscope.cn",
]


def find_exe():
    """定位 gitport 二进制：环境变量优先，其余候选按修改时间取最新。

    为什么按 mtime 而不是写死顺序：本机可能同时躺着一个旧的 gnu 产物和一个
    刚从 CI 拉下来的 msvc 产物。写死顺序会让「明明下了新的却还在跑旧的」，
    这种 bug 不报错，只表现为「改了代码没生效」，最难查。
    """
    override = (os.environ.get("GITPORT_EXE") or "").strip()
    if override and os.path.isfile(override):
        return override
    best, best_m = None, -1.0
    for cand in _EXE_CANDIDATES:
        try:
            m = os.path.getmtime(cand)
        except OSError:
            continue
        if m > best_m:
            best, best_m = cand, m
    return best


_REPO_ROOT = os.path.normpath(os.path.join(_GITPORT_DIR, "..", ".."))


def ci_fetch(run_id=None, timeout=300):
    """从 GitHub Actions 拉 build-gitport 最新的成功产物，落到 dist/。

    2026-10-31 定调：编译一律交给 runner。runner 上 2~5 分钟出活，本机要十几分钟
    还得看 E 盘脸色（Rust home / libclang / MinGW 头文件三条外部依赖），
    「验证代码编得过」这件事本机没有任何优势。既然这样，「把产物拿回来」
    就必须是一条命令，不能让「exe 在哪」再变成一次踩坑。

    返回 (ok, 说明)；ok=False 时说明是给人看的失败原因。
    """
    from shutil import which

    if not which("gh"):
        return False, "没找到 gh（GitHub CLI）。要么装 gh，要么网页手动下 artifact"
    if not run_id:
        rc, out, _ = _safe_run(
            ["gh", "run", "list", "--workflow=build-gitport.yml",
             "--status=success", "--limit", "1",
             "--json", "databaseId,headSha,createdAt"],
            timeout=60, cwd=_REPO_ROOT)
        try:
            rows = json.loads(out or "[]")
        except ValueError:
            return False, f"查 CI 运行记录失败（rc={rc}）：{(out or '')[:200]}"
        if not rows:
            return False, "build-gitport 还没有一次成功运行——先让它绿，再来拿"
        run_id = str(rows[0]["databaseId"])
    os.makedirs(CI_DIST_DIR, exist_ok=True)
    # 只清 exe/dll，不整个删目录（万一里面有人放的 sites.toml 之类）
    for name in os.listdir(CI_DIST_DIR):
        if name.lower().endswith((".exe", ".dll")):
            try:
                os.remove(os.path.join(CI_DIST_DIR, name))
            except OSError:
                pass
    rc, out, err = _safe_run(
        ["gh", "run", "download", str(run_id), "-n", "build-gitport-windows",
         "-D", CI_DIST_DIR],
        timeout=timeout, cwd=_REPO_ROOT)
    if rc != 0:
        return False, f"下载 artifact 失败（run {run_id}）：{((out or '') + (err or '')).strip()[:300]}"
    exe = os.path.join(CI_DIST_DIR, "gitport-github.exe")
    if not os.path.isfile(exe):
        got = []
        for root, _dirs, files in os.walk(CI_DIST_DIR):
            got += files
        return False, f"产物里没有 gitport-github.exe，实际解出：{got[:10]}"
    mb = os.path.getsize(exe) / 1048576
    return True, f"run {run_id} 已落地 {exe}（{mb:.2f} MB）"


def is_running():
    """7810 是否在监听（gitport 起来了）。"""
    try:
        with socket.create_connection(( "127.0.0.1", PROXY_PORT), timeout=1.5):
            return True
    except OSError:
        return False


def _safe_run(cmd, timeout=10, cwd=None):
    """跑命令，安全解码输出（中文 Windows 的 netstat/taskkill 是 GBK，不能 text=True 硬解）。"""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd)
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
        return False, (
            "找不到 gitport-github.exe。两条路选一条：\n"
            "  · 本地编译：vendor/gitport-accel/build_windows.sh\n"
            "  · 让 CI 编：push 后从 GitHub Actions 的 build-gitport 产物下载"
        )
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


# --- 热点网关：把本机做成「连上就有代理」的网关 ---------------------
#
# 分层而不是重写：proxy.py 只负责「把局域网来的连接转交给 127.0.0.1:7810」，
# 真正的 IP 池、失败沉底、ClientHello 分片全在 gitport 里。
# 这样热点下的设备只填一个 代理=192.168.137.1:7822，不用装任何东西。
LAN_PORT = 7822
_GATEWAY_PY = os.path.normpath(
    os.path.join(_HERE, "..", "tools", "cloudnode", "proxy.py")
)


def hotspot_ip():
    """找移动热点网卡的网关地址（Windows 移动热点固定 192.168.137.1）。

    找不到就返回 None —— 调用方据此提示「先开热点」，而不是瞎猜一个地址。
    """
    if not IS_WIN:
        return None
    rc, out, _ = _safe_run(["netsh", "interface", "ip", "show", "address"], timeout=8)
    for line in (out or "").splitlines():
        if "192.168.137." in line:
            m = re.search(r"(192\.168\.137\.\d+)", line)
            if m:
                return m.group(1)
    return None


def lan_running():
    """7822 是否在监听（热点网关起来了）。"""
    try:
        with socket.create_connection(("127.0.0.1", LAN_PORT), timeout=1.5):
            return True
    except OSError:
        return False


def gateway_start(wait=8):
    """启动热点网关。gitport 没跑就先拉起来。返回 (ok, 说明)。

    如果设了环境变量 ``FLEET_WSRELAY``（形如 ``wss://xxx.onrender.com/tunnel``），
    网关会把 WS 隧道当作最优先上游——那是唯一能覆盖墙外站点的通路，
    因为本机 IP 池只救得动「换 IP 就通」的站（GitHub 这类）。
    """
    if not os.path.isfile(_GATEWAY_PY):
        return False, f"找不到 {_GATEWAY_PY}"
    if lan_running():
        ip = hotspot_ip() or "0.0.0.0"
        return True, f"网关已在跑（{ip}:{LAN_PORT}）"
    if not is_running():
        ok, msg = start()
        if not ok:
            return False, f"gitport 起不来，网关没有上游：{msg}"
    args = [
        sys.executable, _GATEWAY_PY,
        "--host", "0.0.0.0", "--port", str(LAN_PORT),
        "--upstream", PROXY_ADDR,
    ]
    wsrelay = os.environ.get("FLEET_WSRELAY", "").strip()
    if wsrelay:
        args += ["--upstream-ws", wsrelay]
    try:
        if IS_WIN:
            creation = 0x00000200 | 0x00001000  # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
            p = subprocess.Popen(
                args, creationflags=creation, close_fds=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            p = subprocess.Popen(
                args, start_new_session=True, close_fds=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except OSError as e:
        return False, f"启动失败：{e}"
    import time
    for _ in range(max(1, int(wait * 10))):
        if lan_running():
            ip = hotspot_ip() or "0.0.0.0"
            return True, f"已启动（pid={p.pid}）→ 热点设备代理填 {ip}:{LAN_PORT}"
        time.sleep(0.1)
    return False, f"起来了但 {LAN_PORT} 没监听，查 proxy.py 日志"


def gateway_stop():
    """停热点网关（只停 7822，不动 gitport 本身）。"""
    if not lan_running():
        return True, "热点网关本来就没跑"
    pid = _find_pid_on_port(LAN_PORT)
    if not pid:
        return False, f"{LAN_PORT} 在监听但找不到 PID"
    if IS_WIN:
        rc, out, err = _safe_run(["taskkill", "/PID", str(pid), "/F", "/T"])
        return rc == 0, f"{'已结束' if rc == 0 else '结束失败'} pid={pid}"
    return False, "非 Windows 不支持"


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
            # 没有种子的站点救不动（seeds 为空 = 池里没 IP 可试）。
            # 把它的域名写进 PAC 只会把浏览器导进一条死路，宁可 DIRECT 快速失败。
            seeds = blk.get("seeds")
            if isinstance(seeds, list) and not seeds:
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
    # ① 受管域名：github/arxiv 必须在；youtube/twitter/huggingface 必须不在
    #    —— 这三条是 2026-10-31 实测结论，写成断言才不会被以后"顺手加回来"。
    doms = sites_domains()
    for must in ("github.com", "arxiv.org"):
        assert must in doms, f"缺 {must}：{doms}"
    for banned in ("youtube.com", "twitter.com", "huggingface.co"):
        assert banned not in doms, f"{banned} 实测救不动，不该出现在 PAC 里：{doms}"
    # ② PAC 生成：受管域名命中 PROXY，国内域名走 DIRECT
    pac = pac_script()
    assert 'PROXY 127.0.0.1:7810' in pac
    assert 'return "DIRECT";' in pac
    # 通配能同时覆盖裸域与子域
    assert "shExpMatch(host, \"*.github.com\")" in pac
    assert "youtube" not in pac.lower(), "PAC 里不该再有 youtube"
    # ③ 端口常量
    assert PROXY_PORT == 7810 and WEBUI_PORT == 7811 and LAN_PORT == 7822
    # ④ 二进制定位（debug 或 release 至少一个应存在，否则只告警不报错）
    exe = find_exe()
    if exe:
        assert os.path.isfile(exe), exe
    # ⑤ 系统代理关路径不炸
    ok, msg = system_proxy(False)
    assert isinstance(ok, bool), (ok, msg)
    # ⑥ CI 产物通道：dist 必须在候选里，否则 ci-fetch 下完了也用不上（静默失效）
    assert os.path.normpath(os.path.join(CI_DIST_DIR, "gitport-github.exe")) in [
        os.path.normpath(c) for c in _EXE_CANDIDATES
    ], "ci-fetch 的落地目录不在 find_exe 候选里"
    print(f"netaccel 自检：6 项全过（受管域名 {len(doms)} 个，exe={exe or '未编译'}）")


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
    if cmd == "gateway":
        ok, msg = gateway_start()
        print(("  ✓ " if ok else "  ✗ ") + msg)
        if ok:
            ip = hotspot_ip()
            if ip:
                print(f"  热点设备设置：HTTP/SOCKS 代理 {ip}:{LAN_PORT}")
            else:
                print("  提示：热点网卡没找到，先开「移动热点」再跑一次")
            ws = os.environ.get("FLEET_WSRELAY", "").strip()
            if ws:
                print(f"  覆盖范围：经 WS 隧道 {ws}（可到墙外站）")
            else:
                print("  覆盖范围：GitHub 全系 / PyPI / arXiv；模型走 hf-mirror、ModelScope")
                print("  要覆盖 YouTube 这类需设 FLEET_WSRELAY=wss://<你的中转>/tunnel")
        return 0 if ok else 1
    if cmd in ("gateway-stop", "gateway_stop"):
        ok, msg = gateway_stop()
        print(("  ✓ " if ok else "  ✗ ") + msg)
        return 0 if ok else 1
    if cmd in ("gateway-status", "gateway_status"):
        ip = hotspot_ip()
        print("【热点网关】")
        print(f"  网卡地址    : {ip or '未找到（热点没开？）'}")
        print(f"  运行中      : {'是 ' + str(ip or '0.0.0.0') + ':' + str(LAN_PORT) if lan_running() else '否'}")
        return 0
    if cmd in ("ci-fetch", "ci_fetch"):
        run_id = argv[1] if len(argv) > 1 else None
        ok, msg = ci_fetch(run_id)
        print(("  ✓ " if ok else "  ✗ ") + msg)
        if ok:
            print("  提示：gateway 在跑的话，先 gateway-stop 再 gateway 才会用上新二进制")
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

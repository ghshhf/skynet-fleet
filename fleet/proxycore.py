"""天网 · 代理数据面（proxycore）：sing-box 核心的配置生成 + 进程管理。

阶段 1 目标：打通「订阅 → 解析 → 生成配置 → 拉起 sing-box → 系统代理接管流量」。
这是 Karing「控制面自研 + 数据面借 sing-box 核心」架构的天网落地——
控制面是 proxymgr（解析/归一/测速/规则），本模块负责把控制面的产出翻译成
sing-box 能吃的 JSON，并管好核心进程 + 系统代理开关。

职责边界（与 netaccel / tunnel 同款克制）：
- 零第三方依赖，只用标准库 + subprocess。
- 改系统代理是**显式动作**（deploy/off 命令触发），绝不静默改。
- 字段翻译严格照抄 sing-box option/ 目录的 JSON tag，不臆断字段名。

    python proxycore.py build "<订阅内容>"    # 只生成配置（不拉起），打印 config 路径
    python proxycore.py deploy "<订阅内容>"   # 解析→生成配置→拉起核心→开系统代理
    python proxycore.py status               # 核心进程 + 系统代理状态
    python proxycore.py off                  # 停核心 + 关系统代理
    python proxycore.py --selftest

GBK 防线（2026-11-01 铁律）：输出含 ✓ ✗ → 等外字符，入口重配 UTF-8 兜底。
"""
from . import endpoints
import json
import os
import re
import socket
import subprocess
import sys
import time

# 相对导入（作为 fleet 包成员）/ 独立运行 双兼容
try:
    from . import proxymgr as PM
except ImportError:  # 独立运行 `python proxycore.py`
    import proxymgr as PM

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

IS_WIN = sys.platform.startswith("win")

# ------------------------------------------------------------------ 常量

SB_VERSION = "1.14.4"
_HERE = os.path.dirname(os.path.abspath(__file__))
SB_DIR = os.path.normpath(os.path.join(_HERE, "..", "vendor", "sing-box"))
SB_EXE = "sing-box.exe"
MIXED_PORT = endpoints.port_num("MIXED", 7891)                      # mixed 入站（HTTP+SOCKS 同端口；避开妙妙屋 7890）
MIXED_ADDR = f"127.0.0.1:{MIXED_PORT}"
CONFIG_PATH = os.path.join(SB_DIR, "config.json")
LOG_PATH = os.path.join(SB_DIR, "sing-box.log")
# urltest 健康检测 URL（Google 可达性探针，Clash/sing-box 经典值）
URLTEST_URL = "http://www.gstatic.com/generate_204"
# 系统代理开关命中的本地入站（sing-box 已做规则分流，系统侧全局指向它即可）
PROXY_SERVER = f"127.0.0.1:{MIXED_PORT}"


# ------------------------------------------------------------------ 字段翻译

def _list(v):
    """逗号分隔字符串/单值/列表 → 列表（sing-box 的 listable 字段）。"""
    if isinstance(v, (list, tuple)):
        return list(v)
    return [x for x in str(v).split(",") if x]


def _reality(extra):
    """VLESS Reality：pbk(公钥)/sid(短 ID) → tls.reality{enabled,public_key,short_id}。"""
    if not extra.get("pbk"):
        return None
    r = {"enabled": True, "public_key": extra["pbk"]}
    if extra.get("sid"):
        r["short_id"] = extra["sid"]
    return r


def _tls(extra, force=False):
    """Clash / 链接的 tls/servername/sni/skip-cert-verify/pbk/sid/fp/alpn → sing-box tls 嵌套。

    force=True 用于 trojan/hysteria2（本质就是 TLS/QUIC 隧道，必须 enabled，否则
    sing-box 握手时 tls 配置为 nil 直接空指针崩溃）。只要出现 sni/servername、
    security=reality、pbk 也视为启用 TLS。额外支持 Reality（pbk/sid）与 utls 指纹（fp）。
    """
    tls = {}
    sni = extra.get("servername") or extra.get("sni")
    if (force or extra.get("tls") in (True, "true", 1, "1") or sni
            or extra.get("security") == "reality" or extra.get("pbk")):
        tls["enabled"] = True
    if sni:
        tls["server_name"] = sni
    if extra.get("skip-cert-verify") in (True, "true", 1, "1"):
        tls["insecure"] = True
    if extra.get("alpn"):
        tls["alpn"] = _list(extra["alpn"])
    reality = _reality(extra)
    if reality:
        tls["reality"] = reality
    # utls 指纹（fp 参数，Reality 常配 chrome/safari/randomized）
    fp = extra.get("fp") or extra.get("fingerprint")
    if fp and fp.lower() not in ("none", "disable", ""):
        tls["utls"] = {"enabled": True, "fingerprint": fp}
    return tls or None


def _transport(extra):
    """Clash 的 network/ws-opts/grpc-opts 或 vless 链接的 type/serviceName → sing-box transport。

    只处理 v2ray 系（vmess/vless/trojan）的 ws/grpc/h2/httpupgrade；tcp 是默认（不写 transport）。
    链接串里网络类型放在 `type` 参数（Clash 的 `type` 是协议名，不会进 extra，无冲突）。
    """
    net = (extra.get("network") or extra.get("type") or "tcp").lower()
    t = None
    if net in ("ws", "websocket"):
        t = {"type": "ws"}
        ws = extra.get("ws") or extra.get("ws-opts")
        path = host = None
        if isinstance(ws, dict):
            path = ws.get("path")
            host = (ws.get("headers") or {}).get("Host") or ws.get("host")
        # 链接串（vmess/vless）：path/host 平铺在 extra，不在 ws-opts 里
        path = path or extra.get("path")
        host = host or extra.get("host")
        if path:
            t["path"] = path
        if host:
            t["headers"] = {"Host": host}
    elif net in ("grpc", "gun"):
        t = {"type": "grpc"}
        gr = extra.get("grpc") or extra.get("grpc-opts") or {}
        if isinstance(gr, dict):
            svc = gr.get("grpc-service-name") or gr.get("serviceName") or gr.get("service_name")
        else:
            svc = extra.get("serviceName") or extra.get("grpc-service-name") or extra.get("service_name")
        if svc:
            t["service_name"] = svc
    elif net in ("h2", "http"):
        t = {"type": "http"}
        hp = extra.get("http-opts") or {}
        if isinstance(hp, dict) and hp.get("path"):
            t["path"] = hp["path"]
        if extra.get("host"):
            t["host"] = [extra["host"]]
    elif net == "httpupgrade":
        t = {"type": "httpupgrade"}
        if extra.get("path"):
            t["path"] = extra["path"]
        if extra.get("host"):
            t["host"] = extra["host"]
    return t


def node_to_singbox(node):
    """把 proxymgr.ProxyNode（Clash 风格 extra）翻译成 sing-box outbound JSON。"""
    ex = node.extra or {}
    p = node.proto
    ob = {"tag": node.name, "type": p, "server": node.server, "server_port": int(node.port)}

    if p == "vmess":
        if ex.get("uuid"):
            ob["uuid"] = ex["uuid"]
        if "alterId" in ex:
            ob["alter_id"] = int(ex["alterId"])
        if ex.get("cipher"):
            ob["security"] = ex["cipher"]
        tls = _tls(ex)
        if tls:
            ob["tls"] = tls
        tr = _transport(ex)
        if tr:
            ob["transport"] = tr

    elif p == "vless":
        if ex.get("uuid"):
            ob["uuid"] = ex["uuid"]
        if ex.get("flow"):
            ob["flow"] = ex["flow"]
        tls = _tls(ex)
        if tls:
            ob["tls"] = tls
        tr = _transport(ex)
        if tr:
            ob["transport"] = tr

    elif p == "trojan":
        if ex.get("password"):
            ob["password"] = ex["password"]
        tls = _tls(ex, force=True)   # trojan 本质就是 TLS，强制 enabled
        if tls:
            ob["tls"] = tls
        tr = _transport(ex)
        if tr:
            ob["transport"] = tr

    elif p == "shadowsocks":
        ob["method"] = ex.get("cipher") or ex.get("method")
        if ex.get("password"):
            ob["password"] = ex["password"]
        if ex.get("plugin"):
            ob["plugin"] = ex["plugin"]
        if ex.get("plugin-opts"):
            ob["plugin_opts"] = ex["plugin-opts"]

    elif p == "hysteria2":
        if ex.get("password"):
            ob["password"] = ex["password"]
        # 带宽（upmbps/downmbps，缺省不限）
        if ex.get("upmbps"):
            ob["up_mbps"] = int(ex["upmbps"])
        if ex.get("downmbps"):
            ob["down_mbps"] = int(ex["downmbps"])
        # 混淆：obfs=salamander + obfs-password
        if ex.get("obfs") or ex.get("obfs-password"):
            ob["obfs"] = {"type": ex.get("obfs") or "salamander",
                          "password": ex.get("obfs-password") or ""}
        # 多端口（ports/mport 逗号分隔）→ server_ports
        ports = ex.get("ports") or ex.get("mport")
        if ports:
            ob["server_ports"] = _list(ports)
        # 端口跳变间隔（hop-interval，秒）
        if ex.get("hop-interval") or ex.get("hop_interval"):
            ob["hop_interval"] = ex.get("hop-interval") or ex.get("hop_interval")
        if ex.get("bbr-profile") or ex.get("bbr_profile"):
            ob["bbr_profile"] = ex.get("bbr-profile") or ex.get("bbr_profile")
        tls = _tls(ex, force=True)   # hysteria2 基于 QUIC，TLS 必需
        if tls:
            ob["tls"] = tls

    elif p in ("socks", "http"):
        if ex.get("username"):
            ob["username"] = ex["username"]
        if ex.get("password"):
            ob["password"] = ex["password"]

    return ob


def nodes_to_outbounds(nodes):
    """节点 → sing-box outbound 列表（只保留真正的代理协议，direct/block/策略组由 build 生成）。"""
    outs = []
    tags = []
    for n in nodes:
        ob = node_to_singbox(n)
        if not ob or ob.get("type") in ("direct", "block", "selector", "urltest", "dns"):
            continue
        if not ob.get("server") or not ob.get("server_port"):
            continue
        outs.append(ob)
        tags.append(ob["tag"])
    return outs, tags


def _ruleset_rules(cn):
    """proxymgr.RuleSet → sing-box inline rule_set 的 rules 数组（domain / ip 分两片）。"""
    dom_rules = []
    doms = sorted({d.lstrip("*.") for d in cn.domains})
    sufs = sorted({s.lstrip("*.") for s in cn.domain_suffix})
    kws = sorted(cn.domain_keyword)
    if doms:
        dom_rules.append({"domain": doms})
    if sufs:
        dom_rules.append({"domain_suffix": sufs})
    if kws:
        dom_rules.append({"domain_keyword": kws})
    cidrs = sorted({str(n) for n in cn.ip_cidrs})
    ip_rules = [{"ip_cidr": cidrs}] if cidrs else []
    return dom_rules, ip_rules


def build_config(nodes, listen_port=MIXED_PORT, cn=None, tun=False, fakeip=None, sniff=True):
    """生成完整 sing-box JSON 配置（阶段 1-3 能力全开）。

    阶段 2（健康检测 + fallback 自动切换）：
      - auto = urltest（周期健康检测，失效自动剔除、恢复自动回归，完整参数）
      - manual = selector（auto + 各节点，可手动钉某节点）
      - proxy = selector（manual + direct，全挂时兜底直连）
    阶段 3（DNS FakeIP / 嗅探 / TUN 整机接管 / 逻辑规则）：
      - fakeip 默认随 tun 开启（TUN 整机接管必须用 fakeip 保域名路由、防泄漏）
      - mixed 入站默认 sniff（嗅探 TLS/HTTP 域名，IP 直连也能按域名分流）
      - tun 入站用 stack:gvisor（纯用户态，无需 wintun 驱动；需管理员权限）
      - 路由用 type:logical mode:or 合并国内域名/网段直连

    参数：
      listen_port：mixed 入站端口（默认 7891，避开妙妙屋 7890）
      cn：国内直连规则集（默认自动加载 karing-ruleset 或内置精简）
      tun：是否开 TUN 整机接管（需管理员权限；system/mixed 栈才需 wintun，默认 gvisor 免驱动）
      fakeip：None=随 tun 自动决定；显式 True/False 覆盖
      sniff：mixed 入站是否开嗅探
    """
    outs, tags = nodes_to_outbounds(nodes)

    # 策略组（阶段 2）：urltest 健康检测 + fallback 链
    if tags:
        outs.append({"type": "urltest", "tag": "auto", "outbounds": tags,
                     "url": URLTEST_URL, "interval": "3m", "tolerance": 50,
                     "idle_timeout": "30m", "interrupt_exist_connections": False})
        # manual：默认走 auto，可手动钉到任一节点
        outs.append({"type": "selector", "tag": "manual",
                     "outbounds": ["auto"] + tags, "default": "auto",
                     "interrupt_exist_connections": False})
        selector_default = "manual"
    else:
        selector_default = "direct"
    # proxy：默认 manual，全挂时兜底 direct（selector 显式列出 direct 供切换）
    outs.append({"type": "selector", "tag": "proxy",
                 "outbounds": [selector_default] + (["direct"] if selector_default != "direct" else []),
                 "default": selector_default})
    outs.append({"type": "direct", "tag": "direct"})
    outs.append({"type": "block", "tag": "block"})

    # 规则集：国内直连（优先 karing-ruleset 全量，回退内置精简）
    cn = cn or PM.load_karing_ruleset() or PM.builtin_cn_ruleset()
    dom_rules, ip_rules = _ruleset_rules(cn)
    rule_sets = []
    if dom_rules:
        rule_sets.append({"tag": "cn-domain", "type": "inline", "format": "source",
                          "rules": dom_rules})
    if ip_rules:
        rule_sets.append({"tag": "cn-ip", "type": "inline", "format": "source",
                          "rules": ip_rules})

    # 入站（sniff 字段 1.13 起已从 inbound 迁到 route 的 sniff 规则动作，见下）
    inbounds = [{"type": "mixed", "tag": "mixed-in",
                 "listen": "127.0.0.1", "listen_port": int(listen_port)}]
    use_fakeip = (bool(tun) if fakeip is None else bool(fakeip))
    if tun:
        inbounds.append({"type": "tun", "tag": "tun-in",
                         "address": ["172.19.0.1/30", "fdfe:dcba:9876::1/126"],
                         "auto_route": True, "strict_route": False,
                         "stack": "gvisor"})  # 纯用户态，免 wintun 驱动

    # 路由规则：先嗅探（非终态动作，嗅出域名再交给后续规则分流），再国内直连，最后代理
    route_rules = []
    if sniff:
        route_rules.append({"action": "sniff", "timeout": "1s"})
    if dom_rules or ip_rules:
        cn_sub = []
        if dom_rules:
            cn_sub.append({"rule_set": "cn-domain"})
        if ip_rules:
            cn_sub.append({"rule_set": "cn-ip"})
        if len(cn_sub) == 1:
            route_rules.append(dict(cn_sub[0], outbound="direct"))
        else:
            route_rules.append({"type": "logical", "mode": "or",
                                "rules": cn_sub, "outbound": "direct"})
    # 兜底规则：空规则（无匹配条件）匹配所有未命中流量，走代理
    route_rules.append({"outbound": "proxy"})

    # DNS（阶段 3）：国内域名走本地 DoH，其余走远程 DoH；TUN 模式加 fakeip 保域名路由。
    # 注意：1.14 起 DNS 规则里的 query_type / ip_cidr 规则集属 legacy address filter，
    # 与 fakeip 组合会被拒，故这里只用 domain 规则 + 裸 catch-all，不碰 ip_cidr/query_type。
    if use_fakeip:
        dns_servers = [
            {"type": "https", "tag": "local", "server": "223.5.5.5"},
            {"type": "fakeip", "tag": "fakeip", "inet4_range": "198.18.0.0/15"},
        ]
        dns_rules = (
            ([{"rule_set": "cn-domain", "server": "local"}] if dom_rules else []) +
            [{"server": "fakeip"}]  # 其余 A/AAAA 走 fakeip，非 A/AAAA 自动落 final
        )
        dns_final = "local"
    else:
        dns_servers = [
            {"type": "https", "tag": "remote", "server": "1.1.1.1", "detour": "proxy"},
            {"type": "https", "tag": "local", "server": "223.5.5.5"},
        ]
        dns_rules = ([{"rule_set": "cn-domain", "server": "local"}] if dom_rules else [])
        dns_final = "remote"

    cfg = {
        "log": {"level": "info", "output": LOG_PATH, "timestamp": True},
        "dns": {
            "servers": dns_servers,
            "rules": dns_rules,
            "final": dns_final,
            "strategy": "ipv4_only",
        },
        "inbounds": inbounds,
        "outbounds": outs,
        "route": {
            "rule_set": rule_sets,
            "rules": route_rules,
            "final": "proxy",
            # sing-box 1.14 起必须显式指定出站域名解析器，否则报 deprecated
            "default_domain_resolver": "local",
            "auto_detect_interface": True,
        },
    }
    return cfg


# ------------------------------------------------------------------ 核心进程

def find_exe():
    p = os.path.join(SB_DIR, SB_EXE)
    return p if os.path.isfile(p) else None


def _extract_zip(zip_path, dest):
    """用标准库解压。Windows 原生没有 unzip，靠外部命令会让「一键 deploy」直接卡死。"""
    try:
        import zipfile
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)
        return True, None
    except Exception as e:                              # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)


def _find_file_under(root, name):
    """在目录树里找第一个同名文件（发行包通常带一层版本目录）。"""
    for dirpath, _dirs, files in os.walk(root):
        if name in files:
            return os.path.join(dirpath, name)
    return None


def download_singbox(version=SB_VERSION, retries=3):
    """下载 sing-box Windows amd64 二进制到 vendor/sing-box（curl 可续传）。"""
    os.makedirs(SB_DIR, exist_ok=True)
    zip_path = os.path.join(SB_DIR, "sing-box.zip")
    url = (f"https://github.com/SagerNet/sing-box/releases/download/"
           f"v{version}/sing-box-{version}-windows-amd64.zip")
    try:
        rc = subprocess.call(["curl", "-L", "-C", "-", "--retry", str(retries),
                              "-f", "-o", zip_path, url], timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"curl 调用失败：{e}"
    if rc != 0 or not os.path.isfile(zip_path):
        return False, f"下载失败（rc={rc}），URL：{url}"
    ok, err = _extract_zip(zip_path, SB_DIR)
    if not ok:
        return False, f"解压失败：{err}"
    exe = find_exe()
    if not exe:
        # 官方包是 sing-box-<ver>-windows-amd64/sing-box.exe，不会直接躺在 SB_DIR 下。
        # 旧实现用 unzip 解完就 find_exe()，永远找不到 —— 下载成功也部署不了。
        found = _find_file_under(SB_DIR, SB_EXE)
        if found:
            import shutil
            try:
                shutil.move(found, os.path.join(SB_DIR, SB_EXE))
            except OSError as e:
                return False, f"已解压但搬移 sing-box.exe 失败：{e}"
            exe = find_exe()
    if not exe:
        return False, "解压后未找到 sing-box.exe"
    return True, f"已就位 {exe}"


def is_running(port=MIXED_PORT):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5):
            return True
    except OSError:
        return False


def download_wintun(version="0.14.1"):
    """下载 wintun.dll 到 vendor/sing-box（TUN system/mixed 栈需要；gvisor 栈免驱动）。

    官方源 https://www.wintun.net/（WireGuard 出品），amd64 版 dll 解压到 sing-box.exe 同目录。
    gvisor 栈（默认）不需要它，这是给「TUN 用 system/mixed 栈」时备的。
    """
    os.makedirs(SB_DIR, exist_ok=True)
    url = f"https://www.wintun.net/builds/wintun-{version}.zip"
    zip_path = os.path.join(SB_DIR, "wintun.zip")
    try:
        rc = subprocess.call(["curl", "-L", "-C", "-", "--retry", "3", "-f",
                              "-o", zip_path, url], timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"curl 调用失败：{e}"
    if rc != 0 or not os.path.isfile(zip_path):
        return False, f"下载失败（rc={rc}），URL：{url}"
    ok, err = _extract_zip(zip_path, SB_DIR)
    if not ok:
        return False, f"解压失败：{err}"
    import shutil
    dll = os.path.join(SB_DIR, "wintun", "bin", "amd64", "wintun.dll")
    if not os.path.isfile(dll):
        dll = _find_file_under(os.path.join(SB_DIR, "wintun"), "wintun.dll") or dll
    if os.path.isfile(dll):
        shutil.copy(dll, os.path.join(SB_DIR, "wintun.dll"))
        return True, f"wintun.dll 已就位（{os.path.join(SB_DIR, 'wintun.dll')}）"
    return False, "解压后未找到 wintun.dll"


def _find_pid_on_port(port):
    if not IS_WIN:
        return None
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            text = out.decode(enc, "strict")
            break
        except UnicodeDecodeError:
            continue
    else:
        return None
    m = re.search(rf":{port}\s+\S+\s+LISTENING\s+(\d+)", text)
    return int(m.group(1)) if m else None


def start(config_path=CONFIG_PATH, listen_port=None, wait=8):
    """无窗拉起 sing-box 核心。返回 (ok, 说明)。"""
    port = listen_port or MIXED_PORT
    addr = f"127.0.0.1:{port}"
    exe = find_exe()
    if not exe:
        return False, "找不到 sing-box.exe（先 proxycore.download_singbox()）"
    if is_running(port):
        return True, f"核心已在跑（{addr}）"
    args = [exe, "run", "-c", config_path]
    try:
        logf = open(LOG_PATH, "ab")
        if IS_WIN:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            p = subprocess.Popen(args, cwd=SB_DIR, stdin=subprocess.DEVNULL,
                                 stdout=logf, stderr=logf, startupinfo=si,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            p = subprocess.Popen(args, cwd=SB_DIR, stdin=subprocess.DEVNULL,
                                 stdout=logf, stderr=logf, start_new_session=True)
        logf.close()
    except OSError as e:
        return False, f"启动失败：{e}"
    for _ in range(max(1, int(wait * 10))):
        if is_running(port):
            return True, f"已启动（pid={p.pid}），监听 {addr}"
        time.sleep(0.1)
    # 没起来多半是配置错误，读日志尾部给个方向
    tail = ""
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            tail = f.read()[-400:]
    except OSError:
        pass
    return False, f"{wait}s 内 {port} 没起来。日志尾部：{tail[-200:]}"


def stop(listen_port=None):
    """停 sing-box 核心（按端口找 PID 杀，兜底按 exe 名）。"""
    port = listen_port or MIXED_PORT
    if not is_running(port):
        return True, "核心本来就没跑"
    pid = _find_pid_on_port(port)
    if pid:
        if IS_WIN:
            rc, out, err = (lambda r: (r.returncode, r.stdout, r.stderr))(
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                               capture_output=True, timeout=15))
            if rc == 0:
                return True, f"已结束 pid={pid}"
            return False, f"taskkill 失败：{((out or b'') + (err or b'')).decode('gbk', 'replace')[:120]}"
    if IS_WIN:
        subprocess.run(["taskkill", "/IM", SB_EXE, "/F", "/T"],
                       capture_output=True, timeout=15)
        return True, f"按名结束 {SB_EXE}"
    return False, "非 Windows 且找不到 PID"


# ------------------------------------------------------------------ 系统代理

def _refresh_system_proxy():
    if not IS_WIN:
        return
    try:
        import ctypes
        ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)   # SETTINGS_CHANGED
        ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)   # REFRESH
    except Exception:
        pass


def system_proxy(on, port=MIXED_PORT):
    """Windows 系统代理开关（全局指向 sing-box mixed 入站）。返回 (ok, 说明)。"""
    if not IS_WIN:
        return False, "非 Windows：请手动把系统代理指向 " + PROXY_SERVER
    try:
        import winreg
    except ImportError:
        return False, "无 winreg 模块"
    key = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ,
                                  f"127.0.0.1:{port}")
                winreg.SetValueEx(k, "AutoConfigURL", 0, winreg.REG_SZ, "")
            else:
                winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 0)
                winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, "")
        _refresh_system_proxy()
        return True, ("系统代理已指向 " + PROXY_SERVER) if on else "系统代理已关闭"
    except OSError as e:
        return False, f"注册表写入失败：{e}"


def proxy_state():
    """系统代理是否开启。"""
    if not IS_WIN:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                            0, winreg.KEY_READ) as k:
            v, _ = winreg.QueryValueEx(k, "ProxyEnable")
            return bool(v)
    except OSError:
        return None


# ------------------------------------------------------------------ 一键部署

def deploy(raw, fmt=None, listen_port=MIXED_PORT, start_core=True, enable_proxy=True,
           tun=False, fakeip=None):
    """订阅 → 解析 → 生成配置 → 落盘 → 拉起核心 → 开系统代理。返回 dict。

    解析走两级兜底：常规 parse_subscription → 失败再 deep_analyze（base64 解包救回 +
    协议逆向）。tun=True 开 TUN 整机接管（需管理员权限，gvisor 栈免驱动）。
    """
    if not (raw or "").strip():
        return {"ok": False, "err": "订阅内容为空"}

    # 1) 解析订阅（常规 → 深解兜底救回）
    nodes = PM.parse_subscription(raw, fmt=fmt)
    method = "standard"
    if not nodes:
        da = PM.deep_analyze(raw)
        if da.get("nodes"):
            nodes = [PM.ProxyNode.from_dict(d) for d in da["nodes"]]
            method = da.get("method", "reversed")
        else:
            return {"ok": False, "err": "订阅解析失败，无可用节点",
                    "deep": da.get("deep"), "note": da.get("note")}

    # 2) 生成配置 + 落盘
    cfg = build_config(nodes, listen_port=listen_port, tun=tun, fakeip=fakeip)
    os.makedirs(SB_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    # 3) 核心二进制
    exe = find_exe()
    if not exe:
        return {"ok": False, "err": "sing-box.exe 不存在（先 proxycore.download_singbox()）",
                "nodes": len(nodes), "config": CONFIG_PATH}

    # 4) 拉起核心
    if start_core:
        ok, msg = start(CONFIG_PATH, listen_port=listen_port)
        if not ok:
            return {"ok": False, "err": msg, "nodes": len(nodes), "config": CONFIG_PATH}

    # 5) 开系统代理
    if enable_proxy:
        ok2, msg2 = system_proxy(True, listen_port)
        if not ok2:
            return {"ok": True, "nodes": len(nodes), "method": method,
                    "config": CONFIG_PATH, "listen": f"127.0.0.1:{listen_port}",
                    "warn": msg2}

    return {"ok": True, "nodes": len(nodes), "method": method, "config": CONFIG_PATH,
            "listen": f"127.0.0.1:{listen_port}", "exe": exe}


def status():
    return {
        "exe": find_exe(),
        "running": is_running(),
        "listen": MIXED_ADDR if is_running() else None,
        "system_proxy": proxy_state(),
        "config": CONFIG_PATH if os.path.isfile(CONFIG_PATH) else None,
    }


# ------------------------------------------------------------------ 自测

def _selftest():
    print("== proxycore 自测 ==")
    fails = []

    def chk(cond, msg):
        print(("  ✓ " if cond else "  ✗ ") + msg)
        if not cond:
            fails.append(msg)

    # 1) 字段翻译：vmess alterId→alter_id、cipher→security、ws 传输
    n1 = PM.ProxyNode("HK-vmess", "vmess", "hk.example.com", 443, {
        "uuid": "11111111-2222-3333-4444-555555555555", "alterId": 0,
        "cipher": "auto", "network": "ws", "tls": True, "servername": "hk.example.com",
        "ws-opts": {"path": "/ws", "headers": {"Host": "cdn.example.com"}},
    })
    ob = node_to_singbox(n1)
    chk(ob["alter_id"] == 0 and "alterId" not in ob, "vmess alterId→alter_id")
    chk(ob["security"] == "auto", "vmess cipher→security")
    chk(ob["tls"] == {"enabled": True, "server_name": "hk.example.com"}, "vmess tls 嵌套")
    chk(ob["transport"]["type"] == "ws" and ob["transport"]["path"] == "/ws", "ws 传输")
    chk(ob["transport"]["headers"] == {"Host": "cdn.example.com"}, "ws Host 头")

    # 2) ss：cipher→method
    n2 = PM.ProxyNode("JP-ss", "shadowsocks", "jp.example.com", 8388,
                      {"cipher": "aes-256-gcm", "password": "secret123"})
    ob2 = node_to_singbox(n2)
    chk(ob2["method"] == "aes-256-gcm" and ob2["password"] == "secret123", "ss cipher→method")

    # 3) trojan：sni→tls.server_name
    n3 = PM.ProxyNode("HK-trojan", "trojan", "hk2.example.com", 443,
                      {"password": "pass", "sni": "hk2.example.com"})
    ob3 = node_to_singbox(n3)
    chk(ob3["tls"]["server_name"] == "hk2.example.com", "trojan sni→tls.server_name")

    # 4) build_config 结构
    cfg = build_config([n1, n2, n3], listen_port=MIXED_PORT)
    chk("inbounds" in cfg and cfg["inbounds"][0]["type"] == "mixed", "mixed 入站")
    chk(any(o["type"] == "urltest" and o["tag"] == "auto" for o in cfg["outbounds"]),
        "urltest 策略组")
    chk(any(o["type"] == "selector" and o["tag"] == "proxy" for o in cfg["outbounds"]),
        "selector 策略组")
    chk(any(r.get("outbound") == "proxy" for r in cfg["route"]["rules"]), "兜底走代理")
    chk(cfg["route"]["rule_set"] and cfg["route"]["rule_set"][0]["type"] == "inline",
        "inline 规则集")
    # 规则集里 domain_suffix 不能带 *. 前缀（sing-box 语义是后缀匹配）
    rs0 = cfg["route"]["rule_set"][0]["rules"]
    all_suf = [s for r in rs0 for s in r.get("domain_suffix", [])]
    chk(all(not s.startswith("*.") for s in all_suf), "domain_suffix 已去 *. 前缀")

    # 5) 节点去重/过滤：direct/block 不进节点 outbound
    outs, tags = nodes_to_outbounds([n1, n2, n3])
    chk(len(tags) == 3 and "direct" not in tags, "只保留代理协议节点")

    # 6) 空节点兜底不崩
    cfg_empty = build_config([])
    chk(any(o["type"] == "selector" and o["default"] == "direct"
            for o in cfg_empty["outbounds"]), "空节点时 selector 兜底 direct")

    # 7) 阶段4：vless Reality 翻译
    n_r = PM.ProxyNode("REALITY", "vless", "real.example.com", 443, {
        "uuid": "abcd-ef01", "flow": "xtls-rprx-vision", "security": "reality",
        "pbk": "PUBKEY123", "sid": "abcd", "fp": "chrome", "servername": "www.microsoft.com"})
    obr = node_to_singbox(n_r)
    chk(obr["flow"] == "xtls-rprx-vision", "vless flow 提取")
    chk(obr["tls"]["reality"] == {"enabled": True, "public_key": "PUBKEY123",
                                  "short_id": "abcd"}, "vless reality 字段")
    chk(obr["tls"]["utls"] == {"enabled": True, "fingerprint": "chrome"}, "vless utls 指纹")

    # 8) 阶段4：hysteria2 翻译
    n_h = PM.ProxyNode("HY2", "hysteria2", "hy2.example.com", 443, {
        "password": "pass", "obfs": "salamander", "obfs-password": "obfs", "upmbps": "100"})
    obh = node_to_singbox(n_h)
    chk(obh["password"] == "pass" and obh["up_mbps"] == 100, "hysteria2 基本字段")
    chk(obh["obfs"] == {"type": "salamander", "password": "obfs"}, "hysteria2 obfs")
    chk(obh["tls"]["enabled"] is True, "hysteria2 TLS 强制 enabled")

    # 9) 阶段2：urltest 健康检测参数 + fallback 链
    cfg2 = build_config([n1, n2, n3], listen_port=MIXED_PORT)
    ur = next(o for o in cfg2["outbounds"] if o["type"] == "urltest")
    chk(ur.get("idle_timeout") == "30m" and "interval" in ur and "tolerance" in ur,
        "urltest 健康检测参数完整")
    manual = next(o for o in cfg2["outbounds"] if o["tag"] == "manual")
    chk(manual["type"] == "selector" and manual["outbounds"][0] == "auto",
        "manual 选择器兜 auto")
    proxy_sel = next(o for o in cfg2["outbounds"] if o["tag"] == "proxy")
    chk("direct" in proxy_sel["outbounds"], "proxy 选择器含 direct 兜底")

    # 10) 阶段3：TUN + fakeip + sniff + 逻辑规则
    cfg_t = build_config([n1, n2, n3], listen_port=MIXED_PORT, tun=True)
    tun_in = next(i for i in cfg_t["inbounds"] if i["type"] == "tun")
    chk("address" in tun_in and tun_in["stack"] == "gvisor", "tun 新 address 字段 + gvisor 栈")
    chk(any(s["type"] == "fakeip" for s in cfg_t["dns"]["servers"]), "TUN 模式含 fakeip DNS")
    chk(cfg_t["dns"]["final"] == "local", "fakeip 模式 final 用 local")
    chk(any(r.get("action") == "sniff" for r in cfg_t["route"]["rules"]), "sniff 规则动作")
    chk(any(r.get("type") == "logical" for r in cfg_t["route"]["rules"]), "逻辑规则")
    chk(not any(s["type"] == "fakeip" for s in cfg["dns"]["servers"]), "常规模式无 fakeip")

    print("\n自测结果：", "全部通过 ✓" if not fails else f"{len(fails)} 项失败 ✗")
    return 1 if fails else 0


# ------------------------------------------------------------------ CLI

def _main(argv):
    argv = list(argv)
    if "--selftest" in argv or argv[:1] == ["selftest"]:
        return _selftest()
    if not argv:
        print(__doc__.strip())
        return 0
    cmd = argv[0]
    if cmd == "build":
        raw = argv[1] if len(argv) > 1 else ""
        nodes = PM.parse_subscription(raw)
        if not nodes:
            print("  ✗ 订阅解析失败")
            return 1
        cfg = build_config(nodes)
        os.makedirs(SB_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"  ✓ 已生成 {CONFIG_PATH}（{len(nodes)} 个节点）")
        return 0
    if cmd == "deploy":
        raw = argv[1] if len(argv) > 1 else ""
        r = deploy(raw)
        print(("  ✓ " if r.get("ok") else "  ✗ ") + json.dumps(
            {k: v for k, v in r.items() if k != "deep"}, ensure_ascii=False))
        return 0 if r.get("ok") else 1
    if cmd == "status":
        s = status()
        print("【代理核心】")
        print(f"  二进制    : {s['exe'] or '未下载'}")
        print(f"  运行中    : {'是 ' + s['listen'] if s['running'] else '否'}")
        print(f"  系统代理  : {s['system_proxy']}")
        print(f"  配置      : {s['config'] or '未生成'}")
        return 0
    if cmd == "off":
        ok1, m1 = stop()
        ok2, m2 = system_proxy(False)
        print(("  ✓ " if ok1 else "  ✗ ") + m1)
        print(("  ✓ " if ok2 else "  ✗ ") + m2)
        return 0
    if cmd == "download":
        ok, msg = download_singbox()
        print(("  ✓ " if ok else "  ✗ ") + msg)
        return 0 if ok else 1
    print(f"  未知命令：{cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))

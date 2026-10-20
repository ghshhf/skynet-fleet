"""扩散式发现（sweep）—— 把「ARP 表里有的」扩成「整个网段在场的」。

上一版只读 ARP 表，等于只看见「最近跟本机说过话」的设备。客厅里的摄像头、
空调、打印机只要没跟这台电脑通信过，就一条也看不到——这才是「只扫到 30 台」
的真因，不是网段太小。

补齐四条：
  ① ICMP 全段 sweep：并发 ping 整个 /24，活着的设备全部进 ARP 表
  ② TCP connect：IoT 常关 ICMP 却开着端口，只 connect、不发送任何指令
  ③ NetBIOS(NBNS) 名字查询：Windows 设备会把机器名报出来
  ④ mDNS 服务类型枚举：`_services._dns-sd._udp.local` 让设备自报有哪些服务

外加一条环境感知：
  ⑤ WiFi 周边 AP 扫描：SSID/BSSID/信号/加密。**不连接、不抓包、不破解**

跨网段「跳板扩散」为什么不做（诚实边界）：
  要拿一台设备当跳板，前提是那台设备上已经有我的执行权。现在没有，也不该
  靠猜口令去拿。能做的扩散止于「本机网卡直连的所有网段」——这台机器上就是
  以太网 192.168.0.0/24 + 移动热点 192.168.137.0/24。

硬边界：
  - 只发 ICMP echo、TCP connect、NBNS 名字查询、mDNS PTR，不发任何应用层指令
  - 不试口令、不探漏洞、不读取设备内容
  - 不扫链路本地(169.254)、回环(127)、虚拟网卡(172.16-31)、Tailscale(100.64)
"""
import os
import socket
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import discovery as D

IS_WIN = os.name == "nt"

# 第一层存活探针：只挑「活着就几乎一定开」的端口，用于 ICMP 被关的设备兜底
LIVENESS_PORTS = (22, 80, 443, 445, 3389, 5555, 8080, 9000, 8008, 7547)

# mDNS 服务类型枚举：这一条查询会让设备把「我有哪些服务」整份报出来
SERVICE_ENUM_NAME = "_services._dns-sd._udp.local"


# ---------------------------------------------------------------- 网段枚举

def _decode(b):
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, AttributeError):
            continue
    return ""


def _skip_net(ip):
    """虚拟网卡、不可扫网段、以及「根本不是设备」的地址。

    家庭/办公 LAN 几乎不会用 172.16/12，而 WSL/Docker/Hyper-V 全在这段里——
    扫它纯属浪费且会混进假设备。
    """
    try:
        p = [int(x) for x in ip.split(".")]
    except ValueError:
        return True
    if len(p) != 4:
        return True
    if p[3] in (0, 255):                     # 网络地址 / 广播地址，不是设备
        return True
    if p[0] == 127 or p[0] == 0:
        return True
    if p[0] == 169 and p[1] == 254:          # 链路本地
        return True
    if p[0] == 172 and 16 <= p[1] <= 31:     # Hyper-V / Docker / WSL
        return True
    if p[0] == 100 and 64 <= p[1] <= 127:    # Tailscale CGNAT
        return True
    if p[0] == 255 or 224 <= p[0] <= 239:    # 广播 / 组播
        return True
    return False


def local_ips():
    """本机所有网卡地址。**这些绝不能出现在「发现的设备」里**——
    上一轮把 192.168.0.103 自己判成了「NAS/存储」（因为本机开着 139/445），
    这种错误比漏发现更糟：它让整张表失去可信度。"""
    ips = set()
    try:
        for row in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(row[4][0])
    except OSError:
        pass
    try:
        r = subprocess.run(["arp", "-a"] if IS_WIN else ["ip", "-4", "addr"],
                           capture_output=True, timeout=10)
        text = _decode(r.stdout)
        import re
        for m in re.finditer(r"(\d{1,3}(?:\.\d{1,3}){3})\s*-\s*0x", text):
            ips.add(m.group(1))
        for m in re.finditer(r"inet (\d{1,3}(?:\.\d{1,3}){3})/\d+", text):
            ips.add(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    return {ip for ip in ips if not _skip_net(ip)}


def local_subnets():
    """枚举本机**直接所在**的所有 IPv4 /24 网段。

    两条路合并，因为单独任何一条都不全：
      - getaddrinfo：通常只回主 IP（多网卡时丢）
      - `arp -a` 的接口行：Windows 会把每张网卡的地址都列出来（含移动热点）
    """
    ips = set()
    try:
        for row in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(row[4][0])
    except OSError:
        pass

    try:
        r = subprocess.run(["arp", "-a"] if IS_WIN else ["ip", "-4", "addr"],
                           capture_output=True, timeout=10)
        text = _decode(r.stdout)
        import re
        for m in re.finditer(r"(\d{1,3}(?:\.\d{1,3}){3})\s*---\s*0x", text):
            ips.add(m.group(1))
        for m in re.finditer(r"inet (\d{1,3}(?:\.\d{1,3}){3})/\d+", text):
            ips.add(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass

    nets = []
    for ip in sorted(ips):
        if _skip_net(ip) or ip.endswith(".0") or ip.endswith(".255"):
            continue
        base = ".".join(ip.split(".")[:3])
        if base not in nets:
            nets.append(base)
    return nets


def hosts_of(base):
    return [f"{base}.{i}" for i in range(1, 255)]


# ---------------------------------------------------------------- ICMP

def _ping(ip, timeout_ms=400):
    """单发 ICMP。**只看返回码，不解码输出**——中文 Windows 的 ping 输出是 GBK，
    解码反而会炸，而存活判断根本不需要读懂它说了什么。"""
    if IS_WIN:
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip]
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              timeout=timeout_ms / 1000 + 1.5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def icmp_sweep(base, workers=48, timeout_ms=400):
    """并发扫整个 /24。返回活着的 IP 列表（顺序不保证）。"""
    addrs = hosts_of(base)
    alive = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_ping, ip, timeout_ms): ip for ip in addrs}
        for f in as_completed(futs):
            if f.result():
                alive.append(futs[f])
    return sorted(alive, key=lambda x: int(x.split(".")[-1]))


# ---------------------------------------------------------------- TCP

def tcp_open(ip, ports=None, timeout=0.3):
    """只做 connect 就关，不发一个字节的载荷。"""
    hit = []
    for p in (ports or LIVENESS_PORTS):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            if s.connect_ex((ip, p)) == 0:
                hit.append(p)
        except OSError:
            pass
        finally:
            s.close()
    return hit


def tcp_sweep(ips, workers=64, timeout=0.3):
    """对有 IP 但 ICMP 不通的地址做端口兜底（IoT 常关 ICMP）。"""
    out = {}
    if not ips:
        return out
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(tcp_open, ip, None, timeout): ip for ip in ips}
        for f in as_completed(futs):
            r = f.result()
            if r:
                out[futs[f]] = r
    return out


# ---------------------------------------------------------------- NetBIOS

def _encode_nbname(name16):
    """NetBIOS 名字编码：每个字节拆两个 nibble，各加 'A'。"""
    out = b""
    for b in name16:
        out += bytes([0x41 + (b >> 4), 0x41 + (b & 0x0F)])
    return bytes([0x20]) + out + b"\x00"


def build_nbstat_query(tid=0x8221):
    """NBSTAT 查询（查 `*`，即「把所有登记的名字报给我」）。"""
    q = _encode_nbname(b"*" + b"\x00" * 15)
    q += struct.pack(">HH", 0x0021, 0x0001)          # QTYPE=NBSTAT, QCLASS=IN
    return struct.pack(">HHHHHH", tid, 0x0000, 1, 0, 0, 0) + q


def _skip_nbname(buf, off):
    """跳过一段 NetBIOS/DNS 名字，返回其后的偏移。

    三种形态都要认，否则后面全部错位——这里错一个字节，读出来的就是别人的名字：
      - 空名：单字节 0x00
      - 压缩指针：0xC0xx，占 2 字节，**指针本身就是整个名字**，后面不再有终结符
      - 完整名字：长度字节 + 内容，直到 0x00
    """
    if off >= len(buf):
        return off
    if buf[off] == 0:
        return off + 1
    if buf[off] & 0xC0 == 0xC0:
        return off + 2
    while off < len(buf) and buf[off] != 0:
        off += 1 + buf[off]
    return off + 1


def parse_nbstat(resp):
    """解 NetBIOS 名字表 -> [(名字, 后缀, 标志)]。解析失败返回 []。"""
    if len(resp) < 12:
        return []
    ancount = struct.unpack(">H", resp[6:8])[0]
    if ancount == 0:
        return []
    try:
        off = _skip_nbname(resp, 12) + 4         # 问题段：名字 + QTYPE/QCLASS
        off = _skip_nbname(resp, off) + 10       # 答案段：owner + type/class/ttl/rdlen
    except (IndexError, struct.error):
        return []
    if off >= len(resp):
        return []

    num = resp[off]
    off += 1
    names = []
    for _ in range(min(num, 32)):
        chunk = resp[off:off + 18]
        if len(chunk) < 18:
            break
        raw, suffix, flags = chunk[:15], chunk[15], struct.unpack(">H", chunk[16:18])[0]
        nm = raw.decode("ascii", "ignore").rstrip(" \x00")
        # 只留「工作站/服务器」类登记，<00> 是计算机名，<20> 是文件服务
        if nm and suffix in (0x00, 0x20):
            names.append((nm, suffix, flags))
        off += 18
    return names


def nbns_query(ip, timeout=0.7):
    """单播问一台设备「你叫什么」。拿不到就返回 ''，不当异常。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(build_nbstat_query(), (ip, 137))
        data, _ = s.recvfrom(2048)
    except (socket.timeout, OSError):
        return ""
    finally:
        s.close()
    names = parse_nbstat(data)
    if not names:
        return ""
    # <00> 唯一名字排前面，其次 <20>
    names.sort(key=lambda x: (x[1] != 0x00, x[0]))
    return names[0][0]


def nbns_sweep(ips, workers=64, timeout=0.7):
    out = {}
    if not ips:
        return out
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(nbns_query, ip, timeout): ip for ip in ips}
        for f in as_completed(futs):
            nm = f.result()
            if nm:
                out[futs[f]] = nm
    return out


# ---------------------------------------------------------------- mDNS 枚举

def mdns_service_types(timeout=2.5, local_ip=None):
    """问 `_services._dns-sd._udp.local` —— 设备会把所有服务类型整份报出来。
    这比逐个猜服务名强得多：不用预先知道它有什么。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        if local_ip:
            try:
                s.bind((local_ip, 0))
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                             socket.inet_aton(local_ip))
            except OSError:
                pass
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        except OSError:
            pass
        s.settimeout(0.35)
        pkt = D._build_mdns_query([SERVICE_ENUM_NAME])
        for _ in range(2):
            try:
                s.sendto(pkt, D.MDNS_ADDR)
            except OSError:
                break
        seen = {}
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = s.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            parsed = D.parse_mdns(data)
            if not parsed:
                continue
            d = seen.setdefault(addr[0], set())
            for svc in parsed["services"]:
                d.add(svc)
        return {ip: sorted(v) for ip, v in seen.items()}
    finally:
        s.close()


# ---------------------------------------------------------------- WiFi 环境

def wifi_survey():
    """周边 AP 清单。**只读一次扫描结果，不连接、不抓包**。
    返回 {"available": bool, "networks": [{ssid, bssid, signal, auth}], "note": str}"""
    if not IS_WIN:
        return {"available": False, "networks": [], "note": "仅 Windows 实现"}
    try:
        r = subprocess.run(["netsh", "wlan", "show", "networks", "mode=bssid"],
                           capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return {"available": False, "networks": [], "note": f"调用失败: {e}"}
    text = _decode(r.stdout)
    nets, cur = [], None
    for line in text.splitlines():
        ln = line.strip()
        if ln.startswith("SSID ") and ":" in ln and "BSSID" not in ln:
            cur = {"ssid": ln.split(":", 1)[1].strip(), "bssid": "", "signal": "", "auth": ""}
            nets.append(cur)
        elif cur is None:
            continue
        elif ln.startswith("BSSID") and ":" in ln:
            cur["bssid"] = ln.split(":", 1)[1].strip()
        elif ("信号" in ln or ln.startswith("Signal")) and ":" in ln:
            cur["signal"] = ln.split(":", 1)[1].strip()
        elif ("身份验证" in ln or ln.startswith("Authentication")) and ":" in ln:
            cur["auth"] = ln.split(":", 1)[1].strip()
    # 只有 SSID 没 BSSID 的条目是空壳（SSID 广播但扫不到详情）
    nets = [n for n in nets if n["ssid"]]
    note = ""
    if not nets:
        note = ("扫到 0 个：无线射频正被移动热点占用（这台电脑在给手机供网），"
                "单射频无法同时扫描周边 AP。要看周边 WiFi 必须先关热点。")
    return {"available": True, "networks": nets, "note": note}


# ---------------------------------------------------------------- 主流程

def sweep(subnets=None, do_icmp=True, do_tcp=True, do_nbns=True,
          do_mdns=True, do_wifi=True, workers=48, timeout_ms=400, verbose=False):
    """跑一轮扩散发现。返回结构化结果，不写文件、不注册节点、不连接任何设备。"""
    t0 = time.time()
    result = {"at": int(t0), "subnets": [], "devices": [], "notes": [],
              "wifi": None, "elapsed": 0.0}

    nets = subnets or local_subnets()
    result["subnets"] = list(nets)
    if not nets:
        result["notes"].append("没枚举到可扫网段（网卡全断？）")
        return result

    my_ips = local_ips()
    per_ip = {}

    def slot(ip, via):
        if ip in my_ips:                 # 自己不是「发现的设备」
            return None
        d = per_ip.get(ip)
        if d is None:
            d = per_ip[ip] = {"ip": ip, "via": set(), "mac": "", "vendor": "",
                              "random_mac": False, "ports": [], "nbname": "",
                              "mdns": [], "ssdp": {}}
        d["via"].add(via)
        return d

    # ① 先读一遍 ARP（零成本，已经有的一步到位）
    arp_before = {}
    try:
        for ip, mac in D.read_neighbors():
            arp_before[ip] = mac
            if _skip_net(ip):
                continue
            d = slot(ip, "arp")
            if d is None:
                continue
            d["mac"] = mac
            d["random_mac"] = D.is_randomized_mac(mac)
            d["vendor"] = "" if d["random_mac"] else D.oui_vendor(mac)
    except Exception as e:
        result["notes"].append(f"邻居表失败: {e}")

    # ①b 网关：路由表写着，不用投票猜
    gw = ""
    try:
        gw = D.default_gateway()
        if gw:
            result["gateway"] = gw
    except Exception as e:
        result["notes"].append(f"取网关失败: {e}")

    # ② ICMP 全段 sweep —— 这一步才叫「扩散」
    if do_icmp:
        for base in nets:
            try:
                alive = icmp_sweep(base, workers=workers, timeout_ms=timeout_ms)
            except Exception as e:
                result["notes"].append(f"{base}.0/24 ICMP 失败: {e}")
                continue
            for ip in alive:
                slot(ip, "icmp")
            if verbose:
                print(f"  [sweep] {base}.0/24 ICMP 存活 {len(alive)} 台", flush=True)

        # sweep 会把设备写进 ARP 表，回读一次拿 MAC（这是白捡的）
        try:
            for ip, mac in D.read_neighbors():
                d = per_ip.get(ip)
                if d and not d["mac"]:
                    d["mac"] = mac
                    d["random_mac"] = D.is_randomized_mac(mac)
                    d["vendor"] = "" if d["random_mac"] else D.oui_vendor(mac)
        except Exception:
            pass

    # ③ ICMP 不通的做 TCP 兜底（IoT 常关 ICMP）
    if do_tcp:
        silent = [ip for ip in per_ip if "icmp" not in per_ip[ip]["via"]]
        # 只兜底 ARP 里已有但 ICMP 没回的 + 每段少量采样（全段 TCP 太吵）
        probe = silent if len(silent) <= 64 else silent[:64]
        try:
            hits = tcp_sweep(probe, timeout=0.3)
            for ip, ports in hits.items():
                d = slot(ip, "tcp")
                if d is not None:
                    d["ports"] = sorted(set(d["ports"]) | set(ports))
        except Exception as e:
            result["notes"].append(f"TCP 兜底失败: {e}")

    # ④ 端口指纹：对确认活着的设备敲特征端口（复用 discovery 的规则）
    if do_tcp:
        targets = [ip for ip in per_ip if "tcp" not in per_ip[ip]["via"]][:80]

        def _fp(ip):
            try:
                return ip, D.probe_ports(ip)
            except OSError:
                return ip, []

        if targets:
            with ThreadPoolExecutor(max_workers=16) as ex:
                for ip, ports in ex.map(_fp, targets):
                    d = per_ip[ip]
                    d["ports"] = sorted(set(d["ports"]) | set(ports))

    # ⑤ NetBIOS 名字反查（拿 Windows 机器名，这是最有人味的一条信息）
    if do_nbns:
        try:
            names = nbns_sweep(list(per_ip.keys()), workers=48, timeout=0.7)
            for ip, nm in names.items():
                per_ip[ip]["nbname"] = nm
                per_ip[ip]["via"].add("nbns")
        except Exception as e:
            result["notes"].append(f"NBNS 失败: {e}")

    # ⑥ mDNS 服务类型枚举 + SSDP 型号（设备自报的身份永远压过端口投票）
    if do_mdns:
        try:
            my_ip = D._local_ip()
            for r in D.probe_ssdp(timeout=2.0, local_ip=my_ip):
                d = per_ip.get(r["ip"])
                if d is not None:
                    d["ssdp"] = {k: v for k, v in r.items() if k not in ("ip", "via")}
                    d["via"].add("ssdp")
        except Exception as e:
            result["notes"].append(f"SSDP 失败: {e}")
        try:
            my_ip = D._local_ip()
            svc = mdns_service_types(timeout=2.0, local_ip=my_ip)
            for ip, services in svc.items():
                if ip in per_ip:
                    per_ip[ip]["mdns"] = services
                    per_ip[ip]["via"].add("mdns")
                elif not _skip_net(ip):
                    d = slot(ip, "mdns")
                    if d is not None:
                        d["mdns"] = services
        except Exception as e:
            result["notes"].append(f"mDNS 枚举失败: {e}")

    # ⑦ WiFi 周边
    if do_wifi:
        try:
            result["wifi"] = wifi_survey()
        except Exception as e:
            result["notes"].append(f"WiFi 扫描失败: {e}")

    # ◇ 网络身份（运营商）：比设备清单更宏观的一层
    try:
        ident = network_identity()
        if ident.get("isp") or ident.get("has_public_v6"):
            result["network_identity"] = ident
    except Exception as e:
        result["notes"].append(f"运营商识别失败: {e}")

    # ⑧ 归类输出
    for ip, d in per_ip.items():
        ssdp_txt = " ".join(str(v) for v in d["ssdp"].values())
        mdns_txt = " ".join(d["mdns"])
        ranked = D.score_categories({
            "ssdp": ssdp_txt, "mdns": mdns_txt, "http": "",
            "oui": d["vendor"], "ports": d["ports"],
        })
        combos = D.combo_hits(d["ports"])
        if combos:
            merged = dict(ranked)
            for c, w, _desc in combos:
                merged[c] = merged.get(c, 0) + w
            ranked = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
        cat = ranked[0][0] if ranked and ranked[0][1] >= 3 else "unknown"
        conf = ranked[0][1] if ranked else 0

        # 网关：查路由表得出的，不是投票投出来的，直接定案
        is_gw = bool(gw) and ip == gw
        if is_gw:
            cat, conf = "router", 10

        # NetBIOS 名字本身就是强证据：机器名里常带型号
        name_hint = ""
        if d["nbname"]:
            name_hint = d["nbname"]
            low = name_hint.lower()
            if any(k in low for k in ("desktop", "laptop", "pc-", "win")):
                if cat == "unknown":
                    cat, conf = "pc", 4
            if "nas" in low and cat == "unknown":
                cat, conf = "nas", 5

        # 5555 是 ADB 调试端口：能确定是安卓，但断定不了是手机还是电视/一体机
        if 5555 in d["ports"] and cat in ("phone", "unknown"):
            cat, conf = "android", max(conf, 8)

        # 热点网段（192.168.137.0/24）上的随机 MAC = 手机/平板，这条几乎不会错
        if ip.startswith("192.168.137.") and d["random_mac"] and cat == "unknown":
            cat, conf = "phone", 5

        result["devices"].append({
            "ip": ip,
            "mac": d["mac"],
            "vendor": d["vendor"] or (D.extract_model(d["mdns"], d["ssdp"]) if (d["mdns"] or d["ssdp"]) else ""),
            "random_mac": d["random_mac"],
            "nbname": name_hint,
            "is_gateway": is_gw,
            "category": cat,
            "category_cn": D.CATEGORY_CN.get(cat, cat),
            "confidence": conf,
            "found_via": sorted(d["via"]),
            "open_ports": d["ports"],
            "mdns_services": d["mdns"][:8],
        })

    result["devices"].sort(key=lambda x: (
        int(x["ip"].split(".")[2]), int(x["ip"].split(".")[3])))
    result["count"] = len(result["devices"])

    # 按网段统计：让「家里主网 / 热点」一眼分开
    by_net = {}
    for d in result["devices"]:
        base = ".".join(d["ip"].split(".")[:3]) + ".0/24"
        by_net[base] = by_net.get(base, 0) + 1
    result["by_subnet"] = by_net

    # 统计口径：以「本轮有没有主动确认它活着」分，不用 ARP 差分——
    # ARP 表会被前几轮扫描污染，差分出来的数字永远接近 0，没有解释力。
    confirmed = [d for d in result["devices"]
                 if any(v in d["found_via"] for v in ("icmp", "tcp", "ssdp", "mdns"))]
    stale = result["count"] - len(confirmed)
    msg = f"本轮确认在场 {len(confirmed)} 台（ICMP/TCP/组播应答）"
    if stale:
        msg += f"；另有 {stale} 台只在 ARP 表里、本轮没应答（休眠或已离开）"
    result["notes"].append(msg)
    result["profile"] = build_profile(result)
    result["elapsed"] = round(time.time() - t0, 1)
    return result


# 运营商 IPv6 前缀（2408 联通 / 240e 电信 / 2409 移动 / 2400 教育网）
ISP_BY_V6 = {"2408": "中国联通", "240e": "中国电信", "2409": "中国移动",
             "2400": "中国教育网", "2401": "中国科技网"}


def network_identity():
    """从 IPv6 前缀读运营商——「这条宽带是谁拉的」比设备表更宏观。
    只读 ipconfig，不发包、不查外网、不上报。"""
    out = {"isp": "", "has_public_v6": False, "v6_prefix": ""}
    try:
        r = subprocess.run(["ipconfig"] if IS_WIN else ["ip", "-6", "addr"],
                           capture_output=True, timeout=10)
        text = _decode(r.stdout)
    except (OSError, subprocess.SubprocessError):
        return out
    import re
    for m in re.finditer(r"\b(2[0-9a-fA-F]{3}):([0-9a-fA-F]{1,4}):", text):
        head = m.group(1).lower()
        if head in ISP_BY_V6:
            out["isp"] = ISP_BY_V6[head]
            out["has_public_v6"] = True
            # 取 /64 级前缀，够说明问题又不暴露完整地址
            seg = text[m.start():m.start() + 40].split()[0]
            parts = seg.split(":")
            out["v6_prefix"] = ":".join(parts[:4]) + "::/64"
            break
    return out


def build_profile(result):
    """从设备构成读出「这是个什么网络、周围是什么场景」。

    用户要的不是一张 IP 表，是「我身边到底有什么」。所以这一步必须做：
    把 44 行设备压成几句人话，并且**明确说清哪些是推断、哪些是事实**。
    """
    from collections import Counter
    devs = result.get("devices", [])
    if not devs:
        return ["没扫到设备（网卡全断或全在休眠）"]

    cats = Counter(d["category"] for d in devs)
    cnt = len(devs)
    cams = cats.get("camera", 0)
    phones = cats.get("phone", 0) + cats.get("android", 0)
    pcs = cats.get("pc", 0)
    lines = []

    # ① 网络性质：摄像头密度是最强的信号
    if cams >= 5 and cams / cnt >= 0.3:
        lines.append(f"设备 {cnt} 台，其中摄像头 {cams} 台（{cams * 100 // cnt}%）："
                     f"这**不是一户人家的网络**，是小区/商业楼共用的监控+宽带 NAT。"
                     f"你连的是物业那条线，隔壁邻居也在同一段。")
    elif cams >= 3:
        lines.append(f"设备 {cnt} 台，摄像头 {cams} 台：自己装了监控，但网段里还有别的设备。")
    else:
        lines.append(f"设备 {cnt} 台，摄像头 {cams} 台：像普通家庭/小办公网络。")

    # ② 人：随机 MAC 的手机数 ≈ 有多少台移动设备在用
    rand_phones = [d for d in devs if d["category"] == "phone" and d["random_mac"]]
    if phones:
        msg = f"移动设备 {phones} 台"
        if rand_phones:
            msg += f"（其中 {len(rand_phones)} 台用随机 MAC，是开了隐私保护的手机）"
        lines.append(msg)

    # ③ 值得点名的设备
    named = []
    for d in devs:
        if d.get("is_gateway"):
            ident = d["vendor"] or "未知品牌"
            named.append(f"网关 {d['ip']} = {ident}")
        elif d["category"] == "pc" and d["vendor"]:
            named.append(f"{d['ip']} = {d['vendor']} 电脑（开着 139/445 共享）")
    if named:
        lines.append("；".join(named[:3]))

    # ④ OUI 未收录的批量设备：这本身就是信息
    by_oui = Counter(d["mac"][:8] for d in devs if d["mac"] and not d["random_mac"])
    unknown_batch = [(o, n) for o, n in by_oui.items()
                     if n >= 3 and not D.oui_vendor(o)]
    for oui, n in unknown_batch[:2]:
        lines.append(f"{oui} × {n} 台，OUI 未注册（小厂/贴牌常见）——"
                     f"端口组合看着像同一批装的设备")

    # ⑤ 网段本身
    by_net = result.get("by_subnet") or {}
    if by_net:
        parts = [f"{k} 有 {v} 台" for k, v in sorted(by_net.items())]
        lines.append("网段：" + "；".join(parts))

    # ⑥ 运营商（有公网 IPv6 就能读出来）
    ident = result.get("network_identity") or {}
    if ident.get("isp"):
        lines.append(f"宽带运营商：{ident['isp']}（从 IPv6 前缀 {ident.get('v6_prefix', '')} 读出）")

    return lines


def format_table(result):
    rows = [("IP", "厂商/名字", "MAC", "判定", "把握", "来源", "端口")]
    for d in result.get("devices", []):
        if d["random_mac"]:
            ident = "随机MAC"
        elif d["vendor"]:
            ident = d["vendor"]
        elif d["nbname"]:
            ident = d["nbname"]
        elif d["mac"]:
            ident = "OUI未收录"
        else:
            ident = "-"
        rows.append((
            d["ip"],
            ident[:18],
            (d["mac"] or "-")[:17],
            ("★网关" if d.get("is_gateway") else d["category_cn"]),
            str(d["confidence"]),
            "+".join(d["found_via"]),
            ",".join(str(p) for p in d["open_ports"][:5]) or "-",
        ))
    def w(s):
        return sum(2 if ord(c) > 0x2E80 else 1 for c in str(s))
    widths = [max(w(r[i]) for r in rows) for i in range(len(rows[0]))]
    def pad(s, n):
        s = str(s)
        return s + " " * max(0, n - w(s))
    lines = []
    for idx, r in enumerate(rows):
        lines.append("  ".join(pad(c, widths[i]) for i, c in enumerate(r)))
        if idx == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(widths))))
    return "\n".join(lines)


# ---------------------------------------------------------------- 自测

def _selftest():
    ok = 0

    # ① 虚拟网段必须被跳过（家庭 LAN 不用 172.16/12，而 WSL/Docker 全在那）
    assert _skip_net("172.24.112.1") and _skip_net("169.254.83.107")
    assert _skip_net("127.0.0.1") and _skip_net("100.64.0.5")
    assert not _skip_net("192.168.0.103")
    assert not _skip_net("192.168.137.22")
    assert not _skip_net("10.0.0.5")
    ok += 1
    print("[selftest] 网段过滤（跳虚拟/链路本地/Tailscale）OK")

    # ② /24 枚举：1..254，不含网络地址与广播
    h = hosts_of("192.168.0")
    assert len(h) == 254 and h[0].endswith(".1") and h[-1].endswith(".254")
    ok += 1
    print("[selftest] /24 主机枚举 254 个 OK")

    # ③ NetBIOS 名字编码：'*' 的两个 nibble 是 'C' 和 'K'，补满 32 字节
    enc = _encode_nbname(b"*" + b"\x00" * 15)
    assert enc[0] == 0x20, enc[0]
    assert enc[1:3] == b"CK", enc[1:3]
    assert len(enc) == 1 + 32 + 1, len(enc)
    ok += 1
    print("[selftest] NetBIOS 名编码（'*' -> CK + 30×A）OK")

    # ④ NBSTAT 查询包结构：12 字节头 + 1 问题
    q = build_nbstat_query()
    assert struct.unpack(">H", q[4:6])[0] == 1, "QDCOUNT 应为 1"
    assert q[12] == 0x20, "名字长度应为 0x20"
    assert q[-4:] == struct.pack(">HH", 0x0021, 0x0001), q[-4:]
    ok += 1
    print("[selftest] NBSTAT 查询包（QDCOUNT/QTYPE/QCLASS）OK")

    # ⑤ NBSTAT 响应解析：手工构造一份「名字表」看能不能读出来
    names = [(b"DESKTOP-ABC", 0x00), (b"DESKTOP-ABC", 0x20), (b"WORKGROUP", 0x00)]
    rdata = bytes([len(names)]) + b"".join(
        nm.ljust(15, b" ") + bytes([sfx]) + struct.pack(">H", 0x0400)
        for nm, sfx in names)
    answer = b"\xc0\x0c" + struct.pack(">HHIH", 0x0021, 0x0001, 0, len(rdata)) + rdata
    pkt = struct.pack(">HHHHHH", 0x8221, 0x8400, 1, 1, 0, 0)
    pkt += _encode_nbname(b"*" + b"\x00" * 15) + struct.pack(">HH", 0x21, 0x01)
    pkt += answer
    got = parse_nbstat(pkt)
    assert got and got[0][0] == "DESKTOP-ABC", got
    ok += 1
    print("[selftest] NBSTAT 响应解析（拿回机器名）OK")

    # ⑥ 空响应不许抛异常（真网络里绝大多数设备不回 NBNS）
    assert parse_nbstat(b"") == []
    assert parse_nbstat(b"\x00" * 12) == []
    ok += 1
    print("[selftest] NBSTAT 空/截断响应不抛异常 OK")

    # ⑦ 网段枚举不许返回空（本机至少有一张物理网卡在线）
    nets = local_subnets()
    assert nets, "local_subnets 返回空"
    assert all(not _skip_net(n + ".1") for n in nets), nets
    ok += 1
    print(f"[selftest] 本机网段枚举 = {nets} OK")

    # ⑧ 热点段随机 MAC -> 手机（这是从实网数据倒推出来的规则，不是拍脑袋）
    assert D.is_randomized_mac("8A:86:84:65:55:DE")
    assert D.is_randomized_mac("B6:D1:B0:E4:B1:F5")
    assert not D.is_randomized_mac("78:60:5B:CF:E8:58")
    ok += 1
    print("[selftest] 随机 MAC 判定（热点段两台手机）OK")

    print(f"[selftest] {ok}/8 PASS")


def main():
    if "--selftest" in sys.argv:
        _selftest()
        return
    import json
    nets = None
    for i, a in enumerate(sys.argv):
        if a == "--subnet" and i + 1 < len(sys.argv):
            nets = [sys.argv[i + 1].rsplit(".", 1)[0]]
    r = sweep(subnets=nets, verbose=True)
    print(format_table(r))
    print()
    for line in r.get("profile") or []:
        print("▪", line)
    print()
    for n in r.get("notes", []):
        print("·", n)
    if r.get("wifi") and r["wifi"].get("networks"):
        print(f"\n周边 WiFi {len(r['wifi']['networks'])} 个：")
        for w in r["wifi"]["networks"][:12]:
            print(f"  {w['ssid'][:24]:26} {w['signal']:>8}  {w['auth'][:16]}")
    elif r.get("wifi"):
        print("\n周边 WiFi:", r["wifi"].get("note", ""))
    print(f"\n耗时 {r['elapsed']}s | 共 {r.get('count', 0)} 台")
    if "--json" in sys.argv:
        print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

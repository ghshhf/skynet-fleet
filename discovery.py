"""主动发现：不问「你是谁」，自己看、自己猜、自己归类。

三条发现路径（互补，任一条失败都不影响其余）：

1. **组播询问** —— SSDP(239.255.255.250:1900) + mDNS(224.0.0.251:5353)
   设备自己举手报名。信息最准（能拿到型号、服务名、厂商）。
   这是「主动向外扩散」的正解：不是去撞端口，而是问「谁在」。
2. **邻居表 + OUI** —— 读 arp/ip neigh，只拿到 IP+MAC。
   零打扰、零风险，靠 MAC 前三字节查厂商，猜出是小米还是海康。
3. **端口探针 + banner** —— 对**已确认存在**的 IP 敲少数几个特征端口，
   拿 HTTP 头/title 补最后一块拼图（打印/路由/摄像头/电视）。

铁律（写在代码里，不是写在文档里）：
- 只在本机所在网段活动；目标不在本网段直接拒（`_in_lan`）
- 不做全网段暴力扫描。ARP 表里没有的 IP 不敲，宁可漏也不当入侵者
- 单轮限时收集（默认 3s），不在网络上长期驻留
- **发现 != 入网**：结果只进发现账本，必须主号批准才成为编队节点

真实上限（不粉饰）：能发现「愿意报名的」（UPnP/mDNS 设备）与「会进 ARP 表的」
（同网段任何活物）。发现不了：关掉组播的、跨 VLAN 的、走云端的（米家/涂鸦 App
里配网但关局域网协议的）、纯红外的（空调根本不在网络上——这是硬件边界，
要靠手机红外或 ESP32 网关了，见 docs 里「红外桥」一节）。
"""
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time

# ---------------------------------------------------------------- 常量

SSDP_ADDR = ("239.255.255.250", 1900)
MDNS_ADDR = ("224.0.0.251", 5353)

# mDNS 想打听的服务类型 -> 网络语义（顺序即优先级）
MDNS_QUERIES = [
    "_services._dns-sd._udp.local",   # 元查询：让设备报出它有哪些服务
    "_googlecast._tcp.local",         # Chromecast / 安卓电视
    "_airplay._tcp.local",            # Apple TV / 支持隔空的电视/音箱
    "_raop._tcp.local",               # AirPlay 音频
    "_miio._udp.local",               # 小米 IoT（局域网协议）
    "_hap._tcp.local",                # HomeKit 配件
    "_ipp._tcp.local",                # 打印机
    "_printer._tcp.local",            # 打印机（老）
    "_http._tcp.local",               # 通用 Web 设备（路由/摄像头/NAS）
    "_ssh._tcp.local",                # 可登录设备（NAS/树莓派/路由）
    "_sftp-ssh._tcp.local",           # OpenSSH on Windows/Linux
    "_smb._tcp.local",                # 共享盘
    "_companion-link._tcp.local",     # Apple 设备互连
    "_androidtvremote2._tcp.local",   # 安卓 TV 遥控
    "_spotify-connect._tcp.local",    # 音箱
    "_sonos._tcp.local",              # Sonos 音箱
    "_lutron._tcp.local",             # 智能灯
    "_esphomelib._tcp.local",         # ESPHome（自己刷的板子）
    "_arduino._tcp.local",            # Arduino/ESP
]

# 敲哪些端口。故意短：每个都是「这个类别才会开」的特征端口
PORT_PROBES = [
    (80, "http"), (443, "https"), (8080, "http-alt"), (8008, "http-alt"),
    (22, "ssh"), (23, "telnet"), (445, "smb"), (139, "smb"),
    (554, "rtsp"),        # 摄像头
    (8009, "cast"),       # Chromecast
    (7000, "airplay"),    # Apple TV
    (9100, "jetdirect"),  # 打印机 RAW
    (631, "ipp"),         # 打印机 IPP
    (54321, "miio"),      # 小米局域网协议
    (6668, "tuya"),       # 涂鸦局域网协议
    (5555, "adb"),        # Android 无线调试
    (62078, "ios-lockdown"),  # iPhone 同步
    (3389, "rdp"),        # 远程桌面
    (5000, "upnp-alt"),   # 群晖/NAS Web
    (9000, "fleet"),      # 咱们自己的节点！
]

# MAC 厂商前缀（OUI）—— 只收常见家用/数码品牌，够判断「这是什么东西家的」
OUI_VENDOR = {
    "00:1A:79": "Roku", "00:1B:A9": "Brother", "00:1E:C2": "Apple",
    "00:03:93": "Apple", "00:0A:27": "Apple", "00:1F:F3": "Apple",
    "00:25:00": "Apple", "3C:22:FB": "Apple", "A4:83:E7": "Apple",
    "F0:18:98": "Apple", "AC:BC:32": "Apple", "D0:81:7A": "Apple",
    "00:1D:BA": "Sony", "00:13:A9": "Sony", "FC:0F:E6": "Sony",
    "30:39:26": "Sony", "54:42:49": "Sony", "78:84:3C": "Sony",
    "00:15:99": "Samsung", "00:16:32": "Samsung", "34:23:87": "Samsung",
    "5C:0A:5B": "Samsung", "8C:77:12": "Samsung", "B8:D9:CE": "Samsung",
    "00:1E:10": "Huawei", "00:46:4B": "Huawei", "04:BD:70": "Huawei",
    "20:F3:A3": "Huawei", "48:46:FB": "Huawei", "70:72:3C": "Huawei",
    "AC:E2:15": "Huawei", "F8:4A:BF": "Huawei", "28:6E:D4": "Huawei",
    "00:9E:C8": "Xiaomi", "04:CF:8C": "Xiaomi", "0C:1D:AF": "Xiaomi",
    "28:6C:07": "Xiaomi", "34:CE:00": "Xiaomi", "50:8F:4C": "Xiaomi",
    "64:09:80": "Xiaomi", "64:B4:73": "Xiaomi", "78:11:DC": "Xiaomi",
    "8C:BE:BE": "Xiaomi", "9C:99:A0": "Xiaomi", "F0:B4:29": "Xiaomi",
    "A4:DA:22": "Xiaomi", "74:23:44": "Xiaomi", "E8:AB:FA": "Xiaomi",
    "28:DA:2D": "Xiaomi-iot", "04:7A:0B": "Xiaomi-iot", "2C:BE:EB": "Xiaomi-iot",
    "00:0C:43": "MediaTek", "00:1C:7E": "TP-Link", "14:CC:20": "TP-Link",
    "50:C7:BF": "TP-Link", "A4:2B:B0": "TP-Link", "C0:25:E9": "TP-Link",
    "EC:08:6B": "TP-Link", "F4:F2:6D": "TP-Link", "B0:BE:76": "TP-Link",
    "00:1D:0F": "TP-Link", "30:B5:C2": "TP-Link", "98:DA:C4": "TP-Link",
    "00:0C:29": "VMware", "00:50:56": "VMware", "08:00:27": "VirtualBox",
    "B8:27:EB": "Raspberry Pi", "DC:A6:32": "Raspberry Pi", "E4:5F:01": "Raspberry Pi",
    "24:0A:C4": "Espressif(ESP)", "30:AE:A4": "Espressif(ESP)",
    "3C:61:05": "Espressif(ESP)", "84:CC:A8": "Espressif(ESP)",
    "8C:AA:B5": "Espressif(ESP)", "A0:20:A6": "Espressif(ESP)",
    "C4:4F:33": "Espressif(ESP)", "DC:4F:22": "Espressif(ESP)",
    "3C:84:6A": "Dahua", "BC:32:5F": "Dahua", "E0:50:8B": "Dahua",
    "4C:BD:8F": "Hikvision", "44:19:B6": "Hikvision", "C0:56:E3": "Hikvision",
    "54:C4:15": "Hikvision", "A4:14:37": "Hikvision",
    "00:1F:92": "Midea", "EC:FA:5C": "Midea", "34:96:72": "Midea",
    "00:23:A7": "Gree", "20:FA:BB": "Gree", "5C:E0:F6": "Gree",
    "00:0E:8E": "Haier", "38:B8:EB": "Haier", "AC:67:B2": "Haier",
    "00:1D:7E": "Cisco", "00:26:0B": "Cisco", "F8:0B:BE": "Cisco",
    "00:0C:6E": "Asus", "2C:56:DC": "Asus", "38:D5:47": "Asus",
    "70:85:C2": "Asus", "AC:22:0B": "Asus", "D8:50:E6": "Asus",
    "00:1E:58": "Synology", "00:11:32": "Synology", "90:09:D0": "Synology",
    "00:0C:34": "Intel", "3C:97:0E": "Intel", "7C:B2:7D": "Intel",
    "00:1B:21": "Intel", "A0:36:9F": "Intel",
    "00:15:5D": "Hyper-V", "00:16:3E": "Xen", "52:54:00": "QEMU",
    "00:9A:CD": "Huawei", "9C:28:EF": "Huawei", "14:5F:94": "Huawei",
    "00:8A:76": "Apple", "9C:20:7B": "Apple", "C8:B5:AD": "Apple",
}

# 类别判定：命中即加权重。分数高者胜出
# 实测教训：**设备自报的型号/服务名远比端口组合可靠**。
# .109 开了 554 被判摄像头，其实 mDNS 报的是 Microsoft-Windows + iVCam（一台电脑）。
CATEGORY_RULES = [
    # (类别, 正则/关键字, 权重, 作用域 all|ssdp|mdns|port|http|oui)
    # —— 设备自报身份（最硬）——
    ("camera", r"_psia|_CGI|HIKVISION|DS-2CD|DS-2DE|onvif|dahua|ezviz|reolink", 9, "all"),
    ("router", r"TPOS|YLWR|易展|openwrt|asuswrt|miwifi|routerlogin", 8, "all"),
    ("pc", r"Microsoft-Windows|e2esoft|ivcam|_sftp-ssh|_smb|_rdp", 8, "all"),
    ("phone", r"_adb|android|iphone|ipad|ios-lockdown|companion-link", 6, "all"),
    # —— 端口/协议族（次之）——
    ("router", r"router|gateway|InternetGatewayDevice|WANDevice|WANConnectionDevice", 7, "all"),
    ("nas", r"synology|qnap|diskstation|openmediavault|truenas|fnos|webdav", 7, "all"),
    ("tv", r"mediarenderer|media\[?server|dial|bravia|webos|tizen|roku|androidtvremote|googlecast", 5, "all"),
    ("speaker", r"speaker|soundbar|sonos|raop|airplay|homepod|jbl|bose|audio", 5, "all"),
    ("camera", r"rtsp|onvif|camera|ipcam|tapo", 5, "all"),
    ("printer", r"printer|jetdirect|ipp|laserjet|deskjet|brother", 7, "all"),
    ("ac", r"aircondition|air-condition|ac-control|gree|midea|haier-ac", 7, "all"),
    ("hub", r"homeassistant|homebridge|esphomelib|hassio|deconz|zigbee|hue", 6, "all"),
    ("light", r"hue|lifx|lutron|yeelight|bulb", 5, "all"),
    ("fleet", r"fleet|北极星|polaris|skynet", 9, "all"),
    ("sensor", r"sensor|thermostat|weather|miio|tuya", 4, "all"),
]

# 端口 -> (类别, 权重)
# 原则：**端口只说明「可能」，不说明「是什么」**。所以任何单端口都不给到能定案的分数，
# 并且「我方协议端口」绝不参与分类（见 POSSIBLE_FLEET_PORTS）——
# 9000 同时也是 Portainer/海康 SDK/各种面板的端口，把它当编队节点会造成灾难性误判：
# 把别人家设备误批准入网，等于给陌生人开门。
PORT_CATEGORY = {
    554: ("camera", 5), 8009: ("tv", 7), 7000: ("tv", 4), 9100: ("printer", 7),
    631: ("printer", 6), 54321: ("sensor", 5), 6668: ("sensor", 5),
    5555: ("phone", 8), 62078: ("phone", 8), 3389: ("pc", 6),
    445: ("nas", 2), 139: ("nas", 2),
    23: ("camera", 3),      # 老式 IPC/NVR 常开 telnet；也算老设备
    80: ("router", 1),      # Web 管理页：什么都可能是它，只给最轻的一票
}

# 这些端口开着只代表「可能是自己人」，必须真握手才认。默认只标标记、不定类别
POSSIBLE_FLEET_PORTS = (9000,)

# 组合特征：单个端口谁都可能是，**几个端口同时出现**才有判别力。
# 实测来自本机真实局域网：9000+554+23+8080 同时开的就是海康系 NVR/IPC 的招牌组合。
COMBO_RULES = [
    ({9000, 554, 23, 8080}, "camera", 12,
     "海康系 NVR/IPC：SDK 9000 + RTSP 554 + telnet 23 + Web 8080"),
    ({9000, 554, 80}, "camera", 9, "海康系 IPC：SDK 9000 + RTSP 554 + Web 80"),
    ({554, 23}, "camera", 8, "摄像头：RTSP + telnet（老式 IPC 标配）"),
    ({8008, 8009}, "tv", 9, "Chromecast：cast 双端口 8008/8009"),
    ({445, 139, 3389}, "pc", 9, "Windows 主机：SMB 445/139 + RDP 3389"),
    ({5000, 22}, "nas", 8, "NAS：Web 5000 + SSH"),
    ({9100, 631}, "printer", 10, "打印机：RAW 9100 + IPP 631"),
    ({80, 443, 53}, "router", 7, "网关类：Web 80/443 + DNS 53"),
]

CATEGORY_CN = {
    "router": "路由器/网关", "nas": "NAS/存储", "tv": "电视/投屏",
    "speaker": "音箱", "camera": "摄像头", "printer": "打印机",
    "ac": "空调/温控", "hub": "智能中枢", "light": "灯具",
    "phone": "手机/平板", "pc": "电脑", "fleet": "编队节点",
    "sensor": "传感器/IoT", "unknown": "未知",
}


# ---------------------------------------------------------------- 网段闸门

def _local_ip():
    """取本机出口 IP。只做路由查询，不发一个包。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 53))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


def _subnet_of(ip, mask=24):
    parts = ip.split(".")
    if len(parts) != 4:
        return None
    return ".".join(parts[:3]) + f".0/{mask}"


def _in_lan(ip, my_ip, mask=24):
    """目标必须和本机同 /24 网段。跨网段一律拒——发现只在自家客厅里发生。"""
    if not my_ip or my_ip == "127.0.0.1":
        return False
    a, b = ip.split("."), my_ip.split(".")
    if len(a) != 4 or len(b) != 4:
        return False
    return a[0] == b[0] and a[1] == b[1] and a[2] == b[2]


# ---------------------------------------------------------------- SSDP

def probe_ssdp(timeout=3.0, target=SSDP_ADDR, local_ip=None):
    """组播问一句「谁在」，收一圈回复。

    target 可注入（自测指向 127.0.0.1），这是唯一为了可测性留的口子。
    local_ip 必须给：本机有多张网卡时（以太网/WLAN/Hyper-V/Tailscale），
    不显式绑定就会从错误的网卡发出去，结果是「一个响应都收不到」。
    """
    msg = ("M-SEARCH * HTTP/1.1\r\n"
           "HOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\n'
           "MX: 2\r\n"
           "ST: ssdp:all\r\n\r\n").encode()
    out = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if local_ip:
            try:
                s.bind((local_ip, 0))
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                             socket.inet_aton(local_ip))
            except OSError:
                pass
        s.settimeout(0.35)
        tries = 2
        for _ in range(tries):
            try:
                s.sendto(msg, target)
            except OSError:
                break
        deadline = time.time() + timeout
        seen = set()
        while time.time() < deadline:
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", "replace")
            ip = addr[0]
            if ip in seen:
                continue
            seen.add(ip)
            out.append(_parse_ssdp(text, ip))
    finally:
        s.close()
    return [r for r in out if r]


def _parse_ssdp(text, ip):
    """解析 SSDP 响应/NOTIFY。第一行可能是 200 OK 或 NOTIFY * HTTP/1.1。"""
    lines = [ln.strip() for ln in text.split("\r\n") if ln.strip()]
    if not lines:
        return None
    head = lines[0].upper()
    if not (head.startswith("HTTP/1.1 200") or head.startswith("NOTIFY")):
        return None
    h = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            h[k.strip().lower()] = v.strip()
    return {
        "ip": ip,
        "via": "ssdp",
        "server": h.get("server", ""),
        "usn": h.get("usn", ""),
        "st": h.get("st", ""),
        "location": h.get("location", ""),
        "nt": h.get("nt", ""),
    }


# ---------------------------------------------------------------- mDNS

def _enc_name(name):
    out = b""
    for label in name.split("."):
        if label:
            lb = label.encode()
            out += bytes([len(lb)]) + lb
    return out + b"\x00"


def _build_mdns_query(names, qid=0):
    body = b""
    for n in names:
        body += _enc_name(n) + struct.pack(">HH", 12, 1)  # PTR, IN
    header = struct.pack(">HHHHHH", qid, 0, len(names), 0, 0, 0)
    return header + body


def _read_name(buf, off, depth=0):
    """读 DNS 名字，支持压缩指针 0xC0。返回 (名字, 新偏移)。"""
    if depth > 8:
        return "", off + 1
    labels = []
    start_off = off
    jumped = False
    while off < len(buf):
        ln = buf[off]
        if ln == 0:
            off += 1
            break
        if ln & 0xC0 == 0xC0:
            if off + 1 >= len(buf):
                return ".".join(labels), off + 1
            ptr = ((ln & 0x3F) << 8) | buf[off + 1]
            if not jumped:
                start_off = off + 2
                jumped = True
            sub, _ = _read_name(buf, ptr, depth + 1)
            if sub:
                labels.append(sub)
            off += 2
            break
        off += 1
        labels.append(buf[off:off + ln].decode("utf-8", "replace"))
        off += ln
    return ".".join([x for x in labels if x]), (start_off if jumped else off)


def parse_mdns(buf):
    """解析 mDNS 响应，抽出 (服务类型列表, 名称->IP, TXT 键值)。"""
    if len(buf) < 12:
        return None
    qd, an, ns, ar = struct.unpack(">HHHH", buf[4:12])
    off = 12
    for _ in range(qd):
        _, off = _read_name(buf, off)
        off += 4
    services, names, txt = [], {}, {}
    total = an + ns + ar
    for _ in range(total):
        if off + 10 > len(buf):
            break
        name, off = _read_name(buf, off)
        if off + 10 > len(buf):
            break
        rtype, rclass, _ttl, rdlen = struct.unpack(">HHIH", buf[off:off + 10])
        off += 10
        rdata_off = off
        off += rdlen
        if rdata_off + rdlen > len(buf):
            break
        if rtype == 12:  # PTR -> 服务类型实例
            target, _ = _read_name(buf, rdata_off)
            if target:
                services.append(target)
        elif rtype == 33:  # SRV -> 目标主机 + 端口
            if rdlen > 6:
                tgt, _ = _read_name(buf, rdata_off + 6)
                port = struct.unpack(">H", buf[rdata_off + 4:rdata_off + 6])[0]
                if tgt:
                    names.setdefault(tgt, {})["srv_port"] = port
        elif rtype == 1 and rdlen == 4:  # A
            ip = ".".join(str(b) for b in buf[rdata_off:rdata_off + 4])
            names.setdefault(name, {})["ip"] = ip
        elif rtype == 16:  # TXT
            i = rdata_off
            end = rdata_off + rdlen
            kv = {}
            while i < end:
                ln = buf[i]
                i += 1
                seg = buf[i:i + ln].decode("utf-8", "replace")
                i += ln
                if "=" in seg:
                    k, v = seg.split("=", 1)
                    kv[k] = v
                elif seg:
                    kv[seg] = ""
            if kv:
                txt[name] = kv
    return {"services": services, "names": names, "txt": txt}


def probe_mdns(timeout=3.0, target=MDNS_ADDR, local_ip=None):
    """组播打听服务。源端口随机，响应会以单播回到本端口。"""
    out = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
        pkt = _build_mdns_query(MDNS_QUERIES)
        for _ in range(2):
            try:
                s.sendto(pkt, target)
            except OSError:
                break
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = s.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            parsed = parse_mdns(data)
            if not parsed:
                continue
            out.append({
                "ip": addr[0],
                "via": "mdns",
                "services": parsed["services"],
                "names": parsed["names"],
                "txt": parsed["txt"],
            })
    finally:
        s.close()
    return out


# ---------------------------------------------------------------- 邻居表

_ARP_LINE = re.compile(
    r"(?P<ip>[0-9]{1,3}(?:\.[0-9]{1,3}){3})"
    r"[^\n]*?"                                  # Windows 直接空格分隔；Linux 夹着 "dev eth0 lladdr"
    r"(?P<mac>(?:[0-9a-fA-F]{2}[-:]){5}[0-9a-fA-F]{2})")


def parse_arp(text):
    """解析 arp -a / ip neigh 的文本输出 -> [(ip, mac)]。"""
    out = []
    for line in text.splitlines():
        m = _ARP_LINE.search(line)
        if m:
            out.append((m.group(1), m.group(2).upper().replace("-", ":")))
    return out


def read_neighbors():
    """读系统邻居表。Windows 用 arp -a，Linux 用 ip neigh（退回 arp -a）。"""
    cmds = [["arp", "-a"]] if os.name == "nt" else [["ip", "neigh"], ["arp", "-an"]]
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=8)
        except (OSError, subprocess.SubprocessError):
            continue
        text = None
        for enc in ("utf-8", "gbk", "cp936"):
            try:
                text = r.stdout.decode(enc)
                break
            except (UnicodeDecodeError, AttributeError):
                continue
        if not text:
            continue
        got = parse_arp(text)
        if got:
            return got
    return []


def _is_noise_ip(ip, my_ip=None):
    """过滤掉「不是设备」的地址：网络地址、广播、组播、链路本地、本机。

    这一条不做，192.168.0.255 会作为一台「未知设备」躺在结果里——
    不是小事，是让整张表失去可信度的脏数据。
    """
    if ip == my_ip:
        return True
    parts = ip.split(".")
    if len(parts) != 4:
        return True
    try:
        n = [int(p) for p in parts]
    except ValueError:
        return True
    if any(x < 0 or x > 255 for x in n):
        return True
    if n[3] in (0, 255):                      # 网络地址 / 广播地址
        return True
    if n[0] == 0 or n[0] == 255:
        return True
    if n[0] == 169 and n[1] == 254:           # 链路本地（没拿到 DHCP 的假地址）
        return True
    if 224 <= n[0] <= 239:                    # 组播
        return True
    return False


def is_randomized_mac(mac):
    """MAC 第一字节的 bit1 = 本地管理位。Android 10+/iOS 默认开随机 MAC，
    这类地址**查 OUI 必然查不到**，不能当成「厂商缺失」，更不能硬猜。
    """
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError, AttributeError):
        return False


def oui_vendor(mac):
    if not mac or len(mac) < 8:
        return ""
    return OUI_VENDOR.get(mac[:8].upper(), "")


# ---------------------------------------------------------------- 网关与组合特征

_GW_WIN = re.compile(
    r"(?:默认网关|Default Gateway)[^\n:：]*[:：]\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})")
_GW_LIN = re.compile(r"default\s+via\s+([0-9]{1,3}(?:\.[0-9]{1,3}){3})")


def parse_gateway(text):
    """从 ipconfig / ip route 的输出里取默认网关。取不到返回空串。"""
    for pat in (_GW_WIN, _GW_LIN):
        m = pat.search(text or "")
        if m:
            return m.group(1)
    return ""


def default_gateway():
    """本网段网关。**这是「知道」而不是「猜」**：网关必然是路由器/主路由。

    Windows 的 ipconfig 输出是 GBK，Linux 是 UTF-8，所以按 utf-8 严格 → gbk 退让。
    """
    cmds = [["ipconfig"], ["ip", "route", "show", "default"], ["ip", "route"]]
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=8)
        except (OSError, subprocess.SubprocessError):
            continue
        text = None
        for enc in ("utf-8", "gbk", "cp936"):
            try:
                text = r.stdout.decode(enc)
                break
            except (UnicodeDecodeError, AttributeError):
                text = None
        if text:
            gw = parse_gateway(text)
            if gw:
                return gw
    return ""


def combo_hits(ports):
    """端口组合命中：[(类别, 权重, 说明)]。

    同一类别只取**最具体**的那条：{9000,554,23,8080} 会同时命中「海康 NVR」和
    「RTSP+telnet」两条，累加会把分数撑到 20，属于自己给自己发奖状。
    """
    s = set(ports or [])
    best = {}
    for need, cat, w, desc in COMBO_RULES:
        if need <= s and (cat not in best or w > best[cat][0]):
            best[cat] = (w, desc)
    return [(cat, w, desc) for cat, (w, desc) in best.items()]


def extract_model(mdns_services=None, ssdp=None):
    """从设备自报的名字里抠型号——这是整套识别里最准的一环。

    mDNS 实例名常形如 "HIKVISION DS-2CD2T10FD-I3 - E89693295._http._tcp.local"，
    点号前那段带空格的，就是「厂商 + 型号 + 序列号」。端口可以猜错，这个猜不错。
    退一步用 SSDP 的 SERVER 头（如 "TPOS/1.0.0 UPnP/1.0 YLWR1200G易展版/1.0"）。
    """
    for s in mdns_services or []:
        head = str(s).split("._")[0].strip().rstrip(".")
        if head and not head.startswith("_") and " " in head and len(head) > 6:
            return head[:72]
    server = str((ssdp or {}).get("server", "") or "").strip()
    # "1.0" 这种只有版本号的 SERVER 头不是型号，宁可留空也不给出假信息
    if len(server) >= 12:
        return server[:72]
    return ""


# ---------------------------------------------------------------- 端口探针

def probe_ports(ip, ports=None, timeout=0.35):
    """只敲特征端口，拿到就关。**不发送任何指令**，只建立连接。

    并发执行：局域网里 34 台 × 20 端口串行要 2 分钟以上，并发后 2 秒内结束。
    """
    from concurrent.futures import ThreadPoolExecutor
    plist = [p for p, _tag in (ports or PORT_PROBES)]

    def one(p):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            return p if s.connect_ex((ip, p)) == 0 else None
        except OSError:
            return None
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=min(20, len(plist) or 1)) as ex:
        got = list(ex.map(one, plist))
    return [p for p in got if p]


def fetch_http_banner(ip, port, timeout=1.2):
    """取 HTTP 头（必要时再取一点 body 找 <title>）。只读，不改任何东西。"""
    for scheme in ("http",):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            if s.connect_ex((ip, port)) != 0:
                return {}
            req = (f"GET / HTTP/1.0\r\nHost: {ip}\r\n"
                   "User-Agent: fleet-discovery/1.0\r\n"
                   "Connection: close\r\n\r\n").encode()
            s.sendall(req)
            buf = b""
            while len(buf) < 4096:
                try:
                    chunk = s.recv(2048)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
            text = buf.decode("utf-8", "replace")
            hdr = {}
            body = ""
            if "\r\n\r\n" in text:
                head, body = text.split("\r\n\r\n", 1)
                for ln in head.split("\r\n")[1:]:
                    if ":" in ln:
                        k, v = ln.split(":", 1)
                        hdr[k.strip().lower()] = v.strip()
            else:
                hdr["_raw"] = text[:200]
            title = ""
            m = re.search(r"<title[^>]*>([^<]{1,80})</title>", body, re.I)
            if m:
                title = m.group(1).strip()
            return {"server": hdr.get("server", ""), "title": title,
                    "location": hdr.get("location", ""), "port": port}
        except OSError:
            return {}
        finally:
            s.close()
    return {}


# ---------------------------------------------------------------- 指纹打分

def score_categories(evidence):
    """把证据揉成一票制打分。返回 [(类别, 分数)] 降序。

    evidence: {"ssdp": str, "mdns": str, "http": str, "oui": str, "ports": [int]}
    """
    scores = {}
    blob_all = " ".join(str(evidence.get(k, "")) for k in ("ssdp", "mdns", "http"))
    for cat, pat, w, scope in CATEGORY_RULES:
        target = blob_all if scope == "all" else str(evidence.get(scope, ""))
        if not target:
            continue
        if re.search(pat, target, re.I):
            scores[cat] = scores.get(cat, 0) + w
    for p in evidence.get("ports", []) or []:
        hit = PORT_CATEGORY.get(p)
        if hit:
            scores[hit[0]] = scores.get(hit[0], 0) + hit[1]
    vendor = str(evidence.get("oui", ""))
    if vendor:
        if "Xiaomi" in vendor:
            scores["sensor"] = scores.get("sensor", 0) + 2
        elif "Espressif" in vendor:
            scores["sensor"] = scores.get("sensor", 0) + 3
        elif "Raspberry" in vendor:
            scores["hub"] = scores.get("hub", 0) + 3
        elif vendor in ("Apple", "Samsung", "Huawei"):
            scores["phone"] = scores.get("phone", 0) + 1
        elif "Hikvision" in vendor or "Dahua" in vendor:
            scores["camera"] = scores.get("camera", 0) + 6
        elif vendor in ("Midea", "Gree", "Haier"):
            scores["ac"] = scores.get("ac", 0) + 5
        elif "TP-Link" in vendor:
            scores["router"] = scores.get("router", 0) + 2
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


# ---------------------------------------------------------------- 主流程

def discover(local_ip=None, timeout=3.0, do_ports=True, max_hosts=24,
             skip_self=True, verbose=False):
    """跑一轮发现。返回结构化结果，不写任何文件、不注册任何节点。"""
    my_ip = local_ip or _local_ip()
    result = {
        "at": int(time.time()),
        "my_ip": my_ip,
        "subnet": _subnet_of(my_ip) if my_ip else None,
        "devices": [],
        "notes": [],
    }
    per_ip = {}

    def slot(ip, via):
        d = per_ip.get(ip)
        if d is None:
            d = per_ip[ip] = {"ip": ip, "via": set(), "ssdp": {}, "mdns": {},
                              "ports": [], "http": {}, "mac": "", "vendor": "",
                              "random_mac": False}
        d["via"].add(via)
        return d

    # ① 组播：谁愿意报名（显式绑本机网卡，否则多网卡机器发错接口一个都收不到）
    try:
        for r in probe_ssdp(timeout=timeout, local_ip=my_ip):
            if skip_self and r["ip"] == my_ip:
                continue
            d = slot(r["ip"], "ssdp")
            d["ssdp"] = {k: v for k, v in r.items() if k not in ("ip", "via")}
    except Exception as e:  # 组播在某些网卡/防火墙下会失败，不能因此整轮作废
        result["notes"].append(f"SSDP 失败: {e}")
    try:
        for r in probe_mdns(timeout=timeout, local_ip=my_ip):
            if skip_self and r["ip"] == my_ip:
                continue
            d = slot(r["ip"], "mdns")
            d["mdns"] = {"services": r["services"], "names": r["names"], "txt": r["txt"]}
    except Exception as e:
        result["notes"].append(f"mDNS 失败: {e}")

    # ② 邻居表：谁确实在线（零打扰）
    try:
        for ip, mac in read_neighbors():
            if not _in_lan(ip, my_ip):
                continue
            if _is_noise_ip(ip, my_ip if skip_self else None):
                continue
            d = slot(ip, "arp")
            d["mac"] = mac
            d["random_mac"] = is_randomized_mac(mac)
            # 随机 MAC 查 OUI 必空——不查、不猜，直接标注来源
            d["vendor"] = "" if d["random_mac"] else oui_vendor(mac)
    except Exception as e:
        result["notes"].append(f"邻居表失败: {e}")

    # ②b 网关：这个不用猜，路由表写着
    gw = ""
    try:
        gw = default_gateway()
        if gw:
            result["gateway"] = gw
    except Exception as e:
        result["notes"].append(f"取网关失败: {e}")

    # ③ 端口 + banner：只对已知存在的 IP 敲（主机间也并发，否则一轮要几分钟）
    if do_ports:
        from concurrent.futures import ThreadPoolExecutor
        hosts = list(per_ip.keys())[:max_hosts]

        def _scan(ip):
            d = per_ip[ip]
            try:
                d["ports"] = probe_ports(ip)
            except OSError:
                d["ports"] = []
            for p in (80, 8080, 8008, 5000):
                if p in d["ports"]:
                    d["http"] = fetch_http_banner(ip, p)
                    if d["http"]:
                        break

        with ThreadPoolExecutor(max_workers=10) as ex:
            list(ex.map(_scan, hosts))

    # ④ 归类
    for ip, d in per_ip.items():
        ssdp_txt = " ".join(str(v) for v in d["ssdp"].values())
        mdns_txt = " ".join(d["mdns"].get("services", []))
        for nm, info in (d["mdns"].get("names") or {}).items():
            mdns_txt += " " + nm
        for name, kv in (d["mdns"].get("txt") or {}).items():
            mdns_txt += " " + name + " " + " ".join(f"{k}={v}" for k, v in kv.items())
        http_txt = " ".join(str(v) for v in d["http"].values())
        ranked = score_categories({
            "ssdp": ssdp_txt, "mdns": mdns_txt, "http": http_txt,
            "oui": d["vendor"], "ports": d["ports"],
        })
        port_set = set(d["ports"])
        # 组合特征加成：几个端口同时出现才有判别力（单端口只能投票）
        combos = combo_hits(d["ports"])
        if combos:
            merged = dict(ranked)
            for c, w, _desc in combos:
                merged[c] = merged.get(c, 0) + w
            ranked = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
        has_ipc = 554 in port_set and (23 in port_set or 8080 in port_set)
        # 网关：查路由表得出的，不是投票投出来的，直接定案
        is_gw = bool(gw) and ip == gw
        if is_gw:
            ranked = [("router", 10)] + [x for x in ranked if x[0] != "router"]
        cat = ranked[0][0] if ranked and ranked[0][1] >= 3 else "unknown"
        conf = ranked[0][1] if ranked else 0
        # 只领先第二名 1-2 分不算定案：明显模棱两可的必须自己承认
        second = ranked[1][1] if len(ranked) > 1 else 0
        ambiguous = bool(cat != "unknown" and conf - second < 2 and not is_gw)
        why = [desc for _c, _w, desc in combos]
        if is_gw:
            why.insert(0, "本网段默认网关（查路由表得出，不是猜）")
        model = extract_model(d["mdns"].get("services"), d["ssdp"])
        result["devices"].append({
            "ip": ip,
            "mac": d["mac"],
            "vendor": d["vendor"],
            "model": model,
            "random_mac": d["random_mac"],
            "category": cat,
            "category_cn": CATEGORY_CN.get(cat, cat),
            "confidence": conf,
            "ambiguous": ambiguous,
            "is_gateway": is_gw,
            # 9000 与海康特征同时出现时，绝不标「疑似我方」——那是把陌生人当自己人
            "possible_fleet": any(p in port_set for p in POSSIBLE_FLEET_PORTS) and not has_ipc,
            "found_via": sorted(d["via"]),
            "why": why,
            "open_ports": d["ports"],
            "ssdp": d["ssdp"],
            "mdns_services": d["mdns"].get("services", []),
            "http": d["http"],
            "alternatives": ranked[1:3],
        })

    # 同 OUI 多台 = 同一批次部署，这条比单台判定更有信息量
    from collections import Counter
    batch = Counter(x["mac"][:8] for x in result["devices"]
                    if x["mac"] and not x["random_mac"])
    for oui, n in batch.items():
        if n >= 3:
            cats = {x["category_cn"] for x in result["devices"] if x["mac"].startswith(oui)}
            result["notes"].append(
                f"同厂家批次 {oui} × {n} 台（{'/'.join(sorted(cats))}），像是批量装的一批设备")

    result["devices"].sort(key=lambda x: (x["category"] != "fleet", -x["confidence"], x["ip"]))
    result["count"] = len(result["devices"])
    return result


def format_table(result):
    """给人看的一屏表格。中文按两个宽度算，保证框不歪。"""
    def w(s):
        n = 0
        for ch in str(s):
            n += 2 if ord(ch) > 0x2E80 else 1
        return n

    def pad(s, width):
        s = str(s)
        return s + " " * max(0, width - w(s))

    rows = [("IP", "设备（自报型号优先）", "判定", "把握", "来源", "端口", "备注")]
    for d in result.get("devices", []):
        tags = []
        if d.get("is_gateway"):
            tags.append("网关")
        if d.get("ambiguous"):
            tags.append("存疑")
        if d.get("possible_fleet"):
            tags.append("9000开放待确认")
        if d.get("random_mac"):
            tags.append("随机MAC")
        elif d["vendor"] == "" and d["mac"] and not d.get("model"):
            tags.append("厂商未收录")
        # 未知设备也给它一点信息量：它自报了哪些服务
        svcs = d.get("mdns_services") or []
        if d["category"] == "unknown" and svcs:
            nm = sorted({s.split(".")[0] for s in svcs if s and not s.startswith("_")})[:1]
            if not nm:
                nm = sorted({s.split(".")[0] for s in svcs if s})[:2]
            if nm:
                tags.append("服务:" + ",".join(nm)[:16])
        dev = d.get("model") or d.get("vendor") or "-"
        if len(dev) > 30:
            dev = dev[:28] + ".."
        rows.append((
            d["ip"],
            dev,
            d["category_cn"],
            str(d["confidence"]),
            "+".join(d["found_via"]),
            ",".join(str(p) for p in d["open_ports"][:5]) or "-",
            "/".join(tags) or "",
        ))
    widths = [max(w(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for idx, r in enumerate(rows):
        line = "  ".join(pad(c, widths[i]) for i, c in enumerate(r)).rstrip()
        lines.append(line)
        if idx == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(widths))))
    notes = result.get("notes") or []
    if notes:
        lines.append("")
        for n in notes:
            lines.append(f"· {n}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 自测

def _selftest():
    import threading
    ok = 0

    # ① SSDP 响应解析
    raw = ("HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\n"
           "LOCATION: http://192.168.0.5:80/desc.xml\r\n"
           "SERVER: Linux/3.14 UPnP/1.0 Sonos/57.0\r\n"
           "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
           "USN: uuid:RINCON_123::urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n")
    r = _parse_ssdp(raw, "192.168.0.5")
    assert r and r["server"].startswith("Linux"), r
    assert "MediaRenderer" in r["st"]
    ok += 1
    print("[selftest] SSDP 响应解析 OK")

    # ② 非 200 且非 NOTIFY 的行必须被丢（防止把垃圾当设备）
    assert _parse_ssdp("HELLO WORLD\r\nX: 1\r\n\r\n", "1.2.3.4") is None
    ok += 1
    print("[selftest] SSDP 垃圾包被丢弃 OK")

    # ③ mDNS 包解析（含压缩指针）
    # 构造：header(qd=0,an=2) + PTR 记录(target 用指针回指 name)
    def enc(n):
        return _enc_name(n)
    svc = "_googlecast._tcp.local"
    inst = "客厅电视._googlecast._tcp.local"
    pkt = struct.pack(">HHHHHH", 0, 0x8400, 0, 2, 0, 0)
    # 记录1 PTR：owner=服务类型（字面），rdata=实例名（字面）
    pkt += enc(svc) + struct.pack(">HHIH", 12, 1, 120, len(enc(inst))) + enc(inst)
    # 记录2 SRV：owner=实例名（字面），rdata = priority/weight/port + target
    tgt = "tv.local"
    rd = struct.pack(">HHH", 0, 0, 8009) + enc(tgt)
    pkt += enc(inst) + struct.pack(">HHIH", 33, 1, 120, len(rd)) + rd
    parsed = parse_mdns(pkt)
    assert parsed, "mDNS 解析返回空"
    assert any("googlecast" in s for s in parsed["services"]), parsed["services"]
    assert parsed["names"].get("tv.local", {}).get("srv_port") == 8009, parsed["names"]
    ok += 1
    print("[selftest] mDNS 解析（PTR/SRV）OK")

    # ④ 压缩指针：解引用正确 + 返回位移落在指针之后（parse 靠这个位移继续读 type）
    buf = b"\x00" * 12
    buf += _enc_name("_airplay._tcp.local")
    ptr_at = len(buf)
    buf += b"\xc0\x0c"                # 指向 offset 12
    buf += struct.pack(">HHIH", 12, 1, 120, 4)   # 紧跟指针之后的记录头
    nm, after = _read_name(buf, ptr_at)
    assert nm == "_airplay._tcp.local", nm
    assert after == ptr_at + 2, (after, ptr_at)   # 位移必须跳过 2 字节指针
    rtype = struct.unpack(">H", buf[after:after + 2])[0]
    assert rtype == 12, rtype                      # 指针不会把 type 解析串位
    ok += 1
    print("[selftest] mDNS 压缩指针（名字+位移+后续字段不错位）OK")

    # ⑤ ARP 解析（Windows 与 Linux 两种格式）
    win = ("接口: 192.168.0.103 --- 0x9\n"
           "  192.168.0.1           aa-bb-cc-dd-ee-ff     动态\n"
           "  192.168.0.50          28-6c-07-11-22-33     动态\n")
    got = parse_arp(win)
    assert got == [("192.168.0.1", "AA:BB:CC:DD:EE:FF"),
                   ("192.168.0.50", "28:6C:07:11:22:33")], got
    lin = "192.168.1.7 dev eth0 lladdr b8:27:eb:01:02:03 REACHABLE"
    assert parse_arp(lin) == [("192.168.1.7", "B8:27:EB:01:02:03")], parse_arp(lin)
    ok += 1
    print("[selftest] ARP/ip neigh 两种格式解析 OK")

    # ⑥ OUI 厂商识别
    assert oui_vendor("28:6C:07:11:22:33") == "Xiaomi"
    assert oui_vendor("B8:27:EB:AA:BB:CC") == "Raspberry Pi"
    assert oui_vendor("FF:FF:FF:FF:FF:FF") == ""
    ok += 1
    print("[selftest] OUI 厂商识别 OK")

    # ⑦ 指纹打分：SSDP MediaRenderer -> 电视/音箱；9100 -> 打印机
    a = score_categories({"ssdp": "urn:schemas-upnp-org:device:MediaRenderer:1",
                          "ports": []})
    assert a and a[0][0] in ("speaker", "tv"), a
    b = score_categories({"ports": [9100], "http": "", "ssdp": "", "mdns": ""})
    assert b[0][0] == "printer", b
    c = score_categories({"ports": [554], "oui": "Hikvision", "ssdp": "", "mdns": ""})
    assert c[0][0] == "camera", c
    d = score_categories({"ports": [54321], "oui": "Xiaomi-iot", "ssdp": "", "mdns": ""})
    assert d[0][0] in ("sensor",), d
    ok += 1
    print("[selftest] 指纹打分（电视/打印机/摄像头/IoT）OK")

    # ⑧ 网段闸门：跨网段必须拒
    assert _in_lan("192.168.0.5", "192.168.0.103")
    assert not _in_lan("192.168.1.5", "192.168.0.103")
    assert not _in_lan("8.8.8.8", "192.168.0.103")
    assert not _in_lan("10.0.0.1", "127.0.0.1")
    ok += 1
    print("[selftest] 跨网段闸门（含 127.0.0.1 一律拒）OK")

    # ⑧b 噪声地址过滤：广播/网络地址/链路本地/组播/本机 都不算设备
    assert _is_noise_ip("192.168.0.255", "192.168.0.103")
    assert _is_noise_ip("192.168.0.0", "192.168.0.103")
    assert _is_noise_ip("169.254.83.107", "192.168.0.103")
    assert _is_noise_ip("224.0.0.251", "192.168.0.103")
    assert _is_noise_ip("192.168.0.103", "192.168.0.103")
    assert not _is_noise_ip("192.168.0.105", "192.168.0.103")
    ok += 1
    print("[selftest] 噪声地址过滤（广播/网络/链路本地/组播/本机）OK")

    # ⑧c 随机 MAC 识别（隐私 MAC 查 OUI 必空，不能当厂商缺失）
    assert is_randomized_mac("82:8D:9D:AA:BB:CC")
    assert is_randomized_mac("E2:DE:5C:11:22:33")
    assert not is_randomized_mac("28:6C:07:11:22:33")
    ok += 1
    print("[selftest] 随机/本地管理 MAC 识别 OK")

    # ⑧d 9000 端口绝不单独判成编队节点
    #     （Portainer/海康 SDK 也开 9000；误判 = 把陌生人当自己人放进来）
    r9000 = score_categories({"ports": [9000], "ssdp": "", "mdns": "",
                                         "http": "", "oui": ""})
    assert not any(c == "fleet" for c, _ in r9000), r9000
    ok += 1
    print("[selftest] 9000 端口不会单独判成编队节点 OK")

    # ⑧e 组合特征：海康招牌组合必须定案为摄像头，且同类别只取最具体一条
    hits = combo_hits([9000, 554, 23, 8080])
    cams = [h for h in hits if h[0] == "camera"]
    assert len(cams) == 1, hits                     # 不许同一类别累加两条
    assert cams[0][1] == 12, cams                   # 取最具体那条的权重
    assert combo_hits([8008, 8009])[0][0] == "tv"
    assert combo_hits([445, 139, 3389])[0][0] == "pc"
    assert combo_hits([9100, 631])[0][0] == "printer"
    assert combo_hits([80]) == []                   # 单端口不算组合
    ok += 1
    print("[selftest] 端口组合特征（海康/投屏/Windows/打印机）OK")

    # ⑧f 网关识别：中文与英文两路正则都要能捞出来
    assert parse_gateway("   默认网关. . . . . . . . . . . . . : 192.168.0.16") == "192.168.0.16"
    assert parse_gateway("   Default Gateway . . . . . . . . . : 10.0.0.1") == "10.0.0.1"
    assert parse_gateway("default via 172.16.5.1 dev eth0 proto dhcp") == "172.16.5.1"
    assert parse_gateway("   默认网关. . . . . . . . . . . . . : ") == ""
    assert default_gateway() != ""                  # 本机真有一条默认路由
    ok += 1
    print("[selftest] 网关识别（中/英/ip route + 未知返回空）OK")

    # ⑧g 型号提取：mDNS 实例名里的型号必须抠出来，下划线服务名不能当型号
    m = extract_model(["HIKVISION DS-2CD2T10FD-I3 - E89693295._http._tcp.local",
                       "_psia._tcp.local"])
    assert m.startswith("HIKVISION DS-2CD"), m
    assert extract_model(["_http._tcp.local", "_adb._tcp.local"]) == "", "服务名不是型号"
    m2 = extract_model([], {"server": "TPOS/1.0.0 UPnP/1.0 YLWR1200G易展版/1.0"})
    assert "YLWR1200G" in m2, m2
    ok += 1
    print("[selftest] 型号提取（mDNS 实例名 / SSDP SERVER / 不误取服务名）OK")

    # ⑧h 自报身份压过端口投票：Windows+iVCam 开着 554 也不能判成摄像头
    ev = {"ssdp": "Microsoft-Windows/10.0 UPnP/1.0", "mdns": "e2esoft ivcam _smb",
          "http": "", "oui": "", "ports": [445, 139, 554]}
    ranked2 = dict(score_categories(ev))
    for c, w, _d in combo_hits([445, 139, 554]):
        ranked2[c] = ranked2.get(c, 0) + w
    top = max(ranked2.items(), key=lambda kv: kv[1])[0]
    assert top == "pc", (top, ranked2)
    ok += 1
    print("[selftest] 自报身份压过端口投票（电脑开 554 不判摄像头）OK")

    # ⑨ 端到端：本地起假 SSDP 响应器 + 假 HTTP，断言合成一台设备
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    fake_port = srv.getsockname()[1]

    def ssdp_responder():
        try:
            data, addr = srv.recvfrom(4096)
            if b"M-SEARCH" in data:
                srv.sendto(("HTTP/1.1 200 OK\r\nSERVER: Linux UPnP/1.1 MiniDLNA\r\n"
                            "ST: urn:schemas-upnp-org:device:MediaServer:1\r\n"
                            "USN: uuid:fake::MediaServer\r\n\r\n").encode(), addr)
        except OSError:
            pass

    t = threading.Thread(target=ssdp_responder, daemon=True)
    t.start()
    got = probe_ssdp(timeout=2.0, target=("127.0.0.1", fake_port))
    assert got and got[0]["ip"] == "127.0.0.1", got
    assert "MediaServer" in got[0]["st"], got
    srv.close()
    ok += 1
    print("[selftest] 端到端：假 SSDP 响应器 -> probe_ssdp 收到并解析 OK")

    # ⑩ 真跑一轮发现（不在网络上留东西；无设备也应正常返回结构）
    res = discover(timeout=0.6, do_ports=False, skip_self=True)
    assert isinstance(res, dict) and "devices" in res and "my_ip" in res
    table = format_table(res)
    assert "IP" in table
    ok += 1
    print(f"[selftest] 实网扫描结构正常（本机 {res['my_ip']}，"
          f"{res.get('count', 0)} 台，无异常退出）OK")

    print(f"[selftest] {ok}/17 PASS")


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        _selftest()
        return
    if "--json" in args:
        print(json.dumps(discover(), ensure_ascii=False, indent=2))
        return
    if "--save" in args:
        path = os.environ.get("FLEET_DISCOVERY", "discovered.json")
        r = discover()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
        print(format_table(r))
        print(f"\n已写入 {path}（发现 != 入网，需主号批准）")
        return
    print(format_table(discover()))


if __name__ == "__main__":
    main()

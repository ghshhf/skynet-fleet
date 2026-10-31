"""天网 · 端口映射层：直接跟网关要一个对外开的门。

## 它解决的是打洞解决不了的那一半

打洞靠「双方同时向对方公网地址发」，前提是 NAT 至少给你一个**可预测**的映射。
但碰上对称 NAT，映射端口每次都在变，打洞就成了猜谜。这时候还有一条更土、
也更管用的路：**直接要求网关给一个固定的外部门**。

这就是 UPnP IGD 和 NAT-PMP 干的事。家用路由器九成支持其中至少一个，
开启之后你有的是一个稳定、可预期的公网端口——打洞从「猜」变成「知道」。

## 为什么要自己写，不引库

项目铁律是零第三方依赖。而且这两套协议本身都很小：
  · NAT-PMP（RFC 6886）是个 12 字节 UDP 请求，网关在 5351 端口，
    响应 16 字节。半小时能写对。
  · UPnP IGD 是「SSDP 组播找设备 → 拉 XML 描述 → SOAP 调 AddPortMapping」，
    麻烦在于 XML 解析和一堆厂商差异，但 200 行够用。

## 现实提醒（本机实测）

本机 2026-11-01：默认网关 192.168.137.1 是 Windows ICS（Internet 连接共享），
它不实现 IGD，也不响应 NAT-PMP。所以本机这两个探测都会失败——
**这不是代码问题，是这条路在本机走不通**。换个直连家用路由的环境就能用。

而且即使映射成功，也只能打穿**自己这一层** NAT。本机上面还有运营商 CGNAT，
那层不归你管。所以端口映射是「提高成功率」，不是「保证连得上」。
真正 100% 的兜底只有中继。
"""
import json
import re
import socket
import struct
import subprocess
import sys
import time

IS_WIN = sys.platform.startswith("win")

NATPMP_PORT = 5351
SSDP_ADDR = ("239.255.255.250", 1900)
#: 常用网关地址，无法从路由表拿到时的候选（按常见程度排序）
COMMON_GATEWAYS = ("192.168.1.1", "192.168.0.1", "192.168.31.1",
                   "10.0.0.1", "192.168.137.1", "172.16.0.1")


def default_gateway():
    """取默认网关。优先读系统路由表，读不到再扫常见地址。

    不用「UDP connect 外部地址再读 getsockname」——那个拿到的是出口网卡地址，
    不是网关，两者不是一回事。
    """
    gws = []
    try:
        if IS_WIN:
            r = subprocess.run(["ipconfig"], capture_output=True, text=True,
                               encoding="gbk", errors="replace", timeout=8,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for m in re.finditer(r"(?:Default Gateway|默认网关)[^\d]*((?:\d{1,3}\.){3}\d{1,3})",
                                 r.stdout):
                ip = m.group(1)
                if ip != "0.0.0.0" and ip not in gws:
                    gws.append(ip)
        else:
            r = subprocess.run(["ip", "route"], capture_output=True, text=True, timeout=8)
            for line in r.stdout.splitlines():
                if line.startswith("default"):
                    m = re.search(r"via\s+((?:\d{1,3}\.){3}\d{1,3})", line)
                    if m and m.group(1) not in gws:
                        gws.append(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    return gws


# ============================================================ NAT-PMP (RFC 6886)

NATPMP_OP_EXTERNAL = 0
NATPMP_OP_MAP_UDP = 1
NATPMP_OP_MAP_TCP = 2

NATPMP_RESULT = {
    0: "成功", 1: "不支持该版本", 2: "网关不支持此操作",
    3: "网络故障", 4: "资源不足", 5: "不支持该协议",
}


def natpmp_external(gateway, timeout=1.5):
    """问网关「你觉得我的公网地址是什么」。顺带验证网关是否支持 NAT-PMP。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(struct.pack("!BBHI", 0, NATPMP_OP_EXTERNAL, 0, 0), (gateway, NATPMP_PORT))
        data, _ = s.recvfrom(64)
    except (socket.timeout, OSError):
        return None
    finally:
        s.close()
    if len(data) < 12:
        return None
    ver, op, result, _epoch = struct.unpack("!BBHI", data[:8])
    if result != 0:
        return {"ok": False, "result": result, "err": NATPMP_RESULT.get(result, f"码 {result}")}
    ip = socket.inet_ntoa(data[8:12])
    return {"ok": True, "public": ip, "epoch": _epoch}


def natpmp_map(gateway, internal_port, external_port=None, protocol="udp",
               lifetime=3600, timeout=1.5):
    """要一个端口映射。

    生命周期别设 0——0 是「删除映射」。也别设太大：网关重启后映射就没了，
    客户端需要定期续（脚本里按 lifetime/2 续最稳）。
    """
    op = NATPMP_OP_MAP_UDP if protocol.lower() == "udp" else NATPMP_OP_MAP_TCP
    ext = internal_port if external_port is None else external_port
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        req = struct.pack("!BBHHHI", 0, op, 0, internal_port, ext, lifetime)
        s.sendto(req, (gateway, NATPMP_PORT))
        data, _ = s.recvfrom(64)
    except (socket.timeout, OSError) as e:
        return {"ok": False, "err": f"网关无响应（{e}）——可能不支持 NAT-PMP"}
    finally:
        s.close()
    if len(data) < 16:
        return {"ok": False, "err": "响应过短"}
    ver, op_r, result, _epoch, int_p, ext_p, life = struct.unpack("!BBHIHHI", data[:16])
    if result != 0:
        return {"ok": False, "result": result,
                "err": NATPMP_RESULT.get(result, f"码 {result}")}
    return {"ok": True, "internal_port": int_p, "external_port": ext_p,
            "lifetime": life, "protocol": protocol, "via": "natpmp"}


# ============================================================ UPnP IGD

def _ssdp_search(timeout=2.0):
    """SSDP 组播找 IGD。返回候选描述 URL 列表。

    ST 头用 InternetGatewayDevice:1 和 WANIPConnection:1 两个都发一遍：
    老路由器只认前者，新固件（尤其 OpenWrt/华硕）常常只答后者。
    """
    urls = []
    targets = ("urn:schemas-upnp-org:device:InternetGatewayDevice:1",
               "urn:schemas-upnp-org:service:WANIPConnection:1",
               "urn:schemas-upnp-org:service:WANPPPConnection:1",
               "upnp:rootdevice")
    for st in targets:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.settimeout(0.7)
        try:
            msg = ("M-SEARCH * HTTP/1.1\r\n"
                   "HOST: 239.255.255.250:1900\r\n"
                   'MAN: "ssdp:discover"\r\n'
                   "MX: 1\r\n"
                   f"ST: {st}\r\n\r\n").encode()
            s.sendto(msg, SSDP_ADDR)
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data, addr = s.recvfrom(4096)
                except (socket.timeout, OSError):
                    break
                text = data.decode("utf-8", "replace")
                m = re.search(r"(?im)^location:\s*(\S+)", text)
                if m:
                    u = m.group(1).strip()
                    if u not in urls:
                        urls.append(u)
        except OSError:
            pass
        finally:
            s.close()
        if urls:
            break
    return urls


def _http_get(url, timeout=3.0):
    """极小 HTTP 客户端。只为拉一个 XML，不引 urllib 之外的依赖。"""
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def _find_control_url(xml, base_url):
    """从设备描述 XML 里找 AddPortMapping 的控制地址。

    不引 XML 库：这份文档结构固定，用正则在 serviceType=WAN*Connection 的
    service 块里捞 controlURL 就够。引 xml.etree 也行，但遇到不规范的
    DTD 会抛异常，反而更脆。
    """
    if not xml:
        return None
    # 只在 WAN*Connection 服务块里找，避免拿到 WANCommonInterfaceConfig
    for m in re.finditer(r"<service>(.*?)</service>", xml, re.S | re.I):
        block = m.group(1)
        if not re.search(r"WAN(?:IP|PPP)Connection", block, re.I):
            continue
        c = re.search(r"<controlURL>\s*([^<]+?)\s*</controlURL>", block, re.I)
        if c:
            path = c.group(1).strip()
            if path.startswith("http"):
                return path
            m2 = re.match(r"(https?://[^/]+)", base_url)
            root = m2.group(1) if m2 else base_url
            if path.startswith("/"):
                return root + path
            return root + "/" + path
    return None


def upnp_map(internal_port, external_port=None, protocol="UDP", lifetime=3600,
             local_ip=None, timeout=3.0):
    """UPnP IGD 加端口映射。"""
    import urllib.request
    ext = internal_port if external_port is None else external_port
    urls = _ssdp_search(timeout=timeout)
    if not urls:
        return {"ok": False, "err": "SSDP 没找到网关——路由器可能关了 UPnP"}
    control = None
    desc_url = None
    for u in urls:
        xml = _http_get(u, timeout=timeout)
        c = _find_control_url(xml, u)
        if c:
            control, desc_url = c, u
            break
    if not control:
        return {"ok": False, "err": "找到设备但拿不到 WANConnection 控制地址",
                "urls": urls}
    if not local_ip:
        local_ip = _local_ip_toward(control)
    if not local_ip:
        return {"ok": False, "err": "无法确定本机在与网关同网段的地址"}
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body><u:AddPortMapping "
        'xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">'
        f"<NewRemoteHost></NewRemoteHost>"
        f"<NewExternalPort>{ext}</NewExternalPort>"
        f"<NewProtocol>{protocol}</NewProtocol>"
        f"<NewInternalPort>{internal_port}</NewInternalPort>"
        f"<NewInternalClient>{local_ip}</NewInternalClient>"
        f"<NewEnabled>1</NewEnabled>"
        f"<NewPortMappingDescription>fleet-punch</NewPortMappingDescription>"
        f"<NewLeaseDuration>{lifetime}</NewLeaseDuration>"
        "</u:AddPortMapping></s:Body></s:Envelope>")
    try:
        req = urllib.request.Request(control, data=body.encode(),
                                     headers={"Content-Type": 'text/xml; charset="utf-8"',
                                              "SOAPAction":
                                              '"urn:schemas-upnp-org:service:WANIPConnection:1#AddPortMapping"'},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return {"ok": True, "internal_port": internal_port, "external_port": ext,
                "protocol": protocol, "lifetime": lifetime, "via": "upnp",
                "local_ip": local_ip, "control": control}
    except Exception as e:
        return {"ok": False, "err": f"AddPortMapping 失败（{e}）",
                "control": control, "local_ip": local_ip}


def _local_ip_toward(url):
    """本机在与 URL 同网段的那个地址。"""
    m = re.match(r"https?://([^/:]+)", url or "")
    if not m:
        return None
    host = m.group(1)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        s.connect((host, 9))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


# ============================================================ 统一入口

def try_map(port, protocol="udp", lifetime=3600, verbose=False):
    """按「NAT-PMP → UPnP」顺序尝试。

    为什么 NAT-PMP 先：它只有一个 UDP 往返，几百毫秒就有结论；
    UPnP 要组播发现 + 拉 XML + SOAP，慢一个数量级。能用快的就别用慢的。
    """
    log = []
    gws = default_gateway()
    if not gws:
        gws = list(COMMON_GATEWAYS)
        log.append("路由表没读到网关，改扫常见地址")
    for gw in gws:
        ext = natpmp_external(gw, timeout=1.2)
        if ext and ext.get("ok"):
            log.append(f"{gw} 支持 NAT-PMP，公网地址 {ext['public']}")
            r = natpmp_map(gw, port, protocol=protocol, lifetime=lifetime)
            r["log"] = log
            if r.get("ok"):
                return r
            log.append(f"NAT-PMP 映射失败：{r.get('err')}")
        else:
            log.append(f"{gw} 不响应 NAT-PMP")
    r = upnp_map(port, protocol=protocol.upper(), lifetime=lifetime)
    r["log"] = log
    if verbose:
        r["tried_gateways"] = gws
    return r


# ============================================================ 自测

def _selftest():
    ok = fail = 0

    def chk(c, m):
        nonlocal ok, fail
        if c:
            ok += 1
        else:
            fail += 1
            print(f"  ✗ {m}")

    # NAT-PMP 请求编码
    req = struct.pack("!BBHHHI", 0, NATPMP_OP_MAP_UDP, 0, 8080, 8080, 3600)
    chk(len(req) == 12, "NAT-PMP 请求应为 12 字节")
    chk(req[0] == 0, "版本应为 0")
    chk(req[1] == 1, "UDP 映射 opcode 应为 1")
    chk(struct.unpack("!H", req[4:6])[0] == 8080, "内部端口位置错误")
    chk(struct.unpack("!I", req[8:12])[0] == 3600, "生命周期位置错误")

    # 响应解码（手工造一个成功响应）
    resp = struct.pack("!BBHIHHI", 0, 129, 0, 12345, 8080, 45678, 3600)
    ver, op, result, epoch, ip_, ep_, life = struct.unpack("!BBHIHHI", resp[:16])
    chk(result == 0, "结果码应为 0")
    chk(ip_ == 8080 and ep_ == 45678, "内外端口解析错误")
    chk(life == 3600, "生命周期解析错误")
    chk(op == 129, "响应 opcode 应为请求 opcode + 128")

    # 失败响应要能被识别
    resp_fail = struct.pack("!BBHIHHI", 0, 129, 2, 0, 8080, 0, 0)
    chk(struct.unpack("!H", resp_fail[2:4])[0] == 2, "失败码应能读出")

    # 网关解析（用真实系统，不联网）
    gws = default_gateway()
    chk(isinstance(gws, list), "网关应返回列表")
    chk(all(re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", g or "") for g in gws),
        f"网关格式应为 IPv4: {gws}")

    # XML 控制地址提取
    xml = """<root><device><serviceList><service>
      <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
      <controlURL>/ctl/IPConn</controlURL></service></serviceList></device></root>"""
    c = _find_control_url(xml, "http://192.168.1.1:5000/rootDesc.xml")
    chk(c == "http://192.168.1.1:5000/ctl/IPConn", f"控制地址提取错误: {c}")

    # 拿不到时返回 None，不能瞎猜
    chk(_find_control_url("<root></root>", "http://x/") is None, "空 XML 应返回 None")
    # WANCommonInterfaceConfig 不该被当成端口映射服务
    xml2 = """<service><serviceType>urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1
      </serviceType><controlURL>/bad</controlURL></service>"""
    chk(_find_control_url(xml2, "http://h/") is None, "不该匹配非 WANConnection 服务")

    # 绝对 URL 应原样返回
    xml3 = """<service><serviceType>WANIPConnection:1</serviceType>
      <controlURL>http://1.2.3.4/ctl</controlURL></service>"""
    chk(_find_control_url(xml3, "http://h/") == "http://1.2.3.4/ctl", "绝对 URL 应原样返回")

    print(f"\n自测：{ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    print("默认网关:", default_gateway() or "（未取到）")
    port = 0
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
    print("SSDP 发现的设备:")
    for u in _ssdp_search(timeout=2.0):
        print("  ", u)
    if port:
        print("\n尝试映射 UDP", port)
        print(json.dumps(try_map(port, verbose=True), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

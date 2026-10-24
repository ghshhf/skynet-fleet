"""设备暴露面探测核心（P3）——电脑侧纯标准库，手机侧经桥 App 执行。

为什么要有两个传输层：电脑只能扫「电脑所在的那个网段」；手机连到哪个网，
就应该能探哪个网（商场 WiFi、办公室、楼道——人在走，探测跟着走）。
手机侧 sockets 由编队桥 App 执行（BridgeService 的 PROBE / PROBE_UDP 命令，
2026-10-24 加入），本模块的解析/判定/时间线两边共用一份代码。

红线（与 tools/wall_probe.py 同一条，两边注释互相盯着）：
  · 只做任何合法播放器/扫描器零凭据第一步就会做的事：RTSP OPTIONS（问支持
    哪些方法）、RTSP DESCRIBE 无凭据（不认证就回 SDP；要认证就回 401——
    **401 本身就是数据**：「此设备有墙」是可报告的事实）、HTTP GET 未授权
    公开路径（登录页形态/Server 头/重定向去向）、ONVIF WS-Discovery 组播
    （标准匿名设备发现机制，设计给匿名客户端）。
  · **只读止于 DESCRIBE。不发 SETUP/PLAY、不携带任何凭据、不做任何口令
    尝试。** 刑法 285/286 管未授权访问本身，与目标是否违法无关。
  · 单连接、单请求、限长限时，不做并发轰炸。

判定四态（与电脑侧一致）：
  exposed  SDP 无认证可读（流地址/编码/分辨率/帧率/路数全在）
  walled   DESCRIBE 401/403——唯一缺的输入是「授权」，不是能力
  silent   端口开但协议不回应
  partial  端口开、HTTP 有回应但形态未知

时间线：每次探测追加 ~/.fleet/wallprobe_log.jsonl（带时间戳），
「走过之处的暴露面地图」由此累积；report() 按 IP 汇总Latest一条。
"""
import base64
import json
import os
import re
import socket
import time

LOG_PATH = os.path.join(os.path.expanduser("~"), ".fleet", "wallprobe_log.jsonl")

# ---------------------------------------------------------------- 请求构造

_RTSP_DESCRIBE = ("DESCRIBE rtsp://{ip}:{port}{path} RTSP/1.0\r\n"
                  "CSeq: 1\r\nUser-Agent: wall-probe\r\nAccept: application/sdp\r\n\r\n")
_RTSP_OPTIONS = ("OPTIONS rtsp://{ip}:{port}{path} RTSP/1.0\r\n"
                 "CSeq: 1\r\nUser-Agent: wall-probe\r\n\r\n")
_HTTP_GET = ("GET {path} HTTP/1.1\r\nHost: {ip}\r\n"
             "User-Agent: wall-probe\r\nConnection: close\r\n\r\n")

ONVIF_PROBE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Envelope xmlns="http://www.w3.org/2005/05/soap-envelope" '
    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    '<Header><wsa:MessageID xmlns:wsa="http://www.w3.org/2005/08/addressing">'
    'urn:uuid:wall-probe-1</wsa:MessageID>'
    '<wsa:To xmlns:wsa="http://www.w3.org/2005/08/addressing">'
    'urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>'
    '<wsa:Action xmlns:wsa="http://www.w3.org/2005/08/addressing">'
    'http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe'
    '</wsa:Action></Header>'
    '<Body><Probe xmlns="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
    '<Types>dn:NetworkVideoTransmitter</Types></Probe></Body></Envelope>'
).encode()


# ---------------------------------------------------------------- 传输层

class PCSender:
    """电脑侧传输：本机 socket 直连（探电脑所在网段的设备）。"""

    def __init__(self, timeout=2.0):
        self.timeout = timeout

    def send(self, ip, port, payload, read_ms=2000):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(min(self.timeout, read_ms / 1000.0))
            s.connect((ip, port))
            s.sendall(payload)
            buf = b""
            deadline = time.time() + read_ms / 1000.0
            while len(buf) < 65536:
                left = deadline - time.time()
                if left <= 0:
                    break
                s.settimeout(left)
                try:
                    chunk = s.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                if b"\r\n\r\n" in buf:
                    head, _, body = buf.partition(b"\r\n\r\n")
                    m = re.search(rb"Content-Length:\s*(\d+)", head, re.I)
                    if m and len(body) >= int(m.group(1)):
                        break
            s.close()
            return buf
        except OSError:
            return None

    __call__ = send            # 统一契约：send 本身可调用，另挂 .mcast 供组播

    def mcast(self, payload, wait_ms=2000, local_ip=None):
        """本机组播（ONVIF）。返回 [(ip, bytes), ...]。

        local_ip：多网卡时指定从哪张网卡发出（否则内核挑路由默认的那张，
        在「电脑接调试网、手机接办公网」的机器上会问错网段）。
        """
        out = []
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if local_ip:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                             socket.inet_aton(local_ip))
            s.settimeout(0.3)
            s.sendto(payload, ("239.255.255.250", 3702))
            deadline = time.time() + wait_ms / 1000.0
            while time.time() < deadline and len(out) < 16:
                try:
                    data, addr = s.recvfrom(65535)
                except OSError:
                    continue
                if b"ProbeMatch" in data:
                    out.append((addr[0], data))
            s.close()
        except OSError:
            pass
        return out


class BridgeSender:
    """手机侧传输：探测由编队桥 App 执行（PROBE / PROBE_UDP 命令）。

    手机连到哪个网，桥就探哪个网——电脑到不了的那个网段，
    经 adb 隧道让手机去。命令格式与 android_bridge/BridgeService.java 对齐，
    改一边必须同步改另一边（两边注释互相点名）。
    """

    def __init__(self, serial):
        self.serial = serial

    def _bridge(self, cmd, timeout=8):
        import android_device as A
        return A.bridge_cmd(self.serial, cmd, timeout=timeout)

    def _bridge_bulk(self, cmd, timeout=15):
        import android_device as A
        return A.bridge_bulk(self.serial, cmd, timeout=timeout)

    def send(self, ip, port, payload, read_ms=2000):
        b64 = base64.b64encode(payload).decode("ascii")
        r = self._bridge(f"PROBE {ip} {port} {b64} {int(read_ms)}")
        out = (r.get("out") or "")
        if not r.get("ok") or not out.startswith("OK probe "):
            return None
        parts = out.split(" ", 3)              # OK probe <n> <b64>
        if len(parts) < 4:
            return None
        try:
            return base64.b64decode(parts[3])
        except Exception:
            return None

    def mcast(self, payload, wait_ms=2000):
        b64 = base64.b64encode(payload).decode("ascii")
        r = self._bridge_bulk(f"PROBE_UDP 3702 {b64} {int(wait_ms)}")
        if not r.get("ok"):
            return []
        lines = (r.get("out") or "").splitlines()
        out = []
        for ln in lines:
            if ln.startswith("OK ") or ln.strip() == "END":
                continue
            parts = ln.split(" ", 1)
            if len(parts) == 2:
                try:
                    out.append((parts[0], base64.b64decode(parts[1])))
                except Exception:
                    continue
        return out

    __call__ = send            # 与 PCSender 同一契约（手机侧同用一套解析/判定）


# ---------------------------------------------------------------- 解析与判定

def _split_resp(raw):
    """raw bytes → (status_line, headers_dict, body_text)。

    无响应（None/空）时 status_line 返回 **None** 而非空串——调用方一律用
    `is None` 判「对面没说话」，空串会被 " 200 " in "" 之类静默判成假。
    """
    if not raw:
        return None, {}, ""
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1", "ignore").split("\r\n")
    hdrs = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdrs[k.strip().lower()] = v.strip()
    return (lines[0] if lines else ""), hdrs, body.decode("utf-8", "ignore")


def parse_sdp(body):
    """SDP → 流清单。只抽媒体事实，不做任何解释。"""
    streams = []
    cur = None
    for ln in body.splitlines():
        ln = ln.strip()
        if ln.startswith("m=video"):
            cur = {"control": "", "codec": "", "resolution": "", "fps": ""}
            streams.append(cur)
        elif cur is not None:
            if ln.startswith("a=control:"):
                cur["control"] = ln.split(":", 1)[1].strip()
            elif ln.startswith("a=rtpmap:") and not cur["codec"]:
                parts = ln.split(":", 1)[1].split("/")[0].split()
                cur["codec"] = parts[-1] if parts else ""
            elif ln.startswith("a=framerate:") and not cur["fps"]:
                cur["fps"] = ln.split(":", 1)[1].strip()
            elif ln.startswith("a=framesize:") and not cur["resolution"]:
                # RFC 4570 标准形 a=framesize:<pt> <w>-<h>；少数厂商写成空格分隔
                parts = ln.split()
                if len(parts) >= 3:
                    cur["resolution"] = parts[1] + "x" + parts[2]
                elif len(parts) == 2:
                    cur["resolution"] = parts[1].replace("-", "x")
    return streams


def rtsp_probe(send, ip, port=554, path="/"):
    """OPTIONS + DESCRIBE 两问。返回判定 dict。**止于 DESCRIBE。**"""
    st, hdrs, _ = _split_resp(send(ip, port, _RTSP_OPTIONS.format(ip=ip, port=port,
                                                                  path=path).encode()))
    st2, _, body2 = _split_resp(send(ip, port, _RTSP_DESCRIBE.format(ip=ip, port=port,
                                                                     path=path).encode()))
    verdict = ""
    streams = []
    if st2 is None:
        verdict = "DESCRIBE 无响应"
    elif " 200 " in st2:
        verdict = "无墙（SDP 可拿）"
        streams = parse_sdp(body2)
    elif "401" in st2 or "403" in st2:
        verdict = "认证墙（401/403，无凭据即停）"
    return {"options": st or "(无响应)",
            "public": hdrs.get("public", ""),
            "server": hdrs.get("server", ""),
            "describe": st2 or "(无响应)",
            "describe_verdict": verdict,
            "streams": streams}


def http_probe(send, ip, port, path="/"):
    """未授权 GET 公开路径：登录页形态 / Server 头 / 重定向去向。"""
    st, hdrs, body = _split_resp(send(ip, port, _HTTP_GET.format(ip=ip, path=path).encode()))
    if st is None:
        return {"status": "(无响应)"}
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    return {"status": st, "server": hdrs.get("server", ""),
            "title": (m.group(1).strip()[:80] if m else ""),
            "location": hdrs.get("location", "")}


def onvif_probe(mcast, wait_ms=2000):
    """WS-Discovery 组播 Probe。返回 [(ip, xaddrs, scopes), ...]。"""
    out = []
    for ip, data in mcast(ONVIF_PROBE_XML, wait_ms=wait_ms):
        text = data.decode("utf-8", "ignore")
        xa = re.search(r"<[^>]*XAddrs[^>]*>(.*?)</[^>]*XAddrs>", text, re.S)
        sc = re.search(r"<[^>]*Scopes[^>]*>(.*?)</[^>]*Scopes>", text, re.S)
        out.append({"ip": ip,
                    "xaddrs": (xa.group(1).strip()[:200] if xa else ""),
                    "scopes": (sc.group(1).strip()[:200] if sc else "")})
    return out


def probe_device(send, ip, ports):
    """一台设备的完整暴露面（给定开放端口集合）。"""
    rec = {"ip": ip, "ports": sorted(ports), "probes": {}, "verdict": ""}
    if 554 in ports:
        rec["probes"]["rtsp"] = rtsp_probe(send, ip, 554)
    for p in (80, 8000, 8080, 443, 8554):
        if p in ports:
            rec["probes"]["http_%d" % p] = http_probe(send, ip, p)
    d = rec["probes"].get("rtsp", {})
    streams = d.get("streams") or []
    if streams:
        rec["verdict"] = "exposed：RTSP 元数据无认证可读（%d 路流）" % len(streams)
    elif d.get("describe_verdict", "").startswith("无墙"):
        rec["verdict"] = "exposed：SDP 200 但解析不出流"
    elif d.get("describe_verdict", "").startswith("认证墙"):
        rec["verdict"] = "walled：视频流需要授权（唯一缺的输入）"
    elif not ports:
        rec["verdict"] = "silent：无开放端口"
    else:
        rec["verdict"] = "partial：端口开但协议不回应/形态未知"
    return rec


def probe_targets(send, targets, onvif=True):
    """targets = [{"ip":..., "open_ports":[...]}]。返回探测记录列表。"""
    recs = [probe_device(send, t["ip"], t.get("open_ports") or []) for t in targets]
    if onvif:
        try:
            found = onvif_probe(send.mcast)
            for rec in recs:
                for o in found:
                    if o["ip"] == rec["ip"]:
                        rec["probes"]["onvif"] = o
        except Exception:
            pass                          # 组播失败不影响 TCP 结果
    return recs


# ---------------------------------------------------------------- 编排

DEFAULT_PORTS = (554, 80, 8000, 8080, 443, 8554)


def _split_ips(text):
    return [x.strip() for x in (text or "").replace(";", ",").split(",")
            if x.strip()]


def _split_ports(text, default=DEFAULT_PORTS):
    if not text or not str(text).strip():
        return list(default)
    out = []
    for x in str(text).replace(";", ",").split(","):
        x = x.strip()
        if x.isdigit():
            out.append(int(x))
    return out or list(default)


def local_targets():
    """PC 侧目标发现：本机 ARP/邻居表里的在线设备。

    ARP 表只列「最近通过话的」——没通过话的设备不出现，这是 ARP 的
    物理性质不是缺陷；要全量得先 ping 扫（那是 discovery/sweep 的活，
    这里不重复造）。噪声地址（广播/组播/链路本地/本机）一律滤掉。
    """
    import discovery as D
    import sweep
    mine = set(sweep.local_ips() or [])
    seen, out = set(), []
    for ip, _mac in D.read_neighbors():
        if ip in seen:
            continue
        seen.add(ip)
        if any(D._is_noise_ip(ip, m) for m in (mine or {None})):
            continue
        out.append({"ip": ip})
    return out


def run_pc(targets=None, ports=None, onvif=True, note="", timeout=2.0,
           log=True, report_only=False):
    """PC 侧一键：发现 → 探测 → 落时间线 → 人话汇总。

    report_only=True 时只汇总已有时间线，不发任何包（给「上次探到哪了」用）。
    """
    if report_only:
        return {"ok": True, "mode": "report", "lines": report(), "probed": 0}
    send = PCSender(timeout=timeout)
    tl = targets if targets else local_targets()
    if isinstance(tl, str):
        tl = [{"ip": x} for x in _split_ips(tl)]
    pl = _split_ports(ports)
    recs = probe_targets(send, [{"ip": t["ip"], "open_ports": pl} for t in tl],
                         onvif=onvif)
    if log:
        log_records(recs, note=note)
    return {"ok": True, "mode": "probe", "records": recs,
            "lines": [r["ip"] + "：" + r["verdict"] for r in recs],
            "probed": len(recs)}


def run_phone(serial, targets=None, ports=None, onvif=True, note="",
              timeout=8.0, log=True, report_only=False):
    """手机侧一键：目标来自手机自己的邻居表（ip neigh），探测经桥 App 发出。

    手机连到哪个网，就探哪个网——电脑到不了的那个网段（商场 WiFi、
    办公室、楼道）经 adb 隧道让手机去。**探测由桥 App 执行**，
    需要装带 PROBE/PROBE_UDP 命令的版本（2026-10-24 起）。
    """
    if report_only:
        return {"ok": True, "mode": "report", "lines": report(), "probed": 0}
    send = BridgeSender(serial)
    if targets:
        tl = ([{"ip": x} for x in _split_ips(targets)]
              if isinstance(targets, str) else targets)
        src = "指定"
    else:
        # 手机侧没有 discovery.read_neighbors（那是 PC 的），用邻居表
        import android_device as A
        import hotspot_roam as H
        rc, txt = A.shell(serial, "ip neigh show")
        ips = []
        for row in (H._parse_neigh(txt) if rc == 0 else []):
            if row["ip"] not in ips:
                ips.append(row["ip"])
        tl = [{"ip": x} for x in ips]
        src = "ip-neigh"
    pl = _split_ports(ports)
    recs = probe_targets(send, [{"ip": t["ip"], "open_ports": pl} for t in tl],
                         onvif=onvif)
    if log:
        log_records(recs, note=note)
    return {"ok": True, "mode": "probe", "source": src, "records": recs,
            "lines": [r["ip"] + "：" + r["verdict"] for r in recs],
            "probed": len(recs)}


# ---------------------------------------------------------------- 时间线

def log_records(recs, path=LOG_PATH, note=""):
    """探测结果追加进时间线（append-only，带时间戳）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    now = int(time.time())
    try:
        with open(path, "a", encoding="utf-8") as f:
            for rec in recs:
                row = dict(rec)
                row["at"] = now
                if note:
                    row["note"] = note
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def read_log(path=LOG_PATH, limit=2000):
    out = []
    if not os.path.isfile(path):
        return out
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except ValueError:
                        continue
    except OSError:
        pass
    return out[-limit:]


def report(path=LOG_PATH, limit=2000):
    """时间线 → 人话汇总：每 IP 最新一条 + 计数。"""
    rows = read_log(path, limit)
    latest = {}
    counts = {}
    for r in rows:
        ip = r.get("ip", "?")
        counts[ip] = counts.get(ip, 0) + 1
        latest[ip] = r                      # 追加序即时间序，后者覆盖
    lines = []
    for ip in sorted(latest, key=lambda i: -counts[i]):
        r = latest[ip]
        st = r.get("probes", {}).get("rtsp", {}).get("describe_verdict", "")
        face = ""
        for k, v in (r.get("probes") or {}).items():
            if k.startswith("http_") and v.get("title"):
                face = "「%s」" % v["title"][:30]
                break
        lines.append("%s：%s（探过 %d 次，最近 %s）%s" % (
            ip, r.get("verdict", "?"), counts[ip],
            time.strftime("%m-%d %H:%M", time.localtime(r.get("at", 0))), face))
    return lines


# ---------------------------------------------------------------- 自测

def _selftest():
    def check(name, cond, extra=""):
        tag = "PASS" if cond else "FAIL"
        print(f"  [{tag}] {name} {extra}")
        if not cond:
            raise SystemExit(f"wallprobe selftest failed: {name}")

    print("wallprobe selftest —— 设备暴露面探测核心")

    # ① SDP 解析（真机抓过的 H264DVR 形态）
    sdp = ("v=0\r\no=- 0 0 IN IP4 192.168.0.101\r\ns=H.264\r\n"
           "m=video 0 RTP/AVP 96\r\na=control:trackID=0\r\n"
           "a=rtpmap:96 H264/90000\r\na=framerate:25\r\n"
           "a=framesize:96 1920-1080\r\n"
           "m=video 0 RTP/AVP 98\r\na=control:trackID=1\r\n"
           "a=rtpmap:98 H265/90000\r\n")
    streams = parse_sdp(sdp)
    check("两路流", len(streams) == 2, f"got {len(streams)}")
    check("编码/控制/帧率/分辨率",
          streams[0]["codec"] == "H264" and streams[0]["control"] == "trackID=0"
          and streams[0]["fps"] == "25" and streams[0]["resolution"] == "1920x1080",
          str(streams[0]))
    check("空 body 不崩", parse_sdp("") == [])

    # ② 假传输层：覆盖四态判定
    class FakeSend:
        def __init__(self, table):
            self.table = table        # (ip,port) → raw bytes|None

        def send(self, ip, port, payload, read_ms=2000):
            return self.table.get((ip, port))

        __call__ = send              # 与 PCSender/BridgeSender 同契约

        def mcast(self, payload, wait_ms=2000):
            return self.mcast_ret

    sdp200 = (b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: %d\r\n\r\n" % len(sdp)) + sdp.encode()
    auth401 = b"RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\nWWW-Authenticate: Basic realm=\"x\"\r\n\r\n"
    fs = FakeSend({("1.1.1.1", 554): sdp200, ("2.2.2.2", 554): auth401,
                   ("3.3.3.3", 554): None})
    r1 = probe_device(fs, "1.1.1.1", [554, 80])
    check("200 → exposed", r1["verdict"].startswith("exposed") and len(r1["probes"]["rtsp"]["streams"]) == 2)
    r2 = probe_device(fs, "2.2.2.2", [554])
    check("401 → walled（即停）", r2["verdict"].startswith("walled"), r2["verdict"])
    check("401 记录在案", "401" in r2["probes"]["rtsp"]["describe"])
    r3 = probe_device(fs, "3.3.3.3", [554])
    check("无响应 → 不算 exposed", "无响应" in r3["probes"]["rtsp"]["describe_verdict"])
    r4 = probe_device(fs, "4.4.4.4", [])
    check("无端口 → silent", r4["verdict"].startswith("silent"))

    # ③ HTTP banner
    page = (b"HTTP/1.1 200 OK\r\nServer: thttpd/2.25\r\n\r\n"
            b"<html><title>DVR Web Login</title></html>")
    fs2 = FakeSend({("5.5.5.5", 80): page})
    h = http_probe(fs2, "5.5.5.5", 80)
    check("HTTP 标题+Server", h["title"] == "DVR Web Login" and h["server"] == "thttpd/2.25", str(h))
    redirect = b"HTTP/1.1 302 Found\r\nLocation: /login.html\r\n\r\n"
    fs3 = FakeSend({("6.6.6.6", 80): redirect})
    h3 = http_probe(fs3, "6.6.6.6", 80)
    check("重定向去向", h3["location"] == "/login.html")

    # ④ ONVIF 组播解析
    pm = (b'<?xml version="1.0"?><Envelope><Body><ProbeMatches><ProbeMatch>'
          b'<XAddrs>http://192.168.0.101:8080/onvif/device_service</XAddrs>'
          b'<Scopes>onvif://www.onvif.org/name/TestCam</Scopes>'
          b'</ProbeMatch></ProbeMatches></Body></Envelope>')
    fs4 = FakeSend({})
    fs4.mcast_ret = [("192.168.0.101", pm)]
    o = onvif_probe(fs4.mcast)
    check("ONVIF xaddrs+scopes", o and o[0]["xaddrs"].endswith("device_service")
          and "TestCam" in o[0]["scopes"], str(o))

    # ⑤ PCSender 对本机回活端口真连（127.0.0.1 上起一次性 echo 服务）
    import threading
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    def _serve():
        try:
            c, _ = srv.accept()
            data = c.recv(4096)
            c.sendall(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: 5\r\n\r\nhello")
            c.close()
        except OSError:
            pass
    th = threading.Thread(target=_serve, daemon=True)
    th.start()
    pc = PCSender(timeout=2.0)
    raw = pc.send("127.0.0.1", port, b"OPTIONS rtsp://127.0.0.1 RTSP/1.0\r\n\r\n", read_ms=1500)
    check("PCSender 真连回活", raw is not None and raw.startswith(b"RTSP/1.0 200")
          and raw.endswith(b"hello"), str(raw)[:60])
    check("PCSender 拒闭端口返 None", pc.send("127.0.0.1", 1, b"x") is None)
    th.join(timeout=2)
    srv.close()

    # ⑥ BridgeSender 的命令格式与解码（不打真设备）
    import android_device as A
    orig_cmd, orig_bulk = A.bridge_cmd, A.bridge_bulk
    seen = {}
    def fake_cmd(serial, cmd, timeout=8):
        seen["cmd"] = cmd
        payload_b64 = cmd.split(" ", 3)[3].rsplit(" ", 1)[0]
        payload = base64.b64decode(payload_b64)
        assert payload.startswith(b"DESCRIBE rtsp://"), payload[:30]
        body = sdp.encode()
        out = b"RTSP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body
        return {"ok": True, "out": "OK probe %d %s" % (len(out),
                                                       base64.b64encode(out).decode())}
    def fake_bulk(serial, cmd, timeout=15):
        seen["bulk"] = cmd
        return {"ok": True, "out": "OK mcast\n192.168.0.101 %s\nEND" % base64.b64encode(pm).decode()}
    A.bridge_cmd, A.bridge_bulk = fake_cmd, fake_bulk
    try:
        bs = BridgeSender("fake-serial")
        raw = bs.send("192.168.0.101", 554, _RTSP_DESCRIBE.format(ip="192.168.0.101",
                                                                  port=554, path="/").encode())
        check("BridgeSender 发 DESCRIBE 格式对", "PROBE 192.168.0.101 554 " in seen.get("cmd", ""))
        check("BridgeSender 解回 200+SDP", raw is not None and b"200 OK" in raw
              and b"m=video" in raw)
        st, _, body = _split_resp(raw)
        check("BridgeSender 的流可解析", len(parse_sdp(body)) == 2)
        mm = bs.mcast(ONVIF_PROBE_XML)
        check("BridgeSender 组播解回", len(mm) == 1 and mm[0][0] == "192.168.0.101")
        check("PROBE_UDP 命令格式", "PROBE_UDP 3702 " in seen.get("bulk", ""))
        # 桥报错时降级为 None（调用方按「无响应」处理，不炸）
        A.bridge_cmd = lambda serial, cmd, timeout=8: {"ok": False, "out": "ERR probe ConnectException"}
        check("桥失败 → None（不炸）", bs.send("1.2.3.4", 554, b"x") is None)
    finally:
        A.bridge_cmd, A.bridge_bulk = orig_cmd, orig_bulk

    # ⑥b 编排层：参数切分 + report_only 不发一个包
    check("端口默认集", _split_ports(None) == list(DEFAULT_PORTS)
          and _split_ports("") == list(DEFAULT_PORTS))
    check("端口指定", _split_ports("554,80") == [554, 80]
          and _split_ports("554; 80") == [554, 80])
    check("端口脏值丢掉不回退默认", _split_ports("554,abc") == [554])
    check("IP 切分容错", _split_ips("1.1.1.1, 2.2.2.2;;3.3.3.3")
          == ["1.1.1.1", "2.2.2.2", "3.3.3.3"])
    # report_only 只读时间线：绝不构造 sender、绝不发包（换掉 PCSender 验证）
    _real_pc = PCSender
    try:
        def _boom(**kw):
            raise AssertionError("report_only 不该建 sender")
        globals()["PCSender"] = _boom
        rr = run_pc(report_only=True)
        check("report_only 不发包", rr.get("mode") == "report" and rr.get("probed") == 0)
    finally:
        globals()["PCSender"] = _real_pc

    # ⑦ 时间线：追加 + 汇总（tmp 文件，不碰真 HOME）
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="wallprobe_")
    lp = os.path.join(tmp, "log.jsonl")
    try:
        recs = [r1, r2]
        check("落盘", log_records(recs, lp))
        # 追加轮重复写 r1：验计数（2 次）+ 验「后者覆盖」的时间序语义
        check("二轮追加（append-only）", log_records([r1, r3], lp))
        rows = read_log(lp)
        check("四条都在", len(rows) == 4)
        check("带时间戳", all("at" in r for r in rows))
        rep = report(lp)
        check("汇总每 IP 一条", len(rep) == 3 and any("exposed" in ln for ln in rep), str(rep[:2]))
        check("汇总带探测次数", any("探过 2 次" in ln for ln in rep), str(rep))
        check("空时间线不崩", read_log(os.path.join(tmp, "none.jsonl")) == [])
        with open(lp, "a", encoding="utf-8") as f:
            n = f.write("{bad json}\n")     # write 返字符数，不是 None
        check("坏行跳过", n == len("{bad json}\n") and len(read_log(lp)) == 4,
              f"wrote {n}, read {len(read_log(lp))}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("wallprobe selftest: ALL PASS")
    return True


if __name__ == "__main__":
    _selftest()

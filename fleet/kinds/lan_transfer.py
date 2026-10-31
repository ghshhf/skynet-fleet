"""局域网直传：把 LocalSend v2 协议接成天网的一台设备。

🔴 这个模块为什么存在（2026-11-01）
  天网早就有「传文件」——`push` / `pull` 两条能力。但那两条的前提是**对端已经入网**：
  有节点记录、有 PSK、走控制面或节点通道。同宿舍/同办公室的一台陌生笔记本、
  主人自己手机上装着的 LocalSend App，都进不来 —— 不是能力不够，是**门槛在那**：
  要传一个文件，先得把对方变成节点。

  LocalSend 的 v2 协议解决的正是这件事：**同网段即达，零配置，不需要入网**。
  它只做两件窄事（组播报个到 + HTTP 收个文件），协议简单到可以用纯 stdlib 实现，
  而它的价值恰恰在天网最缺的那一格：不预设对方是我们的设备。

  所以这里不是「再抄一个传输工具」，是**把一条已有的、公开的互操作协议接进注册表**，
  让「她 / 模型 / 终端」能像调 `pc.state` 一样调「把这个文件丢给手机」。

三条纪律（照抄设备层的规矩，一处都不放松）

  1. **发现 != 入网**（`discovery.py` 就立过这条）。`lan.links` 只把对端列进结果，
     不进节点表、不写 devices.json。看得到和能下令是两件事，混了就再也分不清
     「这台是谁」。
  2. **接收必须由人开窗**。发送方随时能发（他按的是自己的键），但**落盘写进本机**
     是另一回事：`lan.recv` 只在主人显式调用后开一个限时窗口，窗口里还带
     目录 / 文件数 / 总字节 / 单文件上限四道闸；窗口一关，53317 立刻关。
     不做「装完就一直在收」那种后台洞 —— 那是把「我允许这一次」偷换成「永远允许」。
  3. **不可用必须给原因**。53317 被占、没网卡、对端拒绝、对方要 PIN 而我们没给 ——
     各回各的话，不许一律糊成「失败」。上层要能据此决定重试还是叫人。

协议对齐的是 LocalSend v2.2（不是我们自己发明的格式），这意味着：
**手机上现成的 LocalSend App 就是合法对端**，不需要在我们这边装任何东西。
细节全部对着 `localsend/packages/core/src/http/{dto_v2.rs,client/v2.rs}` 核过：
  · 组播 224.0.0.167:53317（IPv6 ff12::fd3a:e420），报文 = RegisterDtoV2 + `announce:true`
  · POST /api/localsend/v2/register        互相登记
  · GET  /api/localsend/v2/info            对端名片
  · POST /api/localsend/v2/prepare-upload?pin=  申请（200 带 sessionId + 每文件 token）
  · POST /api/localsend/v2/upload?sessionId=&fileId=&token=   裸字节
  · POST /api/localsend/v2/cancel?sessionId=   撤单
  · 401=要 PIN / PIN 错，403=对方拒绝，409=对方忙，429=太频繁，204=无需传输

诚实边界（写在这里，免得以后有人以为我们比实际强）
  · HTTPS：对端若只开 HTTPS（新版默认自签），客户端侧我们**不校验证书**——
    这是协议本身的设计（首次信任 + 指纹人工核对），不是偷懒；指纹会回写在结果里
    供人核对，但我们不做 TOFU 拦截。**接收侧只支持 HTTP**：要在本机起 TLS 就得
    管一张自签证书，那是另一件事，没做就是没做。
  · 不实现 v1（老版 protocol），只做 v2。遇到 v1-only 的对端如实说不支持。
  · 不做「下载 API」（prepare-download / download）——那是对方把文件挂出来让我们拉，
    我们的场景是推过去，方向不同，先不铺。
  · 组播依赖网卡真的支持 IGMP。某些 USB 网卡 / 虚拟网卡收不到，表现是
    「扫描 0 台」而不是报错 —— 结果里 `notes` 会写清「只听不announce 的情况」。
"""
import calendar
import hashlib
import json
import mimetypes
import os
import socket
import ssl
import struct
import threading
import time
import uuid
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..core import BaseDevice, DeviceResult as R
from .. import reach

#: 协议默认端口。发现与 HTTP 服务共用同一个端口，这是 LocalSend 的设计。
#: 可用 FLEET_LAN_PORT 覆盖 —— 只为一个场景存在：门禁要在被占用的机器上也能跑，
#: 或者本机真跑着 LocalSend 时换个口做实验。正式使用时别改（组播固定发到 53317）。
PORT = int(os.environ.get("FLEET_LAN_PORT") or 53317)
#: 组播端口。**不跟着 FLEET_LAN_PORT 走**：组播端口是全网段的约定，
#: 改它等于「别人喊你听不见、你喊别人也不知道去哪听」，那不是配置是断链。
MCAST_PORT = 53317
#: IPv4 组播组（刻意落在 224.0.0.0/24：部分安卓只在这个段收得到）
MCAST_V4 = "224.0.0.167"
#: IPv6 组播组（LocalSend 在 v2.2 上的扩展）
MCAST_V6 = "ff12::fd3a:e420"
#: 我们对外的协议版本。写 2.2 而不是 2.0：register 里 download 字段是 2.2 才有
PROTO_VERSION = "2.2"
#: 所有 v2 接口前缀
API = "/api/localsend/v2"

#: 一次 announce 之后等多久收回应（秒）。LocalSend 官方是 100/500/2000ms 三连发，
#: 我们听的时间比它长，因为还要等别的设备自己 announce 过来。
DEFAULT_DISCOVER_SECS = 3.0
#: 单次 HTTP 超时。prepare-upload 要等对方用户点「接受」，所以给得比普通请求宽
PREPARE_TIMEOUT = 120.0
#: 上传单块读多少字节（落盘/发送都按这个粒度流式走，绝不整文件进内存）
CHUNK = 256 * 1024
#: 多大的文件才值得算 sha256。再大就是拿 CPU 换一个本来可选的校验
HASH_LIMIT = 128 * 1024 * 1024

#: 身份文件：fingerprint 必须跨重启稳定，否则对方每次都当你是新设备
IDENTITY_PATH = os.path.join(os.path.expanduser("~"), ".fleet", "lan_identity.json")


# --------------------------------------------------------------------- 身份

def _fingerprint():
    """稳定的设备指纹（sha256 hex）。

    为什么不用随机 UUID：LocalSend 里 fingerprint 就是「这台机器」的代号，
    对方按它做收藏 / 黑名单 / 重连。每次重启换一个，等于每次都是陌生人。
    所以落盘一份，第一次由机器名 + 主网卡 MAC + 用户名派生，之后一直读它。
    """
    try:
        with open(IDENTITY_PATH, "r", encoding="utf-8") as f:
            fp = json.load(f).get("fingerprint", "")
        if len(fp) == 64:
            return fp
    except (OSError, ValueError):
        pass

    seed = "%s|%s|%s" % (_machine_name(), _primary_mac(), os.environ.get("USERNAME") or os.environ.get("USER") or "")
    fp = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    try:
        os.makedirs(os.path.dirname(IDENTITY_PATH), exist_ok=True)
        with open(IDENTITY_PATH, "w", encoding="utf-8") as f:
            json.dump({"fingerprint": fp}, f)
    except OSError:
        pass                      # 写不了也不崩：退化成「每次重启换指纹」，结果里会说
    return fp


def _machine_name():
    return (os.environ.get("COMPUTERNAME") or socket.gethostname() or "fleet").strip() or "fleet"


def _primary_mac():
    """取一个稳定的 MAC 当种子。取不到就返回空串（指纹照样能算，只是弱一点）。"""
    try:
        return "%012x" % uuid.getnode()
    except Exception:
        return ""


def device_info(alias=None, port=PORT, protocol="http", download=False):
    """本机名片（RegisterDtoV2 的形状）。发送与响应 register 都用它。"""
    return {
        "alias": alias or (_machine_name() + " (fleet)"),
        "version": PROTO_VERSION,
        "deviceModel": "SkynetFleet",
        "deviceType": "desktop",
        "fingerprint": _fingerprint(),
        "port": int(port),
        "protocol": protocol,
        "download": bool(download),
    }


# ------------------------------------------------------------------ HTTP 底座

def _ssl_context():
    """HTTPS 客户端上下文：**不校验证书**。

    这不是省事，是跟着协议走：LocalSend 的对端用的是自签证书，校验必然失败。
    它的信任模型是「首次连上记指纹，人眼核对」。所以我们照做，并把对方证书的
    指纹回写到结果里，让主人有东西可核 —— 校验不做，但也不是黑箱。
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class _Peer(object):
    """一个对端的最小描述。协议里就这几样，多的不要。"""

    def __init__(self, host, port=PORT, protocol="http", alias="", fingerprint="", model="", dtype=""):
        self.host = host
        self.port = int(port or PORT)
        self.protocol = (protocol or "http").lower()
        self.alias = alias or host
        self.fingerprint = fingerprint or ""
        self.model = model or ""
        self.dtype = dtype or ""

    def base(self):
        return "%s://%s:%d%s" % (self.protocol, self.host, self.port, API)

    def label(self):
        return "%s @ %s:%d" % (self.alias, self.host, self.port)

    def as_dict(self):
        return {"alias": self.alias, "host": self.host, "port": self.port,
                "protocol": self.protocol, "fingerprint": self.fingerprint,
                "deviceModel": self.model, "deviceType": self.dtype}

    @classmethod
    def from_info(cls, host, dto):
        """从对端回的名片（register 响应 / info 响应）构造。"""
        return cls(host=host,
                   port=dto.get("port") or PORT,
                   protocol=dto.get("protocol") or "http",
                   alias=dto.get("alias") or "",
                   fingerprint=dto.get("fingerprint") or "",
                   model=dto.get("deviceModel") or "",
                   dtype=dto.get("deviceType") or "")


def _conn(peer, timeout):
    if peer.protocol == "https":
        return HTTPSConnection(peer.host, peer.port, timeout=timeout, context=_ssl_context())
    return HTTPConnection(peer.host, peer.port, timeout=timeout)


def _request(peer, method, path, body=None, headers=None, timeout=20.0, raw_file=None):
    """发一个请求，返回 (status, body_bytes, resp_headers)。

    body 给 bytes（JSON / 小块）；raw_file 给已打开的文件对象（流式上传）。
    两者互斥 —— 混着用会让 Content-Length 变成猜的。
    """
    conn = _conn(peer, timeout)
    try:
        hdrs = {"Accept": "application/json"}
        hdrs.update(headers or {})
        payload = body
        if raw_file is not None:
            size = os.fstat(raw_file.fileno()).st_size
            hdrs.setdefault("Content-Length", str(size))
            payload = raw_file
        elif body is not None:
            hdrs.setdefault("Content-Length", str(len(body)))
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, data, dict(resp.getheaders())
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _json_or_text(data):
    try:
        return json.loads(data.decode("utf-8", "ignore"))
    except ValueError:
        return data.decode("utf-8", "ignore")[:400]


#: 把 HTTP 状态码翻成人话。天网的纪律是「不可用必须给原因」，这里就是那个原因表
_STATUS_HINT = {
    204: "对方说无需传输（可能只收到文本 / 没有文件被接受）",
    400: "请求体不合法（对方不认我们发的元数据）",
    401: "需要 PIN，或 PIN 不对",
    403: "对方拒绝了这次传输（接收方点了拒绝）",
    409: "对方正忙于另一个会话",
    429: "太快了，对方限流",
    500: "对方内部错误",
}


def _status_reason(status):
    return _STATUS_HINT.get(status, "HTTP %d" % status)


# --------------------------------------------------------------------- 发现

def _parse_announce(data, addr):
    """解析一条组播报文。返回 _Peer 或 None。

    报文 = RegisterDtoV2 + `announce: true`，但也可能收到对端的普通 register 回执。
    只要 JSON 里有 alias/port，就当一台设备 —— 严格模式只会让我们漏掉真实存在的对端。
    """
    try:
        dto = json.loads(data.decode("utf-8", "ignore"))
    except ValueError:
        return None
    if not isinstance(dto, dict) or not dto.get("alias"):
        return None
    host = addr[0]
    # IPv4-mapped 的 IPv6 地址（::ffff:192.168.0.5）要还原，否则拼出来的 URL 打不开
    if host.startswith("::ffff:"):
        host = host[7:]
    return _Peer.from_info(host, dto)


def channel_id(peer):
    """对端没报 fingerprint 时的兜底身份。

    为什么不直接拿 IP 当 id：IP 是**位置**不是身份，手机换个网段就被当成新设备，
    账本里会长出一堆「192.168.1.5」「192.168.1.7」的鬼影。这里从地址派生一个
    稳定串，虽然换 IP 仍会变，但至少格式上与真指纹同类，不会污染上层判断。
    真指纹存在时永远优先用它。
    """
    seed = "%s:%d" % (peer.host, peer.port)
    return "addr-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _is_ip(text):
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(fam, str(text))
            return True
        except OSError:
            continue
    return False


def _resolve_target(host, port=None, protocol=None):
    """把 host（IP / 设备名 / 指纹前缀）翻成一个能发的目标。

    返回 (target, how, 拒绝原因)。

    「把文件发给手机」这句话里没有 IP、没有端口、没有协议 —— 但一次发送三者都要。
    中间那步翻译必须只有一处，否则每个调用方各猜一套，迟早出现
    「这里发得出去、那里发不出去」而没人知道差在哪。
    """
    text = str(host).strip()
    if _is_ip(text):
        return ({"host": text, "port": int(port or PORT),
                 "protocol": (protocol or "http").lower(), "id": ""}, "ip", "")

    dev = reach.find(text)
    if not dev:
        known = [d.get("alias") for d in reach.devices()][:8]
        return (None, "", "可达层里没有叫 %r 的设备。现在看得到：%s"
                          "（先用 lan.links 扫一遍）"
                % (text, "、".join(x for x in known if x) or "无"))
    ch = reach.best_channel(dev)
    if not ch:
        return (None, "", "%s 现在一条通道都不通" % dev.get("alias"))
    kind, addr, cport = reach.split_key(ch)
    if kind != "lan":
        # 别的通道（ble / relay …）不是这条能力的事。如实说清，别硬发。
        return (None, "", "%s 只登记了 %s 通道，传文件要走 lan 通道"
                % (dev.get("alias"), kind))
    proto = (protocol or (dev.get("meta") or {}).get("protocol") or "http").lower()
    return ({"host": addr, "port": int(port or cport or PORT),
             "protocol": proto, "id": dev["id"]}, "reach/" + ch, "")


class _ProbeHandler(BaseHTTPRequestHandler):
    """扫描期间临时开的收件口：只做一件事 —— 收下别人回的 register。

    🔴 为什么必须有它（2026-11-01 实测抓出来的）
      LocalSend 的发现不是「大家一直在广播、听到就算」。真实链路是：
        我发一条组播 announce → 对端收到 → 对端**用 HTTP 往我的 IP:port 发 register**
      所以只听 UDP 的话，能听到的只有「正好也在 announce 的设备」；
      那些安安静静开着、等着被叫的设备，发完 register 就石沉大海 ——
      因为我们那会儿根本没在监听 TCP。表现就是「明明都开着 App，扫出来 0 台」。
      UDP 53317 与 TCP 53317 是不同协议，可以同时绑，所以扫描期间临时开这个口。
    """
    peers = None
    alias = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        u = urlparse(self.path)
        if not u.path.endswith("/register"):
            self._json(404, {"message": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            dto = json.loads(raw.decode("utf-8", "ignore"))
        except ValueError:
            self._json(400, {"message": "bad json"})
            return
        if isinstance(dto, dict) and self.peers is not None:
            # 这条回执就是「它确实在」的证据 —— 和组播听到的同等效力
            self.peers.append(_Peer.from_info(self.client_address[0], dto))
        self._json(200, device_info(alias=self.alias))


def discover(secs=DEFAULT_DISCOVER_SECS, alias=None, include_self=False):
    """组播扫描同网段的对端，返回 (列表, 说明列表)。

    两条路一起走，因为单靠一条都会漏：
      ① **主动 announce** —— 我们喊一声，别人收到后各自回（有人回 HTTP register，
         也有人直接在组播上再喊一声）；
      ② **被动听** —— 别人喊的时候我们正好在听。局域网里设备启动时间不一样，
         只做 ① 就会漏掉那些「刚开机还没轮到它喊」的。

    🔴 **一张网卡一个 socket，不许只绑 0.0.0.0**（2026-11-01 修）
      「有连接就有网」的前提是承认**连接可以有好几条**：有线在 192.168.1.x、
      手机热点在 192.168.137.x、USB 网络共享又是另一个段。只 bind 一个 0.0.0.0
      的 socket，内核只会把它挂到**一张**默认网卡上 —— 另外几个网段上的设备
      一个都听不到，表现是「明明连着却扫出 0 台」。
      这正是本仓 `discovery.py` 记过的老坑（「多网卡机器不显式绑网卡发组播，
      一个响应都收不到」），LocalSend 的做法也是每个接口一个 SendSocket。
      所以这里对**每张网卡**单独 bind + 单独 join，各自发、各自收。
    """
    notes = []
    found = {}

    ifaces = _local_ipv4s()
    if not ifaces:
        return None, ["本机没有非回环 IPv4（网卡全断？），组播无从下手"]

    # 扫描期间临时开 TCP 收件口，接别人回的 register（见 _ProbeHandler 的注释）。
    # 端口被占（本机有 LocalSend / 接收窗口开着）就只走组播，如实记一笔。
    replies = []
    probe = None
    # 每次扫描用自己的一份 handler 子类，而不是改共享的 _ProbeHandler。
    # 两个 discover 并行时改类属性会互相覆盖（A 的收件口把包记进 B 的列表），
    # 那种 bug 表现为「偶尔多出一台不存在的设备」，最难查。
    # 端口仍然只有一个人能占，第二个 bind 会失败 —— 但那会如实写进 notes。
    handler = type("_Probe", (_ProbeHandler,), {"peers": replies,
                                                "alias": alias or (_machine_name() + " (fleet)")})
    try:
        probe = ThreadingHTTPServer(("0.0.0.0", PORT), handler)
        probe.daemon_threads = True
        threading.Thread(target=probe.serve_forever, kwargs={"poll_interval": 0.2},
                         name="lan-probe", daemon=True).start()
    except OSError as e:
        probe = None
        notes.append("TCP %d 被占（%s）——这一轮只能听到主动广播的设备，"
                     "听不到「在线但等你叫」的那些" % (PORT, e))

    socks = []
    for ip in ifaces:
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:            # 让同机的 LocalSend 也能绑同一个口（Linux/macOS 有效）
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            s.bind((ip, MCAST_PORT))
            # 关键：join 时**必须带上网卡地址**，否则内核按默认路由挑一张网卡，
            # 我们以为在看全部，其实只看了其中一张。
            mreq = struct.pack("4s4s", socket.inet_aton(MCAST_V4), socket.inet_aton(ip))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            s.settimeout(0.12)
            socks.append((ip, s))
        except OSError as e:
            if s is not None:
                s.close()
            notes.append("网卡 %s 起不了组播（%s）" % (ip, e))

    if not socks:
        # 全部失败 = 端口被占（本机有 LocalSend / 我们的接收窗口开着）或没权限。
        # 这不是故障而是「此刻没法用组播」，如实说，别让人以为网里没设备。
        return None, notes or ["没有一张网卡能起组播监听"]

    payload = json.dumps(dict(device_info(alias=alias), announce=True)).encode("utf-8")
    sent_any = False
    for ip, s in socks:
        try:
            s.sendto(payload, (MCAST_V4, MCAST_PORT))
            sent_any = True
        except OSError as e:
            notes.append("网卡 %s 发送失败（%s）" % (ip, e))
    if not sent_any:
        notes.append("一声都没喊出去，只能被动等别人 announce")

    deadline = time.time() + max(0.5, float(secs))
    try:
        while time.time() < deadline:
            for ip, s in socks:
                try:
                    data, addr = s.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    # Windows 上上一次发包的 ICMP 错误会在下一次 recv 冒出来；
                    # 一条错误不该让整轮扫描中断
                    continue
                peer = _parse_announce(data, addr)
                if peer is None:
                    continue
                if not include_self and peer.fingerprint and peer.fingerprint == _fingerprint():
                    continue
                found.setdefault(peer.host, peer)
    finally:
        for _, s in socks:
            try:
                s.close()
            except OSError:
                pass
        if probe is not None:
            try:
                probe.shutdown()
                probe.server_close()
            except Exception:
                pass

    # 把「回我 register 的」并进来：它们和组播听到的是同等效力的证据，
    # 只是走的路不同（组播是它在喊，register 是它应我的喊）
    for p in replies:
        if not include_self and p.fingerprint and p.fingerprint == _fingerprint():
            continue
        found.setdefault(p.host, p)

    if not found:
        notes.append("这一轮没听到任何 announce（扫了 %d 张网卡）。常见原因："
                     "对方没装 LocalSend、不在同一网段、或网卡不收 IGMP 组播"
                     "（虚拟网卡 / 部分 USB 网卡）" % len(socks))
    return list(found.values()), notes


def probe_peer(host, port=PORT, protocol="http", timeout=8.0):
    """问一个指定对端要名片（GET /info）。返回 (peer, None) 或 (None, 原因)。"""
    peer = _Peer(host, port, protocol)
    try:
        status, data, _ = _request(peer, "GET", API + "/info", timeout=timeout)
    except (OSError, ssl.SSLError) as e:
        # 用 http 打不通时，常见情况是对端只开了 https。反向再试一次，
        # 但**把试出来的结果写进 notes**，不假装一开始就知道
        if protocol == "http":
            peer.protocol = "https"
            try:
                status, data, _ = _request(peer, "GET", API + "/info", timeout=timeout)
            except (OSError, ssl.SSLError) as e2:
                return None, "连不上 %s:%d（http: %s；https: %s）" % (host, port, e, e2)
        else:
            return None, "连不上 %s:%d（%s）" % (host, port, e)

    if status != 200:
        return None, "%s:%d 回 %d：%s" % (host, port, status, _status_reason(status))
    dto = _json_or_text(data)
    if not isinstance(dto, dict):
        return None, "%s:%d 的 /info 不是 JSON（对端可能不是 LocalSend）" % (host, port)
    return _Peer.from_info(host, dto), None


# --------------------------------------------------------------------- 发送

def _file_dto(path, fid, with_hash=True):
    """按 FileDto 组织一个文件。大小/类型/时间都取真实值，不猜。"""
    st = os.stat(path)
    name = os.path.basename(path)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    dto = {"id": fid, "fileName": name, "size": st.st_size, "fileType": mime}
    if with_hash and st.st_size <= HASH_LIMIT:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                h.update(chunk)
        dto["sha256"] = h.hexdigest()
    # 时间戳是有用的：对方用它还原 mtime，否则手机上所有文件都变成"现在是修改时间"
    dto["metadata"] = {"modified": _rfc3339(st.st_mtime)}
    return dto


def _rfc3339(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def send(files, host, port=PORT, protocol="http", pin=None, alias=None, timeout=None):
    """把一批本地文件推给对端。返回一份如实的结果 dict（不抛异常）。

    流程严格按协议：prepare-upload（等对方人点接受）→ 拿到每个文件的 token →
    逐个 upload。**部分接受是正常的**：对方可能只收其中两个，我们就只传那两个，
    剩下的一并写进结果里，绝不假装全传了。
    """
    paths = [p for p in files if p]
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        return {"ok": False, "code": "error", "err": "这些路径不是文件：%s" % ", ".join(missing)}
    if not paths:
        return {"ok": False, "code": "error", "err": "没有要发的文件"}

    peer = _Peer(host, port, protocol)
    files_dto, id_map = {}, {}
    for p in paths:
        fid = str(len(id_map) + 1)
        files_dto[fid] = _file_dto(p, fid)
        id_map[fid] = p

    body = json.dumps({"info": device_info(alias=alias, port=PORT),
                       "files": files_dto}).encode("utf-8")
    path = API + "/prepare-upload" + (("?pin=" + str(pin)) if pin else "")
    t0 = time.time()
    try:
        status, data, _ = _request(peer, "POST", path, body=body,
                                   headers={"Content-Type": "application/json"},
                                   timeout=timeout or PREPARE_TIMEOUT)
    except (OSError, ssl.SSLError) as e:
        return {"ok": False, "code": "unavailable",
                "err": "连不上 %s:%d（%s）——检查是否同网段、端口是否放行" % (host, port, e)}

    if status == 204:
        return {"ok": True, "code": "ok", "sent": [], "skipped": list(id_map.values()),
                "err": "", "note": _status_reason(204), "elapsed": round(time.time() - t0, 1)}
    if status != 200:
        code = {401: "denied", 403: "denied", 409: "unavailable", 429: "unavailable"}.get(status, "error")
        extra = ""
        if status == 401:
            extra = "（这次要带 pin 参数：本机屏幕上显示的那个 4~6 位数）"
        return {"ok": False, "code": code,
                "err": "对方回 %d：%s%s" % (status, _status_reason(status), extra),
                "detail": _json_or_text(data)}

    resp = _json_or_text(data)
    if not isinstance(resp, dict) or not resp.get("sessionId"):
        return {"ok": False, "code": "error",
                "err": "prepare-upload 的响应里没有 sessionId", "detail": resp}
    session_id = resp["sessionId"]
    tokens = resp.get("files") or {}

    sent, failed = [], []
    for fid, token in tokens.items():
        local = id_map.get(fid)
        if not local:
            continue
        ok, err = _upload_one(peer, session_id, fid, token, local, timeout=timeout)
        (sent if ok else failed).append({"path": local, "err": err})

    rejected = [id_map[f] for f in id_map if f not in tokens]
    if failed:
        try:                          # 有失败就撤单，别让对端卡在一个不会完成的会话里
            _request(peer, "POST", API + "/cancel?sessionId=" + session_id, timeout=8)
        except (OSError, ssl.SSLError):
            pass

    return {"ok": not failed, "code": "ok" if not failed else "error",
            "session": session_id, "sent": sent, "failed": failed,
            "rejected": rejected, "peer": peer.as_dict(),
            "elapsed": round(time.time() - t0, 1),
            "err": "" if not failed else "%d 个文件没传成功" % len(failed)}


def _upload_one(peer, session_id, fid, token, local, timeout=None):
    """传单个文件。成功返回 (True, "")，失败返回 (False, 原因)。"""
    q = "?sessionId=%s&fileId=%s&token=%s" % (session_id, fid, token)
    name = os.path.basename(local)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    try:
        with open(local, "rb") as f:
            status, data, _ = _request(
                peer, "POST", API + "/upload" + q, raw_file=f,
                headers={"Content-Type": ctype},
                timeout=timeout or max(60.0, os.path.getsize(local) / (512 * 1024)))
    except (OSError, ssl.SSLError) as e:
        return False, "上传中断：%s" % e
    if status not in (200, 204):
        return False, "对方回 %d：%s" % (status, _status_reason(status))
    return True, ""


# --------------------------------------------------------------------- 接收

class _ReceiveWindow(object):
    """一次接收窗口：限时、限目录、限数量、限字节。

    为什么接收要这么多闸：**发送方按他那边的一个键，就能往本机写文件**。
    这条链路上唯一替主人把关的就是这里。四道闸全部是硬上限，到点就拒，
    不做「差不多就放」。
    """

    def __init__(self, dest_dir, secs, max_files, max_bytes, max_file_bytes,
                 alias=None, pin=None):
        self.dest_dir = os.path.abspath(dest_dir)
        self.deadline = time.time() + float(secs)
        self.max_files = int(max_files)
        self.max_bytes = int(max_bytes)
        self.max_file_bytes = int(max_file_bytes)
        self.alias = alias or (_machine_name() + " (fleet)")
        self.pin = (str(pin) if pin else None)
        self.received = []
        self.refused = []             # 被拒的文件（超限 / 一个都不收）
        self.seen = []                # 敲过 /register 的对端，只做证据，不入网
        self.sessions = {}            # sessionId -> {fileId: {name, size, path, token}}
        self.lock = threading.Lock()
        self.httpd = None
        self.thread = None
        self.started_at = time.time()

    # ---- 生命周期

    def open(self):
        """起 HTTP 服务。返回 (port, None) 或 (None, 原因)。"""
        os.makedirs(self.dest_dir, exist_ok=True)
        window = self

        class Handler(_LanHandler):
            pass

        Handler.window = window
        try:
            self.httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        except OSError as e:
            return None, ("端口 %d 起不来（%s）——本机可能已有 LocalSend 在跑，"
                          "或另一个接收窗口开着；一次只能有一个人占用这个端口" % (PORT, e))
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.3},
                                       name="lan-recv", daemon=True)
        self.thread.start()
        threading.Thread(target=self._reaper, name="lan-recv-reaper", daemon=True).start()
        return PORT, None

    def close(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
        self.httpd = None

    def alive(self):
        return self.httpd is not None and time.time() < self.deadline

    def _reaper(self):
        """到点自己关门。窗口不是「开着等主人想起来关」——它必须有寿命。"""
        while time.time() < self.deadline:
            time.sleep(0.5)
        self.close()

    def summary(self, note=""):
        return {"ok": True, "code": "ok",
                "listening": PORT, "dir": self.dest_dir,
                "remaining": max(0, int(self.deadline - time.time())),
                "received": list(self.received), "refused": list(self.refused),
                "seen": list(self.seen),
                "note": note, "err": ""}

    # ---- 准入

    def _committed(self):
        """已经占掉的配额 = 收完的 + 答应了但还没传完的。

        🔴 为什么必须把 pending 也算进去（门禁 T51 抓出来的真 bug）：
        一开始这里只数 `len(received)`，而 `received` 只在**传完之后**追加。
        prepare-upload 是**一次请求带全部文件**，那一刻一个都还没落盘，
        于是三个文件全过 —— 上限形同虚设，32 个的闸能放进几百个。
        配额是要**先占后用**的：答应了就等于占了。
        """
        n = len(self.received)
        b = sum(int(r.get("size") or 0) for r in self.received)
        for sess in self.sessions.values():
            for e in sess.values():
                if e["status"] in ("pending", "uploading"):
                    n += 1
                    b += int(e["dto"].get("size") or 0)
        return n, b

    def _budget_ok(self, name, size, extra_n=0, extra_b=0):
        with self.lock:
            n, used = self._committed()
            if n + extra_n >= self.max_files:
                return False, "已达文件数上限 %d" % self.max_files
            if size > self.max_file_bytes:
                return False, "单文件 %d 字节超过上限 %d" % (size, self.max_file_bytes)
            if used + extra_b + size > self.max_bytes:
                return False, "总字节会超过上限 %d" % self.max_bytes
        return True, ""

    def _safe_path(self, name):
        """文件名只取 basename，绝不允许 `../` 或绝对路径逃出接收目录。

        这是这一层最容易出事的地方：协议里 fileName 是**对端给的**，当数据看，
        不能当路径用。重名加序号，不覆盖已有文件。
        """
        base = os.path.basename((name or "").replace("\\", "/")).strip() or "unnamed"
        base = base.replace("\x00", "")
        target = os.path.join(self.dest_dir, base)
        stem, ext = os.path.splitext(target)
        i = 1
        while os.path.exists(target):
            target = "%s (%d)%s" % (stem, i, ext)
            i += 1
        return target


class _LanHandler(BaseHTTPRequestHandler):
    """LocalSend v2 的服务端。只实现我们需要的四个接口。

    `window` 由 _ReceiveWindow 注入（类属性），所以这里不写全局变量 ——
    一个进程里只该有一个窗口，窗口自己管自己的状态。
    """
    window = None
    protocol_version = "HTTP/1.1"
    server_version = "FleetLan/1.0"

    # 默认实现会往 stderr 打每条请求；天网有后台不许弹窗/刷屏的纪律
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, code, obj):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _err(self, code, msg):
        self._send_json(code, {"message": msg})

    def do_GET(self):
        u = urlparse(self.path)
        if self.window is None:
            self._err(503, "no window")
            return
        if u.path in (API + "/info", "/api/localsend/v1/info"):
            self._send_json(200, device_info(alias=self.window.alias))
            return
        self._err(404, "not found")

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        w = self.window
        if w is None:
            self._err(503, "no window")
            return
        if time.time() >= w.deadline:
            self._err(503, "接收窗口已关闭")
            return

        if u.path == API + "/register":
            self._handle_register()
        elif u.path in (API + "/prepare-upload", API + "/prepare_upload"):
            self._handle_prepare(q)
        elif u.path == API + "/upload":
            self._handle_upload(q)
        elif u.path == API + "/cancel":
            sid = (q.get("sessionId") or [""])[0]
            with w.lock:
                w.sessions.pop(sid, None)
            self._send_json(200, {})
        else:
            self._err(404, "not found")

    # ---- 各接口

    def _handle_register(self):
        raw = self._body()
        try:
            dto = json.loads(raw.decode("utf-8", "ignore"))
        except ValueError:
            self._err(400, "bad json")
            return
        # 对端登记**只进 seen**，不进节点表 —— 「发现 != 入网」。
        # 这条线是全网段谁都能敲的门，把敲过门的都写进 devices.json，
        # 等于让陌生人改了我们「谁是自己人」的账本。
        with self.window.lock:
            self.window.seen.append({"alias": dto.get("alias") or "",
                                     "host": self.client_address[0],
                                     "at": int(time.time())})
        self._send_json(200, device_info(alias=self.window.alias))

    def _handle_prepare(self, q):
        w = self.window
        if w.pin:
            got = (q.get("pin") or [""])[0]
            if got != w.pin:
                self._err(401, "PIN required" if not got else "Wrong PIN")
                return
        raw = self._body()
        try:
            payload = json.loads(raw.decode("utf-8", "ignore"))
        except ValueError:
            self._err(400, "bad json")
            return

        files = payload.get("files") or {}
        sender = (payload.get("info") or {}).get("alias") or self.client_address[0]
        session_id = str(uuid.uuid4())
        accepted = {}
        rejected = []
        acc_n = acc_b = 0          # 本次请求里已经答应掉的配额（同一单多个文件要累加）
        for fid, dto in files.items():
            size = int(dto.get("size") or 0)
            ok, why = w._budget_ok(dto.get("fileName"), size, acc_n, acc_b)
            if not ok:
                rejected.append({"file": dto.get("fileName"), "why": why})
                continue
            accepted[fid] = {"dto": dto, "token": str(uuid.uuid4()), "sender": sender,
                             "status": "pending"}
            acc_n += 1
            acc_b += size
        if not accepted:
            with w.lock:
                w.refused.extend(rejected)
            # 一个都不收 = 拒绝。403 让发送方知道是人拒的，而不是网络坏了
            self._err(403, "全部被拒：" + "；".join("%s %s" % (r["file"], r["why"]) for r in rejected))
            return

        with w.lock:
            w.sessions[session_id] = accepted
            w.refused.extend(rejected)
        self._send_json(200, {"sessionId": session_id,
                              "files": {fid: f["token"] for fid, f in accepted.items()}})

    def _handle_upload(self, q):
        w = self.window
        sid = (q.get("sessionId") or [""])[0]
        fid = (q.get("fileId") or [""])[0]
        token = (q.get("token") or [""])[0]
        with w.lock:
            sess = w.sessions.get(sid)
            entry = sess.get(fid) if sess else None
        if not entry or entry["token"] != token:
            self._err(403, "invalid token")
            return
        if entry["status"] != "pending":
            self._err(409, "already uploaded")
            return
        entry["status"] = "uploading"

        size = int(entry["dto"].get("size") or 0)
        target = w._safe_path(entry["dto"].get("fileName"))
        want_hash = (entry["dto"].get("sha256") or "").lower()
        h = hashlib.sha256() if want_hash else None
        got = 0
        try:
            length = int(self.headers.get("Content-Length") or 0)
            remain = length if length > 0 else None
            with open(target, "wb") as out:
                while True:
                    if remain is None:
                        chunk = self.rfile.read(CHUNK)
                    else:
                        if remain <= 0:
                            break
                        chunk = self.rfile.read(min(CHUNK, remain))
                        remain -= len(chunk)
                    if not chunk:
                        break
                    out.write(chunk)
                    got += len(chunk)
                    if h:
                        h.update(chunk)
        except (OSError, ValueError) as e:
            entry["status"] = "failed"
            try:
                os.remove(target)
            except OSError:
                pass
            self._err(500, "落盘失败：%s" % e)
            return

        # 大小对不上就是没传完，删掉 —— 半个文件比没有更坏（看着像收到了）
        if size and got != size:
            entry["status"] = "failed"
            try:
                os.remove(target)
            except OSError:
                pass
            self._err(400, "字节数不符：声明 %d，实收 %d" % (size, got))
            return
        if h and h.hexdigest() != want_hash:
            entry["status"] = "failed"
            try:
                os.remove(target)
            except OSError:
                pass
            self._err(400, "sha256 不符，已删除")
            return

        # 协议允许对方带 mtime，用它还原时间；设不上不算失败。
        # 必须用 calendar.timegm 而不是 mktime：字符串是 UTC，mktime 按**本地时区**
        # 解释，东八区会整体差 8 小时（还随夏令时漂），文件时间就全错了。
        mod = ((entry["dto"].get("metadata") or {}).get("modified") or "")
        if mod:
            try:
                ts = calendar.timegm(time.strptime(mod, "%Y-%m-%dT%H:%M:%SZ"))
                os.utime(target, (ts, ts))
            except (ValueError, OSError):
                pass

        entry["status"] = "done"
        with w.lock:
            w.received.append({"name": os.path.basename(target), "path": target,
                               "size": got, "from": entry["sender"]})
        self._send_json(200, {})

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        return self.rfile.read(n) if n > 0 else b""


# ------------------------------------------------------------------- 设备

class LanTransfer(BaseDevice):
    """局域网直传（LocalSend v2）。一台「只在这张网里存在」的设备。"""

    kind = "lan_transfer"
    via = "lan"

    def __init__(self, name="lan:transfer", **kw):
        super().__init__(name, via="lan", **kw)
        self.meta.setdefault("note", "同网段零配置直传，走 LocalSend v2 协议；"
                                     "接收端必须由人开窗，窗口有寿命")
        self._window = None           # 当前接收窗口（同时最多一个）

    def probe(self):
        """能不能干活 = 本机有没有一块真实的网卡。

        刻意**不在 probe 里 bind 53317**：probe 会被注册表周期性调用，
        每次去抢端口会让「看一眼状态栏」变成「占一次端口」。
        """
        try:
            nets = _local_ipv4s()
        except OSError as e:
            return False, "枚举网卡失败: %s" % e
        if not nets:
            return False, "本机没有可用的局域网地址（只有回环），传不了"
        return True, ""

    def caps(self):
        return ("lan.links", "lan.probe", "lan.send", "lan.recv", "lan.recv.stop")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "lan.links":
            peer_list, notes = discover(secs=args.get("secs") or DEFAULT_DISCOVER_SECS,
                                        alias=args.get("alias"))
            if peer_list is None:
                return R.unavailable("lan.links", notes[0], device=self.name, via=self.via)
            # 发现即入网：听到一台就把它登记进可达层。
            # 注意只是**进外圈**（能传文件、能被看见），不是进编队 ——
            # 这一步之后它仍然没有发令权、不能执行命令。
            for p in peer_list:
                reach.note(p.fingerprint or channel_id(p),
                           alias=p.alias,
                           channel=reach.channel_key("lan", p.host, p.port),
                           meta={"protocol": p.protocol, "model": p.model,
                                 "dtype": p.dtype, "via": "multicast"})
            return R.success("lan.links", self.name, via=self.via, data={
                "count": len(peer_list),
                "peers": [p.as_dict() for p in peer_list],
                "self": device_info(alias=args.get("alias")),
                "notes": notes})

        if cap == "lan.probe":
            host = args.get("host")
            if not host:
                return R.error("lan.probe", "缺少 host 参数", device=self.name, via=self.via)
            peer, why = probe_peer(host, int(args.get("port") or PORT),
                                   (args.get("protocol") or "http").lower())
            if peer is None:
                # 这一次没打通：记一笔「这条通道现在不通」，但**设备不删**。
                # 锁屏 30 秒不该等于「从网里消失」。
                known = reach.find(host)
                if known:
                    reach.mark_failed(known["id"], reach.channel_key("lan", host, args.get("port") or PORT), why)
                return R.unavailable("lan.probe", why, device=self.name, via=self.via)
            reach.note(peer.fingerprint or channel_id(peer), alias=peer.alias,
                       channel=reach.channel_key("lan", peer.host, peer.port),
                       meta={"protocol": peer.protocol, "model": peer.model,
                             "dtype": peer.dtype, "via": "probe"})
            return R.success("lan.probe", self.name, via=self.via, data={"peer": peer.as_dict()})

        if cap == "lan.send":
            host = args.get("host")
            paths = args.get("paths") or args.get("path")
            if isinstance(paths, str):
                paths = [paths]
            if not host:
                return R.error("lan.send", "缺少 host 参数（用 lan.links 先看有谁）",
                               device=self.name, via=self.via)
            if not paths:
                return R.error("lan.send", "缺少 paths 参数（要发的本地文件）",
                               device=self.name, via=self.via)

            # host 可以是 IP，也可以是**名字**（"手机" / "客厅那台" / 指纹前缀）。
            # 人记名字不记指纹，这层翻译只在这里做一次，别让每个调用方各写一遍。
            target, how, deny = _resolve_target(host, args.get("port"), args.get("protocol"))
            if deny:
                return R.error("lan.send", deny, device=self.name, via=self.via)

            out = send(paths, target["host"], target["port"], target["protocol"],
                       pin=args.get("pin"), alias=args.get("alias"))
            # 这一趟是真是假，用通道状态记下来 —— 通了就是可用，断了就标不可达。
            # 只有这一处会写下「读到的当前事实」，别的地方（发现）只算"存在证据"。
            if target.get("id"):
                ck = reach.channel_key("lan", target["host"], target["port"])
                if out.get("ok"):
                    reach.note(target["id"], channel=ck, meta={"via": "send"})
                else:
                    reach.mark_failed(target["id"], ck, out.get("err") or "发送失败")

            if out.get("ok"):
                out["addressed_as"] = how
                return R.success("lan.send", self.name, via=self.via, data=out)
            code = out.get("code") or "error"
            if code == "denied":
                return R.denied("lan.send", out.get("err") or "对方拒绝",
                                device=self.name, via=self.via)
            if code == "unavailable":
                return R.unavailable("lan.send", out.get("err") or "连不上",
                                     device=self.name, via=self.via)
            # 失败也要把「哪些成了、哪些被拒」带回去：部分成功的场景里，
            # 光一句「发送失败」会让人以为一个都没过去，于是重发一遍，
            # 结果是对方收到两份。
            res = R.error("lan.send", out.get("err") or "发送失败",
                          device=self.name, via=self.via)
            res.data = out
            return res

        if cap == "lan.recv":
            if self._window and self._window.alive():
                return R.error("lan.recv",
                               "已经有一个接收窗口开着（还剩 %d 秒），先 lan.recv.stop"
                               % int(self._window.deadline - time.time()),
                               device=self.name, via=self.via)
            dest = args.get("dir") or os.path.join(os.path.expanduser("~"), "Downloads", "fleet-inbox")
            w = _ReceiveWindow(
                dest_dir=dest,
                secs=args.get("secs") or 300,
                max_files=args.get("max_files") or 32,
                max_bytes=args.get("max_bytes") or (2 * 1024 * 1024 * 1024),
                max_file_bytes=args.get("max_file_bytes") or (1024 * 1024 * 1024),
                alias=args.get("alias"),
                pin=args.get("pin"))
            port, why = w.open()
            if port is None:
                return R.unavailable("lan.recv", why, device=self.name, via=self.via)
            self._window = w
            return R.success("lan.recv", self.name, via=self.via, data=w.summary(
                note="窗口已开：%d 秒，落盘 %s，最多 %d 个文件 / %.1f GB；"
                     "到点自动关，不发通知" % (int(w.deadline - time.time()), w.dest_dir,
                                              w.max_files, w.max_bytes / 1024.0 ** 3)))

        if cap == "lan.recv.stop":
            w = self._window
            if not w:
                return R.success("lan.recv.stop", self.name, via=self.via,
                                 data={"note": "本来就没有窗口开着"})
            out = w.summary(note="已关闭")
            w.close()
            self._window = None
            return R.success("lan.recv.stop", self.name, via=self.via, data=out)

        return R.unsupported(cap, "%s 没有这项能力" % self.name, device=self.name, via=self.via)


def _local_ipv4s():
    """本机所有**非回环** IPv4。

    不用「UDP connect 到 8.8.8.8 再看 getsockname」那套：断网时它会抛异常，
    于是「有网卡但没外网」会被判成「没网卡」——而这正是局域网直传最该能用的时候。
    """
    out = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for item in infos:
        ip = item[4][0]
        if ip and not ip.startswith("127.") and ip not in out:
            out.append(ip)
    return out

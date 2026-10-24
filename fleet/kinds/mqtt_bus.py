"""MQTT 通道：把物联网总线当成设备。

为什么自己写协议不 pip install paho：这台机器的纪律是「少依赖、能自证」——
一条只用到 CONNECT / PUBLISH / SUBSCRIBE 的通道，MQTT 3.1.1 里加起来不到 200 行，
却要为此背一个第三方库（含它自己的版本漂移）不划算。而且自己写才能把
「broker 说不许」和「网络不通」分得干干净净——paho 会把两者糊成一个异常。

三条能力：

  mqtt.probe   真握手，看 CONNACK 返回码。返回码 0 才算通；4/5 是「有服务但不让你进」，
               这跟「连不上」是两码事，必须能分开，否则排查全靠猜。
  mqtt.pub     发一条（QoS 0/1，1 要等 PUBACK 才算真送达——不等就是自欺）。
  mqtt.sub     订阅并收若干条，带总超时；超时也不算失败，而是如实回「收到 N 条」。

诚实性纪律：
1. 返回码一律翻成人话（`RC_TEXT`），不把「5」这种数字丢给上层。
2. QoS1 必须等 PUBACK；等不到就是 timeout，不许说成功。
3. 不收密码进日志：`as_dict()` 的载荷里永不出现 password 字段。
"""
import socket
import struct
import time
import uuid

from ..core import BaseDevice, DeviceResult as R

# CONNACK 返回码（MQTT 3.1.1 §3.2.2.3）
RC_TEXT = {
    0: "通过",
    1: "协议版本不被接受",
    2: "client id 被拒",
    3: "服务不可用",
    4: "用户名或密码错",
    5: "未授权",
}

# 包类型
_CONNECT, _CONNACK, _PUBLISH, _PUBACK = 1, 2, 3, 4
_SUBSCRIBE, _SUBACK, _PINGREQ, _PINGRESP, _DISCONNECT = 8, 9, 12, 13, 14


# ---------------------------------------------------------------- 编解码

def _remaining_len(n):
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _str(s):
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


def _read_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("对端关连接（读到 %d/%d 字节）" % (len(buf), n))
        buf += chunk
    return buf


def _read_packet(s):
    """读一整包，返回 (类型, flags, 载荷)。"""
    h = _read_exact(s, 1)[0]
    mult, val = 1, 0
    for _ in range(4):
        b = _read_exact(s, 1)[0]
        val += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    body = _read_exact(s, val) if val else b""
    return h >> 4, h & 0x0F, body


def _pack(typ, flags, body):
    return bytes([(typ << 4) | flags]) + _remaining_len(len(body)) + body


def _connect_body(cid, user="", pw="", keepalive=30):
    flags = 0x02                                  # clean session
    payload = _str(cid)
    if user:
        flags |= 0x80
        payload += _str(user)
        if pw:
            flags |= 0x40
            payload += _str(pw)
    vh = _str("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive)
    return vh + payload


class ConnectFail(OSError):
    """TCP 握手阶段失败——「门都没进去」。

    为什么要专门一个类：`create_connection` 的超时也是 `socket.timeout`，
    跟「进去了但对方不回 CONNACK」撞成同一句话，排查时白绕一圈。
    `timed_out` 决定上层报 timeout 还是 unavailable——「对方没应答」和
    「对方明确拒绝」是两种处置，不能糊成一种。
    """

    def __init__(self, msg, timed_out=False):
        super().__init__(msg)
        self.timed_out = timed_out


class HandshakeFail(Exception):
    """端口开着、连接建立了，但对面不是会说话的 broker（没 CONNACK / 说一半就断）。"""


def _open(broker, port, user="", pw="", timeout=5.0, cid=None, keepalive=30):
    """连上并握手。返回 (socket, 返回码, 会话标志)。

    失败分三种，调用方必须分开报（混成一句就等于没报）：
      ConnectFail   —— TCP 没建起来（超时 / 被拒 / 域名解析不了）
      HandshakeFail —— 连上了但对面不是 MQTT broker
      socket.timeout—— 连接正常，是 CONNACK 一直没来
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((broker, port))
    except socket.timeout:
        s.close()
        raise ConnectFail(f"{broker}:{port} 的 TCP 握手 {timeout}s 没完成"
                          f"（不是「连上了不说话」）", timed_out=True)
    except OSError as e:
        s.close()
        raise ConnectFail(f"{broker}:{port} 连不上：{e}")
    cid = cid or ("skynet-" + uuid.uuid4().hex[:12])
    s.sendall(_pack(_CONNECT, 0, _connect_body(cid, user, pw, keepalive)))
    try:
        typ, _fl, body = _read_packet(s)
    except socket.timeout:
        s.close()
        raise                                   # 交给调用方报「握手超时」
    except (ConnectionError, OSError) as e:
        s.close()
        raise HandshakeFail(f"{broker}:{port} 连上就被断开（{e}）——不像 MQTT broker")
    if typ != _CONNACK:
        s.close()
        raise HandshakeFail(f"{broker}:{port} 握手期望 CONNACK，收到类型 {typ}")
    sp, rc = (body[0] & 0x01, body[1]) if len(body) >= 2 else (0, 0xFF)
    return s, rc, sp


def _bye(s):
    try:
        s.sendall(_pack(_DISCONNECT, 0, b""))
    except OSError:
        pass
    try:
        s.close()
    except OSError:
        pass


def _qos1_publish_body(topic, payload, pid, retain=False):
    flags = 0x02 | (0x01 if retain else 0x00)      # QoS 1 + 可选 retain
    body = _str(topic) + struct.pack("!H", pid) + payload
    return _pack(_PUBLISH, flags, body)


# ---------------------------------------------------------------- 设备

class MqttBus(BaseDevice):
    kind = "mqtt"
    via = "mqtt"

    def __init__(self, name="mqtt:bus", **kw):
        super().__init__(name, via="mqtt", **kw)
        self.meta.setdefault(
            "note", "只连你自己的 broker / 自己授权的设备；broker 地址是参数不是设备属性")

    def probe(self):
        """这一层没有「默认 broker」，所以 probe 只回答「实现自不自洽」。

        跟 lan:scanner 同一个道理：扫描器不需要事先有目标才叫可用。
        具体某个 broker 通不通，是 `mqtt.probe` 的事（那是真握手）。
        """
        return True, ""

    def caps(self):
        return ("mqtt.probe", "mqtt.pub", "mqtt.sub")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "mqtt.probe":
            return self._probe(args)
        if cap == "mqtt.pub":
            return self._pub(args)
        if cap == "mqtt.sub":
            return self._sub(args)
        return super().invoke(cap, args)

    # ---- 共用参数解析 ----
    @staticmethod
    def _target(args):
        broker = (args.get("broker") or args.get("host") or "").strip()
        port = int(args.get("port") or 1883)
        return broker, port

    def _probe(self, args):
        broker, port = self._target(args)
        if not broker:
            return R.error("mqtt.probe", "缺 broker（如 192.168.1.10 或 test.mosquitto.org）",
                           device=self.name, via=self.via)
        timeout = float(args.get("timeout") or 5)
        t0 = time.time()
        try:
            s, rc, sp = _open(broker, port, args.get("user") or "", args.get("pw") or "",
                              timeout)
        except ConnectFail as e:
            cls = R.timeout if e.timed_out else R.unavailable
            return cls("mqtt.probe", str(e), device=self.name,
                       ms=(time.time() - t0) * 1000, via=self.via)
        except socket.timeout:
            return R.timeout("mqtt.probe",
                             f"{broker}:{port} 连接建立了，但 {timeout}s 内没等到 CONNACK"
                             f"——不像 MQTT broker", device=self.name,
                             ms=(time.time() - t0) * 1000, via=self.via)
        except HandshakeFail as e:
            return R.unavailable("mqtt.probe", f"不像 MQTT broker：{e}",
                                 device=self.name, ms=(time.time() - t0) * 1000,
                                 via=self.via)
        took = (time.time() - t0) * 1000
        _bye(s)
        if rc != 0:
            return R.denied("mqtt.probe",
                            f"{broker}:{port} 是 MQTT broker，但拒绝入场："
                            f"{RC_TEXT.get(rc, '未知返回码 %s' % rc)}（rc={rc}）",
                            device=self.name, ms=took, via=self.via)
        return R.success("mqtt.probe", self.name, via=self.via, ms=took, data={
            "broker": broker, "port": port, "rc": rc, "session_present": bool(sp),
            "verdict": "握手通过"})

    def _pub(self, args):
        broker, port = self._target(args)
        topic = (args.get("topic") or "").strip()
        if not broker or not topic:
            return R.error("mqtt.pub", "缺 broker 或 topic", device=self.name, via=self.via)
        raw = args.get("payload", "")
        payload = raw if isinstance(raw, bytes) else str(raw).encode("utf-8")
        qos = int(args.get("qos") or 0)
        if qos not in (0, 1):
            return R.error("mqtt.pub", f"只支持 QoS 0/1，收到 {qos}",
                           device=self.name, via=self.via)
        retain = bool(args.get("retain"))
        timeout = float(args.get("timeout") or 5)
        t0 = time.time()
        try:
            s, rc, _sp = _open(broker, port, args.get("user") or "", args.get("pw") or "",
                               timeout)
        except ConnectFail as e:
            cls = R.timeout if e.timed_out else R.unavailable
            return cls("mqtt.pub", str(e), device=self.name,
                       ms=(time.time() - t0) * 1000, via=self.via)
        except socket.timeout:
            return R.timeout("mqtt.pub",
                             f"{broker}:{port} 连接建立了，但没等到 CONNACK",
                             device=self.name, ms=(time.time() - t0) * 1000,
                             via=self.via)
        except HandshakeFail as e:
            return R.unavailable("mqtt.pub", f"不像 MQTT broker：{e}",
                                 device=self.name, ms=(time.time() - t0) * 1000,
                                 via=self.via)
        if rc != 0:
            _bye(s)
            return R.denied("mqtt.pub",
                            f"{broker}:{port} 拒绝入场：{RC_TEXT.get(rc, rc)}（rc={rc}）",
                            device=self.name, ms=(time.time() - t0) * 1000,
                            via=self.via)
        try:
            if qos == 0:
                body = _str(topic) + payload
                s.sendall(_pack(_PUBLISH, 0x01 if retain else 0x00, body))
            else:
                pid = 1
                s.sendall(_qos1_publish_body(topic, payload, pid, retain))
                # QoS1 必须等到 PUBACK 才算送达；等不到就不能说成功
                typ, _fl, ack = _read_packet(s)
                if typ != _PUBACK:
                    return R.error("mqtt.pub", f"期望 PUBACK，收到类型 {typ}",
                                   device=self.name, via=self.via)
                if len(ack) >= 2 and struct.unpack("!H", ack[:2])[0] != pid:
                    return R.error("mqtt.pub", "PUBACK 的包 id 对不上",
                                   device=self.name, via=self.via)
        except socket.timeout:
            return R.timeout("mqtt.pub", "发出去了但没等到 PUBACK", device=self.name,
                             ms=(time.time() - t0) * 1000, via=self.via)
        except ConnectionError as e:
            return R.error("mqtt.pub", f"发送中断：{e}", device=self.name, via=self.via)
        finally:
            _bye(s)
        return R.success("mqtt.pub", self.name, via=self.via,
                         ms=(time.time() - t0) * 1000, side_effect=True, data={
                             "broker": broker, "port": port, "topic": topic,
                             "bytes": len(payload), "qos": qos, "retain": retain,
                             "acked": qos == 1})

    def _sub(self, args):
        broker, port = self._target(args)
        topic = (args.get("topic") or "#").strip()
        if not broker:
            return R.error("mqtt.sub", "缺 broker", device=self.name, via=self.via)
        want = max(1, int(args.get("count") or 1))
        timeout = float(args.get("timeout") or 5)
        t0 = time.time()
        try:
            s, rc, _sp = _open(broker, port, args.get("user") or "", args.get("pw") or "",
                               timeout)
        except ConnectFail as e:
            cls = R.timeout if e.timed_out else R.unavailable
            return cls("mqtt.sub", str(e), device=self.name,
                       ms=(time.time() - t0) * 1000, via=self.via)
        except socket.timeout:
            return R.timeout("mqtt.sub",
                             f"{broker}:{port} 连接建立了，但没等到 CONNACK",
                             device=self.name, ms=(time.time() - t0) * 1000,
                             via=self.via)
        except HandshakeFail as e:
            return R.unavailable("mqtt.sub", f"不像 MQTT broker：{e}",
                                 device=self.name, ms=(time.time() - t0) * 1000,
                                 via=self.via)
        if rc != 0:
            _bye(s)
            return R.denied("mqtt.sub",
                            f"{broker}:{port} 拒绝入场：{RC_TEXT.get(rc, rc)}（rc={rc}）",
                            device=self.name, ms=(time.time() - t0) * 1000, via=self.via)
        got, subs = [], []
        try:
            pid = 1
            s.sendall(_pack(_SUBSCRIBE, 0x02,
                            struct.pack("!H", pid) + _str(topic) + b"\x00"))
            typ, _fl, body = _read_packet(s)
            if typ != _SUBACK:
                return R.error("mqtt.sub", f"期望 SUBACK，收到类型 {typ}",
                               device=self.name, via=self.via)
            subs = list(body[2:])                 # 每主题一个授予的 QoS，0x80 = 拒绝
            while len(got) < want:
                typ, fl, body = _read_packet(s)
                if typ == _PINGRESP:
                    continue
                if typ != _PUBLISH:
                    continue
                qos = (fl & 0x06) >> 1
                tl = struct.unpack("!H", body[:2])[0]
                t = body[2:2 + tl]
                rest = body[2 + tl:]
                if qos > 0 and len(rest) >= 2:
                    rest = rest[2:]
                got.append({"topic": t.decode("utf-8", "replace"),
                            "payload": rest.decode("utf-8", "replace"),
                            "qos": qos})
        except socket.timeout:
            # 收不满不算失败：如实回「收到几条」，让上层自己决定够不够
            pass
        except ConnectionError as e:
            _bye(s)
            return R.error("mqtt.sub", f"收的过程中断了：{e}", device=self.name,
                           ms=(time.time() - t0) * 1000, via=self.via)
        finally:
            _bye(s)
        if subs and subs[0] == 0x80:
            return R.denied("mqtt.sub", f"broker 拒绝订阅 {topic}（SUBACK=0x80）",
                            device=self.name, ms=(time.time() - t0) * 1000,
                            via=self.via)
        return R.success("mqtt.sub", self.name, via=self.via,
                         ms=(time.time() - t0) * 1000, data={
                             "broker": broker, "port": port, "topic": topic,
                             "granted_qos": subs[0] if subs else None,
                             "wanted": want, "got": len(got), "messages": got,
                             "complete": len(got) >= want})

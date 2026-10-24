"""配对信箱（N+1b 第 1 步）：广播公告 + 验证码握手 + 会话令牌。

主人要的「信箱机制」：被控端开「可被配对」后，往局域网广播一封极简公告
（信箱在哪）；主控端看到后，输入被控端显示的 6 位验证码（或贴
`fleetpair://` 链接）完成握手，换到一枚**一次性会话令牌**。此后才谈得上
「连接/控制」——发现不等于配对，配对不等于控制，三道各是一道。

本模块只管「配对」这一层， deliberately 不做：

  · 不做画面/输入传输（那是 N+1b-2 的受控通道，走 FleetServer 的
    /api/remote/*，代理现成的 pcctl.* 能力）；
  · 不做 TLS（N+1b-2：stdlib ssl 自签 + 证书指纹钉扎，本模块只在
    会话里记 cert_fp，不碰密钥）；
  · 不做 STUN/打洞/中继（N+1b-4）。

三条铁律（写在代码里，不写在文档里）：

  1. **公告字段最小化**。广播payload 只有五个键：app/v/name/pair_port/nonce。
     没有令牌、没有能力清单、没有任何可执行信息——广播只回答「信箱在哪」。
     谁在公告里多塞一个键，门禁 t19 的静态扫描当场红。
  2. **fail-closed**。信箱默认关闭（enabled=False）；没开放配对时
     try_pair 直接拒，不存在「试试看」路径。验证码一次性、5 分钟过期
     （估，可配）、5 次试爆锁 10 分钟（估，可配）——不用固定密码。
  3. **会话可掐断**。令牌一次性、随 revoke 作废；被控端一键 revoke_all
     之后，所有已发令牌全部当场死亡（不是「下次生效」）。

审计：配对成功写一条 open（topic=pair:<对方名>，被 revoke 时 close 闭环）；
失败/锁定/刷新写 done 不留待办噪音。令牌本身绝不进 journal（redact 之外
  的双保险：body 里根本不拼令牌）。

线程安全：验证码状态与会话表都有锁——GUI 线程点刷新、网络线程 try_pair、
     被控端一键熔断，三处并发是常态。
"""
import json
import secrets
import socket
import threading
import time

# ---------------------------------------------------------------- 常量

#: 配对广播端口（UDP）。避开 AudioSync 的 24548 族。
PAIR_UDP_PORT = 24560
#: 配对会话端口（TCP，TLS）。被控端点 /api/pair 与 /api/remote/* 只在这个
#: 独立监听器上服务；GUI 主循环仍只绑 127.0.0.1，两边攻击面不互通。
PAIR_TCP_PORT = 24561
#: 组播组：239.255.0.0/16 是管理域内组播，配合 TTL=1 路由器不转发
MCAST_GRP = ("239.77.7.77", PAIR_UDP_PORT)
#: 公告间隔（秒，估）：1s 足够主控端秒级发现，又不至于刷屏
BEACON_INTERVAL = 1.0
#: 组播 TTL：1 = 不出本网段（LAN-only 的网络层保证）
MCAST_TTL = 1
#: 验证码有效期（秒，估）：5 分钟。向日葵临时密码同级。
CODE_TTL = 300.0
#: 试爆上限（次，估）：5 次失败即锁
CODE_MAX_FAILS = 5
#: 锁定时长（秒，估）：10 分钟
CODE_LOCK_SECS = 600.0
#: 会话令牌熵（字节）：32 → token_urlsafe 约 43 字符
TOKEN_BYTES = 32
#: 公告 payload 的**全部**合法键。多一个键就是门禁红灯。
BEACON_KEYS = frozenset({"app", "v", "name", "pair_port", "nonce"})
APP_ID = "fleet"
BEACON_VER = 1


# ---------------------------------------------------------------- 公告

def beacon_payload(name, pair_port, nonce):
    """组装广播公告。键集合锁死为 BEACON_KEYS——这是给静态门禁扫的。"""
    return {"app": APP_ID, "v": BEACON_VER,
            "name": str(name), "pair_port": int(pair_port),
            "nonce": str(nonce)}


def encode_beacon(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def decode_beacon(data):
    """解析收到的公告。任何不合法都返回 None——不猜、不补默认值。

    合法 = JSON 对象 + app 是 fleet + 键集合 ⊆ BEACON_KEYS + pair_port 是整数。
    """
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("app") != APP_ID:
        return None
    if not set(obj.keys()) <= BEACON_KEYS:
        return None
    if not isinstance(obj.get("pair_port"), int):
        return None
    return obj


class Beacon:
    """被控端公告器：周期性往本网段广播「信箱在哪」。

    sock 可注入（门禁全离线：换成假的，收 sendto/setsockopt 调用记录）。
    真跑时用 socket 模块；setsockopt(IP_MULTICAST_TTL, 1) 是 LAN-only 的
    网络层保证——不靠「我记得只发本网段」这种口头纪律。
    """

    def __init__(self, name, pair_port, sock=None, interval=BEACON_INTERVAL,
                 grp=MCAST_GRP):
        self.name = str(name)
        self.pair_port = int(pair_port)
        self.interval = float(interval)
        self.grp = grp
        self.nonce = secrets.token_hex(8)     # 每次开机一枚：防旧公告冒充
        self._sock = sock
        self._stop = threading.Event()
        self._thread = None
        self.sent = 0                        # 门禁与自查看

    def payload(self):
        return beacon_payload(self.name, self.pair_port, self.nonce)

    def _ensure_sock(self):
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # TTL 无论真假 sock 都钉（幂等）：LAN-only 是发送行为的一部分，
        # 不能因为「测试注入了假 sock」就跳过——那门禁就白测了。
        try:
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
                                  MCAST_TTL)
        except OSError:
            # 真 socket 上极少数环境（禁组播的策略）会拒：如实炸，不静默
            raise RuntimeError(
                "本机不允许设置组播 TTL（IP_MULTICAST_TTL），广播公告发不了："
                "防火墙/安全策略可能禁了组播。配对请改走 IPv6 直连或手动端点。")
        return self._sock

    def beat(self):
        """发一封公告。返回 payload（自查看/门禁用）。"""
        p = self.payload()
        self._ensure_sock().sendto(encode_beacon(p), self.grp)
        self.sent += 1
        return p

    def start(self):
        if self._thread is not None:
            raise RuntimeError("公告器已经起来了")
        self._stop.clear()

        def _loop():
            try:
                from .beat import register
            except (ImportError, ValueError):
                from beat import register
            bt = register("pair.beacon", stale_after=max(90.0, self.interval * 6))
            bt.beat()                       # 进场先记：免得头一个周期被判假 stale
            while not self._stop.wait(self.interval):
                try:
                    self.beat()
                except OSError as e:
                    # 网络抖动（网卡刚断开等）：不停机，下一轮再试。
                    # 但**不许静默**——以前这里是 pass，结果广播悄悄停了几小时
                    # 也没人知道。现在记进心跳账本，/api/health 里看得见失败原因。
                    bt.allow("broadcast: %s" % e)
                    continue
                except Exception as e:      # 非网络异常更要留证据，但不打死循环
                    bt.allow("%s: %s" % (type(e).__name__, e))
                    continue
                bt.beat()

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread = None


def tap_beacons(sock, timeout=1.5, limit=32):
    """主控端收公告：从 sock 收 timeout 秒，返回解析成功的公告列表。

    收信口复用同一个组播组端口；解析不了的包直接丢（decode_beacon 的纪律：
  不猜）。limit 防「公告风暴」把内存吃穿——一封信只该带五个字段。
    """
    out = []
    deadline = time.time() + max(0.0, timeout)
    sock.settimeout(0.2)
    while len(out) < limit and time.time() < deadline:
        try:
            data, _addr = sock.recvfrom(2048)
        except (socket.timeout, TimeoutError):
            continue
        except OSError:
            break
        p = decode_beacon(data)
        if p is not None:
            out.append(p)
    return out


# ---------------------------------------------------------------- 地址

def local_ips():
    """本机各网卡地址（去重排序）。给自签证书写 SAN 用。

    两条路互补：getaddrinfo(主机名) 拿得到系统登记的全部地址；UDP connect
    外网地址再取 sockname，拿「默认路由走哪张网卡」那个地址（主机名解析
    不全的机器上这条更准）。connect 不真发包（UDP 无连接），只是选路。
    """
    ips = set()
    try:
        for fam in (socket.AF_INET, socket.AF_INET6):
            for info in socket.getaddrinfo(socket.gethostname(), None, fam):
                ips.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


# ---------------------------------------------------------------- 链接

def make_link(host, port, code, fp=""):
    """生成 fleetpair:// 链接。code 与 fp（证书指纹前缀）随链接走。

    形态（估，可调）：fleetpair://<host>:<port>#<code>&fp=<前8位指纹>
    用 # 而不是 ?：code 是「片段」语义，不进服务端日志/Referer
    （同 GUI token 放片段的理由）。
    """
    host = str(host)
    port = int(port)
    code = str(code)
    frag = code + ("&fp=" + str(fp) if fp else "")
    return "fleetpair://%s:%d#%s" % (host, port, frag)


def parse_link(text):
    """解析 fleetpair:// 链接 → {host, port, code, fp}；不合法返回 None。

    只认自家 scheme；缺 host/port/code 任何一样都算不合法（不补默认值）。
    """
    if not text or not isinstance(text, str):
        return None
    if not text.startswith("fleetpair://"):
        return None
    rest = text[len("fleetpair://"):]
    if "#" not in rest:
        return None
    hostport, frag = rest.split("#", 1)
    if ":" not in hostport:
        return None
    host, _, port_s = hostport.rpartition(":")
    if not host:
        return None
    try:
        port = int(port_s)
    except ValueError:
        return None
    code, _, fp = frag.partition("&fp=")
    code = code.strip()
    if not (len(code) == 6 and code.isdigit()):
        return None
    return {"host": host, "port": port, "code": code, "fp": fp.strip()}


# ---------------------------------------------------------------- 信箱

class PairingMailbox:
    """被控端信箱：验证码状态机 + 会话表 + 审计。

    用法（被控端）：
        mb = PairingMailbox(name="PC-A", journal=j)
        mb.enabled = True            # 主人点了「可被配对」
        code = mb.refresh_code()     # GUI 显示这 6 位
        r = mb.try_pair(code, controller_name="PC-B", cert_fp="ab12...")
        r["ok"] and r["token"]       # 握手成功，发令牌
        mb.revoke(token)             # 单会话掐断 / mb.revoke_all() 全掐

    用法（服务端 N+1b-2）：
        s = mb.session_for(token)    # 令牌 → 会话；没有/已 revoked → None
    """

    def __init__(self, name="", journal=None, ttl=CODE_TTL,
                 max_fails=CODE_MAX_FAILS, lock_secs=CODE_LOCK_SECS):
        self.name = str(name)
        self.journal = journal            # None = 不审计（测试/一次性脚本）
        self.ttl = float(ttl)
        self.max_fails = int(max_fails)
        self.lock_secs = float(lock_secs)
        self.enabled = False              # fail-closed：默认不开放配对
        self._lk = threading.Lock()
        self._code = ""                   # 当前验证码（空 = 没有）
        self._code_at = 0.0
        self._fails = 0
        self._locked_until = 0.0
        self._sessions = {}               # token -> session dict

    # ---------------- 验证码 ----------------

    def refresh_code(self):
        """换一枚新验证码（GUI 的「刷新」）。同时清零试爆计数、解锁。"""
        with self._lk:
            self._code = "%06d" % (secrets.randbelow(900000) + 100000)
            self._code_at = time.time()
            self._fails = 0
            self._locked_until = 0.0
            self._audit("pair_code_refresh", body="验证码已刷新", done=True)
            return self._code

    def code_state(self):
        """给 GUI 看的状态。**绝不外发验证码本身**（GUI 在本机读）。"""
        with self._lk:
            now = time.time()
            return {
                "enabled": self.enabled,
                "has_code": bool(self._code),
                "expires_in": max(0.0, round(self._code_at + self.ttl - now, 1)),
                "locked_for": max(0.0, round(self._locked_until - now, 1)),
                "fails": self._fails,
            }

    def current_code(self):
        """当前验证码原文——**只给被控端自己的 GUI 显示用**。

        不过网络：HTTP 层没有任何端点吐它（pair_info 经本方法读，那是
        本机回环里的 GUI）。过期返回空串（GUI 据此显示「已过期，请刷新」）。
        """
        with self._lk:
            if not self._code:
                return ""
            if time.time() - self._code_at > self.ttl:
                return ""
            return self._code

    def _code_ok(self, code, now):
        """锁内校验。返回 (ok, err)。"""
        if self._locked_until > now:
            return False, ("验证码已被锁定（试爆过多），还有 %.0f 秒；"
                           "可点刷新换新码" % (self._locked_until - now))
        if not self._code:
            return False, "还没有验证码（被控端先点「刷新」）"
        if now - self._code_at > self.ttl:
            return False, "验证码已过期（有效期 %.0f 秒），请被控端刷新" % self.ttl
        if not secrets.compare_digest(str(code), self._code):
            self._fails += 1
            if self._fails >= self.max_fails:
                self._locked_until = now + self.lock_secs
                self._audit("pair_lock",
                            body="验证码试爆 %d 次，锁定 %.0f 秒"
                                 % (self._fails, self.lock_secs), done=True)
                return False, ("验证码错误次数过多，已锁定 %.0f 秒"
                               % self.lock_secs)
            left = self.max_fails - self._fails
            return False, "验证码不对（还剩 %d 次机会，之后锁 %d 秒）" \
                          % (left, int(self.lock_secs))
        return True, ""

    # ---------------- 握手 ----------------

    def try_pair(self, code, controller_name="", cert_fp=""):
        """主控端持码握手。成功 → {ok, token, session}；失败 → {ok:False, err}。

        一次性：成功用掉的码当场作废（_code 清空）。防的是「截图/偷看
        一眼验证码，之后反复进」——向日葵临时密码同级纪律。
        """
        with self._lk:
            if not self.enabled:
                self._audit("pair_reject",
                            body="未开放配对时收到配对请求（来自 %s）"
                                 % (controller_name or "?"), done=True)
                return {"ok": False, "err": "被控端没有开放配对（可被配对开关未开）"}
            now = time.time()
            ok, err = self._code_ok(code, now)
            if not ok:
                self._audit("pair_fail",
                            body="配对失败：%s（来自 %s）" % (err, controller_name or "?"),
                            done=True)
                return {"ok": False, "err": err}
            token = secrets.token_urlsafe(TOKEN_BYTES)
            session = {"token": token, "controller": str(controller_name or "?"),
                       "cert_fp": str(cert_fp or ""), "created_at": now,
                       "last_seen": now}
            self._sessions[token] = session
            self._code = ""                # 一次性：用掉即废
            self._fails = 0
            self._audit("pair_ok",
                        body="配对成功：%s 接入（指纹 %s）"
                             % (session["controller"], session["cert_fp"][:8] or "无"),
                        topic="pair:%s" % session["controller"], done=False)
            return {"ok": True, "token": token, "session": self._public(session)}

    # ---------------- 会话 ----------------

    def session_for(self, token):
        """令牌 → 会话（公开形态，无 token 字段）；没有/已 revoked → None。"""
        with self._lk:
            s = self._sessions.get(str(token or ""))
            if s is None:
                return None
            s["last_seen"] = time.time()
            return self._public(s)

    def revoke(self, token):
        """掐断单个会话。返回 bool；已不存在也返回 True（幂等，熔断不报错）。"""
        with self._lk:
            s = self._sessions.pop(str(token or ""), None)
            if s is None:
                return True
            self._audit("pair_revoke",
                        body="会话已掐断：%s" % s["controller"],
                        topic="pair:%s" % s["controller"], done=True)
            return True

    def revoke_all(self):
        """一键熔断：所有会话当场死亡。返回掐断数（GUI 显示用）。"""
        with self._lk:
            n = 0
            for tok in list(self._sessions.keys()):
                s = self._sessions.pop(tok)
                n += 1
                self._audit("pair_revoke",
                            body="一键熔断：%s" % s["controller"],
                            topic="pair:%s" % s["controller"], done=True)
            return n

    def sessions(self):
        """当前会话列表（无 token）——GUI「谁在控我」用。"""
        with self._lk:
            return [self._public(s) for s in self._sessions.values()]

    @staticmethod
    def _public(s):
        return {"controller": s["controller"], "cert_fp": s["cert_fp"],
                "created_at": s["created_at"], "last_seen": s["last_seen"]}

    # ---------------- 审计 ----------------

    def _audit(self, kind, body, topic=None, done=False):
        if self.journal is None:
            return
        # kind 用 "see"（感知事件）：配对是安全感知，不是待办。
        # done=True 的当场闭环；pair_ok 留 open，等 revoke 时 close。
        self.journal.log("see", actor="pair", body=body, topic=topic,
                         status="done" if done else "open")

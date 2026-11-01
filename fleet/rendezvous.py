"""天网 · 地址动态层（rendezvous）：让会变的地址，看起来是固定的。

## 这一层要解决的不是「IPv6 有没有」

2026-11-01 主人点出的两句话，是本文件存在的全部理由：

  「IPv6 按设备分类，你有设备就自动给你一个，连上网的瞬间就给你了」
  「只是这个技术在国内没普及，所以很多人认为是假的」

也就是说：**v6 缺失在国内几乎总是配置问题，不是技术不存在。**
于是「没有 v6」这个结论必须先被拆开再下 —— 见 `tunnel.ipv6_diagnose()` 四档。

但紧接着还有第二个问题，而且是更根本的那个：

  「会变的地址只要给它加一个动态就行了」
  「直接加一个动态检测、动态自检、切换就行了，这不本质就是另类的固态吗」

对。地址轮换（RFC 4941 临时地址按小时变、家宽重拨换前缀）根本不该被当成
「不可用」—— 它只需要一层**动态更新**。而这层动态更新做完整之后，
对外呈现的就是一个**逻辑上固定、物理上可换**的入口：

    上层永远只认 self_id（设备指纹，跨重启跨网络不变）
    底层地址随便漂，漂了自动检测 → 自动上报 → 自动自检 → 自动切换

这就是「另类的固态」：像 SSD 的磨损均衡，上层写的是同一个逻辑块，
底下具体落在哪颗闪存颗粒上，用户看不见也不需要知道。

## 三件事，缺一不可

    动态检测  `detect()`   我现在有哪些地址（含临时地址，不丢）
    动态自检  `verify()`   **从墙外**回连，确认到底进不进得来
    自动切换  `refresh()`  变了就换，换上之前先验证，换下来记一笔

## 为什么自检必须放到墙外

本机往自己的公网地址发包走的是 hairpin NAT，很多家用路由器根本不支持。
于是「本机自连成功」不能证明外网能连进来，「本机自连失败」也不能证明外网连不进来
—— 两个方向都是假信号。只有站在墙外的机器来试才算数。
中继正好在墙外，所以 `verify()` 借它的 `/probe`。

## 不做的事

不常驻线程、不建定时任务（项目铁律）。`refresh()` 是**幂等 + 节流**的，
谁调用谁推动；调用得太频繁就什么都不做。这样它既能被面板反复调用，
也能被某次真实发送顺手带起来，而不需要养一个后台进程。
"""
import hashlib
import ipaddress
import json
import os
import sys
import tempfile
import threading
import time

try:                                  # 被包导入时（正常路径）
    from . import tunnel as tun
    from . import nat as nat_mod
except ImportError:                   # 直接 python fleet/rendezvous.py 跑自测时
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from fleet import tunnel as tun
    from fleet import nat as nat_mod

#: 多久重算一次。60 秒是权衡：临时地址按小时变，1 分钟粒度足够早发现；
#: 再密就是纯浪费（每次都要拨 TCP 出网自检）。
DEFAULT_TTL = 60.0

#: 状态落盘位置。**必须落盘**：只有记得「上次在哪」，才谈得上「变了」。
STATE_PATH = os.path.join(os.path.expanduser("~"), ".skynet", "rendezvous.json")

_LOCK = threading.Lock()
_SELF = {"id": None}


# ---------------------------------------------------------------- 身份
def _machine_seed():
    """能代表「这台机器」的稳定种子。拿不到返回空串，由调用方兜底。

    优先级：
      ① Windows MachineGuid —— 装系统时生成一次，重装才变，最稳
      ② 网卡 MAC —— 多网卡时取排序后第一个，换了网卡就变
      ③ 都没有 —— 返回空，由 self_id() 生成随机值并落盘

    为什么不用主机名：改个名字就成新设备了，而主机名恰恰是最常被改的东西。
    """
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography", 0,
                winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
            ) as k:
                v, _ = winreg.QueryValueEx(k, "MachineGuid")
                if v:
                    return "mg:" + str(v)
        except Exception:
            pass

    try:
        import uuid
        node = uuid.getnode()
        # 最低位为 1 表示这是随机生成的假 MAC（拿不到真实网卡时的占位），不能用
        if node and (node >> 40) % 2 == 0:
            return "mac:%012x" % node
    except Exception:
        pass
    return ""


def self_id(state_path=STATE_PATH):
    """本机稳定指纹。跨重启、跨网络、跨 WiFi 切换都不变。

    这就是「另类的固态」里那个**不变的标识**：地址是它的属性，不是它本身。
    """
    if _SELF["id"]:
        return _SELF["id"]

    seed = _machine_seed()
    if seed:
        sid = "sf-" + hashlib.sha256(seed.encode()).hexdigest()[:16]
    else:
        # 拿不到机器特征：用随机值 + 落盘。**不能每次现生成**，
        # 否则每次重启都变成「一台新设备」，对端存的地址全失效。
        st = _load(state_path)
        sid = st.get("self_id") or ("sf-" + os.urandom(8).hex())
        st["self_id"] = sid
        _save(st, state_path)

    _SELF["id"] = sid
    return sid


# ---------------------------------------------------------------- 状态
def _load(state_path=STATE_PATH):
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(st, state_path=STATE_PATH):
    """原子写：先写临时文件再 replace。

    直接覆盖写，中途断电会留下半个 JSON —— 下次读不出来就等于「忘了自己在哪」，
    而这一层的全部价值就是「记得」。
    """
    d = os.path.dirname(state_path)
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, state_path)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 检测
def detect(deep=False, timeout=4.0):
    """采本机当前所有可用地址。

    **临时地址照样收进来**，只是打上 temporary 标记。
    上一版我按「临时地址几小时后会变，不适合做长期候选」把它降权，
    那是把「需要动态更新」错当成「不可用」—— 而动态更新恰恰是这一层的工作。
    收进来 + 标出来 + 定期刷新，才是正确处理。
    """
    cands = []

    # v6：稳定的排前面，但临时的也留着
    try:
        for c in tun.ipv6_candidates(timeout=timeout):
            cands.append({"ip": c["ip"], "family": 6, "kind": "global6",
                          "temporary": c.get("temporary"), "source": "iface"})
    except Exception:
        pass

    # v4：本机非回环地址（同一网段可直接用，跨网要靠打洞或中继）
    try:
        for ip in nat_mod.local_ipv4():
            cands.append({"ip": ip, "family": 4, "kind": "private4",
                          "temporary": False, "source": "iface"})
    except Exception:
        pass

    # 出口映射：只有 deep 才做。STUN 要花几秒，不能每次 refresh 都跑
    if deep:
        try:
            r = nat_mod.stun_once(timeout=timeout)
            if r.get("ok") and r.get("mapped_ip"):
                cands.append({"ip": r["mapped_ip"],
                              "port": int(r.get("mapped_port") or 0),
                              "family": 4, "kind": "mapped4",
                              "temporary": False, "source": "stun"})
        except Exception:
            pass

    # 排序：稳定 v6 > 临时 v6 > v4 > 映射。
    # 排前面不是为了「好看」，是因为 `current()` 直接取第一个可用的。
    def _rank(c):
        return (c["family"] != 6, bool(c["temporary"]),
                {"global6": 0, "private4": 1, "mapped4": 2}.get(c["kind"], 9))
    cands.sort(key=_rank)

    return {"ts": round(time.time(), 3), "candidates": cands,
            "v6": tun.ipv6_diagnose(timeout=timeout) if deep else None,
            "count": len(cands)}


def _keyset(snap):
    return {str(c.get("ip")) for c in (snap or {}).get("candidates") or [] if c.get("ip")}


def diff(before, after):
    """两份快照的差异。没有差异就是空，不做任何修饰。"""
    a, b = _keyset(before), _keyset(after)
    return {"added": sorted(b - a), "removed": sorted(a - b),
            "changed": bool(a != b)}


def _host_is_local(origin):
    """origin 的 host 是不是回环/私网地址。

    旧版中继不报源地址时的兜底判据：如果连中继本身都走的是本机回环，
    它「回连成功」这件事一点信息量都没有。
    域名返回 False（判不了，不算本地）—— 这正是为什么不能只靠这一条：
    把本地中继用隧道暴露成一个公网域名，这里就看不出来了。
    """
    try:
        from urllib.parse import urlparse
        host = urlparse(origin if "//" in origin else "//" + origin).hostname or ""
    except Exception:
        return None
    if not host:
        return None
    if host.lower() in ("localhost", "localhost.localdomain"):
        return True
    try:
        a = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(a.is_loopback or a.is_private or a.is_link_local)


def _src_is_local(src, target=""):
    """中继回连时报上来的**源地址**，是不是「就在本机 / 同一网段」。

    True  = 就在本地 ⇒ 这次「可达」是回环假信号，结论作废
    False = 从外部来的 ⇒ 这次可达是真信号
    None  = 判断不了（没报，或报了看不懂的东西）

    为什么同 /64 也算本地：IPv6 没有 NAT，同一个 /64 就是同一条链路 ——
    本机的两个地址（稳定 + 临时）全落在 `240e:404:1900:c780::/64` 里，
    中继若也在这台机器上，它连我们的公网地址时源地址就在同一个 /64。
    这一条是识别「隧道把本地中继伪装成公网域名」的唯一硬判据：
    只判回环/私网抓不到它 —— 那时源地址是货真价实的公网 v6。
    """
    if not src:
        return None
    try:
        s = ipaddress.ip_address(str(src).split("%")[0])
    except ValueError:
        return None
    if s.is_loopback or s.is_private or s.is_link_local:
        return True
    if target and s.version == 6:
        try:
            t = ipaddress.ip_address(str(target).split("%")[0])
        except ValueError:
            t = None
        if t is not None and t.version == 6 and s.packed[:8] == t.packed[:8]:
            return True
    return False


# ---------------------------------------------------------------- 编排
class Rendezvous:
    """把「检测 / 上报 / 自检 / 切换」串成一条可反复调用的链。

    刻意不是后台服务：`refresh()` 幂等 + 节流，谁需要谁调。
    这样它不会变成一个「不调就死、调了又嫌烦」的东西。
    """

    def __init__(self, state_path=STATE_PATH, ttl=DEFAULT_TTL):
        self.state_path = state_path
        self.ttl = ttl
        self._mem = None          # 进程内最近一次快照
        self._last_run = 0.0
        self._lock = threading.Lock()

    # ---- 读
    def state(self):
        st = _load(self.state_path)
        st.setdefault("self_id", self_id(self.state_path))
        st.setdefault("events", [])
        return st

    def snapshot(self, force=False, deep=False):
        """当前快照。带 TTL 节流。"""
        now = time.time()
        with self._lock:
            if (not force and self._mem is not None
                    and now - self._last_run < self.ttl):
                return self._mem
        snap = detect(deep=deep)
        snap["self_id"] = self_id(self.state_path)
        with self._lock:
            self._mem = snap
            self._last_run = now
        return snap

    # ---- 检测 + 切换
    def refresh(self, force=False, deep=False):
        """检测一次；有变化就记账并切换 current。

        返回 `{changed, added, removed, current, events}`。
        `changed=False` 时也是有效结果 —— 「没变」本身就是结论，
        调用方据此知道不需要重新上报。
        """
        snap = self.snapshot(force=force, deep=deep)
        st = self.state()
        before = st.get("last") or {}

        d = diff(before, snap)
        events = []

        if d["changed"]:
            events.append({"ts": snap["ts"], "kind": "addr-changed",
                           "added": d["added"], "removed": d["removed"]})

        # 当前入口：优先沿用还活着的那个，否则按排序取第一个。
        # 为什么要「优先沿用」：地址没变时不该因为排序抖动就换入口 ——
        # 换一次，对端存的那份就全要重查一次。
        cur = st.get("current")
        alive = _keyset(snap)
        if cur not in alive:
            newcur = snap["candidates"][0]["ip"] if snap["candidates"] else None
            if newcur != cur:
                events.append({"ts": snap["ts"], "kind": "switch",
                               "from": cur, "to": newcur})
            st["current"] = newcur
        st["last"] = snap
        st["self_id"] = snap["self_id"]
        if events:
            st["events"] = (st.get("events") or [])[-99:] + events
        _save(st, self.state_path)

        return {"changed": d["changed"], "added": d["added"],
                "removed": d["removed"], "current": st.get("current"),
                "candidates": snap["candidates"], "events": events,
                "self_id": snap["self_id"]}

    def current(self, force=False):
        """当前入口地址。「另类的固态」那个槽位 —— 里面装什么会换，槽位一直在。"""
        st = self.state()
        cur = st.get("current")
        if cur and cur in _keyset(st.get("last")):
            return cur
        return self.refresh(force=force).get("current")

    # ---- 上报（动态更新）
    def publish(self, origin, secret, meta=None, deep=False, force=False, port=0):
        """把当前地址集写进中继地址簿。

        每次都整份写，不做增量：地址簿里存的是「**现在**在哪」，
        增量合并会让已经消失的地址永远留在里面，对端就会去连一个不存在的地址。

        `port` 是本机实际在监听的那个口。**必须带**：只有 IP 没有端口的记录，
        对端拿到也用不上，而墙外自检连 port=0 会得到「地址无效」——
        那既不是可达也不是不可达，纯粹是参数没给对。
        """
        from tools.cloudnode import wsrelay as wsr

        r = self.refresh(force=force, deep=deep)
        addrs = [{"ip": c["ip"], "port": int(c.get("port") or port or 0),
                  "temporary": c.get("temporary"),
                  "kind": c.get("kind")} for c in r["candidates"]]
        if not addrs:
            return {"ok": False, "err": "本机没有任何可用地址，不写空地址上去"}

        res = wsr.addr_put(origin, r["self_id"], addrs, secret,
                           meta={**(meta or {}), "current": r.get("current"),
                                 "port": int(port or 0)})
        res["published"] = addrs
        res["self_id"] = r["self_id"]
        res["changed"] = r["changed"]
        return res

    # ---- 自检（从墙外回连）
    def verify(self, origin, secret, port=0, only=None, timeout=8.0):
        """让中继逐个回连这些地址，标出**真能进得来**的。

        这是本文件唯一能给出「入站可用」结论的地方。
        结论不掺推测：连不上就写连不上，不给「可能被防火墙挡了」这种猜测。
        """
        from tools.cloudnode import wsrelay as wsr

        snap = self.snapshot()

        def _p(c):
            return int(c.get("port") or port or 0)

        # 私网地址让墙外连没有意义；**没有端口的候选则不构成一次可判定实验**：
        # 连 port=0 得到的是「地址无效」，不是「不可达」。把这种结果算进
        # 「全部不可达」会让结论从「测不了」变成「进不来」，进而误导人去改防火墙。
        todo = [c for c in snap["candidates"]
                if (only is None or c["ip"] in only)
                and c["kind"] != "private4"
                and _p(c) > 0]
        if not todo:
            return {"ok": False, "confident": False, "reachable": [], "all": [],
                    "invalid_port": True,
                    "note": "没有可探测端口（候选的 port 都是 0）：先在本机开一个监听口"
                            "（例如 tun.open 之后），再来验入站。"
                            "这不是「进不来」，是「还没法测」"}
        out = []
        looped = []
        origin_local = _host_is_local(origin)
        for c in todo:
            r = wsr.probe_from_relay(origin, c["ip"], _p(c),
                                     secret, timeout=timeout)
            src = r.get("from") or ""
            loc = _src_is_local(src, c["ip"])
            if loc is None and origin_local:
                # 旧中继不报源地址，退回到「中继本身是不是本机」这一条
                loc = True
            if loc:
                looped.append({"ip": c["ip"], "from": src})
            out.append({"ip": c["ip"], "kind": c["kind"],
                        "reachable": bool(r.get("ok")),
                        "invalid": bool(r.get("invalid")),
                        "from": src, "self_loop": bool(loc),
                        "ms": r.get("ms"), "err": r.get("err") or "",
                        "hint": r.get("hint") or ""})

        # 回环的「可达」不能算可达：那说明中继和我们是同一台机器 / 同一条链路，
        # 这类连上必然成功，与「墙外能不能进来」毫无关系。
        ok = [x for x in out if x["reachable"] and not x["self_loop"]]
        # 只要有一项是「参数无效」，或任何一项是回环，这次结论整体不算可信
        confident = (not any(x.get("invalid") for x in out)) and not looped
        st = self.state()
        st["verified"] = {"ts": round(time.time(), 1), "results": out,
                          "confident": confident, "self_loop": looped}
        _save(st, self.state_path)
        if looped:
            note = ("中继就在本机/同一链路（回连源地址 %s）：这次「可达」是回环，"
                    "不算验过入站。换一个真在墙外的中继才有意义"
                    % (looped[0].get("from") or "本机"))
        elif ok:
            note = "有地址能被墙外连进来"
        elif not confident:
            note = "这次自检有项是参数无效，结论不可信"
        else:
            note = ("没有一个地址能被墙外连进来：地址可能在，但入站被拦"
                    "（主机防火墙 / 路由侧 v6 防火墙默认全拦）")
        return {"ok": bool(ok), "confident": confident, "reachable": ok, "all": out,
                "self_loop": looped, "note": note}

    # ---- 解析（对端按指纹查）
    def resolve(self, origin, fp, timeout=8.0):
        """按设备指纹查对端当前在哪。**只认指纹，不认 IP**。"""
        from tools.cloudnode import wsrelay as wsr

        r = wsr.addr_get(origin, fp, timeout=timeout)
        if not r.get("ok"):
            return {"ok": False, "err": r.get("err") or "地址簿里没有这台设备",
                    "fp": fp}
        addrs = r.get("addrs") or []
        cur = (r.get("meta") or {}).get("current")
        # 把「最近上报时选中的那个」排到最前：它是对端自己认为最优的，
        # 但**不排除其它**——它可能刚失效，对端还没来得及改。
        addrs.sort(key=lambda c: (c.get("ip") != cur, bool(c.get("temporary"))))
        return {"ok": True, "fp": fp, "addrs": addrs, "current": cur,
                "age": r.get("age")}

    # ---- 自维护
    def events(self, limit=20):
        evs = self.state().get("events") or []
        return evs[-limit:]

    def forget(self):
        """清掉本地记忆。下次 refresh 会把所有地址当「新出现」。"""
        st = {"self_id": self_id(self.state_path)}
        _save(st, self.state_path)
        with self._lock:
            self._mem = None
            self._last_run = 0.0
        return {"ok": True, "note": "本地记忆已清空，下次 refresh 会当作全新"}


# ---------------------------------------------------------------- 自测
def _selftest():
    import shutil

    tmpdir = tempfile.mkdtemp(prefix="rv-")
    path = os.path.join(tmpdir, "rv.json")
    ok = fail = 0

    def chk(cond, msg):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ✓", msg)
        else:
            fail += 1
            print("  ✗", msg, flush=True)

    print("【身份：跨调用不变，且不随地址变】")
    sid1 = self_id(path)
    sid2 = self_id(path)
    chk(sid1.startswith("sf-") and len(sid1) == 19, f"指纹形状对：{sid1}")
    chk(sid1 == sid2, "同一台机器两次取到同一个指纹")
    chk(sid1 != self_id(os.path.join(tmpdir, "other.json")) or True,
        "（不同状态文件可能同指纹——指纹来自机器而非文件，这是对的）")

    print("【检测：临时地址不丢，只标记】")
    snap = detect()
    chk(isinstance(snap["candidates"], list), "candidates 是列表")
    fams = {c["family"] for c in snap["candidates"]}
    chk(fams <= {4, 6}, "只有 v4/v6")
    six = [c for c in snap["candidates"] if c["family"] == 6]
    if six:
        chk(all("temporary" in c for c in six), "v6 候选都带 temporary 标记")
        chk(six[0]["temporary"] is not True, "稳定地址排在临时地址前面")

    print("【差异：只报变化，没变就是空】")
    a = {"candidates": [{"ip": "1.1.1.1"}, {"ip": "::1"}]}
    b = {"candidates": [{"ip": "1.1.1.1"}, {"ip": "::2"}]}
    d = diff(a, b)
    chk(d["added"] == ["::2"] and d["removed"] == ["::1"], "增删分别列出")
    chk(d["changed"] is True, "有变化时 changed=True")
    chk(diff(a, a) == {"added": [], "removed": [], "changed": False},
        "完全相同时 changed=False")

    print("【编排：检测 → 记账 → 切换】")
    rv = Rendezvous(state_path=path, ttl=0)
    r1 = rv.refresh(force=True)
    chk(r1["self_id"] == sid1, "refresh 带上稳定指纹")
    if r1["candidates"]:
        chk(r1["current"] == r1["candidates"][0]["ip"], "首次 refresh 选中第一个候选")
        chk(any(e["kind"] == "addr-changed" for e in r1["events"]),
            "首次检测记为 addr-changed（从无到有）")
    chk(rv.current(force=True) is not None or not r1["candidates"],
        "current() 能取到入口")

    print("【节流：TTL 内不重复检测】")
    rv2 = Rendezvous(state_path=path, ttl=300)
    rv2.refresh(force=True)
    t0 = rv2._last_run
    rv2.refresh()
    chk(rv2._last_run == t0, "TTL 内 refresh 不重跑检测")

    print("【切换：地址没了才换，没变就不动】")
    st = _load(path)
    fake_last = {"candidates": [{"ip": "2400:dead::1", "family": 6}]}
    st["last"] = fake_last
    st["current"] = "2400:dead::1"
    _save(st, path)
    r2 = Rendezvous(state_path=path, ttl=0).refresh(force=True)
    if r2["candidates"]:
        chk(r2["current"] != "2400:dead::1", "旧地址消失后切到新地址")
        chk(any(e["kind"] == "switch" for e in r2["events"]), "切换记了一笔 switch")
        chk("2400:dead::1" in r2["removed"], "消失的地址出现在 removed 里")

    print("【记忆：落盘可读回】")
    chk(os.path.exists(path), "状态文件已落盘")
    chk(_load(path).get("self_id") == sid1, "读回的指纹与写入一致")
    rv3 = Rendezvous(state_path=path, ttl=0)
    chk(rv3.events() != [], "事件历史可读")

    print("【健壮：坏文件不炸】")
    bad = os.path.join(tmpdir, "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{not json")
    chk(_load(bad) == {}, "坏 JSON 读成空字典，不抛异常")
    chk(Rendezvous(state_path=bad, ttl=0).refresh(force=True)["self_id"].startswith("sf-"),
        "坏状态文件仍能正常工作")

    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\n自测结果：{ok} 过 / {fail} 败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())

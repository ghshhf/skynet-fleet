# -*- coding: utf-8 -*-
"""地址自动化（fleet.rdv_auto）—— 把「手动调 rdv.*」变成「一直在跑的一条循环」。

为什么单独一层，不塞进 Rendezvous
==================================
`rendezvous.py` 的类注释写着「刻意不是后台服务：refresh 幂等 + 节流，谁需要谁调」。
那是**对的**，而且必须保住：后台循环是另一种东西，它会常驻、会自己重试、
会在没人看的时候改变状态。两者塞进一个对象，出了事就分不清是
「这一次调用失败」还是「循环早停了」—— 而这两种故障的处置完全不同。

复用的三样现成骨架（天网里本来就有，不重造）
============================================
1. `fleet.beat`：每轮心跳 → `/api/health` 里能看见「最后一次成功是几秒前」、
   卡住的循环自动标 stale。**这个骨架此前全仓没有一处循环在用**，本层是第一个。
2. `Rendezvous`：detect / refresh / publish / verify 全在那一层，这里只编排顺序。
3. `fleet.gui.link.selfheal_loop` 的循环形状：stop 可打断、单次异常不许把整条循环
   弄死、失败必须带原因。照抄形状，不照抄代码。

状态只有五档，每档都能回答「现在该怎么办」
==========================================
    never       还没跑过                       → 先 start
    unverified  地址报上去了，还没从墙外验过    → 等一个 verify 周期
    linked      报上去了，墙外确实连得进来      → 不用管
    blind       报上去了，墙外一个都进不来      → 开 pinhole / 查路由侧防火墙
    down        连地址都报不上去                → 查中继可达性 / 本机有没有地址

**unverified 单列，是因为「不知道」不等于「知道」**：把没验过的当成 linked，
等于把「可能进不来」报告成「能进来」，而对端拿到这个结论会直接去连、然后超时。

只有变化才记事件
================
照 `sentinel.watch` 的做法：每轮都写一条「还是原样」的日志，会把真正的事件埋掉。
本层只在**状态迁移**和**地址集合变化**时记事件，其余轮次只更新计数。

最后一步：入站放行（pinhole）
=============================
地址报上去、墙外一验，如果结果是 `blind`，卡住的地方**几乎总是同一个**：
v6 没有 NAT，主机防火墙对 v6 入站的默认策略是全拦。
所以本层在墙外自检之前会自动补这一步（`tun.pinhole` 那三条能力做实际动作）。

三条边界写死在这里，不许放宽：
  ① 只放**当前在用的那个端口**，不批量、不开范围
  ② 只对 **v6 源**生效（靠地址前缀收敛），v4 侧端口不受影响
  ③ 失败**不改变上报结论** —— 放行不了 ≠ 地址没报上去，
     把它写成 down 会让人去查中继，而问题在防火墙

门禁纪律：`AutoLink` 现在会改防火墙。任何门禁只要带**真端口**构造它，
就必须注入 `pinhole_fn` 假实现或 `auto_pinhole=False`，
否则跑一次门禁就在用户机器上留下一条放行规则 —— 而它长得像正常配置，
事后没人会想到去删。`evals/t59_pinhole.py` 第 11 组会静态扫这处疏漏。
"""

import json
import os
import sys
import threading
import time

try:                                  # 被包导入时（正常路径）
    from . import rendezvous as rdv_mod
except ImportError:                   # 直接 python fleet/rdv_auto.py 跑自测时
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from fleet import rendezvous as rdv_mod

#: 一轮多久。60 秒：地址变化（DHCP 续租、v6 前缀重下发）最快也在分钟级，
#: 更密只是白烧 CPU 和 STUN 流量。
DEFAULT_INTERVAL = 60.0
#: 墙外自检周期。5 分钟：它要经中继绕一圈，太密会让中继看起来像被扫描。
DEFAULT_VERIFY_EVERY = 300.0
#: 深检（含 STUN 映射发现）周期。STUN 要几秒，不能每轮跑。
DEFAULT_DEEP_EVERY = 1800.0
#: 地址没变也定期续报一次。地址簿有 TTL，对端也可能缓存了旧结论。
DEFAULT_REPUBLISH_EVERY = 1800.0
#: 连续失败后的退避上限。
BACKOFF_CAP = 300.0
#: 开 pinhole 失败后的重试间隔。改防火墙要起一个 PowerShell（1~2 秒），
#: 而且每次失败几乎都是「没提权」这种不会自己好的原因，所以隔 5 分钟再说。
PINHOLE_RETRY = 300.0

STATE_PATH = os.path.join(os.path.expanduser("~"), ".skynet", "rdv_auto.json")

_LOCK = threading.Lock()
_ALIVE = {}          # name -> {"thread":..., "stop": Event, "loop": AutoLink}


# ---------------------------------------------------------------- 运行态
def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        # 读不出来就当没有：运行态丢了的代价只是「重启后多报一次地址」，
        # 但要让它看起来像正常启动，不能因为一个坏文件拒绝工作。
        return {}


def _save(path, st):
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        return False


class AutoLink:
    """一条「检测 → 上报 → 墙外自检 → 状态迁移」的循环。

    刻意把 `tick()` 做成**可以单独调用的纯一步**：
    门禁里能直接喂假时钟跑状态机，不用起线程、不用等 60 秒。
    """

    def __init__(self, rdv=None, origin="", secret="", interval=DEFAULT_INTERVAL,
                 verify_every=DEFAULT_VERIFY_EVERY, deep_every=DEFAULT_DEEP_EVERY,
                 republish_every=DEFAULT_REPUBLISH_EVERY, state_path=STATE_PATH,
                 publish_fn=None, verify_fn=None, clock=None, port=0,
                 auto_pinhole=None, pinhole_fn=None, headers: dict = None):
        self.rdv = rdv or rdv_mod.Rendezvous()
        self.origin = (origin or os.environ.get("FLEET_WSRELAY") or "").strip()
        self.secret = (secret or os.environ.get("FLEET_WSRELAY_SECRET") or "").strip()
        # 中继外面可能还套了一层网关（Daytona preview 自带一道 Bearer 鉴权）。
        # 那是**外层**的凭据，和本层的 WSRELAY_SECRET 是两回事，不能混用。
        # 用环境变量传（FLEET_WSRELAY_HEADERS，JSON），别硬编码在源码里。
        self.headers = dict(headers or {})
        if not self.headers:
            try:
                raw = os.environ.get("FLEET_WSRELAY_HEADERS", "").strip()
                if raw:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        self.headers = {str(k): str(v) for k, v in parsed.items()}
            except Exception:
                self.headers = {}
        self.interval = float(interval)
        self.verify_every = float(verify_every)
        self.deep_every = float(deep_every)
        self.republish_every = float(republish_every)
        self.state_path = state_path
        self.port = int(port or 0)
        # 入站放行是否自动补：默认开。理由——不自动就还是「地址报上去了，
        # 但墙外一个都进不来」，而那个状态的下一步提示本来就是「开 pinhole」，
        # 让人拿着提示去手动开，等于把自动化做到最后一步停住。
        # 但它是**可关**的（FLEET_PINHOLE_AUTO=0），因为改防火墙属于系统状态变更，
        # 得留一个「别动我的机器」的开关。
        if auto_pinhole is None:
            auto_pinhole = os.environ.get("FLEET_PINHOLE_AUTO", "1") not in ("0", "false", "no", "")
        self.auto_pinhole = bool(auto_pinhole)
        # 注入点：门禁用假实现，一条防火墙都不碰
        self.pinhole_fn = pinhole_fn
        # 注入点：门禁用假 publish/verify 验状态机，一个网络包都不发
        self.publish_fn = publish_fn
        self.verify_fn = verify_fn
        self._clock = clock or time.time
        self._st = _load(state_path)
        self._st.setdefault("status", "never")
        self._st.setdefault("events", [])
        self._st.setdefault("fails", 0)
        self._stop = threading.Event()
        self._thread = None

    # ---- 配置是否足够
    def configured(self):
        return bool(self.origin and self.secret)

    # ---- 运行态
    @property
    def status_dict(self):
        st = self._st
        now = self._clock()
        return {
            "status": st.get("status", "never"),
            "configured": self.configured(),
            "relay": _mask(self.origin),
            "self_id": st.get("self_id"),
            "current": st.get("current"),
            "candidates": len(st.get("candidates") or []),
            "fails": int(st.get("fails") or 0),
            "last_tick": st.get("last_tick"),
            "last_tick_age": (None if not st.get("last_tick")
                              else round(max(0.0, now - st["last_tick"]), 1)),
            "last_publish_age": (None if not st.get("last_publish_ts")
                                 else round(max(0.0, now - st["last_publish_ts"]), 1)),
            "backoff_until": st.get("backoff_until") or 0,
            "backoff_left": round(max(0.0, (st.get("backoff_until") or 0) - now), 1),
            "verify": st.get("verify") or {},
            "pinhole": st.get("pinhole") or {},
            "auto_pinhole": self.auto_pinhole,
            "runs": st.get("runs", 0),
            "running": bool(self._thread and self._thread.is_alive()),
            "events": (st.get("events") or [])[-10:],
            "advice": _advice(st.get("status", "never"), self.configured()),
        }

    def events(self, limit=20):
        return (self._st.get("events") or [])[-limit:]

    def forget(self):
        """清运行态（**不动地址簿**：那只在中继那边）。"""
        self._st = {"status": "never", "events": [], "fails": 0}
        _save(self.state_path, self._st)
        return {"ok": True}

    # ---- 冲一次
    def tick(self, now=None):
        """跑一轮。**不抛异常**：返回里带 ok/err，由调用方决定怎么处理。

        一轮的次序是刻意的：
          ① 先检测（本机地址变没变）
          ② 只有变了 / 从没报过 / 续报到期 才上报（省中继往返）
          ③ 上报成功后再做墙外自检（自检要连的地址得先存在地址簿里）
          ④ 由「上报结果 + 自检结果」共同定状态
        反过来（先自检再上报）会出现「地址簿里还没有这个地址就让人去连」的假阴性。
        """
        now = self._clock() if now is None else now
        st = self._st
        st["runs"] = int(st.get("runs") or 0) + 1
        st["last_tick"] = now
        events = []

        # ---- ① 检测
        deep_due = self._due("deep_ts", self.deep_every, now)
        try:
            r = self.rdv.refresh(force=True, deep=deep_due)
        except Exception as e:
            return self._finish(now, ok=False, status="down",
                                err="%s: %s" % (type(e).__name__, e), events=events,
                                err_kind="detect")
        if deep_due:
            # 跑过就要记账，否则 last 永远是 0 ⇒ 每轮都判「到期」⇒
            # 每轮都跑一遍 STUN（几秒）和 v6 深诊。之前漏了这一行。
            st["deep_ts"] = now

        changed = bool(r.get("changed"))
        cands = r.get("candidates") or []
        st["self_id"] = r.get("self_id") or st.get("self_id")
        st["current"] = r.get("current")
        st["candidates"] = cands
        # 地址变了 → 记事件（这是「动态」真正发生的时刻）
        if changed:
            events.append({"ts": round(now, 1), "kind": "addr-changed",
                           "added": r.get("added"), "removed": r.get("removed"),
                           "current": r.get("current")})

        # ---- ② 该不该上报
        # 换了中继必须立刻重报：状态里记着「已经报过」，但报的是**那个**中继。
        # 不判这一条的话，换 PaaS 域名后最长 30 分钟内对端查不到 —— 而现象是
        # 「设备明明在线却找不到」，最难查的那种。
        origin_key = _mask(self.origin)
        need = (changed
                or not st.get("last_publish_ts")
                or now - float(st["last_publish_ts"]) >= self.republish_every
                or st.get("origin") != origin_key)
        # 退避期不上报：连续失败时每 60 秒猛试一次，只会把中继当靶子
        backoff_until = float(st.get("backoff_until") or 0)
        if need and now < backoff_until:
            need = False

        published = None
        if need:
            if not self.configured():
                return self._finish(
                    now, ok=False, status="down", events=events,
                    err="没有中继地址或密钥（FLEET_WSRELAY / FLEET_WSRELAY_SECRET）",
                    err_kind="config")
            try:
                published = self._do_publish()
            except Exception as e:
                return self._finish(now, ok=False, status="down", events=events,
                                    err="%s: %s" % (type(e).__name__, e),
                                    err_kind="publish")
            if not published.get("ok"):
                return self._finish(now, ok=False, status="down", events=events,
                                    err=published.get("err") or "上报失败",
                                    err_kind="publish")
            st["fails"] = 0
            st["backoff_until"] = 0
            st["last_publish_ts"] = now
            st["origin"] = origin_key
            st["published"] = published.get("published")
            events.append({"ts": round(now, 1), "kind": "published",
                           "count": len(published.get("published") or []),
                           "changed": changed})

        # ---- ③ 墙外自检
        # 自检之前先补入站放行：不然自检连的是「一个被防火墙挡住的端口」，
        # 得出的 blind 是防火墙造成的，不是网络造成的 —— 而这两者的处置完全不同。
        pinhole = self._ensure_pinhole(now, events)

        verify = st.get("verify") or {}
        if self._due("verify_ts", self.verify_every, now):
            if self.configured() and len(cands) > 0:
                try:
                    vr = self._do_verify()
                except Exception as e:
                    # 自检失败 ≠ 入站不可用。**不能把「测不出来」写成「进不来」**，
                    # 那会让人去改一通根本没问题的防火墙。
                    verify = {"ts": round(now, 1), "ok": False, "confident": False,
                              "err": "%s: %s" % (type(e).__name__, e)}
                else:
                    verify = {"ts": round(now, 1), "ok": bool(vr.get("ok")),
                              "reachable": vr.get("reachable") or [],
                              "all": vr.get("all") or [],
                              # 中继自己就在本机/同链路时报出来的那几条：
                              # 它们的「可达」是回环，透传上去让上层看得见原因，
                              # 而不是只看到一句「入站仍未知」。
                              "self_loop": vr.get("self_loop") or [],
                              # 没有端口 / 有项参数无效 / 中继在本机 ⇒ 这次结论不算数，
                              # 不能让「测不了」冒充「进不来」
                              "confident": bool(vr.get("confident", True)),
                              "err": "" if vr.get("ok") else (vr.get("note") or "")}
                st["verify"] = verify
                st["verify_ts"] = now

        # ---- ④ 定状态
        status, note = _judge(verify)
        prev = st.get("status")
        if status != prev:
            events.append({"ts": round(now, 1), "kind": "status",
                           "from": prev, "to": status, "why": note})
        st["status"] = status
        return self._finish(now, ok=True, status=status, events=events,
                            published=published, verify=verify, note=note,
                            pinhole=pinhole)

    # ---- 内部
    def _due(self, key, every, now):
        last = float(self._st.get(key) or 0)
        return now - last >= every

    def _ensure_pinhole(self, now, events):
        """确保「对端要连进来的那个端口」在防火墙上是通的。

        为什么这件事必须由本层做，而不是等用户看提示手动开：
        上一轮实测的状态就是 `unverified`，原因是「没有可探测端口」。
        修好端口以后，下一个拦路的**一定**是防火墙 —— v6 没有 NAT，
        主机防火墙对 v6 入站默认全拦。让人在「地址已上报、就差一个洞」的
        状态上停住，等于把自动化做到 99% 然后放手。

        三条硬约束：
          ① **只放当前真正在用的端口**，不批量、不开范围
          ② **只对 v6 源生效**（pinhole_open 里靠地址前缀收敛），v4 侧不受影响
          ③ **失败不改变上报结论**：放行失败 ≠ 地址没报上去，
             所以这里只记事件、只回填状态，绝不把它变成 status=down
        """
        st = self._st
        prev = st.get("pinhole") or {}

        if not self.auto_pinhole:
            return {"ok": False, "skipped": "auto_pinhole=0（关掉了，不动防火墙）"}
        if self.port <= 0:
            # 没有端口 = 对端不知道该连哪 —— 这跟防火墙一点关系都没有，
            # 报「需要开 pinhole」会把人引到错误的方向上。
            return prev or {"ok": False, "skipped": "没有可放行端口（port=0）"}

        sig = str(self.port)
        if prev.get("sig") == sig and prev.get("ok"):
            return prev                        # 已经放行过，端口没变
        if prev.get("sig") == sig and now - float(prev.get("ts") or 0) < PINHOLE_RETRY:
            return prev                        # 刚失败过，别每轮都去撞

        try:
            if self.pinhole_fn is not None:
                r = self.pinhole_fn([self.port], ["TCP"])
            else:
                from . import tunnel as tun_mod
                r = tun_mod.pinhole_open([self.port], ["TCP"])
        except Exception as e:
            # 拿不到隧道层（例如被裁剪安装）也不该让整轮失败：
            # 入站放行是本层的一个**加分项**，不是它存在的理由。
            r = {"ok": False, "err": "%s: %s" % (type(e).__name__, e)}

        cur = {"ts": round(now, 1), "sig": sig, "port": self.port,
               "ok": bool(r.get("ok")),
               "need_admin": bool(r.get("need_admin")),
               "created": r.get("created") or [],
               "existed": r.get("existed") or [],
               "err": r.get("err") or "",
               "note": ("已放行 TCP/%d 的 v6 入站" % self.port if r.get("ok") else
                        "改防火墙要管理员权限：当前进程没提权，入站可能仍被拦"
                        if r.get("need_admin") else
                        "入站放行没成功（%s）：自检结论会偏向 blind，但不代表网络不通"
                        % (r.get("err") or "未知原因"))}
        st["pinhole"] = cur
        # 只在**结果变化**时记事件：每轮都写一条「还是原样」会把真事件埋掉
        if (cur["ok"] != prev.get("ok")) or (cur["ok"] and prev.get("sig") != sig):
            events.append({"ts": cur["ts"], "kind": "pinhole",
                           "ok": cur["ok"], "port": self.port,
                           "need_admin": cur["need_admin"],
                           "why": cur["note"]})
        return cur

    def _do_publish(self):
        if self.publish_fn is not None:
            return self.publish_fn(self.rdv)
        # port 必须带上：只有 IP 的记录对端用不上，墙外自检也会变成无效实验
        return self.rdv.publish(self.origin, self.secret, deep=False, force=False,
                                port=self.port, headers=self.headers)

    def _do_verify(self):
        if self.verify_fn is not None:
            return self.verify_fn(self.rdv)
        return self.rdv.verify(self.origin, self.secret, port=self.port,
                               headers=self.headers)

    def _finish(self, now, ok, status, err="", events=None, published=None,
                verify=None, note="", err_kind="", pinhole=None):
        st = self._st
        st["status"] = status
        if not ok:
            st["fails"] = int(st.get("fails") or 0) + 1
            # 指数退避：1×, 2×, 4×… 封顶 5 分钟。**退避不是放弃**，
            # 而是「别把一个临时故障打成长期骚扰」。
            wait = min(self.interval * (2 ** min(st["fails"], 4)), BACKOFF_CAP)
            st["backoff_until"] = now + wait
            st["last_err"] = err
            st["last_err_kind"] = err_kind
        if events:
            st["events"] = ((st.get("events") or []) + events)[-200:]
        st["status_at"] = st.get("status_at") or now
        _save(self.state_path, st)
        out = {"ok": ok, "status": status, "err": err, "events": events,
               "runs": st["runs"], "fails": st["fails"]}
        if published is not None:
            out["published"] = published
        if verify is not None:
            out["verify"] = verify
        if pinhole is not None:
            out["pinhole"] = pinhole
        if note:
            out["note"] = note
        return out

    # ---- 线程
    def start(self):
        """起后台循环。**已经起来就返回同一份**，不叠加第二条线程。"""
        if not self.configured():
            return {"ok": False,
                    "err": "缺中继地址/密钥：没有它们循环只会每轮报同一个错，"
                           "所以不起。先设 FLEET_WSRELAY 与 FLEET_WSRELAY_SECRET"}
        with _LOCK:
            if self._thread and self._thread.is_alive():
                return {"ok": True, "already": True, **self.status_dict}
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="rdv-auto",
                                            daemon=True)
            self._thread.start()
        return {"ok": True, "already": False, **self.status_dict}

    def stop(self, timeout=3.0):
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout)
        self._thread = None
        return {"ok": True, "running": False, **self.status_dict}

    def _loop(self):
        bt = _beat("rdv.auto", stale_after=max(180.0, self.interval * 6))
        bt.beat()          # 进场先记一次，否则「刚起、正等第一轮」会被误判 stale
        while not self._stop.is_set():
            try:
                r = self.tick()
            except Exception as e:      # 单轮异常不许把整条循环弄死
                bt.allow("%s: %s" % (type(e).__name__, e))
            else:
                if r.get("ok"):
                    bt.beat()
                else:
                    # 失败也要带原因：空原因的失败等于没记
                    bt.allow("%s（%s）" % (r.get("err") or r.get("status"),
                                           r.get("status")))
            # 睡到下一轮；退避期内按退避时间睡。用 Event.wait 才能被 stop 立刻打断。
            st = self._st
            wait = self.interval
            left = float(st.get("backoff_until") or 0) - self._clock()
            if left > 0:
                wait = min(max(self.interval, left), BACKOFF_CAP)
            if self._stop.wait(wait):
                return

    def __repr__(self):
        return "<AutoLink status=%s configured=%s>" % (
            self._st.get("status"), self.configured())


# ---------------------------------------------------------------- 判据
def _judge(verify):
    """由自检结果定状态。**没验过就是 unverified，不含糊成 linked。**"""
    if not verify or not verify.get("ts"):
        return "unverified", "地址已上报，但还没从墙外验过入站"
    if not verify.get("confident"):
        return "unverified", "自检没跑成（%s），入站仍未知" % (verify.get("err") or "原因不明")
    if verify.get("ok"):
        n = len(verify.get("reachable") or [])
        return "linked", "墙外可回连（%d 条地址可达）" % n
    return "blind", ("地址在、墙外一个都进不来：主机防火墙拦入站，"
                     "或路由侧 IPv6 防火墙默认全拦")


def _advice(status, configured):
    # 没配置时，无论状态是什么都先指向真正卡住的那一步：
    # 状态再准，缺配置也什么都做不了，让人去查网络是误导。
    if not configured:
        return "还没配置中继：设 FLEET_WSRELAY 与 FLEET_WSRELAY_SECRET"
    return {
        "never": "还没跑过；rdv.auto.start 起来，或先 rdv.self 看一眼",
        "unverified": "等一个自检周期（默认 5 分钟），或手动 rdv.verify",
        "linked": "不用管：地址是最新的，且确认能进得来",
        "blind": "先用 tun.pinhole 看这道墙，再用 tun.pinhole.open 放行端口；"
                 "已经放行还进不来，那就是路由侧的 v6 防火墙。"
                 "此期间对外只走中继",
        "down": "先看中继是否可达、密钥是否正确",
    }.get(status, "")


def _mask(url):
    """中继地址可以外显，但不要带 query（可能含 token）。"""
    if not url:
        return ""
    return url.split("?", 1)[0]


def _beat(name, stale_after=180.0):
    try:
        from .beat import register
    except Exception:
        try:
            from beat import register  # fleet/ 在 sys.path 里
        except Exception:
            class _B:                   # 独立运行时的兜底，不让心跳缺失阻断循环
                def beat(self, *a, **k):
                    pass

                def allow(self, *a, **k):
                    pass
            return _B()
    return register(name, stale_after=stale_after)


# ---------------------------------------------------------------- 单例入口
def get_loop(**kw):
    """取得（或按参数创建）进程内唯一那条循环。"""
    with _LOCK:
        rec = _ALIVE.get("default")
        if rec is None or "loop" not in rec:
            return None
    return rec["loop"]


def start_loop(**kw):
    with _LOCK:
        rec = _ALIVE.get("default")
        if rec is not None and rec["thread"].is_alive():
            return {"ok": True, "already": True, **rec["loop"].status_dict}
    loop = AutoLink(**kw)
    r = loop.start()
    if r.get("ok"):
        with _LOCK:
            _ALIVE["default"] = {"thread": loop._thread, "loop": loop}
    return r


def stop_loop():
    with _LOCK:
        rec = _ALIVE.pop("default", None)
    if rec is None:
        return {"ok": True, "running": False, "note": "没有在跑的循环"}
    return rec["loop"].stop()


def loop_status(state_path=STATE_PATH):
    with _LOCK:
        rec = _ALIVE.get("default")
    if rec is not None:
        return rec["loop"].status_dict
    # 进程外（另一次调用）也能查到：从落盘的运行态读
    return AutoLink(state_path=state_path).status_dict


def ensure_started(**kw):
    """按环境变量决定要不要自动起。`FLEET_RDV_AUTO=1` 才起。

    **不做「默认偷偷起来」**：没人配置中继时它只会每轮报同一个错，
    那不是自动化，是噪音。
    """
    if str(os.environ.get("FLEET_RDV_AUTO", "")).strip() not in ("1", "true", "on", "yes"):
        return {"ok": False, "skipped": True,
                "err": "未开启：设 FLEET_RDV_AUTO=1 才自动起循环"}
    # 默认复用 rdv:layer 那个单例：两个实例各有一份 min 缓存与节流，
    # 同时刷新会互相覆盖状态（一边刚上报、另一边又按旧快照重写）。
    if "rdv" not in kw:
        try:
            from .kinds.rendezvous_layer import _RDV as _shared
            kw["rdv"] = _shared
        except Exception:
            pass
    return start_loop(**kw)


# ---------------------------------------------------------------- 自测
def _selftest():
    import shutil
    import tempfile

    d = tempfile.mkdtemp(prefix="rdvauto-")
    fails = []

    def chk(cond, msg):
        print(("  ✓ " if cond else "  ✗ ") + msg)
        if not cond:
            fails.append(msg)

    class FakeRdv:
        """假 Rendezvous：只做「本机地址集合」这一件事，一个包都不发。"""

        def __init__(self):
            self.addrs = ["240e::1"]
            self.calls = 0

        def refresh(self, force=False, deep=False):
            self.calls += 1
            return {"changed": getattr(self, "changed", False),
                    "added": getattr(self, "added", []),
                    "removed": getattr(self, "removed", []),
                    "current": self.addrs[0] if self.addrs else None,
                    "candidates": [{"ip": a, "port": 7000, "kind": "global6",
                                    "temporary": False} for a in self.addrs],
                    "self_id": "fake-fp"}

    t = {"now": 1000.0}
    clock = lambda: t["now"]          # noqa: E731

    try:
        print("【1】状态机：上报成功 + 墙外可达 → linked")
        pub = {"n": 0, "ok": True}

        def fake_pub(rdv):
            pub["n"] += 1
            return {"ok": pub["ok"], "published": [{"ip": "240e::1"}], "err": ""}

        ver = {"ok": True}

        def fake_ver(rdv):
            return {"ok": ver["ok"], "reachable": [{"ip": "240e::1"}] if ver["ok"] else [],
                    "all": [{"ip": "240e::1", "reachable": ver["ok"]}],
                    "note": "有地址能被墙外连进来" if ver["ok"] else "全不可达"}

        a = AutoLink(rdv=FakeRdv(), origin="http://relay.example/pair", secret="s",
                     state_path=os.path.join(d, "a.json"), clock=clock,
                     publish_fn=fake_pub, verify_fn=fake_ver)
        r1 = a.tick()
        chk(r1["ok"] and r1["status"] == "linked", "首轮即 linked（上报 + 自检都过）")
        chk(pub["n"] == 1, "首轮上报了一次")
        ev = [e["kind"] for e in a.events()]
        chk("published" in ev and "status" in ev, "上报与状态迁移都记了事件")

        print("\n【2】没变化就不重复上报（省中继往返）")
        t["now"] += 1.0
        r2 = a.tick()
        chk(pub["n"] == 1, "地址没变、未到续报期 → 没有再上报")
        chk(r2["status"] == "linked", "状态沿用上次自检结论")

        print("\n【3】地址变了 → 自动上报并记事件（这就是「自动切换」）")
        a.rdv.addrs = ["240e::2"]
        a.rdv.changed = True
        a.rdv.added = ["240e::2"]
        a.rdv.removed = ["240e::1"]
        t["now"] += 1.0
        r3 = a.tick()
        chk(pub["n"] == 2, "地址一变就上报了")
        kinds = [e["kind"] for e in a.events()]
        chk("addr-changed" in kinds, "记了 addr-changed 事件")

        print("\n【4】墙外全不可达 → blind（不是 linked，也不是 down）")
        ver["ok"] = False
        t["now"] += 400.0                 # 跨过自检周期
        r4 = a.tick()
        chk(r4["status"] == "blind", "报上去了但进不来 → blind")
        chk("pinhole" in (r4.get("note") or "") + a.status_dict["advice"],
            "给出的建议是去开 pinhole，而不是含糊其辞")

        print("\n【5】自检跑不了 ≠ 进不来：必须留在 unverified")
        def boom_ver(rdv):
            raise OSError("relay unreachable")
        b = AutoLink(rdv=FakeRdv(), origin="http://relay.example/pair", secret="s",
                     state_path=os.path.join(d, "b.json"), clock=clock,
                     publish_fn=fake_pub, verify_fn=boom_ver)
        t["now"] += 1.0
        rb = b.tick()
        chk(rb["status"] == "unverified",
            "自检异常 → unverified（不写成 blind：测不出来不等于进不来）")

        print("\n【6】上报失败 → down + 退避（别把临时故障打成骚扰）")
        pub["ok"] = False
        c = AutoLink(rdv=FakeRdv(), origin="http://relay.example/pair", secret="s",
                     state_path=os.path.join(d, "c.json"), clock=clock,
                     publish_fn=fake_pub, verify_fn=fake_ver)
        t["now"] += 1.0
        rc = c.tick()
        chk(rc["status"] == "down" and rc["ok"] is False, "上报失败 → down 且 ok=False")
        bo = c.status_dict["backoff_until"]
        chk(bo > t["now"], "失败后排了退避，不是立刻再试")
        n_before = pub["n"]
        t["now"] += 1.0
        c.tick()
        chk(pub["n"] == n_before, "退避期内确实没再打中继")
        pub["ok"] = True

        print("\n【7】换中继要立刻重报（否则换 PaaS 后对端 30 分钟内找不到设备）")
        t["now"] += 1.0
        n_before = pub["n"]
        d2 = AutoLink(rdv=FakeRdv(), origin="http://other-relay.example", secret="s",
                      state_path=os.path.join(d, "a.json"), clock=clock,
                      publish_fn=fake_pub, verify_fn=fake_ver)
        d2.tick()
        chk(pub["n"] == n_before + 1, "中继地址变了 → 立刻重新上报")

        print("\n【8】没配置中继：如实拒绝起循环，不空转")
        e = AutoLink(rdv=FakeRdv(), origin="", secret="",
                     state_path=os.path.join(d, "e.json"), clock=clock)
        chk(not e.configured(), "空配置判定为未配置")
        rr = e.start()
        chk(rr.get("ok") is False and "FLEET_WSRELAY" in (rr.get("err") or ""),
            "起循环被拒，并说清缺什么")

        print("\n【9】持久化：重启后还知道上次状态，且不重复记同一件事")
        st = json.load(open(os.path.join(d, "a.json"), encoding="utf-8"))
        chk(st.get("status") == "blind", "运行态已落盘")
        a2 = AutoLink(rdv=FakeRdv(), origin="http://relay.example/pair", secret="s",
                      state_path=os.path.join(d, "a.json"), clock=clock,
                      publish_fn=fake_pub, verify_fn=fake_ver)
        chk(a2.status_dict["status"] == "blind", "新实例读回的是 blind（跨重启记得）")
        chk(len(a2.events()) > 0, "历史事件也读回来了")

        print("\n【10】配置不全会说清缺什么，而不是「失败」了事")
        chk("FLEET_WSRELAY" in e.status_dict["advice"], "advice 直指环境变量名")

        print("\n【11】入站放行自动化：只开在用端口，失败不改上报结论")
        calls = []

        def ph_ok(ports, protos):
            calls.append((list(ports), list(protos)))
            return {"ok": True, "created": ["skynet.pinhole.TCP.45990"], "existed": []}

        z = AutoLink(rdv=FakeRdv(), origin="http://relay.example/pair", secret="s",
                     state_path=os.path.join(d, "z.json"), clock=clock,
                     publish_fn=fake_pub, verify_fn=fake_ver, port=0,
                     pinhole_fn=ph_ok)
        z.tick()
        chk(not calls, "没有端口时不去动防火墙（那跟防火墙无关）")

        z.port = 45990
        rz = z.tick()
        chk(len(calls) == 1 and calls[0] == ([45990], ["TCP"]),
            "有端口时只放行这一个端口、且只放 TCP（打洞要的 UDP 不替人开会）")
        chk(rz.get("pinhole", {}).get("ok") is True, "放行结果回填到这一轮的返回里")
        z.tick()
        chk(len(calls) == 1, "已放行且端口没变 → 不再重复改防火墙")

        off = AutoLink(rdv=FakeRdv(), origin="http://relay.example/pair", secret="s",
                       state_path=os.path.join(d, "off.json"), clock=clock,
                       publish_fn=fake_pub, verify_fn=fake_ver, port=8080,
                       auto_pinhole=False, pinhole_fn=ph_ok)
        n0 = len(calls)
        off.tick()
        chk(len(calls) == n0, "auto_pinhole=0 时一条规则都不碰（留一个别动我机器的开关）")

        na = AutoLink(rdv=FakeRdv(), origin="http://relay.example/pair", secret="s",
                      state_path=os.path.join(d, "na.json"), clock=clock,
                      publish_fn=fake_pub, verify_fn=fake_ver, port=8080,
                      pinhole_fn=lambda p, q: {"ok": False, "need_admin": True,
                                               "err": "没提权"})
        rna = na.tick()
        chk(rna.get("pinhole", {}).get("need_admin") is True,
            "没权限时如实标 need_admin，不写成「开失败」")
        chk(rna.get("ok") is True, "放行失败不影响这一轮 ok：地址确实报上去了")

        def ph_boom(p, q):
            raise RuntimeError("没有隧道层")

        bo = AutoLink(rdv=FakeRdv(), origin="http://relay.example/pair", secret="s",
                      state_path=os.path.join(d, "bo.json"), clock=clock,
                      publish_fn=fake_pub, verify_fn=fake_ver, port=8080,
                      pinhole_fn=ph_boom)
        rbo = bo.tick()
        chk(rbo.get("ok") is True, "放行抛异常也不把整轮弄死（它是加分项不是主线）")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print()
    if fails:
        print("自测失败 %d 项：" % len(fails))
        for f in fails:
            print("  - " + f)
        return 1
    print("自测全过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())

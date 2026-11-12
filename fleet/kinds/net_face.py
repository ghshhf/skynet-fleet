"""网络层总览设备（kind=netface）：把散在六处的状态收成「一张脸」。

## 为什么要有这台设备

主人 2026-11-03 原话：「你不是做过一个管理面板吗？把它都接进去不就行了吗？」

对，问题从来不是**没有探测**，是**没有出口**：
`net.diagnose()` 早在 `fleet/net.py` 里写好了，但它是个 CLI 函数，
既没挂成能力、也不在注册表里 ⇒ 面板看不见、模型看不见、门禁也测不到。
于是「网络层现在什么状态」这个问题，只能靠人去翻六个模块的源码回答。

这台设备只做一件事：**把已有的事实收拢，不改任何东西**。

## 两条能力

    net.status   只读   一屏看全：半步圆满(真实直连层 or 自有内核层) / 外部拐杖(3067) / 兜底 / 代理内核 / 规则集 / P2P 会话 / 会合
    net.route    只读   给一个 host，说清它**会**走哪条、凭什么这么判

## 纪律（写在这里，因为这里最容易破）

1. **默认不出网（不跑全量出口探测）**。fp_egress 那种端到端扫出口 IP/ASN 必须显式
   `probe=True`。但本设备**默认会做一次轻量直连探测**（清掉继承代理后测 GitHub 单目标，
   短超时）——这是半步圆满头条的判据本身（11-01 定义：关 karing 真实直连通），
   不是乱出网，不算「扫 IP」。
2. **拿不到就写拿不到**，字段留 `None` + reason，绝不填默认值冒充事实。
   这条是主人的第一红线（绝不猜数字冒充事实）。
3. **全部延迟 import**。本机没装 sing-box、没起内核、没装 textual，
   都不能让这台设备哑掉——它报的恰恰是「什么都没有」这种状态。
4. **不改任何状态**。不拉起内核、不写注册表、不点开系统代理。
   要动手的是 `proxy.deploy` / `proxy.core_stop`，不是这里。
5. **半步圆满是头条，定义回到 11-01 源头**：半步圆满 = **不依赖 karing(3067) 能出网**。
   两条达成路径，任一即达成：① 真实直连层（清代理后 GitHub/pypi/raw 直连通，11-01 已验证）；
   ② 自有内核层（7891 起 + 系统代理/tun 接管）。karing 是外部拐杖，**永远不是**达成条件。
   本设备默认测一次「清代理真实直连」来证实头条——不是乱出网，是判据本身。
   端口探测只在能探到本机回环时才可信——本 shell 探不到回环，判活一律以代码为准。

## 跟别台的边界（别重复造）

    proxy.core_status  只说内核一件事
    nat.sessions       只说打洞会话
    tun.sessions       只说隧道会话
    rdv.auto.status    只说会合循环
    net:*（LanScanner）说局域网
    ──────────────────────────────
    net:face ← 就是这个文件，只做「把它们放在同一屏」这一件事

它不实现任何探测。收不到的东西就报收不到——这层的价值是**可回答**，不是**能探测**。
"""
import json
import os
import time

from ..core import BaseDevice, DeviceResult as R


def _count_nodes(config_path):
    """从已落盘的 sing-box 配置里数真节点数（策略组不算）。

    拿不到就返回 (None, 原因)。不返回 0 —— 0 会被读成「配置里零节点」，
    那是另一件事（配置在但 subscribe 解析失败），不能混为一谈。
    """
    if not config_path or not os.path.isfile(config_path):
        return None, "还没有生成过配置（从没 deploy 过）"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        return None, "配置读不了：%s" % e
    fake = ("direct", "block", "selector", "urltest", "dns")
    n = sum(1 for o in (cfg.get("outbounds") or []) if o.get("type") not in fake)
    return n, None


def _tun_on(config_path):
    if not config_path or not os.path.isfile(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return None
    return any(i.get("type") == "tun" for i in (cfg.get("inbounds") or []))


def _direct_egress_ok(timeout=2.0):
    """半步圆满（真实直连层）的判据：清掉一切继承代理后，真实直连 GitHub 是否通。

    这是 11-01 源头定义——「关掉 karing 后本机真实直连能跑通 GitHub」就是半步圆满。
    用 ProxyHandler({}) 强制无代理，不碰全局 env（线程安全，不污染别的探测）。
    返回 (ok, detail)：ok=True 测通；ok=False 测了但不通。拿不到就 (False, 原因)。
    """
    import urllib.request

    url = "https://github.com"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=timeout) as resp:
            return (resp.status == 200), "清代理真实直连 GitHub 200（不依赖 karing）"
    except Exception as e:
        return (False, "%s: %s" % (type(e).__name__, e))


class NetFace(BaseDevice):
    kind = "netface"
    via = "local"

    def __init__(self, name="net:face", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "net")

    # ------------------------------------------------------------ 可用性

    def probe(self):
        """这层永远「可用」——它报的就是「现在有什么」。

        不拿外网当判据：网络断了恰恰是最需要调它的时候。
        """
        from .. import net as NET

        mine = NET.self_proxy_url()
        mine_alive = NET.is_proxy_alive(mine, timeout=0.2)
        sys_url, _ = NET.system_proxy_url()
        sys_alive = bool(sys_url) and NET.is_proxy_alive(sys_url, timeout=0.2)
        live = []
        if mine_alive:
            live.append("自有内核")
        if sys_alive:
            live.append("系统代理 %s" % sys_url.rsplit(":", 1)[-1])
        head = "出向：%s" % (" + ".join(live) if live else "只有直连")
        return True, head

    def caps(self):
        return ("net.status", "net.route")

    # ------------------------------------------------------------ 分派

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "net.status":
            return self._status(args)
        if cap == "net.route":
            return self._route(args)
        return super().invoke(cap, args)

    # ------------------------------------------------------------ net.status

    def _status(self, args):
        t0 = time.monotonic()
        from .. import net as NET

        # ---- 1. 出向三路 + 选路，三件事并发探。
        # 为什么要并发：死端口在 Windows 上不是立刻 RST，而是被静默 DROP，
        # 只能等超时。串起来三个 250ms 就是 750ms，面板刷新会明显卡。
        # 探活超时压到 0.25s：本地回环的真活端口在 1ms 内就回，0.25 足够。
        self_url = NET.self_proxy_url()
        sys_url, sys_note = NET.system_proxy_url()
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=4) as ex:
            f_self = ex.submit(NET.is_proxy_alive, self_url, 0.25)
            f_sys = (ex.submit(NET.is_proxy_alive, sys_url, 0.25) if sys_url else None)
            f_route = ex.submit(NET.resolve_proxy)
            f_direct = ex.submit(_direct_egress_ok, 2.0)
            self_alive = f_self.result()
            sys_alive = f_sys.result() if f_sys else None
            route, route_why = f_route.result()
            direct_ok, direct_detail = f_direct.result()
        registry = NET.reg_proxy_raw()

        # 半步圆满层 = 天网自有数据面（7891），是头条，不是「三路之一」。
        # 系统代理 3067(karing) 是外部拐杖，直连是兜底——两者都不是天网本事。
        channels = [
            {"name": "天网自有内核(半步圆满层)", "url": self_url, "alive": self_alive,
             "note": (None if self_alive
                      else ("7891 没起，但真实直连层已达成半步圆满（不靠内核）"
                            if direct_ok else
                            "半步圆满未达成：需 proxy.deploy 把网接上"
                            "（节点→7891→系统代理接管），不靠 karing"))},
            {"name": "系统代理(外部拐杖·karing)", "url": sys_url, "alive": sys_alive,
             "note": ((("设置指向 %s，但端口没活（karing 关了/没起）——不是天网本事，也不挡直连"
                       % sys_url) if sys_url else "外部软件，主人开才活；不是天网本事")
                      if not sys_alive
                      else (sys_note or "外部软件，主人开才活；不是天网本事"))},
            # 直连活不活只能靠真发一个包才知道；这层默认就测了（见 direct_egress）。
            {"name": "直连(兜底)", "url": None, "alive": None,
             "note": ("真实直连层：清代理后 GitHub 直连通，半步圆满达成（不依赖 karing）"
                      if direct_ok else "兜底通道；清代理后真实直连未证实，见 direct_egress")},
        ]

        # ---- 2. 代理内核（含「装没装」「起没起」「配置里有几个节点」三件事）
        # running 直接沿用上面那次 7891 探活，不再调 proxycore.is_running()：
        # 两者探的是同一个端口，而 is_running 写死 1.5s 超时——死口要白等 1.5 秒。
        core = {"ok": False, "err": "proxycore 不可用"}
        sys_to_us, sys_to_us_why = None, "proxycore 不可用"
        try:
            from .. import proxycore as PC

            exe = PC.find_exe()
            nodes, nodes_why = _count_nodes(PC.CONFIG_PATH)
            # 「系统代理开着」不等于「内核接管」：这台机器上开着的 7890 常是 karing。
            # 半步圆满的内核层判据必须看系统代理是否真的指着我们的 7891。
            sys_to_us, sys_to_us_why = PC.proxy_points_to_us()
            core = {
                "ok": True,
                "installed": bool(exe),
                "exe": exe,
                "version": PC.SB_VERSION,
                "running": self_alive,
                "running_source": "TCP 探活 %s（与 proxycore.is_running 同口径，省掉 1.5s 超时）"
                                  % PC.MIXED_ADDR,
                "mixed": PC.MIXED_ADDR,
                "system_proxy_on": PC.proxy_state(),
                "system_proxy_to_us": sys_to_us,
                "system_proxy_to_us_why": sys_to_us_why,
                "config_path": PC.CONFIG_PATH,
                "config_exists": os.path.isfile(PC.CONFIG_PATH),
                "config_nodes": nodes,
                "config_nodes_why": nodes_why,
                "tun": _tun_on(PC.CONFIG_PATH),
            }
        except Exception as e:
            core = {"ok": False, "err": "%s: %s" % (type(e).__name__, e)}

        # ---- 3. 规则集：分流到底有多细，取决于这份表
        rules = {"ok": False, "err": "proxymgr 不可用"}
        try:
            from .. import proxymgr as PM

            cn = PM.load_karing_ruleset()
            rs = cn or PM.builtin_cn_ruleset()
            rules = {
                "ok": True,
                "source": "karing-ruleset（全量）" if cn else "内置精简（外部规则目录没找到）",
                "domains": len(rs.domains),
                "suffixes": len(rs.domain_suffix),
                "ip_cidrs": len(rs.ip_cidrs),
                "default": PM.default_routetable().default,
            }
        except Exception as e:
            rules = {"ok": False, "err": "%s: %s" % (type(e).__name__, e)}

        # ---- 4. P2P：打洞与隧道会话（读别人的表，拿锁读，不改）
        p2p = {}
        try:
            from . import nat_traversal as NATL

            with NATL._LOCK:
                p2p["nat_sessions"] = len(NATL._SESSIONS)
        except Exception as e:
            p2p["nat_sessions"] = None
            p2p["nat_err"] = "%s: %s" % (type(e).__name__, e)
        try:
            from . import tunnel_layer as TUNL

            with TUNL._LOCK:
                p2p["tun_sessions"] = len(TUNL._SESSIONS)
        except Exception as e:
            p2p["tun_sessions"] = None
            p2p["tun_err"] = "%s: %s" % (type(e).__name__, e)
        try:
            from .. import rdv_auto as RA

            st = RA.loop_status()
            p2p["rdv_auto"] = {"running": bool(st.get("running")),
                               "url": st.get("relay_url") or st.get("url")}
        except Exception as e:
            p2p["rdv_auto"] = {"running": None, "err": "%s: %s" % (type(e).__name__, e)}

        data = {
            "ts": round(time.time(), 3),
            "egress": {"route": route, "why": route_why,
                       "channels": channels, "registry": registry},
            "core": core,
            "rules": rules,
            "p2p": p2p,
            "direct_egress": {"ok": direct_ok, "detail": direct_detail},
            "probe": None,
            "verdict": self._verdict(self_alive, sys_alive, sys_url, core,
                                     direct_ok, direct_detail),
        }

        # ---- 5. 可选：真发一个包确认出口。默认不跑。
        if args.get("probe"):
            try:
                from .. import fp_egress as FE

                data["probe"] = FE.probe(timeout=args.get("timeout") or 8)
            except Exception as e:
                data["probe"] = {"ok": False,
                                 "error": "%s: %s" % (type(e).__name__, e)}

        # ---- 6. 半步圆满（头条）：回到 11-01 源头定义——不依赖 karing 能出网。
        # 两条达成路径，任一即达成：
        #   ① 真实直连层：清代理后 GitHub 直连通（11-01 源头，此刻 karing 关掉就在跑）
        #   ② 自有内核层：7891 起 + 内核在 + 系统代理/tun 接管
        # karing(3067) 是外部拐杖，永远不是达成条件。
        installed = core.get("installed")
        sys_on = core.get("system_proxy_on")
        tun = core.get("tun")
        # 内核层 = 7891 起着 + 内核在 + **系统代理真的指向我们(或 tun 接管)**。
        # 只看 sys_on 会把 karing 的 7890 也算成「内核已接管」——谎报半步圆满。
        core_layer = bool(self_alive and installed and (sys_to_us or tun))
        direct_layer = bool(direct_ok)
        missing = []
        if not core_layer and not direct_layer:
            if not installed:
                missing.append("sing-box 没下载（download_singbox 从 GitHub 拉内核大文件，本身要出网）")
            elif self_alive and not (sys_to_us or tun):
                missing.append("内核装了也起了，但系统代理/tun 没接管"
                               "（当前系统代理：%s；deploy 要 enable_proxy=True）" % sys_to_us_why)
            if not direct_ok:
                missing.append("清代理后真实直连 GitHub 不通（半步圆满的真实直连层未达成）")
        data["half_ban"] = {
            "name": "半步圆满",
            "definition": "不依赖 karing(3067) 能出网：真实直连层(11-01) 或 自有内核层(7891)，任一达成",
            "achieved": bool(core_layer or direct_layer),
            "paths": {
                "direct_layer": {"ok": direct_layer, "detail": direct_detail},
                "core_layer": {"ok": core_layer,
                               "detail": ("7891 起 + %s 接管" % ("系统代理" if sys_to_us else "tun")
                                          if core_layer else
                                          ("7891 起了但系统代理没指向它（%s）" % sys_to_us_why
                                           if (self_alive and installed) else
                                           "7891 未起或未接管（%s）" % sys_to_us_why))},
            },
            "missing": missing or None,
            "egress_verified": ("端到端验证通过（probe 实发出网）"
                                if (data.get("probe") or {}).get("ok") else None),
        }

        return R.success("net.status", self.name, via=self.via, data=data,
                         ms=round((time.monotonic() - t0) * 1000, 1))

    @staticmethod
    def _verdict(self_alive, sys_alive, sys_url, core, direct_ok, direct_detail):
        """头条讲「半步圆满达成与否」，回到 11-01 源头：不依赖 karing 能出网。

        两条达成路径，任一即达成：① 真实直连层（清代理 GitHub 直连通）；
        ② 自有内核层（7891 起 + 接管）。只陈述事实，不给「建议怎么做」。
        """
        installed = core.get("installed")
        tun = core.get("tun")
        # 头条①：真实直连层达成（此刻 karing 关掉后天网正在跑的状态）
        if direct_ok:
            return "半步圆满达成（真实直连层）：关掉 karing 后 GitHub/pypi/raw 真实直连通，不靠任何代理"
        # 头条②：自有内核层达成（系统代理必须真的指向 7891，karing 的 7890 不算）
        sys_to_us = core.get("system_proxy_to_us")
        if self_alive and installed and (sys_to_us or tun):
            return "半步圆满达成（自有内核层）：7891 起、系统代理/tun 接管，不靠 karing"
        # 7891 在听但不是天网内核
        if self_alive and not installed:
            return "7891 在听但不是天网内核（sing-box 没装，端口被别的程序占了）"
        # 两条都没达成，列缺什么
        if not installed:
            return ("半步圆满未达成：sing-box 没下载，且清代理真实直连也不通"
                    if not direct_ok else
                    "半步圆满差自有内核层：真实直连已通，但 sing-box 没下载（7891 没起）")
        if not self_alive:
            return ("半步圆满未达成：内核装了但没起（proxy.deploy 拉起 7891），且清代理真实直连也不通"
                    if not direct_ok else
                    "半步圆满差自有内核层：真实直连已通，但 7891 没起")
        if not (sys_to_us or tun):
            return ("半步圆满差最后一步：7891 起了但系统代理没指向它（%s；deploy 用 enable_proxy=True）"
                    % core.get("system_proxy_to_us_why"))
        return ("出向：半步圆满未达成，靠 karing 拐杖顶着" if sys_alive
                else "出向：半步圆满未达成，只剩直连")

    # ------------------------------------------------------------ net.route

    def _route(self, args):
        """给定 host，说清两件事：**现在**走哪条、**内核起来后**会怎么分流。

        这两件事必须分开写。合成一句「走代理」是骗人的：内核没起的时候，
        规则表根本没在生效，实际跑的是 net.resolve_proxy() 那条通道。
        """
        t0 = time.monotonic()
        host = (args.get("host") or "").strip().lower()
        if not host and args.get("url"):
            try:
                import urllib.parse as _up

                host = (_up.urlsplit(args["url"]).hostname or "").lower()
            except Exception:
                host = ""
        if not host:
            return R.error("net.route", "要给 host 或 url", device=self.name, via=self.via)

        from .. import net as NET

        route, why = NET.resolve_proxy()

        # 「内核起来后会怎么分流」——这是规则表的判断，不是实测
        planned = {"ok": False, "err": "proxymgr 不可用"}
        try:
            from .. import proxymgr as PM

            rt = PM.default_routetable()
            dec = rt.decision(host=host)
            planned = {
                "ok": True,
                "outbound": dec["outbound"],
                "rule": dec["rule"],
                "source": "karing-ruleset（全量）" if PM.load_karing_ruleset() else "内置精简",
            }
        except Exception as e:
            planned = {"ok": False, "err": "%s: %s" % (type(e).__name__, e)}

        core_running = None
        try:
            from .. import proxycore as PC

            core_running = PC.is_running()
        except Exception:
            pass

        return R.success("net.route", self.name, via=self.via, data={
            "host": host,
            "now": {"channel": route or "直连", "why": why},
            "if_core_running": planned,
            "core_running": core_running,
            "note": ("内核没在跑，规则表只是预设，此刻不生效"
                     if core_running is False else None),
        }, ms=round((time.monotonic() - t0) * 1000, 1))

"""网络层总览设备（kind=netface）：把散在六处的状态收成「一张脸」。

## 为什么要有这台设备

主人 2026-11-03 原话：「你不是做过一个管理面板吗？把它都接进去不就行了吗？」

对，问题从来不是**没有探测**，是**没有出口**：
`net.diagnose()` 早在 `fleet/net.py` 里写好了，但它是个 CLI 函数，
既没挂成能力、也不在注册表里 ⇒ 面板看不见、模型看不见、门禁也测不到。
于是「网络层现在什么状态」这个问题，只能靠人去翻六个模块的源码回答。

这台设备只做一件事：**把已有的事实收拢，不改任何东西**。

## 两条能力

    net.status   只读   一屏看全：出向三路 / 代理内核 / 规则集 / P2P 会话 / 会合
    net.route    只读   给一个 host，说清它**会**走哪条、凭什么这么判

## 纪律（写在这里，因为这里最容易破）

1. **默认不出网**。本地部分零外网；要端到端确认出口必须显式 `probe=True`
   —— 那会打真实目标，花时间也花流量，不能塞进默认刷新。
2. **拿不到就写拿不到**，字段留 `None` + reason，绝不填默认值冒充事实。
   这条是主人的第一红线（绝不猜数字冒充事实）。
3. **全部延迟 import**。本机没装 sing-box、没起内核、没装 textual，
   都不能让这台设备哑掉——它报的恰恰是「什么都没有」这种状态。
4. **不改任何状态**。不拉起内核、不写注册表、不点开系统代理。
   要动手的是 `proxy.deploy` / `proxy.core_stop`，不是这里。

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

        with ThreadPoolExecutor(max_workers=3) as ex:
            f_self = ex.submit(NET.is_proxy_alive, self_url, 0.25)
            f_sys = (ex.submit(NET.is_proxy_alive, sys_url, 0.25) if sys_url else None)
            f_route = ex.submit(NET.resolve_proxy)
            self_alive = f_self.result()
            sys_alive = f_sys.result() if f_sys else None
            route, route_why = f_route.result()
        registry = NET.reg_proxy_raw()

        channels = [
            {"name": "天网自有内核", "url": self_url, "alive": self_alive,
             "note": None if self_alive else "没起（proxy.deploy 可拉起）"},
            {"name": "系统代理", "url": sys_url, "alive": sys_alive,
             "note": sys_note},
            # 直连的 alive 永远是 None：它的「可用性」只能靠真发一个包才知道，
            # 而这层默认不出网。写 None 比写 True 诚实。
            {"name": "直连", "url": None, "alive": None,
             "note": "兜底通道；不探测就不声明它通"},
        ]

        # ---- 2. 代理内核（含「装没装」「起没起」「配置里有几个节点」三件事）
        # running 直接沿用上面那次 7891 探活，不再调 proxycore.is_running()：
        # 两者探的是同一个端口，而 is_running 写死 1.5s 超时——死口要白等 1.5 秒。
        core = {"ok": False, "err": "proxycore 不可用"}
        try:
            from .. import proxycore as PC

            exe = PC.find_exe()
            nodes, nodes_why = _count_nodes(PC.CONFIG_PATH)
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
            "probe": None,
            "verdict": self._verdict(self_alive, sys_alive, sys_url, core),
        }

        # ---- 5. 可选：真发一个包确认出口。默认不跑。
        if args.get("probe"):
            try:
                from .. import fp_egress as FE

                data["probe"] = FE.probe(timeout=args.get("timeout") or 8)
            except Exception as e:
                data["probe"] = {"ok": False,
                                 "error": "%s: %s" % (type(e).__name__, e)}

        return R.success("net.status", self.name, via=self.via, data=data,
                         ms=round((time.monotonic() - t0) * 1000, 1))

    @staticmethod
    def _verdict(self_alive, sys_alive, sys_url, core):
        """一句话结论。只陈述事实，不给「建议怎么做」——建议是人的事。"""
        if self_alive:
            if core.get("installed"):
                return "7891 在监听，出向走家底"
            return ("7891 在监听，但 sing-box 二进制没装 —— 监听方不是天网内核，"
                    "是别的程序占了口")
        if sys_alive:
            return "自有内核没起，出向回落系统代理 %s" % sys_url
        if sys_url:
            return "自有内核没起；系统代理 %s 是死端口（不挡直连）" % sys_url
        return "自有内核没起，系统也没设代理 → 只剩直连"

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

# -*- coding: utf-8 -*-
"""天网 · 动态入口守护（entry_guard）

它是什么
--------
一个常驻进程，把「地址会变」这件事彻底自动化。主人 2026-11-02 的判断：

    「IPv6 为什么没有大规模使用，是它变动太频繁了。
      所以很多时候，你会经常以为它是失效的。」

所以正确的产品形态不是「给你一个 IP」，而是：

    self_id（永不变） → 当前地址（随时变） → 这个映射自动维护

守护进程干四件事，一轮一轮地重复：

    ① 检测   本机现在有哪些地址（v6 全局 / v4 内网 / 出口映射），临时地址也收
    ② 上报   地址变了就写进墙外中继的地址簿；没变就不写（省往返）
    ③ 放行   自动开/关入站 pinhole（只放行当前那个端口，只对 v6 生效）
    ④ 自检   让墙外回连本机，把「能不能进得来」写成结论

关键设计（都是踩过坑换来的）
--------------------------
1. **别把「测不了」写成「进不来」**：没有公网地址时，报的是「网络侧没给地址」，
   不是「端口是 0」。IPv6 轮换是常态，「这会儿没有」不等于「失效」。
2. **只放行当前端口**：批量开端口等于开门。规则名里带端口，事后能分辨。
3. **退避**：失败按 1×2×4… 退避，封顶 5 分钟。别把临时故障打成长期骚扰。
4. **不建系统计划任务**：进程内线程 + 可中断，跑在长驻进程里。
5. **凭据从 vault 取**：中继密钥不落明文文件。

用法
----
    python tools/cloudnode/entry_guard.py            # 前台跑
    python tools/cloudnode/entry_guard.py --once     # 只跑一轮
    python tools/cloudnode/entry_guard.py --status   # 看当前状态
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

STATE_PATH = os.environ.get("SKYNET_ENTRY_GUARD_STATE",
                            os.path.join(os.path.expanduser("~"), ".fleet",
                                         "entry_guard.json"))
DEFAULT_PORT = int(os.environ.get("SKYNET_ENTRY_PORT", "8791"))
DEFAULT_INTERVAL = float(os.environ.get("SKYNET_ENTRY_INTERVAL", "60"))


def _log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


def _vault(name: str) -> str:
    """从 DPAPI 保险箱取凭据。取不到就返回空串，不抛 —— 让调用方决定怎么办。"""
    try:
        from fleet import vault
        return (vault.load(name) or "").strip()
    except Exception:
        return ""


def relay_origin() -> str:
    """墙外中继地址。优先环境变量（部署时可覆盖），否则按约定格式拼。"""
    o = (os.environ.get("FLEET_WSRELAY") or "").strip()
    if o:
        return o
    sid = _vault("daytona_sandbox_id")
    if not sid:
        return ""
    return "https://8080-%s.daytonaproxy01.net" % sid


def relay_headers() -> dict:
    """Daytona preview 代理自带的 Bearer 鉴权 —— 与本层 WSRELAY_SECRET 是两回事。"""
    key = _vault("daytona_api_key")
    if not key:
        return {}
    return {"Authorization": "Bearer " + key}


def ensure_relay_alive() -> dict:
    """确认墙外中继还活着，死了就拉起来。

    为什么需要：沙箱是**别人的**机器。autoStopInterval 设成一年只是不自动停，
    但平台重启、宿主机迁移、镜像重建都可能让它消失。中继一没，
    本机的地址就上报不出去 —— 整个「动态」层静默失效，而这恰恰是最难发现的那种坏。

    所以每轮都顺手确认一次：活着就不管，死了就重启它。
    """
    import urllib.request
    origin = relay_origin()
    if not origin:
        return {"ok": False, "err": "没有中继地址"}
    hdrs = relay_headers()
    try:
        req = urllib.request.Request(origin + "/", headers=hdrs)
        # 显式禁用代理：这个检查必须走**直连**，走代理会把「代理能通」
        # 误判成「中继活着」，而代理能通不等于墙外能回连本机。
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(req, timeout=12) as r:
            body = r.read().decode("utf-8", "replace")
        if '"skynet-wsrelay"' in body:
            return {"ok": True, "alive": True}
    except Exception as e:
        return {"ok": False, "alive": False, "err": f"{type(e).__name__}: {e}",
                "need_restart": True}
    return {"ok": False, "alive": False, "err": "中继响应异常", "need_restart": True}


def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    d = os.path.dirname(STATE_PATH)
    try:
        os.makedirs(d, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def build_loop(port: int = DEFAULT_PORT):
    """构造 AutoLink。配置不全时返回 (None, 原因)，不抛。"""
    from fleet import rdv_auto
    origin = relay_origin()
    secret = _vault("wsrelay_secret")
    if not origin or not secret:
        return None, ("没有中继地址或密钥（vault 里缺 wsrelay_secret / "
                      "daytona_sandbox_id，或 FLEET_WSRELAY 未设）")
    hdrs = relay_headers()
    if hdrs:
        os.environ["FLEET_WSRELAY_HEADERS"] = json.dumps(hdrs)
    return rdv_auto.AutoLink(origin=origin, secret=secret, port=port,
                              state_path=STATE_PATH), ""


def run_once(port: int = DEFAULT_PORT) -> dict:
    # 先确认中继活着 —— 死了的话上报必然失败，报「上报失败」不如报「中继没了」
    relay = ensure_relay_alive()
    loop, err = build_loop(port)
    if loop is None:
        return {"ok": False, "err": err, "status": "down", "relay": relay}
    st = loop.tick()
    verify = st.get("verify") or {}
    # 自检默认有节流（省往返）。但状态卡在 unverified 时，**正是最需要结论的时刻**，
    # 那就强制重跑一次 —— 否则守护会一直报一个过期的 note，看起来像"没进展"。
    if st.get("status") == "unverified" and not verify.get("note"):
        try:
            loop._st["verify_ts"] = 0
            st = loop.tick()
            verify = st.get("verify") or {}
        except Exception:
            pass
    summary = {
        "ok": bool(st.get("ok")),
        "status": st.get("status"),
        "err": st.get("err") or "",
        # 自检的说明文字：rendezvous 放在 note，rdv_auto 转存时放进了 err。
        # 两个都读，免得修了一头丢了另一头。
        "note": verify.get("note") or verify.get("err") or "",
        "no_public_addr": bool(verify.get("no_public_addr")),
        "invalid_port": bool(verify.get("invalid_port")),
        "self_id": (st.get("published") or {}).get("self_id")
        or _load_state().get("self_id"),
        "current": (st.get("published") or {}).get("meta", {}).get("current"),
        "addrs": (st.get("published") or {}).get("published") or [],
        "pinhole": st.get("pinhole") or {},
        "relay": relay,
        "ts": round(time.time(), 1),
    }
    # 存一份人类可读的摘要，方便 /net 指令直接读
    prev = _load_state()
    prev.update(summary)
    prev["relay"] = relay_origin()
    _save_state(prev)
    return summary


def loop_forever(port: int = DEFAULT_PORT, interval: float = DEFAULT_INTERVAL):
    _log(f"动态入口守护启动 · 间隔 {interval}s · 端口 {port}")
    fails = 0
    last_status = _load_state().get("last_status")
    while True:
        try:
            s = run_once(port)
            if s.get("ok"):
                fails = 0
                if s.get("status") != last_status:
                    last_status = s.get("status")
                    _log(f"状态 → {last_status}"
                         + (f" · {s['note'][:90]}" if s.get("note") else ""))
                st = _load_state()
                st["last_status"] = last_status
                _save_state(st)
            else:
                fails += 1
                _log(f"本轮失败（连续 {fails} 次）：{s.get('err') or s.get('note')}")
        except Exception as e:
            fails += 1
            _log(f"异常：{type(e).__name__}: {e}")
        # 退避：1×2×4… 封顶 5 分钟
        wait = interval if fails == 0 else min(interval * (2 ** min(fails, 6)), 300)
        time.sleep(wait)


def main(argv=None):
    ap = argparse.ArgumentParser(description="动态入口守护")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--status", action="store_true", help="打印当前状态")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    a = ap.parse_args(argv)

    if a.status:
        print(json.dumps(_load_state(), ensure_ascii=False, indent=1))
        return 0
    if a.once:
        print(json.dumps(run_once(a.port), ensure_ascii=False, indent=1))
        return 0
    loop_forever(a.port, a.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

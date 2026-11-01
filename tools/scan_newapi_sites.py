"""批量扫 new-api 系公益站：只读 /api/status + /api/pricing，零登录零额度。

两轮策略（第二轮才用代理，避免把能直连的站也拖慢）：
  第一轮 直连 + 浏览器 UA；
  第二轮 只跑第一轮失败的，走本地代理 —— 403/SSL 多半是 Cloudflare 挡的
         TLS/UA 指纹，换个出口往往就通了。

自动分层（不靠人肉判断）：
  welfare    公益站：status 可读 + 无在线充值 + 有赠送/邀请额度
  newapi     可读的 new-api（无充值无赠送，可能是商业站关了充值）
  commercial 商业中转：status 可读 + 有在线充值
  suspect    status 被挡但 pricing 公开（别的系统类型或半开放）
  dead       两个接口都不通
"""
import sys
import os
import json
import time
import datetime
import concurrent.futures as cf

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet.kinds.llmgw_device as L  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
# 扫描结果是运行产物，落 tmp/ 不入库；正式工具放 tools/ 供定时任务调用
OUT = os.path.join(ROOT, "tmp", "newapi_sites.json")
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

CANDIDATES = [
    "agentrouter.org", "ps.air-outer.com", "co.agentrouter.org",
    "api.hcnsec.cn", "hcnsec.cn", "api.justwoker.icu", "justwoker.icu",
    "seekai.cc", "www.vyceai.com", "vyceai.com", "api.tabiai.com", "gorouter.ai",
    "blazeai.boxu.dev", "ai.router.team", "zhuozaiya.top", "sub.geiliapi.com",
    "xn--mkrw42a.com", "anyrouter.top", "rawchat.cn", "matrix.moe",
    "kktoken.top", "www.longai.chat", "conduit.ai",
    "wolfai.top", "hezubus.cc", "kapibala.asia", "aixoras.com", "wuai.ai",
    "www.thinkai.tv", "code-cli.cn", "api.jmds.dev", "ai.17nas.com",
    "llm.zerofall.top", "cheaprouter.cc", "9527code.com", "api.ywcode.top",
    "www.duiapi.com", "cloudlogicapi.com",
    "api.chatanywhere.tech", "api.gpt.ge", "free.v36.cm", "api.airforce",
    "api.v3.cm", "api.aivvm.com", "free.vveai.com",
]

_ORIG_REQ = L._req_json


def _install_ua(ua=BROWSER_UA):
    def wrapped(base, path, key, timeout=15, method="GET", body=None,
                extra_headers=None):
        h = dict(extra_headers or {})
        h.setdefault("User-Agent", ua)
        h.setdefault("Accept", "application/json")
        return _ORIG_REQ(base, path, key, timeout, method, body, h)
    L._req_json = wrapped


def _restore():
    L._req_json = _ORIG_REQ


def _classify(r):
    info = r.get("info") or {}
    models = r.get("models") or []
    if info:
        if info.get("topup_enabled"):
            return "commercial"
        if (info.get("signup_usd") or 0) > 0 or (info.get("inviter_usd") or 0) > 0:
            return "welfare"
        return "newapi"
    return "suspect" if models else "dead"


def one(host, tmo=8):
    t0 = time.time()
    try:
        r = L._newapi_site(host, timeout=tmo)
    except Exception as e:
        return {"site": host, "ok": False, "kind": "dead", "info": {}, "models": [],
                "model_count": 0, "errors": ["exc: %s" % str(e)[:80]],
                "elapsed": round(time.time() - t0, 1), "egress": "direct"}
    out = {"site": host, "ok": bool(r.get("info") or r.get("models")),
           "kind": _classify(r), "info": r.get("info") or {},
           "models": r.get("models") or [], "errors": r.get("errors") or [],
           "elapsed": round(time.time() - t0, 1), "egress": "direct"}
    out["model_count"] = len(out["models"])
    return out


def main():
    t_all = time.time()
    _install_ua()
    rows = {}
    try:
        with cf.ThreadPoolExecutor(max_workers=12) as ex:
            for r in ex.map(one, CANDIDATES):
                rows[r["site"]] = r
        dead = [s for s, r in rows.items() if r["kind"] == "dead"]
        px = L._egress_proxy_addr() or os.environ.get("LLMW_SCAN_PROXY") \
            or "http://127.0.0.1:3067"
        os.environ["HTTPS_PROXY"] = px
        L._EGRESS_CACHE.update(addr=px, ts=time.time())
        print("第一轮直连：%d 站可读，%d 站失败 → 第二轮走代理 %s 重试"
              % (len(rows) - len(dead), len(dead), px))
        with cf.ThreadPoolExecutor(max_workers=12) as ex:
            for r in ex.map(one, dead):
                if r["ok"]:
                    r["egress"] = "proxy"
                    rows[r["site"]] = r
                else:
                    rows[r["site"]]["errors"] = (rows[r["site"]]["errors"] or []) + \
                        ["proxy-retry: %s" % ((r["errors"] or ["?"])[0][:60])]
    finally:
        _restore()

    order = {"welfare": 0, "newapi": 1, "commercial": 2, "suspect": 3, "dead": 4}
    out = sorted(rows.values(),
                 key=lambda r: (order.get(r["kind"], 9), -r.get("model_count", 0)))
    print("=" * 100)
    print("new-api 公益站扫描  %s  |  %d 站点 / %.1fs"
          % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), len(out),
             time.time() - t_all))
    print("=" * 100)
    hdr = "%-24s %-11s %-6s %-9s %-8s %-5s %-6s %s"
    print(hdr % ("站点", "类型", "模型", "注册送$", "邀请$", "充值", "出口", "窗口/OAuth"))
    print("-" * 100)
    for r in out:
        i = r.get("info") or {}
        win = i.get("quota_drop_windows_h") or []
        tail = ("窗%s " % win if win else "") + "/".join(i.get("oauth") or [])
        print(hdr % (r["site"][:24], r["kind"], r.get("model_count", 0),
                     i.get("signup_usd") if i.get("signup_usd") is not None else "-",
                     i.get("inviter_usd") if i.get("inviter_usd") is not None else "-",
                     "有" if i.get("topup_enabled") else ("无" if i else "-"),
                     r.get("egress", "-"),
                     (tail or (r["errors"][0][:28] if r.get("errors") else ""))))

    wel = [r for r in out if r["kind"] == "welfare"]
    print("\n公益站（无充值 + 有赠额）：%d 家 → %s"
          % (len(wel), ", ".join(r["site"] for r in wel) or "无"))
    multi = sorted([r for r in out if r.get("model_count", 0) >= 20],
                   key=lambda r: -r["model_count"])[:8]
    print("模型最多的 8 家：%s"
          % ", ".join("%s(%d)" % (r["site"], r["model_count"]) for r in multi))

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"at": datetime.datetime.now().isoformat(timespec="seconds"),
                   "count": len(out), "rows": out}, f, ensure_ascii=False, indent=1)
    print("已落盘 →", OUT)


if __name__ == "__main__":
    main()

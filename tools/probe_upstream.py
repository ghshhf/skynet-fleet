#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公益站「上游成分」判别器：这家到底是自建开源模型，还是转卖别人的 key。

主人问的问题：会不会有人自己花钱买卡，跑开源权重，做成公益站白送？
这个脚本就是回答它的工具，判据全部可核验、可复现，不靠感觉。

四类证据（由强到弱）：
  1) 模型命名成分     —— 最强。自建必有量化后缀或自家别名；转卖必有官方原名。
  2) 中间商别名特征   —— `-openai-compact` / `cs-` 前缀 / `mj_*` 任务接口，
                         说明它在二次封装别家的渠道，而不是自己跑推理。
  3) 规模矛盾         —— 一个站点挂 100+ 模型，物理上不可能自建（一张卡跑不了 100 个）。
  4) 运行时指纹       —— /metrics（vLLM/Prometheus）、/props（llama.cpp）、
                         /api/tags（Ollama）、响应头 Server，能直接看到推理引擎。

用法：
  python tools/probe_upstream.py                 # 离线分析已有扫描结果
  python tools/probe_upstream.py --online        # 加上在线指纹 + 端口探测
  python tools/probe_upstream.py --ports         # 只跑端口探测
"""
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "tmp", "newapi_sites.json")
OUT = os.path.join(ROOT, "tmp", "upstream_report.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

# ---------- 命名成分词典 ----------
# 闭源官方模型（只能来自官方 API / 订阅号池 / 别人转卖）
RE_CLOSED = re.compile(
    r"^(gpt-|gpt\d|o1|o3|o4|chatgpt|claude|gemini|grok|dall-e|sora|imagen|veo|"
    r"nano-banana|mj_|midjourney|suno|kling|hailuo|seedance|runway|pika|"
    r"command-|ernie-|hunyuan-|doubao-|step-|spark|baichuan|sensechat)", re.I)

# 中间商二次封装特征（自建的人不需要这些别名）
RE_RESELL_ALIAS = re.compile(
    r"(-openai-compact|^cs-|-compact$|^cx-|-azure$|-official$|-stable$|"
    r"-reverse|-mirror|-pro$)", re.I)

# 量化后缀 = 自建铁证（官方 API 永远不会给你 INT4 权重）
RE_QUANT = re.compile(
    r"(awq|gptq|gguf|int4|int8|fp8|fp16|w4a16|w8a8|exl2|bnb|4bit|8bit|"
    r"quantiz|q4_k_m|q5_k_m|q8_0)", re.I)

# 开放权重家族（注意：这些名字也能来自官方托管 API，所以只是弱证据）
RE_OPEN_FAMILY = re.compile(
    r"^(qwen|qwq|llama|mistral|mixtral|codestral|devstral|gemma|phi-|deepseek|"
    r"glm|chatglm|yi-|internlm|minicpm|internvl|nemotron|olmo|smollm|granite|"
    r"solar|exaone|falcon|vicuna|wizard|nous-|hermes|magistral|gpt-oss|"
    r"stablelm|zephyr|tinyllama|dolphin)", re.I)

# 万能中转面板才有的非对话类接口（自建推理站不会挂这些）
RE_PANEL = re.compile(r"^(mj_|suno_|kling_|luma_|runway_|pika_)", re.I)

# 推理引擎特征路径：命中 = 能直接看到自建引擎
# 注意 /health 被刻意排除：它太通用了，实测 sub.geiliapi.com / api.jmds.dev /
# cheaprouter.cc 三个站都回 {"status":"ok"}，但 /metrics 是 SPA 首页、/props 也是 SPA ——
# 那是反代或面板加的通用健康检查，不是推理引擎。判据必须能区分「有引擎」和「有个 /health 路由」。
ENGINE_PATHS = [
    ("/metrics", "vLLM/TGI Prometheus 指标（自建推理引擎最常见暴露面）"),
    ("/props", "llama.cpp server 的 /props（专有字段，SPA 假不了）"),
    ("/api/tags", "Ollama 模型清单"),
    ("/v1/completions", "原生 completion（转卖站通常只开 chat）"),
    ("/openapi.json", "自建 FastAPI 的 schema（含 vllm/sglang/llama 才算）"),
    ("/version", "推理引擎版本（含 vllm/tgi 才算）"),
]

PORTS = [80, 443, 3000, 3001, 5000, 7860, 8000, 8080, 8443, 8888, 9000,
         11434, 11435, 18080]


def classify_models(names):
    """按模型名成分给站点定性。返回证据与打分。"""
    names = [str(n or "") for n in names if n]
    if not names:
        return {}
    closed = [n for n in names if RE_CLOSED.search(n)]
    quant = [n for n in names if RE_QUANT.search(n)]
    alias = [n for n in names if RE_RESELL_ALIAS.search(n)]
    panel = [n for n in names if RE_PANEL.search(n)]
    openf = [n for n in names if RE_OPEN_FAMILY.search(n)]

    self_score, resell_score = 0, 0
    reasons = []

    if quant:
        self_score += 40
        reasons.append("发现 %d 个量化权重后缀（%s）—— 官方 API 不可能给你 INT4，这是自建铁证"
                       % (len(quant), ", ".join(quant[:3])))
    ratio_closed = len(closed) / len(names)
    if ratio_closed >= 0.7:
        resell_score += 40
        reasons.append("闭源官方模型名占 %.0f%%（%d/%d）—— 只能来自官方 API 或别人的号池"
                       % (ratio_closed * 100, len(closed), len(names)))
    if alias:
        resell_score += 20
        reasons.append("有 %d 个中间商自造别名（%s）—— 在二次封装别家渠道"
                       % (len(alias), ", ".join(alias[:3])))
    if panel:
        resell_score += 15
        reasons.append("挂了 %d 个非对话类面板接口（%s）—— 万能中转面板，不是推理站"
                       % (len(panel), ", ".join(panel[:3])))
    if len(names) > 40 and ratio_closed > 0.5:
        resell_score += 20
        reasons.append("一口气挂 %d 个模型且多为闭源 —— 一张卡跑不了这么多，物理上只能是渠道池"
                       % len(names))
    if len(names) <= 8 and openf and not closed:
        self_score += 25
        reasons.append("只挂 %d 个开放权重模型且无闭源 —— 自建站的典型形态" % len(names))

    if self_score >= 40 and self_score > resell_score:
        verdict = "self-host"
    elif resell_score >= 40:
        verdict = "reseller"
    elif not reasons:
        verdict = "unknown"
    else:
        verdict = "mixed"

    return {
        "verdict": verdict,
        "self_score": self_score,
        "resell_score": resell_score,
        "model_count": len(names),
        "closed_ratio": round(ratio_closed, 2),
        "quant_hits": quant[:6],
        "alias_hits": alias[:6],
        "panel_hits": panel[:6],
        "open_family_hits": openf[:8],
        "reasons": reasons,
    }


def http_get(host, path, timeout=8, tls=True):
    scheme = "https" if tls else "http"
    url = "%s://%s%s" % (scheme, host, path)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json, text/html"})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read(2048)
            return r.status, dict(r.headers), body
    except urllib.error.HTTPError as e:
        try:
            body = e.read(512)
        except Exception:
            body = b""
        return e.code, dict(e.headers or {}), body
    except Exception as e:
        return 0, {}, str(e)[:100].encode()


def probe_engine(host):
    """探推理引擎特征路径 + 响应头指纹。只读 GET，不提交任何数据。"""
    from concurrent.futures import ThreadPoolExecutor

    hits = []
    st, hdrs, _body = http_get(host, "/")
    server = (hdrs.get("Server") or hdrs.get("server") or "")
    powered = (hdrs.get("X-Powered-By") or "")
    newapi = any("oneapi" in k.lower() or "new-api" in k.lower() for k in hdrs)
    cf = any(k.lower() == "cf-ray" for k in hdrs)
    if server:
        hits.append("Server: %s" % server)
    if powered:
        hits.append("X-Powered-By: %s" % powered)
    if newapi:
        hits.append("new-api 特征头（X-Oneapi-*）")
    if cf:
        hits.append("Cloudflare 前置（看不到源站）")

    engine = []

    def _one(item):
        path, desc = item
        st2, h2, body2 = http_get(host, path, timeout=5)
        if st2 != 200 or not body2:
            return None
        ct = (h2.get("Content-Type") or h2.get("content-type") or "").lower()
        if "html" in ct:
            return None  # SPA 兜底路由会把所有路径都回 index.html，这不是引擎
        low = body2[:800].lower()
        if path == "/metrics" and (b"vllm" in low or b"# help" in low or b"tgi" in low):
            return desc
        if path == "/props" and (b"default_generation_settings" in low or b"n_slots" in low):
            return desc
        if path == "/api/tags" and (b'"models"' in low or b"ollama" in low):
            return desc
        if path == "/openapi.json" and (b"vllm" in low or b"sglang" in low or b"llama" in low):
            return desc
        if path == "/version" and (b"vllm" in low or b"tgi" in low):
            return desc
        return None

    with ThreadPoolExecutor(max_workers=len(ENGINE_PATHS)) as ex:
        for hit in ex.map(_one, ENGINE_PATHS):
            if hit:
                engine.append(hit)
    return {"headers": hits, "engine_hits": engine,
            "is_newapi": newapi, "behind_cdn": cf, "server": server}


def tcp_open(host, port, timeout=2.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def probe_ports(host):
    """只对已登记域名做 TCP connect，看端口开没开，不发任何数据。"""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=16) as ex:
        res = list(ex.map(lambda p: (p, tcp_open(host, p)), PORTS))
    return [p for p, ok in res if ok]


def main():
    from concurrent.futures import ThreadPoolExecutor

    argv = sys.argv[1:]
    online = "--online" in argv or "--ports" in argv
    only_ports = "--ports" in argv and "--online" not in argv

    data = json.load(open(SRC, encoding="utf-8"))
    rows = data["rows"]
    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "count": len(rows), "rows": []}

    for i, r in enumerate(rows, 1):
        site = r["site"]
        entry = {"site": site, "models": len(r.get("models") or [])}
        if not only_ports:
            entry["composition"] = classify_models([m.get("model") for m in (r.get("models") or [])])
        if online:
            print("[%d/%d] %s ..." % (i, len(rows), site), flush=True)
            entry["fingerprint"] = probe_engine(site)
            entry["ports_open"] = probe_ports(site)
        report["rows"].append(entry)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    # ---- 汇总 ----
    print("\n" + "=" * 74)
    print("上游成分判定（判据：命名成分 / 中间商别名 / 规模矛盾 / 引擎指纹）")
    print("=" * 74)
    scored = [e for e in report["rows"] if e.get("composition")]
    scored.sort(key=lambda e: (-e["composition"]["resell_score"], -e["models"]))
    for e in scored:
        c = e["composition"]
        print("%-24s %-9s 自建%3d / 转卖%3d  模型%4d  闭源占比%.0f%%"
              % (e["site"], c["verdict"], c["self_score"], c["resell_score"],
                 c["model_count"], c["closed_ratio"] * 100))
        for rs in c["reasons"][:3]:
            print("     · " + rs)

    if online:
        print("\n" + "=" * 74)
        print("运行时指纹（引擎 / CDN / 端口）")
        print("=" * 74)
        for e in report["rows"]:
            fp = e.get("fingerprint") or {}
            ports = e.get("ports_open") or []
            extra = " | ".join(fp.get("engine_hits") or [])
            print("%-24s 端口%-28s %s" % (e["site"], str(ports), extra or "-"))

    print("\n结论落盘 → %s" % OUT)


if __name__ == "__main__":
    main()

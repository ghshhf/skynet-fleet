#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""天网 · LLM 网关自有协议栈（llmgw.*）

来源（MIT，可合法融入）：
  - OFM service：`D:/Our Free Model/service`（Ebony-Vinyl，MIT），源码本机
  - zen-gate：`D:/code/vendor-src/zen-gate`（LAGcomcom，MIT），Go 源码
  - FreeLLMAPI：`D:/code/vendor-src/freellmapi`（tashfeenahmed，MIT）——
    学习其设计模式：匿名端点聚合、per-key cooldown、family routing、
    sticky session、hedging、X-Routed-Via 可溯源

本模块不依赖上面三者运行时，只复用它们的协议/设计，用 Python 标准库
实现一个 OpenAI 兼容客户端 + 路由器，自动发现 OFM/zen-gate 本地 key，
并聚合匿名端点（pollinations/llm7/kilo/OVH），支持跨网关·跨模型 fallback、
配额追踪、cooldown、粘性会话、家族路由。

能力（7 项）：
  llmgw.endpoints   — 列出所有可用网关（OFM/zen-gate/匿名）+ 健康探测
  llmgw.models      — 按网关列出模型（含 family 分类）
  llmgw.chat        — OpenAI 兼容 chat，跨网关·跨模型 fallback + cooldown + sticky
  llmgw.embeddings  — embeddings，家族内 failover（不同模型向量不混）
  llmgw.probe       — 单模型首字延迟（TTFB）
  llmgw.cooldowns   — 查看当前冷却中的端点/模型
  llmgw.sticky      — 查看/清除粘性会话
"""
import json
import os
import time
import urllib.error
import urllib.request

from ..core import BaseDevice, DeviceResult as R
from ..vendored_llmgw import LLMGW_CAPABILITIES

# ---------- 端点发现 ----------
OFM_SETTINGS = os.path.join(os.path.expanduser("~"), ".our-free-model", "settings.json")
ZENGATE_CONFIG = os.path.join(os.environ.get("APPDATA") or "", "zen-gate", "config.json")

# 匿名端点（FreeLLMAPI 设计：no key，直接打）
ANON_ENDPOINTS = [
    {"name": "pollinations", "base": "https://text.pollinations.ai/openai/v1",
     "key": "", "anon": True, "default_model": "openai-fast",
     "note": "GPT-OSS 20B on OVH，匿名可用"},
    {"name": "llm7", "base": "https://api.llm7.io/v1",
     "key": "", "anon": True, "default_model": "gpt-oss-20b",
     "note": "100 req/hr 匿名，聚合多上游"},
    {"name": "kilo", "base": "https://api.kilo.ai/api/gateway/v1",
     "key": "", "anon": True, "default_model": None,
     "note": "kilo :free 路由，匿名可用"},
    {"name": "ovh", "base": "https://api.ai.endpoints.ovh.net/v1",
     "key": "", "anon": True, "default_model": "Qwen3-235B-A22B",
     "note": "OVH AI Endpoints，匿名可用"},
]


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def discover_gateways():
    gws = []
    # OFM
    s = _load_json(OFM_SETTINGS)
    if s and s.get("forwardKey"):
        gws.append({"name": "ofm", "base": "http://127.0.0.1:18900",
                    "key": s["forwardKey"], "anon": False})
    # zen-gate
    c = _load_json(ZENGATE_CONFIG)
    if c and (c.get("mainKey") or c.get("MainKey")):
        key = c.get("mainKey") or c.get("MainKey")
        gws.append({"name": "zen-gate", "base": "http://127.0.0.1:8787",
                    "key": key, "anon": False})
    # 匿名端点（永远可用，不需 key）
    for a in ANON_ENDPOINTS:
        gws.append({"name": a["name"], "base": a["base"],
                    "key": a["key"], "anon": True,
                    "default_model": a.get("default_model"),
                    "note": a.get("note", "")})
    return gws


# ---------- 配额追踪 / cooldown / sticky（FreeLLMAPI 模式） ----------
_COOLDOWN = {}          # (gw, model) -> 解冻时间戳
_COOLDOWN_REASON = {}   # (gw, model) -> 原因
_STICKY = {}            # session_id -> {"gw":..,"model":..,"ts":..}
_STICKY_TTL = 30 * 60  # 30 分钟（FreeLLMAPI 同款）
_RATE = {}              # (gw, model) -> {"rpm":..,"rpd":..,"tpm":..,"tpd":..,"ts":..}
_COOLDOWN_MS = {"429": 60, "5xx": 30, "timeout": 45, "auth": 300}


def _now():
    return time.time()


def _cooldown_key(gw, model):
    return (gw, model or "*")


def is_cooled(gw, model):
    k = _cooldown_key(gw, model)
    until = _COOLDOWN.get(k, 0)
    if until and _now() < until:
        return True, round(until - _now(), 1)
    if until:
        del _COOLDOWN[k]
        _COOLDOWN_REASON.pop(k, None)
    return False, 0


def set_cooldown(gw, model, reason="429", seconds=None):
    k = _cooldown_key(gw, model)
    sec = seconds or _COOLDOWN_MS.get(reason, 60)
    _COOLDOWN[k] = _now() + sec
    _COOLDOWN_REASON[k] = reason


def record_rate(gw, model, tokens=0):
    k = _cooldown_key(gw, model)
    r = _RATE.setdefault(k, {"rpm": 0, "rpd": 0, "tpm": 0, "tpd": 0,
                             "window": _now()})
    if _now() - r["window"] > 60:
        r["rpm"] = 0
        r["tpm"] = 0
        r["window"] = _now()
    r["rpm"] += 1
    r["rpd"] += 1
    r["tpm"] += tokens
    r["tpd"] += tokens


def _family(model):
    """模型家族：不同家族的向量不混（FreeLLMAPI embeddings 设计）。"""
    if not model:
        return None
    m = model.lower()
    for fam in ("qwen", "llama", "gpt", "gemini", "glm", "deepseek",
                "mistral", "codestral", "nemotron", "kimi"):
        if fam in m:
            return fam
    return m.split("-")[0].split(":")[0]


def _req_json(base, path, key, timeout=15, method="GET", body=None):
    url = base.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, dict(r.headers), json.loads(raw), None
            except Exception:
                return r.status, dict(r.headers), {"_raw": raw}, None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, dict(e.headers), json.loads(raw), "HTTP %s" % e.code
        except Exception:
            return e.code, dict(e.headers), {"_raw": raw}, "HTTP %s" % e.code
    except Exception as e:
        return 0, {}, {"_err": str(e)}, str(e)[:200]


def _chat_once(gw, payload, timeout):
    st, hdrs, body, err0 = _req_json(gw["base"], "/v1/chat/completions", gw["key"],
                                timeout=timeout, method="POST", body=payload)
    if err0 and st == 0:
        return None, err0, None
    if st == 200 and isinstance(body, dict) and "choices" in body:
        via = (hdrs or {}).get("X-Routed-Via") or gw["name"]
        body["_routed_via"] = via
        return body, None, via
    err = ""
    if isinstance(body, dict):
        err = (body.get("error", {}) or {}).get("message") or body.get("_raw") or body.get("_err") or ""
    if not err:
        err = "HTTP %s" % st
    low = str(err).lower()
    if st == 429 or "rate" in low or "quota" in low:
        set_cooldown(gw["name"], payload.get("model"), "429")
    elif st >= 500 or "timeout" in low:
        set_cooldown(gw["name"], payload.get("model"), "5xx" if st >= 500 else "timeout")
    elif st in (401, 403):
        set_cooldown(gw["name"], payload.get("model"), "auth")
    return None, err, None


# ---------- 能力实现 ----------

def _h_endpoints(cap, args, dev):
    gws = discover_gateways()
    out = []
    for g in gws:
        st, _, body, err = _req_json(g["base"], "/v1/models", g["key"], timeout=8)
        nmodels = 0
        if st == 200 and isinstance(body, dict):
            nmodels = len(body.get("data") or body.get("models") or [])
        out.append({"name": g["name"], "base": g["base"],
                    "anon": g.get("anon", False), "ok": st == 200,
                    "models": nmodels, "err": None if st == 200 else ("HTTP %s" % st)})
    return R.success(cap, dev.name, via=dev.via, data={"endpoints": out,
                    "count": len(out), "online": sum(1 for x in out if x["ok"])})


def _h_models(cap, args, dev):
    gws = discover_gateways()
    result = {}
    for g in gws:
        st, _, body, err = _req_json(g["base"], "/v1/models", g["key"], timeout=10)
        ids = []
        if st == 200 and isinstance(body, dict):
            raw = body.get("data") or body.get("models") or []
            for m in raw:
                mid = m.get("id") or m.get("name") or (m if isinstance(m, str) else None)
                if mid:
                    ids.append({"id": mid, "family": _family(mid)})
        result[g["name"]] = {"count": len(ids), "models": ids[:300],
                             "ok": st == 200, "err": None if st == 200 else str(err)[:120]}
    return R.success(cap, dev.name, via=dev.via, data={"endpoints": result})


def _h_chat(cap, args, dev):
    messages = args.get("messages")
    if not messages:
        return R.error(cap, "messages 必填", device=dev.name, via=dev.via)
    models_arg = args.get("models") or args.get("model")
    if isinstance(models_arg, str):
        models = [models_arg]
    elif isinstance(models_arg, list):
        models = list(models_arg)
    else:
        models = []
    session = args.get("session")
    endpoint_pref = args.get("endpoint")
    temperature = args.get("temperature")
    max_tokens = int(args.get("max_tokens") or 1024)
    timeout = int(args.get("timeout") or 120)

    gws = discover_gateways()
    if endpoint_pref:
        gws = [g for g in gws if g["name"] == endpoint_pref] or gws
    if not gws:
        return R.unavailable(cap, "无可用网关（OFM/zen-gate 未装且匿名端点不可达）",
                             device=dev.name, via=dev.via)

    # sticky：优先复用上次成功的 (gw, model)
    if session and session in _STICKY:
        st = _STICKY[session]
        if _now() - st["ts"] < _STICKY_TTL:
            pref = [g for g in gws if g["name"] == st["gw"]]
            if pref:
                gws = pref + [g for g in gws if g["name"] != st["gw"]]
                if not models:
                    models = [st["model"]]

    # 默认模型：从首个可用网关取；匿名端点拿不到 /v1/models 时用 default_model
    if not models:
        for g in gws:
            st, _, body, err = _req_json(g["base"], "/v1/models", g["key"], timeout=8)
            ids = [m.get("id") for m in (body or {}).get("data", []) if m.get("id")] if (st == 200 and isinstance(body, dict)) else []
            if ids:
                models = [ids[0]]
                break
            if g.get("default_model"):
                models = [g["default_model"]]
                break
        if not models:
            return R.error(cap, "未指定 model 且拿不到模型清单", device=dev.name, via=dev.via)

    payload_base = {"messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        payload_base["temperature"] = temperature

    attempts = []
    candidates = []
    for m in models:
        for g in gws:
            cooled, _ = is_cooled(g["name"], m)
            if cooled:
                continue
            candidates.append((g, m))

    for g, m in candidates:
        p = dict(payload_base)
        p["model"] = m
        t0 = _now()
        res, err, via = _chat_once(g, p, timeout)
        lat = round(_now() - t0, 2)
        tokens = (res or {}).get("usage", {}).get("total_tokens", 0) if res else 0
        record_rate(g["name"], m, tokens=tokens)
        if res is not None:
            if session:
                _STICKY[session] = {"gw": g["name"], "model": m, "ts": _now()}
            routed = res.get("_routed_via") or via or g["name"]
            return R.success(cap, dev.name, via=dev.via, data={
                "result": res, "endpoint": g["name"], "base": g["base"],
                "model": m, "routed_via": routed, "latency_s": lat,
                "fallback_attempts": attempts, "session": session})
        attempts.append({"gateway": g["name"], "model": m, "err": (err or "")[:150],
                        "latency_s": lat})

    return R.error(cap, "全部候选失败：%s" % attempts, device=dev.name, via=dev.via)


def _h_embeddings(cap, args, dev):
    inputs = args.get("input") or args.get("inputs")
    if inputs is None:
        return R.error(cap, "input 必填", device=dev.name, via=dev.via)
    if isinstance(inputs, str):
        inputs = [inputs]
    model = args.get("model")
    endpoint_pref = args.get("endpoint")
    timeout = int(args.get("timeout") or 60)

    gws = discover_gateways()
    if endpoint_pref:
        gws = [g for g in gws if g["name"] == endpoint_pref] or gws

    if not model:
        for g in gws:
            st, _, body, err = _req_json(g["base"], "/v1/models", g["key"], timeout=8)
            ids = [m.get("id") for m in (body or {}).get("data", []) if m.get("id")] if (st == 200 and isinstance(body, dict)) else []
            emb = [i for i in ids if "embed" in i.lower() or "bge" in i.lower() or "e5" in i.lower()]
            if emb:
                model = emb[0]
                break
        if not model:
            return R.error(cap, "未指定 model 且拿不到 embeddings 模型", device=dev.name, via=dev.via)

    attempts = []
    for g in gws:
        cooled, _ = is_cooled(g["name"], model)
        if cooled:
            continue
        payload = {"model": model, "input": inputs}
        st, hdrs, body, _ = _req_json(g["base"], "/v1/embeddings", g["key"],
                                    timeout=timeout, method="POST", body=payload)
        if st == 200 and isinstance(body, dict) and "data" in body:
            body["_routed_via"] = (hdrs or {}).get("X-Routed-Via") or g["name"]
            return R.success(cap, dev.name, via=dev.via, data={
                "result": body, "endpoint": g["name"], "model": model,
                "routed_via": body["_routed_via"]})
        err = ""
        if isinstance(body, dict):
            err = (body.get("error", {}) or {}).get("message") or body.get("_raw") or body.get("_err") or ""
        attempts.append({"gateway": g["name"], "err": (err or "HTTP %s" % st)[:150]})
        if st == 429:
            set_cooldown(g["name"], model, "429")
        elif st >= 500:
            set_cooldown(g["name"], model, "5xx")
    return R.error(cap, "全部候选失败：%s" % attempts, device=dev.name, via=dev.via)


def _h_probe(cap, args, dev):
    model = args.get("model")
    if not model:
        return R.error(cap, "model 必填", device=dev.name, via=dev.via)
    gws = discover_gateways()
    ep = args.get("endpoint")
    if ep:
        gws = [g for g in gws if g["name"] == ep] or gws
    out = []
    for g in gws:
        cooled, _ = is_cooled(g["name"], model)
        if cooled:
            out.append({"gateway": g["name"], "ok": False, "ttfb_ms": None,
                        "err": "cooldown"})
            continue
        t0 = _now()
        st, hdrs, body, _ = _req_json(g["base"], "/v1/chat/completions", g["key"],
                                    timeout=int(args.get("timeout") or 30),
                                    method="POST",
                                    body={"model": model,
                                          "messages": [{"role": "user", "content": "hi"}],
                                          "max_tokens": 4})
        ttfb = round((_now() - t0) * 1000)
        ok = st == 200
        out.append({"gateway": g["name"], "ok": ok, "ttfb_ms": ttfb if ok else None,
                    "http": st, "err": None if ok else str(body)[:120]})
        if not ok and st in (429, 500, 502, 503):
            set_cooldown(g["name"], model, "429" if st == 429 else "5xx")
    return R.success(cap, dev.name, via=dev.via, data={"model": model, "probes": out})


def _h_cooldowns(cap, args, dev):
    now = _now()
    items = []
    for (gw, model), until in list(_COOLDOWN.items()):
        if until <= now:
            del _COOLDOWN[(gw, model)]
            _COOLDOWN_REASON.pop((gw, model), None)
            continue
        items.append({"gateway": gw, "model": model,
                      "reason": _COOLDOWN_REASON.get((gw, model)),
                      "remaining_s": round(until - now, 1)})
    return R.success(cap, dev.name, via=dev.via,
                     data={"cooldowns": items, "count": len(items)})


def _h_sticky(cap, args, dev):
    action = (args.get("action") or "list").lower()
    if action == "clear":
        sid = args.get("session")
        if sid and sid in _STICKY:
            del _STICKY[sid]
            return R.success(cap, dev.name, via=dev.via, data={"cleared": sid})
        if not sid:
            _STICKY.clear()
            return R.success(cap, dev.name, via=dev.via, data={"cleared": "all"})
        return R.error(cap, "session 不存在", device=dev.name, via=dev.via)
    now = _now()
    items = []
    for sid, st in list(_STICKY.items()):
        if now - st["ts"] > _STICKY_TTL:
            del _STICKY[sid]
            continue
        items.append({"session": sid, "gateway": st["gw"],
                      "model": st["model"], "age_s": round(now - st["ts"], 1)})
    return R.success(cap, dev.name, via=dev.via, data={"sessions": items, "count": len(items)})


_HANDLERS = {
    "llmgw.endpoints": _h_endpoints,
    "llmgw.models": _h_models,
    "llmgw.chat": _h_chat,
    "llmgw.embeddings": _h_embeddings,
    "llmgw.probe": _h_probe,
    "llmgw.cooldowns": _h_cooldowns,
    "llmgw.sticky": _h_sticky,
}


class LlmGwDevice(BaseDevice):
    name = "llmgw"
    via = "local"

    def __init__(self, name="llmgw:bench", **kw):
        super().__init__(name, via="local", **kw)

    def probe(self):
        gws = discover_gateways()
        return bool(gws), "可用网关：%s" % ", ".join(g["name"] for g in gws) if gws else "无本地网关"

    def caps(self):
        return tuple(_HANDLERS.keys())

    def invoke(self, cap, args=None):
        args = args or {}
        fn = _HANDLERS.get(cap)
        if not fn:
            return R.unsupported(cap, "llmgw 设备没有这个能力",
                                 device=self.name, via=self.via)
        return fn(cap, args, self)

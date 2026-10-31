# -*- coding: utf-8 -*-
"""llmgw_device —— llmgw.* 的真 handler：自有 OpenAI/Anthropic 客户端 + key 自动发现。"""

import json
import time
import urllib.error
import urllib.request

from ..core import BaseDevice, DeviceResult as R
from ..vendored_llmgw import LLMGW_CAPABILITIES

# 已知本地网关的 key/端口自动发现位置（都不硬编码 key 本体）
_OFM_SETTINGS = "C:/Users/admin/.our-free-model/settings.json"
_ZEN_CONFIG = "C:/Users/admin/AppData/Roaming/zen-gate/config.json"


def discover_gateways():
    """返回 [{name, base, key, source}]；只列 key 读到了的。"""
    out = []
    try:
        s = json.load(open(_OFM_SETTINGS, encoding="utf-8"))
        k = (s.get("forwardKey") or "").strip()
        if k:
            port = s.get("standalonePort") if isinstance(s.get("standalonePort"), int) else 18900
            out.append({"name": "ofm", "base": "http://127.0.0.1:%d" % port,
                        "key": k, "source": _OFM_SETTINGS})
    except (OSError, ValueError):
        pass
    try:
        c = json.load(open(_ZEN_CONFIG, encoding="utf-8"))
        k = (c.get("mainKey") or "").strip()
        if k:
            port = c.get("port") if isinstance(c.get("port"), int) else 8787
            out.append({"name": "zen-gate", "base": "http://127.0.0.1:%d" % port,
                        "key": k, "source": _ZEN_CONFIG})
    except (OSError, ValueError):
        pass
    return out


def _pick(endpoint, key):
    """按名字或 base 选端点；key 覆盖自动发现的。"""
    gws = discover_gateways()
    if endpoint:
        for g in gws:
            if g["name"] == endpoint or g["base"] == endpoint:
                if key:
                    g = dict(g, key=key)
                return g, gws
        return {"name": endpoint, "base": endpoint, "key": key or "", "source": "args"}, gws
    return (gws[0] if gws else None), gws


def _req_json(base, path, key, payload=None, timeout=60, stream=False):
    """真 HTTP。返回 (status, headers, body_or_fileobj, err)。"""
    url = base.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", "Bearer %s" % key)
    req.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except OSError:
            pass
        return e.code, None, None, "HTTP %s: %s" % (e.code, body)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return None, None, None, str(e)[:200]
    if stream:
        return resp.status, dict(resp.headers), resp, None
    body = resp.read().decode("utf-8", "replace")
    resp.close()
    try:
        return resp.status, dict(resp.headers), json.loads(body), None
    except ValueError:
        return resp.status, dict(resp.headers), body, None


class LlmGwDevice(BaseDevice):
    name = "llmgw"
    via = "本机"

    def __init__(self, name="llmgw:bench", **kw):
        super().__init__(name, via="local", **kw)

    def probe(self):
        gws = discover_gateways()
        return bool(gws), "可用网关：%s" % ", ".join(g["name"] for g in gws) if gws else "无本地网关（OFM/zen-gate 未装或未配置）"

    def caps(self):
        return tuple(_HANDLERS.keys())

    def invoke(self, cap, args=None):
        args = args or {}
        fn = _HANDLERS.get(cap)
        if not fn:
            return R.unsupported(cap, "llmgw 设备没有这个能力",
                                 device=self.name, via=self.via)
        return fn(cap, args, self)


def _h_endpoints(cap, args, dev):
    gws = discover_gateways()
    if args.get("base"):
        gws = gws + [{"name": "custom", "base": args["base"], "key": args.get("key") or "", "source": "args"}]
    rows = []
    for g in gws:
        t0 = time.time()
        st, _, body, err = _req_json(g["base"], "/v1/models", g["key"], timeout=8)
        lat = round((time.time() - t0) * 1000)
        n = len((body or {}).get("data") or []) if isinstance(body, dict) else None
        rows.append({"name": g["name"], "base": g["base"], "ok": err is None and st == 200,
                     "models": n, "latency_ms": lat,
                     "err": None if err is None else err[:120]})
    return R.success(cap, dev.name, via=dev.via, data={"endpoints": rows, "count": len(rows)})


def _h_models(cap, args, dev):
    gws = discover_gateways()
    want = args.get("endpoint")
    if args.get("base"):
        gws = gws + [{"name": "custom", "base": args["base"], "key": args.get("key") or "", "source": "args"}]
    out = {}
    for g in gws:
        if want and g["name"] != want and g["base"] != want:
            continue
        st, _, body, err = _req_json(g["base"], "/v1/models", g["key"], timeout=10)
        if err or not isinstance(body, dict):
            out[g["name"]] = {"ok": False, "err": (err or "unexpected body")[:150]}
            continue
        ids = sorted(m.get("id") or "" for m in body.get("data") or [])
        out[g["name"]] = {"ok": True, "count": len(ids), "models": ids}
    return R.success(cap, dev.name, via=dev.via, data={"endpoints": out})


def _chat_once(g, payload, timeout):
    st, _, body, err = _req_json(g["base"], "/v1/chat/completions", g["key"],
                                 payload=payload, timeout=timeout)
    if err:
        return None, err
    if not isinstance(body, dict):
        return None, "非 JSON 响应"
    if body.get("error"):
        return None, (body["error"].get("message") or "unknown")[:200]
    try:
        msg = (body.get("choices") or [{}])[0].get("message") or {}
    except (IndexError, AttributeError, TypeError):
        return None, "响应缺 choices"
    return {"model": body.get("model"), "content": msg.get("content"),
            "usage": body.get("usage"), "finish": (body.get("choices") or [{}])[0].get("finish_reason")}, None


def _h_chat(cap, args, dev):
    messages = args.get("messages")
    if not messages:
        return R.error(cap, "messages 必填", device=dev.name, via=dev.via)
    models = []
    if args.get("model"):
        models.append(args["model"])
    for m in (args.get("models") or []):
        if m not in models:
            models.append(m)
    g, gws_all = _pick(args.get("endpoint"), args.get("key"))
    if not g and not gws_all:
        return R.unavailable(cap, "无可用网关", device=dev.name, via=dev.via)
    payload = {"messages": messages, "max_tokens": int(args.get("max_tokens") or 1024)}
    if args.get("temperature") is not None:
        payload["temperature"] = args["temperature"]
    timeout = int(args.get("timeout") or 120)
    gateways = [g] if g else gws_all
    attempts = []
    for g in gateways:
        # 每个网关各取自己的模型清单做默认链（big-pickle 这类 429 了自动换）
        chain = list(models)
        if not chain:
            st, _, body, err = _req_json(g["base"], "/v1/models", g["key"], timeout=10)
            chain = [m.get("id") for m in (body or {}).get("data") or [] if m.get("id")] if not err else []
            if not chain:
                attempts.append({"gateway": g["name"], "err": "模型清单拿不到（%s）" % (err or "空")})
                continue
        default_gw = g["name"]
        for m in chain:
            p = dict(payload, model=m)
            t0 = time.time()
            res, err = _chat_once(g, p, timeout)
            lat = round(time.time() - t0, 2)
            if res is not None:
                return R.success(cap, dev.name, via=dev.via, data={
                    "result": res, "endpoint": default_gw, "base": g["base"],
                    "latency_s": lat, "fallback_attempts": attempts,
                    "default_model": m if not args.get("model") and not args.get("models") else None})
            attempts.append({"gateway": default_gw, "model": m, "err": err[:150], "latency_s": lat})
    return R.error(cap, "全部候选失败：%s" % attempts, device=dev.name, via=dev.via)


def _h_probe(cap, args, dev):
    model = args.get("model")
    if not model:
        return R.error(cap, "model 必填", device=dev.name, via=dev.via)
    timeout = int(args.get("timeout") or 60)
    gws = discover_gateways()
    if args.get("endpoint"):
        g, _ = _pick(args["endpoint"], args.get("key"))
        gws = [g] if g else []
    payload = {"model": model, "stream": True, "max_tokens": 8,
               "messages": [{"role": "user", "content": "ping"}]}
    last_err = None
    for g in gws:
        t0 = time.time()
        st, _, resp, err = _req_json(g["base"], "/v1/chat/completions", g["key"],
                                     payload=payload, timeout=timeout, stream=True)
        if err or resp is None:
            last_err = err or "no response"
            continue
        ttfb = None
        try:
            for raw in resp:
                if raw.strip():
                    ttfb = round((time.time() - t0) * 1000)
                    break
        except OSError as e:
            last_err = str(e)[:150]
            continue
        finally:
            try:
                resp.close()
            except OSError:
                pass
        if ttfb is None:
            last_err = "流建立但无数据"
            continue
        return R.success(cap, dev.name, via=dev.via, data={
            "model": model, "endpoint": g["name"], "available": True, "ttfb_ms": ttfb})
    return R.error(cap, "model=%s 不可用（%s）" % (model, (last_err or "无端点")[:180]),
                   device=dev.name, via=dev.via)


_HANDLERS = {
    "llmgw.endpoints": _h_endpoints,
    "llmgw.models": _h_models,
    "llmgw.chat": _h_chat,
    "llmgw.probe": _h_probe,
}

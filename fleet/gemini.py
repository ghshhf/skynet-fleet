# -*- coding: utf-8 -*-
"""天网 · Gemini 调用通道
================================================================
2026-11-02 打通。链路要点（踩过的坑都在这里，改之前先读）：

1. **服务账号不能直连 Gemini API**
   `generativelanguage.googleapis.com` 对 OAuth 服务账号直接返回
   403 `PERMISSION_DENIED`：Access to Gemini API is restricted with
   service accounts. Use authorization keys instead.
   ⇒ 必须用「绑定服务账号的 API key」（控制台里叫授权密钥 auth key，
     前缀是 `AQ.` 而不是老的 `AIza`）。

2. **key 必须绑到 Gemini API 这一项限制上**
   建 key 时不选「Gemini API」限制，列表里那一项是灰的、选不中，
   会卡在「无法与当前选择的 API 限制结合使用」。

3. **本机网络：直连通常不通，出网代理由 netproxy 自适应解析**
   （环境变量 → 系统代理 → 本机端口探测 → 直连），不再写死端口
   直连 TLS 直接断。但代理是**规则代理**，到 Google 的握手会间歇性
   失败（`schannel: failed to receive handshake`），实测要重试 1~6 次
   才拿到一次成功连接。所以这里必须带重试，不能一次定生死。

4. **Python requests 走同一个代理反而 SSL EOF**
   curl 能过、requests 过不去（TLS 指纹/ALPN 差异）。所以这里改用
   `subprocess` 调 curl，不跟 TLS 库较劲。这是实测结论，不是偏好。

5. **老模型对新用户下架**
   `gemini-2.5-flash` 会返回 404「no longer available to new users」，
   要换 `gemini-3.8-flash` 一类新代际。用 `list_models()` 现查。

用法：
    from fleet import gemini
    print(gemini.ask("一句话说明你在工作"))
    for m in gemini.list_models(): print(m)
"""
import json
import os
import subprocess
import sys
import tempfile

from . import netproxy
from . import vault
from . import text as _fleet_text

KEY_NAME = "gemini_api_key"
# 兼容别名；实际请求时重新解析，改环境变量或系统代理立即生效
PROXY = netproxy.proxy_url()
BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.8-flash"
# 降级链：主模型 503（高负载）或 404（下架）时依次往后退
FALLBACKS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
             "gemini-3.5-flash", "gemini-flash-latest"]
TRIES = 8


def _key():
    k = vault.load(KEY_NAME)
    if not k:
        raise RuntimeError(f"保险箱里没有 {KEY_NAME}，先 python -m fleet.vault put {KEY_NAME}")
    return k


def _curl(url, method="GET", body=None, timeout=45):
    """走 curl。返回 (http_code, text)。000 表示连接没建立。"""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    cmd = [
        "curl", "-s", "-o", path, "-w", "%{http_code}",
        "--max-time", str(timeout), "--http1.1",
        "-H", f"x-goog-api-key: {_key()}",
    ]
    proxy = netproxy.proxy_url()
    if proxy:                       # 没解析到代理就直连，不硬塞一个不存在的端口
        cmd += ["-x", proxy]
    if body is not None:
        rfd, rpath = tempfile.mkstemp(suffix=".json")
        with os.fdopen(rfd, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False)
        cmd += ["-H", "Content-Type: application/json", "-X", method, "--data", "@" + rpath, url]
    else:
        if method != "GET":
            cmd += ["-X", method]
        cmd.append(url)

    try:
        out = _fleet_text.run(cmd, timeout=timeout + 10)
        code = (out.stdout or "").strip() or "000"
        text = ""
        if os.path.exists(path):
            text = open(path, "r", encoding="utf-8", errors="replace").read()
        return code, text
    finally:
        for p in (path, locals().get("rpath")):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


def _curl_retry(url, method="GET", body=None, timeout=45, tries=TRIES):
    last = ("000", "")
    for i in range(tries):
        code, text = _curl(url, method, body, timeout)
        last = (code, text)
        if code != "000":
            return code, text
    return last


def list_models():
    code, text = _curl_retry(f"{BASE}/models")
    if code != "200":
        raise RuntimeError(f"list_models HTTP {code}: {text[:300]}")
    return [m["name"].split("/")[-1] for m in json.loads(text).get("models", [])]


def ask(prompt, model=None, system=None, timeout=90):
    """单轮问答。返回 (文本, usage)。主模型忙/下架时自动降级。"""
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    chain = [model] if model else list(FALLBACKS)
    errs = []
    for m in chain:
        code, text = _curl_retry(
            f"{BASE}/models/{m}:generateContent", "POST", body, timeout
        )
        if code == "200":
            d = json.loads(text)
            try:
                out = d["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError):
                raise RuntimeError(f"响应结构异常: {text[:400]}")
            return out, d.get("usageMetadata", {})
        errs.append(f"{m}:{code}")
        # 只对可降级的情况继续尝试
        if code not in ("503", "404", "429"):
            raise RuntimeError(f"ask HTTP {code}: {text[:400]}")
    raise RuntimeError(f"全部模型不可用 {errs}；最后一次 {text[:300]}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    q = " ".join(sys.argv[1:]) or "用一句中文说明你正在工作。"
    txt, usage = ask(q)
    print(txt)
    print(f"[用量] {usage.get('promptTokenCount')} → {usage.get('candidatesTokenCount')}")

"""Codex(ChatGPT 订阅号) 渠道的 OAuth 客户端 —— 天网自己的实现。

链路全部实测/源码核对自 FCC(`Alishahryar1/free-claude-code`, AGPL) 的
`providers/openai_codex/{login,auth,endpoint}.py`，这里只保留天网要用的部分：

    device code：POST /api/accounts/deviceauth/usercode {client_id}
                 → user_code + device_auth_id，人去 /codex/device 输码
                 → 轮询 /api/accounts/deviceauth/token（403/404 视为「还没输」）
                 → 拿到 authorization_code + code_verifier
                 → POST /oauth/token (form, grant_type=authorization_code) 换 3 个 token

    之后调后端：https://chatgpt.com/backend-api/codex/responses
    必须带 originator: codex_cli_rs + ChatGPT-Account-ID（从 access_token 的 JWT 里取
    `https://api.openai.com/auth`.`chatgpt_account_id`），且**不能带 max_output_tokens
    / metadata** —— 那两个字段 Codex 后端不收（FCC 里写作 omitted_request_fields）。

为什么不自己写一套 UI：device code 天生无浏览器，一条能力就能走完，
天网本来就是个 CLI/能力体，没必要为了「像桌面软件」而造界面。
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

ISSUER = "https://auth.openai.com"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ORIGINATOR = "codex_cli_rs"
SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
TOKEN_URL = ISSUER + "/oauth/token"
REVOKE_URL = ISSUER + "/oauth/revoke"
DEVICE_USERCODE_URL = ISSUER + "/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = ISSUER + "/api/accounts/deviceauth/token"
DEVICE_VERIFY_URL = ISSUER + "/codex/device"
DEVICE_REDIRECT_URI = ISSUER + "/deviceauth/callback"
CODEX_BASE = "https://chatgpt.com/backend-api/codex"
USER_AGENT = ORIGINATOR + "/0.51.0"

REFRESH_EARLY_S = 300          # 提前 5 分钟刷新（FCC 同款）
LOGIN_LIFETIME_S = 15 * 60

AUTH_PATH = os.path.join(os.path.expanduser("~"), ".fleet", "codex_auth.json")
PENDING_PATH = os.path.join(os.path.expanduser("~"), ".fleet", "codex_pending.json")

# auth.openai.com 会按出口 IP 判地区，大陆 IP 直接 403
# unsupported_country_region_territory。所以这里必须能走代理：
#   显式 LLMW_CODEX_PROXY > 标准 HTTPS_PROXY/HTTP_PROXY > 本机常见代理端口（探活）
_PROXY_CACHE = {"addr": "?", "ts": 0.0}
_PROXY_PROBE = (3067, 7890, 7897, 10809, 1080, 10808, 2080)


def _alive(port, timeout=0.15):
    try:
        import socket
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def proxy_addr():
    """返回出口代理地址（http://host:port）或 None。缓存 30s。"""
    now = time.time()
    if _PROXY_CACHE["addr"] != "?" and now - _PROXY_CACHE["ts"] < 30:
        return _PROXY_CACHE["addr"]
    addr = os.environ.get("LLMW_CODEX_PROXY") or os.environ.get("HTTPS_PROXY") \
        or os.environ.get("HTTP_PROXY") or os.environ.get("ALL_PROXY")
    if not addr:
        for p in _PROXY_PROBE:
            if _alive(p):
                addr = "http://127.0.0.1:%d" % p
                break
    if addr and "://" not in addr:
        addr = "http://" + addr
    _PROXY_CACHE["addr"] = addr
    _PROXY_CACHE["ts"] = now
    return addr


def _req(url, *, data=None, form=False, method="POST", timeout=30, via_proxy=True):
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT,
               "originator": ORIGINATOR}
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    handlers = []
    px = proxy_addr() if via_proxy else None
    if px:
        handlers.append(urllib.request.ProxyHandler({"http": px, "https": px}))
    handlers += [urllib.request.HTTPHandler(), urllib.request.HTTPSHandler()]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw), None
            except Exception:
                return r.status, {"_raw": raw}, None
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        try:
            return e.code, json.loads(raw), "HTTP %s" % e.code
        except Exception:
            return e.code, {"_raw": raw[:400]}, "HTTP %s" % e.code
    except Exception as e:
        return 0, {}, str(e)[:200]


def _jwt_claims(tok):
    try:
        seg = tok.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        d = json.loads(base64.urlsafe_b64decode(seg))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _account_id(access_token):
    c = _jwt_claims(access_token)
    auth = c.get("https://api.openai.com/auth") or {}
    return auth.get("chatgpt_account_id") if isinstance(auth, dict) else None


def _email(id_token):
    c = _jwt_claims(id_token)
    return c.get("email") or ((c.get("https://api.openai.com/profile") or {})
                              .get("email"))


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save(path, obj):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    try:
        os.replace(tmp, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except Exception:
        pass


# ---------- 登录：device code，两步（start / poll）----------
def login_start():
    """申请 device code。返回给用户的是 user_code + 验证地址，token 一律不回显。"""
    st, d, err = _req(DEVICE_USERCODE_URL, data={"client_id": CLIENT_ID})
    if st != 200:
        return {"ok": False, "error": err or ("HTTP %s" % st),
                "detail": json.dumps(d, ensure_ascii=False)[:200]}
    device_auth_id = d.get("device_auth_id")
    user_code = d.get("user_code") or d.get("usercode")
    if not device_auth_id or not user_code:
        return {"ok": False, "error": "device code 响应缺字段",
                "detail": json.dumps(d, ensure_ascii=False)[:200]}
    try:
        interval = max(1.0, float(d.get("interval") or 5))
    except (TypeError, ValueError):
        interval = 5.0
    _save(PENDING_PATH, {"device_auth_id": device_auth_id, "user_code": user_code,
                         "interval": interval,
                         "expires_at": int(time.time()) + LOGIN_LIFETIME_S})
    return {"ok": True, "user_code": user_code, "verification_url": DEVICE_VERIFY_URL,
            "interval_s": interval, "expires_in_s": LOGIN_LIFETIME_S,
            "next": "去 verification_url 输入 user_code 授权，然后调 action=poll"}


def login_poll(wait_s=0):
    """轮询一次（wait_s>0 则持续轮询到成功/超时）。403/404 = 用户还没输码。"""
    p = _load(PENDING_PATH)
    if not p:
        return {"ok": False, "error": "没有进行中的登录，先 action=start"}
    if int(time.time()) > int(p.get("expires_at") or 0):
        return {"ok": False, "error": "device code 已过期，重新 action=start"}
    deadline = time.time() + max(0, wait_s)
    last = None
    while True:
        st, d, err = _req(DEVICE_TOKEN_URL,
                          data={"device_auth_id": p["device_auth_id"],
                                "user_code": p["user_code"]})
        if st == 200:
            code = d.get("authorization_code")
            verifier = d.get("code_verifier")
            if not code or not verifier:
                return {"ok": False, "error": "device 授权响应缺 code/verifier"}
            return _exchange(code, verifier)
        if st not in (403, 404):
            return {"ok": False, "error": err or ("HTTP %s" % st),
                    "detail": json.dumps(d, ensure_ascii=False)[:200]}
        last = "waiting"
        if time.time() >= deadline:
            break
        time.sleep(p.get("interval") or 5)
    return {"ok": False, "pending": True, "error": "用户还没完成授权",
            "user_code": p.get("user_code"), "verification_url": DEVICE_VERIFY_URL}


def _exchange(code, verifier):
    st, d, err = _req(TOKEN_URL, form=True, data={
        "grant_type": "authorization_code", "client_id": CLIENT_ID,
        "code": code, "redirect_uri": DEVICE_REDIRECT_URI,
        "code_verifier": verifier})
    if st != 200:
        return {"ok": False, "error": err or ("HTTP %s" % st),
                "detail": json.dumps(d, ensure_ascii=False)[:200]}
    return _store_tokens(d)


def _store_tokens(d, previous=None):
    at = d.get("access_token") or (previous or {}).get("access_token")
    rt = d.get("refresh_token") or (previous or {}).get("refresh_token")
    it = d.get("id_token") or (previous or {}).get("id_token")
    if not (at and rt and it):
        return {"ok": False, "error": "token 响应不完整（缺 access/refresh/id）"}
    acc = _account_id(at)
    if not acc:
        return {"ok": False, "error": "access_token 里没有 chatgpt_account_id —— "
                                     "这个账号可能不是 ChatGPT 订阅号"}
    claims = _jwt_claims(at)
    rec = {"version": 1, "access_token": at, "refresh_token": rt, "id_token": it,
           "account_id": acc, "email": _email(it),
           "expires_at": int(claims.get("exp") or (time.time() + 8 * 86400)),
           "saved_at": int(time.time())}
    _save(AUTH_PATH, rec)
    try:
        os.unlink(PENDING_PATH)
    except Exception:
        pass
    return {"ok": True, "account_id": acc, "email": rec["email"],
            "expires_at": rec["expires_at"], "saved": AUTH_PATH}


# ---------- 令牌生命周期 ----------
def status():
    r = _load(AUTH_PATH)
    if not r or not r.get("access_token"):
        return {"connected": False}
    left = int(r.get("expires_at") or 0) - int(time.time())
    return {"connected": True, "account_id": r.get("account_id"),
            "email": r.get("email"), "expires_in_s": left,
            "needs_refresh": left <= REFRESH_EARLY_S,
            "saved": AUTH_PATH}


def access(force_refresh=False):
    """返回 (access_token, account_id) 或 (None, 错误说明)。快过期会自动续。"""
    r = _load(AUTH_PATH)
    if not r or not r.get("access_token"):
        return None, "未连接 ChatGPT 订阅号（llmgw.codex_login action=start）"
    if force_refresh or int(r.get("expires_at") or 0) - int(time.time()) <= REFRESH_EARLY_S:
        st, d, err = _req(TOKEN_URL, data={"grant_type": "refresh_token",
                                          "client_id": CLIENT_ID,
                                          "refresh_token": r["refresh_token"]})
        if st in (400, 401, 403):
            # 刷新被拒 = 授权已失效，清掉让上层重新登录（不留半死状态）
            try:
                os.unlink(AUTH_PATH)
            except Exception:
                pass
            return None, "刷新被拒（HTTP %s），授权已失效，请重新 codex_login" % st
        if st != 200:
            # 瞬时故障：用旧 token 硬撑，过期后自然会再走这条路
            return r["access_token"], None
        got = _store_tokens(d, previous=r)
        if not got.get("ok"):
            return r["access_token"], None
        r = _load(AUTH_PATH) or r
    return r["access_token"], None


def logout():
    r = _load(AUTH_PATH)
    if r and r.get("refresh_token"):
        _req(REVOKE_URL, data={"client_id": CLIENT_ID, "token": r["refresh_token"],
                               "token_type_hint": "refresh_token"})
    for p in (AUTH_PATH, PENDING_PATH):
        try:
            os.unlink(p)
        except Exception:
            pass
    return {"ok": True, "removed": True}


def headers(account_id, session_id=None):
    """Codex 后端要求的请求头。少 originator 或 ChatGPT-Account-ID 会被拒。"""
    h = {"originator": ORIGINATOR, "User-Agent": USER_AGENT,
         "ChatGPT-Account-ID": account_id, "Accept": "text/event-stream"}
    if session_id:
        h["session_id"] = session_id
    return h


def access_and_account(force_refresh=False):
    """给网关用的一步式取凭证：返回 (token, account_id, err)。"""
    tok, err = access(force_refresh=force_refresh)
    if not tok:
        return None, None, err
    rec = _load(AUTH_PATH) or {}
    acc = rec.get("account_id")
    if not acc:
        return None, None, "凭据里没有 account_id，请重新登录"
    return tok, acc, None

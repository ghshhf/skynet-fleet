# -*- coding: utf-8 -*-
"""Kiro（AWS 官方 AI IDE）凭据与 token 管理。

为什么值得做：Kiro 免费层（不用信用卡、不用 AWS 账号）自带
`claude-sonnet-4.5` / `claude-haiku-4.5`，Amazon 是 Anthropic 最大股东，
跑的是原生 Claude。跟 agentrouter 同一类逻辑——正规订阅套一层，不需要破解。

两条真门槛，都不是对抗性的：
  1. 凭据来自官方 CLI 的 device-code 授权（人输一次验证码），存在 CLI 的 sqlite 里；
  2. token 刷新走 **AWS SSO OIDC**，不是老版 `prod.*.auth.desktop.kiro.dev/refreshToken`。
     后者实测四种组合全 401（`Bad credentials`），只有
     `POST https://oidc.{region}.amazonaws.com/token`
     + `{clientId, clientSecret, grantType:"refresh_token", refreshToken}` 才 200。

refresh_token 每次刷新都会轮换，必须写回，否则下次 401。
"""
import json
import os
import sqlite3

CRED_FILE = os.path.join(os.path.expanduser("~"), ".fleet", "kiro.json")
DEFAULT_REGION = "us-east-1"


def _cli_db_candidates():
    """Kiro CLI 把凭据存在 data.sqlite3（用户目录，per-user 安装）。"""
    la = os.environ.get("LOCALAPPDATA") or ""
    home = os.path.expanduser("~")
    return [
        os.path.join(la, "kiro-cli", "data.sqlite3"),
        os.path.join(la, "Kiro-Cli", "data.sqlite3"),
        os.path.join(home, ".local", "share", "kiro-cli", "data.sqlite3"),
    ]


def _from_cli_db():
    """直接从 CLI 的 sqlite 读凭据（不需要 root，也没有加密）。"""
    for p in _cli_db_candidates():
        if not os.path.isfile(p):
            continue
        try:
            con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
            rows = dict(con.execute("SELECT key, value FROM auth_kv").fetchall())
            con.close()
            tok = json.loads(rows.get("kirocli:odic:token") or "{}")
            reg = json.loads(rows.get("kirocli:odic:device-registration") or "{}")
        except Exception:
            continue
        if not tok.get("refresh_token"):
            continue
        return {
            "refresh_token": tok["refresh_token"],
            "client_id": reg.get("client_id") or "",
            "client_secret": reg.get("client_secret") or "",
            "region": tok.get("region") or reg.get("region") or DEFAULT_REGION,
            "profile_arn": tok.get("profile_arn") or "",
            "access_token": tok.get("access_token") or "",
            "expires_at": tok.get("expires_at") or "",
            "source": p,
        }
    return None


def load():
    """凭据：~/.fleet/kiro.json 优先，否则从 Kiro CLI 的 sqlite 自动取。"""
    d = None
    if os.path.isfile(CRED_FILE):
        try:
            with open(CRED_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = None
    if not d or not d.get("refresh_token"):
        d = _from_cli_db()
    return d


def save(d):
    try:
        os.makedirs(os.path.dirname(CRED_FILE), exist_ok=True)
        tmp = CRED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CRED_FILE)
    except Exception:
        pass


def _refresh_http(d):
    """AWS SSO OIDC 换 token。返回新 dict，失败抛异常。"""
    import urllib.request

    region = d.get("region") or DEFAULT_REGION
    url = "https://oidc.%s.amazonaws.com/token" % region
    body = json.dumps({
        "clientId": d.get("client_id") or "",
        "clientSecret": d.get("client_secret") or "",
        "grantType": "refresh_token",
        "refreshToken": d.get("refresh_token") or "",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "Kiro-CLI"})
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    at = j.get("accessToken")
    if not at:
        raise RuntimeError("refresh 响应里没有 accessToken")
    return j


def access_token(d=None, force=False):
    """拿一个有效 access_token。有效期内直接用，过期或 force 就刷新并写回。

    返回 (token, cred_dict, err)。err 非空表示失败。
    """
    import time

    d = d or load()
    if not d:
        return "", None, "没有 Kiro 凭据（先跑 kiro-cli login）"
    now = time.time()
    exp = 0.0
    try:
        ea = (d.get("expires_at") or "").replace("Z", "+00:00")
        if ea:
            from datetime import datetime
            exp = datetime.fromisoformat(ea).timestamp()
    except Exception:
        exp = 0.0
    at = d.get("access_token") or ""
    # 留 5 分钟余量
    if at and not force and exp and now < exp - 300:
        return at, d, ""
    try:
        j = _refresh_http(d)
    except Exception as e:
        # 刷新失败但手上的 token 还没到期，就用旧的（别让一次网络抖动断掉整条链）
        if at and exp and now < exp:
            return at, d, ""
        return "", d, "refresh 失败：%s" % str(e)[:160]
    d["access_token"] = j.get("accessToken") or at
    if j.get("refreshToken"):
        d["refresh_token"] = j["refreshToken"]      # 会轮换，必须写回
    if j.get("profileArn"):
        d["profile_arn"] = j["profileArn"]
    d["expires_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + int(j.get("expiresIn") or 3600)))
    save(d)
    return d["access_token"], d, ""


def status():
    """给能力层看的体检信息：只报有没有、什么时候过期，不回显任何凭据值。"""
    d = load()
    if not d:
        return {"connected": False, "reason": "没有凭据（kiro-cli login 后自动识别）"}
    import time
    exp = 0.0
    try:
        ea = (d.get("expires_at") or "").replace("Z", "+00:00")
        if ea:
            from datetime import datetime
            exp = datetime.fromisoformat(ea).timestamp()
    except Exception:
        pass
    return {
        "connected": True,
        "region": d.get("region") or DEFAULT_REGION,
        "source": d.get("source") or CRED_FILE,
        "has_access_token": bool(d.get("access_token")),
        "access_expires_in_s": int(max(0, exp - time.time())) if exp else None,
        "has_client_secret": bool(d.get("client_secret")),
    }

# -*- coding: utf-8 -*-
"""天网 · 注册工具箱（把「注册一个站」拆成可复用的零件）
================================================================

一条判据（2026-11-03 主人定的）
------------------------------
注册不是一个动作，是**四段流水**，每段的失败原因完全不同：

    L1 身份   邮箱 / 手机号      —— 有没有「能收码的地址」
    L2 通道   密码 / 邮件码 / OAuth —— 站方要哪一种
    L3 闸门   captcha / 滑块 / 算力挑战 —— 能不能过
    L4 兜底   手机绑定            —— 这一段的出口常常只有人（扫码）

所以「注册失败」这句话没有信息量。必须报到 **L1~L4 的哪一段**，
并且带证据（哪个端点说的、它说了什么）。

三条边界（写死）
----------------
1. **不猜站点规则。** 规则从站方公开端点读（Sub2API 的 `/api/v1/settings/public`、
   Clerk 的 `/v1/environment`），读不到就报 `unknown`，不许按经验编。
2. **不产生假成功。** 没 key 报 `no_key`，没余额报 `no_credit`，
   不许返回一个「应该能行」的字符串。
3. **人工交接是正式出口，不是失败。** 有些段（Google 的扫码验证）本来就只有人能过，
   这时正确行为是**吐出准确的地址和动作**，而不是硬撑。

为什么单独成一个模块
--------------------
前面三次失败的教训：注册逻辑散在一次性脚本里，改一次丢一次，
而且每次都在「到底是邮箱不行、还是 IP 不行、还是输错了」之间反复猜。
把它抽成数据 + 纯函数之后，猜的部分就没有了 —— 每个判断都能回指一个端点。
"""
import json
import re
import secrets
import string
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")

#: 收码超时的默认上限。接码平台的经验值也是 120s 起 —— 短了就是白买号。
DEFAULT_WAIT = 180


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _req(url, data=None, token=None, timeout=20, method=None, headers=None):
    """只做一件事：把 JSON 拿回来。异常一律转成 (None, 原因)，不抛给上层。"""
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
    r = urllib.request.Request(url, data=body, method=method)
    r.add_header("User-Agent", UA)
    r.add_header("Accept", "application/json")
    if body is not None:
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return json.loads(raw), ""
            except ValueError:
                return {"_raw": raw[:4000]}, ""
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            detail = ""
        return None, "HTTP %s %s" % (e.code, detail)
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:160])


def _get_html(url, timeout=20):
    r = urllib.request.Request(url)
    r.add_header("User-Agent", UA)
    r.add_header("Accept", "text/html,application/xhtml+xml")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace"), resp.headers, ""
    except urllib.error.HTTPError as e:
        try:
            return e.read().decode("utf-8", "replace"), e.headers, "HTTP %s" % e.code
        except Exception:
            return "", {}, "HTTP %s" % e.code
    except Exception as e:
        return "", {}, "%s: %s" % (type(e).__name__, str(e)[:160])


def _rand(n=10, alphabet=None):
    alphabet = alphabet or (string.ascii_lowercase + string.digits)
    return "".join(secrets.choice(alphabet) for _ in range(n))


# --------------------------------------------------------------------------
# L1 · 邮箱（临时信箱适配器）
# --------------------------------------------------------------------------

def _mailtm_new(prefix="u"):
    dom, err = _req("https://api.mail.tm/domains?page=1")
    if err or not isinstance(dom, list) or not dom:
        return None, "拿不到 mail.tm 域名：%s" % (err or "返回空")
    domain = dom[0]["domain"]
    addr = "%s%s@%s" % (prefix, _rand(10), domain)
    pwd = "Mb!" + _rand(16, string.ascii_letters + string.digits)
    _, err = _req("https://api.mail.tm/accounts", {"address": addr, "password": pwd})
    if err:
        return None, "建 mail.tm 账号失败：%s" % err
    tok, err = _req("https://api.mail.tm/token", {"address": addr, "password": pwd})
    if err or not tok or "token" not in tok:
        return None, "mail.tm 取 token 失败：%s" % (err or "无 token")
    return {"provider": "mailtm", "address": addr, "password": pwd,
            "token": tok["token"], "domain": domain, "at": time.time()}, ""


def _mailtm_poll(mb):
    j, err = _req("https://api.mail.tm/messages?page=1", token=mb.get("token"))
    if err:
        return [], err
    out = []
    for m in (j or {}).get("hydra:member", []) if isinstance(j, dict) else []:
        mid = m.get("id")
        full, e2 = _req("https://api.mail.tm/messages/" + str(mid), token=mb.get("token"))
        if e2:
            continue
        html = full.get("html")
        if isinstance(html, list):
            html = " ".join(str(x) for x in html)
        out.append({"id": mid,
                    "subject": str(full.get("subject") or ""),
                    "text": str(full.get("text") or "") + " " + str(html or ""),
                    "from": str((full.get("from") or {}).get("address") or "")})
    return out, ""


def _tempmail_lol_new(prefix="u"):
    j, err = _req("https://api.tempmail.lol/v2/inbox/create", {}, method="POST")
    if err or not j or "address" not in j:
        return None, "建 tempmail.lol 信箱失败：%s" % (err or "无 address")
    return {"provider": "tempmail_lol", "address": j["address"], "token": j["token"],
            "domain": j["address"].split("@")[-1], "at": time.time()}, ""


def _tempmail_lol_poll(mb):
    j, err = _req("https://api.tempmail.lol/v2/inbox?token=" + str(mb.get("token")))
    if err:
        return [], err
    out = []
    for e in (j or {}).get("emails") or []:
        html = e.get("html")
        if isinstance(html, list):
            html = " ".join(str(x) for x in html)
        out.append({"id": e.get("date") or e.get("id") or "",
                    "subject": str(e.get("subject") or ""),
                    "text": str(e.get("body") or "") + " " + str(html or ""),
                    "from": str(e.get("from") or "")})
    return out, ""


MAIL_PROVIDERS = {
    "mailtm": {"new": _mailtm_new, "poll": _mailtm_poll,
               "note": "mail.tm —— 免费、要建账号、域名池小（fldubai.com 之类）"},
    "tempmail_lol": {"new": _tempmail_lol_new, "poll": _tempmail_lol_poll,
                     "note": "tempmail.lol —— 免注册，域名是三级（cmb.xxxx.com）"},
}


def mailbox_new(provider="mailtm", prefix="u"):
    p = MAIL_PROVIDERS.get(provider)
    if not p:
        return None, "不认识的邮箱服务：%s（可选 %s）" % (provider, "/".join(MAIL_PROVIDERS))
    mb, err = p["new"](prefix)
    if err:
        return None, err
    disposable, why = looks_disposable(mb["domain"])
    mb["disposable"] = disposable
    mb["disposable_reason"] = why
    return mb, ""


def mailbox_poll(mb):
    p = MAIL_PROVIDERS.get((mb or {}).get("provider"))
    if not p:
        return [], "信箱记录里没有可识别的 provider"
    return p["poll"](mb)


#: Clerk / 站方常见的一次性域名判定，宁可报「可疑」也不报「干净」。
_DISPOSABLE_HINTS = (
    "mail.tm", "fldubai", "arcadehearth", "tempmail", "1secmail", "guerrilla",
    "mailinator", "yopmail", "sharklasers", "10minutemail", "temp-mail",
    "trashmail", "getnada", "dispostable", "throwaway", "maildrop", "mohmal",
)


def looks_disposable(domain):
    """粗判。返回 (可疑?, 理由)。**判不出来就报可疑** —— 反了会白烧账号。"""
    d = (domain or "").strip().lower()
    if not d:
        return True, "域名为空"
    if d.count(".") >= 2:
        return True, "三级及以上域名（%s）—— 一次性邮箱的典型形状" % d
    for h in _DISPOSABLE_HINTS:
        if h in d:
            return True, "命中已知一次性邮箱特征（%s）" % h
    return False, ""


# --------------------------------------------------------------------------
# L2/L3 · 站点画像
# --------------------------------------------------------------------------

#: 已经**实测确认**过的站点。带日期，过期就该重测，不许当永久事实。
PROFILES = {
    "openrouter.ai": {
        "checked": "2026-11-02",
        "engine": "clerk",
        "register_url": "https://openrouter.ai/sign-up",
        "login_url": "https://openrouter.ai/sign-in",
        "keys_url": "https://openrouter.ai/settings/keys",
        "channels": ["email+password", "oauth_google", "oauth_github", "web3_wallet"],
        "captcha": "无图形验证；Clerk 的 proof_of_transfer 是**算力挑战**"
                   "（req_up/req_dn=2000000），页面里 clerk-js 自己算完，不用逆向",
        "email_policy": {"block_disposable": True, "block_subaddress": True,
                         "note": "实测 fldubai.com / arcadehearth.com 都被 Clerk 拒："
                                 "Temporary email services are not supported"},
        "hard_step": "L4 —— Google 账号创建的最后一步是**手机扫码**，纯脚本过不去",
        "quota": "免费模型 50 次/天（累计充值 <9 刀）；>=9 刀升 1000 次/天。"
                 "多开账号/多开 key 都不涨额度（官方原文：容量按全局治理）",
    },
    "go.toter.me": {
        "checked": "2026-11-02",
        "engine": "nextjs",
        "register_url": "https://go.toter.me/register",
        "login_url": "https://go.toter.me/login",
        "channels": ["email+code"],
        "captcha": "关闭",
        "email_policy": {"suffix_whitelist": ["@qq.com"]},
        "api": ["/api/auth/send-email-code", "/api/auth/register",
                "/api/auth/remembered-login", "/api/user/signin"],
        "quota": "permanentQuota 注册送 5000（长期）+ dailyQuota 签到 1000~3000（当日清零，先烧）",
    },
    "livelove.dev": {
        "checked": "2026-11-02",
        "engine": "sub2api",
        "register_url": "https://livelove.dev/register",
        "login_url": "https://livelove.dev/login",
        "channels": ["email+password"],
        "captcha": "turnstile 开启但**被它自己的 CSP 弄坏**"
                   "（connect-src 拒绝 data:, → widget 不渲染 → 后端恒定 TURNSTILE_VERIFICATION_FAILED）",
        "email_policy": {"suffix_whitelist": ["@qq.com"]},
        "public_api": "/api/v1/settings/public",
        "note": "这个端点直接公开全部注册规则，摸站第一步就该打它",
    },
}

_SUFFIX_KEYS = ("registration_email_suffix_whitelist", "email_suffix_whitelist",
                "emailSuffixWhitelist")
_OAUTH_KEYS = ("google_oauth_enabled", "github_oauth_enabled", "linuxdo_oauth_enabled",
               "wechat_oauth_enabled", "dingtalk_oauth_enabled", "oidc_oauth_enabled")


def _pick(d, keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d:
            return d[k]
    return default


def detect_engine(html="", headers=None, body=None):
    """按证据判面板类型。判不出来返回 unknown —— 不猜。"""
    h = (html or "").lower()
    hd = {str(k).lower(): str(v).lower() for k, v in (headers or {}).items()}
    if isinstance(body, dict):
        if _pick(body, _SUFFIX_KEYS) is not None:
            return "sub2api"
        if "auth_config" in body or "user_settings" in body:
            return "clerk"
    if "/_next/static/" in h or "__next_data__" in h:
        return "nextjs"
    if hd.get("x-powered-by", "").find("next.js") >= 0:
        return "nextjs"
    if "clerk.browser.js" in h or "clerk-js" in h:
        return "clerk"
    return "unknown"


def recon(domain, timeout=12):
    """给一个域名，把「注册这件事」的规则尽量读出来。

    探测顺序是**按信息密度排的**，不是随便试：
        1. Sub2API 的 public settings —— 一次响应给全规则，最划算
        2. Clerk 的 environment    —— 一次响应给全部登录方式
        3. 首页 HTML               —— 兜底，只能看出面板类型和验证码厂商
    """
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0]
    out = {"domain": d, "engine": "unknown", "evidence": [], "channels": [],
           "email_policy": {}, "captcha": [], "ok": False}

    # 1) Sub2API
    body, err = _req("https://%s/api/v1/settings/public" % d, timeout=timeout)
    if isinstance(body, dict) and _pick(body, _SUFFIX_KEYS) is not None:
        out["engine"] = "sub2api"
        out["evidence"].append("/api/v1/settings/public 直接命中")
        out["email_policy"] = {"suffix_whitelist": list(_pick(body, _SUFFIX_KEYS) or [])}
        for k in _OAUTH_KEYS:
            if body.get(k) is True:
                out["channels"].append(k.replace("_enabled", ""))
        if body.get("turnstile_enabled"):
            out["captcha"].append("turnstile")
        if body.get("password_login_enabled") is not False:
            out["channels"].insert(0, "email+password")
        if body.get("registration_enabled") is False:
            out["email_policy"]["registration"] = "closed"
        out["ok"] = True
        return out

    # 2) Clerk
    ck, err2 = _req("https://clerk.%s/v1/environment"
                    "?__clerk_api_version=2025-11-10&_clerk_js_version=6.41.0" % d,
                    timeout=timeout)
    if isinstance(ck, dict) and ("auth_config" in ck or "user_settings" in ck):
        out["engine"] = "clerk"
        out["evidence"].append("clerk.%s/v1/environment 可读（公开实例配置）" % d)
        us = ck.get("user_settings") or {}
        soc = us.get("social") or {}
        for k, v in soc.items():
            if isinstance(v, dict) and v.get("enabled"):
                out["channels"].append("oauth_" + k.replace("oauth_", ""))
        ap = us.get("attack_protection") or {}
        for k, v in ap.items():
            if isinstance(v, dict) and v.get("enabled"):
                out["captcha"].append(k)
        r = us.get("restrictions") or {}
        out["email_policy"] = {
            "block_disposable": bool(r.get("block_disposable_email_domains")),
            "block_subaddress": bool(r.get("block_email_subaddresses")),
            "mode": _pick(us.get("sign_up") or {}, ("mode", "restrictions")),
        }
        out["ok"] = True
        return out

    # 3) HTML 兜底
    html, hdrs, err3 = _get_html("https://%s/" % d, timeout=timeout)
    if not html:
        out["error"] = "三个端点都没读到：%s / %s / %s" % (err or "-", err2 or "-", err3 or "-")
        return out
    out["engine"] = detect_engine(html, hdrs)
    out["evidence"].append("首页 HTML %d 字节，engine=%s" % (len(html), out["engine"]))
    low = html.lower()
    for name, mark in (("turnstile", "cf-turnstile"), ("hcaptcha", "hcaptcha"),
                       ("recaptcha", "recaptcha"), ("geetest", "geetest")):
        if mark in low:
            out["captcha"].append(name)
    out["ok"] = True
    return out


# --------------------------------------------------------------------------
# 收码
# --------------------------------------------------------------------------

def wait_code(mb, timeout=DEFAULT_WAIT, pattern=r"\b(\d{6})\b", since=None, step=5):
    """等一封带码的邮件。返回 {"code","subject","waited","tried"}。

    不返回 None —— 超时也返回结构体，把「等了多久、收到几封、为什么没中」写清楚，
    否则上游只能看见一句「失败」。
    """
    t0 = time.time()
    seen = set()
    mails = 0
    last_err = ""
    while time.time() - t0 < timeout:
        msgs, err = mailbox_poll(mb)
        if err:
            last_err = err
        for m in msgs:
            mid = m.get("id")
            if mid in seen:
                continue
            seen.add(mid)
            mails += 1
            blob = (m.get("subject") or "") + " " + (m.get("text") or "")
            hit = re.findall(pattern, blob)
            if hit:
                return {"code": hit[0], "subject": m.get("subject", ""),
                        "waited": round(time.time() - t0, 1), "mails": mails,
                        "from": m.get("from", "")}
        time.sleep(step)
    return {"code": "", "subject": "", "waited": round(time.time() - t0, 1),
            "mails": mails, "error": last_err or "超时没收到匹配 %s 的邮件" % pattern}


# --------------------------------------------------------------------------
# 人工交接：有些段只有人能过，这时正确出口是「准确地址 + 准确动作」
# --------------------------------------------------------------------------

HANDOFF = {
    "openrouter.ai": {
        "why": "L4 —— Google 账号创建最后一步是手机扫码，纯脚本过不去；"
               "这一步本来就只有人能过，不是失败",
        "steps": [
            {"n": 1, "url": "https://accounts.google.com/signup",
             "do": "建一个 Google 账号。密码只用「字母+数字」，别用 ! # 这类符号 —— "
                   "自动化输入会把它们吞成别的字符，导致两次密码对不上。",
             "got": "一个能登录的 Google 账号"},
            {"n": 2, "url": "https://openrouter.ai/sign-up",
             "do": "点 Continue with Google，选刚建的账号，走完 OAuth。",
             "got": "登录态"},
            {"n": 3, "url": "https://openrouter.ai/settings/keys",
             "do": "Create Key，名字随便（比如 fleet），复制 sk-or-v1-... 开头那串。",
             "got": "API key"},
            {"n": 4, "url": "—",
             "do": "把 key 交给 fleet（或环境变量 OPENROUTER_API_KEY）。",
             "got": "接进 llmgw 的一路货源"},
        ],
        "note": "免费额度：累计充值 <9 刀 = 50 次/天。想上 1000 次/天要充 9 刀。"
                "多开账号不涨额度（官方明说容量按全局治理）。",
    },
    "go.toter.me": {
        "why": "只收 @qq.com；要一个能收信的 QQ 邮箱",
        "steps": [
            {"n": 1, "url": "https://go.toter.me/register",
             "do": "用 @qq.com 邮箱收码注册", "got": "账号"},
            {"n": 2, "url": "https://go.toter.me/dashboard",
             "do": "每天点签到，拿 dailyQuota", "got": "每日额度"},
        ],
    },
    "livelove.dev": {
        "why": "只收 @qq.com；而且它自己的 CSP 把 Turnstile 弄坏了，真人注册也会失败",
        "steps": [
            {"n": 1, "url": "https://livelove.dev/register",
             "do": "用 @qq.com 注册。如果卡在人机验证且页面空白 —— "
                   "那是它自己的 bug，不是你操作错",
             "got": "账号（可能过不去）"},
        ],
    },
}


def handoff(domain):
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0]
    h = HANDOFF.get(d)
    if h:
        return dict(h, domain=d, known=True)
    prof = PROFILES.get(d) or {}
    return {"domain": d, "known": False,
            "why": "这个站还没实测过，先跑 signup.recon 拿到规则再决定",
            "steps": [{"n": 1, "url": prof.get("register_url") or "https://%s/" % d,
                       "do": "打开注册页，看它要邮箱还是社交登录", "got": "规则"}],
            }


# --------------------------------------------------------------------------
# 自测（离线，不碰网络）
# --------------------------------------------------------------------------

def _selftest():
    fails = []

    def ck(cond, msg):
        if not cond:
            fails.append(msg)

    # 一次性域名判定：三级域名必须报可疑（我们在这上面栽过一次）
    ck(looks_disposable("cmb.arcadehearth.com")[0], "三级域名应判可疑")
    ck(looks_disposable("fldubai.com")[0], "fldubai.com 应判可疑")
    ck(not looks_disposable("qq.com")[0], "qq.com 不该判可疑")
    ck(not looks_disposable("gmail.com")[0], "gmail.com 不该判可疑")
    ck(looks_disposable("")[0], "空域名应判可疑")

    # 面板识别
    ck(detect_engine(body={"registration_email_suffix_whitelist": ["@qq.com"]}) == "sub2api",
       "带 suffix_whitelist 的 public settings 应判 sub2api")
    ck(detect_engine(body={"auth_config": {}}) == "clerk", "带 auth_config 应判 clerk")
    ck(detect_engine('<script src="/_next/static/x.js">') == "nextjs", "_next 应判 nextjs")
    ck(detect_engine("nothing here") == "unknown", "无证据必须报 unknown，不许猜")

    # 画像完整性
    for name, p in PROFILES.items():
        ck(p.get("register_url"), "%s 缺 register_url" % name)
        ck(p.get("checked"), "%s 缺 checked 日期（过期事实要有日期）" % name)
    ck("openrouter.ai" in HANDOFF, "openrouter 必须有交接单")
    ck(HANDOFF["openrouter.ai"]["steps"][-1]["url"], "交接单每步都要有地址或 —")

    # 域名清洗
    ck(handoff("https://openrouter.ai/sign-up")["domain"] == "openrouter.ai",
       "handoff 应能吃掉协议和路径")

    if fails:
        raise AssertionError("；".join(fails))
    return True


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    _selftest()
    print("signup_kit 自测通过（%d 个站点画像 / %d 份交接单）"
          % (len(PROFILES), len(HANDOFF)))

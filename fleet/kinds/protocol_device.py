# -*- coding: utf-8 -*-
"""天网 · 协议设备：承载 protocol.* 能力（伪装登录第三级·协议层）。

三级分工见 `fleet/vendored_protocol.py` 的模块文档。这里只说实现上的三条硬约定：

  1. **不外抛异常，失败带原因**。recon 打不动就说打不动（还差在哪一层），
     不把「我不会」写成「它不行」——这是 11-01 agentrouter 那次误判换来的规矩。
  2. **不猜语义**。表单字段靠 name/id/type/placeholder 认，认不出来就如实列全部字段
     让人指定；猜错了比不猜更贵（会在目标站点留下垃圾账号）。
  3. **不污染全局**。`net.opener()` 返回的直连 opener 是模块级共享单例，
     往上 add_handler 会让全站带上 cookie。所以这里自己 build，只挂自己的 CookieJar。

会话落盘位置：`~/.fleet/sessions/<name>.json`（cookie + 身份 + 时间戳）。
不存明文密码——身份包里的密码只在注册当次用，落盘时不写。
"""
import http.cookiejar
import json
import os
import re
import secrets
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from ..core import BaseDevice, DeviceResult as R
from ..vendored_protocol import PROTOCOL_CAPABILITIES

try:
    from .. import net
except Exception:  # pragma: no cover
    net = None
try:
    from .. import email_pool as EP
except Exception:  # pragma: no cover
    EP = None

_CAPS = tuple(c.name for c in PROTOCOL_CAPABILITIES)
_HANDLERS = {}

SESS_DIR = os.path.join(os.path.expanduser("~"), ".fleet", "sessions")


def _reg(name):
    def deco(fn):
        _HANDLERS[name] = fn
        return fn
    return deco


# ---------------------------------------------------------------- 会话与请求

def _mk(proxy=None):
    """造一个带 CookieJar 的 opener。自己 build，不动 net 的共享单例。"""
    if net is None:
        raise RuntimeError("出向层 fleet.net 不可用")
    base = net.opener(proxy)
    proxies = {}
    for h in base.handlers:
        if isinstance(h, urllib.request.ProxyHandler):
            proxies = dict(getattr(h, "proxies", None) or {})
            break
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies),
        urllib.request.HTTPCookieProcessor(cj),
    )
    return op, cj


def _fetch(op, url, timeout=15.0, method=None, data=None, headers=None):
    """打一发。HTTPError 不算异常——403/503 是反爬判定的关键证据，必须拿到。

    返回 (status, headers_dict, body_bytes, final_url, err)。
    status=0 才是真失败（连不上/DNS/超时）。
    """
    hdrs = {"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with op.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(), r.geturl(), None
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        return e.code, dict(e.headers or {}), body, url, None
    except Exception as e:
        return 0, {}, b"", url, "%s: %s" % (type(e).__name__, str(e)[:200])


def _text(b, limit=400000):
    try:
        return b.decode("utf-8", "replace")[:limit]
    except Exception:
        return ""


def _dump_cj(cj):
    return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
             "secure": bool(c.secure), "expires": c.expires} for c in cj]


def _load_cj(cj, data):
    n = 0
    for d in data or []:
        try:
            cj.set_cookie(http.cookiejar.Cookie(
                version=0, name=d["name"], value=d["value"], port=None, port_specified=False,
                domain=d.get("domain") or "", domain_specified=bool(d.get("domain")),
                domain_initial_dot=False, path=d.get("path") or "/", path_specified=True,
                secure=bool(d.get("secure")), expires=d.get("expires"),
                discard=False, comment=None, comment_url=None, rest={}, rfc2109=False))
            n += 1
        except Exception:
            pass
    return n


def _sess_path(name):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name or "default")[:64] or "default"
    return os.path.join(SESS_DIR, safe + ".json")


# ---------------------------------------------------------------- 表单解析

class _FormParser(HTMLParser):
    """抽表单。只认结构，不猜语义——猜的事留给 `_guess_fields`，并且要报「怎么猜的」。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self._cur = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "form":
            self._cur = {"action": a.get("action", ""), "method": (a.get("method") or "get").lower(),
                         "id": a.get("id", ""), "fields": []}
            self.forms.append(self._cur)
            return
        if tag in ("input", "textarea"):
            if self._cur is None:  # 页面里 <form> 之外的裸 input，收进一个匿名表单
                self._cur = {"action": "", "method": "post", "id": "", "fields": []}
                self.forms.append(self._cur)
            self._cur["fields"].append({
                "name": a.get("name", ""),
                "type": (a.get("type") or ("textarea" if tag == "textarea" else "text")).lower(),
                "value": a.get("value", ""),
                "id": a.get("id", ""),
                "placeholder": a.get("placeholder", ""),
                "required": "required" in a,
            })

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None


def _parse_forms(html, base_url):
    p = _FormParser()
    try:
        p.feed(html or "")
    except Exception:
        pass
    out = []
    for f in p.forms:
        action = urllib.parse.urljoin(base_url, f["action"] or base_url)
        out.append({"action": action, "method": f["method"], "id": f["id"],
                    "fields": [x for x in f["fields"] if x["name"]]})
    return out


_CODE_RE = re.compile(r"(?<![0-9])(\d{4,8})(?![0-9])")


def _find_email_field(fields):
    for f in fields:
        n = (f["name"] + " " + f["id"] + " " + f["placeholder"]).lower()
        if f["type"] == "email" or "mail" in n:
            return f["name"]
    return None


def _find_pass_field(fields):
    for f in fields:
        if f["type"] == "password":
            return f["name"]
    for f in fields:
        n = (f["name"] + " " + f["id"]).lower()
        if "pass" in n or "pwd" in n:
            return f["name"]
    return None


def _find_code_field(fields):
    for f in fields:
        n = (f["name"] + " " + f["id"] + " " + f["placeholder"]).lower()
        if any(k in n for k in ("code", "captcha", "verify", "验证", "校验")):
            return f["name"]
    return None


# ---------------------------------------------------------------- 反爬判定

# 只认「专用特征」，不认正文关键词——11-02 HF 那次页面 JS 里全是 verify，一判一个假通过。
#
# 两档标记必须分开，别混：
#   硬标记 = 出现即挑战页（cf_chl_opt / challenge-platform / jschl / 滑块组件的初始化）
#   软标记 = 只能说明「用了这家 CDN/挂了脚本」（cf-ray / __cf_bm / recaptcha 引用）
# 混在一起的后果实测过：example.com 挂在 Cloudflare 后面，响应头有 cf-ray，
# 第一版据此判「hard 挑战」，还说「纯协议过不了」——纯属胡说，那页 200 正常返回。
_HARD_MARKS = (
    ("cloudflare_challenge", ("cf_chl_opt", "cf-chl", "/cdn-cgi/challenge-platform")),
    ("js_challenge", ("jschl", "challenge-platform", "checking your browser", "验证您的浏览器")),
    ("aliyun_punish", ("acw_sc__v2", "punish?")),
    ("geetest", ("initgeetest", "gt.js")),
    ("tencent_captcha", ("tcaptcha", "tc_captcha")),
    ("recaptcha", ("g-recaptcha", "recaptcha/api.js")),
    ("hcaptcha", ("hcaptcha.com/1/api.js",)),
)
_SOFT_MARKS = (
    ("cloudflare_cdn", ("cf-ray", "__cf_bm")),
    ("cloudflare_other", ("cloudflare",)),
)


def _antibot(status, headers, body):
    """返回 {"level": none|soft|hard, "signals": [...]}。

    hard = 拿到的确实是挑战页（要跑 JS / 拖滑块才能过）
    soft = 有防护痕迹但页面正常返回（能直接读，只是可能随时被拦）
    none = 干净

    判据刻意保守：**宁可判 soft 也不要误判 hard**。误判 hard 会让人白白去起浏览器，
    而软标记遍地都是（半个互联网在 Cloudflare 后面）。
    """
    blob = (json.dumps(headers, ensure_ascii=False).lower() + " "
            + (body or "").lower()[:200000])
    hard = [n for n, ms in _HARD_MARKS if any(m in blob for m in ms)]
    soft = [n for n, ms in _SOFT_MARKS if any(m in blob for m in ms)]
    blocked = status in (403, 429, 503)
    if hard and (blocked or any(k in blob for k in ("challenge", "jschl"))):
        return {"level": "hard", "signals": hard + soft}
    if blocked and (hard or soft):
        return {"level": "hard", "signals": hard + soft}
    if hard or soft:
        return {"level": "soft", "signals": hard + soft}
    return {"level": "none", "signals": []}


_API_RE = re.compile(r"""["'](/[A-Za-z0-9_\-./]{2,80}?(?:api|login|signin|signup|register|auth)[A-Za-z0-9_\-./]{0,40})["']""")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


# ---------------------------------------------------------------- 指纹

_FP = {
    "desktop_win": {
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "platform": "Windows", "tz": "Asia/Shanghai", "screen": [1920, 1080],
        "sec_ch_ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    },
    "desktop_mac": {
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "platform": "macOS", "tz": "Asia/Shanghai", "screen": [2560, 1440],
        "sec_ch_ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    },
    "mobile_android": {
        "ua": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"),
        "platform": "Android", "tz": "Asia/Shanghai", "screen": [1080, 2400],
        "sec_ch_ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    },
}


def _gen_password():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10)) + "!Aa9"


def _make_identity(provider="maildrop", locale="zh-CN", preset="desktop_win"):
    fp = dict(_FP.get(preset) or _FP["desktop_win"])
    fp["locale"] = locale
    pwd = _gen_password()
    ident = {"preset": preset, "fingerprint": fp, "password": pwd,
             "mailbox": None, "ok": False, "err": None}
    if EP is None:
        ident["err"] = "邮箱池模块不可用"
        return ident
    try:
        mb = EP.new_mailbox(provider)
    except Exception as e:
        ident["err"] = "生成邮箱异常: %s" % e
        return ident
    if not mb.get("ok"):
        ident["err"] = mb.get("err") or "生成邮箱失败"
        return ident
    ident["mailbox"] = mb
    ident["ok"] = True
    return ident


def _headers_from_fp(fp):
    # locale 只在 _make_identity 里补上；注入指纹时可能没有，所以用 get 兜底。
    # 直接 fp["locale"] 会在「注入身份」这条路径上抛 KeyError —— 门禁第一次就撞到了。
    locale = fp.get("locale") or "zh-CN"
    return {
        "User-Agent": fp["ua"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "%s,zh;q=0.9,en;q=0.8" % locale,
        "sec-ch-ua": fp["sec_ch_ua"],
        "sec-ch-ua-platform": '"%s"' % fp["platform"],
    }


# ---------------------------------------------------------------- 能力实现

@_reg("re.protocol.recon")
def _h_recon(cap, args, dev):
    """给一个 URL，回答「怎么进」。只读，不提交任何东西。"""
    url = (args.get("url") or "").strip()
    if not url:
        return R.error(cap, "url 必填", device=dev.name, via=dev.via)
    if "://" not in url:
        url = "https://" + url
    timeout = float(args.get("timeout") or 15)
    op, _cj = _mk(args.get("proxy"))
    st, hd, body, final, err = _fetch(op, url, timeout=timeout)
    if st == 0:
        return R.unavailable(cap, "目标打不通：%s" % err, device=dev.name, via=dev.via)

    html = _text(body)
    forms = _parse_forms(html, final)
    wall = _antibot(st, hd, html)
    title = (_TITLE_RE.search(html).group(1).strip() if _TITLE_RE.search(html) else "")
    apis = sorted(set(_API_RE.findall(html)))[:15]

    # ---- 判「怎么进」：按证据说话，每条结论都带依据 ----
    has_pw = any(_find_pass_field(f["fields"]) for f in forms)
    has_mail = any(_find_email_field(f["fields"]) for f in forms)
    regish = any(any(k in (fld["name"] + fld["id"] + fld["placeholder"]).lower()
                     for k in ("confirm", "repass", "repassword", "repeat"))
                 for f in forms for fld in f["fields"])
    plan, why = [], []
    if wall["level"] == "hard":
        plan.append("needs_browser")
        why.append("检测到 %s 挑战（%s），纯协议过不了 —— 切 register.* 有头浏览器"
                   % (",".join(wall["signals"]), "状态 %s" % st))
    if has_pw and has_mail:
        plan.append("register" if regish else "login_or_register")
        why.append("表单里有 %s + 密码框" % ("邮箱框和确认密码" if regish else "邮箱框"))
    elif has_pw:
        plan.append("login")
        why.append("表单里有密码框但没邮箱框（可能用手机号/用户名）")
    elif forms:
        why.append("有表单但没密码字段（搜索/订阅类？）")
    else:
        plan.append("anonymous")
        why.append("页面上没有登录/注册表单 —— 大概率有免登录通道，直接抓内容")
    if apis:
        why.append("页面内露出 %d 个疑似接口，可优先试这些" % len(apis))

    return R.success(cap, dev.name, via=dev.via, data={
        "url": final, "status": st, "title": title[:120],
        "antibot": wall, "forms": forms, "api_hints": apis,
        "plan": plan, "why": why,
        "next": ("用 register.*（浏览器，过挑战）" if "needs_browser" in plan
                 else "用 protocol.register（协议注册）" if "register" in plan
                 else "用 protocol.login（协议登录）" if "login" in plan
                 else "用 register 或 login 试一次（注册/登录二选一）"
                 if "login_or_register" in plan
                 else "直接抓内容，不需要身份"),
    })


@_reg("re.protocol.identity")
def _h_identity(cap, args, dev):
    ident = _make_identity(provider=args.get("provider") or "maildrop",
                           locale=args.get("locale") or "zh-CN",
                           preset=args.get("preset") or "desktop_win")
    if not ident["ok"]:
        return R.unavailable(cap, "造身份失败：%s" % ident["err"],
                             device=dev.name, via=dev.via)
    return R.success(cap, dev.name, via=dev.via, data={
        "email": ident["mailbox"].get("email"),
        "mailbox": ident["mailbox"],
        "fingerprint": ident["fingerprint"],
        "password": ident["password"],
        "note": "身份仅本次使用；落盘不留明文密码",
    })


@_reg("re.protocol.form")
def _h_form(cap, args, dev):
    url = (args.get("url") or "").strip()
    if not url:
        return R.error(cap, "url 必填", device=dev.name, via=dev.via)
    if "://" not in url:
        url = "https://" + url
    op, _cj = _mk(args.get("proxy"))
    st, _hd, body, final, err = _fetch(op, url, timeout=float(args.get("timeout") or 15))
    if st == 0:
        return R.unavailable(cap, "目标打不通：%s" % err, device=dev.name, via=dev.via)
    forms = _parse_forms(_text(body), final)
    guess = [{"email_field": _find_email_field(f["fields"]),
              "password_field": _find_pass_field(f["fields"]),
              "code_field": _find_code_field(f["fields"])} for f in forms]
    return R.success(cap, dev.name, via=dev.via,
                     data={"url": final, "status": st, "forms": forms, "guess": guess})


@_reg("re.protocol.register")
def _h_register(cap, args, dev):
    """协议注册全流程。每一步都记进 steps，失败停在原地并说清停在哪。"""
    url = (args.get("url") or "").strip()
    if not url:
        return R.error(cap, "url 必填", device=dev.name, via=dev.via)
    if "://" not in url:
        url = "https://" + url
    steps = []
    op, cj = _mk(args.get("proxy"))

    # ① 造身份。identity 可注入 —— 门禁与批量场景用现成身份，
    #    不必每次都去临时邮箱服务取号（否则门禁会随外部服务红）。
    ident_in = args.get("identity") or {}
    if ident_in.get("email"):
        email = ident_in["email"]
        pwd = ident_in.get("password") or _gen_password()
        fp = ident_in.get("fingerprint") or _FP["desktop_win"]
        ident = {"ok": True, "mailbox": ident_in.get("mailbox") or {"email": email}}
    else:
        ident = _make_identity(provider=args.get("provider") or "maildrop")
        if not ident["ok"]:
            return R.unavailable(cap, "造身份失败：%s" % ident["err"],
                                 device=dev.name, via=dev.via)
        email = ident["mailbox"]["email"]
        pwd = ident["password"]
        fp = ident["fingerprint"]
    hdrs = _headers_from_fp(fp)
    steps.append({"step": "identity", "ok": True, "email": email})

    # ② 取表单
    st, _hd, body, final, err = _fetch(op, url, timeout=20, headers=hdrs)
    if st == 0:
        return R.unavailable(cap, "注册页打不通：%s" % err, device=dev.name, via=dev.via)
    forms = _parse_forms(_text(body), final)
    if not forms:
        return R.unavailable(cap, "页面上没找到表单（可能需要 JS 渲染 → 走 register.*）",
                             device=dev.name, via=dev.via, data={"steps": steps})
    form = max(forms, key=lambda f: len(f["fields"]))
    em_f = args.get("email_field") or _find_email_field(form["fields"])
    pw_f = args.get("password_field") or _find_pass_field(form["fields"])
    if not em_f or not pw_f:
        return R.unavailable(
            cap, "认不出邮箱/密码字段（不猜，避免在目标站留垃圾账号）",
            device=dev.name, via=dev.via,
            data={"steps": steps, "fields": [f["name"] for f in form["fields"]]})
    steps.append({"step": "form", "ok": True, "action": form["action"],
                  "email_field": em_f, "password_field": pw_f,
                  "field_count": len(form["fields"])})

    # ③ 填表并提交（隐藏字段原样带上，很多站的 csrf/nonce 在隐藏域里）
    payload = {f["name"]: f["value"] for f in form["fields"] if f["type"] == "hidden"}
    payload[em_f] = email
    payload[pw_f] = pwd
    for f in form["fields"]:  # 确认密码框跟着填
        n = (f["name"] + f["id"]).lower()
        if f["name"] not in payload and any(k in n for k in ("confirm", "repass", "repeat")):
            payload[f["name"]] = pwd
    submit_url = args.get("submit_url") or form["action"] or final
    data = urllib.parse.urlencode(payload).encode()
    st2, _h2, b2, _f2, err2 = _fetch(op, submit_url, timeout=20, method="POST", data=data,
                                     headers=dict(hdrs, **{"Content-Type": "application/x-www-form-urlencoded"}))
    if st2 == 0:
        return R.unavailable(cap, "提交失败：%s" % err2, device=dev.name, via=dev.via,
                             data={"steps": steps})
    t2 = _text(b2)
    steps.append({"step": "submit", "ok": True, "status": st2,
                  "antibot": _antibot(st2, _h2, t2), "snippet": t2[:160]})

    # ④ 等验证码（code 可注入，门禁不依赖外部邮箱服务）
    code = args.get("code")
    if code is None and EP is not None:
        try:
            rc = EP.wait_code(ident["mailbox"], timeout=float(args.get("code_timeout") or 120))
            code = rc.get("code") if isinstance(rc, dict) else rc
        except Exception as e:
            steps.append({"step": "wait_code", "ok": False, "err": str(e)[:120]})
    steps.append({"step": "wait_code", "ok": bool(code), "code": code})

    # ⑤ 回填验证码
    if code:
        cf = args.get("code_field") or _find_code_field(form["fields"]) or "code"
        vurl = args.get("verify_url") or submit_url
        vdata = urllib.parse.urlencode({cf: code, em_f: email}).encode()
        st3, _h3, b3, _f3, err3 = _fetch(op, vurl, timeout=20, method="POST", data=vdata,
                                         headers=dict(hdrs, **{"Content-Type": "application/x-www-form-urlencoded"}))
        steps.append({"step": "verify", "ok": st3 not in (0,), "status": st3,
                      "snippet": _text(b3)[:160], "err": err3})

    cookies = _dump_cj(cj)
    ok = bool(cookies) or st2 in (200, 201, 302)
    return R.success(cap, dev.name, via=dev.via, data={
        "ok": ok, "email": email, "password": pwd, "steps": steps,
        "cookies": cookies, "cookie_names": [c["name"] for c in cookies],
        "note": "会话已拿到；用 protocol.session save 可落盘复用",
    }) if ok else R.error(cap, "注册链路没拿到会话（看 steps 里停在哪一步）",
                          device=dev.name, via=dev.via)


def _resp_preview(st, body):
    return {"status": st, "snippet": _text(body)[:200] if body else ""}


@_reg("re.protocol.login")
def _h_login(cap, args, dev):
    url = (args.get("url") or "").strip()
    if not url:
        return R.error(cap, "url 必填", device=dev.name, via=dev.via)
    if "://" not in url:
        url = "https://" + url
    uf = args.get("user_field") or "username"
    pf = args.get("pass_field") or "password"
    payload = dict(args.get("extra") or {})
    payload[uf] = args.get("username") or ""
    payload[pf] = args.get("password") or ""
    op, cj = _mk(args.get("proxy"))
    if args.get("json_body"):
        data = json.dumps(payload).encode()
        ctype = "application/json"
    else:
        data = urllib.parse.urlencode(payload).encode()
        ctype = "application/x-www-form-urlencoded"
    st, hd, body, _f, err = _fetch(op, url, timeout=20, method="POST", data=data,
                                   headers={"Content-Type": ctype})
    if st == 0:
        return R.unavailable(cap, "登录请求失败：%s" % err, device=dev.name, via=dev.via)
    cookies = _dump_cj(cj)
    return R.success(cap, dev.name, via=dev.via, data={
        "response": _resp_preview(st, body), "cookies": cookies,
        "cookie_names": [c["name"] for c in cookies],
        "antibot": _antibot(st, hd, _text(body)[:60000]),
        "verdict": "拿到会话" if cookies else "没拿到 cookie（可能要二次验证/token 在 body 里）",
    })


@_reg("re.protocol.session")
def _h_session(cap, args, dev):
    op_name = (args.get("op") or "").strip().lower()
    if op_name not in ("save", "load", "probe", "list"):
        return R.error(cap, "op 必须是 save / load / probe / list", device=dev.name, via=dev.via)
    name = args.get("name") or "default"
    path = _sess_path(name)

    if op_name == "list":
        if not os.path.isdir(SESS_DIR):
            return R.success(cap, dev.name, via=dev.via, data={"sessions": [], "dir": SESS_DIR})
        out = []
        for fn in sorted(os.listdir(SESS_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(SESS_DIR, fn), encoding="utf-8") as f:
                    d = json.load(f)
                out.append({"name": fn[:-5], "url": d.get("url"), "saved_at": d.get("saved_at"),
                            "cookie_count": len(d.get("cookies") or [])})
            except Exception:
                out.append({"name": fn[:-5], "err": "读不出来"})
        return R.success(cap, dev.name, via=dev.via, data={"sessions": out, "dir": SESS_DIR})

    if op_name == "load":
        if not os.path.isfile(path):
            return R.unavailable(cap, "会话不存在：%s" % path, device=dev.name, via=dev.via)
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        _op, cj = _mk(args.get("proxy"))
        n = _load_cj(cj, d.get("cookies"))
        return R.success(cap, dev.name, via=dev.via,
                         data={"name": name, "url": d.get("url"), "loaded": n,
                               "cookie_names": [c["name"] for c in (d.get("cookies") or [])]})

    if op_name == "save":
        cookies = args.get("cookies")
        if cookies is None:
            return R.error(cap, "save 需要 cookies（把 protocol.login/register 返回的 cookies 传进来）",
                           device=dev.name, via=dev.via)
        os.makedirs(SESS_DIR, exist_ok=True)
        payload = {"name": name, "url": args.get("url"), "saved_at": time.time(),
                   "cookies": cookies}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return R.success(cap, dev.name, via=dev.via,
                         data={"name": name, "path": path, "cookie_count": len(cookies)})

    # probe
    url = (args.get("url") or "").strip()
    if not url:
        return R.error(cap, "probe 需要 url", device=dev.name, via=dev.via)
    if not os.path.isfile(path):
        return R.unavailable(cap, "会话不存在：%s" % path, device=dev.name, via=dev.via)
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    op, cj = _mk(args.get("proxy"))
    _load_cj(cj, d.get("cookies"))
    st, _hd, body, final, err = _fetch(op, url, timeout=20)
    return R.success(cap, dev.name, via=dev.via, data={
        "alive": st not in (0, 401, 403), "status": st, "final_url": final,
        "snippet": _text(body)[:160], "err": err,
    })


# ---------------------------------------------------------------- 设备

class ProtocolDevice(BaseDevice):
    kind = "protocol"
    via = "internet"

    def __init__(self, name="protocol:bench", **kw):
        super().__init__(name, via="internet", **kw)
        self.meta.setdefault(
            "note", "协议能力（伪装登录第三级·协议层）；纯 HTTP 不开浏览器，"
                    "造身份→注册/登录→拿会话，是渗透+逆向+爬虫的汇合点")

    def probe(self):
        if net is None:
            return False, "出向层 fleet.net 不可用"
        return True, ""

    def caps(self):
        return _CAPS

    def invoke(self, cap, args=None, dev=None):
        # dev 由委派方（Reverse）传入，让结果统一署名 re:bench；不传就自用
        dev = dev or self
        args = args or {}
        if cap not in _CAPS:
            return R.unsupported(cap, "re 设备没有这个能力", device=dev.name, via=dev.via)
        h = _HANDLERS.get(cap)
        if h is None:
            return R.unavailable(cap, "能力已声明，handler 待接入",
                                 device=dev.name, via=dev.via)
        try:
            return h(cap, args, dev)
        except Exception as e:
            return R.error(cap, "handler 执行异常：%s" % e, device=dev.name, via=dev.via)

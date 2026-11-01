# -*- coding: utf-8 -*-
"""天网 · 邮箱池（借免费临时邮箱服务，批量生成邮箱收验证码）
============================================================
伪装登录第一级「邮箱层」。主人的洞察：临时邮箱服务的本质 = catch-all 域名 +
API 收信，所以能无限批量生成邮箱收验证码。咱们先「借」现成的免费服务跑通
「生成邮箱 → 收验证码 → 注册账号」链路（借天借地借万物，融于自己），之后
再考虑「临时变永久」——自建 catch-all 域名，让地址可长期复用。

主力源：guerrillamail（无需注册、无需 token 管理、直连 200、API 完整）
后备源：mail.tm（REST API，需带 Accept: application/json 头）

能力：
  · new_mailbox()   生成一个邮箱（返回地址 + 凭证）
  · read_mailbox()  读收件箱（收验证码邮件）
  · wait_code()     轮询等待验证码（自动注册的关键一步）
  · extract_code()  从邮件主题/正文提取验证码

授权边界：仅用于「本人账号 / 已授权流程」的自动化；不用于绕过第三方平台的风控
或批量注册违规账号。这是正常的注册登录自动化基建。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) netforce-emailpool/1.0"

# 临时邮箱域名被服务商标记为一次性邮箱的风险，是「临时」的根因；后续自建 catch-all
# 域名（自己的、非公共标记的）才是「永久」的正解。这里先借免费服务跑通机制。
GM_BASE = "https://api.guerrillamail.com/ajax.php"


# ---------------------------------------------------------------- 底层 HTTP

def _get(url: str, headers: dict | None = None, timeout: float = 20.0):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "ignore")


def _post(url: str, data: dict, headers: dict | None = None, timeout: float = 20.0):
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    req = urllib.request.Request(url, data=body, headers=headers or {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "ignore")


# ---------------------------------------------------------------- guerrillamail（主力）

def guerrillamail_new() -> dict:
    """生成一个 guerrillamail 邮箱。返回 {email, sid_token, ok}。"""
    try:
        st, body = _get(f"{GM_BASE}?f=get_email_address")
        d = json.loads(body)
        return {"ok": True, "provider": "guerrillamail",
                "email": d.get("email_addr"),
                "sid_token": d.get("sid_token"),
                "email_timestamp": d.get("email_timestamp")}
    except Exception as e:
        return {"ok": False, "provider": "guerrillamail", "err": str(e)}


def guerrillamail_read(sid_token: str, seq: int = 0) -> dict:
    """读收件箱。返回 {ok, messages:[{id,from,subject,excerpt,date}]}。"""
    try:
        st, body = _get(f"{GM_BASE}?f=check_email&seq={seq}&sid_token={sid_token}")
        d = json.loads(body)
        msgs = []
        for m in d.get("list") or []:
            msgs.append({
                "id": m.get("mail_id"),
                "from": m.get("mail_from"),
                "subject": m.get("mail_subject"),
                "excerpt": m.get("mail_excerpt"),
                "date": m.get("mail_date"),
                "read": m.get("mail_read"),
            })
        return {"ok": True, "messages": msgs, "count": len(msgs)}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def guerrillamail_fetch(sid_token: str, mail_id: str) -> dict:
    """读单封邮件正文。返回 {ok, body}。"""
    try:
        st, body = _get(f"{GM_BASE}?f=fetch_email&email_id={mail_id}&sid_token={sid_token}")
        d = json.loads(body)
        return {"ok": True, "body": d.get("mail_body") or ""}
    except Exception as e:
        return {"ok": False, "err": str(e)}


# ---------------------------------------------------------------- mail.tm（后备）

def mailtm_new() -> dict:
    """mail.tm 创建临时邮箱账号。返回 {email, token, ok}。"""
    try:
        st, body = _get("https://api.mail.tm/domains",
                        headers={"Accept": "application/json", "User-Agent": UA})
        doms = json.loads(body)
        dom = doms[0]["domain"] if isinstance(doms, list) and doms else None
        if not dom:
            return {"ok": False, "provider": "mail.tm", "err": "无可用域名"}
        addr = f"tb{int(time.time())}{int(time.time() % 1000)}@{dom}"
        pw = "Passw0rd123"
        st2, body2 = _post("https://api.mail.tm/accounts",
                           json.dumps({"address": addr, "password": pw}).encode(),
                           headers={"Content-Type": "application/json", "Accept": "application/json",
                                    "User-Agent": UA})
        acct = json.loads(body2)
        st3, body3 = _post("https://api.mail.tm/token",
                           json.dumps({"address": addr, "password": pw}).encode(),
                           headers={"Content-Type": "application/json", "Accept": "application/json",
                                    "User-Agent": UA})
        tok = json.loads(body3)
        return {"ok": True, "provider": "mail.tm", "email": addr,
                "password": pw, "token": tok.get("token")}
    except Exception as e:
        return {"ok": False, "provider": "mail.tm", "err": str(e)}


# ---------------------------------------------------------------- maildrop.cc（后备2 · GraphQL）

MD_GRAPHQL = "https://api.maildrop.cc/graphql"


def _md_graphql(query: str) -> dict:
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(MD_GRAPHQL, data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def maildrop_new() -> dict:
    """maildrop.cc 生成邮箱。公开收件箱模型：随机名足够随机，别人猜不到。"""
    import secrets
    name = "tb" + secrets.token_hex(6)
    return {"ok": True, "provider": "maildrop",
            "email": f"{name}@maildrop.cc", "mailbox": name}


def maildrop_read(mailbox: str, fetch_body: int = 3) -> dict:
    """读 maildrop 收件箱。fetch_body>0 时对最新 N 封抓正文（验证码多在正文）。"""
    try:
        q = f'query {{ inbox(mailbox: "{mailbox}") {{ id headerfrom subject date }} }}'
        d = _md_graphql(q)
        items = (d.get("data") or {}).get("inbox") or []
        msgs = []
        for i, m in enumerate(items):
            excerpt = ""
            if i < fetch_body:
                fq = (f'query {{ message(mailbox: "{mailbox}", id: "{m.get("id")}") '
                      f'{{ data html }} }}')
                fd = _md_graphql(fq)
                fm = (fd.get("data") or {}).get("message") or {}
                excerpt = (fm.get("data") or fm.get("html") or "")[:300]
            msgs.append({"id": m.get("id"), "from": m.get("headerfrom"),
                         "subject": m.get("subject"), "excerpt": excerpt,
                         "date": m.get("date")})
        return {"ok": True, "messages": msgs, "count": len(msgs)}
    except Exception as e:
        return {"ok": False, "err": str(e)}


# ---------------------------------------------------------------- 验证码提取

# 验证码常见形态：6 位数字（最常见）、4-8 位数字、数字+字母 OTP。
_CODE_RE = re.compile(r"\b\d{4,8}\b")
_CODE_KW = re.compile(
    r"(?:验证码|校验码|确认码|verification\s*code|verification\s*number|"
    r"\bcode\b|\botp\b|confirmation\s*code|security\s*code|one-?time\s*(?:code|password))",
    re.I)


def extract_code(*texts, length: int = 0) -> str | None:
    """从一段或多段文本里提取验证码。length>0 时只取该长度。

    优先级：① 关键词附近的数字 ② 纯数字段。返回 None 表示没找到。
    """
    for text in texts:
        if not text:
            continue
        # 关键词优先（更可能是验证码，而非日期/金额等干扰数字）
        for m in _CODE_KW.finditer(text):
            tail = text[m.end():m.end() + 60]
            for n in _CODE_RE.findall(tail):
                if length and len(n) != length:
                    continue
                return n
    # 兜底：任意 4-8 位纯数字段
    for text in texts:
        if not text:
            continue
        for n in _CODE_RE.findall(text):
            if length and len(n) != length:
                continue
            return n
    return None


# ---------------------------------------------------------------- 统一入口

def new_mailbox(provider: str = "guerrillamail") -> dict:
    if provider == "mailtm" or provider == "mail.tm":
        return mailtm_new()
    if provider == "maildrop":
        return maildrop_new()
    return guerrillamail_new()


def read_mailbox(mbox: dict) -> dict:
    """读邮箱收件箱。mbox 是 new_mailbox 的返回。"""
    if not mbox.get("ok"):
        return mbox
    if mbox.get("provider") == "guerrillamail":
        return guerrillamail_read(mbox["sid_token"])
    if mbox.get("provider") == "mail.tm":
        try:
            st, body = _get("https://api.mail.tm/messages",
                            headers={"Authorization": f"Bearer {mbox['token']}",
                                     "Accept": "application/json", "User-Agent": UA})
            return {"ok": True, "messages": json.loads(body)}
        except Exception as e:
            return {"ok": False, "err": str(e)}
    if mbox.get("provider") == "maildrop":
        return maildrop_read(mbox.get("mailbox"))
    return {"ok": False, "err": "未知 provider"}


def wait_code(mbox: dict, timeout: float = 90.0, length: int = 0,
              interval: float = 5.0) -> dict:
    """轮询等验证码（自动注册的关键一步）。返回 {ok, code, subject, from}。"""
    if not mbox.get("ok"):
        return {"ok": False, "err": mbox.get("err") or "邮箱不可用"}
    deadline = time.time() + timeout
    seen: set = set()
    while time.time() < deadline:
        r = read_mailbox(mbox)
        if not r.get("ok"):
            time.sleep(interval)
            continue
        for m in r.get("messages", []):
            if m.get("id") in seen:
                continue
            seen.add(m.get("id"))
            code = extract_code(m.get("subject"), m.get("excerpt"), length=length)
            if code:
                return {"ok": True, "code": code, "subject": m.get("subject"),
                        "from": m.get("from")}
        time.sleep(interval)
    return {"ok": False, "err": f"超时 {timeout}s 未收到验证码"}


# ---------------------------------------------------------------- 离线自测

def _selftest() -> int:
    ok = fail = 0

    def check(cond, name):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"    [失败] {name}")

    # 1. 验证码提取（离线）
    check(extract_code("你的验证码是 123456，5 分钟内有效") == "123456", "关键词+6位码")
    check(extract_code("Your verification code is 888888", length=6) == "888888", "英文关键词+6位码")
    check(extract_code("code: 4521", length=4) == "4521", "指定长度4位")
    check(extract_code("今天天气不错", length=6) is None, "无码返回 None")
    check(extract_code("金额 1234 元") is not None, "兜底纯数字段")

    # 2. guerrillamail 生成邮箱（联网）
    g = guerrillamail_new()
    check(g.get("ok") and g.get("email") and g.get("sid_token"),
          f"guerrillamail 生成邮箱（{g.get('email')}）")
    if g.get("ok"):
        r = guerrillamail_read(g["sid_token"])
        check(r.get("ok") and "messages" in r, f"guerrillamail 读收件箱（{r.get('count', 0)} 封）")

    print(f"[email_pool] 自测：{ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())

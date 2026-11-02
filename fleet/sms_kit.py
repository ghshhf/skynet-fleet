# -*- coding: utf-8 -*-
"""天网 · 接码平台适配层（L4 兜底：手机号那一关）
====================================================

为什么需要这一层（2026-11-02 实测得出的）
-----------------------------------------
Google 账号创建走到最后，拦的不是邮箱、不是密码、不是 IP，是**手机**。
而且它现在给的还不是短信，是**手机扫码**（要求手机上已有一个登录着的 Google 账号）。
扫不出来的时候才会退到短信。短信这一关，接码平台能过；扫码那一关，只有人能过。

所以这一层的定位很明确：**补短信，不吹扫码。**

三条边界（写死）
----------------
1. **不替你花钱。** 默认只做询价（`sms.price`）和余额查询（`sms.balance`）。
   真正下单（`sms.buy`）是 `side_effect=True` 的能力，必须过一次审批。
2. **没 key 报 no_key。** 不返回「应该能行」，不静默降级成假数据。
3. **号码归属地必须和出口 IP 一致。** 号码在美国、IP 在香港，平台一眼就看出来 ——
   这是最常见的「买了号还是不过」的原因，不是号的问题。

平台差异（2026-11 实测行情，价格会变，只当量级看）
--------------------------------------------------
    平台            Google/Gmail 单价      成功率     备注
    SMS-Activate    ~$0.08–0.15            ~97%      轮询式，20 分钟窗口，无 webhook
    5SIM            ~$0.008 起（最低）      ~73%      最便宜但东南亚号成功率差
    SMSBower        ~$0.004 起              —        「无码不收费」，自动退款
    共同点：一次性号码、无码自动退款、轮询间隔 3–5 秒、超时窗口 20 分钟
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "fleet-sms-kit/1.0"
TIMEOUT = 25

#: 服务代码是**平台自定义**的，不是通用标准。这里只放实测确认过的对照。
SERVICE_CODES = {
    "sms_activate": {"google": "go", "openai": "dr", "telegram": "tg",
                     "discord": "ds", "github": "gh"},
    "five_sim": {"google": "google", "openai": "openai", "telegram": "telegram",
                 "discord": "discord", "github": "github"},
}

#: 平台元信息。价格区间只当量级，真价必须现查。
PLATFORMS = {
    "sms_activate": {
        "label": "SMS-Activate",
        "base": "https://api.sms-activate.org/stubs/handler_api.php",
        "key_env": "SMS_ACTIVATE_KEY",
        "balance": "getBalance",
        "buy": "getNumber",
        "status": "getStatus",
        "finish": "setStatus",
        "finish_value": "6",
        "cancel_value": "8",
        "price_note": "Google 约 $0.08–0.15/条；180+ 国家；成功率约 97%",
        "window_sec": 1200,
    },
    "five_sim": {
        "label": "5SIM",
        "base": "https://5sim.net/v1",
        "key_env": "FIVE_SIM_KEY",
        "price_note": "起步 $0.008；成功率约 73%（东南亚号更差，美欧号更好）",
        "window_sec": 1200,
    },
}

#: 号码国家要和**出口 IP 的国家**一致，否则平台侧一眼看出矛盾。
COUNTRY_HINT = {
    "HK": {"sms_activate": "14", "five_sim": "hongkong"},
    "US": {"sms_activate": "0", "five_sim": "usa"},
    "GB": {"sms_activate": "16", "five_sim": "england"},
    "ID": {"sms_activate": "6", "five_sim": "indonesia"},
    "SG": {"sms_activate": "—", "five_sim": "singapore"},
}


# --------------------------------------------------------------------------
# 底层
# --------------------------------------------------------------------------

def _key(platform):
    p = PLATFORMS.get(platform)
    if not p:
        return "", "不认识的接码平台：%s（可选 %s）" % (platform, "/".join(PLATFORMS))
    # 环境变量优先；其次 ~/.fleet/sms.json 里按平台名取
    k = os.environ.get(p["key_env"], "").strip()
    if k:
        return k, ""
    path = os.path.expanduser("~/.fleet/sms.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                blob = json.load(f)
            k = str((blob or {}).get(platform, {}).get("key") or "").strip()
            if k:
                return k, ""
        except Exception as e:
            return "", "读 %s 失败：%s" % (path, str(e)[:80])
    return "", ("no_key —— 没配 %s。设环境变量 %s，或写进 ~/.fleet/sms.json："
                "{\"%s\": {\"key\": \"...\"}}" % (p["key_env"], p["key_env"], platform))


def _http(url, data=None, headers=None, timeout=TIMEOUT, raw=False):
    body = None
    if data is not None and not raw:
        body = json.dumps(data).encode("utf-8")
    elif isinstance(data, bytes):
        body = data
    r = urllib.request.Request(url, data=body)
    r.add_header("User-Agent", UA)
    r.add_header("Accept", "application/json")
    if body is not None and not raw:
        r.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            txt = resp.read().decode("utf-8", "replace")
            try:
                return json.loads(txt), "", resp.status
            except ValueError:
                return {"_text": txt.strip()}, "", resp.status
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        return None, "HTTP %s %s" % (e.code, detail), e.code
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:140]), 0


def _sa(platform, action, **params):
    """SMS-Activate：GET + api_key + action 的老式接口，返回是一行文本。"""
    p = PLATFORMS[platform]
    key, err = _key(platform)
    if err:
        return None, err
    q = {"api_key": key, "action": action}
    q.update({k: v for k, v in params.items() if v not in (None, "")})
    url = p["base"] + "?" + urllib.parse.urlencode(q)
    j, err, _ = _http(url)
    if err:
        return None, err
    txt = (j or {}).get("_text", "")
    if not txt:
        return None, "平台返回空"
    if txt.startswith("ACCESS_"):
        return {"raw": txt, "parts": txt.split(":")}, ""
    if txt.startswith("STATUS_OK"):
        parts = txt.split(":")
        return {"status": "ok", "code": parts[1] if len(parts) > 1 else "", "raw": txt}, ""
    if txt.startswith("STATUS_WAIT"):
        return {"status": "wait", "raw": txt}, ""
    if txt in ("BAD_KEY", "BAD_ACTION", "NO_NUMBERS", "NO_BALANCE", "WRONG_MAX_PRICE"):
        return None, "平台拒绝：%s" % txt
    return {"raw": txt}, ""


def _5s(platform, path, method="GET", country="", operator="any", product=""):
    key, err = _key(platform)
    if err:
        return None, err
    url = PLATFORMS[platform]["base"] + path
    j, err, _ = _http(url, headers={"Authorization": "Bearer " + key})
    if err:
        return None, err
    return j, ""


# --------------------------------------------------------------------------
# 对外动作
# --------------------------------------------------------------------------

def balance(platform):
    if platform == "sms_activate":
        r, err = _sa(platform, PLATFORMS[platform]["balance"])
        if err:
            return None, err
        try:
            return {"platform": platform, "balance": float(r["raw"]),
                    "currency": "RUB"}, ""
        except Exception:
            return {"platform": platform, "raw": r.get("raw")}, ""
    if platform == "five_sim":
        j, err = _5s(platform, "/user/profile")
        if err:
            return None, err
        return {"platform": platform, "balance": (j or {}).get("balance"),
                "currency": "RUB", "raw": j}, ""
    return None, "不认识的接码平台：%s" % platform


def price(platform, service="google", country=""):
    """询价。**只读，不买。** 买号要另外过审批。"""
    if platform == "five_sim":
        c = country or "hongkong"
        j, err = _5s(platform, "/guest/prices?product=%s&country=%s" %
                     (urllib.parse.quote(service), urllib.parse.quote(c)))
        if err:
            return None, err
        return {"platform": platform, "service": service, "country": c, "raw": j}, ""
    if platform == "sms_activate":
        c = country or COUNTRY_HINT["HK"]["sms_activate"]
        r, err = _sa(platform, "getPrices", service=service, country=c)
        if err:
            return None, err
        return {"platform": platform, "service": service, "country": c, "raw": r}, ""
    return None, "不认识的接码平台：%s" % platform


def buy(platform, service="google", country=""):
    """下单买一个号 —— 这一步**花钱**。调用方必须先过审批。"""
    if platform == "sms_activate":
        c = country or COUNTRY_HINT["HK"]["sms_activate"]
        code = SERVICE_CODES["sms_activate"].get(service, service)
        r, err = _sa(platform, PLATFORMS[platform]["buy"], service=code, country=c)
        if err:
            return None, err
        parts = r.get("parts") or []
        if len(parts) >= 3:
            return {"platform": platform, "activation_id": parts[1],
                    "phone": parts[2], "service": service}, ""
        return None, "平台返回了看不懂的东西：%s" % r.get("raw", "")[:120]
    if platform == "five_sim":
        c = country or COUNTRY_HINT["HK"]["five_sim"]
        code = SERVICE_CODES["five_sim"].get(service, service)
        j, err = _5s(platform, "/user/buy/activation/%s/any/%s" % (c, code))
        if err:
            return None, err
        return {"platform": platform, "activation_id": (j or {}).get("id"),
                "phone": (j or {}).get("phone"), "service": service,
                "raw": j}, ""
    return None, "不认识的接码平台：%s" % platform


def wait_code(platform, activation_id, timeout=240, step=5, pattern=r"\b(\d{4,8})\b"):
    """轮询取码。返回结构和 signup_kit.wait_code 对齐，方便上面统一处理。"""
    import re
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout:
        if platform == "sms_activate":
            r, err = _sa(platform, PLATFORMS[platform]["status"], id=activation_id)
            if err:
                last = err
            elif r and r.get("status") == "ok":
                return {"code": r.get("code", ""), "waited": round(time.time() - t0, 1),
                        "raw": r.get("raw", "")}
            elif r:
                last = r.get("raw", "wait")
        elif platform == "five_sim":
            j, err = _5s(platform, "/user/check/" + str(activation_id))
            if err:
                last = err
            else:
                sms = (j or {}).get("sms") or []
                for m in sms:
                    hit = re.findall(pattern, str(m.get("text") or ""))
                    if hit:
                        return {"code": hit[0], "waited": round(time.time() - t0, 1),
                                "raw": str(m.get("text") or "")[:120]}
                last = (j or {}).get("status", "wait")
        else:
            return {"code": "", "error": "不认识的接码平台：%s" % platform}
        time.sleep(step)
    return {"code": "", "waited": round(time.time() - t0, 1),
            "error": "超时没收到码（最后状态 %s）" % last}


def finish(platform, activation_id):
    """告诉平台这一单成了（不调会导致号码一直被占，也可能不退失败的单）。"""
    if platform == "sms_activate":
        r, err = _sa(platform, PLATFORMS[platform]["finish"],
                     status=PLATFORMS[platform]["finish_value"], id=activation_id)
        return (not err), err or "ok"
    if platform == "five_sim":
        j, err = _5s(platform, "/user/finish/" + str(activation_id))
        return (not err), err or "ok"
    return False, "不认识的接码平台：%s" % platform


def cancel(platform, activation_id):
    if platform == "sms_activate":
        r, err = _sa(platform, PLATFORMS[platform]["finish"],
                     status=PLATFORMS[platform]["cancel_value"], id=activation_id)
        return (not err), err or "ok"
    if platform == "five_sim":
        j, err = _5s(platform, "/user/cancel/" + str(activation_id))
        return (not err), err or "ok"
    return False, "不认识的接码平台：%s" % platform


def country_for_ip(ip_country, platform):
    """把出口 IP 的国家翻译成平台的 country 参数。

    这条是**最容易被忽略、也最容易白花钱**的一步：号码和 IP 不同国，
    平台侧关联一下就露馅，钱花了号还是过不去。
    """
    c = (ip_country or "").strip().upper()
    m = COUNTRY_HINT.get(c) or {}
    v = m.get(platform)
    if not v:
        return "", ("出口在 %s，但这里没有 %s 的 country 映射 —— "
                    "别瞎猜一个，先去平台页面查" % (c or "未知", platform))
    return v, ""


# --------------------------------------------------------------------------
# 自测（离线）
# --------------------------------------------------------------------------

def _selftest():
    fails = []

    def ck(cond, msg):
        if not cond:
            fails.append(msg)

    ck(set(PLATFORMS) == {"sms_activate", "five_sim"}, "平台清单变了要同步门禁")
    for name, p in PLATFORMS.items():
        ck(p.get("key_env"), "%s 缺 key_env" % name)
        ck(p.get("price_note"), "%s 缺价格量级说明" % name)
    # 没 key 必须报 no_key，不许返回假数据
    old = {k: os.environ.pop(v["key_env"], None) for k, v in PLATFORMS.items()}
    try:
        for name in PLATFORMS:
            b, err = balance(name)
            ck(b is None and "no_key" in err, "%s 没 key 时应报 no_key，实际 %r" % (name, err[:40]))
    finally:
        for k, v in PLATFORMS.items():
            if old.get(k):
                os.environ[v["key_env"]] = old[k]
    # 国家映射必须覆盖我们实际用的出口
    for cc in ("HK", "US", "GB"):
        for name in PLATFORMS:
            v, err = country_for_ip(cc, name)
            ck(v and not err, "%s/%s 缺少国家映射" % (cc, name))
    ck(country_for_ip("ZZ", "five_sim")[0] == "", "未知国家必须返回空，不许兜底猜")

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
    print("sms_kit 自测通过（%d 个平台 / %d 组国家映射）"
          % (len(PLATFORMS), len(COUNTRY_HINT)))

"""沙箱：一人一份历史，一份历史一部机器。

为什么沙箱比伪装高一级（2026-11-02 主人定的判断）
--------------------------------------------------
伪装是「我假装是另一台机器」，会被抓矛盾；
沙箱是「**我真的重新出生了一次**」，矛盾天然不存在。

检测方看的历史，说到底是三样东西：
    ① cookie / localStorage / IndexedDB / cache    —— 这台机器经历过什么
    ② 出口 IP / ASN / 地理位置                     —— 这台机器从哪来
    ③ 指纹（UA/时区/语言/GPU/Canvas）              —— 这台机器长什么样

    ①②③ **必须互相指向同一个人**。
    profile 目录管 ①，egress 绑定管 ②，fp_profile 管 ③。
    三者错位一次，历史分就废了 —— 所以本模块的核心是 check()。

两种沙箱，别混着用
------------------
    ephemeral（一次性）  抓一次就走。用完即毁，没有历史可被打分。
                         代价：在需要登录态/需要历史稳定的站上，新访客分偏低。
    persistent（养号）   长期用一个。**出口 IP 必须绑定，不许漂** ——
                         IP 一漂，之前攒的历史全部作废（对方看到的是「同一台机器
                         突然换了国家」，比新访客危险得多）。

    ⇒ 这不是「哪个更高级」的问题，是两种任务用两种工具。

目录布局（`~/.fleet/sandbox/` 下，跟 browser/ cookies/ 同级）
------------------------------------------------------------
    <id>/meta.json      档案 + 出生时间 + 绑定出口 + 使用次数
    <id>/profile/       chromium 的 user-data-dir（直接喂给 launch(profile=...)）

    id 由 seed 派生 ⇒ 同 seed 同沙箱（幂等）；fresh=True 才另起一个。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time

from . import fp_profile as FP

FLEET_HOME = os.path.join(os.path.expanduser("~"), ".fleet")
SANDBOX_DIR = os.path.join(FLEET_HOME, "sandbox")
META = "meta.json"
_SID_RE = re.compile(r"^[a-z]{2}-[a-z]+-[0-9a-f]{6,}$")


# ---------------------------------------------------------------- 路径
def _ensure():
    os.makedirs(SANDBOX_DIR, exist_ok=True)


def sandbox_path(sid):
    """把一个 id 变成目录路径。**防穿越**：不合法就直接拒，不做「尽力修正」。"""
    if not _SID_RE.match(str(sid or "")):
        raise ValueError("非法沙箱 id %r（只允许 zh 这种形状：cn-win-a1b2c3）" % (sid,))
    p = os.path.join(SANDBOX_DIR, sid)
    # 双保险：正则过了也再核一次真实路径还在根下
    if os.path.commonpath([os.path.abspath(p), os.path.abspath(SANDBOX_DIR)]) != os.path.abspath(SANDBOX_DIR):
        raise ValueError("沙箱路径越界：%r" % p)
    return p


def profile_dir(sid):
    """给 browser_reg.launch(profile=...) / launch_persistent_context 用。"""
    return os.path.join(sandbox_path(sid), "profile")


def make_id(geo, os_, seed):
    h = hashlib.sha256(("%s|%s|%s" % (geo, os_, seed)).encode()).hexdigest()[:8]
    return "%s-%s-%s" % (str(geo).lower(), str(os_).lower(), h)


# ---------------------------------------------------------------- 增
def create(platform="win", geo="CN", seed=None, fresh=False, persist=False,
           egress=None, ttl_days=7, note=""):
    """取或建一个沙箱。同 seed 幂等；fresh=True 才另起一个。

    返回 {'ok', 'id', 'path', 'profile_dir', 'profile', 'created', 'reused', 'err'}
    """
    try:
        seed = seed if seed is not None else "%s-%s" % (geo, platform)
        if fresh:
            seed = "%s|%s" % (seed, time.time_ns())
        sid = make_id(geo, platform, seed)
        p = sandbox_path(sid)
        mp = os.path.join(p, META)
        if os.path.exists(mp):                      # 已有 → 复用，不重造（重造会丢历史）
            meta = json.load(open(mp, encoding="utf-8"))
            return {"ok": True, "id": sid, "path": p, "profile_dir": profile_dir(sid),
                    "profile": meta["profile"], "created": meta["created"],
                    "reused": True, "err": ""}
        prof = FP.make(platform=platform, geo=geo, seed=seed)
        bad = FP.contradictions(prof)
        if bad:                                     # 自洽不过的档案不许建沙箱
            return {"ok": False, "id": sid, "err": "档案不自洽：%s" % bad}
        _ensure()
        os.makedirs(profile_dir(sid), exist_ok=True)
        meta = {"id": sid, "seed": seed, "profile": prof,
                "created": time.time(), "last_used": None, "uses": 0,
                "persist": bool(persist), "ttl_days": int(ttl_days),
                "egress": egress, "note": note, "version": 1}
        _write_meta(sid, meta)
        return {"ok": True, "id": sid, "path": p, "profile_dir": profile_dir(sid),
                "profile": prof, "created": meta["created"], "reused": False, "err": ""}
    except Exception as e:
        return {"ok": False, "id": "", "err": "%s: %s" % (type(e).__name__, e)}


def _write_meta(sid, meta):
    mp = os.path.join(sandbox_path(sid), META)
    tmp = mp + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    os.replace(tmp, mp)                             # 原子写：半截文件比没文件更糟


# ---------------------------------------------------------------- 查
def get(sid):
    try:
        mp = os.path.join(sandbox_path(sid), META)
        if not os.path.exists(mp):
            return None
        return json.load(open(mp, encoding="utf-8"))
    except Exception:
        return None


def list_pool(include_expired=False, now=None):
    """列出池子。默认滤掉过期的 —— 但**不删**，删是 purge() 的事。"""
    now = now or time.time()
    out = []
    if not os.path.isdir(SANDBOX_DIR):
        return out
    for name in sorted(os.listdir(SANDBOX_DIR)):
        if not _SID_RE.match(name):
            continue
        m = get(name)
        if not m:
            continue
        m = dict(m)
        m["age_days"] = round((now - float(m.get("created") or now)) / 86400.0, 2)
        m["expired"] = (not m.get("persist")) and m["age_days"] > float(m.get("ttl_days") or 7)
        if m["expired"] and not include_expired:
            continue
        out.append(m)
    return out


def touch(sid, egress=None):
    """记一次使用。用一次就更新 —— 「上次活跃」是判断这台机器还在不在用的唯一依据。"""
    m = get(sid)
    if not m:
        return {"ok": False, "err": "沙箱不存在：%s" % sid}
    m["uses"] = int(m.get("uses") or 0) + 1
    m["last_used"] = time.time()
    if egress is not None:
        m["egress"] = egress
    _write_meta(sid, m)
    return {"ok": True, "id": sid, "uses": m["uses"]}


# ---------------------------------------------------------------- 体检（本模块的真正功能）
def check(sid, live_country=None, live_proxy=None, now=None, auto=False, timeout=8):
    """这份沙箱还能不能用。返回问题清单，空 = 能上。

    最要紧的一条是**出口漂移**：profile 里攒的历史是给「某个国家的人」攒的，
    IP 一换国家，这套历史就变成「同一台机器突然搬了家」——
    那比一个全新访客更可疑。所以这一条单列，不用合并进别的结论。

    auto=True 时自己走网络探一次出口（`fp_egress`）。
    **探不出来会单独报 `egress_unknown`** —— 不知道不等于没问题，
    静默放行就等于这条规则从来没生效过。
    """
    now = now or time.time()
    issues = []
    m = get(sid)
    if not m:
        return [{"rule": "no_sandbox", "why": "沙箱 %s 不存在" % sid}]

    # 0) 自动探出口。探不到就是「不知道」，必须显式报，不许当通过。
    probed = None
    if auto and not live_country:
        try:
            from . import fp_egress as EG
            probed = EG.probe(timeout=timeout)
        except Exception as e:
            probed = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        if probed.get("ok"):
            live_country = probed.get("country")
            if live_proxy is None:
                live_proxy = probed.get("proxy")
        else:
            issues.append({"rule": "egress_unknown", "why":
                           "探不出当前出口（%s）—— 不知道不等于没问题，"
                           "这条不许当通过" % str(probed.get("error"))[:140]})

    # 1) 档案本身还自洽吗（可能被人手改过 meta）
    for bad in FP.contradictions(m.get("profile")):
        issues.append({"rule": "profile_" + bad["rule"], "why": bad["why"]})

    # 2) 过期
    age = (now - float(m.get("created") or now)) / 86400.0
    if not m.get("persist") and age > float(m.get("ttl_days") or 7):
        issues.append({"rule": "expired", "why": "一次性沙箱已存在 %.1f 天，超过 ttl %.0f 天"
                                                % (age, float(m.get("ttl_days") or 7))})

    # 3) profile 目录还在吗（被删了就是空壳，历史已经没了）
    if not os.path.isdir(profile_dir(sid)):
        issues.append({"rule": "no_profile_dir", "why": "profile 目录不见了 —— 历史已经丢了"})

    # 4) **出口漂移**：这条最致命
    prof_geo = str((m.get("profile") or {}).get("geo") or "")
    bound = (m.get("egress") or {}).get("country") if isinstance(m.get("egress"), dict) else None
    if live_country:
        if bound and str(live_country).upper() != str(bound).upper():
            issues.append({"rule": "egress_drift", "why":
                           "绑定的出口是 %s，现在实际出口是 %s —— 历史是给前者的，"
                           "换了国家等于「同一台机器突然搬家」，比新访客更可疑"
                           % (bound, live_country)})
        elif not bound and prof_geo and str(live_country).upper() != prof_geo.upper():
            issues.append({"rule": "egress_mismatch", "why":
                           "档案宣称 %s，实际出口在 %s —— 时区/语言全对不上" % (prof_geo, live_country)})
    if live_proxy is not None and isinstance(m.get("egress"), dict):
        was = (m.get("egress") or {}).get("proxy")
        if was and live_proxy and was != live_proxy:
            issues.append({"rule": "proxy_changed", "why":
                           "出口代理从 %s 换成了 %s —— 同一个 profile 换线路也等于换人" % (was, live_proxy)})
    if probed and probed.get("ok"):
        # 探到了就把住宅/机房也报出来：机房 IP 本身不是错，但要知道代价
        try:
            from . import fp_egress as EG
            kind = EG.looks_residential(probed.get("asn"), probed.get("isp"))
        except Exception:
            kind = None
        if kind == "datacenter":
            issues.append({"rule": "egress_datacenter", "why":
                           "出口是机房 IP（%s / %s）—— 不是不能过，是先天分低。"
                           "无感打分看的是「像不像一个真人上网的线路」"
                           % (probed.get("asn"), probed.get("isp"))})
    return issues


def bind_egress(sid, proxy=None, country=None, ip=None, asn=None):
    """把出口钉在沙箱上。养号必须调这个，否则漂了都不知道。"""
    m = get(sid)
    if not m:
        return {"ok": False, "err": "沙箱不存在：%s" % sid}
    m["egress"] = {"proxy": proxy, "country": (country or "").upper() or None,
                   "ip": ip, "asn": asn, "checked_at": time.time()}
    _write_meta(sid, m)
    return {"ok": True, "id": sid, "egress": m["egress"]}


# ---------------------------------------------------------------- 删
def destroy(sid):
    """真删。只删 SANDBOX_DIR 下的子目录，路径不合法直接拒。"""
    try:
        p = sandbox_path(sid)
    except ValueError as e:
        return {"ok": False, "err": str(e)}
    if not os.path.isdir(p):
        return {"ok": False, "err": "沙箱不存在：%s" % sid}
    shutil.rmtree(p, ignore_errors=False)
    return {"ok": True, "id": sid, "freed": p}


def purge(now=None, dry_run=True):
    """清掉过期的一次性沙箱。**默认 dry_run** —— 删历史这件事不该是隐式的。"""
    now = now or time.time()
    gone = []
    for m in list_pool(include_expired=True, now=now):
        if not m.get("expired"):
            continue
        if dry_run:
            gone.append({"id": m["id"], "age_days": m["age_days"], "dry_run": True})
        else:
            r = destroy(m["id"])
            gone.append({"id": m["id"], "age_days": m["age_days"], "ok": r.get("ok"),
                         "err": r.get("err", "")})
    return {"removed": len(gone), "dry_run": dry_run, "items": gone}


def summary(now=None):
    """池子现状，一句话能说清的那种。"""
    now = now or time.time()
    allm = list_pool(include_expired=True, now=now)
    live = [m for m in allm if not m.get("expired")]
    return {
        "total": len(allm),
        "live": len(live),
        "expired": len(allm) - len(live),
        "persistent": len([m for m in allm if m.get("persist")]),
        "by_geo": _count(m.get("profile", {}).get("geo") for m in live),
        "by_os": _count(m.get("profile", {}).get("os") for m in live),
        "root": SANDBOX_DIR,
    }


def _count(it):
    out = {}
    for k in it:
        if k:
            out[k] = out.get(k, 0) + 1
    return out


# ---------------------------------------------------------------- 自测
def _selftest():
    import tempfile

    global SANDBOX_DIR
    okc = failc = 0

    def chk(cond, msg):
        nonlocal okc, failc
        if cond:
            okc += 1
        else:
            failc += 1
            print("  FAIL:", msg)

    real = SANDBOX_DIR
    tmp = tempfile.mkdtemp(prefix="fpsb-")
    SANDBOX_DIR = os.path.join(tmp, "sandbox")
    try:
        # --- 建 ---
        r = create(platform="win", geo="CN", seed="self")
        chk(r["ok"], "创建应成功：%s" % r.get("err"))
        sid = r["id"]
        chk(_SID_RE.match(sid), "id 形状应对：%r" % sid)
        chk(os.path.isdir(r["profile_dir"]), "profile 目录应被建出来")

        # 幂等：同 seed 再建一次，应该是复用而不是重造（重造会丢历史）
        r2 = create(platform="win", geo="CN", seed="self")
        chk(r2["ok"] and r2["reused"] is True, "同 seed 应复用，实得 reused=%r" % r2.get("reused"))
        chk(r2["id"] == sid, "同 seed 应同 id")

        # fresh=True 才另起
        r3 = create(platform="win", geo="CN", seed="self", fresh=True)
        chk(r3["ok"] and r3["id"] != sid, "fresh=True 应另起一个")

        # --- 路径防穿越 ---
        for bad in ("../etc", "cn-win", "CN-WIN-abc", "x/y", "", None):
            try:
                sandbox_path(bad)
                chk(False, "非法 id %r 必须被拒" % (bad,))
            except ValueError:
                okc += 1

        # --- 体检：干净 ---
        chk(check(sid) == [], "刚建的沙箱应无问题，实得 %s" % check(sid))

        # --- 体检：出口漂移（最致命的一条）---
        bind_egress(sid, proxy="http://127.0.0.1:7891", country="CN", ip="1.2.3.4")
        chk(check(sid, live_country="CN") == [], "出口与绑定一致时不该报问题")
        iss = check(sid, live_country="US")
        chk(any(i["rule"] == "egress_drift" for i in iss),
            "出口从 CN 漂到 US 必须报 egress_drift，实得 %s" % [i["rule"] for i in iss])
        chk(any("搬家" in i["why"] for i in iss), "要说清为什么搬家比新访客更危险")

        # --- 体检：换线路也算换人 ---
        iss = check(sid, live_proxy="http://127.0.0.1:9999")
        chk(any(i["rule"] == "proxy_changed" for i in iss), "换出口线路必须报 proxy_changed")

        # --- 体检：档案被手改坏 ---
        m = get(sid)
        m["profile"]["tz"] = "Asia/Tokyo"           # 中国档案配东京时区
        _write_meta(sid, m)
        iss = check(sid)
        chk(any(i["rule"] == "profile_tz_not_in_geo" for i in iss),
            "meta 被改坏也要体检出来，实得 %s" % [i["rule"] for i in iss])

        # --- 体检：profile 目录被删 ---
        m["profile"]["tz"] = "Asia/Shanghai"
        _write_meta(sid, m)
        shutil.rmtree(profile_dir(sid))
        iss = check(sid)
        chk(any(i["rule"] == "no_profile_dir" for i in iss), "profile 目录没了要报")

        # --- 池子 ---
        s = summary()
        chk(s["total"] >= 2, "池子里应有至少 2 个（实得 %r）" % s["total"])
        chk("CN" in s["by_geo"], "by_geo 应统计到 CN")

        # --- 过期与清理 ---
        old = create(platform="mac", geo="US", seed="old", ttl_days=1)["id"]
        m = get(old)
        m["created"] = time.time() - 86400 * 3        # 假装三天前建的
        _write_meta(old, m)
        chk(any(x["id"] == old for x in list_pool(include_expired=True)), "过期项在 include_expired 里应出现")
        chk(not any(x["id"] == old for x in list_pool()), "过期项默认不该出现在列表里")
        p1 = purge(dry_run=True)
        chk(p1["removed"] >= 1 and p1["dry_run"] is True, "dry_run 应只报不删")
        chk(os.path.isdir(sandbox_path(old)), "dry_run 不许真删（目录还在）")
        p2 = purge(dry_run=False)
        chk(p2["removed"] >= 1, "真清理应删掉过期项")
        chk(not os.path.isdir(sandbox_path(old)), "真清理后目录应消失")

        # --- 养号不参与过期清理 ---
        keep = create(platform="win", geo="JP", seed="keep", persist=True, ttl_days=1)["id"]
        m = get(keep)
        m["created"] = time.time() - 86400 * 30
        _write_meta(keep, m)
        purge(dry_run=False)
        chk(os.path.isdir(sandbox_path(keep)), "persist=True 的沙箱不该被 purge 清掉")

        # --- 删 ---
        d = destroy(keep)
        chk(d["ok"] and not os.path.isdir(sandbox_path(keep)), "destroy 应真删")
        chk(destroy(keep)["ok"] is False, "删不存在的应返回 ok=False 而不是抛异常")

        # --- 不许建自洽不过的档案 ---
        bad = create(platform="win", geo="XX")
        chk(bad["ok"] is False, "未知地理必须拒绝建沙箱，实得 %r" % bad)
    finally:
        SANDBOX_DIR = real
        shutil.rmtree(tmp, ignore_errors=True)

    print("fp_sandbox 自测：%d 过 / %d 败" % (okc, failc))
    return failc == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)

"""门禁 t70：注册流水线 —— 四段分开、失败不互相掩盖、不产生假成功。

为什么需要这条门禁
------------------
2026-11-02 那一轮，我用浏览器一次一次点注册，失败了三次，猜了三个原因
（域名不行？IP 不行？密码不行？），最后发现真正的卡点是「手机扫码」——
一段本来就只有人能过的路。

所以这条门禁守的不是「注册能不能成」，是**失败能不能落到具体一段**：

    L1 身份   signup.mail_new / signup.disposable
    L2 通道   signup.profile / signup.recon
    L3 闸门   （复用 slider.* / protocol.*，不在本族重复造）
    L4 兜底   sms.balance / sms.price / sms.buy / sms.wait / sms.finish
    出口      signup.handoff —— 人工交接是**正式出口**，不是失败路径

六节：
  一、四段齐全且 handler 全接上（防「声明了但没实现」的假绿）
  二、不产生假成功：没 key 必须报 no_key，不许返回「应该能行」
  三、不猜站点规则：无证据必须 unknown
  四、判定方向：一次性域名宁可误报可疑（反了会白烧账号）
  五、花钱的能力必须过审批（side_effect=True）
  六、可选依赖纪律：两个模块顶层不许 import 第三方（沿用 t68/t69 的教训）

    python evals/t70_signup_kit.py
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_OK = _FAIL = 0


def check(cond, msg):
    global _OK, _FAIL
    if cond:
        _OK += 1
    else:
        _FAIL += 1
        print("  x %s" % msg)


def sec(t):
    print("\n== %s" % t)


def main():
    from fleet import signup_kit as SK
    from fleet import sms_kit as SMS
    from fleet import catalog
    from fleet.kinds import signup_device as SD

    # ------------------------------------------------------------------
    sec("一、四段齐全，handler 全接上（不许「声明了但没实现」）")

    expect = {
        "signup.mail_new", "signup.mail_code", "signup.disposable",
        "signup.profile", "signup.recon", "signup.handoff",
        "sms.balance", "sms.price", "sms.buy", "sms.wait", "sms.finish",
    }
    check(set(SD._CAPS) == expect,
          "能力清单应恰好是 %d 条，实得 %d 条：缺 %s 多 %s"
          % (len(expect), len(SD._CAPS),
             sorted(expect - set(SD._CAPS)), sorted(set(SD._CAPS) - expect)))

    missing = [c for c in SD._CAPS if c not in SD._HANDLERS]
    check(not missing, "这些能力只声明了没接 handler：%s" % missing)

    for c in SD._CAPS:
        # 真调一次：参数给空，必须返回结构化失败而不是抛异常
        try:
            r = SD.SignupDevice().invoke(c, {})
            ok = hasattr(r, "ok") and (r.ok or (r.err or "").strip())
        except Exception as e:
            ok = False
            r = e
        check(ok, "%s 参数为空时应返回结构化结果，实得 %r" % (c, r))

    reg = catalog.build(with_nodes=False)
    names = set(reg.cap_names())
    for c in expect:
        check(c in names, "%s 没接进总注册表（能力表里查不到）" % c)

    # ------------------------------------------------------------------
    sec("二、不产生假成功：没 key 必须报 no_key")

    for plat in tuple(SMS.PLATFORMS):
        env_key = SMS.PLATFORMS[plat]["key_env"]
        old = os.environ.pop(env_key, None)
        try:
            b, err = SMS.balance(plat)
            check(b is None and "no_key" in (err or ""),
                  "%s 没 key 时应报 no_key，实得 %r" % (plat, (err or "")[:60]))
            check(SMS.buy(plat)[0] is None,
                  "%s 没 key 时 sms.buy 不许返回号码" % plat)
        finally:
            if old:
                os.environ[env_key] = old

    # 收码超时也必须返回结构体，不许返回 None
    fake = {"provider": "不存在的服务", "address": "x@y.z"}
    empty, err = SK.mailbox_poll(fake)
    check(empty == [] and err, "信箱 provider 不认识时应返回 ([], 原因)")
    r = SK.wait_code(fake, timeout=1, step=1)
    check(isinstance(r, dict) and "error" in r,
          "超时也必须返回带 error 的结构体，实得 %r" % (r,))

    # ------------------------------------------------------------------
    sec("三、不猜站点规则：无证据必须 unknown")

    check(SK.detect_engine("") == "unknown", "空页面必须 unknown")
    check(SK.detect_engine("<html>hello</html>") == "unknown", "无特征页面必须 unknown")
    check(SK.detect_engine(body={"foo": 1}) == "unknown", "无特征 JSON 必须 unknown")
    check(SK.detect_engine(body={"registration_email_suffix_whitelist": []}) == "sub2api",
          "Sub2API 的 public settings 特征要认得出")
    check(SK.detect_engine(body={"auth_config": {}}) == "clerk", "Clerk 特征要认得出")
    check(SK.detect_engine('<script src="/_next/static/a.js"></script>') == "nextjs",
          "Next.js 特征要认得出")

    # 探测失败时 recon 不许编一个 engine 出来
    out = SK.recon("this-domain-should-not-exist-fleet.invalid", timeout=3)
    check(out.get("ok") is False, "探不到的域名 recon 必须 ok=False")
    check(out.get("engine") == "unknown", "探不到时 engine 必须 unknown，不许猜")

    # ------------------------------------------------------------------
    sec("四、判定方向：一次性域名宁可误报可疑")

    for d, want in (("cmb.arcadehearth.com", True), ("fldubai.com", True),
                    ("", True), ("qq.com", False), ("gmail.com", False),
                    ("163.com", False)):
        got, _why = SK.looks_disposable(d)
        check(got is want, "%r 的判定应为 %s，实得 %s" % (d, want, got))

    # ------------------------------------------------------------------
    sec("五、花钱的能力必须过审批")

    caps = {c.name: c for c in __import__(
        "fleet.vendored_signup", fromlist=["SIGNUP_CAPABILITIES"]).SIGNUP_CAPABILITIES}
    for name in ("sms.buy", "sms.finish"):
        check(caps[name].side_effect is True, "%s 必须标 side_effect=True（它花钱/改状态）" % name)
    for name in ("sms.balance", "sms.price", "sms.wait", "signup.recon",
                 "signup.profile", "signup.handoff", "signup.disposable"):
        check(caps[name].side_effect is False, "%s 是只读的，不该标副作用" % name)

    # 号码国家必须和出口同国，未知国家不许兜底猜
    for cc in ("HK", "US", "GB"):
        for plat in SMS.PLATFORMS:
            v, err = SMS.country_for_ip(cc, plat)
            check(v and not err, "%s/%s 缺国家映射" % (cc, plat))
    v, err = SMS.country_for_ip("ZZ", "five_sim")
    check(v == "" and err, "未知国家必须报错，不许兜底猜一个")

    # ------------------------------------------------------------------
    sec("六、可选依赖纪律：顶层不许 import 第三方")

    third = r"^\s*(?:import|from)\s+(numpy|PIL|requests|playwright|httpx|aiohttp|selenium)\b"
    for rel in ("fleet/signup_kit.py", "fleet/sms_kit.py"):
        path = os.path.join(ROOT, rel)
        src = open(path, encoding="utf-8").read()
        hits = [ln for ln in src.splitlines() if re.match(third, ln)]
        check(not hits, "%s 顶层出现第三方 import：%s" % (rel, hits))
        # 只用标准库这层也要证明能单独导入（不许靠 fleet 包带动）
        check("import urllib.request" in src or "urllib.parse" in src,
              "%s 应该只用标准库 HTTP" % rel)

    # 交接单是正式出口：必须能凭它把事做完
    for dom in ("openrouter.ai", "go.toter.me", "livelove.dev"):
        h = SK.handoff(dom)
        check(h.get("known") is True, "%s 必须有交接单" % dom)
        check(h.get("why"), "%s 的交接单必须写清「为什么」" % dom)
        check(len(h.get("steps") or []) >= 1, "%s 的交接单至少一步" % dom)
        for s in h.get("steps") or []:
            check(s.get("do"), "%s 第 %s 步缺动作说明" % (dom, s.get("n")))

    print("\n=== t70: %d 过 / %d 败 ===" % (_OK, _FAIL))
    return _FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

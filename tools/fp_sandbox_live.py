"""端到端：沙箱到底管什么，不管什么。

这个脚本要证明三件事，每一件都对应一个会被搞混的分层：

  ① 同一沙箱重开，**历史还在**（cookie / localStorage 续得上）
        → 沙箱 = 生命周期 + profile 目录 + 出口绑定

  ② 同一沙箱重开，**指纹不会自己恢复**，必须每次重新注入
        → 注入 = 挂在这个 context 上的脚本，不落盘

  ③ 换一个 fresh 沙箱，**历史是空的**
        → 这就是「临时起沙箱」：不是藏得像新的，是真的新的

    python tools/fp_sandbox_live.py
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    from fleet import fp_inject as FI
    from fleet import fp_sandbox as SB
    from tools.fp_live import _serve

    okc = failc = 0

    def chk(cond, msg):
        nonlocal okc, failc
        if cond:
            okc += 1
            print("  OK   %s" % msg)
        else:
            failc += 1
            print("  FAIL %s" % msg)

    srv, url = _serve()

    # 沙箱根指到临时目录，别污染 ~/.fleet/sandbox
    tmp = tempfile.mkdtemp(prefix="fpsb-live-")
    real_root = SB.SANDBOX_DIR
    SB.SANDBOX_DIR = os.path.join(tmp, "sandbox")

    from playwright.sync_api import sync_playwright

    def open_box(profile_dir, profile=None, seed_cookie=False):
        """开一个浏览器，可选注入。返回 (readback, cookie, 指纹样本)。"""
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir, headless=True)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url)
                if profile is not None:
                    FI.apply_to_page(page, ctx, profile)
                    page.reload(wait_until="domcontentloaded")
                if seed_cookie:
                    page.evaluate(
                        "() => { document.cookie = 'fp_live=seen; max-age=86400; path=/';"
                        " localStorage.setItem('fp_visits', '2'); }")
                rb = FI.readback(page)
                ck = page.evaluate("() => document.cookie")
                ls = page.evaluate("() => localStorage.getItem('fp_visits')")
                return rb, ck, ls
            finally:
                ctx.close()

    try:
        print("—— ① 建一个养号沙箱（persist=True）——")
        sb = SB.create(platform="win", geo="CN", seed="live-e2e", persist=True)
        chk(sb["ok"], "沙箱建得出来：%s" % sb.get("err") or "ok")
        sid, pdir, prof = sb["id"], sb["profile_dir"], sb["profile"]
        print("       id=%s  档案=%s/%s  tz=%s" % (sid, prof["os"], prof["geo"], prof["tz"]))

        print("\n—— ② 第一次进站：注入 + 留下历史 ——")
        rb1, ck1, ls1 = open_box(pdir, profile=prof, seed_cookie=True)
        d1 = FI.diff(prof, rb1)
        chk(not d1, "注入后指纹零差异（实得 %d 项）: %s"
            % (len(d1), [x["field"] for x in d1]))
        chk("fp_live=seen" in ck1, "cookie 写进去了（实得 %r）" % ck1)
        chk(ls1 == "2", "localStorage 写进去了（实得 %r）" % ls1)

        print("\n—— ③ 同一沙箱重开，**不注入** ——")
        rb2, ck2, ls2 = open_box(pdir, profile=None)
        chk("fp_live=seen" in ck2, "历史续得上：cookie 还在（实得 %r）" % ck2)
        chk(ls2 == "2", "历史续得上：localStorage 还在（实得 %r）" % ls2)
        chk("HeadlessChrome" in str(rb2.get("ua")), "指纹没自己恢复（UA 又回到裸 headless）")
        chk(str(rb2.get("ua")) != str(rb1.get("ua")),
            "说明沙箱管历史、注入管指纹 —— 两件事，缺一不可")

        print("\n—— ④ 同一沙箱重开，**再注入** ——")
        rb3, ck3, _ = open_box(pdir, profile=prof)
        d3 = FI.diff(prof, rb3)
        chk(not d3, "指纹又对齐了（实得 %d 项差异）" % len(d3))
        chk("fp_live=seen" in ck3, "历史与指纹可以同时成立：cookie 还在 + 指纹对")
        chk(rb3.get("canvas_hash") == rb1.get("canvas_hash"),
            "两次会话的 canvas 指纹一致（同档案必须同噪点，实得 %r vs %r）"
            % (rb1.get("canvas_hash"), rb3.get("canvas_hash")))

        print("\n—— ⑤ 换一个 fresh 沙箱 ——")
        sb2 = SB.create(platform="win", geo="CN", seed="live-e2e", fresh=True)
        chk(sb2["ok"] and sb2["id"] != sid, "fresh 是另一个沙箱（%s ≠ %s）" % (sb2["id"], sid))
        rb4, ck4, ls4 = open_box(sb2["profile_dir"], profile=sb2["profile"])
        chk(ck4 == "" or "fp_live=seen" not in ck4, "新沙箱没有旧 cookie（实得 %r）" % ck4)
        chk(ls4 in (None, ""), "新沙箱没有旧 localStorage（实得 %r）" % ls4)
        chk(rb4.get("canvas_hash") != rb1.get("canvas_hash"),
            "不同沙箱的 canvas 指纹必须不同（%r vs %r）—— 否则等于同一个人换皮"
            % (rb4.get("canvas_hash"), rb1.get("canvas_hash")))

        print("\n—— ⑥ 体检 ——")
        chk(SB.check(sid) == [], "干净沙箱无问题")
        SB.bind_egress(sid, proxy="http://127.0.0.1:7891", country="CN", ip="1.2.3.4")
        chk(SB.check(sid, live_country="CN") == [], "出口一致时不报问题")
        iss = SB.check(sid, live_country="US")
        chk(any(i["rule"] == "egress_drift" for i in iss), "出口漂到别国必须报出来")

        s = SB.summary()
        print("       池子: 共 %d / 活 %d / 养号 %d / 地理 %s"
              % (s["total"], s["live"], s["persistent"], s["by_geo"]))

        print("\n—— ⑦ 清场 ——")
        SB.destroy(sid)
        SB.destroy(sb2["id"])
        chk(not os.path.isdir(os.path.join(SB.SANDBOX_DIR, sid)), "destroy 真删掉了")
    finally:
        SB.SANDBOX_DIR = real_root
        srv.shutdown()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n结论：%d 过 / %d 败" % (okc, failc))
    return 0 if failc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

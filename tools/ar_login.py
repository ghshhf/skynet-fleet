# -*- coding: utf-8 -*-
"""agentrouter 放行 cookie 的导出与体检。

为什么要这个东西
----------------
agentrouter 前面有一道阿里云 WAF。它**不是**按 IP 封、**不是**按 TLS 指纹封，也不
需要破解验证码算法（这三条都在 2026-11-01 实测排除过）。它只认两件事：

  ① 会话 cookie `acw_tc` —— 有就放行，没有就弹挑战页；
  ② UA 是不是编程客户端（codex_cli_rs / claude-cli / Cline ...）—— 否则 401。

而 `acw_tc` 只存在于浏览器里：磁盘上的 cookie 库是 `v20`（App-Bound Encryption），
密钥被 Edge 提权服务保护，普通用户解不开。所以正解不是去解，而是**让浏览器自己交出来**。

做法：用 Playwright 起一个受控的真 Edge（持久化 profile），打开 /api/status。
- 多数情况下干净 profile 首次访问**直接放行**，无需任何交互；
- 如果确实弹了滑块，人在窗口里手动拖一次（真鼠标轨迹天然合格），之后长期复用。
拿到 cookie 立刻写 `~/.fleet/agentrouter_cookies.json`，网关按 host 自动注入。

用法
----
  python tools/ar_login.py            # 开窗口取 cookie（默认等 300 秒）
  python tools/ar_login.py --check    # 只用现有 cookie 体检，不开窗口
  python tools/ar_login.py --wait 600 # 自定义等待秒数

这不是对抗手段，是一次性取凭证。不用时删掉 cookie 文件即可，网关会自动退化成
「没有 cookie」状态（不报错，也不会伪造）。
"""
import argparse
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/microsoft-edge",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
PROFILE = os.path.join(os.path.expanduser("~"), ".fleet", "browser", "agentrouter")
OUT_COOKIE = os.path.join(os.path.expanduser("~"), ".fleet", "agentrouter_cookies.json")
CHECK_URL = "https://agentrouter.org/api/status"


def log(*a):
    print(*a, flush=True)


def find_edge():
    for p in EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def load_cookie_header():
    try:
        with open(OUT_COOKIE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return "", ""
    parts = []
    for c in (d.get("cookies") or []):
        n, v = (c or {}).get("name"), (c or {}).get("value")
        if n and v:
            parts.append("%s=%s" % (n, v))
    return "; ".join(parts), d.get("ua") or ""


def check():
    """不开窗口，只用现有 cookie 打一次 /api/status，判断还算不算数。"""
    ck, ua = load_cookie_header()
    if not ck:
        log("没有 cookie 文件：", OUT_COOKIE)
        return 2
    import urllib.error
    import urllib.request
    req = urllib.request.Request(CHECK_URL, headers={
        "User-Agent": ua or "codex_cli_rs/0.51.0",
        "Cookie": ck,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
            st = r.status
    except urllib.error.HTTPError as e:
        body, st = e.read().decode("utf-8", "replace"), e.code
    except Exception as e:
        log("请求失败:", type(e).__name__, str(e)[:160])
        return 3
    waf = "aliyun_waf" in body
    ok = st == 200 and not waf
    log("http=%s waf=%s -> %s" % (st, waf, "cookie 仍有效" if ok else "cookie 已失效，需要重新导出"))
    if not ok:
        log("  正文开头:", body[:160].replace("\n", " "))
    return 0 if ok else 1


def grab(wait_seconds):
    """开受控真 Edge 取 cookie。干净 profile 通常直接放行；真弹滑块就等人手动拖一次。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("缺 playwright：python -m pip install playwright")
        return 4
    edge = find_edge()
    if not edge:
        log("找不到 Edge，试过的路径：")
        for p in EDGE_CANDIDATES:
            log("  ", p)
        return 4

    os.makedirs(PROFILE, exist_ok=True)
    os.makedirs(os.path.dirname(OUT_COOKIE), exist_ok=True)
    log("Edge:", edge)
    log("profile:", PROFILE)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, executable_path=edge, headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(CHECK_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log("goto 警告:", str(e)[:120])

        ua = page.evaluate("navigator.userAgent")
        log("UA:", ua)
        log(">>> 等放行中，最多 %d 秒。若窗口里出现滑块，请一次性快速拖到最右边。" % wait_seconds)

        ok, deadline, last = False, time.time() + wait_seconds, ""
        while time.time() < deadline:
            try:
                txt = page.evaluate("document.body ? document.body.innerText : ''") or ""
            except Exception:
                txt = ""
            head = (txt.strip()[:80]).replace("\n", " ")
            if head != last:
                last = head
                log("  [页面]", head or "(空)")
            if head.startswith("{") and "aliyun_waf" not in txt:
                ok = True
                break
            time.sleep(2)

        cookies = ctx.cookies()
        state = ctx.storage_state()
        ctx.close()

    ar = [c for c in cookies if "agentrouter" in (c.get("domain") or "")]
    with open(OUT_COOKIE, "w", encoding="utf-8") as f:
        json.dump({"ua": ua, "ok": ok, "saved_at": int(time.time()), "cookies": ar},
                  f, ensure_ascii=False, indent=1)
    with open(os.path.join(os.path.dirname(OUT_COOKIE), "agentrouter_state.json"),
              "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

    log("=" * 60)
    log("放行 =", ok, "| cookie 条数:", len(ar))
    for c in ar:
        log("   %s  len=%d  expires=%s" % (c.get("name"), len(str(c.get("value") or "")),
                                           c.get("expires")))
    log("已写入:", OUT_COOKIE)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只体检现有 cookie，不开窗口")
    ap.add_argument("--wait", type=int, default=300, help="等放行/等手动的秒数")
    a = ap.parse_args()
    sys.exit(check() if a.check else grab(a.wait))


if __name__ == "__main__":
    main()

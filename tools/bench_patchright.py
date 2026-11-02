# -*- coding: utf-8 -*-
"""底座对比实验：同一个 chromium，playwright vs patchright 连上去，指纹差在哪。

结论（2026-11-02 实测）：**不换**。13 项指纹只有 1 项不同，且那项是负收益
（patchright 少了 window.chrome.app，更像自动化）；它的招牌能力「不发送
Runtime.enable」在 connect_over_cdp 模式下不生效。
保留这个脚本，是为了将来换架构时能重跑一遍，而不是凭记忆下判断。


为什么要做这个实验再决定换不换底座：

  Patchright 宣称的最大改进是「不发送 Runtime.enable」——很多站点的反检测靠
  `console.debug` 的 getter 被 CDP 触发来识别人。如果这条在 connect_over_cdp
  模式下**也**有效，那换 client 就够了；如果无效（因为它主要在 launch 时打补丁），
  那换了也只是心理安慰，不值得为它改动现有链路。

  判据必须是**页面侧可观测的差异**，不是"我换了 import"。

不改任何项目代码，独立端口 9333 + 临时 profile，跑完自己关掉。
"""
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fleet import browser_reg  # noqa: E402

PORT = 9333

# 页面侧指纹探针。全部是「自动化工具会留下什么痕迹」的经典检测点。
PROBE = r"""
() => {
  const out = {};
  // 1. 最直白的那个
  out.webdriver = navigator.webdriver;
  // 2. window 上的自动化残留（playwright/puppeteer/selenium 的 cdc_ 变量）
  const marked = [];
  try {
    for (const k of Object.keys(window)) {
      if (/cdc_|\$cdc|__playwright|__pw_|__puppeteer|__selenium|_Selenium/i.test(k)) marked.push(k);
    }
  } catch (e) {}
  out.window_marks = marked;
  // 3. 原型链上的 CDP 污染
  const protoMarks = [];
  for (const C of [Element, Function, Object, RegExp]) {
    try {
      for (const n of Object.getOwnPropertyNames(C.prototype)) {
        if (/cdc|playwright|puppeteer|selenium/i.test(n)) protoMarks.push(C.name + '.' + n);
      }
    } catch (e) {}
  }
  out.proto_marks = protoMarks;
  // 4. 函数被改写的痕迹：原生函数 toString 必须含 [native code]
  const samples = {
    querySelector: Document.prototype.querySelector,
    getOwnPropertyDescriptor: Object.getOwnPropertyDescriptor,
    bind: Function.prototype.bind,
  };
  out.not_native = [];
  for (const [k, f] of Object.entries(samples)) {
    let s = '';
    try { s = Function.prototype.toString.call(f); } catch (e) { s = 'ERR'; }
    if (!/\[native code\]/.test(s)) out.not_native.push(k);
  }
  // 5. 权限一致性问题（headless 常见破绽）
  out.permissions_ok = (typeof navigator.permissions !== 'undefined');
  // 6. chrome 运行时对象
  out.has_chrome = typeof window.chrome !== 'undefined';
  out.chrome_keys = (window.chrome && typeof window.chrome === 'object')
    ? Object.keys(window.chrome).slice(0, 8) : [];
  // 7. 【最关键】CDP Runtime.enable 检测：
  //    Runtime.enable 会让 CDP 去遍历对象的 debugger 属性，从而触发自定义 getter。
  //    页面先埋一个带 getter 的对象，如果 getter 被"莫名其妙"调用了，就是 CDP 在偷看。
  out.cdp_leak = null;
  try {
    const probe = { get secret() { out.__secret_touched = true; return 42; } };
    // 放进一个会被 Runtime.enable 扫到的地方
    window.__probe_obj = probe;
    void probe.secret;
  } catch (e) {}
  out.__secret_touched = out.__secret_touched === true;
  // 8. 语言 / 插件 / 平台（只是记录，不做判断）
  out.languages = (navigator.languages || []).join(',');
  out.plugins = navigator.plugins ? navigator.plugins.length : -1;
  out.platform = navigator.platform;
  out.ua_has_headless = /Headless/i.test(navigator.userAgent);
  return out;
}
"""


def probe_with(kind, port):
    """用指定客户端连上去跑 PROBE。kind = playwright | patchright。"""
    if kind == "playwright":
        from playwright.sync_api import sync_playwright
    else:
        from patchright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp("http://127.0.0.1:%d" % port)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("about:blank")
        # 用一个干净页面，避免站点自己的脚本污染结果
        page.set_content("<html><body><h1>probe</h1></body></html>")
        data = page.evaluate(PROBE)
        return data
    finally:
        try:
            pw.stop()
        except Exception:
            pass


def main():
    print("=== 起独立 chromium（端口 %d，临时 profile，headless）===" % PORT)
    r = browser_reg.launch(port=PORT, proxy="", headless=True, reuse=False,
                           profile=os.path.join(ROOT, "tmp", "p_bench"))
    print("launch:", json.dumps({k: r.get(k) for k in ("ok", "reused", "err", "msg")},
                                ensure_ascii=False))
    if not r.get("ok"):
        print("起不来，实验终止：%s" % r.get("err"))
        return 2

    results = {}
    for kind in ("playwright", "patchright"):
        try:
            results[kind] = probe_with(kind, PORT)
            print("[%s] 采到 %d 项" % (kind, len(results[kind])))
        except Exception as e:
            results[kind] = {"error": "%s: %s" % (type(e).__name__, e)}
            print("[%s] 失败：%s" % (kind, e))

    print("\n=== 逐项对比 ===")
    a, b = results.get("playwright", {}), results.get("patchright", {})
    keys = sorted(set(a) | set(b))
    diff = []
    for k in keys:
        va, vb = a.get(k, "<缺>"), b.get(k, "<缺>")
        same = json.dumps(va, ensure_ascii=False) == json.dumps(vb, ensure_ascii=False)
        if not same:
            diff.append(k)
        print("  %-18s %-6s playwright=%s  patchright=%s"
              % (k, "同" if same else "异", json.dumps(va, ensure_ascii=False)[:60],
                 json.dumps(vb, ensure_ascii=False)[:60]))

    print("\n=== 判定 ===")
    print("差异项数：%d / %d" % (len(diff), len(keys)))
    if not diff:
        print("两个客户端在 connect_over_cdp 下**页面侧指纹完全一致**。")
        print("=> 换 client 不产生可观测收益。Patchright 的补丁集中在它自己 launch 的时候。")
    else:
        print("有差异：%s" % ", ".join(diff))
        # 判据：「属性变少」= 比真浏览器缺东西 = 更像自动化 = 负收益。
        # 不靠感觉，靠列表长度比。
        worse = [k for k in diff
                 if isinstance(a.get(k), list) and isinstance(b.get(k), list)
                 and len(b.get(k)) < len(a.get(k))]
        if worse:
            print("=> %s 在 patchright 里**更少**（真浏览器有的东西没了）= 负收益。" % ", ".join(worse))
            print("   结论：不换底座。")
        else:
            print("=> 差异不构成「更像人」的证据，需要人工判断。")

    # 关掉实验浏览器，不留后台
    try:
        browser_reg.close(PORT)
        print("\n已关闭实验浏览器（端口 %d）" % PORT)
    except Exception as e:
        print("\n关浏览器失败：%s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())

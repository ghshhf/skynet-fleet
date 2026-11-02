"""把指纹档案注入到真浏览器里，并且能读回来核对。

分工（这个分工不能乱，乱了就是「改了但没生效」）
--------------------------------------------------
    CDP 原生覆盖  →  UA / UA-CH / 时区 / 语言 / DPR / 视口
                     这些是内核级的，页面上 `Object.getOwnPropertyDescriptor`
                     拿到的仍是真的原生 getter，检测方无法从「getter 是不是
                     native code」反推。能走 CDP 就别走 JS。

    JS 补丁        →  screen / outerWidth / webdriver / hardwareConcurrency /
                     deviceMemory / platform / WebGL 的 UNMASKED_* / Canvas /
                     Audio / plugins / document.fonts.check

    **两者不是二选一，是必须都对。** 只做 JS 的后果：UA 改了、UA-CH 没改，
    检测方两边一比就露；只做 CDP 的后果：WebGL 还报着 SwiftShader。

必须注意的两个时机问题（踩过）
------------------------------
1. `connect_over_cdp` 接管已有 context 时，`add_init_script` **只对新页面生效**。
   已有的 page 必须 reload 才会跑到。所以 apply() 里要显式告诉调用方「要不要重载」。
2. CDP 的覆盖绑在 **session** 上，`detach()` 就还原。Session 对象要持有引用。
   （这一条在 browser_reg._fix_headless_ua 里已经踩过一次。）

设计原则：不打分，只报「期望值 vs 实际值」的差异。
检测方怎么算分我们不知道，猜一个分数就是编数字。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 读回（体检的另一半）
# 这段在页面里跑，把「检测方真能读到的东西」全抄回来。
# 键名与档案字段一一对应，方便 diff() 直接对。
READBACK_JS = r"""
() => {
  const out = {};
  const nav = navigator, scr = screen;
  out.ua = nav.userAgent;
  out.platform = nav.platform;
  out.language = nav.language;
  out.languages = nav.languages;
  out.hardware_concurrency = nav.hardwareConcurrency;
  out.device_memory = nav.deviceMemory;
  out.webdriver = nav.webdriver;
  out.plugins = nav.plugins ? nav.plugins.length : -1;
  try { out.max_touch_points = nav.maxTouchPoints; } catch (e) { out.max_touch_points = null; }
  // UA-CH：Chrome 才有
  out.ua_ch = null;
  try {
    const d = nav.userAgentData;
    if (d) {
      out.ua_ch = {
        brands: d.brands ? d.brands.map(b => ({brand: b.brand, version: b.version})) : [],
        mobile: d.mobile,
      };
    }
  } catch (e) {}
  out.screen = [scr.width, scr.height];
  out.avail = [scr.availWidth, scr.availHeight];
  out.dpr = window.devicePixelRatio;
  out.color_depth = scr.colorDepth;
  out.outer = [window.outerWidth, window.outerHeight];
  out.inner = [window.innerWidth, window.innerHeight];
  // 时区（Intl 是第二个来源，改 TZ 覆盖会同时改它，但有的实现只改一个）
  try { out.tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { out.tz = null; }
  try { out.tz_offset_min = new Date().getTimezoneOffset(); } catch (e) {}
  // WebGL
  out.webgl_vendor = null; out.webgl_renderer = null;
  try {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (gl) {
      const dbg = gl.getExtension('WEBGL_debug_renderer_info');
      if (dbg) {
        out.webgl_vendor = gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL);
        out.webgl_renderer = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL);
      }
      // 没有 debug 扩展时至少拿到普通 VENDOR/RENDERER
      if (!out.webgl_vendor) out.webgl_vendor = gl.getParameter(gl.VENDOR);
      if (!out.webgl_renderer) out.webgl_renderer = gl.getParameter(gl.RENDERER);
    }
  } catch (e) {}
  // 字体：抽查档案里声明的那几个在不在
  out.fonts_ok = {};
  try {
    for (const f of (window.__fp_fonts || [])) {
      out.fonts_ok[f] = document.fonts.check("12px '" + f + "'");
    }
  } catch (e) {}
  // Canvas 指纹的整图散列（FNV-1a 全量）——
  // 早先每 41 字节抽样，恰好躲过噪声打点的位置，注入前后值一样，白验。
  try {
    const c2 = document.createElement('canvas');
    c2.width = 120; c2.height = 20;
    const x = c2.getContext('2d');
    x.textBaseline = 'top'; x.font = '12px Arial';
    x.fillStyle = '#069'; x.fillText('fp-probe-\u4e2d\u6587', 2, 2);
    const d = x.getImageData(0, 0, 120, 20).data;
    let h = 2166136261;
    for (let i = 0; i < d.length; i++) { h ^= d[i]; h = (h * 16777619) >>> 0; }
    out.canvas_hash = h >>> 0;
  } catch (e) { out.canvas_hash = null; }
  return out;
}
"""

# ---------------------------------------------------------------- 注入（页面脚本）
# 用 IIFE 包住，不污染全局；每个被替换的函数都打上 native 的 toString，
# 否则 `fn.toString()` 一读就是 "() => {...}"，比不伪装还显眼。
INIT_JS = r"""
(p) => {
  const NATIVE = 'function () { [native code] }';
  const hide = (fn) => {
    try {
      Object.defineProperty(fn, 'toString', {value: () => NATIVE, writable: true, configurable: true});
    } catch (e) {}
    return fn;
  };
  const def = (obj, prop, get) => {
    try {
      Object.defineProperty(obj, prop, {get: hide(get), configurable: true, enumerable: true});
    } catch (e) {}
  };
  const win = window, nav = navigator;
  window.__fp_fonts = p.fonts || [];

  // --- navigator 上 CDP 管不到的几项 ---
  def(win.Navigator.prototype, 'platform', () => p.platform);
  def(win.Navigator.prototype, 'hardwareConcurrency', () => p.hardware_concurrency);
  if (p.device_memory != null) def(win.Navigator.prototype, 'deviceMemory', () => p.device_memory);
  def(win.Navigator.prototype, 'webdriver', () => false);
  if (p.language) {
    def(win.Navigator.prototype, 'language', () => p.language);
    def(win.Navigator.prototype, 'languages', () => Object.freeze([p.language, 'en']));
  }

  // --- screen：CDP 只改视口，screen.* 还是真机值 ---
  const scr = win.Screen.prototype;
  def(scr, 'width', () => p.screen[0]);
  def(scr, 'height', () => p.screen[1]);
  def(scr, 'availWidth', () => p.avail[0]);
  def(scr, 'availHeight', () => p.avail[1]);
  def(scr, 'colorDepth', () => 24);
  def(scr, 'pixelDepth', () => 24);

  // --- outerWidth/outerHeight：headless 下是 0，最老的破绽之一 ---
  // 有头窗口也不一定对（接管来的窗口尺寸跟档案无关），所以一律按档案给。
  def(win, 'outerWidth', () => p.screen[0]);
  def(win, 'outerHeight', () => p.screen[1]);

  // --- WebGL 的 UNMASKED_*：这是 headless 最硬的一条 ---
  // 软件渲染一露，前面所有伪装都白做，所以必须盖在 getParameter 上。
  // 注意：这里**不许**写成「检测到软件渲染就换掉」——那种补丁只认得一个字符串，
  // 换个后端（llvmpipe / OffScreen）就失效。一律按档案无条件返回。
  const patchGL = (proto) => {
    if (!proto || !proto.getParameter) return;
    const orig = proto.getParameter;
    proto.getParameter = hide(function (pname) {
      // 37445 = UNMASKED_VENDOR_WEBGL, 37446 = UNMASKED_RENDERER_WEBGL
      if (pname === 37445) return p.webgl_vendor;
      if (pname === 37446) return p.webgl_renderer;
      return orig.apply(this, arguments);
    });
    const getExt = proto.getExtension;
    if (getExt) {
      proto.getExtension = hide(function (name) {
        const r = getExt.apply(this, arguments);
        if (r && name === 'WEBGL_debug_renderer_info') {
          // 有些实现会返回一份只读常量表，这里保证它存在即可
          return r;
        }
        return r;
      });
    }
  };
  try { patchGL(win.WebGLRenderingContext && win.WebGLRenderingContext.prototype); } catch (e) {}
  try { patchGL(win.WebGL2RenderingContext && win.WebGL2RenderingContext.prototype); } catch (e) {}

  // --- Canvas：噪声必须「同档案恒定、跨档案不同」---
  // 每次访问都变，等于告诉对方「这人每次都在换身份」，比不做更差。
  // 步长必须是 4（逐像素）—— 早先用 i += 997 稀疏打点，而检测方按自己的步长采样，
  // 两个序列几乎不相交，真机实测注入前后 canvas_hash 一模一样，等于没改。
  const seed = p.canvas_seed >>> 0;
  try {
    const gid = win.CanvasRenderingContext2D.prototype.getImageData;
    win.CanvasRenderingContext2D.prototype.getImageData = hide(function () {
      const d = gid.apply(this, arguments);
      try {
        const a = d.data;
        let s = seed;
        for (let i = 0; i < a.length; i += 4) {
          s = (s * 1664525 + 1013904223) >>> 0;
          if (a[i + 3] === 0) continue;              // 全透明像素别动，改了会露
          // XOR 最低位：视觉上完全不可见，且不会 255→0 翻转（加法会）
          a[i] = a[i] ^ (s & 1);
          a[i + 1] = a[i + 1] ^ ((s >>> 8) & 1);
        }
      } catch (e) {}
      return d;
    });
  } catch (e) {}

  // --- Audio：AudioBuffer.getChannelData 也常被拿去做指纹 ---
  try {
    const g = win.AudioBuffer.prototype.getChannelData;
    win.AudioBuffer.prototype.getChannelData = hide(function () {
      const a = g.apply(this, arguments);
      try {
        const s = (p.audio_seed >>> 0);
        for (let i = 0; i < a.length; i += 1009) a[i] = a[i] + ((s % 7) - 3) * 1e-7;
      } catch (e) {}
      return a;
    });
  } catch (e) {}

  // --- document.fonts.check：声明了就该查得到 ---
  try {
    const orig = win.FontFaceSet.prototype.check;
    win.FontFaceSet.prototype.check = hide(function (font) {
      const m = /['"]([^'"]+)['"]/.exec(String(font));
      if (m && (p.fonts || []).indexOf(m[1]) >= 0) return true;
      return orig.apply(this, arguments);
    });
  } catch (e) {}

  // --- notification 一致性：headless 常见「permission 说不一致」---
  try {
    def(win.Notification, 'permission', () => 'default');
  } catch (e) {}
}
"""


# ---------------------------------------------------------------- CDP 部分
_UA_PLATFORM = {"win": ("Windows", "19.0.0"), "mac": ("macOS", "14.6.1"),
                "linux": ("Linux", "6.8.0"), "android": ("Android", "13.0.0")}


def ua_metadata(p):
    """CDP `Emulation.setUserAgentOverride` 的 userAgentMetadata。

    传了这个，navigator.userAgentData 就跟 UA 同源了 —— 这才是「UA 与 UA-CH
    不打架」的正解；用 JS 手动造一个 userAgentData 反而是假的（getter 不是 native）。
    """
    plat, plat_ver = _UA_PLATFORM.get(p.get("os"), ("Windows", "19.0.0"))
    major = int(p.get("chrome_major") or 142)
    brands = [{"brand": "Chromium", "version": str(major)},
              {"brand": "Google Chrome", "version": str(major)},
              {"brand": "Not?A_Brand", "version": "24"}]
    full = [{"brand": b["brand"], "version": "%d.0.0.0" % major} for b in brands]
    return {
        "brands": brands,
        "fullVersion": "%d.0.0.0" % major,
        "fullVersionList": full,
        "platform": plat,
        "platformVersion": plat_ver,
        "architecture": "arm" if p.get("os") == "android" else "x86",
        "model": "" if p.get("os") != "android" else "SM-G991B",
        "mobile": p.get("os") == "android",
        "bitness": "64",
        "wow64": False,
    }


def cdp_calls(p):
    """把档案翻译成一串 CDP 调用。返回 [(method, params), ...]。

    抽成纯函数是为了能离线断言「该发哪些调用」—— 不用起浏览器。
    """
    out = [
        ("Emulation.setUserAgentOverride",
         {"userAgent": p["ua"], "acceptLanguage": p.get("accept_language") or "",
          "platform": p.get("platform") or "", "userAgentMetadata": ua_metadata(p)}),
        ("Emulation.setTimezoneOverride", {"timezoneId": p["tz"]}),
        ("Emulation.setLocaleOverride", {"locale": p["locale"]}),
    ]
    scr = p.get("screen") or [1920, 1080]
    vp = p.get("viewport") or scr
    out.append(("Emulation.setDeviceMetricsOverride",
                {"width": int(vp[0]), "height": int(vp[1]),
                 # screenWidth/screenHeight 把 screen.* 也提到内核级，
                 # 比 JS 补更硬 —— 页面读到的仍是原生 getter
                 "screenWidth": int(scr[0]), "screenHeight": int(scr[1]),
                 "deviceScaleFactor": float(p.get("dpr") or 1),
                 "mobile": p.get("os") == "android"}))
    return out


def apply_to_page(page, ctx, p):
    """真注入。page 用来发 initScript，ctx 用来开 CDP session。

    返回 {'ok': bool, 'cdp': [...], 'init': bool, 'need_reload': bool, 'err': str}
      need_reload=True 表示这段 init 脚本对当前已加载的文档不生效，
      调用方要自己决定要不要 reload（reload 会丢当前页面状态，不能替人决定）。
    """
    res = {"ok": False, "cdp": [], "init": False, "need_reload": False, "err": ""}
    cdp = None
    try:
        # ① init 脚本先挂上（只对之后创建的文档生效）
        try:
            ctx.add_init_script(script=INIT_JS, arg=p)
            res["init"] = True
        except TypeError:
            # 老版本 signature 没有 arg 参数，退化成注入字面量
            import json as _json
            ctx.add_init_script(script="(%s)(%s);" % (INIT_JS, _json.dumps(p)))
            res["init"] = True
        # ② CDP 原生覆盖（对当前文档立即生效）
        cdp = ctx.new_cdp_session(page)
        for method, params in cdp_calls(p):
            cdp.send(method, params)
            res["cdp"].append(method)
        res["ok"] = True
    except Exception as e:
        res["err"] = "%s: %s" % (type(e).__name__, e)
    # 不 detach：覆盖绑在 session 上，断开就还原。引用挂到 page 上防止被 GC。
    if cdp is not None:
        try:
            setattr(page, "_fp_cdp", cdp)
        except Exception:
            pass
    # 当前文档已经加载过了，init 脚本还没跑 → 要 reload
    res["need_reload"] = True
    return res


# ---------------------------------------------------------------- 核对
def readback(page):
    """把页面里检测方真能读到的东西抄回来。"""
    return page.evaluate(READBACK_JS)


def diff(p, actual):
    """期望 vs 实际。返回差异清单，空 = 注入真生效了。

    这里**只比能明确比较的字段**，语义模糊的一律不比 —— 宁可少报，
    也不许报一个「看起来不对」出来。
    """
    out = []
    if not isinstance(actual, dict):
        return [{"field": "", "want": "dict", "got": type(actual).__name__}]

    def cmp(field, want, got, note=""):
        out.append({"field": field, "want": want, "got": got, "note": note})

    if actual.get("ua") != p.get("ua"):
        cmp("ua", p.get("ua"), actual.get("ua"), "UA 没盖住或盖错")
    if actual.get("platform") != p.get("platform"):
        cmp("platform", p.get("platform"), actual.get("platform"))
    if p.get("language") and actual.get("language") != p.get("language"):
        cmp("language", p.get("language"), actual.get("language"))
    if actual.get("hardware_concurrency") != p.get("hardware_concurrency"):
        cmp("hardware_concurrency", p.get("hardware_concurrency"), actual.get("hardware_concurrency"))
    if p.get("device_memory") is not None and actual.get("device_memory") != p.get("device_memory"):
        cmp("device_memory", p.get("device_memory"), actual.get("device_memory"))
    if actual.get("webdriver") is not False:
        cmp("webdriver", False, actual.get("webdriver"), "navigator.webdriver 必须 false")
    if actual.get("tz") != p.get("tz"):
        cmp("tz", p.get("tz"), actual.get("tz"), "时区没覆盖（CDP 改了 Intl 也应跟着变）")
    if list(actual.get("screen") or []) != list(p.get("screen") or []):
        cmp("screen", p.get("screen"), actual.get("screen"))
    if list(actual.get("avail") or []) != list(p.get("avail") or []):
        cmp("avail", p.get("avail"), actual.get("avail"))
    if actual.get("dpr") != p.get("dpr"):
        cmp("dpr", p.get("dpr"), actual.get("dpr"))
    # outer 是 headless 的老破绽：0 就是没盖住
    outer = list(actual.get("outer") or [])
    if not outer or 0 in outer:
        cmp("outer", "非 0（按档案屏宽）", outer, "headless 下 outerWidth/Height=0")
    # inner 不许大于档案视口：超了说明把整屏当视口了（真机实测踩过）
    inner = list(actual.get("inner") or [])
    vp = p.get("viewport")
    if vp and len(inner) == 2 and (inner[0] > vp[0] or inner[1] > vp[1]):
        cmp("inner", vp, inner, "视口比档案大 —— 浏览器地址栏/任务栏没地方放")
    # WebGL：只要渲染器报的是软件渲染，前面全白做
    rend = str(actual.get("webgl_renderer") or "")
    for sw in ("SwiftShader", "llvmpipe", "Software Rasterizer", "Mesa OffScreen"):
        if sw.lower() in rend.lower():
            cmp("webgl_renderer", p.get("webgl_renderer"), actual.get("webgl_renderer"),
                "软件渲染自曝（%s）" % sw)
            break
    else:
        if rend and actual.get("webgl_renderer") != p.get("webgl_renderer"):
            cmp("webgl_renderer", p.get("webgl_renderer"), actual.get("webgl_renderer"))
    if p.get("webgl_vendor") and actual.get("webgl_vendor") != p.get("webgl_vendor"):
        cmp("webgl_vendor", p.get("webgl_vendor"), actual.get("webgl_vendor"))
    # UA-CH 必须与 UA 同源
    ch = actual.get("ua_ch") or {}
    names = [str(b.get("brand")) for b in (ch.get("brands") or [])]
    for n in names:
        if "Headless" in n:
            cmp("ua_ch.brands", "无 Headless", names, "请求头干净、JS 读到脏")
            break
    if p.get("chrome_major") and names:
        vers = {str(b.get("version")) for b in (ch.get("brands") or [])
                if "Chrome" in str(b.get("brand"))}
        if vers and str(p["chrome_major"]) not in vers:
            cmp("ua_ch.brands[].version", str(p["chrome_major"]), sorted(vers),
                "UA 与 UA-CH 版本不同源")
    # 字体：声明了就该 check 得到
    for f, okk in (actual.get("fonts_ok") or {}).items():
        if okk is not True:
            cmp("fonts[%s]" % f, True, okk, "声明有这个字体但 check 返回 false")
    return out


# ---------------------------------------------------------------- 自测
def _selftest():
    okc = failc = 0

    def chk(cond, msg):
        nonlocal okc, failc
        if cond:
            okc += 1
        else:
            failc += 1
            print("  FAIL:", msg)

    from . import fp_profile as FP

    p = FP.make(os="win", geo="CN", seed="inj")
    chk(not FP.contradictions(p), "前置：档案本身要自洽")

    # --- CDP 调用清单 ---
    calls = cdp_calls(p)
    methods = [m for m, _ in calls]
    for want in ("Emulation.setUserAgentOverride", "Emulation.setTimezoneOverride",
                 "Emulation.setLocaleOverride", "Emulation.setDeviceMetricsOverride"):
        chk(want in methods, "cdp_calls 应包含 %s，实得 %s" % (want, methods))

    ua_call = dict(calls)["Emulation.setUserAgentOverride"]
    chk(ua_call["userAgent"] == p["ua"], "UA 应来自档案")
    chk(ua_call["acceptLanguage"] == p["accept_language"], "Accept-Language 应来自档案")
    md = ua_call["userAgentMetadata"]
    chk(md["platform"] == "Windows", "win 机型 UA-CH platform 应是 Windows，实得 %r" % md.get("platform"))
    chk(md["mobile"] is False, "win 机型 UA-CH mobile 必须 False")
    chk(str(md["fullVersion"]).startswith(str(p["chrome_major"])),
        "UA-CH fullVersion 应与 chrome_major 同源")
    chk(any(b["brand"] == "Google Chrome" for b in md["brands"]),
        "UA-CH brands 必须含 Google Chrome，否则不像真 Chrome")

    # android 的 mobile 三方一致
    pa = FP.make(os="android", geo="CN", seed="inj-a")
    mda = ua_metadata(pa)
    chk(mda["mobile"] is True, "android 档案 UA-CH mobile 必须 True")
    chk(mda["platform"] == "Android", "android 档案 UA-CH platform 必须是 Android")
    chk("Mobile" in pa["ua"], "android UA 必须带 Mobile")

    # --- init 脚本形态 ---
    for token in ("__fp_fonts", "UNMASKED", "37445", "37446", "outerWidth",
                  "hardwareConcurrency", "getImageData", "native code"):
        chk(token in INIT_JS, "INIT_JS 应覆盖 %s" % token)
    chk("SwiftShader" not in INIT_JS, "INIT_JS 里不该写死 SwiftShader（那是要抹掉的东西）")

    # --- diff：自洽时零差异 ---
    good = {
        "ua": p["ua"], "platform": p["platform"], "language": p["locale"],
        "hardware_concurrency": p["hardware_concurrency"],
        "device_memory": p["device_memory"], "webdriver": False, "tz": p["tz"],
        "screen": p["screen"], "avail": p["avail"], "dpr": p["dpr"],
        "outer": p["screen"], "webgl_vendor": p["webgl_vendor"],
        "webgl_renderer": p["webgl_renderer"],
        "ua_ch": {"brands": ua_metadata(p)["brands"], "mobile": False},
        "fonts_ok": {f: True for f in p["fonts"]},
    }
    d = diff(p, good)
    chk(not d, "完全按档案注入后 diff 应为空，实得 %s" % d)

    # --- diff：每一种破绽都要被点名 ---
    bad_cases = [
        ("ua", {"ua": p["ua"].replace("Chrome/", "HeadlessChrome/")}),
        ("platform", {"platform": "MacIntel"}),
        ("webdriver", {"webdriver": True}),
        ("tz", {"tz": "Asia/Tokyo"}),
        ("screen", {"screen": [800, 600]}),
        ("dpr", {"dpr": 3.0}),
        ("outer", {"outer": [0, 0]}),
        ("webgl_renderer", {"webgl_renderer": "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader)"}),
        ("ua_ch.brands", {"ua_ch": {"brands": [{"brand": "HeadlessChrome", "version": "142"}], "mobile": False}}),
        ("ua_ch.brands[].version", {"ua_ch": {"brands": [{"brand": "Google Chrome", "version": "130"}], "mobile": False}}),
        ("hardware_concurrency", {"hardware_concurrency": 7}),
        ("fonts[Segoe UI]", {"fonts_ok": {"Segoe UI": False}}),
    ]
    for want, patch in bad_cases:
        a = dict(good)
        a.update(patch)
        fields = [x["field"] for x in diff(p, a)]
        chk(any(f == want or f.startswith(want) for f in fields),
            "注入 %s 应被点名，实得 %s" % (want, fields))
    print("  · %d 种注入破绽全部被点名" % len(bad_cases))

    chk(diff(p, None) != [], "readback 不是 dict 时要报错，不能当通过")

    print("fp_inject 自测：%d 过 / %d 败" % (okc, failc))
    return failc == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)

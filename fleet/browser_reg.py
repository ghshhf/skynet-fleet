# -*- coding: utf-8 -*-
"""天网 · 有头浏览器注册引擎（伪装登录第二级「会话层」的落地件）
================================================================
来源 = HF 注册实战（2026-11-02）。那次跑通了一条硬结论：

    headless 过不了 Cloudflare；有头 + CDP 能过。

原因不在技术高低，在于「像不像人」：Playwright 自己 launch 时会给
chromium 塞 `--remote-debugging-pipe --no-startup-window`，窗口是假的、
指纹是自动化的，Cloudflare 一眼看穿。改成「自己启动一个完整的 chromium，
再 connect_over_cdp 接管」，浏览器就是浏览器，墙自然放行。

所以本模块的第一原则是 **不自建浏览器，只接管浏览器**。

第二原则是 **人机分工**：8 步里机器干 7 步，真人只点图形验证码那 1 步。
凡是要「判断」的地方（点哪个花、认哪辆车）留给主人，其余全自动。
这和「强者走最正常的路」是一件事——最正常的路才不需要骗过谁。

进程模型（重要）
----------------
Playwright 的 sync API 不能跨线程/跨事件循环使用，而天网设备会被任意线程
调用。所以这里不维持长连接：**每个动作 fork 一个子进程**去 connect_over_cdp，
干完就退，浏览器进程本身不受影响（playwright 子进程退出=断开连接，不是关浏览器）。
好处：无状态、可并发、单步超时隔离、host 进程永不被卡住。

本文件既是库（给 register_device 用），也是 CLI：
    python -m fleet.browser_reg goto '{"url": "https://..."}'
"""
import base64
import json
import os
import glob
import re
import socket
import subprocess
import sys
import time
import urllib.request

FLEET_HOME = os.path.join(os.path.expanduser("~"), ".fleet")
BROWSER_DIR = os.path.join(FLEET_HOME, "browser")      # 每个端口一个 user-data-dir
COOKIE_DIR = os.path.join(FLEET_HOME, "cookies")       # 与 netforce/_cookies 同格式
STATE_PATH = os.path.join(BROWSER_DIR, "state.json")   # port -> {pid, profile, started}

# 「还没渲染完」的门槛。**不能拿来判墙**：example.com 只有 2KB，正常页面被
# 判成挑战壳（2026-11-02 踩过）。墙只由标题 + 挑战 DOM 决定，这里只管 loading。
_MIN_HTML = 800

# Chrome 内建错误页专有元素。**必须单独判**：错误页既没有墙、也不是正常内容，
# 只看 wall_state 会得出「没墙 = 过了」的假结论（2026-11-02 实测踩到）。
_ERROR_SELECTORS = ("#main-frame-error", "div.neterror", "#error-information-popup-content")


# ---------------------------------------------------------------- 代理探测

# 本机常见代理端口，按「越可能是规则代理越优先」排：规则代理能自动分流，
# 挂上去等于国内直连+国外走节点，比全局代理干净。
# 7891 排第一 —— 那是**天网自有**的 sing-box（proxycore.MIXED_PORT），
# 不是外部软件。自家内核起来了却不用，等于白起（2026-11-02 踩过）。
_PROXY_PORTS = (7891, 7890, 7897, 10808, 3067, 10809, 1080, 2080)


def detect_proxy():
    """找本机该用的代理。找不到返回 None（那就直连，如实标注）。

    **委托给 `net.resolve_proxy()`，这里不再自己实现一套选路。**

    2026-11-02 的教训：`net.py` 曾经「一律直连」，把天网自己起的 sing-box
    （127.0.0.1:7891）当僵尸配置；同一时刻浏览器层又有一套自己的扫端口逻辑。
    两套选路各说各话，就会出现「出向层不走代理、浏览器走了、或者反过来」的分叉。
    选路只该有一个来源。
    """
    try:
        from . import net
        url, _why = net.resolve_proxy()
        if url:
            return url
        return None                      # net 判定直连 —— 那就直连，别自作主张
    except Exception:
        pass
    # net 不可用时的兜底：按端口扫。7891 排第一，那是天网自有的内核。
    for port in _PROXY_PORTS:
        if _port_busy(port):
            return "http://127.0.0.1:%d" % port
    return None



# ---------------------------------------------------------------- 环境探测

def _candidates():
    """按「越像真人浏览器越优先」排列候选可执行文件。

    只收完整 chromium（chrome-win*/chrome.exe），headless_shell 目录天然被
    通配符排除——那个壳正是过不了墙的元凶，绝不能误选。
    """
    out = []
    local = os.environ.get("LOCALAPPDATA") or ""
    roots = []
    if local:
        roots.append(os.path.join(local, "ms-playwright"))
    # 本机双账户（21393 / 123），playwright 各自装各自的，都算可用
    roots.extend(glob.glob(r"C:\Users\*\AppData\Local\ms-playwright"))
    seen = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for exe in glob.glob(os.path.join(root, "chromium-*", "chrome-win*", "chrome.exe")):
            # 版本号大的排前面：新版本指纹更新，更像真的
            try:
                ver = int(os.path.basename(os.path.dirname(os.path.dirname(exe))).split("-")[-1])
            except Exception:
                ver = 0
            out.append((-ver, exe))
    out.sort()
    cands = [e for _, e in out]
    # 系统浏览器兜底：真 Chrome 比任何 chromium 都更「正常」
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.isfile(p):
            cands.append(p)
    uniq, seen = [], set()
    for c in cands:
        k = os.path.normcase(c)
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def find_chromium(exe=None):
    """返回可用的完整浏览器路径；找不到返回 None（调用方如实报 unavailable）。"""
    if exe and os.path.isfile(exe):
        return exe
    c = _candidates()
    return c[0] if c else None


def playwright_ok():
    try:
        import playwright.sync_api  # noqa: F401
        return True, ""
    except Exception as e:
        return False, "未安装 playwright：%s" % e


# ---------------------------------------------------------------- CDP 探活

def _get(url, timeout=3):
    req = urllib.request.Request(url, headers={"User-Agent": "skynet-fleet"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def cdp_alive(port, timeout=2.0):
    """调试端口在不在。返回 (alive, version_dict|None)。"""
    try:
        v = _get("http://127.0.0.1:%d/json/version" % int(port), timeout=timeout)
        return True, v
    except Exception:
        return False, None


def _port_busy(port):
    s = socket.socket()
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        s.close()


# ---------------------------------------------------------------- 状态簿记

def _load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(st):
    os.makedirs(BROWSER_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- 启 / 停

def launch(port=9222, proxy=None, profile=None, exe=None, timeout=40,
           width=1360, height=940, lang="zh-CN", reuse=True, headless=False):
    """启动一个**完整的有头 chromium** 并开好 CDP 端口。

    reuse=True 时若端口已是可用的 CDP 就复用（避免开出第二个窗口打架）。
    proxy=None 表示**自动探测**本机代理（不挂代理时 HF 直连是 ERR_CONNECTION_CLOSED，
    实测过）；要强制直连就传 proxy=""。
    headless=True 只用于「不需要过墙」的场景，过 Cloudflare 必失败。
    """
    port = int(port)
    proxy_from = ""
    if proxy is None:
        proxy = detect_proxy()
        proxy_from = "auto" if proxy else "无可用代理"
    elif proxy in ("", "none", "direct"):
        proxy = None
        proxy_from = "强制直连"

    if reuse and cdp_alive(port)[0]:
        return dict(ok=True, reused=True, port=port, pid=_state_pid(port),
                    exe=find_chromium(exe), proxy=proxy or "", proxy_from=proxy_from,
                    msg="端口 %d 已有可用 CDP，直接复用" % port)
    if _port_busy(port):
        return dict(ok=False, err="端口 %d 已被别的进程占用（不是可用 CDP），换端口" % port)

    exe = find_chromium(exe)
    if not exe:
        return dict(ok=False, err="找不到完整的 chromium/chrome（只有 headless_shell 时过不了墙）")

    profile = profile or os.path.join(BROWSER_DIR, "p%d" % port)
    os.makedirs(profile, exist_ok=True)
    args = [
        exe,
        "--remote-debugging-port=%d" % port,
        "--remote-allow-origins=*",          # 新版 CDP 不带这行，playwright 连接会被 403
        "--user-data-dir=%s" % profile,      # 必须独立目录，否则复用已跑实例、端口被忽略
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "--disable-blink-features=AutomationControlled",  # 去掉 navigator.webdriver
        "--lang=%s" % lang,
        "--window-size=%d,%d" % (width, height),
    ]
    if proxy:
        args.append("--proxy-server=%s" % proxy)
    if headless:
        args.append("--headless=new")

    creationflags = 0
    if os.name == "nt":
        creationflags = 0x00000008  # DETACHED_PROCESS：浏览器独立存活，不随父进程退出
    try:
        p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, creationflags=creationflags,
                             close_fds=True)
    except Exception as e:
        return dict(ok=False, err="启动浏览器失败：%s" % e)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp_alive(port, timeout=1.0)[0]:
            st = _load_state()
            st[str(port)] = {"pid": p.pid, "profile": profile, "exe": exe,
                             "started": int(time.time()), "proxy": proxy or "",
                             "proxy_from": proxy_from}
            _save_state(st)
            return dict(ok=True, reused=False, port=port, pid=p.pid, exe=exe,
                        profile=profile, proxy=proxy or "", proxy_from=proxy_from,
                        headless=bool(headless))
        rc = p.poll()
        # Windows 下 chrome.exe 是个启动器：fork 出真正的浏览器后它会立刻以 0 退出。
        # 所以 code==0 绝不能当失败（2026-11-02 实测把成功误判成"立即退出"）。
        if rc is not None and rc != 0:
            return dict(ok=False, err="浏览器启动失败（退出码 %s），多半是 profile 被别的实例占用" % rc)
        time.sleep(0.5)
    return dict(ok=False, err="等了 %ds CDP 端口还没起来%s" % (
        timeout, "（挂了代理 %s，可能是代理不通）" % proxy if proxy else "（没挂代理，外网可能连不上）"), pid=p.pid)


def _state_pid(port):
    return (_load_state().get(str(int(port))) or {}).get("pid")


def _pid_of_port(port):
    """从 netstat 反查监听该端口的 pid。

    为什么需要：Windows 下 Chrome 是「启动器 fork 后自退」，簿记里的 pid 可能
    不是真正持端口的那个；另外手动开的浏览器也没有簿记。关之前先问系统要真 pid。
    """
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             timeout=15).stdout.decode("utf-8", "replace")
    except Exception:
        return None
    needle = ":%d" % int(port)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3].upper() != "LISTENING":
            continue
        local = parts[1]
        if local.startswith("127.0.0.1") and local.endswith(needle):
            try:
                return int(parts[4])
            except Exception:
                continue
    return None


def _kill(pid):
    if not pid:
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=15)
        else:
            os.kill(pid, 15)
        return True
    except Exception:
        return False


def close(port=9222):
    """关掉注册浏览器。

    优先关自己簿记的 pid；没簿记但端口还活着（手动开的 / 启动器 pid 不对），
    就从 netstat 反查真 pid 再关——否则「关不掉」会一直占着端口，下次 open 直接失败。
    """
    port = int(port)
    st = _load_state()
    rec = st.get(str(port))
    alive = cdp_alive(port, timeout=1.0)[0]
    if not rec and not alive:
        return dict(ok=True, pid=None, msg="端口 %d 本来就没开" % port)

    # 两个 pid 都杀：簿记里那个是启动器（Windows 下 fork 完就退了，杀它没用），
    # 真正持端口的是 netstat 反查出来的那个。少杀一个就关不掉（2026-11-02 实测）。
    pid = (rec or {}).get("pid")
    real = _pid_of_port(port)
    killed = False
    for cand in {pid, real} - {None}:
        killed = _kill(cand) or killed
    if pid and real and pid != real:
        pid_from = "簿记 pid=%s + 端口 pid=%s" % (pid, real)
    elif pid:
        pid_from = "簿记 pid=%s" % pid
    else:
        pid_from = "netstat 反查 pid=%s" % real
    st.pop(str(port), None)
    _save_state(st)
    for _ in range(20):
        if not cdp_alive(port, timeout=0.8)[0]:
            return dict(ok=True, pid=pid, real_pid=real, pid_from=pid_from, killed=killed)
        time.sleep(0.5)
    return dict(ok=False, pid=pid, real_pid=real, pid_from=pid_from, killed=killed,
                err="已发 kill（pid %s），但 CDP 端口 %d 还在响" % (pid, port))


def list_open():
    """当前开着的注册浏览器（按 CDP 实际探活，不信簿记）。"""
    out = []
    for k, rec in (_load_state() or {}).items():
        port = int(k)
        alive, ver = cdp_alive(port, timeout=0.8)
        out.append(dict(port=port, alive=alive, pid=rec.get("pid"),
                        profile=rec.get("profile"), exe=rec.get("exe"),
                        browser=(ver or {}).get("Browser", "")))
    return out


# ---------------------------------------------------------------- 连接与页面

class Session(object):
    """一次子进程生命周期内的 CDP 会话。用完 close() 只断连接，不关浏览器。"""

    def __init__(self, port=9222):
        self.port = int(port)
        self._pw = None
        self.browser = None
        self.ctx = None
        self.page = None

    def __enter__(self):
        ok, why = playwright_ok()
        if not ok:
            raise RuntimeError(why)
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.connect_over_cdp("http://127.0.0.1:%d" % self.port)
        self.ctx = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.ua_fixed = self._fix_headless_ua()
        return self

    def _fix_headless_ua(self):
        """headless 模式的 UA 里带 "HeadlessChrome"，等于在门口挂块牌子写「我是机器人」。

        过墙本身靠有头（launch 的注释写了 headless 必败），但没理由再自曝一次。
        这里读浏览器**真实**的 UA，只把 HeadlessChrome 换成 Chrome 再覆盖回去 ——
        版本号跟着浏览器走，不写死。写死的版本号半年后就是新的破绽。

        注意：`Emulation.setUserAgentOverride` 才能改 navigator.userAgent，
        `Network.setUserAgentOverride` 只改请求头，页面脚本照样读得到 Headless。
        """
        try:
            ua = self.page.evaluate("() => navigator.userAgent")
            if not ua or "Headless" not in ua:
                return False
            fixed = ua.replace("HeadlessChrome", "Chrome").replace("Headless", "")
            cdp = self.ctx.new_cdp_session(self.page)
            cdp.send("Emulation.setUserAgentOverride", {"userAgent": fixed})
            # **不能 detach**：覆盖是绑在这个 CDP 会话上的，一断开就还原
            # （实测过：detach 之后 navigator.userAgent 又变回 HeadlessChrome）。
            self._ua_cdp = cdp
            return True
        except Exception:
            return False                    # 改不了也不能让会话起不来

    def __exit__(self, *exc):
        # 只断开：绝不调 browser.close()，那是真关浏览器
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        return False


# ---------------------------------------------------------------- 墙检测

_CHALLENGE_SELECTORS = (
    "#challenge-form",
    "#cf-challenge-running",
    "#challenge-running",
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[src*='challenge-platform']",
    "div.cf-turnstile",
)

_BLOCK_TITLES = ("just a moment", "attention required", "请稍候", "安全检查", "ddos-guard")

_RE_ERR = re.compile(r"ERR_[A-Z_]{4,40}")


# ---------------------------------------------------------------- 人机验证识别

# 厂商识别表。判据**全部是专用结构**：iframe 域名片段 + 专有 class/id。
# 绝不认正文关键词 —— 2026-11-02 那次 HF 页面 JS 里到处是 verify/checked，
# 一判一个假通过。结构是客观的，文案是可变的。
#
# 每项：(vendor, kind, iframe 域名片段, 主文档选择器)
#   kind 决定「怎么打」：
#     slider         滑块 → slider.detect/track/drag 直接上
#     click          点选 → 要跑厂商官方 JS 或人工，滑块能力打不了
#     invisible      无感 → 后台打分，**没有"解"只有"养"**
#     js_challenge   JS 挑战 → 只能有头浏览器等它自己过
_VERIFY_RULES = (
    # Cloudflare 整页挑战排在第一位：它是拦截性的，别的判定都在它之后才轮得到。
    # **这一条只能靠主文档 DOM，不能靠 iframe 域名**：Turnstile 的 iframe 也在
    # challenges.cloudflare.com 下、路径同样含 challenge-platform，靠 iframe 分不开，
    # 会把「一个嵌了 Turnstile 的正常页面」误判成整页被拦（实测踩到）。
    ("cloudflare_challenge", "js_challenge",
     (),
     ("#challenge-form", "#cf-challenge-running", "#challenge-running", "#challenge-stage")),
    ("cloudflare_turnstile", "invisible",
     ("challenges.cloudflare.com",),
     ("div.cf-turnstile", "#cf-turnstile")),
    ("geetest", "slider",
     ("geetest.com", "gcaptcha4.geetest.com", "static.geetest.com"),
     (".geetest_holder", ".geetest_panel", ".geetest_slider", ".geetest_box",
      "div[class^='geetest_']")),
    ("tencent", "slider",
     ("captcha.qq.com", "turing.captcha.qcloud.com", "t.captcha.qq.com"),
     (".tcaptcha-transform", "#tcaptcha_iframe", ".tcaptcha-body", ".tcaptcha-popup-wrap")),
    ("aliyun", "slider",
     # 别用 "aliyun.com" —— 满站都是 aliyun CDN，会把正常页面全判成有墙
     ("aliyunwaf", "cf.aliyun.com/nocaptcha", "baxia"),
     ("#nc_1_wrapper", ".nc-container", "#nocaptcha", ".nc_scale", "#baxia-dialog-content")),
    ("hcaptcha", "click",
     ("hcaptcha.com",),
     (".h-captcha", "#hcaptcha")),
    ("recaptcha", "click",
     ("recaptcha/api2", "recaptcha/enterprise", "google.com/recaptcha"),
     (".g-recaptcha",)),
)

_KIND_ADVICE = {
    "slider": "滑块：用 slider.detect 算缺口 + slider.drag 拖过去"
              "（若墙在 iframe 里，先做坐标换算）",
    "click": "点选：需要跑厂商官方 JS 或人工点选，滑块能力打不了这一类",
    "invisible": "无感打分：没有可解的东西，只能靠环境与行为「养」，"
                 "别指望一次通过 —— 这是能力边界，不是失败",
    "js_challenge": "JS 挑战：只能有头浏览器等它自己过（headless 必败），"
                    "等 wait_pass 连续稳定",
}


def verify_state(page, max_frames=40):
    """认出页面上有没有人机验证、是哪一家、什么类型、该怎么打。

    为什么要分类而不是只回一个 blocked：**不同类型走完全不同的解法**。
    只回 blocked=True，调用方还得再猜一遍自己面对的是什么，等于把问题往后推。

    返回：
        found    bool     有没有认出来
        vendor   str      哪一家（认不出为 ""）
        kind     str      slider / click / invisible / js_challenge / ""
        advice   str      下一步该干什么（能直接照做的一句话）
        signals  [str]    命中的具体证据（选择器 / iframe src 片段），可核查
        matches  [str]    全部命中的 vendor（一个页面可能既有整页挑战又有小组件）
        frames   [str]    页面上所有 iframe 的 src —— 穿透 iframe 要用（P3）
    """
    frames = []
    try:
        for el in (page.query_selector_all("iframe") or [])[:max_frames]:
            try:
                src = el.get_attribute("src") or ""
                if src:
                    frames.append(src)
            except Exception:
                continue
    except Exception:
        pass

    blob = " ".join(frames).lower()
    matches, signals = [], []
    vendor, kind, advice = "", "", ""

    for vd, kd, src_marks, sels in _VERIFY_RULES:
        hit = ""
        for m in src_marks:
            if m.lower() in blob:
                hit = "iframe:%s" % m
                break
        if not hit:
            for sel in sels:
                try:
                    if page.query_selector(sel):
                        hit = "dom:%s" % sel
                        break
                except Exception:
                    continue
        if hit:
            matches.append(vd)
            signals.append("%s → %s" % (vd, hit))
            if not vendor:                     # 规则顺序即优先级
                vendor, kind = vd, kd
                advice = _KIND_ADVICE.get(kd, "")

    return dict(found=bool(vendor), vendor=vendor, kind=kind, advice=advice,
                signals=signals, matches=matches, frames=frames)


# ---------------------------------------------------------------- 滑块元素定位（含 iframe）

# 各家的滑块零件选择器。顺序就是试的顺序：先精确的，后兜底的。
# 为什么需要这张表：`slider.detect` 要两张图（背景 + 滑块块），
# 而这两张图长得完全取决于厂商，没有通用选择器。
_SLIDER_HINTS = {
    "geetest": {
        # v3 用 canvas 画，v4 用 div 背景图；两代都要覆盖
        "bg": (".geetest_canvas_bg", "canvas.geetest_canvas_bg", ".geetest_bg"),
        "slice": (".geetest_canvas_slice", "canvas.geetest_canvas_slice", ".geetest_slice"),
        "handle": (".geetest_slider_button", ".geetest_btn", ".geetest_slider_btn"),
    },
    "tencent": {
        "bg": (".tcaptcha-bg", "#tcaptcha-bg", ".tcaptcha-normal-bg"),
        "slice": (),
        "handle": (".tcaptcha-slider-btn", ".tcaptcha-slider", "#tcaptcha-slider"),
    },
    "aliyun": {
        "bg": ("#nc_1__bg", ".nc-lang-cnt", ".nc_scale"),
        "slice": (),
        "handle": ("#nc_1_n1z", ".nc_iconfont.btn_slide", ".nc-slide-block"),
    },
}


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _frames_of(page):
    """主 frame 排第一，其余 iframe 按出现顺序。去重靠对象身份。"""
    out = []
    main = _safe(lambda: page.main_frame)
    if main is not None:
        out.append(main)
    for f in (_safe(lambda: list(page.frames), []) or []):
        if f is not main:
            out.append(f)
    return out


def _elem_box(el):
    b = _safe(lambda: el.bounding_box())
    if not b:
        return None
    return {"x": round(float(b["x"]), 2), "y": round(float(b["y"]), 2),
            "w": round(float(b["width"]), 2), "h": round(float(b["height"]), 2)}


def locate_slider(page, vendor=None, want_png=False):
    """在页面（**含跨域 iframe**）里找到滑块的背景图、缺口图、拖动把手。

    为什么必须支持 iframe：极验 v4、腾讯 TCaptcha 的滑块层都在跨域 iframe 里。
    只查主文档会得出「页面上没有滑块」的假结论 —— 而它其实就长在那儿，只隔了一层。
    这是 P3，也是 P1 的能力真正能用起来的前提。

    want_png=True 时顺带把两张图截成 base64 返回，可以直接喂给 `slider.detect`。
    用 base64 而不是 PIL 对象，是为了让结果能被 JSON 序列化（跨进程、进日志都能活）。

    返回里 `in_iframe` 会明确告诉你坐标是哪套坐标系：
      · False → 主文档的坐标，可直接用
      · True  → playwright 的 bounding_box 已经换算到主 frame 视口，也能直接用
                 （这一点实测过：ElementHandle.bounding_box 返回相对主 frame 的坐标，
                  不需要自己加 iframe 偏移。自己再加一次就错位了。）
    """
    vendor = vendor or ""
    hints = _SLIDER_HINTS.get(vendor)
    if not hints:
        # 没指定厂商就按表里全部试一遍
        merged = {"bg": (), "slice": (), "handle": ()}
        for h in _SLIDER_HINTS.values():
            for k in merged:
                merged[k] = tuple(merged[k]) + tuple(h.get(k, ()))
        hints = merged

    for idx, frame in enumerate(_frames_of(page)):
        found = {}
        for role in ("bg", "slice", "handle"):
            for sel in hints.get(role, ()):
                el = _safe(lambda: frame.query_selector(sel))
                if el is None:
                    continue
                box = _elem_box(el)
                if not box or box["w"] <= 0 or box["h"] <= 0:
                    continue
                found[role] = {"selector": sel, "box": box}
                if want_png:
                    raw = _safe(lambda: el.screenshot())
                    if raw:
                        found[role]["png_b64"] = base64.b64encode(raw).decode("ascii")
                break
        if found.get("bg") or found.get("handle"):
            return {
                "ok": True,
                "vendor": vendor or "(auto)",
                "in_iframe": idx > 0,
                "frame_url": (_safe(lambda: frame.url, "") or "")[:200],
                "bg": found.get("bg"),
                "slice": found.get("slice"),
                "handle": found.get("handle"),
                "hint": ("在 iframe 内找到（bounding_box 已是主 frame 视口坐标，"
                         "不要再自己加偏移）") if idx > 0 else "在主文档找到",
            }

    return {"ok": False, "vendor": vendor or "(auto)",
            "err": "没找到滑块元素（遍历了 %d 个 frame）" % len(_frames_of(page)),
            "tried": {k: list(v) for k, v in hints.items()}}




def wall_state(page):
    """判断当前页的状态：有没有人机验证墙 / 是不是个错误页 / 正常。

    三条判据，都是**客观结构**，不碰正文关键词：
      · 墙   → 标题命中挑战页标题，或存在挑战专用 DOM
      · 错误 → Chrome 内建错误页元素，或正文里出现 ERR_XXXX
      · 过薄 → 页面不到 _MIN_HTML，通常还是挑战壳

    `error` 必须单独判：错误页既没有墙、也没有真内容，只看 blocked 会得出
    「没墙 = 过了」的假结论（2026-11-02 把 ERR_CONNECTION_CLOSED 页判成通过）。
    """
    url, title, html_len, body = "", "", 0, ""
    try:
        url = page.url or ""
    except Exception:
        pass
    try:
        title = (page.title() or "")
    except Exception:
        pass
    try:
        body = page.content() or ""
        html_len = len(body)
    except Exception:
        pass

    low = title.lower()
    blocked, reason = False, ""
    for t in _BLOCK_TITLES:
        if t in low:
            blocked, reason = True, "标题命中：%s" % title.strip()[:60]
            break
    if not blocked:
        for sel in _CHALLENGE_SELECTORS:
            try:
                if page.query_selector(sel):
                    blocked, reason = True, "挑战元素：%s" % sel
                    break
            except Exception:
                continue

    # 厂商级识别。这一步是 2026-11-02 盘点补上的：原来只认 Cloudflare 的 DOM，
    # 遇到极验/腾讯/阿里的滑块层会判成「没墙」—— 等于闭着眼撞上去，还白白报了「通过」。
    verify = verify_state(page)
    if verify["found"] and not blocked:
        blocked = True
        reason = "人机验证 %s（%s）" % (verify["vendor"], verify["signals"][0])

    # 「还没渲染完」独立成一态：Chromium 导航途中会挂一个 "Loading <url>" 的壳，
    # 页面确实薄，但那不是墙，等一等就好。
    loading = html_len < _MIN_HTML or low.startswith("loading ")
    if loading and not blocked:
        reason = "页面还没渲染完（%d 字节，标题 %r）" % (html_len, title.strip()[:40])

    # 错误页优先于「过薄」：两者都像「没内容」，但错误页是要立刻报错的，
    # 不该让人干等一次 180 秒超时。
    err = ""
    if body:
        m = _RE_ERR.search(body)
        if m:
            err = m.group(0)
    if not err:
        for sel in _ERROR_SELECTORS:
            try:
                if page.query_selector(sel):
                    err = "chrome_error_page"
                    break
            except Exception:
                continue
    if err:
        blocked = False
        reason = "页面是错误页：%s" % err

    return dict(blocked=blocked, reason=reason, url=url, error=err, loading=loading,
                title=title.strip()[:120], html_len=html_len, verify=verify)


def wait_pass(page, timeout=180, poll=2.5, need_stable=2):
    """等到「没有墙 + 已渲染完」。连续 need_stable 次才算过——防瞬时抖动。

    一旦发现是错误页就**立刻返回**，不浪费人 180 秒。
    """
    t0 = time.time()
    stable = 0
    st = {"blocked": True, "reason": "未检测", "url": "", "title": "",
          "html_len": 0, "error": "", "loading": True}
    while time.time() - t0 < timeout:
        st = wall_state(page)
        if st.get("error"):
            st["secs"] = round(time.time() - t0, 1)
            st["passed"] = False
            return st
        if not st["blocked"] and not st.get("loading"):
            stable += 1
            if stable >= need_stable:
                st["secs"] = round(time.time() - t0, 1)
                st["passed"] = True
                return st
        else:
            stable = 0
        page.wait_for_timeout(int(poll * 1000))
    st["secs"] = round(time.time() - t0, 1)
    st["passed"] = False
    return st


# ---------------------------------------------------------------- 表单

def read_form(page):
    """读页面上的输入框与按钮，供「机器填/人确认」用。"""
    form = {"url": page.url, "title": "", "inputs": [], "buttons": [], "links": []}
    try:
        form["title"] = (page.title() or "").strip()[:120]
    except Exception:
        pass
    try:
        for inp in page.query_selector_all("input, textarea, select"):
            try:
                labels = []
                iid = inp.get_attribute("id")
                if iid:
                    for lb in page.query_selector_all("label[for='%s']" % iid):
                        labels.append((lb.inner_text() or "").strip()[:40])
                form["inputs"].append(dict(
                    tag=inp.evaluate("e => e.tagName.toLowerCase()"),
                    name=inp.get_attribute("name") or "",
                    id=iid or "",
                    type=inp.get_attribute("type") or "text",
                    placeholder=inp.get_attribute("placeholder") or "",
                    autocomplete=inp.get_attribute("autocomplete") or "",
                    label=" ".join([x for x in labels if x])[:60],
                    required=bool(inp.get_attribute("required") is not None),
                ))
            except Exception:
                continue
        for btn in page.query_selector_all("button, input[type=submit], a[role=button]"):
            try:
                txt = (btn.inner_text() or "").strip()[:40]
                if not txt:
                    txt = btn.get_attribute("value") or ""
                form["buttons"].append(dict(text=txt, type=btn.get_attribute("type") or "",
                                            name=btn.get_attribute("name") or ""))
            except Exception:
                continue
    except Exception:
        pass
    return form


def _selector_for(page, key):
    """把一个「字段名」翻译成能点中的选择器。从精确到宽松依次试。"""
    safe = key.replace('"', '\\"')
    tries = [
        'input[name="%s"]' % safe, 'textarea[name="%s"]' % safe, 'select[name="%s"]' % safe,
        'input[id="%s"]' % safe, 'textarea[id="%s"]' % safe,
        'input[placeholder="%s"]' % safe, 'input[autocomplete="%s"]' % safe,
        'input[aria-label="%s"]' % safe,
    ]
    for sel in tries:
        try:
            el = page.query_selector(sel)
            if el:
                return sel
        except Exception:
            continue
    # 最后按 label 文本反查
    try:
        for lb in page.query_selector_all("label"):
            try:
                if (lb.inner_text() or "").strip().lower().startswith(key.lower()):
                    fid = lb.get_attribute("for")
                    if fid:
                        return '[id="%s"]' % fid.replace('"', '\\"')
                    inner = lb.query_selector("input, textarea")
                    if inner:
                        return None  # 嵌套型：交给调用方按索引处理，这里不猜
            except Exception:
                continue
    except Exception:
        pass
    return None


def fill(page, fields, only_empty=False):
    """按字段名填表。fields 形如 {"email": "...", "password": "..."}。

    返回每个字段的落位结果——**填没填上必须知道**，静默失败比报错更坏。
    """
    done, missed = {}, []
    for key, val in (fields or {}).items():
        sel = _selector_for(page, key)
        if not sel:
            missed.append(dict(field=key, why="找不到对应输入框"))
            continue
        try:
            el = page.query_selector(sel)
            typ = (el.get_attribute("type") or "text").lower() if el else "text"
            if only_empty and el is not None and (el.input_value() or "").strip():
                done[key] = "skipped(已有内容)"
                continue
            if typ in ("checkbox", "radio"):
                if str(val).lower() in ("1", "true", "yes", "on") or val is True:
                    el.check()
                done[key] = "checked" if typ == "checkbox" else "radio"
            elif typ == "file":
                el.set_input_files(str(val))
                done[key] = "file"
            else:
                page.fill(sel, str(val))
                done[key] = "filled"
        except Exception as e:
            missed.append(dict(field=key, why=str(e)[:120]))
    return dict(ok=not missed, filled=done, missed=missed)


def submit(page, text=None, press_enter=False):
    """点提交。text 给了就按文本找按钮；没给就找 submit 按钮。"""
    if press_enter:
        try:
            page.keyboard.press("Enter")
            return dict(ok=True, how="enter")
        except Exception as e:
            return dict(ok=False, err="回车失败：%s" % e)
    if text:
        for sel in ('button:has-text("%s")', 'input[value="%s"]', 'a:has-text("%s")'):
            try:
                el = page.query_selector(sel % text.replace('"', '\\"'))
                if el:
                    el.click()
                    return dict(ok=True, how="text", selector=sel % text)
            except Exception:
                continue
        return dict(ok=False, err="没找到文本含「%s」的按钮" % text)
    for sel in ('button[type=submit]', 'input[type=submit]', 'form button'):
        try:
            el = page.query_selector(sel)
            if el:
                el.click()
                return dict(ok=True, how="submit", selector=sel)
        except Exception:
            continue
    return dict(ok=False, err="页面上没有 submit 按钮，传 text 指定按钮文本")


def click(page, text=None, selector=None):
    """泛用点击：按文本或选择器。给人机交接留的口子（比如「开始」按钮）。"""
    if selector:
        try:
            page.click(selector, timeout=8000)
            return dict(ok=True, how="selector", selector=selector)
        except Exception as e:
            return dict(ok=False, err=str(e)[:150])
    if text:
        for sel in ('button:has-text("%s")', 'a:has-text("%s")', 'div[role=button]:has-text("%s")'):
            try:
                el = page.query_selector(sel % text.replace('"', '\\"'))
                if el:
                    el.click()
                    return dict(ok=True, how="text", text=text)
            except Exception:
                continue
        return dict(ok=False, err="没找到含「%s」的可点元素" % text)
    return dict(ok=False, err="text 和 selector 至少给一个")


# ---------------------------------------------------------------- 会话落盘

def save_session(ctx, page=None, host=None, extra=None):
    """把 Cookie 落到 ~/.fleet/cookies/<host>.json。

    格式与 netforce/_cookies 对齐（name=value 扁平表，curl_cffi 可直接回灌），
    另外保留一份完整字段（domain/expiry）给需要精确还原的场景。
    """
    url = ""
    if page is not None:
        try:
            url = page.url or ""
        except Exception:
            pass
    if not host:
        try:
            from urllib.parse import urlparse
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
    if not host:
        return dict(ok=False, err="不知道往哪个域存（host 和 page.url 都空）")

    try:
        cookies = ctx.cookies()
    except Exception as e:
        return dict(ok=False, err="读 Cookie 失败：%s" % e)

    def _match(c):
        d = (c.get("domain") or "").lstrip(".").lower()
        return d == host or d.endswith("." + host) or host.endswith("." + d)

    picked = [c for c in cookies if _match(c)]
    flat = {c["name"]: c["value"] for c in picked}
    os.makedirs(COOKIE_DIR, exist_ok=True)
    path = os.path.join(COOKIE_DIR, host + ".json")
    rec = {
        "host": host,
        "saved_at": int(time.time()),
        "url": url,
        "cookies": flat,
        "cookies_full": [{k: c.get(k) for k in
                          ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")}
                         for c in picked],
    }
    if extra:
        rec.update(extra)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return dict(ok=True, host=host, path=path, count=len(flat), names=sorted(flat.keys())[:40])


def load_session(host):
    path = os.path.join(COOKIE_DIR, host + ".json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------- CLI 入口

_VERBS = {}


def _verb(name):
    def deco(fn):
        _VERBS[name] = fn
        return fn
    return deco


@_verb("open")
def _v_open(a):
    return launch(port=a.get("port") or 9222, proxy=a.get("proxy"),
                  profile=a.get("profile"), exe=a.get("exe"),
                  headless=bool(a.get("headless")), reuse=a.get("reuse", True))


@_verb("close")
def _v_close(a):
    return close(port=a.get("port") or 9222)


@_verb("list")
def _v_list(a):
    return dict(ok=True, browsers=list_open())


@_verb("goto")
def _v_goto(a):
    with Session(a.get("port") or 9222) as s:
        url = a.get("url")
        if not url:
            return dict(ok=False, err="url 必填")
        s.page.goto(url, timeout=int(a.get("timeout") or 60000),
                    wait_until=a.get("wait_until") or "domcontentloaded")
        if a.get("settle"):
            s.page.wait_for_timeout(int(a["settle"]) * 1000)
        st = wall_state(s.page)
        st["ok"] = True
        return st


@_verb("wait_pass")
def _v_wait(a):
    with Session(a.get("port") or 9222) as s:
        st = wait_pass(s.page, timeout=float(a.get("timeout") or 180),
                       poll=float(a.get("poll") or 2.5))
        st["ok"] = bool(st.get("passed"))
        return st


@_verb("inspect")
def _v_inspect(a):
    with Session(a.get("port") or 9222) as s:
        st = wall_state(s.page)
        st.update(read_form(s.page))
        st["ok"] = True
        return st


@_verb("fill")
def _v_fill(a):
    with Session(a.get("port") or 9222) as s:
        r = fill(s.page, a.get("fields") or {}, only_empty=bool(a.get("only_empty")))
        if a.get("then_submit"):
            r["submit"] = submit(s.page, text=a.get("submit_text"),
                                 press_enter=bool(a.get("press_enter")))
        return r


@_verb("submit")
def _v_submit(a):
    with Session(a.get("port") or 9222) as s:
        return submit(s.page, text=a.get("text"), press_enter=bool(a.get("press_enter")))


@_verb("click")
def _v_click(a):
    with Session(a.get("port") or 9222) as s:
        return click(s.page, text=a.get("text"), selector=a.get("selector"))


@_verb("save")
def _v_save(a):
    with Session(a.get("port") or 9222) as s:
        return save_session(s.ctx, s.page, host=a.get("host"), extra=a.get("extra"))


@_verb("shot")
def _v_shot(a):
    with Session(a.get("port") or 9222) as s:
        path = a.get("path") or os.path.join(BROWSER_DIR, "shot_%d.png" % int(time.time()))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        s.page.screenshot(path=path, full_page=bool(a.get("full")))
        return dict(ok=True, path=path, url=s.page.url)


def run_verb(verb, payload=None, timeout=180):
    """在**子进程**里跑一个动作，返回它的 JSON 结果。

    子进程隔离是刻意的：playwright 的 sync API 在别的线程/事件循环里会炸，
    而天网设备会被任意线程调。分出去以后，单步超时、崩了也不牵连 host。
    """
    payload = payload or {}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        p = subprocess.run([sys.executable, "-m", "fleet.browser_reg", verb],
                           input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           cwd=root, env=env, timeout=timeout,
                           creationflags=(0x08000000 if os.name == "nt" else 0))  # CREATE_NO_WINDOW
    except subprocess.TimeoutExpired:
        return dict(ok=False, err="动作 %s 超时（%ss）" % (verb, timeout), timeout=True)
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    err = (p.stderr or b"").decode("utf-8", "replace").strip()
    if not out:
        return dict(ok=False, err="子进程无输出（code %s）：%s" % (p.returncode, err[-400:]))
    try:
        r = json.loads(out.splitlines()[-1])
    except Exception:
        return dict(ok=False, err="子进程输出不是 JSON：%s" % out[-400:])
    if p.returncode != 0 and r.get("ok"):
        r["ok"] = False
        r["err"] = r.get("err") or "子进程退出码 %s" % p.returncode
    return r


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("verbs:", ", ".join(sorted(_VERBS)))
        return 0
    verb = argv[0]
    raw = sys.stdin.buffer.read().decode("utf-8", "replace") if len(sys.argv) <= 2 else "{}"
    payload = {}
    if len(argv) > 1:
        try:
            payload = json.loads(argv[1])
        except Exception:
            payload = {}
    elif raw.strip():
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
    fn = _VERBS.get(verb)
    if fn is None:
        print(json.dumps({"ok": False, "err": "未知动作 %s" % verb}, ensure_ascii=False))
        return 2
    try:
        r = fn(payload)
    except Exception as e:
        r = {"ok": False, "err": "%s: %s" % (type(e).__name__, e)}
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

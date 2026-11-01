# -*- coding: utf-8 -*-
"""天网 · 注册能力（伪装登录第二级「会话层」）
=================================================
第一级「邮箱层」（email.*）解决「拿什么地址注册」；这一级解决「怎么把表单填完
并通过人机验证」。两层分开是为了可续：邮箱换了不影响浏览器，浏览器换了不影响邮箱。

实战来源（2026-11-02 HF 注册）得到的三条硬结论，这里全部固化：

  1. **headless 过不了 Cloudflare，有头能过。**
     不是技术问题，是「像不像人」的问题。Playwright 自 launch 会塞自动化参数，
     墙一眼识破；自己起完整 chromium 再接管，就放行。

  2. **人机分工 7:1。**
     8 步里机器干 7 步（开浏览器/导航/填表/提交/收信/提码/落 Cookie），
     真人只点图形验证码那 1 步。凡是「要判断」的（认哪朵花、点哪辆车）留给人，
     凡是「照做」的全部自动。这是效率最高、也最不容易被判定为批量作业的分法。

  3. **判「过没过墙」不能用正文关键词。**
     HF 那次页面 JS 里全是 verify，一判一个假通过。只认标题和挑战专用 DOM。

优先级（2026-11-02 03:33 更新）
------------------------------
主人装了 WorkBuddy Chrome 扩展后，最优通道变成了「MCP 直控真 Chrome」：
真指纹、真 IP、还能接管已登录标签页。但那套只在 WorkBuddy 会话内可用，
天网是独立进程调不到，所以本模块这条自建腿必须保留，定位 = 兜底 + 无人值守。

能力族 register.*（9 个）：

  register.open         -> 起一个有头 chromium（可带代理），开 CDP 端口
  register.close        -> 关掉（只认自己开的 pid，不误杀别人浏览器）
  register.list         -> 当前开着哪几个（按 CDP 实际探活，不信簿记）
  register.goto         -> 导航，回来时附带「墙的状态」
  register.inspect      -> 读当前页：标题/URL/墙/表单字段/按钮
  register.fill         -> 按字段名填表（填没填上逐项报）
  register.submit       -> 点提交（按按钮文本或 submit）
  register.click        -> 泛用点击（人机交接的口子）
  register.await_pass   -> 等人机验证通过（人点验证码，机器盯结果）
  register.save_session -> Cookie 落盘到 ~/.fleet/cookies/<host>.json

授权边界：仅用于「本人账号 / 已授权流程」的自动化注册登录。
"""
from .core import Capability, Param


def _check_engine():
    """引擎在线 = playwright 可导入 + 本机有完整 chromium（不是只有 headless_shell）。"""
    def _chk(reg, dev):
        try:
            from . import browser_reg as B
        except Exception as e:
            return False, "浏览器注册引擎不可用：%s" % e
        ok, why = B.playwright_ok()
        if not ok:
            return False, why
        exe = B.find_chromium()
        if not exe:
            return False, "找不到完整 chromium/chrome（headless_shell 过不了 Cloudflare）"
        return True, "引擎在线：%s" % exe
    return _chk


_REQUIRED = ("这个能力要先 register.open 起一个浏览器（或用 list 看现有的端口）",)


REGISTER_CAPABILITIES = [
    Capability("register.open",
               "起一个**有头**的完整 chromium 并开 CDP 端口（headless 过不了 Cloudflare，所以默认有头）",
               params=(Param("port", "int", doc="调试端口，默认 9222"),
                       Param("proxy", "str", doc="代理，如 http://127.0.0.1:7890；不传=直连"),
                       Param("profile", "str", doc="user-data-dir，默认 ~/.fleet/browser/p<port>"),
                       Param("exe", "str", doc="指定浏览器可执行文件；不传=自动找 chromium/chrome"),
                       Param("width", "int", doc="窗口宽，默认 1360"),
                       Param("height", "int", doc="窗口高，默认 940")),
               side_effect=True, check=_check_engine(),
               note="[REG] 有副作用：起一个可见的浏览器窗口 + 在 user-data-dir 落盘。过墙必需有头"),

    Capability("register.close",
               "关掉本设备起过的浏览器（只认自己簿记的 pid，不误杀你自己开的浏览器）",
               params=(Param("port", "int", doc="要关的端口，默认 9222"),),
               side_effect=True, check=_check_engine(),
               note="[REG] 有副作用：taskkill 自己起的浏览器进程树"),

    Capability("register.list",
               "列当前开着的注册浏览器（按 CDP 端口实际探活，不信簿记）",
               check=_check_engine(),
               note="[REG] 只读：CDP 探活") ,

    Capability("register.goto",
               "导航到目标页，返回时附带「有没有被人机验证墙拦住」的判断",
               params=(Param("url", "str", True, "目标地址"),
                       Param("port", "int", doc="调试端口，默认 9222"),
                       Param("timeout", "int", doc="导航超时毫秒，默认 60000"),
                       Param("wait_until", "str", doc="load / domcontentloaded / networkidle"),
                       Param("settle", "int", doc="导航后再等几秒（给 SPA 渲染）")),
               side_effect=False, check=_check_engine(),
               note="[REG] 会发一次真实 HTTP 导航；不点任何东西"),

    Capability("register.inspect",
               "读当前页状态：标题/URL/是否卡在验证墙/所有输入框与按钮",
               params=(Param("port", "int", doc="调试端口，默认 9222"),),
               side_effect=False, check=_check_engine(),
               note="[REG] 只读：读 DOM，不点击不输入"),

    Capability("register.fill",
               "按字段名填表（email/password/username 等），逐项回报填没填上",
               params=(Param("fields", "json", True, '字段表，如 {"email":"a@b.c","password":"..."}'),
                       Param("port", "int", doc="调试端口，默认 9222"),
                       Param("only_empty", "bool", doc="只填空白框，已有人工内容不动"),
                       Param("then_submit", "bool", doc="填完顺手提交"),
                       Param("submit_text", "str", doc="提交按钮文本；不传=找 submit 按钮"),
                       Param("press_enter", "bool", doc="用回车代替点按钮")),
               side_effect=True, check=_check_engine(),
               note="[REG] 有副作用：向页面输入内容（可能触发前端校验）"),

    Capability("register.submit",
               "点提交按钮（按文本找，或找 form 里的 submit）",
               params=(Param("port", "int", doc="调试端口，默认 9222"),
                       Param("text", "str", doc="按钮文本，如「Next」「注册」"),
                       Param("press_enter", "bool", doc="用回车提交")),
               side_effect=True, check=_check_engine(),
               note="[REG] 有副作用：点按钮，可能真的提交表单"),

    Capability("register.click",
               "泛用点击：按文本或 CSS 选择器点（人机交接用，如点「开始」把验证码交给人）",
               params=(Param("port", "int", doc="调试端口，默认 9222"),
                       Param("text", "str", doc="元素文本"),
                       Param("selector", "str", doc="CSS 选择器（优先级高于 text）")),
               side_effect=True, check=_check_engine(),
               note="[REG] 有副作用：真实点击"),

    Capability("register.await_pass",
               "等人机验证通过：真人去点图形验证码，机器盯「墙没了」这个客观结果（不猜关键词）",
               params=(Param("port", "int", doc="调试端口，默认 9222"),
                       Param("timeout", "int", doc="最多等几秒，默认 180"),
                       Param("poll", "int", doc="轮询间隔秒，默认 2.5")),
               side_effect=False, check=_check_engine(),
               note="[REG] 只读等待：把「点验证码」这一步留给人，机器只负责判结果"),

    Capability("register.save_session",
               "把 Cookie 落到 ~/.fleet/cookies/<host>.json（格式与 netforce 对齐，可回灌 curl_cffi）",
               params=(Param("port", "int", doc="调试端口，默认 9222"),
                       Param("host", "str", doc="目标域；不传=取当前页 host"),
                       Param("extra", "json", doc="额外要存进档的字段（如 username）")),
               side_effect=True, check=_check_engine(),
               note="[REG] 有副作用：把会话写进本地文件（含 Cookie，敏感）"),
]

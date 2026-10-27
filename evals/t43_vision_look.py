"""视觉门禁：锁死「她真的看见了」这条链，而不是「有个截图文件躺在临时目录」。

背景（2026-10-28 核出来的断头）：仓库里四个取图能力
（`pcctl.screenshot` / `adb.wifi.screenshot` / `screen.read` / `phone.see`）
**全部止步于「存成 PNG」**。文件给谁看？给人看。模型一个字都读不到——
`screen.read` 回的是前台应用名 + 最多 40 个 UI 文字标签，不是画面。
`pcctl.screenshot` 回 path + bytes，然后就没有然后了。

这道门禁锁六件事（全离线，**一次真实模型调用都不发**）：

  ① `need="vision"` 真的传到了 providers——不是写了个参数没人理；
  ② 图真的被塞进 messages 的 `image_url`（data URI），不是只放了个路径；
  ③ 模型回围栏 JSON 能解析；回不出 JSON 时落 `raw`，**不谎报成功也不当失败**；
  ④ 四种失败可分辨（capture / prepare / encode / ask）——糊成一句「看不了」
     等于让人从零排查，这是项目反复踩的坑；
  ⑤ 两条能力注册成 `approval="ask"`（画面出本机，不是只读）+ 无闸门必拒；
  ⑥ 工具清单措辞互相指路（问「屏幕上写的什么」要能选到 look，不是 screenshot）。

为什么不用真模型验：门禁结论不能随网速和免费额度变。模型那一段用假 chat 验，
真模型的质量另说——本条只保证「接线是对的」。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


from fleet import vision as V
from fleet import providers as P

# ---------------------------------------------------------------- 工具：造真 PNG
# 不依赖 Pillow：imgscale 自己就会写 PNG，用它造图，门禁在任何机器上都能跑。
from fleet import imgscale

d = tempfile.mkdtemp()


def mk_png(name, w, h, ch=3):
    path = os.path.join(d, name)
    px = bytearray(w * h * ch)
    for y in range(0, h, max(1, h // 20)):          # 画几条横纹，别让整图纯色
        for x in range(w):
            off = (y * w + x) * ch
            for c in range(ch):
                px[off + c] = (x + y * 7 + c * 53) % 256
    imgscale.write_png(path, w, h, ch, px)
    return path


# ---------------------------------------------------------------- ① 全链路（假 chat）
calls = []


def fake_ok(messages, provs=None, need=None, model=None, timeout=90, **_kw):
    calls.append({"messages": messages, "need": need, "model": model})
    return {"ok": True, "used": "fake-gw", "used_model": "fake-vl-1",
            "content": '```json\n{"app": "测试界面", "scene": "在跑门禁",'
                       ' "texts": ["甲", "乙"], "issues": null}\n```',
            "why": []}


_big = mk_png("big.png", 1900, 1200)      # 会被缩
_orig_chat = P.chat
P.chat = fake_ok
try:
    r = V.look(source="file", path=_big, prompt="测试问题")
finally:
    P.chat = _orig_chat

check(r.get("ok") is True, "假模型回合法 JSON 时 look 应成功，实得 %r" % (r.get("err"),))
check(len(calls) == 1, "look 应该只问一次模型，实得 %d 次" % len(calls))

if calls:
    c0 = calls[0]
    # ① need 真的传下去了
    check(c0["need"] == "vision",
          "必须带 need='vision' 让 providers 选视觉模型，实得 %r" % (c0["need"],))
    # ② 图真的在 messages 里
    msgs = c0["messages"] or []
    blob = repr(msgs)
    check("image_url" in blob, "messages 里必须有 image_url（否则模型只是在猜）")
    check("data:image/png;base64," in blob, "图必须以 data URI 形式塞进去")
    check(len(blob) > 5000, "base64 长度不对（像是个空图）：%d 字符" % len(blob))
    # 系统提示要带「不编造」的口径
    sys_msg = next((m for m in msgs if m.get("role") == "system"), {})
    check("不编造" in (sys_msg.get("content") or "") or "不要猜" in (sys_msg.get("content") or ""),
          "system 里必须有「不编造/不要猜」的口径")

# ③ 解析出来了
data = r.get("data") or {}
check(data.get("app") == "测试界面", "围栏 JSON 没解析出来：%r" % (data,))
check(data.get("texts") == ["甲", "乙"], "texts 字段丢了")
# 缩略真的发生了
img = r.get("image") or {}
check(img.get("resized") is True, "1900x1200 的图必须被缩（否则超接口上限）")
check(int(img.get("w") or 9999) <= V.MAX_W, "缩完宽度还超上限：%r" % (img.get("w"),))

# ---------------------------------------------------------------- ③b 非 JSON 不谎报
def fake_garbage(messages, **kw):
    return {"ok": True, "used": "fake", "used_model": "fake-vl-1",
            "content": "我看到一个窗口，但我不打算给你 JSON。", "why": []}


P.chat = fake_garbage
try:
    r2 = V.look(source="file", path=_big)
finally:
    P.chat = _orig_chat
check(r2.get("ok") is True, "模型回了话但没 JSON，不该判成失败（那是答得不好，不是链路坏）")
_d2 = r2.get("data") or {}
check(_d2.get("raw") and "窗口" in _d2["raw"],
      "没解析出 JSON 时必须原样留 raw，不许丢掉模型说的话")

# ---------------------------------------------------------------- ④ 四种失败可分辨
# 4a 结构错：source=file 但没给 path
r3 = V.look(source="file")
check(r3.get("ok") is False and r3.get("stage") == "capture",
      "source=file 缺 path 必须在 capture 阶段挡住，实得 stage=%r" % (r3.get("stage"),))

# 4b 取图失败：桌面截不到（把两个后端都弄坏）
_orig_cap = V.capture_screen
V.capture_screen = lambda *a, **k: (False, "故意炸：没有屏幕")
try:
    r4 = V.look(source="screen")
finally:
    V.capture_screen = _orig_cap
check(r4.get("ok") is False and r4.get("stage") == "capture",
      "截图失败要在 capture 阶段报，实得 stage=%r" % (r4.get("stage"),))
check("故意炸" in (r4.get("err") or ""), "截图失败的原始原因不许被吞掉")

# 4c 缩略失败：给了个不存在的文件
r5 = V.look(source="file", path=os.path.join(d, "nope.png"))
check(r5.get("ok") is False and r5.get("stage") == "prepare",
      "图不存在要在 prepare 阶段报，实得 stage=%r" % (r5.get("stage"),))

# 4d 模型侧失败：没有可用视觉模型
def fake_dead(messages, **kw):
    return {"ok": False, "err": "全部失败", "why": ["fake-gw：模型表里没有视觉模型"]}


P.chat = fake_dead
try:
    r6 = V.look(source="file", path=_big)
finally:
    P.chat = _orig_chat
check(r6.get("ok") is False and r6.get("stage") == "ask",
      "模型不可用要在 ask 阶段报，实得 stage=%r" % (r6.get("stage"),))
check(r6.get("why"), "模型失败的 why 要带出来（不然只能干瞪眼）")

# ---------------------------------------------------------------- ⑤ 能力注册与闸门
from fleet import catalog as C

_caps = {c.name: c for c in C.CAPABILITIES}
for name in ("screen.look", "pcctl.look"):
    check(name in _caps, "能力没注册：%s" % name)
    if name in _caps:
        c = _caps[name]
        check(c.side_effect is True,
              "%s 必须标 side_effect（画面出本机，不是只读）" % name)
        check(c.approval == "ask",
              "%s 的审批档必须是 ask，实得 %r" % (name, c.approval))
        check("出" in c.note or "本机" in c.note,
              "%s 的 note 要把「内容出本机」说清楚" % name)

# 无闸门必须拒（fail-closed）
_reg = C.build()
_r = _reg.call("pcctl.look", args={"prompt": "x"})
check(getattr(_r, "ok", True) is False, "没注入闸门时 pcctl.look 必须被拒")
check("闸门" in (getattr(_r, "err", "") or ""),
      "拒绝原因要说「缺闸门」，实得 %r" % (getattr(_r, "err", ""),))

# 手机侧同理（不真跑 adb，只到策略层）
_r2 = _reg.call("screen.look", args={})
check(getattr(_r2, "ok", True) is False, "没注入闸门时 screen.look 必须被拒")

# ---------------------------------------------------------------- ⑥ 工具清单措辞
from fleet import agent_tools as T

_tools = {t.name: t for t in T.TOOLS}
check("screen_look" in _tools, "决策工具表少了 screen_look")
check("pc_look" in _tools, "决策工具表少了 pc_look")
for n in ("screen_look", "pc_look"):
    check(n in _tools and _tools[n].perm == "ask",
          "%s 的权限档必须是 ask（画面外发要人点）" % n)
    check(n in _tools and _tools[n].cap in _caps,
          "%s 指向的能力不存在：%r" % (n, _tools[n].cap if n in _tools else None))

# 措辞必须互相指路 —— 这条防的是「有人把描述改回去」：
# 只写「截一张屏幕的画面」，模型会拿 screenshot 去答「屏幕上写了什么」，
# 结果是回了文件路径而不是内容，用户看到的是「看不了」。
check("pc_look" in (_tools.get("pc_screenshot").desc if "pc_screenshot" in _tools else ""),
      "pc_screenshot 的描述必须指向 pc_look（否则模型拿它答「屏幕上有什么」）")
check("screen_look" in (_tools.get("screenshot_describe").desc
                        if "screenshot_describe" in _tools else ""),
      "screenshot_describe 的描述必须指向 screen_look")

_enum = (((T.DECIDE_SCHEMA or {}).get("properties") or {}).get("tool") or {}).get("enum") or []
for n in ("screen_look", "pc_look"):
    check(n in _enum, "决策 schema 的枚举里没有 %s（模型看不见就选不到）" % n)

# 渲染器必须把「她看到了什么」说出来 —— 这是最阴的一种失败：
# 能力跑了、模型也答了，渲染器却因为键名对不上回一句空话，
# 用户只看到「看不了」。所以单独钉住三种输入。
_r_ok = T._render_look({"ok": True, "data": {
    "app": "某界面", "scene": "在跑门禁", "texts": ["甲", "乙"], "issues": "有个弹窗"}})
check(_r_ok and "某界面" in _r_ok and "在跑门禁" in _r_ok,
      "正常观察必须渲染出界面和动作，实得 %r" % (_r_ok[:80],))
check("有个弹窗" in _r_ok, "issues 不能被渲染丢掉")
_r_raw = T._render_look({"ok": True, "data": {"raw": "我看到了一个窗口"}})
check(_r_raw and "窗口" in _r_raw, "只有 raw 时也要把模型说的话带出来，实得 %r" % (_r_raw[:80],))
_r_bad = T._render_look({"ok": False, "err": "没有可用的视觉模型"})
check("失败" in _r_bad and "视觉模型" in _r_bad,
      "失败要渲染成失败并带原因，实得 %r" % (_r_bad[:80],))

# ---------------------------------------------------------------- ⑦ 二进制铁律
# 手机截图必须 exec-out。`adb shell screencap -p > x.png` 会经 pty 把 \n 改 \r\n，
# PNG 当场损坏——文件非空、sha 全错、解码报错，且看起来像「截图成功」。
_src = open(os.path.join(ROOT, "fleet", "vision.py"), encoding="utf-8").read()
check('"exec-out", "screencap"' in _src, "手机截图必须走 adb exec-out screencap -p")
check("screencap -p" not in _src or "exec-out" in _src, "不许用 shell 版截图")

# ---------------------------------------------------------------- ⑧ 临时图要清掉
# 拿「模型不可用」这条路来验清理：它会走过缩略、生成 .look.png，然后失败退出——
# 正好检验失败分支也清了文件（最容易漏的就是失败路径上的清理）。
P.chat = fake_dead
try:
    r7 = V.look(source="file", path=_big, keep=False)
finally:
    P.chat = _orig_chat
check(r7.get("ok") is False, "假死模型不该成功（校验清理路径本身）")
_look_sibs = [f for f in os.listdir(os.path.dirname(_big)) if f.endswith(".look.png")]
check(len(_look_sibs) == 0,
      "keep=False 时缩略临时图必须清掉，实际还剩 %r" % (_look_sibs,))

# 收尾
import shutil
shutil.rmtree(d, ignore_errors=True)

print("t43 vision_look:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL), flush=True)
for f in FAIL:
    print("  ✗", f, flush=True)
sys.exit(1 if FAIL else 0)

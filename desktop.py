"""天网 · 桌面端（语音优先界面）。

## 界面照谁做的

照 ChatGPT 的**独立语音模式**（Separate Mode）做的：一屏一个大光球，
底下一条输入栏，没有聊天记录列表。用户给的参考图就是这个形态。

为什么这么做，以及我改了什么：

* **光球就是状态机**。ChatGPT 那个蓝球不是装饰，是把「她在听 / 在想 / 在说」
  这件事变成可见的光。语音交互最大的焦虑是「它到底收没收到我的话」，
  光球的呼吸和波纹就是在回答这个焦虑。
* **但纯语音模式有个公认的坑**：一段话没听清，只能退出重进才能看到文字。
  ChatGPT 后来自己也补了字幕。所以我加了**字幕行**——球下面一行，只显示
  最近一句（你说的话 / 她的回答），不堆历史，不破坏极简。
* **底部三个键**：左边 ➕（切角色卡）、中间输入条（可打字，也可以直接说）、
  右边 🎤 / ✕（开始说 / 停）。

## 状态

IDLE 空闲（慢呼吸）→ LISTEN 聆听（波纹扩散）→ THINK 思考（转圈）
→ SPEAK 说话（脉冲）→ 回 IDLE。免手连续模式下自动回到 LISTEN。

## 依赖策略

PIL 缺失时退化成 Canvas 同心圆（不崩，只是没那么好看）。
voice / llm / persona_card 任一缺失都只降级，不炸。
"""
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dialogue            # noqa: E402
import tianwang_cards as persona_card   # noqa: E402  嵌合件：走 X6 统一卡层

try:
    import llm             # noqa: E402
except Exception:
    llm = None

try:
    import voice            # noqa: E402
except Exception:
    voice = None

try:
    import journal          # noqa: E402
except Exception:
    journal = None

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageTk
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

try:
    import numpy as np
    _HAS_NUMPY = True
except Exception:
    np = None
    _HAS_NUMPY = False

APP_NAME = "天网"
ASR_MODEL = os.environ.get("FLEET_ASR_MODEL", "small")

IDLE, LISTEN, THINK, SPEAK = "空闲", "聆听", "思考", "说话"

# 界面是浅色（跟参考图一致）：底 #f7f8fa，球是蓝白渐变
BG = "#f7f8fa"
SURFACE = "#ffffff"
INK = "#1f2328"
DIM = "#8b949e"
LINE = "#e6e8eb"
BLUE_HI = "#3f7ef7"
BLUE_LO = "#a9c6ff"

# 状态 -> 球的主色（呼吸节奏另行处理）
STATE_TINT = {
    IDLE: (0.55, 0.71, 0.98),
    LISTEN: (0.36, 0.55, 0.97),
    THINK: (0.55, 0.58, 0.95),
    SPEAK: (0.30, 0.52, 0.95),
}


class Orb(tk.Canvas):
    """呼吸光球。

    实现方式：用 PIL 生成一张径向渐变球（带柔光晕），按帧改尺寸贴上去，
    比 Canvas 画同心圆平滑得多。没有 PIL 时退化成同心圆。

    动画只由一个 after 循环驱动，随时可停，不会堆积定时器。
    """

    def __init__(self, master, size=300, **kw):
        super().__init__(master, width=size, height=size, bg=BG,
                         highlightthickness=0, **kw)
        self.size = size
        self.state = IDLE
        self.phase = 0.0
        self.level = 0.0          # 0~1，来自麦克风或 TTS 包络
        self._level_smooth = 0.0
        self._img_id = None
        self._photo = None
        self._cache = {}
        self._running = True
        self._tick()

    def set_state(self, state):
        self.state = state
        if state == IDLE:
            self.level = 0.0

    def set_level(self, x):
        """喂一个 0~1 的能量值。球会跟着它跳。nan/inf 一律丢弃（会毒死整个渲染循环）。"""
        try:
            v = float(x)
        except (TypeError, ValueError):
            return
        if v != v or v in (float("inf"), float("-inf")):
            return
        self.level = max(0.0, min(1.0, v))

    # ---- 帧 ----
    def _tick(self):
        if not self._running:
            return
        try:
            self.phase += 0.06
            # 能量平滑：上冲快（有冲击感）、回落慢（不抽搐）
            target = self.level
            k = 0.55 if target > self._level_smooth else 0.18
            self._level_smooth += (target - self._level_smooth) * k
            self._render()
        except Exception:
            # 渲染出错绝不杀死循环——球消失就是被异常断掉的
            self._level_smooth = 0.0
            self._cache.clear()
        self.after(40, self._tick)

    def stop(self):
        self._running = False

    def _radius(self):
        """状态决定呼吸基线；能量决定跃动幅度。"""
        s = self.size
        if self.state == LISTEN:
            base, amp, speed = 0.34, 0.030, 1.6
        elif self.state == THINK:
            base, amp, speed = 0.33, 0.016, 2.6
        elif self.state == SPEAK:
            base, amp, speed = 0.35, 0.022, 3.0
        else:
            base, amp, speed = 0.33, 0.010, 1.0
        breath = amp * math.sin(self.phase * speed)
        # 能量项：最多再涨 22%，这是「跃动」的来源
        return s * (base + breath + 0.22 * self._level_smooth)

    def _render(self):
        r = int(self._radius())
        # 尺寸量化到 3px 一档再查缓存：否则每帧半径都不一样，缓存全废、狂吃 CPU
        qr = max(12, (r // 3) * 3)
        tint = STATE_TINT.get(self.state, STATE_TINT[IDLE])
        key = (qr, self.state, round(self._level_smooth * 4) / 4.0)
        img = self._cache.get(key)
        if img is None:
            img = self._make_orb(qr, tint)
            if img is None:
                return
            if len(self._cache) > 160:
                self._cache.clear()
            self._cache[key] = img
        self._photo = img
        if self._img_id is None:
            self._img_id = self.create_image(self.size // 2, self.size // 2,
                                             image=img)
        else:
            self.itemconfigure(self._img_id, image=img)

    def _make_orb(self, r, tint):
        """生成一张云雾渐变球。r 是球半径。

        ## 为什么不画同心圆

        同心圆叠出来的球是「塑料球」——边界分明、没有内部质感，一眼假。
        参考图那个球是**云雾状**的：上半偏深蓝、下半化进白色，边界柔但有芯。
        所以这里用 numpy 算场：径向衰减（形）+ 低频正弦噪声（云雾）+ 顶部偏色（光感）。

        分辨率按窗口实尺寸**全分辨率**渲染（不再压 1/2 再放大——那会把球糊掉），
        只在 alpha 边缘做亚像素羽化，芯部纹理保持锐利。
        """
        if not _HAS_PIL or not _HAS_NUMPY:
            self._fallback_circles(r, tint)
            return None
        pad = 30
        side = max(24, int((r + pad) * 2))
        scale = 2 if side > 200 else 1                # 半分辨率渲染，BICUBIC 放大
        s = max(24, side // scale)

        yy, xx = np.mgrid[0:s, 0:s].astype(np.float32)
        cx = cy = (s - 1) / 2.0
        rr = (r / float(side)) * s
        dx = (xx - cx) / rr
        dy = (yy - cy) / rr
        dist = np.sqrt(dx * dx + dy * dy)

        # 形：球体内部的径向衰减，边缘做 smoothstep 羽化
        t = np.clip(dist, 0.0, 1.0)
        shape = np.clip(1.0 - t ** 1.6, 0.0, 1.0)

        # 光：光从左上打来，左上加亮
        light = np.clip(0.55 - 0.35 * dx - 0.30 * dy, 0.0, 1.0)

        # 云雾：三组不同频率的正弦叠加，制造不规则的白色絮状
        ph = self.phase * 0.35
        cloud = (np.sin(dx * 3.1 + ph) * 0.5
                 + np.sin(dy * 2.7 - ph * 0.8) * 0.3
                 + np.sin((dx + dy) * 4.3 + ph * 0.5) * 0.2)
        cloud = (cloud + 1.0) / 2.0                      # 0~1

        # 合成：基础色随位置在「深蓝→浅蓝→白」之间过渡
        base = np.array(tint, dtype=np.float32)          # 0~1
        deep = base                                     # 芯部
        pale = np.array([1.0, 1.0, 1.0], dtype=np.float32)

        # 白化只发生在外圈 40%+ 与下缘：芯部保持饱和的蓝，对比才出得来
        whiten = np.clip((dist - 0.45) * 1.15 + dy * 0.30
                         + (1.0 - cloud) * 0.18, 0.0, 1.0)
        color = deep[None, None, :] * (1 - whiten[..., None]) \
            + pale[None, None, :] * whiten[..., None]

        # 边缘：实心到 78%，最后 22% 羽化——清晰但有过渡，不塑料
        alpha = np.clip((1.0 - t) / 0.22, 0.0, 1.0) ** 0.9
        alpha = alpha * 255.0 * (0.92 + 0.08 * cloud)
        rgba = np.dstack([np.clip(color * 255.0, 0, 255), np.clip(alpha, 0, 255)])
        img = Image.fromarray(rgba.astype(np.uint8), mode="RGBA")
        if scale > 1:
            img = img.resize((side, side), Image.BICUBIC)
        # 只羽化 alpha 边缘（防锯齿），纹理保持锐利
        r_ch, g_ch, b_ch, a_ch = img.split()
        a_ch = a_ch.filter(ImageFilter.GaussianBlur(0.7))
        img = Image.merge("RGBA", (r_ch, g_ch, b_ch, a_ch))
        return ImageTk.PhotoImage(img)

    def _fallback_circles(self, r, tint):
        self.delete("all")
        cx = cy = self.size // 2
        steps = max(10, r // 6)
        for i in range(steps, 0, -1):
            t = i / float(steps)
            k = 1.0 - t
            cr = int(255 - (255 - tint[0] * 255) * (k ** 0.7))
            cg = int(255 - (255 - tint[1] * 255) * (k ** 0.7))
            cb = int(255 - (255 - tint[2] * 255) * (k ** 0.7))
            rad = r * t
            self.create_oval(cx - rad, cy - rad, cx + rad, cy + rad,
                             fill="#%02x%02x%02x" % (cr, cg, cb), outline="")


def _similar(a, b):
    """两段中文文本的相似度（字符 2-gram Jaccard，0~1）。回声防护用。"""
    def grams(s):
        s = "".join(ch for ch in (s or "") if ch.isalnum())
        return {s[i:i + 2] for i in range(len(s) - 1)} or {s} if s else set()
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


class TianWangApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("520x860")
        self.minsize(420, 640)
        self.configure(bg=BG)

        self.state = IDLE
        self.card_id = persona_card.current()
        self._q = queue.Queue()
        self._mic_busy = False
        self.call_mode = False    # 微信通话式：True 时她说完自动续听
        self._mic_miss = 0        # 连续没听清计数（防死循环空转）
        self._last_her = ""       # 她上一句（回声防护用）
        self.handsfree_on = False
        self._last_user = ""

        self._build_ui()
        self._poll()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build_ui(self):
        # ---- 顶栏：左菜单、右设置 ----
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=18, pady=(16, 0))

        self.menu_btn = tk.Label(top, text="☰", bg=BG, fg=INK,
                                 font=("Segoe UI", 20), cursor="hand2")
        self.menu_btn.pack(side="left")
        self.menu_btn.bind("<Button-1>", lambda e: self._popup_cards())

        self.gear = tk.Label(top, text="⚙", bg=BG, fg=INK,
                             font=("Segoe UI", 18), cursor="hand2")
        self.gear.pack(side="right")
        self.gear.bind("<Button-1>", lambda e: self._popup_settings())

        self.title_label = tk.Label(top, text="", bg=BG, fg=DIM,
                                    font=("Microsoft YaHei UI", 11))
        self.title_label.pack(side="top")
        self._refresh_title()

        # ---- 中间：光球 ----
        mid = tk.Frame(self, bg=BG)
        mid.pack(fill="both", expand=True)
        self.orb = Orb(mid, size=300)
        self.orb.pack(expand=True)

        # ---- 字幕：只显示最近一句 ----
        sub = tk.Frame(self, bg=BG)
        sub.pack(fill="x", padx=28, pady=(0, 8))
        self.caption = tk.Label(sub, text="", bg=BG, fg=INK, wraplength=430,
                                justify="center",
                                font=("Microsoft YaHei UI", 13))
        self.caption.pack()
        self.hint = tk.Label(sub, text="点下面的按钮说话，或者直接打字",
                             bg=BG, fg=DIM, font=("Microsoft YaHei UI", 10))
        self.hint.pack(pady=(6, 0))

        # ---- 底部输入条 ----
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=20, pady=(4, 22))

        # 左：切角色卡（➕）
        self.plus = tk.Label(bar, text="＋", bg=SURFACE, fg=INK,
                             font=("Segoe UI", 18), width=2, height=1,
                             cursor="hand2")
        self.plus.pack(side="left", ipady=8)
        self.plus.bind("<Button-1>", lambda e: self._popup_cards())

        # 右：麦克风 / 停止
        self.mic = tk.Label(bar, text="🎤", bg=SURFACE, fg=INK,
                            font=("Segoe UI", 16), width=3, cursor="hand2")
        self.mic.pack(side="right", ipady=9, padx=(8, 0))
        self.mic.bind("<Button-1>", lambda e: self._on_mic())

        self.close_btn = tk.Label(bar, text="✕", bg="#111", fg="#fff",
                                  font=("Segoe UI", 13), width=3, cursor="hand2")
        self.close_btn.pack(side="right", ipady=10, padx=(8, 0))
        self.close_btn.bind("<Button-1>", lambda e: self._on_stop_all())

        # 中：输入框
        wrap = tk.Frame(bar, bg=SURFACE, highlightbackground=LINE,
                        highlightthickness=1)
        wrap.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.entry = tk.Entry(wrap, bg=SURFACE, fg=INK, relief="flat",
                              font=("Microsoft YaHei UI", 12),
                              insertbackground=INK)
        self.entry.pack(fill="x", padx=14, pady=12)
        self.entry.insert(0, "和她说点什么…")
        self.entry.configure(fg=DIM)
        self.entry.bind("<FocusIn>", self._on_entry_focus)
        self.entry.bind("<FocusOut>", self._on_entry_blur)
        self.entry.bind("<Return>", self._on_enter)

        # 状态小字（后端 + 卡）
        self.status = tk.Label(self, text="", bg=BG, fg=DIM,
                               font=("Microsoft YaHei UI", 9))
        self.status.pack(fill="x", padx=22, pady=(0, 8))
        self._refresh_status()

    # ---- 输入框占位符 ----
    def _on_entry_focus(self, _e):
        if self.entry.get() == "和她说点什么…":
            self.entry.delete(0, "end")
            self.entry.configure(fg=INK)

    def _on_entry_blur(self, _e):
        if not self.entry.get().strip():
            self.entry.insert(0, "和她说点什么…")
            self.entry.configure(fg=DIM)

    # ------------------------------------------------------------------
    # 顶部状态文字
    # ------------------------------------------------------------------
    def _refresh_title(self):
        card = persona_card.compile_card(self.card_id) if self.card_id else None
        name = (card or {}).get("name") or "天网"
        self.title_label.configure(text=name)

    def _refresh_status(self):
        parts = []
        chain = []
        if llm is not None:
            try:
                chain = llm.backends()
            except Exception:
                chain = []
        if chain:
            parts.append("后端：" + " → ".join(chain))
        if voice is not None:
            try:
                caps = voice.caps()
                parts.append("语音：%s / %s" % (
                    "能听" if caps.get("asr") else "不能听",
                    caps.get("tts") or "不能念"))
            except Exception:
                pass
        parts.append("卡：%s" % (self.card_id or "无"))
        self.status.configure(text="　·　".join(parts))

    # ------------------------------------------------------------------
    # 字幕
    # ------------------------------------------------------------------
    def _show_caption(self, text, who="her"):
        if who == "her":
            self._last_her = text or ""
        self.caption.configure(text=text or "")
        self.hint.configure(text=("她在说…" if who == "her" else "你说："))

    def _set_hint(self, text):
        self.hint.configure(text=text)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def _set_state(self, state):
        self.state = state
        self.orb.set_state(state)
        if state in (LISTEN, THINK, SPEAK):
            self.mic.configure(text="■")
        else:
            self.mic.configure(text="🎤")
        self._set_hint({
            IDLE: "点麦克风说话，或者直接打字",
            LISTEN: "听着呢……说完就自动识别",
            THINK: "她在想……",
            SPEAK: "她在说……",
        }.get(state, ""))

    # ------------------------------------------------------------------
    # 弹窗：角色卡 / 设置
    # ------------------------------------------------------------------
    def _popup_cards(self):
        win = tk.Toplevel(self)
        win.title("角色卡")
        win.configure(bg=BG)
        win.geometry("360x420")
        win.transient(self)

        tk.Label(win, text="选一个她", bg=BG, fg=INK,
                 font=("Microsoft YaHei UI", 13)).pack(anchor="w", padx=16, pady=(14, 8))

        box = tk.Frame(win, bg=BG)
        box.pack(fill="both", expand=True, padx=10)

        for c in persona_card.list_cards():
            cid = c.get("id")
            row = tk.Frame(box, bg=SURFACE, highlightbackground=LINE,
                           highlightthickness=1)
            row.pack(fill="x", pady=3)
            mark = "● " if cid == self.card_id else "○ "
            lab = tk.Label(row, text=mark + (c.get("name") or cid),
                           bg=SURFACE, fg=INK, anchor="w",
                           font=("Microsoft YaHei UI", 11), cursor="hand2")
            lab.pack(fill="x", padx=10, pady=8)

            def _pick(_e, cid=cid, win=win):
                self._switch_card(cid)
                win.destroy()
            lab.bind("<Button-1>", _pick)

        tk.Button(win, text="关闭", command=win.destroy).pack(pady=10)

    def _switch_card(self, cid):
        res = persona_card.switch(cid)
        if res.get("ok"):
            self.card_id = cid
            self._refresh_title()
            self._refresh_status()
            self._show_caption("换好了，现在是「%s」。" % cid, "her")
        else:
            self._show_caption("切卡失败：%s" % res.get("error"), "sys")

    def _popup_settings(self):
        win = tk.Toplevel(self)
        win.title("设置")
        win.configure(bg=BG)
        win.geometry("420x360")
        win.transient(self)

        pad = {"padx": 16, "pady": 6, "anchor": "w"}

        tk.Label(win, text="语音", bg=BG, fg=INK,
                 font=("Microsoft YaHei UI", 12)).pack(**pad)

        self.voice_status = tk.Label(win, text="", bg=BG, fg=DIM,
                                     justify="left", wraplength=380)
        self.voice_status.pack(**pad)
        if voice is not None:
            try:
                self.voice_status.configure(text=str(voice.caps()))
            except Exception:
                pass

        tk.Label(win, text="音色", bg=BG, fg=INK,
                 font=("Microsoft YaHei UI", 12)).pack(**pad)
        profiles = getattr(voice, "VOICE_PROFILES", {}) if voice else {}
        prof = profiles.get(self.card_id) or {}
        tk.Label(win, text="kokoro：%s\nedge：%s\n语速：%s%%　音调：%s" % (
            prof.get("kokoro", "-"), prof.get("edge", "-"),
            prof.get("rate", 0), prof.get("pitch", "-")),
                 bg=BG, fg=DIM, justify="left").pack(**pad)

        def _preview():
            if voice is None:
                return
            try:
                voice.say_for_card("嗯——鲸鱼娘在呢，主人。今天想吃米饭。",
                                   card_id=self.card_id)
            except Exception as e:
                self._set_hint("试听失败：%s" % e)

        tk.Button(win, text="试听这个音色", command=_preview).pack(pady=8)

        tk.Label(win, text="后端", bg=BG, fg=INK,
                 font=("Microsoft YaHei UI", 12)).pack(**pad)
        chain = []
        if llm is not None:
            try:
                chain = llm.backends()
            except Exception:
                pass
        tk.Label(win, text=" → ".join(chain) or "未配置", bg=BG, fg=DIM).pack(**pad)

        tk.Button(win, text="体检", command=self._on_health).pack(pady=8)
        tk.Button(win, text="关闭", command=win.destroy).pack(pady=(0, 10))

    def _on_health(self):
        lines = []
        if voice is not None:
            try:
                caps = voice.caps()
                lines.append("语音：ASR=%s TTS=%s" % (caps.get("asr"), caps.get("tts")))
                for k, v in (caps.get("why") or {}).items():
                    lines.append("  %s: %s" % (k, v))
            except Exception as e:
                lines.append("语音自检失败：%s" % e)
        if llm is not None:
            try:
                lines.append("后端链：" + " → ".join(llm.backends()))
            except Exception:
                pass
        lines.append("卡片：%d 张，当前 %s" % (len(persona_card.list_cards()),
                                            self.card_id or "无"))
        self._show_caption("\n".join(lines), "sys")

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _on_enter(self, _e):
        if self.state in (THINK, SPEAK):
            return "break"
        text = self.entry.get().strip()
        if not text or text == "和她说点什么…":
            return "break"
        self.entry.delete(0, "end")
        self.entry.configure(fg=INK)
        self._last_user = text
        self._show_caption(text, "me")
        self._ask(text)
        return "break"

    def _on_mic(self):
        """微信通话式：点一下开始连续对话，再点一下挂断。"""
        if self.call_mode:
            self._end_call()
            return
        self.call_mode = True
        self._mic_miss = 0
        self._set_hint("通话中 · 点麦克风挂断")
        self._start_mic()

    def _start_mic(self):
        if not self.call_mode:
            return
        if self._mic_busy or self.state in (THINK, SPEAK):
            return
        self._mic_busy = True
        threading.Thread(target=self._mic_worker, daemon=True).start()

    def _end_call(self, msg=None):
        self.call_mode = False
        self._set_state(IDLE)
        self._set_hint(msg or "已挂断。点麦克风重新开始。")

    def _mic_worker(self):
        self._q.put(("state", LISTEN))
        text, err = "", None
        try:
            if voice is None:
                raise RuntimeError("语音模块不可用")
            caps = voice.caps()
            if not caps.get("asr"):
                raise RuntimeError("听不了：缺 sounddevice 或 faster-whisper")
            # on_level 让球跟着说话音量跳（这是「跃动」的来源）
            # listen() 返回 (text, wav_path, info)，只取识别文本
            res = voice.listen(model=ASR_MODEL, language="zh",
                               on_level=lambda v: self._q.put(("level", v)))
            text = res[0] if isinstance(res, tuple) else res
            # 回声防护：没有 AEC（声学回声消除）时，扬声器里她的声音会被
            # 麦克风录回去当成「你说的」。识别结果和她上一句高度相似就整句丢弃。
            if text and self._last_her and _similar(text, self._last_her) > 0.55:
                text = ""                       # 当没听见，继续听
        except Exception as e:
            err = str(e)
        self._q.put(("mic_done", (text, err)))
        self._mic_busy = False

    def _on_stop_all(self):
        """停止键：挂断通话，回到空闲。"""
        self._end_call()

    def _on_close(self):
        try:
            self.orb.stop()
        except Exception:
            pass
        self.destroy()

    # ------------------------------------------------------------------
    # 问答
    # ------------------------------------------------------------------
    def _ask(self, text):
        self._set_state(THINK)
        threading.Thread(target=self._reply_worker, args=(text,), daemon=True).start()

    def _reply_worker(self, text):
        try:
            res = dialogue.reply(text, card_id=self.card_id, journal=journal)
        except Exception as e:
            self._q.put(("reply_error", str(e)))
            return
        self._q.put(("reply", res))

    def _speak_worker(self, text):
        """念出来，同时把声音的响度喂给球。

        winsound 拿不到播放进度，所以：先算好 wav 的包络（每 50ms 一格），
        起播时记 t0，之后每 40ms 用 (now - t0) 查表 —— 球就跟着她说话的
        音量起伏，而不是傻乎乎匀速呼吸。
        """
        env = []
        t0 = None
        try:
            if voice is None:
                raise RuntimeError("语音模块不可用")
            if hasattr(voice, "say_for_card"):
                backend, path = voice.say_for_card(text, card_id=self.card_id)
            else:
                backend, path = voice.say(text)
            if path and hasattr(voice, "envelope"):
                env = voice.envelope(path, hop_ms=50) or []
                t0 = time.time()
        except Exception:
            pass

        if not env:
            # 没包络（mp3 或合成失败）：等它自然放完就回空闲
            time.sleep(0.6 + 0.13 * len(text))
            self._q.put(("level", 0.0))
            self._q.put(("speak_done", None))
            return

        hop = 0.05
        while True:
            i = int((time.time() - t0) / hop)
            if i >= len(env):
                break
            self._q.put(("level", env[i]))
            time.sleep(0.04)
        self._q.put(("level", 0.0))
        self._q.put(("speak_done", None))

    # ------------------------------------------------------------------
    # UI 线程轮询
    # ------------------------------------------------------------------
    def _poll(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "state":
                    self._set_state(payload)
                elif kind == "level":
                    self.orb.set_level(payload)
                elif kind == "mic_done":
                    text, err = payload
                    if err:
                        self._set_state(IDLE)
                        self._show_caption("听不了：%s" % err, "sys")
                        self._end_call()
                    elif not text:
                        # 没听清：通话模式里不算错误，继续等下一句
                        self._set_state(IDLE)
                        if self.call_mode:
                            self._mic_miss += 1
                            if self._mic_miss >= 4:
                                self._end_call("连着几轮没听清，先挂断了。点麦克风再开始。")
                            else:
                                self._set_hint("没听清，继续说…（%d/4）" % self._mic_miss)
                                self.after(700, self._start_mic)
                        else:
                            self._set_hint("没听清，再说一次？")
                    else:
                        self._mic_miss = 0
                        self._last_user = text
                        self._show_caption(text, "me")
                        self._ask(text)
                elif kind == "speak_done":
                    # 她说完了：通话模式自动回到「听」，这就是免按通话
                    self._set_state(IDLE)
                    if self.call_mode:
                        self.after(350, self._start_mic)
                elif kind == "reply":
                    self._handle_reply(payload)
                elif kind == "reply_error":
                    self._set_state(IDLE)
                    self._show_caption("出错了：%s" % payload, "sys")
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _handle_reply(self, res):
        say = res.get("say") or ""
        backend = res.get("backend") or ""
        self._show_caption(say or "（她没说话）", "her")
        if backend:
            self._set_hint("%s　·　%dms" % (backend, res.get("ms", 0)))
        if res.get("card") and res["card"] != self.card_id:
            self.card_id = res["card"]
            self._refresh_title()

        if say and voice is not None:
            self._set_state(SPEAK)
            threading.Thread(target=self._speak_worker, args=(say,), daemon=True).start()
        else:
            self._set_state(IDLE)


def main():
    app = TianWangApp()
    app.mainloop()


if __name__ == "__main__":
    main()

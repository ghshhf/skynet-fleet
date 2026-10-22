"""语音链路的「融合层」：文本规范化 + 切块 + 边合边播 + 可打断。

为什么单独一个文件、而不是继续往 voice.py 里塞：
voice.py 已经 1340 行，管着录音/识别/合成/播放四件事。这一层是**新增**
的编排逻辑（谁先谁后、什么时候停），和「怎么合成」不是一类东西。
把它隔开，voice.py 那边只要提供 synth(text)->wav 就能接上，不用改它的内部。

三个实测结论决定了这里的写法（数据见 docs/语音_开源全景调研与融合设计_2026-10-22.md）：

1. **首音延迟由「第一个可播单位有多长」决定，与模型 RTF 几乎无关。**
   同一句 39 字：整段合成 906ms、按标点切 329ms、首短尾长 211ms。
   切块超过 14 字一块就退化成整段（405ms）。⇒ 首块必须短。
2. **端点等待 ≈ min_silence_duration**，Silero 与 TEN VAD 只差 30ms，
   所以慢的原因是把静音设成 0.8 秒，不是选错 VAD。⇒ 参数调到 0.35。
3. **不能打断的助手会让人本能地缩短句子。** 打断决定「她到底在不在听」，
   比音色重要。

刻意没做的：不上全双工 S2S（要 GPU）、不接 WebRTC 框架（要 media server）、
不搬克隆模型（要 GPU + 声音权红线）。
"""
import os
import queue
import re
import threading
import time

# ============================ 一、文本规范化 ============================

_DIGITS = "零一二三四五六七八九"
_UNITS = ["", "十", "百", "千"]
_BIG = ["", "万", "亿", "万亿"]

# 中文习惯的「两」：二百/两千 口语更常说两百/两千，但 2 万不说两万…… 其实也说。
# 保守起点：不改二/两，避免过度改写。


def _read_4(n, is_lead=False):
    """读 0~9999 的一节。is_lead=True 时开头的「一十」读成「十」。"""
    if n == 0:
        return ""
    out = []
    zero = False
    s = str(n)
    L = len(s)
    for i, ch in enumerate(s):
        d = int(ch)
        pos = L - i - 1
        if d == 0:
            zero = True
            continue
        if zero and out:
            out.append("零")
        zero = False
        out.append(_DIGITS[d] + _UNITS[pos])
    r = "".join(out)
    if is_lead and r.startswith("一十"):
        r = r[1:]
    return r


def read_int(n):
    """把非负整数读成中文。0 -> 零，10 -> 十，105 -> 一百零五，10005 -> 一万零五。"""
    n = int(n)
    if n == 0:
        return "零"
    if n < 0:
        return "负" + read_int(-n)
    # 从低到高切节，每节 4 位
    secs = []
    while n > 0:
        secs.append(n % 10000)
        n //= 10000
    out = []
    for i in range(len(secs) - 1, -1, -1):
        chunk = secs[i]
        if chunk == 0:
            continue
        piece = _read_4(chunk, is_lead=(i == len(secs) - 1))
        # 补零：本节不足 4 位、且不是最高节 -> 前面要读一个「零」
        #   10005 -> 一万 | 五     => 一万零五
        #   12005 -> 一万 | 两千零五 => 一万二千零五
        if i != len(secs) - 1 and chunk < 1000:
            out.append("零")
        out.append(piece + _BIG[i])
    return "".join(out)


def read_year(y):
    """年份逐位读：2026 -> 二零二六（不是「两千零二十六」）。"""
    return "".join(_DIGITS[int(c)] for c in str(int(y)))


def read_decimal(int_part, frac):
    return read_int(int_part) + "点" + "".join(_DIGITS[int(c)] for c in frac)


# --- 各类模式：顺序很重要，先处理长的、specific 的 ---

_RE_RANGE = re.compile(r"(\d+)\s*[-~－—]\s*(\d+)")
_RE_TIME = re.compile(r"\b([01]?\d|2[0-3])[:：]([0-5]\d)\b")
_RE_DATE_CN = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")
_RE_MONTH_DAY = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_RE_VER = re.compile(r"\bv?(\d+)\.(\d+)(?:\.(\d+))?\b")   # 版本号 1.2.3
_RE_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")
_RE_TEMP = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:℃|°C|摄氏度)")
_RE_MONEY = re.compile(r"[¥￥]\s*(\d[\d,]*(?:\.\d+)?)")
_RE_MONEY_CN = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(元|块|块钱|美元|刀)")
_RE_MB = re.compile(r"(\d+(?:\.\d+)?)\s*(GB|MB|KB|TB|G|M|K)\b", re.I)
_RE_FRAC = re.compile(r"(\d+)\s*/\s*(\d+)")
_RE_SCI = re.compile(r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)", re.I)
_RE_NUM = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


def _num_token(tok):
    tok = tok.replace(",", "")
    if "." in tok:
        a, b = tok.split(".", 1)
        return read_decimal(a or "0", b)
    return read_int(tok)


def normalize_zh(text):
    """把数字/日期/金额/百分比/单位展开成中文读法。

    为什么必须自己做：kokoro / edge_tts 的中文前端都**不做**完整数字规范化，
    数字会按字面念或者直接吞掉。icefall 模型目录里带 number.fst / date.fst /
    phone.fst —— 工业界用规则状态机做这事，不是字符串替换。这里就是那层规则。

    保守原则：**只改有把握的**。改错比不改难听得多（「一百二十三」念成
    「一二三」只是别扭，「二零二六」念成「两千零二十六」是错）。
    """
    if not text:
        return text
    s = text

    # 年份优先：2026年 / 2026 单独出现且是 19xx/20xx
    s = _RE_DATE_CN.sub(lambda m: "%s年%s月%s日" % (
        read_year(m.group(1)), read_int(m.group(2)), read_int(m.group(3))), s)
    s = _RE_MONTH_DAY.sub(lambda m: "%s月%s日" % (
        read_int(m.group(1)), read_int(m.group(2))), s)

    # 时间 14:30 -> 十四点三十（分钟 0 读「整」太自然，但会改变语义，先不读）
    def _time(m):
        h, mi = int(m.group(1)), int(m.group(2))
        if mi == 0:
            return "%s点整" % read_int(h)
        if mi < 10:
            return "%s点零%s分" % (read_int(h), read_int(mi))
        return "%s点%s分" % (read_int(h), read_int(mi))
    s = _RE_TIME.sub(_time, s)

    s = _RE_PCT.sub(lambda m: "百分之" + _num_token(m.group(1)), s)
    s = _RE_TEMP.sub(lambda m: _num_token(m.group(1)) + "摄氏度", s)
    s = _RE_MONEY.sub(lambda m: _num_token(m.group(1)) + "元", s)
    s = _RE_MONEY_CN.sub(lambda m: _num_token(m.group(1)) + m.group(2), s)

    def _mb(m):
        unit = m.group(2).upper()
        words = {"GB": "吉", "TB": "太", "MB": "兆", "KB": "千字节",
                 "G": "吉", "M": "兆", "K": "千"}
        return _num_token(m.group(1)) + words.get(unit, unit) + "字节"
    s = _RE_MB.sub(_mb, s)

    s = _RE_FRAC.sub(lambda m: "%s分之%s" % (read_int(m.group(2)), read_int(m.group(1))), s)
    s = _RE_SCI.sub(lambda m: "%s乘%s" % (_num_token(m.group(1)), _num_token(m.group(2))), s)
    s = _RE_RANGE.sub(lambda m: "%s到%s" % (_num_token(m.group(1)), _num_token(m.group(2))), s)

    # 只剩纯数字了
    def _bare(m):
        tok = m.group(0)
        # 4 位且 19xx/20xx 当成年份读
        plain = tok.replace(",", "")
        if re.fullmatch(r"(19|20)\d{2}", plain):
            return read_year(plain)
        return _num_token(tok)
    s = _RE_NUM.sub(_bare, s)
    return s


# ============================ 二、切块（首短尾长） ============================

_PUNCT = "，。！？；、,.!?;：:…—"
_SENT_END = "。！？.!?…"


def split_chunks(text, head=7, tail=18, min_len=6):
    """首块取短（抢首音），后续块放长（保韵律）。

    实测依据：首音延迟 = 首块合成时间。首块 7 字约 211ms，首块 14 字约 329ms，
    整段 39 字 906ms。而尾块放宽能减少拼接痕迹——块越多，韵律越碎。

    切点优先落在标点上；找不到标点就在 min_len 之后硬切（保证首块不超长）。
    """
    t = (text or "").strip()
    if not t:
        return []
    if len(t) <= head:
        return [t]

    out = []
    rest = t

    # 首块：尽量在标点处切，且不超过 head
    cut = -1
    for i in range(min(head, len(rest)), max(min_len - 1, 0), -1):
        if rest[i - 1] in _PUNCT:
            cut = i
            break
    if cut < 0:
        cut = min(head, len(rest))
    seg = rest[:cut].strip()
    if seg:
        out.append(seg)
    rest = rest[cut:]

    # 后续块：优先句子边界，其次逗号，最后按 tail 硬切
    while rest:
        rest = rest.lstrip()
        if not rest:
            break
        if len(rest) <= tail:
            out.append(rest)
            break
        cut = -1
        # 先找句末标点（在 tail 之前最近的）
        for i in range(min(tail, len(rest)), min_len - 1, -1):
            if rest[i - 1] in _SENT_END:
                cut = i
                break
        if cut < 0:
            for i in range(min(tail, len(rest)), min_len - 1, -1):
                if rest[i - 1] in _PUNCT:
                    cut = i
                    break
        if cut < 0:
            cut = tail
        out.append(rest[:cut].strip())
        rest = rest[cut:]

    return [c for c in out if c]


# ============================ 三、播放 + 打断 ============================

class _Player:
    """Windows 下顺序播放 wav，且能从别的线程叫停。

    winsound.PlaySound(None, 0) 是唯一能中断「正在播」的手段：
    带 SND_ASYNC 的调用会立刻返回但播放继续，想停只能再喊一次 None。
    """

    def __init__(self):
        self._winsound = None
        try:
            import winsound
            self._winsound = winsound
        except Exception:
            self._winsound = None
        self._lock = threading.Lock()
        self._interrupted = False

    @property
    def available(self):
        return self._winsound is not None

    def play_blocking(self, path):
        """播完才返回。被 interrupt() 打断时立刻返回 False。"""
        if not self._winsound or not path or not os.path.isfile(path):
            return False
        with self._lock:
            if self._interrupted:
                return False
        try:
            self._winsound.PlaySound(path, self._winsound.SND_FILENAME)
            return True
        except Exception:
            return False

    def interrupt(self):
        self._interrupted = True
        if self._winsound:
            try:
                self._winsound.PlaySound(None, 0)
            except Exception:
                pass

    @property
    def interrupted(self):
        """是不是已经被叫停。对外只暴露这个，不暴露 _interrupted——
        测试用的假播放器只要实现 interrupt()/interrupted 就能顶替。"""
        return self._interrupted

    def reset(self):
        self._interrupted = False


class MicWatcher:
    """播放期间盯着麦克风：听到人声就喊停。

    阈值不比录音 VAD 激进——播放时扬声器的声音会漏进麦克风（回声），
    太灵敏会自己把自己打断。所以要求「连续 N 块超阈值」才算数，
    单次掌声、关门声不会误触发。
    """

    def __init__(self, rms_thresh=0.02, need_blocks=3, block_sec=0.032,
                 samplerate=16000, device=None):
        self.rms_thresh = rms_thresh
        self.need_blocks = need_blocks
        self.block_sec = block_sec
        self.samplerate = samplerate
        self.device = device
        self._stop = threading.Event()
        self._hit = threading.Event()
        self.fired_at = None
        self._th = None
        self._why = ""

    @property
    def ready(self):
        return self._th is not None

    def start(self, on_hit=None):
        try:
            import sounddevice as sd
        except Exception as e:
            self._why = "缺少 sounddevice：%s" % e
            return False
        try:
            import numpy as np
        except Exception as e:
            self._why = "缺少 numpy：%s" % e
            return False

        bs = max(1, int(self.samplerate * self.block_sec))

        def loop():
            run = 0
            try:
                with sd.InputStream(samplerate=self.samplerate, channels=1,
                                    dtype="int16", blocksize=bs,
                                    device=self.device) as st:
                    while not self._stop.is_set():
                        data, _ = st.read(bs)
                        a = np.frombuffer(
                            data.tobytes() if hasattr(data, "tobytes") else bytes(data),
                            dtype="<i2").astype("float32") / 32768.0
                        rms = float((a * a).mean() ** 0.5)
                        if rms > self.rms_thresh:
                            run += 1
                            if run >= self.need_blocks:
                                self.fired_at = time.time()
                                self._hit.set()
                                if on_hit:
                                    try:
                                        on_hit()
                                    except Exception:
                                        pass
                                return
                        else:
                            run = 0
            except Exception as e:
                self._why = "麦克风打开失败：%s" % e

        self._th = threading.Thread(target=loop, daemon=True)
        self._th.start()
        return True

    @property
    def hit(self):
        return self._hit.is_set()

    @property
    def why(self):
        return self._why

    def stop(self):
        self._stop.set()


class Speech:
    """一次朗读的完整过程：切块 → 边合边播 → 可被打断。

    用法：
        sp = Speech(synth=lambda t: path, player=_Player())
        r = sp.run("长句子……", interruptible=True)
        # r = {"chunks":n, "first_audio_ms":..., "interrupted":bool, ...}
    """

    def __init__(self, synth, player=None, mic=None):
        self.synth = synth          # text -> wav 路径（失败返回 None）
        self.player = player or _Player()
        self.mic = mic

    def run(self, text, interruptible=True, on_chunk=None, on_first_audio=None,
            max_prefetch=2, cleanup=True):
        chunks = split_chunks(text)
        if not chunks:
            return {"chunks": 0, "interrupted": False, "first_audio_ms": None,
                    "why": "空文本"}
        if not self.player.available:
            return {"chunks": len(chunks), "interrupted": False,
                    "first_audio_ms": None, "why": "winsound 不可用"}

        q = queue.Queue(maxsize=max(1, max_prefetch))
        halt = threading.Event()
        errs = []

        def worker():
            for i, c in enumerate(chunks):
                if halt.is_set():
                    break
                try:
                    p = self.synth(c)
                except Exception as e:
                    errs.append("块%d 合成异常：%s" % (i + 1, e))
                    p = None
                if p is None:
                    errs.append("块%d 合成失败" % (i + 1))
                    continue
                while not halt.is_set():
                    try:
                        q.put((i, c, p), timeout=0.1)
                        break
                    except queue.Full:
                        continue
            try:
                q.put(None, timeout=1.0)
            except queue.Full:
                pass

        self.player.reset()
        t0 = time.time()
        first_ms = None
        served = 0
        interrupted = False

        th = threading.Thread(target=worker, daemon=True)
        th.start()

        if interruptible and self.mic is not None:
            self.mic.start(on_hit=self.player.interrupt)
        try:
            while True:
                try:
                    item = q.get(timeout=0.2)
                except queue.Empty:
                    if not th.is_alive():
                        break
                    if self.player.interrupted:
                        interrupted = True
                        break
                    continue
                if item is None:
                    break
                i, c, p = item
                if self.player.interrupted:
                    interrupted = True
                    break
                if first_ms is None:
                    first_ms = (time.time() - t0) * 1000
                    if on_first_audio:
                        try:
                            on_first_audio(first_ms)
                        except Exception:
                            pass
                ok = self.player.play_blocking(p)
                served += 1
                if on_chunk:
                    try:
                        on_chunk(i, c, ok)
                    except Exception:
                        pass
                if not ok:
                    interrupted = True
                    break
                if cleanup:
                    try:
                        os.unlink(p)
                    except Exception:
                        pass
        finally:
            halt.set()
            if interruptible and self.mic is not None:
                self.mic.stop()

        return {
            "chunks": len(chunks),
            "served": served,
            "interrupted": interrupted,
            "first_audio_ms": round(first_ms, 1) if first_ms else None,
            "total_ms": round((time.time() - t0) * 1000, 1),
            "texts": chunks,
            "errors": errs,
            "mic_why": (self.mic.why if self.mic is not None else ""),
        }


# ============================ 四、自测 ============================

def _selftest():
    ok = 0
    bad = []

    def chk(name, got, want):
        nonlocal ok
        if got == want:
            ok += 1
        else:
            bad.append("%s: 得到 %r 期望 %r" % (name, got, want))

    # --- 数字读法 ---
    chk("read_int 0", read_int(0), "零")
    chk("read_int 10", read_int(10), "十")
    chk("read_int 105", read_int(105), "一百零五")
    chk("read_int 10000", read_int(10000), "一万")
    chk("read_int 123", read_int(123), "一百二十三")
    chk("read_int 2026(整数)", read_int(2026), "二千零二十六")   # 整数读法本就如此
    chk("read_year 2026", read_year(2026), "二零二六")
    chk("read_decimal", read_decimal("3", "14"), "三点一四")

    # --- 规范化 ---
    chk("百分比", normalize_zh("湿度 45%"), "湿度 百分之四十五")
    chk("年份", normalize_zh("2026年10月22日"), "二零二六年十月二十二日")
    chk("时间", normalize_zh("14:30 开会"), "十四点三十分 开会")
    chk("整点", normalize_zh("9:00"), "九点整")
    chk("金额", normalize_zh("¥1,200"), "一千二百元")
    chk("温度", normalize_zh("26℃"), "二十六摄氏度")
    chk("范围", normalize_zh("3-5 天"), "三到五 天")
    chk("容量", normalize_zh("1.6GB"), "一点六吉字节")
    chk("分数", normalize_zh("3/4"), "四分之三")
    chk("纯数字年份", normalize_zh("2026 年"), "二零二六 年")
    chk("小数", normalize_zh("3.14"), "三点一四")
    chk("不动中文", normalize_zh("你好呀"), "你好呀")

    # --- 切块 ---
    long_text = "现在是下午两点十一分，屋里温度二十六度，湿度百分之四十五。再过三天就是立冬了。"
    cs = split_chunks(long_text)
    chk("切块数 <=5", len(cs) <= 5, True)
    chk("首块 <=7 字", len(cs[0]) <= 7, True)
    chk("拼回原文（去空白）", re.sub(r"\s", "", "".join(cs)),
        re.sub(r"\s", "", long_text))
    chk("短句不切", split_chunks("好的"), ["好的"])
    chk("空文本", split_chunks("  "), [])

    print("voice_stream 自测：%d 通过 / %d 失败" % (ok, len(bad)))
    for b in bad:
        print("  ✗", b)
    return not bad


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    print(normalize_zh(" ".join(sys.argv[1:]) or "湿度 45%，2026年10月22日 14:30"))
    print(split_chunks(" ".join(sys.argv[1:]) or "现在是下午两点十一分，屋里温度二十六度，湿度百分之四十五。"))

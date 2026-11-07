# -*- coding: utf-8 -*-
"""fun —— 生活小工具：纯 stdlib、不联网、不落盘。

为什么单独一层：这些能力没有设备依赖（不需要 adb / 无障碍 / 注册表里的某个
kind），也不该为了它们新增一种设备。`agent_tools` 里以 `fun_` 开头的工具直接
走这里的 `run()`，和 `web_search` 的特例同级。

边界：
  · 全部无副作用，权限一律 auto；
  · 输入上限 4000 字符，输出由 agent_tools 再裁到 1200；
  · 密码/哈希的输入不写审计（见 agent_tools.execute 的脱敏）。
"""
import base64
import codecs
import colorsys
import hashlib
import random
import re
import secrets
import string
import urllib.parse
from datetime import date

MAX_IN = 4000

_LOWER = "abcdefghijkmnopqrstuvwxyz"          # 去掉 l
_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"           # 去掉 I/O
_DIGITS = "23456789"                          # 去掉 0/1
_SYMBOLS = "!@#$%^&*-_=+"
_ALL = _LOWER + _UPPER + _DIGITS + _SYMBOLS


def _ok(text):
    return {"ok": True, "text": text}


def _err(text):
    return {"ok": False, "text": text}


def run(tool, arg, today=None):
    """执行一个小工具。返回 {ok, text}，永远不抛异常（工具失败也是事实）。"""
    s = (arg or "").strip()
    if len(s) > MAX_IN:
        return _err("输入太长（上限 %d 字符）" % MAX_IN)
    fn = _TOOLS.get(tool)
    if fn is None:
        return _err("未知小工具 %s" % tool)
    try:
        return fn(s, today)
    except Exception as e:  # 纯函数也可能被极端输入打穿，不能拖垮对话
        return _err("小工具 %s 失败：%s: %s" % (tool, type(e).__name__, e))


# ---------------------------------------------------------------- 哈希

_ALGOS = ("md5", "sha1", "sha256", "sha512")


def _hash(s, _today):
    algo, text = "sha256", s
    m = re.match(r"^(md5|sha1|sha256|sha512)\s*[:：]?\s*(.*)$", s, re.I | re.S)
    if m:
        algo, text = m.group(1).lower(), m.group(2)
    if not text:
        return _err("给要计算的文本，例如：sha256 你好")
    raw = text.encode("utf-8")
    return _ok("%s：%d 字节 → %s" % (
        algo.upper(), len(raw), hashlib.new(algo, raw).hexdigest()))


# ---------------------------------------------------------------- 密码

def _password(s, _today):
    m = re.search(r"\d+", s)
    n = int(m.group()) if m else 20
    n = max(8, min(64, n))
    pools = (_LOWER, _UPPER, _DIGITS, _SYMBOLS)
    chars = [secrets.choice(p) for p in pools]
    chars += [secrets.choice(_ALL) for _ in range(n - len(chars))]
    random.SystemRandom().shuffle(chars)
    # 每字符 log2(68) ≈ 6.09 bit，只报保守值
    return _ok("%d 位强密码（约 %.0f bit）：%s" % (n, n * 6.0, "".join(chars)))


# ---------------------------------------------------------------- 颜色

_NAMED = {
    "红": "#ff0000", "橙": "#ff8000", "黄": "#ffff00", "绿": "#00ff00",
    "青": "#00ffff", "蓝": "#0000ff", "紫": "#8000ff", "黑": "#000000",
    "白": "#ffffff", "灰": "#808080", "粉": "#ffc0cb", "棕": "#8b4513",
}


def _parse_color(s):
    t = s.strip().lower()
    if t in _NAMED:
        t = _NAMED[t]
    m = re.fullmatch(r"#?([0-9a-f]{3})", t)
    if m:
        return tuple(int(c * 2, 16) for c in m.group(1))
    m = re.fullmatch(r"#?([0-9a-f]{6})", t)
    if m:
        h = m.group(1)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    m = re.fullmatch(r"rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3}).*\)", t)
    if m:
        rgb = tuple(int(x) for x in m.groups())
        return tuple(max(0, min(255, x)) for x in rgb)
    m = re.fullmatch(r"hsla?\(\s*([\d.]+)\s*[, ]\s*([\d.]+)%?\s*[, ]\s*([\d.]+)%?.*\)", t)
    if m:
        h, sat, light = (float(x) for x in m.groups())
        rgb = colorsys.hls_to_rgb((h % 360) / 360.0, light / 100.0, sat / 100.0)
        return tuple(round(x * 255) for x in rgb)
    return None


def _hex(rgb):
    return "#%02X%02X%02X" % rgb


def _lum(rgb):
    def f(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _color(s, _today):
    rgb = _parse_color(s)
    if rgb is None:
        return _err("认不出这个颜色。给 #ff0000 / rgb(255,0,0) / hsl(0,100%,50%) / 颜色名")
    r, g, b = rgb
    h, l, sat = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
    hd, sd, ld = round(h * 360), round(sat * 100), round(l * 100)

    def at(dh):
        rr, gg, bb = colorsys.hls_to_rgb(((h * 360 + dh) % 360) / 360.0, l, sat)
        return _hex((round(rr * 255), round(gg * 255), round(bb * 255)))

    return _ok(
        "%s = rgb(%d, %d, %d) = hsl(%d, %d%%, %d%%)\n"
        "亮度 %.2f；对比黑 %.2f:1 / 白 %.2f:1\n"
        "配色：互补 %s；邻近 %s / %s；三角 %s / %s" % (
            _hex(rgb), r, g, b, hd, sd, ld,
            _lum(rgb), _contrast(rgb, (0, 0, 0)), _contrast(rgb, (255, 255, 255)),
            at(180), at(-30), at(30), at(120), at(-120)))


# ---------------------------------------------------------------- 单位换算

_UNITS = {
    "length": {
        "mm": 0.001, "毫米": 0.001, "cm": 0.01, "厘米": 0.01,
        "dm": 0.1, "分米": 0.1, "m": 1.0, "米": 1.0,
        "km": 1000.0, "公里": 1000.0, "千米": 1000.0,
        "in": 0.0254, "英寸": 0.0254, "ft": 0.3048, "英尺": 0.3048,
        "yd": 0.9144, "码": 0.9144, "mi": 1609.344, "英里": 1609.344,
        "里": 500.0, "尺": 1 / 3.0, "寸": 1 / 30.0,
    },
    "mass": {
        "mg": 0.001, "毫克": 0.001, "g": 1.0, "克": 1.0,
        "kg": 1000.0, "公斤": 1000.0, "千克": 1000.0,
        "t": 1e6, "吨": 1e6, "斤": 500.0, "两": 50.0,
        "lb": 453.59237, "磅": 453.59237, "oz": 28.349523125, "盎司": 28.349523125,
    },
    "data": {
        "b": 1.0, "byte": 1.0, "字节": 1.0,
        "kb": 1024.0, "mb": 1024.0 ** 2, "gb": 1024.0 ** 3,
        "tb": 1024.0 ** 4, "pb": 1024.0 ** 5,
        "kib": 1024.0, "mib": 1024.0 ** 2, "gib": 1024.0 ** 3,
    },
    "time": {
        "ms": 0.001, "毫秒": 0.001, "s": 1.0, "sec": 1.0, "秒": 1.0,
        "min": 60.0, "分钟": 60.0, "h": 3600.0, "hr": 3600.0, "小时": 3600.0,
        "day": 86400.0, "天": 86400.0, "week": 604800.0, "周": 604800.0,
    },
    "speed": {
        "m/s": 1.0, "米/秒": 1.0, "km/h": 1 / 3.6, "kph": 1 / 3.6,
        "公里/小时": 1 / 3.6, "mph": 0.44704, "knot": 0.514444, "节": 0.514444,
    },
}
_TEMP = {"c": "C", "celsius": "C", "摄氏": "C", "度": "C",
         "f": "F", "fahrenheit": "F", "华氏": "F",
         "k": "K", "kelvin": "K", "开": "K", "开尔文": "K"}


def _to_c(v, unit):
    if unit == "C":
        return v
    if unit == "F":
        return (v - 32) * 5 / 9
    return v - 273.15


def _from_c(v, unit):
    if unit == "C":
        return v
    if unit == "F":
        return v * 9 / 5 + 32
    return v + 273.15


def _unit(s, _today):
    t = s.replace("→", " ").replace("->", " ").replace("=>", " ")
    t = re.sub(r"\s*(换成|转成|转为|等于|to|in)\s*", " ", t, flags=re.I)
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*([^\s\d]+)\s+([^\s]+)\s*$", t)
    if not m:
        return _err("格式：10 km mi / 100 c f / 1 GB MB（数值 源单位 目标单位）")
    val, src, dst = float(m.group(1)), m.group(2).lower(), m.group(3).lower()

    if src in _TEMP and dst in _TEMP:
        out = _from_c(_to_c(val, _TEMP[src]), _TEMP[dst])
        return _ok("%g%s = %g%s" % (val, _TEMP[src], out, _TEMP[dst]))

    for cat, table in _UNITS.items():
        if src in table and dst in table:
            out = val * table[src] / table[dst]
            return _ok("%g %s = %.6g %s（%s）" % (val, src, out, dst, cat))
    known = "、".join(sorted(set(list(_UNITS["length"]) + list(_UNITS["mass"]) +
                                list(_UNITS["data"]) + list(_UNITS["time"]) +
                                list(_UNITS["speed"]) + list(_TEMP))))
    return _err("不认识的单位：%s / %s。支持：%s" % (src, dst, known))


# ---------------------------------------------------------------- 摩斯

_MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "!": "-.-.--", "/": "-..-.",
    "(": "-.--.", ")": "-.--.-", "&": ".-...", ":": "---...", ";": "-.-.-.",
    "=": "-...-", "+": ".-.-.", "-": "-....-", "_": "..--.-", '"': ".-..-.",
    "$": "...-..-", "@": ".--.-.",
}
_MORSE_REV = {v: k for k, v in _MORSE.items()}


def _morse(s, _today):
    m = re.match(r"^(?:摩斯|morse)?\s*(解码|编码|decode|encode)\s*[:：]?\s*", s, re.I)
    if m:
        mode = "d" if m.group(1).lower() in ("解码", "decode") else "e"
        s = s[m.end():]
    elif s and not re.search(r"[A-Za-z0-9\u4e00-\u9fa5]", s):
        mode = "d"
    else:
        mode = "e"
    if mode == "d":
        words = [w for w in re.split(r"\s*/\s*", s.strip()) if w]
        out = []
        for w in words:
            letters = []
            for code in w.split():
                letters.append(_MORSE_REV.get(code, "?"))
            out.append("".join(letters))
        text = " ".join(out)
        return _ok("摩斯解码：%s" % text) if text else _err("给点 . 和 -")
    out = []
    for ch in s.upper():
        if ch == " ":
            out.append("/")
        elif ch in _MORSE:
            out.append(_MORSE[ch])
        else:
            out.append("?")
    text = " ".join(out)
    return _ok("摩斯编码：%s%s" % (text, "（含未支持字符，已标 ?）" if "?" in text else ""))


# ---------------------------------------------------------------- 编解码

_CODEC_START = re.compile(
    r"^(b64e|b64d|b32e|b32d|hexe|hexd|urle|urld|rot13|"
    r"base64编码|base64解码|base32编码|base32解码|十六进制编码|十六进制解码|"
    r"url编码|url解码|base64\s*编码|base64\s*解码)\s*[:：]?\s*(.*)$",
    re.I | re.S,
)
_CODEC_ALIAS = {
    "base64编码": "b64e", "base64解码": "b64d", "base64 编码": "b64e",
    "base64 解码": "b64d", "base32编码": "b32e", "base32解码": "b32d",
    "十六进制编码": "hexe", "十六进制解码": "hexd",
    "url编码": "urle", "url解码": "urld",
}


def _codec(s, _today):
    m = _CODEC_START.match(s)
    if not m:
        return _err("前面加命令：b64e / b64d / urle / urld / hexe / hexd / rot13，"
                    "例如：b64e 你好")
    cmd = _CODEC_ALIAS.get(m.group(1).lower(), m.group(1).lower())
    body = m.group(2)
    if cmd == "b64e":
        out = base64.b64encode(body.encode("utf-8")).decode("ascii")
    elif cmd == "b64d":
        pad = body + "=" * (-len(body) % 4)
        out = base64.b64decode(pad, validate=True).decode("utf-8", "replace")
    elif cmd == "b32e":
        out = base64.b32encode(body.encode("utf-8")).decode("ascii")
    elif cmd == "b32d":
        pad = body.upper() + "=" * (-len(body) % 8)
        out = base64.b32decode(pad).decode("utf-8", "replace")
    elif cmd == "hexe":
        out = body.encode("utf-8").hex()
    elif cmd == "hexd":
        out = bytes.fromhex(re.sub(r"[\s:,-]", "", body)).decode("utf-8", "replace")
    elif cmd == "urle":
        out = urllib.parse.quote(body, safe="")
    elif cmd == "urld":
        out = urllib.parse.unquote(body)
    else:
        out = codecs.encode(body, "rot13")
    return _ok("%s：%s\n（%d → %d 字符）" % (cmd, out, len(body), len(out)))


# ---------------------------------------------------------------- 随机

def _random(s, _today):
    low = s.lower()
    if "coin" in low or "硬币" in low or "抛" in low:
        return _ok("硬币：%s" % secrets.choice(("正面", "反面")))
    m = (re.search(r"(\d+)\s*[dD]\s*(\d+)", s)
         or re.search(r"(\d+)\s*个\s*(\d+)\s*面", s))
    if m:
        n, sides = int(m.group(1)), int(m.group(2))
        n, sides = max(1, min(100, n)), max(2, min(1000, sides))
        rolls = [secrets.randbelow(sides) + 1 for _ in range(n)]
        return _ok("%dd%d：%s（合计 %d）" % (n, sides, rolls, sum(rolls)))
    body = re.sub(r"^(pick|抽签?|选|随机)\s*[:：]?\s*", "", s, flags=re.I)
    items = [x for x in re.split(r"[,，、\s]+", body) if x]
    if len(items) < 2:
        return _err("格式：dice 2d6 / coin / pick 甲,乙,丙")
    return _ok("抽中：%s（共 %d 个）" % (secrets.choice(items), len(items)))


# ---------------------------------------------------------------- 日期

_WEEK = "一二三四五六日"


def _date(s, today):
    base = today or date.today()
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    target = None
    if m:
        try:
            target = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return _err("日期不存在：%s" % m.group(0))
    else:
        m = re.search(r"(\d{1,2})[-/.](\d{1,2})", s)
        if m:
            try:
                target = date(base.year, int(m.group(1)), int(m.group(2)))
                if target < base:
                    target = date(base.year + 1, int(m.group(1)), int(m.group(2)))
            except ValueError:
                return _err("日期不存在：%s" % m.group(0))
    if target is None:
        return _ok("今天 %s，星期%s；今年第 %d 天，第 %d 周。" % (
            base.isoformat(), _WEEK[base.weekday()], base.timetuple().tm_yday,
            base.isocalendar()[1]))

    delta = (target - base).days
    if delta > 0:
        rel = "还有 %d 天" % delta
    elif delta < 0:
        rel = "已过去 %d 天" % (-delta)
    else:
        rel = "就是今天"
    lines = ["%s 星期%s，%s" % (
        target.isoformat(), _WEEK[target.weekday()], rel)]
    if delta:
        lines[0] += "（%+d 天）" % delta
    try:
        anniv = date(base.year, target.month, target.day)
        if anniv < base:
            anniv = date(base.year + 1, target.month, target.day)
        lines.append("下一个周年还剩 %d 天（%s）" % (
            (anniv - base).days, anniv.isoformat()))
    except ValueError:
        lines.append("2 月 29 日：平年没有对应周年")
    return _ok("\n".join(lines))


_TOOLS = {
    "fun_hash": _hash,
    "fun_password": _password,
    "fun_color": _color,
    "fun_unit": _unit,
    "fun_morse": _morse,
    "fun_codec": _codec,
    "fun_random": _random,
    "fun_date": _date,
}

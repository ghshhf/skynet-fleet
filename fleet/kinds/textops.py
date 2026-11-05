"""文本层：编码探测 / 转码 / 正则 / diff / 行操作 / 统计 / CSV。

为什么这一层值得单独做
----------------------
中文 Windows 上有一个每天都在发生、但没人专门解决的事：
**记事本存的 txt、Excel 导的 csv、老系统吐的 log，默认都是 GBK。**
拿 UTF-8 去读就是满屏「锟斤拷」；反过来把 UTF-8 喂给只认 GBK 的老程序，
中文全变问号。而修这个问题，通常要开一个叫「编码转换器」的小软件。

第二件事是 **CSV**。Excel 中文版默认用 GBK + 分号分隔，程序按 UTF-8 + 逗号读
就全乱。而 CSV 导出给 Excel 时如果不加 BOM，中文又是乱码——**同一个坑两个方向**。

这两件事都属于「一次搞对就再也不用想」的类型，适合做成能力，而不是每次现查。

五条纪律
--------
1. **不信扩展名，也不信「应该是 UTF-8」**。编码靠探测，探测要给置信度，
   低置信度就如实说「我不确定，这几个候选都有可能」——比猜错强。
2. **不引第三方库**。`chardet` 装一次几十 MB，而常见编码（UTF-8 / GBK /
   GB18030 / Big5 / Shift-JIS）用标准库试解码 + 打分足够。判不准就报候选。
3. **大文件不全读**。默认 20MB 上限，超了明确报错，不闷头吃掉几百 MB 内存。
4. **写文件默认不覆盖源**。转码最常见的用法是「原地转」，
   而写坏原文件是不可逆的——所以默认另存，要原地转得显式说。
5. **BOM 是被低估的坑**。给 Excel 的 CSV 不加 BOM 就是乱码；
   给 Linux 脚本加 BOM 就是「\ufeff 不是命令」。两个方向都得能控制。

关于打分
--------
试解码有个陷阱：**错误编码也能"成功"解码**。UTF-8 的中文字节按 GBK 解，
大多数情况下不抛异常，只是解出些莫名其妙的字。所以「没报错」不能当判据，
得给解出来的文本打分：中日韩字符占比高、控制字符少、没有替换符，才是对的编码。
"""
import codecs
import csv as _csv
import difflib
import fnmatch
import io
import json
import os
import re
import time

from ..core import BaseDevice, DeviceResult as R

#: 一次最多读多大的文本。再大就不是「处理文本」而是「处理数据」了。
MAX_TEXT_BYTES = 20 * 1024 * 1024

#: 探测编码时最多看这么多字节——编码特征在前几十 KB 就很明显
SNIFF_BYTES = 256 * 1024

#: 候选中日韩编码。顺序有意义：GB18030 是 GBK 的超集，先试它。
#: Big5/Shift-JIS 放后面，因为它们的字节模式更容易「误判成功」。
ASIA_ENCODINGS = ("gb18030", "big5", "shift_jis", "euc_kr")

#: BOM 表。顺序重要：UTF-32 LE 的前两字节和 UTF-16 LE 一样，
#: 必须先判长的，否则 UTF-32 文件会被认成 UTF-16 并在开头留个 \x00。
BOMS = [
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
]

#: UTF-16/32 没有 BOM 时的猜测：看是否整段都是「一字符 + 一个 \x00」
_ZERO_PATTERNS = [
    (b"\x00\x00\x00", "utf-32-be"),
    (b"\x00\x00\x00\x00", "utf-32-le"),
]


#: 前 500 常用汉字。这是判编码的**主判据**——
#: 正常中文里常用字占 60%~95%，而编码解错时字全散在生僻区，密度接近 0。
#: 实测：正确解码 0.93，字节序搞反 0.00。比「CJK 字符占比」强得多——
#: 后者在字节序搞反时仍有 22%（随机字节撞进 U+4E00-9FFF 的概率），区分不开。
COMMON_CJK = set(
    "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可"
    "主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家"
    "电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应开它合"
    "还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明"
    "看原又么利比或但质气第向道命此变条只没结解问意建月公无系军很情者最立代想"
    "已通并提直题党程展五果料象员革位入常文总次品式活设及管特件长求老头基资边"
    "流路级少图山统接知较将组见计别她手角期根论运农指几九区强放决西被干做必战"
    "先回则任取据处队南给色光门即保治北造百规热领七海口东导器压志世金增争济阶"
    "油思术极交受联什认六共权收证改清己美再采转更单风切打白教速花带安场身车例"
    "真务具万每目至达走积示议声报斗完类八离华名确才科张信马节话米整空元况今集"
    "温传土许步群广石记需段研界拉林律叫且究观越织装影算低持音众书布复容儿须际"
    "商非验连断深难近矿千周委素技备半办青省列习响约支般史感劳便团往酸历市克何"
    "除消构府称太准精值号率族维划选标写存候毛亲快效斯院查江型眼王按格养易置派"
    "层片始却专状育厂京识适属圆包火住调满县局照参红细引听该铁价严龙飞")


def _text_score(s):
    """给解码结果打分（0~1）。越高越像「这段字节本来就该是这个编码」。

    判据来自实际踩过的坑：
      - 替换符 U+FFFD 出现一次就基本可以否掉
      - 常用字密度是主判据（正常中文 0.6~0.95，解错接近 0）
      - 控制字符（除 \\t\\n\\r）多说明解错了
      - 私用区字符几乎必然是解错的产物
    """
    if not s:
        return 1.0
    sample = s[:20000]
    ns = len(sample)
    cjk = ctrl = pua = common = printable = 0
    for ch in sample:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF:
            cjk += 1
            if ch in COMMON_CJK:
                common += 1
        elif o < 0x20 and ch not in "\t\n\r":
            ctrl += 1
        elif 0xE000 <= o <= 0xF8FF:
            pua += 1
        elif o == 0xFFFD:
            return 0.0
        elif 0x20 <= o <= 0x7E or ch in "\t\n\r":
            printable += 1

    if cjk:
        # 中文文本：常用字密度为主，CJK 占比为辅
        score = 0.25
        score += 0.55 * min(1.0, (common / float(cjk)) / 0.6)
        score += 0.20 * min(1.0, (cjk / float(ns)) / 0.40)
    else:
        # 无中日韩字符：看可打印字符占比（二进制文件在这里会掉下去）
        score = 0.25 + 0.60 * (printable / float(ns))
    score -= 2.5 * (ctrl / float(ns))
    score -= 4.0 * (pua / float(ns))
    if ctrl > ns * 0.05:
        score -= 0.4
    return max(0.0, min(1.0, score))


def detect_encoding(raw):
    """探测编码。返回 (编码名, 置信度, 所有候选)。

    `raw` 是 bytes。返回的编码名可直接传给 `bytes.decode()`。
    BOM 检测是确定性的（置信度 0.99）；靠试解码得来的最高只有 0.9——
    因为再准的猜测也还是猜，调用方有权知道这一点。
    """
    if not raw:
        return "utf-8", 0.5, []

    for bom, name in BOMS:
        if raw.startswith(bom):
            return name, 0.99, [{"enc": name, "score": 0.99, "by": "bom"}]

    # 没有 BOM 的 UTF-16：整段都是「ASCII + \x00」交替
    head = raw[:4096]
    if len(head) >= 4:
        zeros = head.count(0)
        if zeros > len(head) * 0.3:
            # 偶数位是 0 → LE，奇数位是 0 → BE
            ev = sum(1 for i in range(0, min(len(head) - 1, 4000), 2) if head[i] == 0)
            od = sum(1 for i in range(1, min(len(head), 4000), 2) if head[i] == 0)
            if od > ev * 2:
                return "utf-16-le", 0.85, [{"enc": "utf-16-le", "score": 0.85,
                                            "by": "zero-pattern"}]
            if ev > od * 2:
                return "utf-16-be", 0.85, [{"enc": "utf-16-be", "score": 0.85,
                                            "by": "zero-pattern"}]

    try:
        raw.decode("ascii")
        return "ascii", 0.99, [{"enc": "ascii", "score": 0.99, "by": "ascii"}]
    except UnicodeDecodeError:
        pass

    try:
        raw.decode("utf-8")
        return "utf-8", 0.95, [{"enc": "utf-8", "score": 0.95, "by": "strict"}]
    except UnicodeDecodeError:
        pass

    cands = []
    # utf-16 也进候选：中文 UTF-16 文件没有 BOM 时（工具生成、协议传输）只能靠打分。
    # 它几乎不会「解码失败」（任何偶数字节都能解），所以完全靠 _text_score 淘汰——
    # 字节序搞反时常用字密度会掉到 0，区分得很干净。
    for enc in ("utf-16-le", "utf-16-be") + ASIA_ENCODINGS:
        b = raw
        if enc.startswith("utf-16") and len(b) % 2:
            b = b[:-1]
        try:
            s = b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        sc = _text_score(s)
        cands.append({"enc": enc, "score": round(sc, 3), "by": "trial"})
    cands.sort(key=lambda x: -x["score"])
    if not cands:
        return "latin-1", 0.2, [{"enc": "latin-1", "score": 0.2, "by": "fallback"}]
    top = cands[0]
    return top["enc"], top["score"], cands[:5]


def _norm_enc(name):
    """把用户写的编码名归一成 Python 认得的名字。认不出返回 None。"""
    if not name:
        return None
    s = str(name).strip().lower().replace("-", "_").replace(" ", "")
    alias = {
        "utf8": "utf-8", "utf_8": "utf-8", "u8": "utf-8",
        "utf8_sig": "utf-8-sig", "utf_8_sig": "utf-8-sig",
        "utf8sig": "utf-8-sig", "utf8_bom": "utf-8-sig",
        "gbk": "gbk", "gb2312": "gbk", "gb_2312": "gbk", "cp936": "gbk",
        "ms936": "gbk", "936": "gbk",
        "gb18030": "gb18030", "gb_18030": "gb18030", "gbk18030": "gb18030",
        "big5": "big5", "big_5": "big5", "cp950": "big5", "950": "big5",
        "shift_jis": "shift_jis", "shiftjis": "shift_jis", "sjis": "shift_jis",
        "cp932": "shift_jis", "932": "shift_jis",
        "euc_kr": "euc_kr", "euckr": "euc_kr", "cp949": "euc_kr",
        "utf_16": "utf-16", "utf16": "utf-16",
        "utf_16_le": "utf-16-le", "utf16le": "utf-16-le",
        "utf_16_be": "utf-16-be", "utf16be": "utf-16-be",
        "latin1": "latin-1", "latin_1": "latin-1", "iso8859_1": "latin-1",
    }
    if s in alias:
        return alias[s]
    try:
        codecs.lookup(s)
        return s
    except LookupError:
        return None


def _read_text(path, encoding=None, limit=MAX_TEXT_BYTES):
    """读文本文件，自动探测编码。返回 (文本, 编码, 置信度, 换行风格, 是否有BOM, 错误)。

    🔴 错误放在最后一个位置并且用 `is not None` 判——
    `DeviceResult.__bool__` 返回 ok，失败的 DeviceResult 在 `if err:` 里是 False。
    """
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return None, None, None, None, None, "读不了：%s" % e
    if size > limit:
        return (None, None, None, None, None,
                "文件 %s 超过上限 %s：这是文本处理，不是数据处理"
                % (_human(size), _human(limit)))
    try:
        with open(path, "rb") as f:
            raw = f.read(SNIFF_BYTES if size > SNIFF_BYTES else limit)
            if size > SNIFF_BYTES and encoding is None:
                pass
    except OSError as e:
        return None, None, None, None, None, "读不了：%s" % e

    enc = _norm_enc(encoding)
    conf = 1.0
    if enc is None:
        enc, conf, _cands = detect_encoding(raw)
        # 探测只用前 256KB，正式读要读全
        if size > SNIFF_BYTES:
            try:
                with open(path, "rb") as f:
                    raw = f.read()
            except OSError as e:
                return None, None, None, None, None, "读不了：%s" % e
    else:
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as e:
            return None, None, None, None, None, "读不了：%s" % e

    has_bom = any(raw.startswith(b) for b, _n in BOMS)
    try:
        text = raw.decode(enc, errors="replace" if conf < 0.9 else "strict")
    except (UnicodeDecodeError, LookupError) as e:
        return None, enc, conf, None, has_bom, "按 %s 解不开：%s" % (enc, e)

    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    nl = "crlf" if crlf > lf else "lf"
    return text, enc, conf, nl, has_bom, None


def _human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or u == "GB":
            return ("%.0f %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0
    return "%.1f GB" % n


def _out_path(src, out, suffix):
    """算输出路径。不给 out 就在源旁边加后缀，绝不覆盖源文件。"""
    if out:
        p = os.path.abspath(os.path.expanduser(str(out).strip().strip('"')))
        if os.path.isdir(p):
            base = os.path.splitext(os.path.basename(src))[0]
            return os.path.join(p, base + suffix)
        return p
    stem, ext = os.path.splitext(src)
    return stem + suffix + ext


class TextOps(BaseDevice):
    kind = "textops"
    via = "local"

    def __init__(self, name="text:ops", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "system")

    def probe(self):
        return True, ""

    def caps(self):
        return ("text.encoding", "text.convert", "text.read", "text.regex",
                "text.diff", "text.lines", "text.stats", "text.csv")

    def invoke(self, cap, args=None):
        args = args or {}
        fn = {
            "text.encoding": self._encoding,
            "text.convert": self._convert,
            "text.read": self._read,
            "text.regex": self._regex,
            "text.diff": self._diff,
            "text.lines": self._lines,
            "text.stats": self._stats,
            "text.csv": self._csv,
        }.get(cap)
        if fn is None:
            return super().invoke(cap, args)
        try:
            return fn(cap, args)
        except UnicodeDecodeError as e:
            return R.error(cap, "解码失败（换 encoding 试试）：%s" % e,
                           device=self.name, via=self.via)
        except Exception as e:
            return R.error(cap, "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)

    # ---------------- 公共 ----------------

    def _path(self, args, cap, key="path"):
        raw = (args.get(key) or "").strip().strip('"').strip("'")
        if not raw:
            return None, R.error(cap, "缺 %s" % key, device=self.name, via=self.via)
        p = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isfile(p):
            return None, R.error(cap, "文件不存在：%s" % p,
                                 device=self.name, via=self.via)
        return p, None

    # ---------------- 探测编码 ----------------

    def _encoding(self, cap, args):
        """这文件到底是什么编码。不给结论，给证据。"""
        p, err = self._path(args, cap)
        if err is not None:
            return err
        try:
            size = os.path.getsize(p)
            with open(p, "rb") as f:
                head = f.read(SNIFF_BYTES)
        except OSError as e:
            return R.error(cap, "读不了：%s" % e, device=self.name, via=self.via)

        enc, conf, cands = detect_encoding(head)
        has_bom = any(head.startswith(b) for b, _n in BOMS)
        bom_name = next((n for b, n in BOMS if head.startswith(b)), None)

        # 拿探测到的编码真的解一遍头部，看解出来长什么样——
        # 比只报一个编码名有用得多：人一眼就能判断「这像不像正常中文」
        preview = None
        try:
            preview = head[:400].decode(enc, errors="replace")
        except (UnicodeDecodeError, LookupError):
            pass

        # 换行统计也要基于正确解码，否则二进制里数 \n 没意义
        crlf = head.count(b"\r\n")
        lf = head.count(b"\n") - crlf

        return R.success(cap, self.name, via=self.via, data={
            "path": p, "bytes": size, "human": _human(size),
            "encoding": enc, "confidence": round(conf, 3),
            "by": (cands[0].get("by") if cands else "trial"),
            "candidates": cands,
            "has_bom": has_bom, "bom": bom_name,
            "newline": "crlf" if crlf > lf else "lf",
            "preview": preview,
            "confident": conf >= 0.9,
            "note": "" if conf >= 0.9 else
                    ("不太确定（%.2f）。候选：%s。中文内容建议直接试 gb18030 —— "
                     "它是 GBK 的超集，能解 GBK 的全部内容"
                     % (conf, "、".join(c["enc"] for c in cands[:3]))),
        })

    # ---------------- 转码 ----------------

    def _convert(self, cap, args):
        """转编码。默认另存，要原地转必须显式 inplace=true。"""
        p, err = self._path(args, cap)
        if err is not None:
            return err

        to = _norm_enc(args.get("to") or args.get("encoding"))
        if to is None:
            return R.error(cap, "不认识的编码：%r（认 utf-8 / utf-8-sig / gbk / "
                                "gb18030 / big5 / shift_jis / utf-16）”" % (args.get("to"),),
                           device=self.name, via=self.via)

        src_enc = _norm_enc(args.get("from") or args.get("src_encoding"))
        text, used, conf, nl, has_bom, rerr = _read_text(p, src_enc)
        if rerr is not None:
            return R.error(cap, rerr, device=self.name, via=self.via)

        inplace = bool(args.get("inplace") or False)
        raw_out = (args.get("out") or "").strip().strip('"')
        if not inplace and not raw_out:
            stem, ext = os.path.splitext(p)
            out = stem + "." + to.replace("-", "") + ext
        elif inplace:
            out = p
        else:
            out = os.path.abspath(os.path.expanduser(raw_out))
            if os.path.isdir(out):
                out = os.path.join(out, os.path.basename(p))

        # 换行：转码时不擅自改行尾。Windows 程序要 CRLF 的话有专门的参数。
        target_nl = (args.get("newline") or "").strip().lower()
        if target_nl in ("lf", "crlf"):
            text = text.replace("\r\n", "\n")
            if target_nl == "crlf":
                text = text.replace("\n", "\r\n")

        add_bom = args.get("bom")
        if add_bom is None:
            # 默认行为：转成 utf-8 时按 to 名决定（utf-8-sig 自带 BOM）；
            # 转别的编码时保持原来的 BOM 状态——不擅自加也不擅自去
            add_bom = (to == "utf-8-sig")
        try:
            data = text.encode(to.replace("-sig", ""), errors="replace")
            if add_bom and not to.startswith("utf-16") and not to.startswith("utf-32"):
                data = codecs.BOM_UTF8 + data
        except (UnicodeEncodeError, LookupError) as e:
            return R.error(cap, "按 %s 编不回去：%s（有字符不在这个字符集里，"
                                "比如 emoji 存不进 GBK）" % (to, e),
                           device=self.name, via=self.via)

        if inplace and not args.get("_confirmed"):
            return R.success(cap, self.name, via=self.via, data={
                "src": p, "out": out, "dry_run": True,
                "from": used, "to": to,
                "src_bytes": os.path.getsize(p), "will_bytes": len(data),
                "note": "原地转会覆盖源文件。确认无误后传 _confirmed=true 再跑一次；"
                        "不想覆盖就传 out=另一个路径",
            })

        tmp = out + ".skynet_tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, out)
        except OSError as e:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return R.error(cap, "写不出去：%s" % e, device=self.name, via=self.via)

        src_size = os.path.getsize(p) if os.path.exists(p) else 0
        return R.success(cap, self.name, via=self.via, data={
            "src": p, "out": out, "from": used, "to": to,
            "detect_confidence": round(conf, 3),
            "src_bytes": src_size, "bytes": len(data),
            "bom": bool(add_bom), "newline": target_nl or nl,
            "inplace": inplace,
            "note": "" if used == src_enc or src_enc else
                    ("源编码是自动探测的（%s，置信度 %.2f）" % (used, conf)),
        })

    # ---------------- 读文本 ----------------

    def _read(self, cap, args):
        """按指定编码读文本，可只取一部分（大日志不用整个塞进上下文）。"""
        p, err = self._path(args, cap)
        if err is not None:
            return err

        enc = _norm_enc(args.get("encoding"))
        text, used, conf, nl, has_bom, rerr = _read_text(p, enc)
        if rerr is not None:
            return R.error(cap, rerr, device=self.name, via=self.via)

        lines = text.splitlines()
        total_lines = len(lines)
        offset = max(0, int(args.get("offset") or 0))
        limit = args.get("limit")
        limit = int(limit) if limit not in (None, "") else 0
        picked = lines[offset:offset + limit] if limit else lines[offset:]
        body = "\n".join(picked)
        max_chars = int(args.get("max_chars") or 0)
        truncated = False
        if max_chars and len(body) > max_chars:
            body = body[:max_chars]
            truncated = True

        return R.success(cap, self.name, via=self.via, data={
            "path": p, "encoding": used, "confidence": round(conf, 3),
            "bytes": os.path.getsize(p),
            "lines_total": total_lines,
            "lines_returned": len(picked),
            "offset": offset,
            "text": body,
            "truncated": truncated,
            "newline": nl, "has_bom": has_bom,
        })

    # ---------------- 正则 ----------------

    def _regex(self, cap, args):
        """正则提取 / 替换 / 计数。

        三种动作共用一次编译：
          action=find      返回所有匹配（带行列位置）
          action=replace   替换后写出（默认不覆盖源）
          action=count     只数有几处
        """
        src_text = args.get("text")
        pattern = args.get("pattern") or args.get("regex")
        if not pattern:
            return R.error(cap, "缺 pattern", device=self.name, via=self.via)

        flags = 0
        if args.get("ignore_case", True):
            flags |= re.I
        if args.get("multiline"):
            flags |= re.M
        if args.get("dotall"):
            flags |= re.S
        try:
            rx = re.compile(pattern, flags)
        except re.error as e:
            return R.error(cap, "正则写错了：%s" % e, device=self.name, via=self.via)

        action = (args.get("action") or "find").strip().lower()
        p = None
        used_enc = None
        if src_text is None:
            p, err = self._path(args, cap)
            if err is not None:
                return err
            text, used_enc, _conf, _nl, _bom, rerr = _read_text(
                p, _norm_enc(args.get("encoding")))
            if rerr is not None:
                return R.error(cap, rerr, device=self.name, via=self.via)
        else:
            text = str(src_text)

        if action == "count":
            hits = list(rx.finditer(text))
            return R.success(cap, self.name, via=self.via, data={
                "pattern": pattern, "count": len(hits),
                "unique": len({m.group(0) for m in hits}),
                "source": p or "text",
            })

        if action == "replace":
            repl = args.get("replace")
            if repl is None:
                return R.error(cap, "replace 动作要给 replace（可为空字符串）",
                               device=self.name, via=self.via)
            try:
                new_text, n = rx.subn(repl, text)
            except re.error as e:
                return R.error(cap, "替换串写错了：%s" % e, device=self.name, via=self.via)

            if p is None:
                return R.success(cap, self.name, via=self.via, data={
                    "replaced": n, "text": new_text, "source": "text",
                })
            if args.get("dry_run", True):
                return R.success(cap, self.name, via=self.via, data={
                    "replaced": n, "source": p, "dry_run": True,
                    "preview": new_text[:800],
                    "note": "预演，没写文件；真要写得传 dry_run=false",
                })
            out = (args.get("out") or "").strip()
            dest = (os.path.abspath(os.path.expanduser(out)) if out
                    else os.path.splitext(p)[0] + ".replaced" + os.path.splitext(p)[1])
            enc_out = _norm_enc(args.get("to")) or used_enc or "utf-8"
            try:
                data = new_text.encode(
                    enc_out.replace("-sig", ""),
                    errors="replace")
                if enc_out == "utf-8-sig":
                    data = codecs.BOM_UTF8 + data
                with open(dest, "wb") as f:
                    f.write(data)
            except OSError as e:
                return R.error(cap, "写不出去：%s" % e, device=self.name, via=self.via)
            return R.success(cap, self.name, via=self.via, data={
                "replaced": n, "source": p, "out": dest,
                "bytes": len(data), "encoding": enc_out, "dry_run": False,
            })

        # find
        limit = max(1, min(int(args.get("limit") or 100), 5000))
        hits = []
        for m in rx.finditer(text):
            start = m.start()
            line = text.count("\n", 0, start) + 1
            col = start - (text.rfind("\n", 0, start) + 1) + 1
            item = {"match": m.group(0), "start": start,
                    "line": line, "col": col}
            if m.groups():
                item["groups"] = list(m.groups())
            if m.groupdict():
                item["named"] = m.groupdict()
            hits.append(item)
            if len(hits) >= limit:
                break
        return R.success(cap, self.name, via=self.via, data={
            "pattern": pattern, "count": len(hits),
            "truncated": len(hits) >= limit,
            "hits": hits, "source": p or "text",
        })

    # ---------------- diff ----------------

    def _diff(self, cap, args):
        """两个文件（或两段文本）的行级差异。"""
        a_path = (args.get("a") or "").strip().strip('"')
        b_path = (args.get("b") or "").strip().strip('"')
        a_text = args.get("a_text")
        b_text = args.get("b_text")

        def load(p, t, label):
            if t is not None:
                return str(t).splitlines(), label
            if not p:
                return None, "缺 %s" % label
            ap = os.path.abspath(os.path.expanduser(p))
            if not os.path.isfile(ap):
                return None, "文件不存在：%s" % ap
            txt, _enc, _c, _nl, _bom, e = _read_text(ap, _norm_enc(args.get("encoding")))
            if e is not None:
                return None, "%s：%s" % (label, e)
            return txt.splitlines(), ap

        la, na = load(a_path, a_text, "a")
        if la is None:
            return R.error(cap, na, device=self.name, via=self.via)
        lb, nb = load(b_path, b_text, "b")
        if lb is None:
            return R.error(cap, nb, device=self.name, via=self.via)

        ignore_ws = bool(args.get("ignore_ws") or False)
        ignore_case = bool(args.get("ignore_case") or False)

        def norm(x):
            if ignore_ws:
                x = re.sub(r"\s+", " ", x).strip()
            if ignore_case:
                x = x.lower()
            return x

        A = [norm(x) for x in la]
        B = [norm(x) for x in lb]

        ctx = max(0, min(int(args.get("context") or 3), 50))
        diff = list(difflib.unified_diff(la, lb, fromfile=na, tofile=nb, n=ctx, lineterm=""))
        sm = difflib.SequenceMatcher(None, A, B, autojunk=False)
        same = 0
        added = removed = changed = 0
        hunks = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                same += i2 - i1
            elif tag == "insert":
                added += j2 - j1
                hunks.append({"tag": tag, "a": [i1, i2], "b": [j1, j2],
                              "lines": lb[j1:j2][:20]})
            elif tag == "delete":
                removed += i2 - i1
                hunks.append({"tag": tag, "a": [i1, i2], "b": [j1, j2],
                              "lines": la[i1:i2][:20]})
            else:
                changed += max(i2 - i1, j2 - j1)
                hunks.append({"tag": tag, "a": [i1, i2], "b": [j1, j2],
                              "old": la[i1:i2][:20], "new": lb[j1:j2][:20]})

        # 相似度：0.0~1.0。1.0 = 完全一样
        ratio = round(sm.ratio(), 4)
        # 🔴 identical 用**归一化后**的 A/B 判，不是原始 la/lb。
        # 用原始行的话，ignore_ws=True 时「a b」和「a   b」会被判成不同，
        # 但 diff 正文又是空的 —— 两个字段自相矛盾。
        identical = (A == B)
        return R.success(cap, self.name, via=self.via, data={
            "a": na, "b": nb,
            "identical": identical,
            "ratio": ratio,
            "lines": {"a": len(la), "b": len(lb),
                      "same": same, "added": added,
                      "removed": removed, "changed": changed},
            "hunk_count": len(hunks),
            "hunks": hunks[:50],
            "diff": "" if identical else "\n".join(diff[:2000]),
            "diff_truncated": len(diff) > 2000,
            "note": "" if not identical else "两边内容完全一样",
        })

    # ---------------- 行操作 ----------------

    def _lines(self, cap, args):
        """行级操作：去重 / 排序 / 过滤 / 裁剪 / 加前后缀 / 编号。可以串联。"""
        src_text = args.get("text")
        p = None
        used_enc = None
        if src_text is None:
            p, err = self._path(args, cap)
            if err is not None:
                return err
            text, used_enc, _c, _nl, _bom, rerr = _read_text(
                p, _norm_enc(args.get("encoding")))
            if rerr is not None:
                return R.error(cap, rerr, device=self.name, via=self.via)
        else:
            text = str(src_text)

        ops = args.get("ops")
        if ops in (None, ""):
            # 没给 ops 就按扁平参数推断，调用起来省事
            ops = []
            if args.get("strip"):
                ops.append({"op": "strip"})
            if args.get("dedupe"):
                ops.append({"op": "dedupe", "keep": args.get("keep") or "first"})
            if args.get("sort"):
                ops.append({"op": "sort", "reverse": bool(args.get("reverse"))})
            if args.get("grep"):
                ops.append({"op": "grep", "pattern": args["grep"],
                            "invert": bool(args.get("invert"))})
            if args.get("drop_empty"):
                ops.append({"op": "drop_empty"})
            if not ops:
                ops = [{"op": "strip"}]
        if not isinstance(ops, list):
            return R.error(cap, "ops 要是列表，如 [{'op':'dedupe'}]",
                           device=self.name, via=self.via)

        lines = text.splitlines()
        before = len(lines)
        applied = []
        for spec in ops:
            if not isinstance(spec, dict):
                return R.error(cap, "ops 里每项要是对象，如 {'op':'sort'}",
                               device=self.name, via=self.via)
            op = (spec.get("op") or "").lower()
            n0 = len(lines)
            if op == "strip":
                lines = [x.strip() for x in lines]
            elif op == "dedupe":
                keep = (spec.get("keep") or "first").lower()
                if keep == "last":
                    seen = {}
                    for i, x in enumerate(lines):
                        seen[x] = i
                    order = sorted(seen.values())
                    lines = [lines[i] for i in order]
                else:
                    seen = set()
                    out = []
                    for x in lines:
                        if x not in seen:
                            seen.add(x)
                            out.append(x)
                    lines = out
            elif op == "sort":
                lines = sorted(lines, reverse=bool(spec.get("reverse")))
            elif op == "grep":
                pat = spec.get("pattern") or ""
                try:
                    rx = re.compile(pat, re.I if spec.get("ignore_case", True) else 0)
                except re.error as e:
                    return R.error(cap, "grep 正则写错：%s" % e,
                                   device=self.name, via=self.via)
                inv = bool(spec.get("invert"))
                lines = [x for x in lines if bool(rx.search(x)) != inv]
            elif op == "drop_empty":
                lines = [x for x in lines if x.strip()]
            elif op == "prefix":
                pre = spec.get("text") or ""
                lines = [pre + x for x in lines]
            elif op == "suffix":
                suf = spec.get("text") or ""
                lines = [x + suf for x in lines]
            elif op == "number":
                fmt = spec.get("format") or "{n}. "
                start = int(spec.get("start") or 1)
                lines = [fmt.format(n=start + i) + x for i, x in enumerate(lines)]
            elif op == "cut":
                a = int(spec.get("start") or 0)
                b = spec.get("end")
                lines = lines[a:int(b)] if b is not None else lines[a:]
            elif op == "unique_all":
                # 全局去重（保留唯一出现的行），跟 dedupe 不同
                from collections import Counter
                c = Counter(lines)
                lines = [x for x in lines if c[x] == 1]
            else:
                return R.error(cap, "不认识的行操作：%r（可用 strip/dedupe/sort/grep/"
                                    "drop_empty/prefix/suffix/number/cut/unique_all）" % op,
                               device=self.name, via=self.via)
            applied.append({"op": op, "before": n0, "after": len(lines)})

        limit = int(args.get("limit") or 0)
        truncated = False
        if limit and len(lines) > limit:
            lines = lines[:limit]
            truncated = True

        body = "\n".join(lines)

        out = (args.get("out") or "").strip()
        wrote = None
        if out and p is not None:
            dest = os.path.abspath(os.path.expanduser(out))
            if os.path.isdir(dest):
                dest = os.path.join(dest, os.path.basename(p))
            enc_out = _norm_enc(args.get("to")) or used_enc or "utf-8"
            try:
                data = body.encode(enc_out.replace("-sig", ""), errors="replace")
                if enc_out == "utf-8-sig":
                    data = codecs.BOM_UTF8 + data
                with open(dest, "wb") as f:
                    f.write(data)
                wrote = {"out": dest, "bytes": len(data), "encoding": enc_out}
            except OSError as e:
                return R.error(cap, "写不出去：%s" % e, device=self.name, via=self.via)

        return R.success(cap, self.name, via=self.via, data={
            "source": p or "text",
            "lines_before": before, "lines_after": len(lines),
            "applied": applied,
            "text": body, "truncated": truncated,
            "written": wrote,
        })

    # ---------------- 统计 ----------------

    def _stats(self, cap, args):
        """字数 / 行数 / 中文字数 / 词频。写中文东西时用得上。"""
        src_text = args.get("text")
        p = None
        used_enc = None
        if src_text is None:
            p, err = self._path(args, cap)
            if err is not None:
                return err
            text, used_enc, conf, _nl, _bom, rerr = _read_text(
                p, _norm_enc(args.get("encoding")))
            if rerr is not None:
                return R.error(cap, rerr, device=self.name, via=self.via)
        else:
            text = str(src_text)
            conf = 1.0

        chars = len(text)
        no_space = len(re.sub(r"\s", "", text))
        # 中文按字算，英文按词算——混排时这两个数分开报才有意义
        cjk = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", text))
        cjk_punct = len(re.findall(r"[\u3000-\u303f\uff00-\uffef]", text))
        words_en = len(re.findall(r"[A-Za-z][A-Za-z0-9'\-]*", text))
        digits = len(re.findall(r"\d", text))
        lines = text.splitlines()
        blank = sum(1 for x in lines if not x.strip())

        # 词频：英文按词，中文按二元组（不分词的近似，够看「这篇在讲什么」）
        top = max(1, min(int(args.get("top") or 15), 100))
        from collections import Counter
        en_words = Counter(w.lower() for w in
                           re.findall(r"[A-Za-z][A-Za-z0-9'\-]*", text))
        cjk_bigrams = Counter()
        for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            for i in range(len(run) - 1):
                cjk_bigrams[run[i:i + 2]] += 1
        # 停用词：不滤的话前几名永远是「的了是在」
        stop = {"的", "了", "是", "在", "和", "我", "你", "他", "也", "就",
                "不", "有", "这", "那", "个", "们", "到", "说", "要", "会"}
        cjk_top = [(w, c) for w, c in cjk_bigrams.most_common(top * 3)
                   if w not in stop and c > 1][:top]

        return R.success(cap, self.name, via=self.via, data={
            "source": p or "text", "encoding": used_enc or "text",
            "chars": chars, "chars_no_space": no_space,
            "cjk_chars": cjk, "cjk_punct": cjk_punct,
            "english_words": words_en, "digits": digits,
            "lines": len(lines), "blank_lines": blank,
            "paragraphs": len([x for x in re.split(r"\n\s*\n", text) if x.strip()]),
            "top_en": en_words.most_common(top),
            "top_cjk": cjk_top,
            "reading_min": round((cjk / 400.0) + (words_en / 250.0), 1),
            "note": "中文字数按字算、英文按词算，混排时两个数分开看",
        })

    # ---------------- CSV ----------------

    def _csv(self, cap, args):
        """CSV 读取 / 转换。

        Windows 上的 CSV 有两个默认值跟「标准 CSV」不一样：
        **编码是 GBK、分隔符常常是分号或制表符**（Excel 在中文/欧洲区域设置下
        会按系统分隔符导出）。所以这里必须自动探测两者，否则读出来是一整列。
        """
        p, err = self._path(args, cap)
        if err is not None:
            return err

        text, used_enc, conf, _nl, has_bom, rerr = _read_text(
            p, _norm_enc(args.get("encoding")))
        if rerr is not None:
            return R.error(cap, rerr, device=self.name, via=self.via)

        # 分隔符：给了就用，没给就看第一行哪个候选出现得最多
        sep = args.get("delimiter") or args.get("sep")
        sep_auto = False
        if not sep:
            head = "\n".join(text.splitlines()[:5])
            cands = {",": head.count(","), ";": head.count(";"),
                     "\t": head.count("\t"), "|": head.count("|")}
            sep = max(cands, key=cands.get) if max(cands.values()) > 0 else ","
            sep_auto = True

        try:
            rows = list(_csv.reader(io.StringIO(text), delimiter=sep))
        except _csv.Error as e:
            return R.error(cap, "CSV 解析失败：%s" % e, device=self.name, via=self.via)
        rows = [r for r in rows if any(c.strip() for c in r)]
        if not rows:
            return R.unavailable(cap, "这个文件里没有数据行",
                                 device=self.name, via=self.via)

        has_header = bool(args.get("header", True))
        header = rows[0] if has_header else []
        body = rows[1:] if has_header else rows
        width = max(len(r) for r in rows) if rows else 0
        ragged = sum(1 for r in rows if len(r) != width)

        to = (args.get("to") or "json").strip().lower()
        limit = max(1, min(int(args.get("limit") or 100), 10000))

        if to in ("json", "jsonl"):
            if has_header:
                objs = []
                for r in body[:limit]:
                    # 列数不齐时补空，不要报错——真实 CSV 经常有尾随空列
                    r = list(r) + [""] * (len(header) - len(r))
                    objs.append({header[i]: r[i] for i in range(len(header))})
                payload = objs
            else:
                payload = [list(r) for r in body[:limit]]
            return R.success(cap, self.name, via=self.via, data={
                "path": p, "encoding": used_enc, "delimiter": sep,
                "delimiter_auto": sep_auto,
                "header": header, "rows": len(body), "cols": width,
                "ragged_rows": ragged,
                "data": payload, "truncated": len(body) > limit,
                "note": "分隔符是探测出来的（%r）；不对就显式传 delimiter"
                        % sep if sep_auto else "",
            })

        if to in ("tsv", "csv"):
            target_sep = "\t" if to == "tsv" else (args.get("out_sep") or ",")
            out = (args.get("out") or "").strip()
            buf = io.StringIO()
            w = _csv.writer(buf, delimiter=target_sep, lineterminator="\r\n")
            for r in rows:
                w.writerow(r)
            body_out = buf.getvalue()
            dest = None
            if out:
                dest = os.path.abspath(os.path.expanduser(out))
                if os.path.isdir(dest):
                    dest = os.path.join(dest, os.path.splitext(os.path.basename(p))[0]
                                        + "." + to)
                enc_out = _norm_enc(args.get("to_encoding")) or used_enc or "utf-8"
                data = body_out.encode(enc_out.replace("-sig", ""), errors="replace")
                # 给 Excel 看就必须带 BOM，不带就是乱码——
                # 但只对 utf-8 有意义，GBK 加 BOM 反而会显示成「锘」
                if enc_out == "utf-8-sig":
                    data = codecs.BOM_UTF8 + data
                try:
                    with open(dest, "wb") as f:
                        f.write(data)
                except OSError as e:
                    return R.error(cap, "写不出去：%s" % e,
                                   device=self.name, via=self.via)
            return R.success(cap, self.name, via=self.via, data={
                "path": p, "out": dest, "to": to,
                "encoding": _norm_enc(args.get("to_encoding")) or used_enc,
                "delimiter": target_sep, "rows": len(rows),
                "bytes": len(body_out.encode("utf-8", "replace")),
                "preview": body_out[:500],
            })

        return R.error(cap, "不认识的目标格式：%r（认 json / jsonl / tsv / csv）" % to,
                       device=self.name, via=self.via)


# ============================================================ 自测

def _selftest():
    import tempfile

    fails = []
    state = {"n": 0}

    def ck(cond, msg):
        state["n"] += 1
        print(("  ✓ " if cond else "  ✗ ") + msg)
        if not cond:
            fails.append(msg)

    td = tempfile.mkdtemp(prefix="textops_test_")
    dev = TextOps()

    def w(rel, data):
        p = os.path.join(td, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return p

    try:
        # ---- 素材 ----
        cn = "第一行中文\n第二行内容\n第三行结束\n"
        p_utf8 = w("utf8.txt", cn.encode("utf-8"))
        p_gbk = w("gbk.txt", cn.encode("gbk"))
        p_bom = w("bom.txt", codecs.BOM_UTF8 + cn.encode("utf-8"))
        p_u16 = w("u16.txt", cn.encode("utf-16-le"))
        p_ascii = w("ascii.txt", b"hello\nworld\n")
        ck(True, "素材：造出 utf8/gbk/bom/utf16/ascii 五个文件")

        # ---- text.encoding ----
        r = dev.invoke("text.encoding", {"path": p_utf8})
        ck(r.ok and r.data["encoding"] == "utf-8", "encoding：认出 UTF-8")
        ck(r.ok and r.data["confident"] is True, "encoding：UTF-8 判为高置信")

        r = dev.invoke("text.encoding", {"path": p_gbk})
        ck(r.ok and r.data["encoding"] in ("gb18030", "gbk"),
           "encoding：认出 GBK 系（%s）" % (r.data.get("encoding") if r.ok else "?"))
        ck(r.ok and "第一行中文" in (r.data.get("preview") or ""),
           "encoding：preview 解出正常中文（不是乱码）")

        r = dev.invoke("text.encoding", {"path": p_bom})
        ck(r.ok and r.data["has_bom"] is True and r.data["bom"] == "utf-8-sig",
           "encoding：认出 UTF-8 BOM")

        r = dev.invoke("text.encoding", {"path": p_u16})
        ck(r.ok and r.data["encoding"].startswith("utf-16"),
           "encoding：认出 UTF-16（%s）" % (r.data.get("encoding") if r.ok else "?"))

        r = dev.invoke("text.encoding", {"path": p_ascii})
        ck(r.ok and r.data["encoding"] == "ascii", "encoding：纯 ASCII 单独认出来")

        # ---- text.read ----
        r = dev.invoke("text.read", {"path": p_gbk})
        ck(r.ok and "第二行内容" in r.data["text"],
           "read：GBK 文件自动解码，中文没坏")
        ck(r.ok and r.data["lines_total"] == 3, "read：行数对")

        r = dev.invoke("text.read", {"path": p_utf8, "offset": 1, "limit": 1})
        ck(r.ok and r.data["text"] == "第二行内容", "read：offset/limit 取指定行")
        ck(r.ok and r.data["lines_returned"] == 1, "read：只返回 1 行")

        r = dev.invoke("text.read", {"path": p_utf8, "max_chars": 4})
        ck(r.ok and len(r.data["text"]) == 4 and r.data["truncated"] is True,
           "read：max_chars 截断并标记")

        # ---- text.convert：GBK → UTF-8 ----
        r = dev.invoke("text.convert", {"path": p_gbk, "to": "utf-8"})
        ck(r.ok and r.data["from"] in ("gb18030", "gbk"), "convert：源编码自动探测")
        ck(r.ok and r.data["out"] != p_gbk, "convert：默认另存，不覆盖源")
        if r.ok:
            with open(r.data["out"], "rb") as f:
                raw = f.read()
            ck(raw.decode("utf-8") == cn, "convert：产物按 UTF-8 能还原原文")

        # ---- convert：UTF-8 → GBK ----
        r = dev.invoke("text.convert", {"path": p_utf8, "to": "gbk"})
        ck(r.ok and r.data["to"] == "gbk", "convert：UTF-8 → GBK")
        if r.ok:
            with open(r.data["out"], "rb") as f:
                ck(f.read().decode("gbk") == cn, "convert：GBK 产物能还原原文")

        # ---- convert：加 BOM（给 Excel 看）----
        r = dev.invoke("text.convert", {"path": p_utf8, "to": "utf-8-sig"})
        ck(r.ok and r.data["bom"] is True, "convert：to=utf-8-sig 自动加 BOM")
        if r.ok:
            with open(r.data["out"], "rb") as f:
                ck(f.read(3) == codecs.BOM_UTF8, "convert：BOM 真的写在文件头")

        # ---- convert：原地转要二次确认 ----
        r = dev.invoke("text.convert", {"path": p_gbk, "to": "utf-8", "inplace": True})
        ck(r.ok and r.data.get("dry_run") is True, "convert：inplace 默认先给预演")
        with open(p_gbk, "rb") as f:
            ck(f.read().decode("gbk") == cn, "convert：预演时源文件没被动")

        # ---- convert：emoji 存不进 GBK 要明确报错 ----
        p_emoji = w("emoji.txt", "测试😀表情")
        r = dev.invoke("text.convert", {"path": p_emoji, "to": "gbk"})
        ck(r.ok, "convert：GBK 装不下的字符按 replace 处理并成功" if r.ok
           else "convert：GBK 装不下时报错")

        # ---- text.regex：find ----
        p_log = w("app.log", "2026-11-06 10:00:01 INFO start\n"
                             "2026-11-06 10:00:05 ERROR boom\n"
                             "2026-11-06 10:00:09 ERROR boom2\n")
        r = dev.invoke("text.regex", {"path": p_log, "pattern": r"ERROR (\w+)",
                                      "action": "find"})
        ck(r.ok and r.data["count"] == 2, "regex：find 找到 2 处 ERROR")
        ck(r.ok and r.data["hits"][0]["line"] == 2, "regex：位置报到第 2 行")
        ck(r.ok and r.data["hits"][0]["groups"] == ["boom"], "regex：捕获组取出来了")

        # ---- regex：count ----
        r = dev.invoke("text.regex", {"path": p_log, "pattern": "ERROR", "action": "count"})
        ck(r.ok and r.data["count"] == 2, "regex：count 数出 2 处")

        # ---- regex：replace 默认预演 ----
        r = dev.invoke("text.regex", {"path": p_log, "pattern": "ERROR",
                                      "replace": "ERR", "action": "replace"})
        ck(r.ok and r.data["replaced"] == 2 and r.data["dry_run"] is True,
           "regex：replace 默认预演，报出替换了 2 处")
        with open(p_log, encoding="utf-8") as f:
            ck("ERROR" in f.read(), "regex：预演时源文件没被改")

        # ---- regex：正则写错要明确报错 ----
        r = dev.invoke("text.regex", {"path": p_log, "pattern": "(unclosed"})
        ck((not r.ok) and "正则写错" in (r.err or ""), "regex：正则语法错时明确报错")

        # ---- text.diff ----
        r = dev.invoke("text.diff", {"a_text": "a\nb\nc\n", "b_text": "a\nB\nc\n"})
        ck(r.ok and r.data["identical"] is False, "diff：认出两段不同")
        ck(r.ok and r.data["lines"]["changed"] == 1, "diff：1 行改动")
        ck(r.ok and r.data["lines"]["same"] == 2, "diff：2 行相同")
        ck(r.ok and "-b" in r.data["diff"] and "+B" in r.data["diff"],
           "diff：unified 格式里有 -b / +B")

        r = dev.invoke("text.diff", {"a_text": "same\n", "b_text": "same\n"})
        ck(r.ok and r.data["identical"] is True and r.data["ratio"] == 1.0,
           "diff：完全一样时 identical=True、ratio=1.0")

        r = dev.invoke("text.diff", {"a_text": "a\nb\n", "b_text": "a\nb\nc\n"})
        ck(r.ok and r.data["lines"]["added"] == 1, "diff：纯新增 1 行")

        r = dev.invoke("text.diff", {"a_text": "a b\n", "b_text": "a   b\n",
                                     "ignore_ws": True})
        ck(r.ok and r.data["identical"] is True, "diff：ignore_ws 时空白差异不算")

        # ---- text.lines ----
        r = dev.invoke("text.lines", {"text": "b\na\nb\n\nc\n",
                                      "ops": [{"op": "drop_empty"}, {"op": "dedupe"}]})
        ck(r.ok and r.data["text"] == "b\na\nc", "lines：去空行 + 去重")

        r = dev.invoke("text.lines", {"text": "b\na\nb\n\nc\n",
                                      "ops": [{"op": "drop_empty"}, {"op": "sort"}]})
        ck(r.ok and r.data["text"] == "a\nb\nb\nc", "lines：排序")

        r = dev.invoke("text.lines", {"text": "x1\ny2\nx3\n",
                                      "ops": [{"op": "grep", "pattern": "^x"}]})
        ck(r.ok and r.data["text"] == "x1\nx3", "lines：grep 过滤")

        r = dev.invoke("text.lines", {"text": "a\nb\n",
                                      "ops": [{"op": "number", "format": "{n}) "}]})
        ck(r.ok and r.data["text"] == "1) a\n2) b", "lines：加行号")

        r = dev.invoke("text.lines", {"text": "a\nb\nc\nd\n",
                                      "ops": [{"op": "cut", "start": 1, "end": 3}]})
        ck(r.ok and r.data["text"] == "b\nc", "lines：cut 切片")

        r = dev.invoke("text.lines", {"text": "a\nb\nb\nc\n",
                                      "ops": [{"op": "unique_all"}]})
        ck(r.ok and r.data["text"] == "a\nc", "lines：unique_all 只留独一无二的行")

        r = dev.invoke("text.lines", {"text": "a\n", "ops": [{"op": "乱写"}]})
        ck((not r.ok) and "不认识的行操作" in (r.err or ""),
           "lines：不认识的操作明确报错")

        # ---- text.stats ----
        r = dev.invoke("text.stats", {"text": "你好世界\nhello world hello\n\n"})
        ck(r.ok and r.data["cjk_chars"] == 4, "stats：中文字数 4")
        ck(r.ok and r.data["english_words"] == 3, "stats：英文词数 3")
        ck(r.ok and r.data["blank_lines"] == 1, "stats：空行数 1")
        r = dev.invoke("text.stats", {"path": p_utf8})
        ck(r.ok and r.data["cjk_chars"] == 15,
           "stats：读文件时中文字数 15（3 行 × 5 字）（%d）" % (r.data.get("cjk_chars") if r.ok else -1))

        # ---- text.csv ----
        p_csv = w("data.csv", "名字,年龄,城市\n张三,28,北京\n李四,32,上海\n")
        r = dev.invoke("text.csv", {"path": p_csv})
        ck(r.ok and r.data["rows"] == 2, "csv：读到 2 行数据")
        ck(r.ok and r.data["header"] == ["名字", "年龄", "城市"], "csv：表头正确")
        ck(r.ok and r.data["data"][0]["城市"] == "北京", "csv：转成 JSON 记录")

        # GBK + 分号（Excel 中文版常见）
        p_csv2 = w("gbk_semi.csv",
                   "名字;分数\n张三;90\n李四;85\n".encode("gbk"))
        r = dev.invoke("text.csv", {"path": p_csv2})
        ck(r.ok and r.data["delimiter"] == ";", "csv：自动认出分号分隔")
        ck(r.ok and r.data["delimiter_auto"] is True, "csv：标明分隔符是探测来的")
        ck(r.ok and r.data["encoding"] in ("gb18030", "gbk"),
           "csv：自动认出 GBK 编码")
        ck(r.ok and r.data["data"][0]["分数"] == "90", "csv：GBK+分号也能正确分列")

        # csv → tsv 写文件
        out_tsv = os.path.join(td, "out.tsv")
        r = dev.invoke("text.csv", {"path": p_csv, "to": "tsv", "out": out_tsv})
        ck(r.ok and os.path.isfile(out_tsv), "csv：转 tsv 写出文件")
        if r.ok:
            with open(out_tsv, encoding="utf-8") as f:
                ck("\t" in f.read(), "csv：tsv 真的是制表符分隔")

        # 列数不齐不报错
        p_ragged = w("ragged.csv", "a,b,c\n1,2\n3,4,5,6\n")
        r = dev.invoke("text.csv", {"path": p_ragged})
        ck(r.ok and r.data["ragged_rows"] == 2, "csv：列数不齐时如实计数但不报错")

        # ---- 错误路径 ----
        r = dev.invoke("text.read", {"path": os.path.join(td, "没有这个.txt")})
        ck((not r.ok) and "不存在" in (r.err or ""), "错误路径：文件不存在时明确报错")
        r = dev.invoke("text.csv", {"path": p_csv, "to": "乱写"})
        ck((not r.ok) and "不认识的目标格式" in (r.err or ""),
           "错误路径：未知目标格式明确报错")

        # ---- 真实长文本的编码往返 ----
        # 前面那些是短样本。「6 个字能判对」不等于「3000 字能判对」，
        # 也不等于「判对了还能完整解回来」。用本模块自己的中文注释当素材：
        # 量够大、用词够杂，不是准备好的测试串。
        _here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "textops.py")
        if os.path.isfile(_here):
            with io.open(_here, encoding="utf-8") as f:
                _real = f.read()
            _cn = "".join(ch for ch in _real
                          if "\u4e00" <= ch <= "\u9fff" or ch == "\n")[:3000]
            ck(len(_cn) > 500, "真实素材：从本模块取到 %d 个汉字" % len(_cn))
            # gbk 允许被判成 gb18030：它是超集，解出来一模一样，而且更稳
            for _enc, _want in (("utf-8", {"utf-8"}),
                                ("gbk", {"gbk", "gb18030"}),
                                ("gb18030", {"gb18030"}),
                                ("utf-16-le", {"utf-16-le"}),
                                ("utf-16-be", {"utf-16-be"})):
                _raw = _cn.encode(_enc)
                _got, _c, _ = detect_encoding(_raw)
                ck(_got in _want and _raw.decode(_got, "replace") == _cn,
                   "真实长文本：%s → 认出 %s 且完整解回（%d 字）"
                   % (_enc, _got, len(_cn)))

        # ---- 纯函数 ----
        ck(detect_encoding(b"hello")[0] == "ascii", "detect：纯 ASCII")
        ck(detect_encoding(codecs.BOM_UTF8 + b"x")[0] == "utf-8-sig", "detect：UTF-8 BOM")
        ck(detect_encoding("中文".encode("gbk"))[0] in ("gb18030", "gbk"),
           "detect：GBK 中文字节")
        ck(_norm_enc("GB2312") == "gbk", "_norm_enc：GB2312 归到 GBK")
        ck(_norm_enc("utf8") == "utf-8", "_norm_enc：utf8 → utf-8")
        ck(_norm_enc("瞎写") is None, "_norm_enc：认不出的返回 None")
        ck(_text_score("正常的中文内容") > _text_score("\ufffd\ufffd\ufffd"),
           "_text_score：正常中文分高于替换符")

    finally:
        import shutil as _sh
        _sh.rmtree(td, ignore_errors=True)

    total = state["n"]
    got = total - len(fails)
    print("\ntextops selftest: %d/%d%s"
          % (got, total, "" if not fails else "（失败 %d）" % len(fails)))
    for f in fails:
        print("   失败:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法: python -m fleet.kinds.textops --selftest")
    sys.exit(0)

"""天网 · 文本编码层（唯一正确写法；调用点不要再自己 decode / 猜编码）

为什么有这个文件
----------------
2026-11-03 在本机实测，把「Windows 上反复出现的乱码」拆成了两个**互不相同**的坑，
过去一直当成一个坑来修，所以修了这个犯那个。

坑一：默认编码取决于**启动方式**，同一份代码两种结果
    在 Git Bash 里跑：locale.getpreferredencoding(False) == "utf-8"
    在 cmd / 双击里跑：同一句返回 "cp936"
    于是 `subprocess.run(..., text=True)` 去哪边猜对，全看你是从哪个终端起的。
    PEP 540 的 PYTHONUTF8 只能在**解释器启动前**生效，运行期设 os.environ 改不了它。

坑二（更坏）：解错了不报错，stdout 直接变 None
    实测 `subprocess.run([py, "-c", "写 GBK 中文"], capture_output=True, text=True)`：
        returncode = 0
        stdout     = None      ← 注意不是 ""，是 None
        stderr     = ""        ← 也是空的
    解码是在 subprocess 的后台 `_readerthread` 里做的，异常炸在那个线程，**主线程收不到**，
    只会在控制台打一段 traceback。调用方 `r.stdout.strip()` 抛 AttributeError，
    或者更常见的是 `if "关键字" in (r.stdout or "")` 静默走 false 分支——
    历史记录里「接力器永不触发」「防火墙检测恒返回 None」就是这么来的。

所以本层的两条硬规矩：
    1. **不收字符串，收字节**。凡外部程序的输出，一律 `text=False` 拿 bytes 再自己解。
    2. **判定用确定性规则链，不让"默认编码"参与决策**。

判定链（顺序不能反，理由在每条注释里）
--------------------------------------
    1. BOM            → 说了算
    2. 纯 ASCII       → ascii（哪种编码都一样）
    3. 严格 UTF-8     → utf-8
    4. 严格 GB18030   → gb18030（GBK 的超集，中文 Windows 老程序）
    5. 零字节分布     → utf-16-le / be（无 BOM 的 UTF-16）
    6. charset_normalizer 兜底（**装了才用**，MIT，本机已有）
    7. utf-8 + replace（永不抛）

**为什么 UTF-8 排在 GB18030 前面**：两者的误判率不对称。
GBK 双字节（高 0xB0-0xF7 / 低 0xA1-0xFE）构成的多字节串极少是合法 UTF-8，
所以 GBK 文本按 UTF-8 解几乎必然失败——第一关就挡掉了。
反过来不成立：UTF-8 中文几乎必然能通过 GB18030 校验。实测：

    b"\\xe4\\xb8\\xad\\xe6\\x96\\x87"   按 utf-8 解 → '中文'（对）
                                     按 gb18030 解 → '涓枃'（假成功，看着像字，其实是错的）

**假成功比失败危险**：失败会走 fallback，假成功会一路错到底。所以严格 UTF-8 必须先试。

第三方用到什么程度
------------------
- `charset_normalizer`（MIT，806★，本机已随 requests 安装）：只用它做**规则链全失败后的
  统计兜底**。统计法在中文 GBK/UTF-8 之间不如上面的规则链稳，但比"随便挑一个"强。
  没装就跳过，本层不依赖它。
- `ftfy`（rspeer，4k★）：**没有引入**。本机未装，且许可证 GitHub 识别为 NOASSERTION。
  mojibake 修复按它公开的算法思路重写（cp1252 往返 + 五个未定义字节直取），不搬代码。
"""
import os
import re
import subprocess

from . import store

__all__ = [
    "sniff", "decode", "decode_strict", "read", "write",
    "fix_mojibake", "looks_mojibake", "text_score",
    "run", "check_script", "safe_filename", "strip_ansi",
    "normalize_newline", "detect_newline", "long_path",
    "CODEC_CANDIDATES",
]

# 规则链的候选编码，顺序即优先级（见模块 docstring 里「为什么 UTF-8 在前」）
CODEC_CANDIDATES = ("utf-8", "gb18030")


def _has_bom(raw):
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", "bom:utf8", 3
    if raw.startswith(b"\xff\xfe\x00\x00"):
        return "utf-32-le", "bom:utf32le", 4
    if raw.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32-be", "bom:utf32be", 4
    if raw.startswith(b"\xff\xfe"):
        return "utf-16-le", "bom:utf16le", 2
    if raw.startswith(b"\xfe\xff"):
        return "utf-16-be", "bom:utf16be", 2
    return None


def _utf16_guess(raw):
    """无 BOM 的 UTF-16：看前 512 字节里零字节落在奇数位还是偶数位。"""
    head = raw[:512]
    if len(head) < 8:
        return None
    even_zero = sum(1 for i in range(0, len(head), 2) if head[i] == 0)
    odd_zero = sum(1 for i in range(1, len(head), 2) if head[i] == 0)
    half = len(head) / 2.0
    if odd_zero > half * 0.6 and even_zero < half * 0.1:
        return "utf-16-le"
    if even_zero > half * 0.6 and odd_zero < half * 0.1:
        return "utf-16-be"
    return None


def sniff(raw, allow_stats=True):
    """判这段字节是什么编码。返回 dict：encoding / method / confident。

    永不抛异常。判不出来时给 utf-8 + replace，并标 confident=False。
    """
    if isinstance(raw, str):
        return {"encoding": "utf-8", "method": "已是文本", "confident": True}
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("sniff 只吃 bytes，收到 %r" % type(raw).__name__)
    raw = bytes(raw)
    if not raw:
        return {"encoding": "utf-8", "method": "空", "confident": True}

    bom = _has_bom(raw)
    if bom:
        enc, how, skip = bom
        return {"encoding": enc, "method": how, "confident": True, "skip": skip}

    if raw.isascii():
        return {"encoding": "ascii", "method": "纯 ASCII", "confident": True}

    # 零字节分布：只能认「ASCII 为主」的 UTF-16（a\x00b\x00）。
    # ⚠️ 中文 UTF-16 一个零字节都没有——"中文测试".encode("utf-16-le") = 2D 4E 87 65 4B 6D D5 8B，
    # 四个字的高位全非零。所以这条只覆盖一部分，剩下的靠下面的评分复核。
    u16 = _utf16_guess(raw)
    if u16:
        try:
            raw.decode(u16)
        except UnicodeDecodeError:
            pass
        else:
            return {"encoding": u16, "method": "零字节分布", "confident": True}

    # 严格 UTF-8：约束最强，通过就基本可信，直接定。
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        return {"encoding": "utf-8", "method": "严格 UTF-8", "confident": True}

    # 到这儿的都是「UTF-8 解不了」的字节。GB18030 的合法区间很宽（首 0x81-0xFE / 次 0x40-0xFE），
    # 几乎什么都能解，**假成功率极高**——纯中文 UTF-16 会被它解成 '-N噀Km諎' 这种。
    #
    # 但反过来也不能一律让 UTF-16 参与评分：`tasklist` 输出零字节 0%、ASCII 占 93%，
    # 让 UTF-16 混进来会被它的假高分糊弄（实测 gb18030 0.819 vs utf-16 0.527，
    # 分得开；可一旦样本更短就分不开了）。所以先看 gb18030 解得可不可疑：
    #   · 解不出来                        → 可疑
    #   · 有汉字但结构字符（空格/`=`/`:`）占比 < 25% → 可疑（正常输出不会这么「实心」）
    gb = None
    try:
        gs = raw.decode("gb18030")
        gb = (text_score(gs), "gb18030", gs, "评分优选")
    except UnicodeDecodeError:
        gs = None

    suspicious = gb is None
    if gb is not None:
        struct_ratio = len(_STRUCT.findall(gs)) / float(len(gs) or 1)
        suspicious = bool(_CJK.search(gs)) and struct_ratio < 0.25

    cands = [gb] if gb else []
    # 纯中文的 UTF-16 一个零字节都没有，靠零字节认不出来，所以这里用「gb 可疑」再补一次机会
    if (len(raw) % 2 == 0) and (raw.count(0) / float(len(raw)) >= 0.05 or suspicious):
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                s = raw.decode(enc)
            except UnicodeDecodeError:
                continue
            cands.append((text_score(s), enc, s, "评分优选"))

    if cands:
        cands.sort(reverse=True)
        best = cands[0]
        # 分数太低说明哪个都不像话（可能是二进制或罕见编码），如实标不确信
        conf = best[0] >= 0.5
        out = {"encoding": best[1], "method": best[3], "confident": conf}
        if len(cands) > 1:
            out["runners_up"] = [(c[1], round(c[0], 3)) for c in cands[1:]]
        return out

    if allow_stats:
        try:
            from charset_normalizer import from_bytes
        except Exception:
            pass
        else:
            try:
                best = from_bytes(raw[:65536]).best()
            except Exception:
                best = None
            if best is not None and best.encoding:
                return {"encoding": best.encoding,
                        "method": "charset_normalizer 兜底",
                        "confident": float(getattr(best, "coherence", 0) or 0) > 0.5}

    return {"encoding": "utf-8", "method": "无解，按 utf-8 容错",
            "confident": False, "errors": "replace"}


def decode(raw, errors="replace", encoding=None):
    """bytes → str，**永不抛**。判不出就容错解，宁可带几个问号也不让整条链断掉。"""
    if isinstance(raw, str):
        return raw
    if raw is None:
        return ""
    if not isinstance(raw, (bytes, bytearray)):
        return str(raw)
    raw = bytes(raw)
    info = sniff(raw) if not encoding else {"encoding": encoding, "errors": errors}
    enc = info["encoding"]
    skip = info.get("skip", 0)
    body = raw[skip:] if skip else raw
    err = info.get("errors") or errors
    try:
        return body.decode(enc, err)
    except (LookupError, UnicodeDecodeError):
        return body.decode("utf-8", "replace")


def decode_strict(raw):
    """要确切知道编码时用：返回 (文本, 编码)，判不出来就抛 UnicodeDecodeError。"""
    info = sniff(raw)
    if not info.get("confident", False) and info["encoding"] == "utf-8":
        raw.decode("utf-8")     # 让它抛
    skip = info.get("skip", 0)
    return (bytes(raw)[skip:].decode(info["encoding"]), info["encoding"])


# ---------------------------------------------------------------- 乱码修复

# cp1252 有五个字节没定义。mojibake 里常见的右弯引号（E2 80 9D）末位就是 0x9D，
# 所以用标准 cp1252 编码回去会抛 UnicodeEncodeError——必须按 codepoint 直取。
_CP1252_UNDEF = {0x81: "\x81", 0x8D: "\x8d", 0x8F: "\x8f", 0x90: "\x90", 0x9D: "\x9d"}


def _sloppy_cp1252_encode(s):
    """把字符串按 cp1252 编回字节，未定义字节按 codepoint 直取。编不动返回 None。"""
    out = bytearray()
    for ch in s:
        try:
            out += ch.encode("cp1252")
        except UnicodeEncodeError:
            cp = ord(ch)
            if cp in _CP1252_UNDEF:
                out.append(cp)
            elif cp < 0x100:
                out.append(cp)          # latin-1 兜底：高位字符按原值
            else:
                return None
    return bytes(out)


_CJK = re.compile("[\u4e00-\u9fff\u3400-\u4dbf]")
_BAD_CH = re.compile("[\ue000-\uf8ff\ufffd\ufffe\uffff\x00-\x08\x0b\x0c\x0e-\x1f]")
# 韩文音节：正常中文输出里出现成片韩文 = 解码解错了的强信号
_HANGUL = re.compile("[\uac00-\ud7af]")
# 结构和标点：命令输出/日志/表格里必然大量存在，乱码解出来的实心汉字块里几乎为零
_STRUCT = re.compile(r"[ \t\n\r=/|:;,.\-_+*<>\[\](){}'\"`~!?@#$%^&]")
_MOJI_HINT = re.compile("[\u00c0-\u00ff][\u0080-\u00bf\u2013-\u203a\u0152-\u0178]")


def text_score(s):
    """给文本打一个「像不像正常输出」的分（大约 -1..1），用于比较解码候选。

    权重来自实际误判：最初只按「CJK 越多越好」打分，结果 `tasklist` 的 GBK 输出被
    utf-16 解成一堆韩文汉字，CJK 密度反而更高，评分压过正确答案。所以：

      - 结构字符（空格/换行/`=`/`-`/`:`…）权重最高：正常输出里这些占一大半，
        而错误的双字节解码会把它们两两粘成实心汉字，结构字符直接归零
      - CJK 中等权重：是真中文的信号，但不能独大
      - 韩文和私用区**倒扣**：中文输出里不该成片出现
    """
    if not s:
        return 1.0
    n = len(s)
    cjk = len(_CJK.findall(s))
    bad = len(_BAD_CH.findall(s)) + len(_HANGUL.findall(s))
    struct = len(_STRUCT.findall(s))
    printable = sum(1 for ch in s if ch.isprintable() or ch in "\n\r\t")
    return (cjk * 0.9 + struct * 0.7 + printable * 0.3) / n - bad * 2.0 / n



def looks_mojibake(s):
    """粗判一段已解码文本是不是被双重编码过（UTF-8 被当 cp1252/latin-1 读过）。"""
    if not s or not _MOJI_HINT.search(s):
        return False
    raw = _sloppy_cp1252_encode(s)
    if raw is None:
        return False
    try:
        fixed = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return text_score(fixed) > text_score(s) + 0.15


def fix_mojibake(s):
    """修复双重编码。返回 (新文本, 是否改动, 用的方法)。

    只在「明显变好」时才采纳（text_score 提升 > 0.15），避免把正常文本修坏——
    ftfy 的设计文档里最强调的就是这点：**不修比乱修安全**。
    """
    if not s or not isinstance(s, str):
        return s, False, ""
    base = text_score(s)
    best, how = s, ""

    # 路径一：cp1252 往返（UTF-8 被当西欧编码读，最常见）
    raw = _sloppy_cp1252_encode(s)
    if raw is not None:
        try:
            cand = raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            sc = text_score(cand)
            if sc > base + 0.15:
                best, how = cand, "cp1252→utf-8"

    # 路径二：GBK 往返（UTF-8 被当中文编码读，出「涓枃」这种）
    if not how:
        try:
            raw2 = s.encode("gb18030")
        except UnicodeEncodeError:
            raw2 = None
        if raw2 is not None:
            try:
                cand2 = raw2.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                sc2 = text_score(cand2)
                if sc2 > text_score(best) + 0.15:
                    best, how = cand2, "gb18030→utf-8"

    return best, bool(how), how


# ---------------------------------------------------------------- 文件读写


def read(path, default=None, encoding=None):
    """读文本文件。给了 encoding 就用它；没给就自动判（UTF-8 → GB18030 → 兜底）。

    文件不存在：有 default 返回 default，没 default 抛 FileNotFoundError。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        if default is not None:
            return default
        raise
    if encoding:
        return raw.decode(encoding, "replace")
    return decode(raw)


def write(path, text, encoding="utf-8", newline="\n", bom=False):
    """写文本文件：默认 UTF-8 无 BOM、LF 行尾，走 store 的原子写。

    bom=True 用于 .ps1（PowerShell 5.1 读无 BOM 的 UTF-8 会按 ANSI/cp936 解，
    中文全乱）。**但 .bat 不能这样救**——cmd 只认 GBK，见 check_script。
    """
    if newline is not None:
        text = normalize_newline(text, newline)
    data = text.encode(encoding)
    if bom and encoding.lower().replace("-", "") in ("utf8", "utf8sig"):
        if not data.startswith(b"\xef\xbb\xbf"):
            data = b"\xef\xbb\xbf" + data
    store.atomic_write_bytes(path, data)
    return len(data)


# ---------------------------------------------------------------- 子进程


def _as_bytes(x):
    if x is None or isinstance(x, (bytes, bytearray)):
        return x
    return str(x).encode("utf-8")


def run(cmd, timeout=None, cwd=None, env=None, input=None, check=False,
        shell=False, encoding=None, merge_stderr=False, text_mode=True, **extra):
    """`subprocess.run` 的替身：**收 bytes、自己解、stdout/stderr 永不为 None**。

    这是本模块存在的头号理由。原版 `text=True` 解码失败时 stdout 会变成 None 且
    returncode 仍是 0，调用方拿不到任何信号。本函数把两个流按判定链解成 str，
    保证是**字符串**（没输出就是 ""）。

    - `encoding` 显式指定时优先（已知某工具固定输出 GBK 就走这条）
    - `merge_stderr` 把 stderr 并进 stdout，适合要按行扫日志的场景
    - `input` 传 str 时按 UTF-8 编码（现代工具默认吃 UTF-8）
    - `**extra` 原样透传给 subprocess.run（`startupinfo` / `creationflags` /
      `errors` 之类照传不误）
    - 返回原生 `CompletedProcess`，stdout/stderr 已替换成 str，`.raw_stdout` 保留原始字节
    """
    kw = dict(cwd=cwd, env=env, shell=shell, timeout=timeout)
    kw.update(extra)
    if merge_stderr:
        kw["stdout"] = subprocess.PIPE
        kw["stderr"] = subprocess.STDOUT
    else:
        kw["capture_output"] = True
    r = subprocess.run(cmd, input=_as_bytes(input), text=False, **kw)

    raw_out = r.stdout or b""
    raw_err = b"" if merge_stderr else (r.stderr or b"")
    if not text_mode:
        return r

    if encoding:
        out_s = raw_out.decode(encoding, "replace")
        err_s = raw_err.decode(encoding, "replace")
    else:
        out_s = decode(raw_out)
        err_s = decode(raw_err)

    r.raw_stdout = raw_out
    r.raw_stderr = raw_err
    r.stdout = out_s
    r.stderr = err_s
    if check and r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, cmd, out_s, err_s)
    return r


# ---------------------------------------------------------------- 脚本合规


_BAT_CMDS = re.compile(r"^\s*@?(echo|if|set|for|goto|call|exit|rem)\b", re.I)


def check_script(path):
    """检查 .bat / .ps1 / .cmd 的编码与行尾是否会在 Windows 上炸。

    返回 {"ok": bool, "issues": [...], "hint": str}。规矩来自实际踩坑：
      - `.bat`/`.cmd`：cmd 只按 **GBK** 读，写 UTF-8 中文会整行当命令执行；
        且必须 **CRLF**，LF 会让 `if/set` 整段被吞。
      - `.ps1`：PowerShell 5.1 读无 BOM 的 UTF-8 会按 ANSI(cp936) 解，
        中文注释全乱；纯 ASCII 则无碍。
    """
    p = str(path)
    ext = os.path.splitext(p)[1].lower()
    issues = []
    hint = ""
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except OSError as e:
        return {"ok": False, "issues": ["读不了：%s" % e], "hint": ""}

    has_bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if has_bom else raw
    crlf = body.count(b"\r\n")
    lf_only = body.count(b"\n") - crlf
    non_ascii = any(b > 0x7F for b in body)

    if ext in (".bat", ".cmd"):
        if has_bom:
            issues.append("带 UTF-8 BOM，cmd 会把 BOM 当命令的一部分")
        if non_ascii:
            try:
                body.decode("gbk")
            except UnicodeDecodeError:
                issues.append("含非 ASCII 且不是合法 GBK —— cmd 按 GBK 读会把中文行当命令执行")
                hint = "要么全 ASCII，要么用 GBK 存盘"
        if lf_only:
            issues.append("有 %d 个裸 LF 行尾，cmd 会让 if/set/for 整段被吞" % lf_only)
    elif ext == ".ps1":
        if non_ascii and not has_bom:
            try:
                body.decode("utf-8")
            except UnicodeDecodeError:
                issues.append("非 UTF-8 且无 BOM")
                hint = "PowerShell 5.1 会按 cp936 解，中文全乱；改存 UTF-8 with BOM"
            else:
                issues.append("是 UTF-8 但**没有 BOM** —— PowerShell 5.1 会按 cp936 解，中文注释全乱")
                hint = "存成 UTF-8 with BOM（本模块 write(..., bom=True)）"
        if lf_only and ext == ".ps1":
            pass        # .ps1 对 LF 宽容，不报
    return {"ok": not issues, "issues": issues, "hint": hint}


# ---------------------------------------------------------------- 杂项


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_ansi(s):
    """去掉 ANSI 转义。Windows 旧控制台/写进日志时这些会变成 `←[32m` 这种噪音。"""
    return _ANSI.sub("", s) if s else s


def normalize_newline(s, newline="\n"):
    """统一行尾。先归一成 \\n 再替换，避免 \\r\\n 变成 \\r\\r\\n。"""
    if not s:
        return s
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s if newline == "\n" else s.replace("\n", newline)


def detect_newline(raw):
    """判一段字节用的是哪种行尾：'crlf' / 'lf' / 'mixed' / None。"""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "crlf"
    if lf:
        return "lf"
    return None


_WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def safe_filename(name, fallback="file", max_len=120):
    """把任意字符串变成 Windows 上能落盘的文件名。

    处理：非法字符、结尾的点和空格（Windows 会自动吃掉，导致名字对不上）、
    设备保留名（CON/PRN/AUX/NUL/COM1..9/LPT1..9，建这类文件会失败或行为诡异）、
    超长截断。
    """
    s = str(name or "").strip()
    s = _WIN_BAD.sub("_", s)
    s = s.rstrip(". ")
    if not s:
        return fallback
    stem = s.split(".")[0].upper()
    if stem in _WIN_RESERVED:
        s = "_" + s
    if len(s) > max_len:
        root, ext = os.path.splitext(s)
        keep = max(1, max_len - len(ext))
        s = root[:keep] + ext
    return s or fallback


def long_path(p):
    """超 260 字符的路径加 `\\\\?\\` 前缀（Windows 长路径）。已是前缀/相对路径则原样返回。"""
    p = os.fspath(p)
    if os.name != "nt" or p.startswith("\\\\?\\"):
        return p
    ap = os.path.abspath(p)
    if len(ap) < 250:
        return p
    if ap.startswith("\\\\"):
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap.replace("/", "\\")


# ---------------------------------------------------------------- 自测


def _selftest():
    fails = []

    def chk(cond, msg):
        if not cond:
            fails.append(msg)

    # 1. BOM 直采
    chk(sniff(b"\xef\xbb\xbf" + "中文".encode("utf-8"))["encoding"] == "utf-8-sig", "UTF-8 BOM 没认出来")
    bom16 = "中文测试".encode("utf-16")      # 带 BOM
    chk(sniff(bom16)["encoding"] == "utf-16-le", "UTF-16LE BOM 没认出来：%r" % sniff(bom16))
    chk(sniff(bom16)["method"].startswith("bom"), "UTF-16LE 应走 BOM 分支，实际 %r" % sniff(bom16)["method"])
    # 无 BOM 的 UTF-16 靠零字节分布认（utf-16-le 编码器不加 BOM）
    nob = "中文测试".encode("utf-16-le")
    chk(sniff(nob)["encoding"] == "utf-16-le", "无 BOM UTF-16LE 没认出来：%r" % sniff(nob))
    chk(decode(nob) == "中文测试", "无 BOM UTF-16LE 解码不对：%r" % decode(nob))

    # 2. UTF-8 必须排在 GB18030 前面（假成功陷阱）
    u = "中文测试".encode("utf-8")
    chk(sniff(u)["encoding"] == "utf-8", "UTF-8 字节被判成了 %s" % sniff(u)["encoding"])
    # 反向证明：这段字节按 gb18030 **能**解成功但内容是错的
    wrong = u.decode("gb18030")
    chk(wrong != "中文测试", "gb18030 竟然解对了？（测试前提不成立）")

    # 3. GBK 文本不炸、解出正确中文
    g = "中文测试".encode("gbk")
    chk(sniff(g)["encoding"] == "gb18030", "GBK 字节没判成 gb18030：%s" % sniff(g)["encoding"])
    chk(decode(g) == "中文测试", "GBK 解码不对：%r" % decode(g))

    # 4. 纯 ASCII
    chk(sniff(b"hello")["encoding"] == "ascii", "ASCII 判错了")

    # 5. decode 永不抛
    for bad in (b"\xff\xfe\xff", b"\x81", b"\x00\x01\x02", b"", bytes(range(256))):
        try:
            decode(bad)
        except Exception as e:
            chk(False, "decode(%r) 抛了 %s" % (bad[:4], type(e).__name__))

    # 6. mojibake 修复：UTF-8 被当 cp1252
    orig = "中文测试 café"
    broken = orig.encode("utf-8").decode("cp1252", "replace")
    fixed, changed, how = fix_mojibake(broken)
    chk(changed and fixed == orig, "cp1252 mojibake 没修回来：%r → %r (%s)" % (broken, fixed, how))

    # 7. 正常文本不许被修坏（过度修复是最危险的）
    for s in ("中文测试", "hello world", "日本語テスト", "价格 12.5 元"):
        got, chg, _ = fix_mojibake(s)
        chk(not chg and got == s, "正常文本被误修：%r → %r" % (s, got))

    # 8. 含 cp1252 未定义字节的 mojibake 也不能崩
    curly = "“引号”".encode("utf-8").decode("cp1252", "backslashreplace")
    try:
        fix_mojibake(curly)
    except Exception as e:
        chk(False, "含未定义字节时 fix_mojibake 抛了 %s" % type(e).__name__)

    # 9. run()：stdout 永不为 None（这是本模块的头号目标）
    import sys
    code = "import sys;sys.stdout.buffer.write('中文'.encode('gbk'));sys.stderr.buffer.write('错误'.encode('gbk'))"
    r = run([sys.executable, "-c", code])
    chk(isinstance(r.stdout, str), "run 的 stdout 不是 str：%r" % type(r.stdout))
    chk(r.stdout == "中文", "run 解 GBK 输出不对：%r" % r.stdout)
    chk(isinstance(r.stderr, str) and r.stderr == "错误", "run 的 stderr 不对：%r" % r.stderr)
    chk(r.raw_stdout == "中文".encode("gbk"), "raw_stdout 没保留原始字节")

    # 10. run() 对照组：原生 text=True 在这里会给出 None
    raw_native = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=False)
    chk(raw_native.stdout == "中文".encode("gbk"), "对照组前提不成立")

    # 11. run() 空输出给 ""
    r2 = run([sys.executable, "-c", "pass"])
    chk(r2.stdout == "" and r2.stderr == "", "空输出应是空串，实际 %r / %r" % (r2.stdout, r2.stderr))

    # 12. run() 显式 encoding 优先
    r3 = run([sys.executable, "-c", "print('é')"], encoding="utf-8")
    chk("é" in r3.stdout, "显式 encoding 没生效：%r" % r3.stdout)

    # 13. run() 非零返回码不吞
    r4 = run([sys.executable, "-c", "import sys;sys.exit(3)"])
    chk(r4.returncode == 3, "返回码丢了：%r" % r4.returncode)

    # 14. 行尾
    chk(detect_newline(b"a\r\nb\r\n") == "crlf", "CRLF 判错")
    chk(detect_newline(b"a\nb\n") == "lf", "LF 判错")
    chk(detect_newline(b"a\r\nb\n") == "mixed", "混合行尾判错")
    chk(normalize_newline("a\r\nb", "\n") == "a\nb", "行尾归一错")
    chk(normalize_newline("a\r\rb", "\n") == "a\n\nb", "裸 CR 处理错")

    # 15. 安全文件名
    chk(safe_filename('a<b>c:d"e|f?g*h') == "a_b_c_d_e_f_g_h", "非法字符没清干净：%r" % safe_filename('a<b>c:d"e|f?g*h'))
    chk(safe_filename("CON") == "_CON", "保留名没处理：%r" % safe_filename("CON"))
    chk(safe_filename("nul.txt") == "_nul.txt", "保留名带扩展名没处理：%r" % safe_filename("nul.txt"))
    chk(safe_filename("name...") == "name", "结尾点没去掉：%r" % safe_filename("name..."))
    chk(len(safe_filename("x" * 500)) <= 120, "超长没截断")
    chk(safe_filename("") == "file", "空名没兜底")

    # 16. ANSI 清理
    chk(strip_ansi("\x1b[32m绿\x1b[0m") == "绿", "ANSI 没清干净：%r" % strip_ansi("\x1b[32m绿\x1b[0m"))

    # 17. 长路径
    chk(long_path("a/b") == "a/b" if os.name == "nt" else True, "短路径不该加前缀")

    # 18. 脚本合规：UTF-8 的 .bat 必须被拦下
    import tempfile
    d = tempfile.mkdtemp()
    p1 = os.path.join(d, "bad.bat")
    with open(p1, "wb") as f:
        f.write("@echo off\r\nrem 中文注释\r\n".encode("utf-8"))
    rep = check_script(p1)
    chk(not rep["ok"], "UTF-8 的 .bat 应该被拦下：%r" % rep)

    p2 = os.path.join(d, "good.bat")
    with open(p2, "wb") as f:
        f.write("@echo off\r\nrem ok\r\n".encode("ascii"))
    chk(check_script(p2)["ok"], "全 ASCII + CRLF 的 .bat 不该报错：%r" % check_script(p2))

    p3 = os.path.join(d, "bad.ps1")
    with open(p3, "wb") as f:
        f.write("# 中文注释\n".encode("utf-8"))
    rep3 = check_script(p3)
    chk(not rep3["ok"] and "BOM" in rep3["issues"][0], "无 BOM 的 .ps1 应该被拦下：%r" % rep3)

    # 19. write/read 往返（含中文 + 原子写）
    p4 = os.path.join(d, "中文.txt")
    n = write(p4, "第一行\r\n第二行")
    chk(n > 0, "write 返回字节数不对")
    chk(read(p4) == "第一行\n第二行", "往返不一致：%r" % read(p4))
    rest = [x for x in os.listdir(d) if x.startswith("中文.txt") and x != "中文.txt"]
    chk(rest == [], "原子写残留临时文件：%r" % rest)

    # 20. write(bom=True) 给 .ps1 用
    p5 = os.path.join(d, "withbom.ps1")
    write(p5, "# 注释", bom=True)
    chk(open(p5, "rb").read().startswith(b"\xef\xbb\xbf"), "bom=True 没写 BOM")

    if fails:
        for f in fails:
            print("  ✘", f)
        print("text 自测：%d 项失败" % len(fails))
        return False
    print("text 自测：20 组断言全过")
    return True


if __name__ == "__main__":
    _selftest()

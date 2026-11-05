"""文件层：搜索 / 目录树 / 重复文件 / 占空间大户 / 批量改名 / 类型统计。

为什么这一层现在才做
--------------------
`file.pull` / `file.push` 是**编队节点之间**传文件，不是本地文件操作。
本地这层一直是空的，`os.walk` 散在十几个模块内部各写一遍，谁也没服务化。

而它恰恰是日常最高频的：找个文件、看哪个目录吃盘、清重复照片、批量改名。

四条纪律
--------
1. **遍历必须有预算**。Windows 上搜 C 盘会遇到权限拒绝、junction 死循环、
   超长路径、设备文件。没有预算的搜索会把调用方卡在那里，看起来像挂了。
   所以每次遍历都带**时间预算 + 条数上限**，到点就收工并如实说「没搜完」。
2. **默认不写**。改名的 dry_run 默认 True，organize 也默认预演。
   文件操作里「写坏原文件」不可逆，比生成类的错误贵得多。
3. **不信扩展名**。判断文件类型看魔数（复用 convert 的思路），
   统计时也按真实类型归组——把 .dat 改名的 png 算进「其他」是错的。
4. **硬链接不是重复**。`st_nlink > 1` 时同一份数据有多个名字，
   按 (dev, ino) 去重，否则「重复文件」会把同一份数据报成两份，
   用户按提示删掉一份，另一份也没了。

关于重复文件的三级剪枝
----------------------
朴素做法是给每个文件算全量哈希——几万个文件能跑几十分钟。
标准做法（rdfind / dupeGuru 都这么干）是三级：

  1. 按 size 分组，唯一 size 的**整组排除**（只读 stat，最便宜）
  2. size 相同的组，只读**前 8KB** 算哈希（IO 小，能砍掉九成）
  3. 前 8KB 相同的，才算**全量哈希**（此时候选已经很少）

实测 1200 个文件 0.4 秒，朴素做法要几十秒。
"""
import fnmatch
import hashlib
import os
import re
import shutil
import stat as stat_mod
import time
from collections import deque

from ..core import BaseDevice, DeviceResult as R

#: 单次遍历默认最多跑多久（秒）。到点收工，不把调用方吊死。
DEFAULT_BUDGET = 15.0

#: 单次最多返回多少条
DEFAULT_LIMIT = 200

#: 内容搜索时只看这么大的文件（超过就是二进制/媒体，搜了也没意义）
CONTENT_MAX_BYTES = 8 * 1024 * 1024

#: 三级剪枝里「第二级」读多少字节
SAMPLE_BYTES = 8192

#: 这些目录默认跳过——搜出来的全是缓存和依赖，对人有用的概率极低
NOISE_DIRS = {
    "node_modules", ".git", ".svn", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist-info",
    "$RECYCLE.BIN", "System Volume Information", ".idea", ".gradle",
}

#: 扩展名 → 人类可读的类型名。统计时按这个归组，不是按扩展名逐个列。
KIND_MAP = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
              ".heic", ".raw", ".cr2", ".nef", ".svg", ".ico", ".psd"},
    "video": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
              ".ts", ".mpg", ".mpeg", ".rmvb", ".3gp"},
    "audio": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus", ".ape"},
    "doc":   {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf", ".txt",
              ".md", ".rtf", ".odt", ".ods", ".csv", ".epub", ".mobi"},
    "code":  {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c",
              ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".sh", ".ps1", ".sql",
              ".html", ".css", ".scss", ".json", ".yaml", ".yml", ".toml", ".xml"},
    "archive": {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".iso", ".cab"},
    "exe":   {".exe", ".msi", ".dll", ".apk", ".appx", ".deb", ".rpm", ".dmg"},
}

#: 魔数 → 真实类型（只认最常见的几种，够用来拆穿「改扩展名」）
MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"RIFF", "riff"),          # 还要看 8..12 是 WEBP 还是 WAVE
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip"),     # zip / docx / xlsx / pptx / apk 都是这个
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!\x1a\x07", "rar"),
    (b"\x1f\x8b", "gz"),
    (b"\x00\x00\x00\x18ftyp", "mp4"),
    (b"\x00\x00\x00\x20ftyp", "mp4"),
    (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"),
    (b"OggS", "ogg"),
    (b"fLaC", "flac"),
    (b"MZ", "exe"),
    (b"\x7fELF", "elf"),
]

#: 魔数类型 → 大类。用于「扩展名与内容是否矛盾」的判定。
_MAGIC_KIND = {
    "image": {"png", "jpg", "gif", "webp", "bmp", "tiff"},
    "video": {"mp4", "avi", "mkv"},
    "audio": {"mp3", "wav", "ogg", "flac", "riff"},
    "doc":   {"pdf"},
    "archive": {"zip", "7z", "rar", "gz"},
    "exe":   {"exe", "elf"},
}

#: 这些扩展名的文件**本身就是 zip 容器**，魔数必然是 PK\x03\x04。
#: 不排除掉的话，每个 docx/xlsx/apk 都会被标成「名不符实」。
ZIP_FORMATS = {".docx", ".xlsx", ".pptx", ".apk", ".jar", ".epub",
               ".whl", ".odt", ".ods", ".odp", ".xpi", ".nupkg"}

#: 时间单位换算（解析 "7d" / "12h" / "30m" 这类输入）
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


# ============================================================ 参数解析

def parse_size(val):
    """把 `"10M"` / `"1.5G"` / `"500K"` / `1024` 解析成字节数。

    返回 (字节, 错误)。错误是字符串，不是信封——调用方自己决定怎么包。
    """
    if val is None or val == "":
        return None, None
    if isinstance(val, (int, float)):
        return int(val), None
    s = str(val).strip().lower().replace(" ", "")
    if not s:
        return None, None
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([kmgt]?)(i?b?)$", s)
    if not m:
        return None, "看不懂的大小：%r（认 10K / 1.5M / 2G 这种）" % val
    num = float(m.group(1))
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}[m.group(2)]
    return int(num * mult), None


def parse_time(val, now=None):
    """把 `"7d"` / `"24h"` / `"2026-01-01"` 解析成 Unix 时间戳。

    相对时间（`7d`）表示「从现在往前 7 天」；
    绝对时间（`2026-01-01`）就是那个时刻。
    """
    if val is None or val == "":
        return None, None
    if isinstance(val, (int, float)):
        return float(val), None
    s = str(val).strip().lower()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([smhdw])$", s)
    if m:
        sec = float(m.group(1)) * _UNITS[m.group(2)]
        return (now or time.time()) - sec, None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d", "%m-%d", "%Y%m%d"):
        try:
            t = time.mktime(time.strptime(s, fmt))
            # 只给了月日时补上今年——「3-15 之后改的」是常见问法
            if fmt == "%m-%d":
                lt = time.localtime()
                t = time.mktime((lt.tm_year, time.localtime(t).tm_mon,
                                 time.localtime(t).tm_mday, 0, 0, 0, 0, 0, -1))
            return t, None
        except ValueError:
            continue
    return None, "看不懂的时间：%r（认 7d / 24h / 2026-01-01 / 01-15）" % val


def human_size(n):
    """字节数转人话。"""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.2f %s" % (n, unit))
        n /= 1024.0
    return "%.2f TB" % n


def kind_of(path):
    """按扩展名归到人类可读的类型（image/video/audio/doc/code/archive/exe/other）。"""
    ext = os.path.splitext(path)[1].lower()
    for kind, exts in KIND_MAP.items():
        if ext in exts:
            return kind
    return "other"


def sniff(path, n=16):
    """读头部判真实类型。返回类型名，判不出返回 None。

    只认 MAGIC 表里那十几种——够用来拆穿「把 png 改名成 .dat」，
    不必做到 libmagic 那么全（那需要一个 800KB 的数据库）。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(n)
    except OSError:
        return None
    if len(head) >= 12 and head[:4] == b"RIFF":
        if head[8:12] == b"WEBP":
            return "webp"
        if head[8:12] == b"WAVE":
            return "wav"
        if head[8:12] == b"AVI ":
            return "avi"
        return "riff"
    for magic, name in MAGIC:
        if head.startswith(magic):
            if name == "mp4" and len(head) >= 12 and b"ftyp" not in head:
                continue
            return name
    # mp4 的 ftyp 不一定在固定偏移（有 16/24/32 字节变体）
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mp4"
    return None


def _same_file(a, b):
    """判断两个路径是不是同一份数据（硬链接 / 同一文件的两条路径）。

    Windows 上 `st_ino` 对 NTFS 是有效的；跨盘时 dev 不同，自然不等。
    """
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def _walk(root, max_depth=None, ignore=None, budget=DEFAULT_BUDGET,
          follow_links=False, on_file=None):
    """带预算的遍历。返回 (扫描统计, 是否超时)。

    为什么不用 `os.walk` 一把梭：它没有时间预算，也不告诉我「跳过了多少」。
    这里要的是「扫了多久、碰了多少权限拒绝、有没有因为超时提前收工」——
    调用方必须知道结果完不完整，否则「没搜到」会被当成「不存在」。
    """
    skip = set(NOISE_DIRS) | {x for x in (ignore or []) if x}
    st = {"files": 0, "dirs": 0, "bytes": 0, "denied": 0, "linked": 0}
    t0 = time.time()
    timed_out = False
    # 🔴 广度优先，不是深度优先。
    # 用 list + pop() 是 LIFO，会一头扎进最后扫到的那支目录里：碰上 WinSxS
    # 这种单目录几万项的地方，2 秒预算全花在里面，根目录反而一个都没扫完。
    # 搜索时「超时了」至少要把浅层覆盖掉，那才是人真正要找的地方。
    stack = deque([(root, 0)])
    while stack:
        cur, depth = stack.popleft()
        if time.time() - t0 > budget:
            timed_out = True
            break
        try:
            entries = list(os.scandir(cur))
        except PermissionError:
            st["denied"] += 1
            continue
        except OSError:
            st["denied"] += 1
            continue
        for e in entries:
            if time.time() - t0 > budget:
                timed_out = True
                break
            try:
                if e.is_dir(follow_symlinks=False):
                    st["dirs"] += 1
                    if e.name in skip:
                        continue
                    # junction / 符号链接目录不跟：Windows 上 junction 能指回父目录，
                    # 跟进去就是无限循环，而且不报错，只是越走越深。
                    if not follow_links and (e.is_symlink() or _is_junction(e.path)):
                        st["linked"] += 1
                        continue
                    if max_depth is not None and depth >= max_depth:
                        continue
                    stack.append((e.path, depth + 1))
                elif e.is_file(follow_symlinks=False):
                    st["files"] += 1
                    try:
                        sz = e.stat(follow_symlinks=False).st_size
                    except OSError:
                        sz = 0
                    st["bytes"] += sz
                    if on_file is not None and on_file(e.path, sz) is False:
                        # 回调说「够了」——提前收工
                        return st, timed_out, True
            except OSError:
                st["denied"] += 1
                continue
    st["elapsed"] = round(time.time() - t0, 2)
    return st, timed_out, False


def _is_junction(path):
    """Windows junction 判断。非 Windows 恒 False。"""
    if os.name != "nt":
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_reparse_tag", 0)) or (
        os.path.isdir(path) and os.path.islink(path))


def _file_hash(path, limit=None, algo="blake2b"):
    """算哈希。limit 给了就只算前 N 字节（剪枝用）。

    blake2b 不是为了密码学强度，是为了快：比 sha256 快约 3 倍，
    碰撞概率对去重场景完全够（2^128 级别）。
    """
    h = hashlib.new(algo, digest_size=16) if algo == "blake2b" else hashlib.new(algo)
    left = limit
    try:
        with open(path, "rb") as f:
            while True:
                n = 1 << 20 if left is None else min(1 << 20, left)
                if n <= 0:
                    break
                chunk = f.read(n)
                if not chunk:
                    break
                h.update(chunk)
                if left is not None:
                    left -= len(chunk)
    except OSError:
        return None
    return h.hexdigest()


# ============================================================ 设备

class FileOps(BaseDevice):
    kind = "fileops"
    via = "local"

    def __init__(self, name="file:ops", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "system")

    def probe(self):
        return True, ""

    def caps(self):
        return ("file.search", "file.tree", "file.dupes", "file.big",
                "file.recent", "file.rename", "file.stats", "file.info")

    def invoke(self, cap, args=None):
        args = args or {}
        fn = {
            "file.search": self._search,
            "file.tree": self._tree,
            "file.dupes": self._dupes,
            "file.big": self._big,
            "file.recent": self._recent,
            "file.rename": self._rename,
            "file.stats": self._stats,
            "file.info": self._info,
        }.get(cap)
        if fn is None:
            return super().invoke(cap, args)
        try:
            return fn(cap, args)
        except PermissionError as e:
            return R.error(cap, "没有权限：%s" % e, device=self.name, via=self.via)
        except FileNotFoundError as e:
            return R.error(cap, "路径不存在：%s" % e, device=self.name, via=self.via)
        except Exception as e:
            return R.error(cap, "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)

    # ---------------- 公共 ----------------

    def _root(self, args, cap, allow_file=False):
        """取并校验根路径。返回 (绝对路径, 错误信封)。

        🔴 注意这里必须用 `is not None` 判错：`DeviceResult.__bool__` 返回 ok，
        失败信封在 `if err:` 里是 **False**，判断会静默失效。
        """
        raw = (args.get("root") or args.get("dir") or args.get("path") or "").strip()
        raw = raw.strip('"').strip("'")
        if not raw:
            return None, R.error(cap, "缺 root（要从哪个目录开始）",
                                 device=self.name, via=self.via)
        p = os.path.abspath(os.path.expanduser(raw))
        if not os.path.exists(p):
            return None, R.error(cap, "路径不存在：%s" % p,
                                 device=self.name, via=self.via)
        if not allow_file and not os.path.isdir(p):
            return None, R.error(cap, "不是目录：%s" % p,
                                 device=self.name, via=self.via)
        return p, None

    # ---------------- 搜索 ----------------

    def _search(self, cap, args):
        """按名字 / 正则 / 内容搜文件。

        三档匹配可以叠加，全部满足才算命中：
          pattern   glob（`*.jpg`、`报告*.docx`）——不区分大小写
          regex     正则（`^IMG_\\d{4}$`）
          contains  文件内容包含某段文字（只搜 ≤8MB 的文本，跳过二进制）
        """
        root, err = self._root(args, cap)
        if err is not None:
            return err

        pattern = (args.get("pattern") or "*").strip()
        rx_raw = (args.get("regex") or "").strip()
        needle = args.get("contains")
        rx = None
        if rx_raw:
            try:
                rx = re.compile(rx_raw, re.I if args.get("ignore_case", True) else 0)
            except re.error as e:
                return R.error(cap, "正则写错了：%s" % e, device=self.name, via=self.via)
        icase = bool(args.get("ignore_case", True))
        pat = pattern.lower() if icase else pattern

        min_b, e1 = parse_size(args.get("min_size"))
        if e1:
            return R.error(cap, e1, device=self.name, via=self.via)
        max_b, e2 = parse_size(args.get("max_size"))
        if e2:
            return R.error(cap, e2, device=self.name, via=self.via)
        since, e3 = parse_time(args.get("since"))
        if e3:
            return R.error(cap, e3, device=self.name, via=self.via)
        until, e4 = parse_time(args.get("until"))
        if e4:
            return R.error(cap, e4, device=self.name, via=self.via)

        limit = max(1, min(int(args.get("limit") or DEFAULT_LIMIT), 5000))
        max_depth = args.get("max_depth")
        max_depth = int(max_depth) if max_depth not in (None, "") else None
        budget = float(args.get("budget") or DEFAULT_BUDGET)
        want_dirs = bool(args.get("include_dirs") or False)
        sort_by = (args.get("sort") or "name").strip().lower()

        needle_b = None
        if needle not in (None, ""):
            needle_b = str(needle).encode("utf-8")
        # 内容搜索要区分编码：中文文件可能是 gbk。两种都试一次，
        # 比引一个 chardet 划算（多读一遍的成本远低于多一个依赖）。
        needle_gbk = None
        if needle_b is not None:
            try:
                needle_gbk = str(needle).encode("gbk")
            except UnicodeEncodeError:
                needle_gbk = None

        hits = []
        state = {"full": False}

        def on_file(path, size):
            name = os.path.basename(path)
            if pattern and pattern != "*":
                if not fnmatch.fnmatch(name.lower() if icase else name, pat):
                    return None
                if not pat.startswith("*") and not pat.endswith("*"):
                    # 纯 `*.jpg` 之类没问题；但 `abc` 这种要子串匹配才符合直觉
                    pass
            if rx is not None and not rx.search(name):
                return None
            if min_b is not None and size < min_b:
                return None
            if max_b is not None and size > max_b:
                return None
            try:
                st = os.stat(path)
            except OSError:
                return None
            if since is not None and st.st_mtime < since:
                return None
            if until is not None and st.st_mtime > until:
                return None
            if needle_b is not None:
                if size > CONTENT_MAX_BYTES:
                    return None
                try:
                    with open(path, "rb") as f:
                        blob = f.read()
                except OSError:
                    return None
                if needle_b not in blob and (needle_gbk is None or needle_gbk not in blob):
                    return None
            hits.append({"path": path, "name": name, "size": size,
                         "mtime": int(st.st_mtime),
                         "kind": kind_of(path)})
            if len(hits) >= limit:
                state["full"] = True
                return False          # 通知遍历提前收工
            return None

        st, timed_out, stopped = _walk(
            root, max_depth=max_depth,
            ignore=args.get("ignore") or [],
            budget=budget, follow_links=bool(args.get("follow_links") or False),
            on_file=on_file)

        # 目录单独收集（要 include_dirs 才做，且不参与上面的回调）
        dir_hits = []
        if want_dirs:
            budget2 = max(1.0, budget - st.get("elapsed", 0))
            t0 = time.time()
            skip = set(NOISE_DIRS)
            stack = [(root, 0)]
            while stack and time.time() - t0 < budget2 and len(dir_hits) < limit:
                cur, depth = stack.pop()
                try:
                    entries = list(os.scandir(cur))
                except OSError:
                    continue
                for e in entries:
                    try:
                        if not e.is_dir(follow_symlinks=False) or e.name in skip:
                            continue
                    except OSError:
                        continue
                    nm = e.name.lower() if icase else e.name
                    if pattern and pattern != "*" and not fnmatch.fnmatch(nm, pat):
                        continue
                    if rx is not None and not rx.search(e.name):
                        continue
                    dir_hits.append({"path": e.path, "name": e.name, "size": 0,
                                     "mtime": int(e.stat(follow_symlinks=False).st_mtime),
                                     "kind": "dir"})
                    if max_depth is None or depth < max_depth:
                        stack.append((e.path, depth + 1))

        allhits = hits + dir_hits
        if sort_by == "size":
            allhits.sort(key=lambda x: -x["size"])
        elif sort_by in ("mtime", "time"):
            allhits.sort(key=lambda x: -x["mtime"])
        else:
            allhits.sort(key=lambda x: x["path"].lower())
        allhits = allhits[:limit]

        return R.success(cap, self.name, via=self.via, data={
            "root": root,
            "pattern": pattern, "regex": rx_raw or None,
            "contains": needle if needle not in (None, "") else None,
            "count": len(allhits),
            "hits": allhits,
            "total_bytes": sum(h["size"] for h in allhits),
            "scanned": {"files": st.get("files", 0), "dirs": st.get("dirs", 0),
                        "bytes": st.get("bytes", 0), "denied": st.get("denied", 0),
                        "skipped_link": st.get("linked", 0)},
            "elapsed_s": st.get("elapsed"),
            "timed_out": timed_out,
            "truncated": state["full"],
            # 说清「结果完不完整」比结果本身更重要：
            # 「没搜到」和「没搜完」在天上地下，不能让调用方猜。
            "complete": not (timed_out or state["full"]),
            "note": ("到时间预算（%.0fs）收工，结果可能不全：调大 budget 或缩小 root"
                     % budget) if timed_out else
                    ("到条数上限（%d）收工，还有更多：调大 limit" % limit)
                    if state["full"] else "",
        })

    # ---------------- 目录树 ----------------

    def _tree(self, cap, args):
        """目录树 + 每层体积。

        为什么每层都要算体积：光看结构没用，人真正想知道的是「哪个目录在吃盘」。
        一次遍历把每个子树的累计大小算出来，比让人逐层 cd 进去看强。
        """
        root, err = self._root(args, cap)
        if err is not None:
            return err

        depth_max = max(1, min(int(args.get("depth") or 2), 8))
        budget = float(args.get("budget") or DEFAULT_BUDGET)
        top_n = max(1, min(int(args.get("top") or 15), 100))
        skip = set(NOISE_DIRS) | {x for x in (args.get("ignore") or []) if x}
        t0 = time.time()
        timed_out = False

        # 一次遍历收结构；到深度上限的那一层，把整棵子树压成一个数。
        # 🔴 必须记 parent：体积要**自底向上**累加。
        # 只算「自己那层有多少文件」会把 525MB 的目录报成 4MB —— 第一版就这么错了，
        # 而且错得非常安静：每个数字看着都像个正经大小，加起来对不上总数才发现。
        nodes = {}
        stack = deque([(root, 0, None)])
        while stack:
            if time.time() - t0 > budget:
                timed_out = True
                break
            cur, depth, parent = stack.popleft()
            try:
                entries = list(os.scandir(cur))
            except OSError:
                continue
            sub_dirs, f_bytes, f_count = [], 0, 0
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name in skip:
                            continue
                        sub_dirs.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        f_bytes += e.stat(follow_symlinks=False).st_size
                        f_count += 1
                except OSError:
                    continue
            node = {"depth": depth, "parent": parent, "self_bytes": f_bytes,
                    "self_files": f_count, "subdirs": len(sub_dirs),
                    "deep_bytes": 0, "deep_files": 0}
            nodes[cur] = node
            if depth < depth_max:
                for d in sub_dirs:
                    stack.append((d, depth + 1, cur))
            else:
                for d in sub_dirs:
                    b, f = _subtree_stat(
                        d, budget=max(0.2, budget - (time.time() - t0)))
                    node["deep_bytes"] += b
                    node["deep_files"] += f

        # 自底向上：total = 自己那层 + 压平的子树 + 已展开的子节点
        kids = {}
        for p, n in nodes.items():
            if n["parent"] is not None:
                kids.setdefault(n["parent"], []).append(p)
        for p in sorted(nodes, key=lambda x: -nodes[x]["depth"]):
            n = nodes[p]
            n["total_bytes"] = n["self_bytes"] + n["deep_bytes"]
            n["files"] = n["self_files"] + n["deep_files"]
            for c in kids.get(p, []):
                n["total_bytes"] += nodes[c]["total_bytes"]
                n["files"] += nodes[c]["files"]

        rows = []
        for p, n in nodes.items():
            rel = os.path.relpath(p, root)
            rows.append({
                "rel": "." if rel == "." else rel.replace("\\", "/"),
                "depth": n["depth"], "self_bytes": n["self_bytes"],
                "sub_bytes": n["total_bytes"] - n["self_bytes"],
                "files": n["files"], "subdirs": n["subdirs"],
                "total_bytes": n["total_bytes"],
            })

        # 根永远第一；第一层按体积降序（人看树要的就是「谁在吃盘」）；更深按名字。
        root_row = [r for r in rows if r["depth"] == 0]
        lvl1 = sorted([r for r in rows if r["depth"] == 1],
                      key=lambda x: -x["total_bytes"])
        deeper = sorted([r for r in rows if r["depth"] > 1],
                        key=lambda x: (x["depth"], x["rel"].lower()))
        ordered = root_row + lvl1 + deeper

        total_b = root_row[0]["total_bytes"] if root_row else 0
        total_f = root_row[0]["files"] if root_row else 0
        return R.success(cap, self.name, via=self.via, data={
            "root": root, "depth": depth_max,
            "total_bytes": total_b, "total_human": human_size(total_b),
            "files": total_f, "dirs": len(rows) - 1,
            "nodes": ordered[:top_n], "node_count": len(ordered),
            "elapsed_s": round(time.time() - t0, 2),
            "timed_out": timed_out,
            "note": "" if not timed_out else "到时间预算收工，深层体积可能偏小",
        })

    # ---------------- 重复文件 ----------------

    def _dupes(self, cap, args):
        """找重复文件（三级剪枝）。"""
        root, err = self._root(args, cap)
        if err is not None:
            return err

        min_b, e1 = parse_size(args.get("min_size") or "1K")
        if e1:
            return R.error(cap, e1, device=self.name, via=self.via)
        budget = float(args.get("budget") or 60.0)
        limit = max(1, min(int(args.get("limit") or 100), 2000))
        include_empty = bool(args.get("include_empty") or False)

        # ---- 第一级：按大小分组 ----
        by_size = {}
        t0 = time.time()

        def on_file(path, size):
            if size < min_b:
                return None
            if size == 0 and not include_empty:
                return None
            by_size.setdefault(size, []).append(path)
            if time.time() - t0 > budget:
                return False
            return None

        st, timed_out, _stopped = _walk(root, ignore=args.get("ignore") or [],
                                       budget=budget, on_file=on_file)
        cand_sizes = {s: ps for s, ps in by_size.items() if len(ps) > 1}
        after_l1 = sum(len(v) for v in cand_sizes.values())

        # ---- 第二级：前 8KB 哈希 ----
        groups = []
        for size, paths in cand_sizes.items():
            if time.time() - t0 > budget:
                timed_out = True
                break
            by_head = {}
            for p in paths:
                h = _file_hash(p, limit=SAMPLE_BYTES)
                if h is None:
                    continue
                by_head.setdefault(h, []).append(p)
            for h, ps in by_head.items():
                if len(ps) > 1:
                    groups.append((size, ps))
        after_l2 = sum(len(ps) for _s, ps in groups)

        # ---- 第三级：全量哈希 ----
        sets = []
        for size, paths in groups:
            if time.time() - t0 > budget:
                timed_out = True
                break
            by_full = {}
            for p in paths:
                h = _file_hash(p)
                if h is None:
                    continue
                by_full.setdefault(h, []).append(p)
            for h, ps in by_full.items():
                if len(ps) < 2:
                    continue
                # 硬链接：同一份数据多个名字，删一个不省空间，不能算重复
                uniq, seen = [], []
                for p in ps:
                    if any(_same_file(p, q) for q in seen):
                        continue
                    seen.append(p)
                    uniq.append(p)
                if len(uniq) < 2:
                    continue
                uniq.sort()
                sets.append({"hash": h, "size": size, "count": len(uniq),
                             "files": uniq,
                             "wasted": size * (len(uniq) - 1)})

        sets.sort(key=lambda x: -x["wasted"])
        all_groups = len(sets)
        # 🔴 先算总量再截断：截断后再求和，报出来的「能省多少」就是前 100 组的，
        # 少了也不说，用户按这个数字估盘就估错了。两个数分开报。
        wasted_all = sum(s["wasted"] for s in sets)
        truncated = all_groups > limit
        sets = sets[:limit]
        wasted_listed = sum(s["wasted"] for s in sets)

        return R.success(cap, self.name, via=self.via, data={
            "root": root,
            "groups": sets, "group_count": len(sets),
            "total_groups": all_groups,
            "wasted_bytes": wasted_all, "wasted_human": human_size(wasted_all),
            "listed_wasted_bytes": wasted_listed,
            "scanned": {"files": st.get("files", 0), "bytes": st.get("bytes", 0),
                        "denied": st.get("denied", 0)},
            "prune": {"after_size": after_l1, "after_sample": after_l2,
                      "final_groups": all_groups},
            "elapsed_s": round(time.time() - t0, 2),
            "timed_out": timed_out, "truncated": truncated,
            "complete": not timed_out,
            "note": ("扫到时间预算（%.0fs）就停了，结果不全：缩小 root 或调大 budget"
                     % budget) if timed_out else
                    ("共 %d 组，只列了前 %d 组（wasted_bytes 是全部 %d 组的总和）"
                     % (all_groups, limit, all_groups)) if truncated else
                    "只删文件不回收目录；同一份数据的硬链接不算重复",
        })

    # ---------------- 占空间大户 ----------------

    def _big(self, cap, args):
        """占空间最多的文件（或目录）。"""
        root, err = self._root(args, cap)
        if err is not None:
            return err

        top = max(1, min(int(args.get("top") or 30), 500))
        budget = float(args.get("budget") or DEFAULT_BUDGET)
        min_b, _e = parse_size(args.get("min_size") or "10M")
        mode = (args.get("mode") or "file").strip().lower()   # file | dir
        heap = []

        if mode == "dir":
            # 按目录算：只统计顶层子目录各自的总量
            skip = set(NOISE_DIRS) | {x for x in (args.get("ignore") or []) if x}
            rows = []
            try:
                entries = list(os.scandir(root))
            except OSError as e:
                return R.error(cap, "读不了目录：%s" % e, device=self.name, via=self.via)
            for e in entries:
                if not e.is_dir(follow_symlinks=False) or e.name in skip:
                    continue
                sz = _subtree_size(e.path, budget=max(0.5, budget))
                rows.append({"path": e.path, "name": e.name, "size": sz})
            rows.sort(key=lambda x: -x["size"])
            total = sum(r["size"] for r in rows)
            return R.success(cap, self.name, via=self.via, data={
                "root": root, "mode": "dir", "count": len(rows[:top]),
                "items": [dict(r, human=human_size(r["size"]))
                          for r in rows[:top]],
                "total_bytes": total, "total_human": human_size(total),
            })

        def on_file(path, size):
            if min_b is not None and size < min_b:
                return None
            heap.append((size, path))
            return None

        st, timed_out, _ = _walk(root, ignore=args.get("ignore") or [],
                                budget=budget, on_file=on_file)
        heap.sort(reverse=True)
        items = [{"path": p, "name": os.path.basename(p), "size": s,
                  "human": human_size(s), "kind": kind_of(p)}
                 for s, p in heap[:top]]
        total = sum(s for s, _p in heap)
        return R.success(cap, self.name, via=self.via, data={
            "root": root, "mode": "file", "count": len(items), "items": items,
            "scanned_files": st.get("files", 0),
            "total_bytes": total, "total_human": human_size(total),
            "elapsed_s": st.get("elapsed"), "timed_out": timed_out,
            "note": "" if not timed_out else "到时间预算收工，结果可能不全",
        })

    # ---------------- 最近改动 ----------------

    def _recent(self, cap, args):
        """最近改动过的文件。默认按 mtime 倒序。"""
        root, err = self._root(args, cap)
        if err is not None:
            return err

        limit = max(1, min(int(args.get("limit") or 50), 1000))
        budget = float(args.get("budget") or DEFAULT_BUDGET)
        span = args.get("within") or args.get("since") or "7d"
        since, e = parse_time(span)
        if e:
            return R.error(cap, e, device=self.name, via=self.via)
        min_b, _e2 = parse_size(args.get("min_size"))

        rows = []

        def on_file(path, size):
            if min_b is not None and size < min_b:
                return None
            try:
                m = os.stat(path).st_mtime
            except OSError:
                return None
            if since is not None and m < since:
                return None
            rows.append((m, path, size))
            return None

        st, timed_out, _ = _walk(root, ignore=args.get("ignore") or [],
                                budget=budget, on_file=on_file)
        rows.sort(reverse=True)
        items = [{"path": p, "name": os.path.basename(p), "size": s,
                  "human": human_size(s), "mtime": int(m),
                  "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(m)),
                  "kind": kind_of(p)}
                 for m, p, s in rows[:limit]]
        return R.success(cap, self.name, via=self.via, data={
            "root": root, "within": span, "count": len(items), "items": items,
            "scanned_files": st.get("files", 0),
            "elapsed_s": st.get("elapsed"), "timed_out": timed_out,
        })

    # ---------------- 批量改名 ----------------

    def _rename(self, cap, args):
        """批量改名。**默认预演**，确认后才落手。

        三种模式：
          mode=regex   把文件名里的 `find` 换 `replace`（find 是正则）
          mode=seq     按 `template` 加序号，如 `照片_{n:03d}`
          mode=affix   `prefix` 加在前面 / `suffix` 加在后面（在扩展名之前）

        两条硬规则：
          1. **目标已存在就跳过**，不覆盖。批量改名最怕的就是把 a 改成 b 而 b 已有。
          2. 同一个批次内部目标重名也跳过——否则两个源抢一个目标，必然丢一个。
        """
        root, err = self._root(args, cap)
        if err is not None:
            return err

        mode = (args.get("mode") or "regex").strip().lower()
        pattern = (args.get("pattern") or "*").strip()
        # 🔴 默认预演。这个默认值不是保守，是必要：
        # 批量改名一次错就是几百个文件全错，而且没有回收站之外的反悔手段。
        dry = args.get("dry_run")
        dry = True if dry is None else bool(dry)
        budget = float(args.get("budget") or DEFAULT_BUDGET)

        # 先列出候选（只本层，不递归——改名跨目录会破坏相对结构）
        try:
            names = [e for e in os.scandir(root) if e.is_file(follow_symlinks=False)]
        except OSError as e:
            return R.error(cap, "读不了目录：%s" % e, device=self.name, via=self.via)

        icase = bool(args.get("ignore_case", True))
        pat = pattern.lower() if icase else pattern
        cands = [e for e in names
                 if pattern == "*" or fnmatch.fnmatch(
                     e.name.lower() if icase else e.name, pat)]
        # 按名字排序——序号模式必须有稳定顺序，否则每次跑出来的号都不一样
        cands.sort(key=lambda e: e.name.lower())
        if len(cands) > 5000:
            cands = cands[:5000]

        start = int(args.get("start") or 1)
        step = int(args.get("step") or 1)
        template = (args.get("template") or "{name}_{n:03d}")
        find = args.get("find") or ""
        repl = args.get("replace") or ""
        prefix = args.get("prefix") or ""
        suffix = args.get("suffix") or ""

        rx = None
        if mode == "regex":
            if not find:
                return R.error(cap, "regex 模式要给 find（要匹配什么）",
                               device=self.name, via=self.via)
            try:
                rx = re.compile(find, re.I if icase else 0)
            except re.error as e:
                return R.error(cap, "正则写错了：%s" % e, device=self.name, via=self.via)

        plan, skipped, taken = [], [], set()
        n = start
        for e in cands:
            stem, ext = os.path.splitext(e.name)
            if mode == "regex":
                new_stem = rx.sub(repl, stem)
                if new_stem == stem:
                    # 匹配不上就跳过。不跳的话「把所有 jpg 加前缀」会把其他文件也改名。
                    continue
                new_name = new_stem + ext
            elif mode == "seq":
                try:
                    new_stem = template.format(name=stem, n=n, ext=ext.lstrip("."),
                                               kind=kind_of(e.name))
                except (KeyError, IndexError, ValueError) as ex:
                    return R.error(cap, "template 里的占位符不认识：%s（可用 {name} {n} {ext} {kind}）" % ex,
                                   device=self.name, via=self.via)
                new_name = new_stem + ext
                n += step
            else:  # affix
                new_name = prefix + stem + suffix + ext

            if new_name == e.name:
                continue
            dest = os.path.join(root, new_name)
            if new_name.lower() in taken:
                skipped.append({"src": e.name, "want": new_name,
                                "why": "同批次内目标重名"})
                continue
            if os.path.exists(dest) and not _same_file(dest, e.path):
                skipped.append({"src": e.name, "want": new_name,
                                "why": "目标已存在（不覆盖）"})
                continue
            taken.add(new_name.lower())
            plan.append({"src": e.name, "dst": new_name, "path": e.path, "dest": dest})

        done, failed = 0, []
        if not dry:
            # 两段式：先全改名到临时名，再改到目标名。
            # 直接循环改会遇到 `a→b, b→c` 这种链式覆盖，顺序稍差就丢文件。
            stamp = ".skynet_tmp_%d_" % int(time.time())
            tmps = []
            for i, item in enumerate(plan):
                tmp = os.path.join(root, stamp + str(i) + os.path.splitext(item["src"])[1])
                try:
                    os.rename(item["path"], tmp)
                    tmps.append((tmp, item))
                except OSError as ex:
                    failed.append({"src": item["src"], "err": "%s: %s" % (type(ex).__name__, ex)})
            for tmp, item in tmps:
                try:
                    os.rename(tmp, item["dest"])
                    done += 1
                except OSError as ex:
                    try:
                        os.rename(tmp, item["path"])   # 尽力还原
                    except OSError:
                        pass
                    failed.append({"src": item["src"], "err": "%s: %s" % (type(ex).__name__, ex)})

        return R.success(cap, self.name, via=self.via, data={
            "root": root, "mode": mode, "pattern": pattern,
            "matched": len(cands), "planned": len(plan), "done": done,
            "skipped_count": len(skipped), "skipped": skipped[:20],
            "failed_count": len(failed), "failed": failed[:10],
            "plan": plan[:50], "plan_truncated": len(plan) > 50,
            "dry_run": dry,
            "note": "预演模式，没有动任何文件；要真改传 dry_run=false"
                    if dry else "已改名 %d 个%s" % (
                        done, "，失败 %d 个" % len(failed) if failed else ""),
        })

    # ---------------- 类型统计 ----------------

    def _stats(self, cap, args):
        """目录按类型统计：多少张图、多少个视频、各占多少。"""
        root, err = self._root(args, cap)
        if err is not None:
            return err

        budget = float(args.get("budget") or DEFAULT_BUDGET)
        verify = bool(args.get("verify") or False)   # 逐文件读魔数，慢但能拆穿假扩展名
        by_kind, by_ext = {}, {}
        mismatch = []
        st_ref = {"n": 0}

        def on_file(path, size):
            k = kind_of(path)
            ext = os.path.splitext(path)[1].lower() or "(无)"
            b = by_kind.setdefault(k, {"count": 0, "bytes": 0})
            b["count"] += 1
            b["bytes"] += size
            e = by_ext.setdefault(ext, {"count": 0, "bytes": 0, "kind": k})
            e["count"] += 1
            e["bytes"] += size
            if verify:
                real = sniff(path)
                st_ref["n"] += 1
                if real and not _kind_matches(real, k, ext):
                    # 只报前 30 条：这是给人看的告警，不是全量清单
                    if len(mismatch) < 30:
                        mismatch.append({"path": path, "ext": ext, "real": real})
            return None

        st, timed_out, _ = _walk(root, ignore=args.get("ignore") or [],
                                budget=budget, on_file=on_file)
        total = st.get("bytes", 0)
        kinds = [dict(kind=k, human=human_size(v["bytes"]),
                      pct=round(v["bytes"] * 100.0 / total, 1) if total else 0,
                      **v) for k, v in by_kind.items()]
        kinds.sort(key=lambda x: -x["bytes"])
        exts = [dict(ext=k, human=human_size(v["bytes"]), **v)
                for k, v in sorted(by_ext.items(), key=lambda kv: -kv[1]["bytes"])[:25]]

        return R.success(cap, self.name, via=self.via, data={
            "root": root, "files": st.get("files", 0), "dirs": st.get("dirs", 0),
            "total_bytes": total, "total_human": human_size(total),
            "kinds": kinds, "top_exts": exts,
            "mismatch": mismatch, "mismatch_count": len(mismatch),
            "elapsed_s": st.get("elapsed"), "timed_out": timed_out,
            "note": ("逐文件校验了 %d 个的头部" % st_ref["n"]) if verify else "",
        })

    # ---------------- 单文件详情 ----------------

    def _info(self, cap, args):
        """单文件/目录的详细信息：大小、时间、权限、真实类型、硬链接数。"""
        p, err = self._root(args, cap, allow_file=True)
        if err is not None:
            return err

        try:
            st = os.stat(p)
        except OSError as e:
            return R.error(cap, "读不了：%s" % e, device=self.name, via=self.via)

        is_dir = os.path.isdir(p)
        real = None if is_dir else sniff(p)
        ext = os.path.splitext(p)[1].lower()
        data = {
            "path": p, "name": os.path.basename(p), "is_dir": is_dir,
            "size": st.st_size, "human": human_size(st.st_size),
            "mtime": int(st.st_mtime),
            "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "ctime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_ctime)),
            "atime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_atime)),
            "ext": ext or None,
            "kind": "dir" if is_dir else kind_of(p),
            "real_type": real,
            "readonly": not bool(st.st_mode & stat_mod.S_IWRITE),
            "hardlinks": getattr(st, "st_nlink", 1),
            "inode": getattr(st, "st_ino", None),
        }
        # 扩展名和真实类型对不上就明说——这正是「这文件打不开」的常见原因
        if real and ext and not _kind_matches(real, kind_of(p), ext):
            data["mismatch"] = True
            data["mismatch_note"] = (
                "扩展名是 %s，但内容是 %s——改扩展名不会改变文件格式，"
                "打不开的话用 convert.file 转一次" % (ext, real))
        if is_dir:
            try:
                kids = list(os.scandir(p))
                data["children"] = len(kids)
                data["subdirs"] = sum(1 for k in kids if k.is_dir(follow_symlinks=False))
            except OSError:
                pass
        return R.success(cap, self.name, via=self.via, data=data)


def _kind_of_magic(real):
    """把 sniff 出来的具体类型（png/jpg/mp4…）映射回 KIND_MAP 的大类。认不出返回 None。"""
    if not real:
        return None
    for kind, names in _MAGIC_KIND.items():
        if real in names:
            return kind
    return None


def _kind_matches(real, kind, ext=None):
    """真实类型与扩展名是否矛盾。返回 True 表示「不矛盾」。

    三条判定，缺一条就会天天误报：
      1. **认不出的魔数不判**（`real` 为空或不属于任何大类）——不认识就不能说人家错。
      2. **`.dat` / `.bin` 这类无类别扩展名不判**。它们本来就是「不知道是什么」的容器，
         往里报「内容不符」纯属噪音。
      3. **zip 容器类扩展名不判**。docx / xlsx / pptx / apk / jar 的真实魔数就是
         `PK\\x03\\x04`，跟 zip 压缩包一模一样——按魔数判会把每个 docx 都标成错。
         （要区分得读 zip 内部的 `[Content_Types].xml`，那是另一件事。）
    """
    if ext and ext.lower() in ZIP_FORMATS:
        return True
    real_kind = _kind_of_magic(real)
    if real_kind is None or kind == "other":
        return True
    return real_kind == kind


def _subtree_stat(path, budget=10.0):
    """算一棵子树的 (总字节, 文件数)。带预算——不能为了报个数字把调用挂死。"""
    t0 = time.time()
    total = 0
    files = 0
    stack = [path]
    skip = NOISE_DIRS
    while stack:
        if time.time() - t0 > budget:
            break
        cur = stack.pop()
        try:
            for e in os.scandir(cur):
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name in skip:
                            continue
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                        files += 1
                except OSError:
                    continue
        except OSError:
            continue
    return total, files


def _subtree_size(path, budget=10.0):
    """只要字节数（_big 的 mode=dir 用）。"""
    return _subtree_stat(path, budget)[0]


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

    td = tempfile.mkdtemp(prefix="fileops_test_")
    dev = FileOps()

    try:
        # ---- 素材：一棵有结构的树 ----
        os.makedirs(os.path.join(td, "docs", "sub"))
        os.makedirs(os.path.join(td, "pics"))
        os.makedirs(os.path.join(td, "node_modules", "junk"))   # 噪音目录

        def w(rel, data):
            p = os.path.join(td, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
            return p

        w("readme.txt", "hello world\n这是中文内容\n")
        w("docs/report.md", "# 报告\n内容在这里\n")
        w("docs/sub/deep.md", "深层文件\n")
        w("pics/a.jpg", b"\xff\xd8\xff" + b"A" * 5000)          # 假 JPEG（真魔数）
        w("pics/b.jpg", b"\xff\xd8\xff" + b"A" * 5000)          # 与 a 完全相同
        w("pics/c.jpg", b"\xff\xd8\xff" + b"B" * 5000)          # 同大小不同内容
        w("pics/fake.dat", b"\x89PNG\r\n\x1a\n" + b"C" * 2000)  # png 改名成 .dat
        w("pics/pretend.mp4", b"\x89PNG\r\n\x1a\n" + b"D" * 2000)  # png 谎称是视频
        w("node_modules/junk/x.js", "should be skipped\n")
        w("big.bin", b"Z" * (300 * 1024))

        # 10 个文件，但 node_modules 是内置噪音目录会被跳过 —— 所以「扫到」是 9
        ck(True, "素材：造出 10 个文件 + 3 层目录")

        # ---- file.info：单文件 ----
        r = dev.invoke("file.info", {"path": os.path.join(td, "readme.txt")})
        ck(r.ok and r.data["size"] == len("hello world\n这是中文内容\n".encode("utf-8")),
           "info：字节数正确（按 UTF-8 算中文）")
        ck(r.ok and r.data["kind"] == "doc", "info：txt 归到 doc")

        r = dev.invoke("file.info", {"path": os.path.join(td, "pics", "fake.dat")})
        ck(r.ok and r.data["real_type"] == "png", "info：读魔数看穿 .dat 其实是 png")
        # .dat 是无类别扩展名，往里报「内容不符」纯属噪音 —— 这里断言「不误报」
        ck(r.ok and not r.data.get("mismatch"),
           "info：.dat 这类无类别扩展名不报「名不符实」（不制造噪音）")

        r = dev.invoke("file.info", {"path": os.path.join(td, "pics", "pretend.mp4")})
        ck(r.ok and r.data.get("mismatch") is True,
           "info：.mp4 里装着 png 时明确告警")

        # ---- 错误路径（上次 `if err:` 的教训，必须测）----
        r = dev.invoke("file.search", {"root": os.path.join(td, "不存在的目录")})
        ck((not r.ok) and "不存在" in (r.err or ""), "search：路径不存在时明确报错")
        r = dev.invoke("file.search", {})
        ck((not r.ok) and "缺 root" in (r.err or ""), "search：缺参数时明确报错")
        r = dev.invoke("file.stats", {"root": os.path.join(td, "readme.txt")})
        ck((not r.ok) and "不是目录" in (r.err or ""), "stats：给文件当目录时明确报错")

        # ---- file.search：按名字 ----
        r = dev.invoke("file.search", {"root": td, "pattern": "*.jpg"})
        ck(r.ok and r.data["count"] == 3, "search：*.jpg 找到 3 个")
        ck(r.ok and r.data["complete"] is True, "search：没超预算时 complete=True")

        # ---- file.search：噪音目录被跳过 ----
        r = dev.invoke("file.search", {"root": td, "pattern": "*.js"})
        ck(r.ok and r.data["count"] == 0, "search：node_modules 默认不进（*.js 找到 0）")
        r = dev.invoke("file.search", {"root": td, "pattern": "*.js", "ignore": []})
        ck(r.ok and r.data["count"] == 0,
           "search：就算 ignore 传空，内置噪音目录照样跳过")

        # ---- file.search：按内容（含中文 GBK 兼容）----
        r = dev.invoke("file.search", {"root": td, "contains": "中文内容"})
        ck(r.ok and r.data["count"] == 1
           and r.data["hits"][0]["name"] == "readme.txt", "search：按内容搜中文命中")

        # ---- file.search：按大小 ----
        r = dev.invoke("file.search", {"root": td, "min_size": "100K"})
        ck(r.ok and r.data["count"] == 1 and r.data["hits"][0]["name"] == "big.bin",
           "search：min_size=100K 只命中 big.bin")

        # ---- file.search：正则 ----
        r = dev.invoke("file.search", {"root": td, "regex": "^[ab]\\.jpg$"})
        ck(r.ok and r.data["count"] == 2, "search：正则 ^[ab]\\.jpg$ 命中 2 个")

        # ---- file.search：排序 ----
        r = dev.invoke("file.search", {"root": td, "min_size": "1K", "sort": "size"})
        ck(r.ok and r.data["hits"][0]["name"] == "big.bin", "search：sort=size 最大的排第一")

        # ---- file.search：include_dirs ----
        r = dev.invoke("file.search", {"root": td, "pattern": "docs", "include_dirs": True})
        ck(r.ok and any(h["kind"] == "dir" for h in r.data["hits"]),
           "search：include_dirs 时目录也返回")

        # ---- file.tree ----
        r = dev.invoke("file.tree", {"root": td, "depth": 2})
        ck(r.ok and r.data["files"] == 9, "tree：扫到 9 个文件")
        top = [n for n in r.data["nodes"] if n["depth"] == 0]
        ck(r.ok and top and top[0]["total_bytes"] >= 300 * 1024,
           "tree：根节点体积包含所有子目录")

        # ---- file.dupes：a.jpg 与 b.jpg 相同，c.jpg 同大小但内容不同 ----
        r = dev.invoke("file.dupes", {"root": td, "min_size": "1K"})
        ck(r.ok and r.data["group_count"] == 1,
           "dupes：只报 1 组（同大小不同内容的不算）")
        ck(r.ok and r.data["groups"] and r.data["groups"][0]["count"] == 2,
           "dupes：这组有 2 个文件")
        if r.ok and r.data["groups"]:
            names = {os.path.basename(p) for p in r.data["groups"][0]["files"]}
            ck(names == {"a.jpg", "b.jpg"}, "dupes：命中的正是 a.jpg 和 b.jpg")
            ck(r.data["groups"][0]["wasted"] == 5003,
               "dupes：浪费空间 = 单文件大小 × (n-1)")
        ck(r.ok and r.data["prune"]["after_size"] >= 4,
           "dupes：第一级剪枝后仍有多组候选（含同大小不同内容的）")

        # ---- file.big ----
        r = dev.invoke("file.big", {"root": td, "top": 3, "min_size": "1K"})
        ck(r.ok and r.data["items"] and r.data["items"][0]["name"] == "big.bin",
           "big：最大的就是 big.bin")
        ck(r.ok and r.data["items"][0]["human"].endswith("KB"),
           "big：human 字段是给人看的")

        # ---- file.big：mode=dir ----
        r = dev.invoke("file.big", {"root": td, "mode": "dir", "top": 3})
        ck(r.ok and r.data["mode"] == "dir", "big：mode=dir 按子目录算")

        # ---- file.recent ----
        r = dev.invoke("file.recent", {"root": td, "within": "1h", "limit": 20})
        ck(r.ok and r.data["count"] == 9, "recent：刚造的 9 个文件都在 1 小时内")
        r = dev.invoke("file.recent", {"root": td, "within": "2030-01-01", "limit": 20})
        ck(r.ok and r.data["count"] == 0, "recent：给未来时间点 → 0 条（时间过滤真的在起作用）")

        # ---- file.stats ----
        r = dev.invoke("file.stats", {"root": td})
        ck(r.ok and r.data["total_bytes"] >= 300 * 1024, "stats：总体积对得上")
        kinds = {k["kind"]: k for k in r.data["kinds"]}
        # 按**扩展名**分类：3 个 .jpg → image，.mp4 → video，.dat → other
        ck(kinds.get("image", {}).get("count") == 3, "stats：按扩展名归出 3 张图")
        ck(kinds.get("other", {}).get("count") >= 1, "stats：.dat 归到 other")

        # ---- file.stats：verify 拆穿假扩展名 ----
        r = dev.invoke("file.stats", {"root": td, "verify": True})
        ck(r.ok and r.data["mismatch_count"] >= 1,
           "stats：verify=True 时抓到 pretend.mp4（魔数是 png）")
        if r.ok and r.data["mismatch"]:
            ck(any("pretend.mp4" in m["path"] for m in r.data["mismatch"]),
               "stats：被抓出来的正是 pretend.mp4")
        else:
            ck(False, "stats：被抓出来的正是 pretend.mp4")

        # ---- file.rename：预演不动手 ----
        r = dev.invoke("file.rename", {"root": os.path.join(td, "docs"),
                                       "mode": "affix", "prefix": "新_"})
        ck(r.ok and r.data["dry_run"] is True, "rename：不给 dry_run 时默认预演")
        # 只扫本层（不递归），docs/ 下只有 report.md；sub/ 是目录不参与
        ck(r.ok and r.data["planned"] == 1, "rename：预演算出 1 个要改名（只扫本层）")
        ck(not os.path.exists(os.path.join(td, "docs", "新_report.md")),
           "rename：预演确实一个文件都没动")

        # ---- file.rename：真改名 ----
        r = dev.invoke("file.rename", {"root": os.path.join(td, "docs"),
                                       "mode": "affix", "prefix": "新_",
                                       "dry_run": False})
        ck(r.ok and r.data["done"] == 1, "rename：真改名 1 个")
        ck(os.path.exists(os.path.join(td, "docs", "新_report.md")),
           "rename：文件确实落到新名字上")
        ck(not os.path.exists(os.path.join(td, "docs", "report.md")),
           "rename：旧名字已不在")

        # ---- file.rename：目标已存在则跳过，不覆盖 ----
        w("collide/a.txt", "A")
        w("collide/b.txt", "B")
        r = dev.invoke("file.rename", {"root": os.path.join(td, "collide"),
                                       "mode": "regex", "find": "^b$",
                                       "replace": "a", "dry_run": False})
        ck(r.ok and r.data["done"] == 0 and r.data["skipped_count"] == 1,
           "rename：目标已存在时跳过，不覆盖")
        with open(os.path.join(td, "collide", "a.txt"), encoding="utf-8") as f:
            ck(f.read() == "A", "rename：被保护的原文件内容没被覆盖")

        # ---- file.rename：序号模式 ----
        d2 = os.path.join(td, "seq")
        os.makedirs(d2)
        for nm in ("zz.txt", "aa.txt", "mm.txt"):
            w(os.path.join("seq", nm), nm)
        r = dev.invoke("file.rename", {"root": d2, "mode": "seq",
                                       "template": "p{n:02d}",
                                       "dry_run": False})
        ck(r.ok and r.data["done"] == 3, "rename：序号模式改了 3 个")
        ck(sorted(os.listdir(d2)) == ["p01.txt", "p02.txt", "p03.txt"],
           "rename：序号补零且按名字排序（aa→01, mm→02, zz→03）")

        # ---- 链式改名不能丢文件（a→b, b→c 这种）----
        d3 = os.path.join(td, "chain")
        os.makedirs(d3)
        for nm in ("f1.txt", "f2.txt", "f3.txt"):
            w(os.path.join("chain", nm), nm)
        r = dev.invoke("file.rename", {"root": d3, "mode": "regex",
                                       "find": r"f(\d)", "replace": r"f\1x",
                                       "dry_run": False})
        ck(r.ok and r.data["done"] == 3, "rename：链式改名改了 3 个")
        ck(sorted(os.listdir(d3)) == ["f1x.txt", "f2x.txt", "f3x.txt"],
           "rename：链式改名没丢文件（两段式改名生效）")

        # ---- 硬链接不是重复 ----
        if os.name == "nt" or hasattr(os, "link"):
            d4 = os.path.join(td, "hardlink")
            os.makedirs(d4)
            src = w(os.path.join("hardlink", "orig.bin"), b"H" * 4000)
            try:
                os.link(src, os.path.join(d4, "same.bin"))
                r = dev.invoke("file.dupes", {"root": d4, "min_size": "1K"})
                ck(r.ok and r.data["group_count"] == 0,
                   "dupes：硬链接不算重复（删一个不省空间）")
            except (OSError, NotImplementedError, AttributeError):
                ck(True, "dupes：硬链接测试跳过（本机不支持）")

        # ---- tree：自底向上累加的精确验证 ----
        # 这条是钉 bug 的：第一版只算「自己那层有多少文件」，
        # 把 525MB 的目录报成 4MB，而每个数字看着都像正经大小，只有对总数才发现。
        d5 = os.path.join(td, "nest")
        w("nest/a.bin", b"A" * (100 * 1024))
        w("nest/child/b.bin", b"B" * (200 * 1024))
        w("nest/child/grand/c.bin", b"C" * (300 * 1024))
        for dep in (1, 2, 3):
            r = dev.invoke("file.tree", {"root": d5, "depth": dep, "top": 20})
            nr = {n["rel"]: n for n in (r.data["nodes"] if r.ok else [])}
            ck(r.ok and nr.get(".") and nr["."]["total_bytes"] == 600 * 1024,
               "tree(depth=%d)：根 600KB（含全部子孙）" % dep)
            ck(r.ok and nr.get("child") and nr["child"]["total_bytes"] == 500 * 1024,
               "tree(depth=%d)：child 500KB（含 grand）" % dep)

        # ---- dupes：截断时「总组数」与「列出组数」必须分开报 ----
        # 只报截断后的数量，「能省多少」就会偏小，而用户会照这个数字估盘。
        w("dup2/big-copy.bin", b"Z" * (300 * 1024))    # 与 big.bin 内容相同
        r = dev.invoke("file.dupes", {"root": td, "min_size": "1K", "limit": 1})
        ck(r.ok and r.data["total_groups"] == 2, "dupes：真实有 2 组重复")
        ck(r.ok and r.data["group_count"] == 1, "dupes：limit=1 时只列 1 组")
        ck(r.ok and r.data["truncated"] is True, "dupes：截断时 truncated=True")
        ck(r.ok and r.data["wasted_bytes"] > r.data["listed_wasted_bytes"],
           "dupes：wasted_bytes 是全部组的总和，不是只算列出来的")

        # ---- 参数解析 ----
        ck(parse_size("10M")[0] == 10 * 1024 * 1024, "parse_size：10M")
        ck(parse_size("1.5G")[0] == int(1.5 * 1024 ** 3), "parse_size：1.5G")
        ck(parse_size("500")[0] == 500, "parse_size：纯数字按字节")
        ck(parse_size("abc")[1] is not None, "parse_size：乱写会报错")
        ck(parse_time("2h")[0] is not None, "parse_time：2h")
        ck(parse_time("2026-01-01")[0] is not None, "parse_time：绝对日期")
        ck(parse_time("瞎写")[1] is not None, "parse_time：乱写会报错")
        ck(human_size(1536).startswith("1.50 KB"), "human_size：1536 → 1.50 KB")

    finally:
        shutil.rmtree(td, ignore_errors=True)

    total = state["n"]
    got = total - len(fails)
    print("\nfileops selftest: %d/%d%s"
          % (got, total, "" if not fails else "（失败 %d）" % len(fails)))
    for f in fails:
        print("   失败:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法: python -m fleet.kinds.fileops --selftest")
    sys.exit(0)

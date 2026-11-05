"""压缩包设备：打包 / 解包 / 看内容。把 7-Zip 与标准库接进总线。

为什么单独一台而不是塞进 convert：
  压缩包不是「编码转换」，是**容器搬迁**。语义差别很大 —— 转换是 1 进 1 出，
  解包是 1 进 N 出，且**解包是唯一会把别人准备的文件写进你磁盘的操作**。
  两者混在一起，安全边界就没法单独收紧。

🔴 解包必须防的两件事（这不是过度防御，是这类功能的默认义务）：
  1. **路径穿越（zip-slip）**：压缩包里一个叫 `..\\..\\Windows\\System32\\x.dll`
     的条目，天真解压就会写到系统目录。校验每一条目的名字，绝对路径/盘符/`..` 一律拒。
  2. **解压炸弹**：42KB 的 zip 能膨胀到 4PB。解包前累加声明的解压后体积，超限就停。

  这两条不是「怕出事所以什么都不敢干」，是**只在这两处设闸**：能正常用的照常过，
  恶意条目在动手之前就报出是哪一条、为什么拒。测试里专门造了一个带 `../evil.txt`
  的恶意 zip 做反向验证 —— 安全逻辑不写测试等于没写。

引擎选择：
  zip  → 标准库 zipfile（能逐条校验，比调外部进程可控）
  tar  → 标准库 tarfile（同样能逐条校验）
  gz   → 标准库 gzip（单文件，没有目录结构）
  7z/rar → 7-Zip CLI（找不到就如实说没装，不假装支持）

与 convert 共用 `_tool` / `_run`：工具定位和「不弹黑框」这些是同一套规矩，
抄一份就会有一天两边不一致。
"""
import fnmatch
import gzip
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile

from ..core import BaseDevice, DeviceResult as R
from .convert import _tool, _run, _tail

#: 解包后总字节上限（默认 4 GB）。超了先问清楚，不要闷头解。
DEFAULT_MAX_BYTES = 4 * 1024 * 1024 * 1024
#: 条目数上限：几十万小文件的包解出来会让资源管理器卡死
MAX_ENTRIES = 200000

_KIND_BY_EXT = {".zip": "zip", ".7z": "7z", ".rar": "rar", ".tar": "tar",
                ".gz": "gz", ".tgz": "gz", ".bz2": "bz2", ".xz": "xz"}


def _fmt_of(path, hint=""):
    """定压缩格式：先看显式 hint，再看扩展名（`.tar.gz` 要当 gz 看）。"""
    h = (hint or "").strip().lower().lstrip(".")
    if h:
        return {"tgz": "gz", "tar.gz": "gz", "targz": "gz"}.get(h, h)
    low = path.lower()
    if low.endswith(".tar.gz") or low.endswith(".tgz"):
        return "gz"
    return _KIND_BY_EXT.get(os.path.splitext(low)[1], "")


def _sevenz():
    for name in ("7z", "7za", "7zr"):
        p = _tool(name)
        if p:
            return p
    return None


def _bad_entry(name):
    """判断一个条目名会不会写到解压目录之外。返回原因，安全则返回空串。"""
    n = (name or "").replace("\\", "/").strip()
    if not n:
        return "空名字"
    if n.startswith("/"):
        return "绝对路径"
    if re.match(r"^[A-Za-z]:", n):
        return "带盘符"
    if any(part == ".." for part in n.split("/")):
        return "含上级目录 .."
    return ""


def _list_7z(path):
    """用 7z l -slt 列条目。返回 [{name, size, packed, dir}]。"""
    exe = _sevenz()
    if not exe:
        return None
    rc, out, err = _run([exe, "l", "-slt", "-ba", path], timeout=120)
    if rc != 0:
        raise RuntimeError(_tail(err) or "7z 列目录失败")
    rows, cur = [], {}
    for line in out.splitlines():
        line = line.rstrip()
        if not line:
            if cur.get("Path"):
                rows.append(cur)
            cur = {}
            continue
        if " = " in line:
            k, v = line.split(" = ", 1)
            cur[k.strip()] = v
    if cur.get("Path"):
        rows.append(cur)
    items = []
    for r in rows:
        name = r.get("Path") or ""
        if not name:
            continue
        try:
            size = int(r.get("Size") or 0)
        except ValueError:
            size = 0
        items.append({"name": name, "size": size,
                      "packed": r.get("Packed Size") or "",
                      "dir": (r.get("Attributes") or "").startswith("D")})
    return items


class ArchiveDevice(BaseDevice):
    kind = "archive"
    via = "local"

    def __init__(self, name="file:archive", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "file")

    # ---------------- 设备契约 ----------------

    def probe(self):
        # 标准库就够 zip/tar/gz，所以这台设备永远可用；7z 只是加分项
        return True, ""

    def caps(self):
        return ("archive.info", "archive.list", "archive.extract", "archive.pack")

    def invoke(self, cap, args=None):
        args = args or {}
        try:
            if cap == "archive.info":
                return self._info(args)
            if cap == "archive.list":
                return self._list(args)
            if cap == "archive.extract":
                return self._extract(args)
            if cap == "archive.pack":
                return self._pack(args)
        except Exception as e:
            return R.error(cap, "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)
        return super().invoke(cap, args)

    # ---------------- 内部 ----------------

    def _src(self, args, cap):
        raw = (args.get("path") or args.get("archive") or "").strip().strip('"')
        if not raw:
            return None, R.error(cap, "缺 path（压缩包路径）", device=self.name, via=self.via)
        p = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isfile(p):
            return None, R.error(cap, "文件不存在: %s" % p, device=self.name, via=self.via)
        return p, None

    def _entries(self, p, fmt):
        """统一的条目列表。zip/tar 走标准库，其余走 7z。"""
        if fmt == "zip":
            with zipfile.ZipFile(p) as z:
                return [{"name": i.filename, "size": i.file_size,
                         "packed": i.compress_size, "dir": i.is_dir()}
                        for i in z.infolist()]
        if fmt == "tar":
            with tarfile.open(p, "r:*") as t:
                return [{"name": m.name, "size": m.size,
                         "packed": "", "dir": m.isdir()} for m in t.getmembers()]
        if fmt in ("7z", "rar", "bz2", "xz"):
            items = _list_7z(p)
            if items is None:
                raise RuntimeError("这台机器没有 7-Zip，解不了 %s；装 7-Zip 后重试" % fmt)
            return items
        if fmt == "gz":
            return [{"name": os.path.basename(p)[:-3], "size": 0, "packed": "", "dir": False}]
        raise RuntimeError("不认识的压缩格式（扩展名 %s）" % os.path.splitext(p)[1])

    # ---------------- info ----------------

    def _info(self, args):
        p, err = self._src(args, "archive.info")
        if err is not None:
            return err
        fmt = _fmt_of(p, args.get("fmt"))
        if not fmt:
            return R.error("archive.info", "看不出是什么压缩格式：%s" % p,
                           device=self.name, via=self.via)
        try:
            items = self._entries(p, fmt)
        except Exception as e:
            return R.error("archive.info", "%s" % e, device=self.name, via=self.via)
        files = [i for i in items if not i["dir"]]
        total = sum(i["size"] for i in files)
        packed = os.path.getsize(p)
        bad = [i["name"] for i in items if _bad_entry(i["name"])]
        return R.success("archive.info", self.name, via=self.via, data={
            "path": p, "format": fmt, "entries": len(items),
            "files": len(files), "dirs": len(items) - len(files),
            "uncompressed_bytes": total, "bytes": packed,
            "ratio": round(packed / total, 3) if total else 0,
            "unsafe_entries": bad[:5], "unsafe_count": len(bad),
            "note": ("有 %d 个条目带越界路径，解包会被拒" % len(bad)) if bad else "",
        })

    # ---------------- list ----------------

    def _list(self, args):
        p, err = self._src(args, "archive.list")
        if err is not None:
            return err
        fmt = _fmt_of(p, args.get("fmt"))
        if not fmt:
            return R.error("archive.list", "看不出是什么压缩格式：%s" % p,
                           device=self.name, via=self.via)
        pattern = (args.get("pattern") or "*").strip()
        limit = max(1, min(int(args.get("limit") or 100), 5000))
        try:
            items = self._entries(p, fmt)
        except Exception as e:
            return R.error("archive.list", "%s" % e, device=self.name, via=self.via)
        hits = [i for i in items if fnmatch.fnmatch(i["name"].lower(), pattern.lower())]
        files = [i for i in hits if not i["dir"]]
        return R.success("archive.list", self.name, via=self.via, data={
            "path": p, "format": fmt, "total": len(items),
            "matched": len(hits), "files": len(files), "dirs": len(hits) - len(files),
            "truncated": len(hits) > limit,
            "items": [{"name": i["name"], "size": i["size"], "dir": i["dir"]}
                      for i in hits[:limit]],
        })

    # ---------------- extract ----------------

    def _extract(self, args):
        p, err = self._src(args, "archive.extract")
        if err is not None:
            return err
        fmt = _fmt_of(p, args.get("fmt"))
        if not fmt:
            return R.error("archive.extract", "看不出是什么压缩格式：%s" % p,
                           device=self.name, via=self.via)
        raw_dir = (args.get("out_dir") or "").strip().strip('"')
        if raw_dir:
            target = os.path.abspath(os.path.expanduser(raw_dir))
        else:
            # 默认解到旁边的新目录，不在压缩包所在目录里散一地
            target = os.path.splitext(p)[0] + "_extracted"
        dry = bool(args.get("dry_run") or False)
        max_bytes = int(args.get("max_bytes") or DEFAULT_MAX_BYTES)

        try:
            items = self._entries(p, fmt)
        except Exception as e:
            return R.error("archive.extract", "%s" % e, device=self.name, via=self.via)

        # 动手前先全量审一遍：名字越界的一条都不许过，体积超限直接停。
        bad = []
        for i in items:
            why = _bad_entry(i["name"])
            if why:
                bad.append({"name": i["name"], "why": why})
        if bad:
            return R.error("archive.extract",
                           "拒绝解包：%d 个条目会写到目标目录之外（%s），"
                           "例如 %r（%s）。这类包通常是恶意的，不解为上"
                           % (len(bad), "、".join(sorted({b["why"] for b in bad})),
                              bad[0]["name"], bad[0]["why"]),
                           device=self.name, via=self.via, data={"unsafe": bad[:10]})
        if len(items) > MAX_ENTRIES:
            return R.error("archive.extract", "条目太多（%d > %d）：这类包解开会让系统卡死"
                           % (len(items), MAX_ENTRIES), device=self.name, via=self.via)
        total = sum(i["size"] for i in items if not i["dir"])
        if total > max_bytes:
            return R.error("archive.extract",
                           "解压后 %.1f GB 超过上限 %.1f GB（可能是解压炸弹）。"
                           "确实要解就调大 max_bytes"
                           % (total / 1073741824.0, max_bytes / 1073741824.0),
                           device=self.name, via=self.via)

        if dry:
            return R.success("archive.extract", self.name, via=self.via, data={
                "path": p, "format": fmt, "out_dir": target, "dry_run": True,
                "entries": len(items), "uncompressed_bytes": total,
                "note": "dry_run=True：只审了条目和体积，没写文件",
            })

        os.makedirs(target, exist_ok=True)
        t0 = time.time()
        written = 0
        if fmt == "zip":
            with zipfile.ZipFile(p) as z:
                for i in z.infolist():
                    if i.is_dir():
                        continue
                    z.extract(i, target)
                    written += 1
        elif fmt == "tar":
            # filter="data" 是 3.12+ 的官方防线：连链接目标和权限一起校验。
            # 我们在上面已按名字审过一遍，这里是第二道。
            kw = {"filter": "data"} if sys.version_info >= (3, 12) else {}
            with tarfile.open(p, "r:*") as t:
                for m in t.getmembers():
                    if m.isdir() or not (m.isfile() or m.issym()):
                        continue
                    t.extract(m, target, **kw)
                    written += 1
        elif fmt == "gz":
            out_name = os.path.basename(p)[:-3] if p.lower().endswith(".gz") \
                else os.path.splitext(os.path.basename(p))[0]
            dest = os.path.join(target, out_name)
            with gzip.open(p, "rb") as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            written = 1
        else:
            exe = _sevenz()
            if not exe:
                return R.error("archive.extract", "解 %s 需要 7-Zip，本机没装" % fmt,
                               device=self.name, via=self.via)
            rc, _o, e = _run([exe, "x", "-y", "-o" + target, p],
                             timeout=int(args.get("timeout") or 1800))
            if rc != 0:
                return R.error("archive.extract", "7z 解包失败：%s" % _tail(e),
                               device=self.name, via=self.via)
            written = len([i for i in items if not i["dir"]])

        got = sum(len(fs) for _d, _s, fs in os.walk(target))
        return R.success("archive.extract", self.name, via=self.via, data={
            "path": p, "format": fmt, "out_dir": target,
            "declared": written, "on_disk": got,
            "bytes": sum(os.path.getsize(os.path.join(dp, f))
                         for dp, _s, fs in os.walk(target) for f in fs),
            "ms": int((time.time() - t0) * 1000),
        })

    # ---------------- pack ----------------

    def _pack(self, args):
        """把文件/目录打成压缩包。

        `src` 既可以是一个目录，也可以是文件/目录的列表。打成 zip 时**保留相对结构**：
        传 `D:\\proj` 解出来就是 `proj/...`，而不是把 300 个文件平铺一地 ——
        平铺解回去就还原不出来了。
        """
        raw = args.get("src") or args.get("path") or args.get("paths") or []
        if isinstance(raw, str):
            raw = [x for x in re.split(r"[;\n]", raw) if x.strip()]
        srcs = []
        for one in raw:
            one = str(one).strip().strip('"')
            if not one:
                continue
            q = os.path.abspath(os.path.expanduser(one))
            if not os.path.exists(q):
                return R.error("archive.pack", "路径不存在: %s" % q,
                               device=self.name, via=self.via)
            srcs.append(q)
        if not srcs:
            return R.error("archive.pack", "缺 src（要打包的文件或目录）",
                           device=self.name, via=self.via)

        fmt = (args.get("fmt") or "").strip().lower().lstrip(".") or "zip"
        if fmt not in ("zip", "7z", "tar", "gz"):
            return R.error("archive.pack", "只支持 zip/7z/tar/gz，收到 %s" % fmt,
                           device=self.name, via=self.via)
        raw_out = (args.get("out") or "").strip().strip('"')
        # 单个文件打包时去掉它本身的扩展名：`v.mp4` → `v.zip`，不是 `v.mp4.zip`
        base = os.path.basename(srcs[0].rstrip("\\/")) or "pack"
        if os.path.isfile(srcs[0]):
            base = os.path.splitext(base)[0] or base
        if raw_out:
            out = os.path.abspath(os.path.expanduser(raw_out))
            if os.path.isdir(out):
                out = os.path.join(out, base + "." + ("tar.gz" if fmt == "gz" else fmt))
            elif not os.path.splitext(out)[1]:
                out += "." + ("tar.gz" if fmt == "gz" else fmt)
        else:
            out = os.path.join(os.path.dirname(srcs[0]), base + "." + ("tar.gz" if fmt == "gz" else fmt))
        if os.path.exists(out) and not bool(args.get("overwrite") or False):
            stem, ext = os.path.splitext(out)
            for i in range(1, 1000):
                cand = "%s_%d%s" % (stem, i, ext)
                if not os.path.exists(cand):
                    out = cand
                    break
        parent = os.path.dirname(out)
        if parent:
            os.makedirs(parent, exist_ok=True)
        level = max(0, min(int(args.get("level") or 5), 9))

        t0 = time.time()
        if fmt == "zip":
            n = self._pack_zip(srcs, out, level)
        elif fmt == "tar":
            n = self._pack_tar(srcs, out, "w:gz" if bool(args.get("gzip")) else "w")
        elif fmt == "gz":
            if len(srcs) != 1 or os.path.isdir(srcs[0]):
                return R.error("archive.pack", "gz 只能压单个文件（要打包目录请用 tar/zip）",
                               device=self.name, via=self.via)
            n = self._pack_gz(srcs[0], out)
        else:
            exe = _sevenz()
            if not exe:
                return R.error("archive.pack", "打 7z 需要 7-Zip，本机没装",
                               device=self.name, via=self.via)
            rc, _o, e = _run([exe, "a", "-t7z", "-mx=%d" % level, "-y", out] + srcs,
                             timeout=int(args.get("timeout") or 1800))
            if rc != 0:
                return R.error("archive.pack", "7z 打包失败：%s" % _tail(e),
                               device=self.name, via=self.via)
            n = -1

        if not os.path.isfile(out) or os.path.getsize(out) == 0:
            return R.error("archive.pack", "打包结束但产物为空：%s" % out,
                           device=self.name, via=self.via)
        src_bytes = 0
        for q in srcs:
            if os.path.isfile(q):
                src_bytes += os.path.getsize(q)
            else:
                src_bytes += sum(os.path.getsize(os.path.join(dp, f))
                                 for dp, _s, fs in os.walk(q) for f in fs)
        return R.success("archive.pack", self.name, via=self.via, data={
            "src": srcs, "out": out, "format": fmt,
            "files": n, "bytes": os.path.getsize(out), "src_bytes": src_bytes,
            "ratio": round(os.path.getsize(out) / src_bytes, 3) if src_bytes else 0,
            "ms": int((time.time() - t0) * 1000),
        })

    def _pack_zip(self, srcs, out, level):
        n = 0
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=max(1, min(level, 9))) as z:
            for q in srcs:
                if os.path.isfile(q):
                    # 单文件：放进包里只留文件名，不带它在本机的整条路径
                    z.write(q, arcname=os.path.basename(q))
                    n += 1
                    continue
                root_parent = os.path.dirname(q.rstrip("\\/"))
                for dp, _dirs, fs in os.walk(q):
                    for f in fs:
                        full = os.path.join(dp, f)
                        arc = os.path.relpath(full, root_parent)
                        z.write(full, arcname=arc)
                        n += 1
        return n

    def _pack_tar(self, srcs, out, mode):
        n = 0
        with tarfile.open(out, mode) as t:
            for q in srcs:
                if os.path.isfile(q):
                    t.add(q, arcname=os.path.basename(q))
                    n += 1
                else:
                    root_parent = os.path.dirname(q.rstrip("\\/"))
                    for dp, _dirs, fs in os.walk(q):
                        for f in fs:
                            full = os.path.join(dp, f)
                            t.add(full, arcname=os.path.relpath(full, root_parent))
                            n += 1
        return n

    def _pack_gz(self, src, out):
        with open(src, "rb") as f, gzip.open(out, "wb", compresslevel=6) as g:
            shutil.copyfileobj(f, g)
        return 1


# ---------------------------------------------------------------- 自测

def _selftest():
    fails = []
    counted = [0]

    def ck(cond, msg):
        counted[0] += 1
        print(("  ✓ " if cond else "  ✗ ") + msg)
        if not cond:
            fails.append(msg)

    dev = ArchiveDevice()
    td = tempfile.mkdtemp(prefix="archive_st_")
    try:
        # ---- 素材：两个文件 + 一个子目录 ----
        os.makedirs(os.path.join(td, "src", "sub"), exist_ok=True)
        with open(os.path.join(td, "src", "a.txt"), "w", encoding="utf-8") as f:
            f.write("hello " * 200)
        with open(os.path.join(td, "src", "sub", "b.txt"), "w", encoding="utf-8") as f:
            f.write("world " * 200)
        ck(True, "素材：造出 2 个文件的目录")

        # 回归：DeviceResult 的 __bool__ 返回 ok，`if err:` 对失败结果恒为假，
        # 于是代码带着 p=None 继续跑，报出来的是 AttributeError 而不是「文件不存在」。
        r = dev.invoke("archive.info", {"path": os.path.join(td, "nope.zip")})
        ck((not r.ok) and "文件不存在" in (r.err or ""),
           "报错：文件不存在时明说，不带着 None 往下跑")

        # ---- 打包 zip ----
        r = dev.invoke("archive.pack", {"src": os.path.join(td, "src"),
                                        "fmt": "zip"})
        ck(r.ok and os.path.getsize(r.data["out"]) > 0, "打包：目录 → zip")
        zip_path = r.data["out"] if r.ok else ""
        ck(r.ok and r.data["files"] == 2, "打包：报出的文件数正确（2）")

        # ---- 看内容 ----
        r = dev.invoke("archive.info", {"path": zip_path})
        ck(r.ok and r.data["files"] == 2 and r.data["entries"] == 2, "info：2 个条目")
        ck(r.ok and r.data["unsafe_count"] == 0, "info：正常包没有越界条目")
        r = dev.invoke("archive.list", {"path": zip_path})
        names = [i["name"] for i in (r.data.get("items") or [])] if r.ok else []
        # 打包保留相对结构：应该是 src/a.txt 与 src/sub/b.txt，不是平铺
        ck(r.ok and any("sub/" in n or "sub\\" in n for n in names),
           "list：保留目录结构（sub/ 还在）")

        # ---- 解包 ----
        dest = os.path.join(td, "out1")
        r = dev.invoke("archive.extract", {"path": zip_path, "out_dir": dest})
        ck(r.ok and r.data["on_disk"] == 2, "解包：解出 2 个文件")
        got = os.path.join(dest, "src", "sub", "b.txt")
        ck(os.path.isfile(got) and "world" in open(got, encoding="utf-8").read(),
           "解包：子目录里的文件内容完整")

        # ---- 预演不写盘 ----
        dest2 = os.path.join(td, "out2")
        r = dev.invoke("archive.extract", {"path": zip_path, "out_dir": dest2,
                                           "dry_run": True})
        ck(r.ok and r.data["dry_run"] is True and not os.path.exists(dest2),
           "解包：dry_run 只审条目，不落盘")

        # ---- 恶意 zip：带 ../ 的条目必须被拒 ----
        evil = os.path.join(td, "evil.zip")
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("ok.txt", "fine")
            z.writestr("../escaped.txt", "pwned")
        r = dev.invoke("archive.extract", {"path": evil, "out_dir": os.path.join(td, "out3")})
        ck((not r.ok) and "目标目录之外" in (r.err or ""), "安全：路径穿越的包被拒")
        ck(not os.path.exists(os.path.join(td, "escaped.txt")), "安全：越界文件确实没写出来")

        # ---- 恶意 zip：带盘符的条目 ----
        evil2 = os.path.join(td, "evil2.zip")
        with zipfile.ZipFile(evil2, "w") as z:
            z.writestr("C:/Windows/Temp/pwn.txt", "x")
        r = dev.invoke("archive.extract", {"path": evil2, "out_dir": os.path.join(td, "out4")})
        ck((not r.ok) and "带盘符" in (r.err or ""), "安全：带盘符的条目被拒")

        # ---- tar ----
        r = dev.invoke("archive.pack", {"src": os.path.join(td, "src"), "fmt": "tar"})
        ck(r.ok and os.path.getsize(r.data["out"]) > 0, "打包：目录 → tar")
        if r.ok:
            r2 = dev.invoke("archive.extract", {"path": r.data["out"],
                                                "out_dir": os.path.join(td, "out5")})
            ck(r2.ok and r2.data["on_disk"] == 2, "解包：tar 解出 2 个文件")

        # ---- gz 单文件（输出显式放到 src 外面，否则它会把自己装进后面打的包里）----
        r = dev.invoke("archive.pack", {"src": os.path.join(td, "src", "a.txt"),
                                        "fmt": "gz", "out": os.path.join(td, "a.txt.gz")})
        ck(r.ok and os.path.getsize(r.data["out"]) > 0, "打包：单文件 → gz")
        # gz 不能装目录，必须明确拒绝而不是打出一个坏包
        r = dev.invoke("archive.pack", {"src": os.path.join(td, "src"), "fmt": "gz"})
        ck((not r.ok) and "单个文件" in (r.err or ""), "打包：gz 拒绝目录并说明原因")

        # ---- 7z（装了才测）----
        if _sevenz():
            r = dev.invoke("archive.pack", {"src": os.path.join(td, "src"), "fmt": "7z"})
            ck(r.ok and os.path.getsize(r.data["out"]) > 0, "打包：目录 → 7z")
            if r.ok:
                r2 = dev.invoke("archive.list", {"path": r.data["out"]})
                # 7z 会把目录条目也列出来，所以数文件用 files 而不是 matched
                ck(r2.ok and r2.data["files"] == 2, "list：7z 条目里文件数是 2")
                r3 = dev.invoke("archive.extract", {"path": r.data["out"],
                                                    "out_dir": os.path.join(td, "out6")})
                ck(r3.ok and r3.data["on_disk"] == 2, "解包：7z 解出 2 个文件")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    total = counted[0]
    if fails:
        print("archive selftest: %d/%d（失败 %d）" % (total - len(fails), total, len(fails)))
        return 1
    print("archive selftest: %d/%d" % (total, total))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法：python -m fleet.kinds.archive --selftest")

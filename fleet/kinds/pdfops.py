"""PDF 层：看结构 / 合并 / 拆分 / 提页 / 抽文字表格 / 旋转 / 水印 / 加密 / 压缩。

为什么值得单独一层
------------------
`doc.convert` 只会「把 PDF 抽成文本」。而 PDF 真正麻烦的是**改它**：
把三个报告合成一个、把 80 页里第 12-20 页抽出来、给合同加水印、
给扫描件加密码、把一个 30MB 的扫描件压小。这些事每一件都要开一个软件，
而且各有各的坑（免费工具常偷偷加广告页）。

本机已有 `pypdf 6.21` 和 `pdfplumber 0.11`，纯 Python，不需要外部渲染器。

做不到的事，明说
----------------
**PDF 转图片做不了**：那需要一个真正的渲染引擎（pdftoppm / Ghostscript /
PyMuPDF），本机三个都没有。用 pypdf 硬凑只会出白图。
所以 `pdf.engines` 会如实报「渲染器缺失」，而不是假装支持。
**扫描件抽不出文字**：没有文字层就是没有，会明确说「这是扫描件」，
而不是返回空字符串让人以为文件坏了。

四个纪律
--------
1. **默认不覆盖源文件**。合并/拆分/旋转/加密一律另存，要覆盖得显式说。
2. **页号一律 1 起**。用户说「第 5 页」就是 `pages[4]`。这是最容易错的地方，
   所以解析函数单独测。
3. **加密必须能解回来**。写完立刻用密码重读一遍验证——写坏一个加密 PDF
   等于把文件锁死，这比写坏普通文件严重得多。
4. **不假装成功**。抽不到文字、渲染器缺失、页号越界，都返回明确原因。
"""
import logging
import os
import re
import shutil

from ..core import BaseDevice, DeviceResult as R

#: pypdf 解析某些 PDF（尤其是国内排版软件导出的）时会逐字符刷
#: "Unexpected escaped string" / "'utf-16-be' codec can't decode" 警告。
#: 那是它解析字符串的细节，对我们没有信息量，但会把调用方的输出整个淹掉——
#: 实测一本 581 页的书中抽 10 页，警告行数是正文的十倍。
#: 只压 pypdf 自己的 logger，不动全局 logging 配置。
logging.getLogger("pypdf").setLevel(logging.ERROR)

#: 一次最多处理多少页。合并几百页的 PDF 不是「处理文档」而是「跑批」。
MAX_PAGES = 5000

#: 页面尺寸归一成「宽 x 高 点」时的换算（1 pt = 1/72 inch）
PT_PER_MM = 72.0 / 25.4


def _have(mod):
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def parse_pages(spec, total):
    """把 `"1-3,5,7-"` 解析成 0 起的页索引列表。

    规则（写在这里因为每个能力都要用，错一次全错）：
      `"1-3"`  第 1 到 3 页
      `"5"`    只有第 5 页
      `"7-"`   第 7 页到最后一页
      `"-3"`   第 1 页到第 3 页
      `"-1"`   最后一页（负数从末尾数）
      `"all"`  全部
    返回 (索引列表, 错误字符串)。**页号 1 起**。
    """
    if spec in (None, "", "all", "*"):
        return list(range(total)), None
    if isinstance(spec, (list, tuple)):
        out = []
        for one in spec:
            sub, err = parse_pages(one, total)
            if err is not None:
                return None, err
            out.extend(sub)
        seen, uniq = set(), []
        for i in out:
            if i not in seen:
                seen.add(i)
                uniq.append(i)
        return uniq, None

    out = []
    spec_s = str(spec).strip().lower()
    if spec_s == "last":
        return [total - 1], None
    for chunk in str(spec).replace("，", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        # 🔴 第一组**不允许负号**。允许的话 "-3" 会被整段吃成「负 3」，
        # 而页范围的直觉是「-3 = 第 1 到 3 页」（跟 "7-" 对称）。
        # 要「最后一页」写 last，那个没有歧义。
        m = re.match(r"^(\d+)?\s*(-)?\s*(\d+)?$", chunk)
        if not m:
            return None, "看不懂的页范围：%r（写 1-3,5,7- 这种，" \
                         "最后一页写 last）" % chunk
        a, has_dash, b = m.group(1), m.group(2), m.group(3)
        if not has_dash:
            if a is None:
                return None, "页范围 %r 里没有页号" % chunk
            n = int(a)
            if n < 1:
                return None, "页号从 1 开始（写的是 %d）" % n
            if n > total:
                return None, "页号 %d 越界（这个 PDF 共 %d 页）" % (n, total)
            out.append(n - 1)
            continue
        # 区间：左端省略 = 从第 1 页；右端省略 = 到最后一页
        lo = 0 if a in (None, "") else max(1, int(a)) - 1
        hi = total - 1 if b in (None, "") else int(b) - 1
        if lo > hi:
            lo, hi = hi, lo
        if lo < 0 or hi >= total:
            return None, "页范围 %r 越界（这个 PDF 共 %d 页）" % (chunk, total)
        out.extend(range(lo, hi + 1))

    seen, uniq = set(), []
    for i in out:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    if not uniq:
        return None, "页范围 %r 解出来是空的" % spec
    return uniq, None


def _human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or u == "GB":
            return ("%.0f %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0
    return "%.1f GB" % n


def _out_path(src, out, suffix=".pdf"):
    """算输出路径。不给 out 就在源旁边加后缀，绝不覆盖源。"""
    if out:
        p = os.path.abspath(os.path.expanduser(str(out).strip().strip('"')))
        if os.path.isdir(p):
            base = os.path.splitext(os.path.basename(src))[0]
            return os.path.join(p, base + suffix)
        if not os.path.splitext(p)[1]:
            p += suffix
        return p
    stem, ext = os.path.splitext(src)
    return stem + suffix + (ext if ext.lower() == ".pdf" else ".pdf")


def _open(path, password=None):
    """打开 PDF。返回 (reader, 错误)。加密且没给密码/给错密码要能说清楚。"""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        rd = PdfReader(path)
    except FileNotFoundError:
        return None, "文件不存在：%s" % path
    except PdfReadError as e:
        return None, "这不是有效的 PDF（或者文件坏了）：%s" % e
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)

    if getattr(rd, "is_encrypted", False):
        if not password:
            return None, "这个 PDF 加密了，需要密码（传 password）"
        try:
            ok = rd.decrypt(str(password))
        except Exception as e:
            return None, "解密失败：%s: %s" % (type(e).__name__, e)
        if not ok:
            return None, "密码不对"
    return rd, None


class PdfOps(BaseDevice):
    kind = "pdfops"
    via = "local"

    def __init__(self, name="pdf:ops", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "document")

    def probe(self):
        if not _have("pypdf"):
            return False, "缺 pypdf（pip install pypdf）"
        return True, ""

    def caps(self):
        return ("pdf.info", "pdf.merge", "pdf.split", "pdf.extract",
                "pdf.text", "pdf.tables", "pdf.rotate", "pdf.watermark",
                "pdf.encrypt", "pdf.compress", "pdf.meta", "pdf.outline",
                "pdf.engines")

    def invoke(self, cap, args=None):
        args = args or {}
        fn = {
            "pdf.info": self._info,
            "pdf.merge": self._merge,
            "pdf.split": self._split,
            "pdf.extract": self._extract,
            "pdf.text": self._text,
            "pdf.tables": self._tables,
            "pdf.rotate": self._rotate,
            "pdf.watermark": self._watermark,
            "pdf.encrypt": self._encrypt,
            "pdf.compress": self._compress,
            "pdf.meta": self._meta,
            "pdf.outline": self._outline,
            "pdf.engines": self._engines,
        }.get(cap)
        if fn is None:
            return super().invoke(cap, args)
        try:
            return fn(cap, args)
        except Exception as e:
            return R.error(cap, "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)

    # ---------------- 公共 ----------------

    def _src(self, args, cap, key="path"):
        raw = (args.get(key) or "").strip().strip('"').strip("'")
        if not raw:
            return None, R.error(cap, "缺 %s" % key, device=self.name, via=self.via)
        p = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isfile(p):
            return None, R.error(cap, "文件不存在：%s" % p,
                                 device=self.name, via=self.via)
        return p, None

    # ---------------- 引擎清单 ----------------

    def _engines(self, cap, args):
        """本机有什么 PDF 引擎、缺什么。缺渲染器就明说「转图片做不了」。"""
        have = {}
        for mod, ver_attr in (("pypdf", "__version__"), ("pdfplumber", "__version__"),
                              ("PIL", "__version__")):
            try:
                m = __import__(mod)
                have[mod] = getattr(m, ver_attr, "")
            except Exception:
                pass
        bins = {}
        for exe in ("pdftoppm", "pdftocairo", "gs", "mutool", "magick", "qpdf"):
            bins[exe] = bool(shutil.which(exe))
        missing = [k for k, v in bins.items() if not v]
        return R.success(cap, self.name, via=self.via, data={
            "python": have, "binaries": bins,
            "can": {
                "结构读写": bool(have.get("pypdf")),
                "文字抽取": bool(have.get("pypdf")),
                "表格抽取": bool(have.get("pdfplumber")),
                "水印": bool(have.get("pypdf")) and bool(have.get("PIL")),
                "渲染成图片": any(bins[k] for k in ("pdftoppm", "pdftocairo",
                                                   "gs", "mutool")),
            },
            "missing": missing,
            "note": "" if any(bins[k] for k in ("pdftoppm", "pdftocairo", "gs", "mutool"))
                    else ("没有 PDF 渲染器（pdftoppm/gs/mutool 都没有），"
                          "所以「PDF 转图片」这条做不了——那是渲染，不是解析。"
                          "要的话装 poppler 或 PyMuPDF"),
        })

    # ---------------- 看结构 ----------------

    def _info(self, cap, args):
        p, err = self._src(args, cap)
        if err is not None:
            return err

        rd, err = _open(p, args.get("password"))
        if err is not None:
            return R.error(cap, err, device=self.name, via=self.via)

        total = len(rd.pages)
        # 抽样看页面尺寸：一个 PDF 里可能混着 A4 和 A3
        sizes = {}
        for i in range(min(total, 50)):
            try:
                box = rd.pages[i].mediabox
                key = (round(float(box.width)), round(float(box.height)))
                sizes[key] = sizes.get(key, 0) + 1
            except Exception:
                continue
        size_list = [{"w_pt": w, "h_pt": h, "count": c,
                      "w_mm": round(w / PT_PER_MM), "h_mm": round(h / PT_PER_MM),
                      "name": _paper_name(w, h)}
                     for (w, h), c in sorted(sizes.items(), key=lambda kv: -kv[1])]

        # 有没有文字层：抽前几页看看。这是「扫描件 vs 正常 PDF」的判据，
        # 比报「页数」有用得多——扫描件抽不出字，不知道的人会以为工具坏了。
        text_chars = 0
        probed = min(total, 3)
        for i in range(probed):
            try:
                text_chars += len((rd.pages[i].extract_text() or "").strip())
            except Exception:
                continue
        has_text = text_chars > 20
        scanned = (not has_text) and probed > 0

        meta = {}
        try:
            for k, v in (rd.metadata or {}).items():
                if v:
                    meta[str(k).lstrip("/")] = str(v)
        except Exception:
            pass

        outline_n = 0
        try:
            outline_n = len(rd.outline or [])
        except Exception:
            pass

        return R.success(cap, self.name, via=self.via, data={
            "path": p, "bytes": os.path.getsize(p),
            "human": _human(os.path.getsize(p)),
            "pages": total,
            "encrypted": bool(getattr(rd, "is_encrypted", False)),
            "page_sizes": size_list[:5],
            "mixed_sizes": len(size_list) > 1,
            "has_text": has_text,
            "looks_scanned": scanned,
            "text_sample_chars": text_chars,
            "metadata": meta,
            "outline_items": outline_n,
            "note": "看着是扫描件：前 %d 页几乎抽不出文字，"
                    "要文字得先 OCR（本机没 OCR 引擎）" % probed if scanned else "",
        })

    # ---------------- 合并 ----------------

    def _merge(self, cap, args):
        """把多个 PDF 合成一个。"""
        raw = args.get("paths") or args.get("sources") or []
        if isinstance(raw, str):
            raw = [x for x in re.split(r"[;\n,]", raw) if x.strip()]
        paths = []
        for one in raw:
            one = str(one).strip().strip('"').strip("'")
            if not one:
                continue
            q = os.path.abspath(os.path.expanduser(one))
            if not os.path.isfile(q):
                return R.error(cap, "文件不存在：%s" % q,
                               device=self.name, via=self.via)
            paths.append(q)
        if len(paths) < 2:
            return R.error(cap, "至少要两个 PDF（现在 %d 个）："
                                "一个文件直接复制就行，用不着合并" % len(paths),
                           device=self.name, via=self.via)

        from pypdf import PdfWriter
        w = PdfWriter()
        per_file = []
        total_pages = 0
        try:
            for q in paths:
                rd, err = _open(q, args.get("password"))
                if err is not None:
                    return R.error(cap, "%s：%s" % (os.path.basename(q), err),
                                   device=self.name, via=self.via)
                n = len(rd.pages)
                if total_pages + n > MAX_PAGES:
                    return R.error(cap, "合并后超过 %d 页上限" % MAX_PAGES,
                                   device=self.name, via=self.via)
                for page in rd.pages:
                    w.add_page(page)
                total_pages += n
                per_file.append({"path": q, "pages": n})
        except Exception as e:
            return R.error(cap, "合并失败：%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)

        dest = _out_path(paths[0], args.get("out"), "_merged")
        try:
            with open(dest, "wb") as f:
                w.write(f)
        except OSError as e:
            return R.error(cap, "写不出去：%s" % e, device=self.name, via=self.via)

        src_bytes = sum(os.path.getsize(q) for q in paths)
        return R.success(cap, self.name, via=self.via, data={
            "out": dest, "sources": len(paths), "pages": total_pages,
            "per_file": per_file,
            "bytes": os.path.getsize(dest), "src_bytes": src_bytes,
            "note": "合并后源文件的书签/表单会丢——pypdf 只搬页面",
        })

    # ---------------- 拆分 ----------------

    def _split(self, cap, args):
        """拆分。三种模式：每页一个 / 按份数均分 / 按页区间。"""
        p, err = self._src(args, cap)
        if err is not None:
            return err
        rd, err = _open(p, args.get("password"))
        if err is not None:
            return R.error(cap, err, device=self.name, via=self.via)

        total = len(rd.pages)
        mode = (args.get("mode") or "each").strip().lower()
        out_dir = (args.get("out_dir") or "").strip()
        if not out_dir:
            stem = os.path.splitext(p)[0]
            out_dir = stem + "_split"
        else:
            out_dir = os.path.abspath(os.path.expanduser(out_dir))
        name = (args.get("name") or os.path.splitext(os.path.basename(p))[0]).strip()

        from pypdf import PdfWriter
        groups = []
        if mode == "each":
            every = max(1, int(args.get("every") or 1))
            for i in range(0, total, every):
                groups.append((i, min(i + every, total)))
        elif mode == "chunks":
            n = max(1, min(int(args.get("chunks") or 2), total))
            base = total // n
            extra = total % n
            start = 0
            for i in range(n):
                size = base + (1 if i < extra else 0)
                if size:
                    groups.append((start, start + size))
                    start += size
        else:
            spec = args.get("pages")
            idx, perr = parse_pages(spec, total)
            if perr is not None:
                return R.error(cap, perr, device=self.name, via=self.via)
            # 每个页号单独一份
            groups = [(i, i + 1) for i in idx]

        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            return R.error(cap, "建不了目录：%s" % e, device=self.name, via=self.via)

        limit = max(1, min(int(args.get("limit") or 500), MAX_PAGES))
        made, failed = [], []
        for gi, (a, b) in enumerate(groups[:limit]):
            w = PdfWriter()
            for i in range(a, b):
                w.add_page(rd.pages[i])
            if mode == "each" and b - a == 1:
                fn = "%s_p%03d.pdf" % (name, a + 1)
            else:
                fn = "%s_%03d-%03d.pdf" % (name, a + 1, b)
            dest = os.path.join(out_dir, fn)
            try:
                with open(dest, "wb") as f:
                    w.write(f)
                made.append({"out": dest, "pages": [a + 1, b],
                             "bytes": os.path.getsize(dest)})
            except OSError as e:
                failed.append({"pages": [a + 1, b],
                               "err": "%s: %s" % (type(e).__name__, e)})

        return R.success(cap, self.name, via=self.via, data={
            "src": p, "out_dir": out_dir, "mode": mode,
            "total_pages": total, "made": len(made),
            "failed": failed[:10], "failed_count": len(failed),
            "files": made[:30],
            "truncated": len(groups) > limit,
            "note": "" if len(groups) <= limit else
                    "共 %d 份，只做了前 %d 份" % (len(groups), limit),
        })

    # ---------------- 提页 ----------------

    def _extract(self, cap, args):
        """把指定页抽出来组成新 PDF。支持 `1-3,5,7-` 和倒序。"""
        p, err = self._src(args, cap)
        if err is not None:
            return err
        rd, err = _open(p, args.get("password"))
        if err is not None:
            return R.error(cap, err, device=self.name, via=self.via)

        total = len(rd.pages)
        spec = args.get("pages")
        if spec in (None, ""):
            return R.error(cap, "缺 pages（要哪几页，如 1-3,5）",
                           device=self.name, via=self.via)
        idx, perr = parse_pages(spec, total)
        if perr is not None:
            return R.error(cap, perr, device=self.name, via=self.via)
        if len(idx) > MAX_PAGES:
            return R.error(cap, "超过 %d 页上限" % MAX_PAGES,
                           device=self.name, via=self.via)

        from pypdf import PdfWriter
        w = PdfWriter()
        for i in idx:
            w.add_page(rd.pages[i])

        dest = _out_path(p, args.get("out"), "_p%d" % (idx[0] + 1))
        try:
            with open(dest, "wb") as f:
                w.write(f)
        except OSError as e:
            return R.error(cap, "写不出去：%s" % e, device=self.name, via=self.via)

        return R.success(cap, self.name, via=self.via, data={
            "src": p, "out": dest, "pages_spec": spec,
            "picked": [i + 1 for i in idx],
            "count": len(idx), "src_pages": total,
            "bytes": os.path.getsize(dest),
            "note": "按你给的顺序排列；1 起页号",
        })

    # ---------------- 抽文字 ----------------

    def _text(self, cap, args):
        """按页抽文字。抽不到就明说「这是扫描件」，不返回空串让人猜。"""
        p, err = self._src(args, cap)
        if err is not None:
            return err
        rd, err = _open(p, args.get("password"))
        if err is not None:
            return R.error(cap, err, device=self.name, via=self.via)

        total = len(rd.pages)
        spec = args.get("pages") or "all"
        idx, perr = parse_pages(spec, total)
        if perr is not None:
            return R.error(cap, perr, device=self.name, via=self.via)
        limit = max(1, min(len(idx), int(args.get("limit") or 50)))
        idx = idx[:limit]

        pages = []
        empty = 0
        total_chars = 0
        for i in idx:
            try:
                t = rd.pages[i].extract_text() or ""
            except Exception as e:
                t = ""
                pages.append({"page": i + 1, "text": "",
                              "chars": 0, "error": str(e)})
                continue
            t = t.strip()
            total_chars += len(t)
            if not t:
                empty += 1
            pages.append({"page": i + 1, "text": t, "chars": len(t)})

        max_chars = int(args.get("max_chars") or 0)
        joined = "\n\n".join(x["text"] for x in pages if x["text"])
        if max_chars and len(joined) > max_chars:
            joined = joined[:max_chars]

        return R.success(cap, self.name, via=self.via, data={
            "path": p, "pages_total": total,
            "pages_read": len(idx), "pages_empty": empty,
            "chars": total_chars,
            "text": joined,
            "pages": pages if args.get("per_page") else [],
            "truncated": len(idx) < len(parse_pages(spec, total)[0]),
            "looks_scanned": total_chars < 20 and len(idx) > 0,
            "note": ("抽不出文字：这个 PDF 大概率是扫描件（图片），"
                     "本机没有 OCR 引擎，需要先用别的工具识别")
                    if total_chars < 20 and len(idx) > 0 else "",
        })

    # ---------------- 抽表格 ----------------

    def _tables(self, cap, args):
        """用 pdfplumber 抽表格。它比 pypdf 看得懂「表格线」，但慢。"""
        if not _have("pdfplumber"):
            return R.unavailable(cap, "缺 pdfplumber（pip install pdfplumber）",
                                 device=self.name, via=self.via)
        p, err = self._src(args, cap)
        if err is not None:
            return err

        import pdfplumber
        total = 0
        try:
            with pdfplumber.open(p) as pdf:
                total = len(pdf.pages)
                spec = args.get("pages") or "all"
                idx, perr = parse_pages(spec, total)
                if perr is not None:
                    return R.error(cap, perr, device=self.name, via=self.via)
                limit = max(1, min(len(idx), int(args.get("limit") or 20)))
                idx = idx[:limit]
                found = []
                for i in idx:
                    try:
                        ts = pdf.pages[i].extract_tables() or []
                    except Exception as e:
                        found.append({"page": i + 1, "error": str(e), "tables": []})
                        continue
                    clean = []
                    for t in ts:
                        rows = [[("" if c is None else str(c).strip()) for c in row]
                                for row in t]
                        # 全空的行去掉：pdfplumber 常把表格边框当空行
                        rows = [r for r in rows if any(c for c in r)]
                        if rows:
                            clean.append(rows)
                    if clean:
                        found.append({"page": i + 1, "tables": clean,
                                      "count": len(clean)})
        except Exception as e:
            return R.error(cap, "解析失败：%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)

        n_tables = sum(x.get("count", 0) for x in found)
        return R.success(cap, self.name, via=self.via, data={
            "path": p, "pages_total": total, "pages_read": len(idx),
            "tables": found, "table_count": n_tables,
            "note": "" if n_tables else
                    "没找到表格。pdfplumber 靠「线条 + 对齐」识别，"
                    "纯文字排版的表它看不出；那种用 text.regex 自己切更靠谱",
        })

    # ---------------- 旋转 ----------------

    def _rotate(self, cap, args):
        """整本或指定页旋转。角度只收 90 / 180 / 270（PDF 只支持这四个方向）。"""
        p, err = self._src(args, cap)
        if err is not None:
            return err
        rd, err = _open(p, args.get("password"))
        if err is not None:
            return R.error(cap, err, device=self.name, via=self.via)

        deg = int(args.get("degrees") or args.get("angle") or 90)
        if deg % 90 != 0:
            return R.error(cap, "角度只能是 90 的倍数（现在是 %d）："
                                "PDF 页面只有四个方向，转 45 度不是旋转是重绘" % deg,
                           device=self.name, via=self.via)
        deg = deg % 360
        total = len(rd.pages)
        spec = args.get("pages") or "all"
        idx, perr = parse_pages(spec, total)
        if perr is not None:
            return R.error(cap, perr, device=self.name, via=self.via)

        from pypdf import PdfWriter
        w = PdfWriter()
        target = set(idx)
        for i in range(total):
            page = rd.pages[i]
            if i in target:
                try:
                    page.rotate(deg)
                except Exception as e:
                    return R.error(cap, "第 %d 页旋转失败：%s" % (i + 1, e),
                                   device=self.name, via=self.via)
            w.add_page(page)

        dest = _out_path(p, args.get("out"), "_rot%d" % deg)
        try:
            with open(dest, "wb") as f:
                w.write(f)
        except OSError as e:
            return R.error(cap, "写不出去：%s" % e, device=self.name, via=self.via)

        return R.success(cap, self.name, via=self.via, data={
            "src": p, "out": dest, "degrees": deg,
            "pages_rotated": len(idx), "pages_total": total,
            "bytes": os.path.getsize(dest),
        })

    # ---------------- 水印 ----------------

    def _watermark(self, cap, args):
        """文字水印：用 PIL 画一张透明水印页，再叠到每一页上。

        为什么不用 reportlab：那是又一个依赖。PIL 已经在（image.* 全靠它），
        而 PIL 能把一张 RGBA 图直接存成 PDF——这就够了。
        """
        if not _have("PIL"):
            return R.unavailable(cap, "缺 Pillow（水印要它画字）",
                                 device=self.name, via=self.via)
        p, err = self._src(args, cap)
        if err is not None:
            return err
        text = (args.get("text") or "").strip()
        if not text:
            return R.error(cap, "缺 text（水印写什么）",
                           device=self.name, via=self.via)

        rd, err = _open(p, args.get("password"))
        if err is not None:
            return R.error(cap, err, device=self.name, via=self.via)
        total = len(rd.pages)
        spec = args.get("pages") or "all"
        idx, perr = parse_pages(spec, total)
        if perr is not None:
            return R.error(cap, perr, device=self.name, via=self.via)
        target = set(idx)

        import io as _io

        from PIL import Image, ImageDraw, ImageFont
        from pypdf import PdfReader as _R
        from pypdf import PdfWriter

        opacity = max(0.05, min(float(args.get("opacity") or 0.18), 1.0))
        angle = int(args.get("angle") or 35)
        color = _parse_color(args.get("color") or "#808080")
        tile = bool(args.get("tile") or False)

        w = PdfWriter()
        made = 0
        for i in range(total):
            page = rd.pages[i]
            if i not in target:
                w.add_page(page)
                continue
            try:
                box = page.mediabox
                pw, ph = float(box.width), float(box.height)
            except Exception:
                pw, ph = 595.0, 842.0
            wm = self._wm_page(pw, ph, text, opacity, angle, color, tile,
                               Image, ImageDraw, ImageFont, _io)
            try:
                from pypdf import PdfReader as _R2
                wm_page = _R2(wm).pages[0]
                page.merge_page(wm_page)
                made += 1
            except Exception as e:
                return R.error(cap, "第 %d 页叠水印失败：%s" % (i + 1, e),
                               device=self.name, via=self.via)
            w.add_page(page)

        dest = _out_path(p, args.get("out"), "_wm")
        try:
            with open(dest, "wb") as f:
                w.write(f)
        except OSError as e:
            return R.error(cap, "写不出去：%s" % e, device=self.name, via=self.via)

        return R.success(cap, self.name, via=self.via, data={
            "src": p, "out": dest, "text": text,
            "pages_stamped": made, "pages_total": total,
            "opacity": opacity, "angle": angle, "tile": tile,
            "bytes": os.path.getsize(dest),
        })

    @staticmethod
    def _wm_page(pw, ph, text, opacity, angle, color, tile,
                 Image, ImageDraw, ImageFont, _io):
        """造一页水印 PDF，返回 BytesIO。"""
        # 2 倍分辨率再缩：PDF 里的矢量字在 1:1 时会糊
        scale = 2
        W, H = int(pw * scale), int(ph * scale)
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        dr = ImageDraw.Draw(layer)

        fsize = max(14, int(args_size(text, W)))
        font = _load_font(fsize * scale, ImageFont)
        a = int(255 * opacity)
        fill = (color[0], color[1], color[2], a)

        if tile:
            # 平铺：密铺到整页，适合「机密」这种
            step_x = max(120, int(len(text) * fsize * 1.6))
            step_y = max(100, fsize * 5)
            for y in range(-H // 2, H, step_y):
                for x in range(-W // 2, W, step_x):
                    dr.text((x, y), text, font=font, fill=fill)
            layer = layer.rotate(angle, expand=False, resample=Image.BICUBIC)
        else:
            try:
                bb = dr.textbbox((0, 0), text, font=font)
                tw, th = bb[2] - bb[0], bb[3] - bb[1]
            except Exception:
                tw, th = len(text) * fsize, fsize
            canvas = Image.new("RGBA", (tw + 40, th + 40), (0, 0, 0, 0))
            ImageDraw.Draw(canvas).text((20, 20), text, font=font, fill=fill)
            canvas = canvas.rotate(angle, expand=True, resample=Image.BICUBIC)
            layer.alpha_composite(canvas, ((W - canvas.width) // 2,
                                          (H - canvas.height) // 2))
        layer = layer.resize((int(pw), int(ph)), Image.LANCZOS)

        buf = _io.BytesIO()
        # 白底转 RGB 会丢透明——必须存成 RGB 但用白色背景再叠？
        # 不行：merge_page 是叠加，白底会把原内容全盖住。
        # 正确做法：PIL 存 PDF 时不支持 alpha，所以把水印图画成
        # 「白色底 + 深色字」再靠 opacity 变淡，仍然是叠加而不是覆盖——
        # pypdf 的 merge_page 会把页面内容画在上面。
        flat = Image.new("RGB", layer.size, (255, 255, 255))
        # 用 alpha 通道当蒙版，把水印色混到白底上
        r, g, b, al = layer.split()
        wm_rgb = Image.merge("RGB", (r, g, b))
        flat.paste(wm_rgb, (0, 0), al)
        flat.save(buf, format="PDF", resolution=72.0)
        buf.seek(0)
        return buf

    # ---------------- 加密 ----------------

    def _encrypt(self, cap, args):
        """加密 / 解密。写完立刻用密码重读验证——写坏就是锁死文件。"""
        p, err = self._src(args, cap)
        if err is not None:
            return err

        password = args.get("password") or args.get("user_password")
        action = (args.get("action") or "encrypt").strip().lower()

        if action == "decrypt":
            rd, err = _open(p, args.get("password"))
            if err is not None:
                return R.error(cap, err, device=self.name, via=self.via)
            from pypdf import PdfWriter
            w = PdfWriter()
            for page in rd.pages:
                w.add_page(page)
            dest = _out_path(p, args.get("out"), "_decrypted")
            try:
                with open(dest, "wb") as f:
                    w.write(f)
            except OSError as e:
                return R.error(cap, "写不出去：%s" % e, device=self.name, via=self.via)
            # 验证：解出来的必须能不带密码打开
            check, cerr = _open(dest)
            if cerr is not None:
                return R.error(cap, "解密出的文件打不开，已保留源文件：%s" % cerr,
                               device=self.name, via=self.via)
            return R.success(cap, self.name, via=self.via, data={
                "src": p, "out": dest, "action": "decrypt",
                "pages": len(rd.pages), "bytes": os.path.getsize(dest),
                "verified": True,
            })

        if not password:
            return R.error(cap, "加密要给 password", device=self.name, via=self.via)

        rd, err = _open(p, args.get("password") if args.get("already_encrypted") else None)
        if err is not None:
            return R.error(cap, err, device=self.name, via=self.via)

        from pypdf import PdfWriter
        w = PdfWriter()
        for page in rd.pages:
            w.add_page(page)
        owner = args.get("owner_password") or password
        algo = (args.get("algorithm") or "AES-128").strip()
        try:
            w.encrypt(user_password=str(password), owner_password=str(owner),
                      algorithm=algo)
        except Exception as e:
            return R.error(cap, "加密参数不被支持：%s（算法试 AES-128 / AES-256-R5 / RC4-128）"
                           % e, device=self.name, via=self.via)

        dest = _out_path(p, args.get("out"), "_locked")
        try:
            with open(dest, "wb") as f:
                w.write(f)
        except OSError as e:
            return R.error(cap, "写不出去：%s" % e, device=self.name, via=self.via)

        # 🔴 验证：用密码重读一遍，确认真的能解开。
        # 加密写坏了等于把文件锁死，比写坏普通文件严重得多——
        # 所以这一步不是「可选校验」，是必须。
        check, cerr = _open(dest, str(password))
        if cerr is not None:
            return R.error(cap, "新文件用这个密码打不开（%s），"
                                "源文件没动，别用这个产物" % cerr,
                           device=self.name, via=self.via)
        # 同时确认不带密码打不开（否则等于没加密）
        no_pw, _e = _open(dest)
        if no_pw is not None:
            return R.error(cap, "新文件不带密码也能打开，加密没生效；源文件没动",
                           device=self.name, via=self.via)

        return R.success(cap, self.name, via=self.via, data={
            "src": p, "out": dest, "action": "encrypt",
            "algorithm": algo, "pages": len(rd.pages),
            "bytes": os.path.getsize(dest),
            "verified": "用密码能开、不带密码打不开",
            "warning": "密码丢了就打不开，没有后门——这不是威胁，是事实",
        })

    # ---------------- 压缩 ----------------

    def _compress(self, cap, args):
        """压缩。pypdf 能做的是「合并重复对象 + 压内容流」，不能重采样图片。

        所以对「扫描件」这种全是图片的 PDF 效果有限——那种要用渲染器重编码。
        诚实报出压了多少，压不动就说压不动。
        """
        p, err = self._src(args, cap)
        if err is not None:
            return err
        rd, err = _open(p, args.get("password"))
        if err is not None:
            return R.error(cap, err, device=self.name, via=self.via)

        from pypdf import PdfWriter
        w = PdfWriter()
        for page in rd.pages:
            w.add_page(page)
        try:
            # 这两步是 pypdf 的压缩手段：去掉重复对象、压内容流
            if hasattr(w, "compress_identical_objects"):
                w.compress_identical_objects(remove_identicals=True,
                                             remove_orphans=True)
            for page in w.pages:
                try:
                    page.compress_content_streams()
                except Exception:
                    continue
        except Exception as e:
            return R.error(cap, "压缩失败：%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)

        dest = _out_path(p, args.get("out"), "_min")
        try:
            with open(dest, "wb") as f:
                w.write(f)
        except OSError as e:
            return R.error(cap, "写不出去：%s" % e, device=self.name, via=self.via)

        before = os.path.getsize(p)
        after = os.path.getsize(dest)
        ratio = round(after / float(before), 3) if before else 1.0
        return R.success(cap, self.name, via=self.via, data={
            "src": p, "out": dest, "pages": len(rd.pages),
            "src_bytes": before, "bytes": after,
            "src_human": _human(before), "human": _human(after),
            "ratio": ratio, "saved_pct": round((1 - ratio) * 100, 1),
            "note": ("几乎没压下来：这类 PDF 的体积在图片里（扫描件），"
                     "pypdf 只能压结构不能重采样图片。要真压小得装 "
                     "pdftoppm/Ghostscript 重新编码")
                    if ratio > 0.95 else "",
        })

    # ---------------- 元数据 ----------------

    def _meta(self, cap, args):
        """读/写元数据。"""
        p, err = self._src(args, cap)
        if err is not None:
            return err
        rd, err = _open(p, args.get("password"))
        if err is not None:
            return R.error(cap, err, device=self.name, via=self.via)

        cur = {}
        try:
            for k, v in (rd.metadata or {}).items():
                if v is not None:
                    cur[str(k).lstrip("/")] = str(v)
        except Exception:
            pass

        sets = args.get("set")
        if not sets:
            return R.success(cap, self.name, via=self.via, data={
                "path": p, "metadata": cur, "changed": False,
            })
        if not isinstance(sets, dict):
            return R.error(cap, "set 要是对象，如 {'Title':'年报'}",
                           device=self.name, via=self.via)

        from pypdf import PdfWriter
        w = PdfWriter()
        for page in rd.pages:
            w.add_page(page)
        merged = dict(cur)
        merged.update({str(k): str(v) for k, v in sets.items()})
        # 🔴 add_metadata 收不带斜杠的 key **不报错**，要等到 w.write() 序列化时
        # 才抛 "Incorrect first char in NameObject"。也就是错在最后一步，
        # 那时文件已经写了一半 —— 所以这里必须自己把斜杠加回去。
        payload = {(k if k.startswith("/") else "/" + k): v
                   for k, v in merged.items()}
        try:
            w.add_metadata(payload)
        except Exception as e:
            return R.error(cap, "写元数据失败：%s" % e, device=self.name, via=self.via)

        dest = _out_path(p, args.get("out"), "_meta")
        try:
            with open(dest, "wb") as f:
                w.write(f)
        except OSError as e:
            return R.error(cap, "写不出去：%s" % e, device=self.name, via=self.via)

        return R.success(cap, self.name, via=self.via, data={
            "src": p, "out": dest, "before": cur, "after": merged,
            "changed": True, "bytes": os.path.getsize(dest),
        })

    # ---------------- 书签 ----------------

    def _outline(self, cap, args):
        """读目录/书签。"""
        p, err = self._src(args, cap)
        if err is not None:
            return err
        rd, err = _open(p, args.get("password"))
        if err is not None:
            return R.error(cap, err, device=self.name, via=self.via)

        def walk(items, depth=0, out=None):
            out = out if out is not None else []
            for it in items or []:
                if isinstance(it, list):
                    walk(it, depth + 1, out)
                else:
                    try:
                        title = str(getattr(it, "title", "") or "")
                        page = None
                        try:
                            page = rd.get_destination_page_number(it) + 1
                        except Exception:
                            pass
                        out.append({"title": title, "page": page,
                                    "depth": depth})
                    except Exception:
                        continue
            return out

        try:
            items = walk(rd.outline)
        except Exception as e:
            return R.error(cap, "读目录失败：%s" % e, device=self.name, via=self.via)

        total = len(rd.pages)
        if not items:
            # 没书签的 PDF 太常见了，顺手给个「按页首行猜章节」的提示
            return R.success(cap, self.name, via=self.via, data={
                "path": p, "pages": total, "items": [], "count": 0,
                "note": "这个 PDF 没有书签。很多导出工具默认不写目录，"
                        "要目录得在源文档里先加",
            })
        return R.success(cap, self.name, via=self.via, data={
            "path": p, "pages": total, "items": items[:300],
            "count": len(items),
            "truncated": len(items) > 300,
        })


def _paper_name(w, h):
    """按尺寸猜纸张名。人不记得 595×842 是 A4，但知道要 A4。"""
    lo, hi = (w, h) if w <= h else (h, w)
    table = {"A4": (595, 842), "A3": (842, 1191), "A5": (420, 595),
             "Letter": (612, 792), "Legal": (612, 1008),
             "B5": (499, 709), "A6": (298, 420)}
    for name, (tw, th) in table.items():
        if abs(lo - tw) <= 6 and abs(hi - th) <= 6:
            return name
    return "自定义"


def _parse_color(s):
    s = str(s or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return (128, 128, 128)


def args_size(text, W):
    """水印字号：按页宽和文字长度自动定，长文本要小一点才放得下。"""
    return min(W // max(6, len(text) + 4), W // 12)


def _load_font(size, ImageFont):
    """找一个能显示中文的字体。找不到就退回默认（英文能看，中文会方框）。"""
    cands = [
        r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",    # 黑体
        r"C:\Windows\Fonts\simsun.ttc",    # 宋体
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for f in cands:
        if os.path.isfile(f):
            try:
                return ImageFont.truetype(f, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


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

    # ---- 页范围解析（纯函数，先测，它是所有能力的地基）----
    ck(parse_pages("all", 10)[0] == list(range(10)), "pages：all 给全 10 页")
    ck(parse_pages(None, 10)[0] == list(range(10)), "pages：不给也给全")
    ck(parse_pages("3", 10)[0] == [2], "pages：单页 3 → 索引 2（1 起）")
    ck(parse_pages("1-3", 10)[0] == [0, 1, 2], "pages：1-3")
    ck(parse_pages("3-1", 10)[0] == [0, 1, 2], "pages：3-1 倒着写也认")
    ck(parse_pages("7-", 10)[0] == list(range(6, 10)), "pages：7- 到最后")
    ck(parse_pages("-3", 10)[0] == [0, 1, 2], "pages：-3 = 第 1 到 3 页（与 7- 对称）")
    ck(parse_pages("last", 10)[0] == [9], "pages：last 是最后一页")
    ck(parse_pages("-1", 10)[0] == [0], "pages：-1 = 第 1 页（不是倒数，避免歧义）")
    ck(parse_pages("1,3,5", 10)[0] == [0, 2, 4], "pages：逗号列举")
    ck(parse_pages("1-3,5", 10)[0] == [0, 1, 2, 4], "pages：区间 + 单页混写")
    ck(parse_pages("5,5,5", 10)[0] == [4], "pages：重复页号去重")
    e = parse_pages("99", 10)[1]
    ck(e is not None and "越界" in e, "pages：越界明确报错")
    e = parse_pages("1-99", 10)[1]
    ck(e is not None and "越界" in e, "pages：区间越界明确报错")
    e = parse_pages("瞎写", 10)[1]
    ck(e is not None, "pages：乱写明确报错")

    if not _have("pypdf"):
        print("\npdfops selftest: %d/%d（pypdf 缺失，只测了纯函数）"
              % (state["n"] - len(fails), state["n"]))
        return 1 if fails else 0

    td = tempfile.mkdtemp(prefix="pdfops_test_")

    def make_pdf(path, pages=3, text_prefix="Page"):
        """造一个真 PDF。用 PIL 画文字再存，不引 reportlab。"""
        from PIL import Image, ImageDraw, ImageFont
        ims = []
        for i in range(pages):
            im = Image.new("RGB", (595, 842), (255, 255, 255))
            d = ImageDraw.Draw(im)
            f = _load_font(36, ImageFont)
            if f:
                d.text((80, 120), "%s %d" % (text_prefix, i + 1), font=f,
                       fill=(0, 0, 0))
            ims.append(im)
        ims[0].save(path, format="PDF", save_all=True, append_images=ims[1:],
                    resolution=72.0)
        for im in ims:
            im.close()
        return path

    try:
        a = make_pdf(os.path.join(td, "a.pdf"), 3, "Alpha")
        b = make_pdf(os.path.join(td, "b.pdf"), 2, "Beta")
        ck(os.path.getsize(a) > 0 and os.path.getsize(b) > 0, "素材：造出 3 页和 2 页的 PDF")

        # ---- pdf.info ----
        r = dev = PdfOps()
        r = dev.invoke("pdf.info", {"path": a})
        ck(r.ok and r.data["pages"] == 3, "info：页数 3")
        ck(r.ok and r.data["encrypted"] is False, "info：未加密")
        ck(r.ok and len(r.data["page_sizes"]) >= 1, "info：报出页面尺寸")
        ck(r.ok and r.data["page_sizes"][0]["name"] == "A4",
           "info：认出 A4（%s）" % (r.data["page_sizes"][0]["name"] if r.ok else "?"))

        # ---- pdf.merge ----
        r = dev.invoke("pdf.merge", {"paths": [a, b]})
        ck(r.ok and r.data["pages"] == 5, "merge：3 + 2 = 5 页")
        ck(r.ok and r.data["out"] != a, "merge：输出不覆盖源")
        if r.ok:
            merged = r.data["out"]
            r2 = dev.invoke("pdf.info", {"path": merged})
            ck(r2.ok and r2.data["pages"] == 5, "merge：产物真的是 5 页")

        # ---- merge 参数不足 ----
        r = dev.invoke("pdf.merge", {"paths": [a]})
        ck((not r.ok) and "至少要两个" in (r.err or ""), "merge：只给一个时明确拒绝")

        # ---- pdf.split：每页一个 ----
        r = dev.invoke("pdf.split", {"path": a, "mode": "each",
                                     "name": "part"})
        ck(r.ok and r.data["made"] == 3, "split：3 页拆成 3 个文件")
        if r.ok:
            files = sorted(os.listdir(r.data["out_dir"]))
            ck(files == ["part_p001.pdf", "part_p002.pdf", "part_p003.pdf"],
               "split：文件名带页码且补零（%s）" % files)

        # ---- split：按份数 ----
        r = dev.invoke("pdf.split", {"path": a, "mode": "chunks", "chunks": 2,
                                     "name": "chunk"})
        ck(r.ok and r.data["made"] == 2, "split：按份数拆成 2 份")

        # ---- pdf.extract ----
        r = dev.invoke("pdf.extract", {"path": a, "pages": "2-3"})
        ck(r.ok and r.data["count"] == 2, "extract：抽出 2 页")
        ck(r.ok and r.data["picked"] == [2, 3], "extract：页号对（1 起）")
        if r.ok:
            r2 = dev.invoke("pdf.info", {"path": r.data["out"]})
            ck(r2.ok and r2.data["pages"] == 2, "extract：产物真是 2 页")

        # ---- extract：越界 ----
        r = dev.invoke("pdf.extract", {"path": a, "pages": "9"})
        ck((not r.ok) and "越界" in (r.err or ""), "extract：页号越界明确报错")

        # ---- pdf.rotate ----
        r = dev.invoke("pdf.rotate", {"path": a, "degrees": 90})
        ck(r.ok and r.data["pages_rotated"] == 3, "rotate：3 页都转了")
        r = dev.invoke("pdf.rotate", {"path": a, "degrees": 45})
        ck((not r.ok) and "90 的倍数" in (r.err or ""),
           "rotate：45 度明确拒绝并说明原因")
        r = dev.invoke("pdf.rotate", {"path": a, "degrees": 180, "pages": "1"})
        ck(r.ok and r.data["pages_rotated"] == 1, "rotate：只转指定页")

        # ---- pdf.encrypt ----
        r = dev.invoke("pdf.encrypt", {"path": a, "password": "s3cret"})
        ck(r.ok and r.data["action"] == "encrypt", "encrypt：加密成功")
        ck(r.ok and r.data["verified"], "encrypt：写完自己验证过")
        if r.ok:
            locked = r.data["out"]
            # 不带密码应该打不开
            r2 = dev.invoke("pdf.info", {"path": locked})
            ck((not r2.ok) and "密码" in (r2.err or ""),
               "encrypt：不带密码确实打不开")
            # 带密码应该能开
            r3 = dev.invoke("pdf.info", {"path": locked, "password": "s3cret"})
            ck(r3.ok and r3.data["pages"] == 3, "encrypt：带密码能正常打开")
            # 错密码要拒绝
            r4 = dev.invoke("pdf.info", {"path": locked, "password": "wrong"})
            ck((not r4.ok) and "密码" in (r4.err or ""), "encrypt：错密码明确拒绝")

            # ---- decrypt ----
            r5 = dev.invoke("pdf.encrypt", {"path": locked, "action": "decrypt",
                                            "password": "s3cret"})
            ck(r5.ok and r5.data["verified"], "decrypt：解密并验证过")
            if r5.ok:
                r6 = dev.invoke("pdf.info", {"path": r5.data["out"]})
                ck(r6.ok and r6.data["encrypted"] is False, "decrypt：产物不再加密")

        # ---- pdf.compress ----
        r = dev.invoke("pdf.compress", {"path": a})
        ck(r.ok and r.data["pages"] == 3, "compress：3 页都保留")
        ck(r.ok and r.data["bytes"] > 0, "compress：产物非空")

        # ---- pdf.watermark ----
        r = dev.invoke("pdf.watermark", {"path": a, "text": "机密",
                                         "opacity": 0.2})
        ck(r.ok and r.data["pages_stamped"] == 3, "watermark：3 页都盖了章")
        if r.ok:
            r2 = dev.invoke("pdf.info", {"path": r.data["out"]})
            ck(r2.ok and r2.data["pages"] == 3, "watermark：页数没变（是叠加不是替换）")

        # ---- pdf.meta ----
        r = dev.invoke("pdf.meta", {"path": a,
                                    "set": {"Title": "测试文档",
                                            "Author": "领航员"}})
        ck(r.ok and r.data["changed"] is True, "meta：写入元数据")
        if r.ok:
            r2 = dev.invoke("pdf.meta", {"path": r.data["out"]})
            ck(r2.ok and r2.data["metadata"].get("Title") == "测试文档",
               "meta：读回来是刚写的标题")

        # ---- pdf.text：PIL 造的 PDF 没有文字层，应该明确说 ----
        r = dev.invoke("pdf.text", {"path": a})
        ck(r.ok, "text：能跑（内容是图片，抽不出文字属正常）")
        if r.ok:
            ck(r.data["looks_scanned"] is True,
               "text：抽不出文字时明确标为「像扫描件」而不是返回空串不说")
            ck("扫描件" in (r.data["note"] or ""), "text：note 说清了原因")

        # ---- pdf.outline ----
        r = dev.invoke("pdf.outline", {"path": a})
        ck(r.ok and r.data["count"] == 0, "outline：PIL 造的 PDF 没书签")
        ck(r.ok and "没有书签" in (r.data["note"] or ""),
           "outline：没书签时说明原因")

        # ---- pdf.engines ----
        r = dev.invoke("pdf.engines", {})
        ck(r.ok and "pypdf" in r.data["python"], "engines：报出 pypdf")
        ck(r.ok and r.data["can"]["结构读写"] is True, "engines：结构读写可用")

        # ---- 错误路径 ----
        r = dev.invoke("pdf.info", {"path": os.path.join(td, "没有.pdf")})
        ck((not r.ok) and "不存在" in (r.err or ""), "错误：文件不存在")
        r = dev.invoke("pdf.info", {})
        ck((not r.ok) and "缺 path" in (r.err or ""), "错误：缺参数")
        bad = os.path.join(td, "bad.pdf")
        with open(bad, "wb") as f:
            f.write(b"this is not a pdf at all")
        r = dev.invoke("pdf.info", {"path": bad})
        ck((not r.ok) and ("不是有效的 PDF" in (r.err or "") or "坏" in (r.err or "")),
           "错误：不是 PDF 时明确报错")

    finally:
        import shutil as _sh
        _sh.rmtree(td, ignore_errors=True)

    total = state["n"]
    got = total - len(fails)
    print("\npdfops selftest: %d/%d%s"
          % (got, total, "" if not fails else "（失败 %d）" % len(fails)))
    for f in fails:
        print("   失败:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法: python -m fleet.kinds.pdfops --selftest")
    sys.exit(0)

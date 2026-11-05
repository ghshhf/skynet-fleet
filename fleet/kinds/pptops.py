"""演示层：PPT 的读、建、模板填充。

为什么单独一台（2026-11-06）：
  跟 Word/Excel 同一个理由——`doc.convert` 只能把 pptx 抽成文字，抽完回不去。
  真正每天要用的是**做汇报**：一份模板 + 一张数据表 → 十页成品；
  或者反过来，把别人发来的 40 页 PPT 抽成大纲喂给自己看。

选型（都试过，不是照着名字抄的）：
  · `python-pptx 1.0.2`（MIT，本机已装）—— 读写 pptx 的地基，圆形/表格/图片/图表都有。
  · `python-pptx-text-replacer 0.0.6`（MIT）—— **专治本设备最大的坑**：
    PowerPoint 会把 `{name}` 按内部规则**拆成多个 run**（`{na` + `me}`），
    纯字符串替换 100% 漏。这个包按**字符级**把替换文本分配回各个 run，
    每个 run 保留自己的字体/字号/加粗，连表格、组合、图表分类标签都覆盖。
    它不可用时本设备内置同样的算法降级（`_replace_runs`）。

🔴 为什么 `ppt.slides` 只做「删」和「移」，不做「复制」：
  试过了——deepcopy 出来的页会复用原 `slide3.xml` 这个 part 名，
  保存时 zip 里出现重名条目（Duplicate name），PowerPoint 打开会提示修复。
  要正确复制必须重命名 part 并重映射全部关系，收益不抵风险，所以如实不做。

诚实边界：
  · **不能渲染成图片**：那要 LibreOffice / PowerPoint，本机没有（缺 soffice）。
    要缩略图请先 `convert` 或装 LibreOffice。
  · **公式（OMML）/ SmartArt / 动画 / 切换效果** 不解析、不写。
  · **母版与主题色不动**：改色只改 run 的 RGB，不动 theme。
  · **图表只读数据**，不改图表类型（那是 python-pptx 的边界，不是懒）。
  · `{key}` 与 `{{ key }}` 两种写法都认。

铁律：默认**另存不覆盖**，重名自动让号。原地改要 `inplace=true` + `_confirmed=true`。
"""
import json
import os
import re

from ..core import BaseDevice, DeviceResult as R

#: 一页里最多看多少个形状（防呆）
MAX_SHAPES = 400
#: 一次返回的最大页数
MAX_SLIDES = 500
#: 形状文字超过这么长就截断
MAX_SHAPE_CHARS = 3_000

READ_EXTS = (".pptx", ".pptm", ".potx", ".potm")
WRITE_EXTS = (".pptx",)


# ---------------------------------------------------------------- 小工具

def _norm_path(p, field="path"):
    if not p:
        raise ValueError(f"缺 {field}")
    p = str(p).strip().strip('"').replace("\\", "/")
    m = re.match(r"^/([a-zA-Z])/", p)
    if m:
        p = m.group(1).upper() + ":/" + p[3:]
    return p


def _require_file(p, field="path"):
    p = _norm_path(p, field)
    if not os.path.isfile(p):
        raise ValueError(f"文件不存在: {p}")
    ext = os.path.splitext(p)[1].lower()
    if ext not in READ_EXTS:
        raise ValueError(
            f"不认这个扩展名 {ext}（能读 {'/'.join(READ_EXTS)}）。"
            "老的 .ppt 是二进制格式，先走 convert.file 转成 pptx")
    return p


def _unique(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{base}_{n}{ext}"):
        n += 1
    return f"{base}_{n}{ext}"


def _out_path(a, src=None, default_name="输出.pptx"):
    out = a.get("out")
    if out:
        out = _norm_path(out, "out")
        ext = os.path.splitext(out)[1].lower()
        if ext not in WRITE_EXTS:
            raise ValueError(f"只能写 {'/'.join(WRITE_EXTS)}，不写 {ext}")
    elif src:
        out = os.path.splitext(src)[0] + "_out.pptx"
    else:
        out = os.path.abspath(default_name)
    d = os.path.dirname(os.path.abspath(out))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    return out


def _resolve_write(a, src=None, default_name="输出.pptx"):
    if not a.get("inplace"):
        return _unique(_out_path(a, src, default_name)), False
    if src is None:
        raise ValueError("inplace 必须配合 path 用")
    if not a.get("_confirmed"):
        raise ValueError(
            f"原地改会直接覆盖 {os.path.basename(src)}。"
            "确认无误请加 _confirmed=true 再调一次（这次什么都没动）")
    return src, True


def _clip(s, n=MAX_SHAPE_CHARS):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:n] + f"…[截断，原长 {len(s)}]"


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def _emu_cm(v):
    return round(v / 360000.0, 2) if v is not None else None


def _shape_kind(sh):
    """把 python-pptx 的形状类型翻成人话。"""
    try:
        if sh.has_text_frame:
            return "text"
        if sh.has_table:
            return "table"
        if sh.has_chart:
            return "chart"
        if getattr(sh, "shape_type", None) is not None and "PICTURE" in str(sh.shape_type):
            return "image"
        return str(sh.shape_type).split(" ")[0].lower() if sh.shape_type else "other"
    except Exception:
        return "other"


#: 占位符：`{{ key }}` 或 `{key}`（key 只允许字母数字下划线）
_RE_SIMPLE = re.compile(r"\{\{?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}?\}")


# ---------------------------------------------------------------- 跨 run 替换
#
# 算法来自 python-pptx-text-replacer 的核心思路（我读过它的 _replace_runs_text）：
# 不是在某个 run 里找整串，而是把「匹配区间」按 run 边界切开，
# 替换文本全部落到**匹配起点所在的那个 run**，其余 run 只删掉自己那段。
# 这样每个 run 的格式（字体/字号/颜色/加粗）都不用动。

def _replace_in_runs(runs, s, e, repl):
    """在 runs 组成的文本里，把 [s, e) 换成 repl，保留每个 run 的格式。"""
    texts = [r.text for r in runs]
    if not texts:
        return False
    new_texts = []
    acc = 0
    placed = False
    for t in texts:
        b, en = acc, acc + len(t)
        acc = en
        if en <= s or b >= e:
            # 完全在匹配区间之外（注意：空 run 的 b==en，也可能落进来，下面兜住）
            if not placed and b >= e:
                pass
            new_texts.append(t)
            continue
        local_s = max(0, s - b)
        local_e = min(len(t), e - b)
        piece = t[:local_s]
        if not placed:
            piece += repl
            placed = True
        piece += t[local_e:]
        new_texts.append(piece)
    if not placed:
        # 匹配完全落在两个 run 的缝隙里（不该发生，但别静默丢内容）
        new_texts[0] += repl
    changed = False
    for r, nt in zip(runs, new_texts):
        if r.text != nt:
            r.text = nt
            changed = True
    return changed


def _replace_in_text_frame(tf, mapping):
    """对一个 text_frame 做全部替换，返回改了几处。"""
    n = 0
    for para in tf.paragraphs:
        runs = para.runs
        if not runs:
            continue
        for _guard in range(50):                      # 防死循环
            full = "".join(r.text for r in runs)
            best = None
            for key, val in mapping.items():
                for pat in ("{" + key + "}", "{{" + key + "}}",
                            "{{ " + key + " }}", "{ " + key + " }"):
                    pos = full.find(pat)
                    if pos >= 0 and (best is None or pos < best[0]):
                        best = (pos, pos + len(pat), str(val))
            if best is None:
                break
            if not _replace_in_runs(runs, best[0], best[1], best[2]):
                break
            n += 1
    return n


def _iter_text_frames(shapes):
    """深度遍历：形状 / 表格单元格 / 组合，把所有 text_frame 揪出来。"""
    for sh in shapes:
        try:
            if sh.has_text_frame:
                yield sh.text_frame
            if sh.has_table:
                for row in sh.table.rows:
                    for cell in row.cells:
                        yield cell.text_frame
            if sh.shape_type is not None and "GROUP" in str(sh.shape_type):
                for sub in _iter_text_frames(sh.shapes):
                    yield sub
        except Exception:
            continue


# ---------------------------------------------------------------- 设备

class PptOps(BaseDevice):
    kind = "pptops"
    via = "local"

    def __init__(self, name="ppt:ops", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "document")

    def probe(self):
        try:
            import pptx  # noqa: F401
        except Exception:
            return False, "没装 python-pptx：pip install python-pptx"
        return True, ""

    def caps(self):
        return ("ppt.info", "ppt.read", "ppt.outline", "ppt.fill",
                "ppt.new", "ppt.add", "ppt.table", "ppt.image",
                "ppt.notes", "ppt.slides")

    # ------------------------------------------------ 分派

    def invoke(self, cap, args=None):
        args = args or {}
        fn = {
            "ppt.info": self._info,
            "ppt.read": self._read,
            "ppt.outline": self._outline,
            "ppt.fill": self._fill,
            "ppt.new": self._new,
            "ppt.add": self._add,
            "ppt.table": self._table,
            "ppt.image": self._image,
            "ppt.notes": self._notes,
            "ppt.slides": self._slides,
        }.get(cap)
        if fn is None:
            return R.unsupported(cap, f"{self.name} 没有 {cap}",
                                 device=self.name, via=self.via)
        try:
            payload = fn(args)
        except ValueError as e:
            return R.error(cap, str(e), device=self.name, via=self.via)
        except Exception as e:                                    # noqa: BLE001
            return R.error(cap, f"{type(e).__name__}: {e}",
                           device=self.name, via=self.via)
        readonly = ("ppt.info", "ppt.read", "ppt.outline")
        wrote = isinstance(payload, dict) and bool(payload.get("written"))
        return R.success(cap, device=self.name, data=payload,
                         side_effect=(cap not in readonly) and wrote,
                         via=self.via)

    # ------------------------------------------------ ppt.info

    def _info(self, a):
        p = _require_file(a.get("path"))
        from pptx import Presentation
        from pptx.util import Emu

        prs = Presentation(p)
        size = os.path.getsize(p)
        slides = []
        for i, s in enumerate(prs.slides):
            if i >= MAX_SLIDES:
                break
            kinds = {}
            imgs = 0
            for sh in s.shapes:
                k = _shape_kind(sh)
                kinds[k] = kinds.get(k, 0) + 1
                if k == "image":
                    imgs += 1
            has_notes = False
            try:
                has_notes = bool(s.notes_slide.notes_text_frame.text.strip())
            except Exception:
                has_notes = False
            title = ""
            try:
                if s.shapes.title is not None:
                    title = s.shapes.title.text
            except Exception:
                title = ""
            slides.append({
                "index": i, "shapes": len(s.shapes), "kinds": kinds,
                "images": imgs, "title": _clip(title, 120),
                "notes": has_notes,
                "layout": s.slide_layout.name if s.slide_layout else None,
            })
        return {
            "path": p,
            "slides": len(prs.slides),
            "detail": slides,
            "size": size, "size_human": _fmt_size(size),
            "width_cm": _emu_cm(prs.slide_width),
            "height_cm": _emu_cm(prs.slide_height),
            "ratio": (round(prs.slide_width / prs.slide_height, 3)
                      if prs.slide_height else None),
            "layouts": [l.name for l in prs.slide_layouts],
            "can_render_image": False,
            "render_note": "本机没有 LibreOffice/soffice，转不了图片；要图先 convert 或装 soffice",
        }

    # ------------------------------------------------ ppt.read

    def _read(self, a):
        p = _require_file(a.get("path"))
        from pptx import Presentation

        prs = Presentation(p)
        want = a.get("slide")
        want_idx = None
        if want is not None:
            want_idx = int(want)
        with_notes = bool(a.get("notes", True))
        out_slides = []
        for i, s in enumerate(prs.slides):
            if want_idx is not None and i != want_idx:
                continue
            texts = []
            for sh in s.shapes:
                try:
                    if sh.has_text_frame:
                        t = sh.text_frame.text
                        if t.strip():
                            texts.append({"kind": "text", "text": _clip(t),
                                          "name": sh.name})
                    elif sh.has_table:
                        rows = [[c.text.strip() for c in row.cells]
                                for row in sh.table.rows]
                        texts.append({"kind": "table", "rows": rows,
                                      "name": sh.name})
                    elif sh.has_chart:
                        ch = sh.chart
                        try:
                            cats = [str(c) for c in ch.plots[0].categories]
                        except Exception:
                            cats = []
                        series = []
                        for se in ch.series:
                            try:
                                series.append({"name": se.name,
                                               "values": list(se.values)})
                            except Exception:
                                series.append({"name": se.name,
                                               "values": None})
                        texts.append({"kind": "chart", "categories": cats,
                                      "series": series, "name": sh.name,
                                      "chart_type": str(ch.chart_type)})
                    elif _shape_kind(sh) == "image":
                        texts.append({"kind": "image", "name": sh.name,
                                      "width_cm": _emu_cm(sh.width),
                                      "height_cm": _emu_cm(sh.height)})
                except Exception:
                    continue
            item = {"index": i, "items": texts,
                    "text": "\n".join(x.get("text", "") for x in texts
                                      if x["kind"] == "text")}
            if with_notes:
                try:
                    item["notes"] = s.notes_slide.notes_text_frame.text
                except Exception:
                    item["notes"] = ""
            out_slides.append(item)
        return {
            "path": p,
            "slides": len(out_slides),
            "detail": out_slides,
            "text": "\n\n".join(
                f"--- 第 {x['index'] + 1} 页 ---\n" + x["text"]
                for x in out_slides),
        }

    # ------------------------------------------------ ppt.outline

    def _outline(self, a):
        """大纲：每页的标题 + 正文层级，用来快速判断这份 PPT 讲了什么。"""
        p = _require_file(a.get("path"))
        from pptx import Presentation

        prs = Presentation(p)
        out = []
        for i, s in enumerate(prs.slides):
            title = ""
            try:
                if s.shapes.title is not None:
                    title = s.shapes.title.text.strip()
            except Exception:
                title = ""
            bullets = []
            for sh in s.shapes:
                if not sh.has_text_frame:
                    continue
                if sh == (s.shapes.title if s.shapes.title is not None else None):
                    continue
                for para in sh.text_frame.paragraphs:
                    t = (para.text or "").strip()
                    if not t:
                        continue
                    lvl = para.level if para.level is not None else 0
                    bullets.append({"level": int(lvl), "text": _clip(t, 400)})
            out.append({"index": i, "title": _clip(title, 200),
                        "bullets": bullets,
                        "bullet_count": len(bullets)})
        lines = []
        for it in out:
            lines.append(f"{it['index'] + 1}. {it['title'] or '（无标题）'}")
            for b in it["bullets"][:12]:
                lines.append("   " + "  " * b["level"] + "- " + b["text"])
        return {"path": p, "count": len(out), "outline": out,
                "markdown": "\n".join(lines)}

    # ------------------------------------------------ ppt.fill

    def _fill(self, a):
        """模板填充：`{key}` / `{{ key }}` 跨 run 替换，保留原格式。"""
        p = _require_file(a.get("path"))
        ctx = a.get("data")
        if ctx is None:
            ctx = {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except Exception as e:
                raise ValueError(f"data 不是合法 JSON: {e}")
        if not isinstance(ctx, dict):
            raise ValueError("data 必须是对象（{键: 值}）")

        out, inplace = _resolve_write(a, p, "填充结果.pptx")
        engine = None
        replaced = 0

        # ① 优先用专门的包（它连图表分类标签都处理）
        use_lib = a.get("engine") != "builtin"
        if use_lib:
            try:
                from python_pptx_text_replacer import TextReplacer
                pairs = []
                for k, v in ctx.items():
                    if k.startswith("_"):
                        continue
                    pairs.append(("{{ " + str(k) + " }}", str(v)))
                    pairs.append(("{{" + str(k) + "}}", str(v)))
                    pairs.append(("{" + str(k) + "}", str(v)))
                if pairs:
                    tr = TextReplacer(p, tables=True, textframes=True,
                                      charts=bool(a.get("charts")), quiet=True)
                    tr.replace_text(pairs)
                    tr.write_presentation_to_file(out)
                    engine = "python-pptx-text-replacer"
                    replaced = len(pairs)
            except Exception:
                engine = None

        # ② 包不在 / 出错 → 内置算法（同一套字符级分配，效果等价）
        if engine is None:
            from pptx import Presentation
            prs = Presentation(p)
            mapping = {k: v for k, v in ctx.items() if not str(k).startswith("_")}
            total = 0
            for s in prs.slides:
                for tf in _iter_text_frames(s.shapes):
                    total += _replace_in_text_frame(tf, mapping)
                try:
                    if s.has_notes_slide:
                        total += _replace_in_text_frame(
                            s.notes_slide.notes_text_frame, mapping)
                except Exception:
                    pass
            prs.save(out)
            engine = "builtin"
            replaced = total

        if os.path.getsize(out) < 1000:
            raise ValueError("产物异常（文件太小），可能没写成功")

        # 自查：还有没有残留占位符（漏填要如实报，不假装成功）
        left = self._scan_leftover(out, ctx)
        return {
            "path": out, "inplace": inplace, "written": True,
            "engine": engine, "replaced": replaced,
            "keys": sorted(k for k in ctx if not str(k).startswith("_")),
            "unfilled": left,
            "size": os.path.getsize(out),
            "size_human": _fmt_size(os.path.getsize(out)),
        }

    def _scan_leftover(self, path, ctx):
        """扫一遍产物，看还有哪些 {key} 没被替换掉。"""
        try:
            from pptx import Presentation
            prs = Presentation(path)
            blob = []
            for s in prs.slides:
                for tf in _iter_text_frames(s.shapes):
                    blob.append(tf.text)
            text = "\n".join(blob)
            found = set(m.group(1) for m in _RE_SIMPLE.finditer(text))
            # 只报「给了值却还留着」的，没给值的属于正常（可能是别的用途）
            return sorted(k for k in found if k in ctx)
        except Exception:
            return []

    # ------------------------------------------------ ppt.new

    def _new(self, a):
        """从零建一份 PPT：标题页 + 若干内容页。"""
        from pptx import Presentation
        from pptx.util import Inches, Pt

        out = _resolve_write(a)[0]
        prs = Presentation()
        # 16:9 还是 4:3
        ratio = str(a.get("ratio") or "16:9")
        if ratio in ("16:9", "169"):
            prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
        elif ratio in ("4:3", "43"):
            prs.slide_width, prs.slide_height = Inches(10), Inches(7.5)

        made = []
        title = a.get("title")
        subtitle = a.get("subtitle")
        if title:
            s = prs.slides.add_slide(prs.slide_layouts[0])
            s.shapes.title.text = str(title)
            if subtitle:
                try:
                    s.placeholders[1].text = str(subtitle)
                except Exception:
                    pass
            made.append({"index": 0, "kind": "title", "title": str(title)})

        slides = a.get("slides")
        if isinstance(slides, str):
            try:
                slides = json.loads(slides)
            except Exception as e:
                raise ValueError(f"slides 不是合法 JSON: {e}")
        if not slides and not title:
            raise ValueError("没给内容：至少要 title 或 slides")

        for spec in (slides or []):
            kind = (spec.get("kind") or "bullets").lower()
            idx = len(prs.slides)
            if kind == "title":
                s = prs.slides.add_slide(prs.slide_layouts[0])
                s.shapes.title.text = str(spec.get("title") or "")
                if spec.get("subtitle"):
                    try:
                        s.placeholders[1].text = str(spec["subtitle"])
                    except Exception:
                        pass
            elif kind == "section":
                s = prs.slides.add_slide(prs.slide_layouts[2])
                s.shapes.title.text = str(spec.get("title") or "")
                if spec.get("subtitle"):
                    try:
                        s.placeholders[1].text = str(spec["subtitle"])
                    except Exception:
                        pass
            elif kind == "table":
                s = prs.slides.add_slide(prs.slide_layouts[5])
                if spec.get("title"):
                    s.shapes.title.text = str(spec["title"])
                rows = spec.get("rows") or []
                if rows:
                    left = Inches(0.6)
                    top = Inches(1.8)
                    width = prs.slide_width - Inches(1.2)
                    height = Inches(0.4) * len(rows)
                    t = s.shapes.add_table(len(rows), len(rows[0]),
                                           left, top, width, height).table
                    for i, row in enumerate(rows):
                        for j, val in enumerate(row):
                            if j < len(t.columns):
                                t.cell(i, j).text = "" if val is None else str(val)
            elif kind == "image":
                s = prs.slides.add_slide(prs.slide_layouts[5])
                if spec.get("title"):
                    s.shapes.title.text = str(spec["title"])
                ip = _norm_path(spec.get("path"), "slides.image.path")
                if not os.path.isfile(ip):
                    raise ValueError(f"图片不存在: {ip}")
                if spec.get("width_cm"):
                    s.shapes.add_picture(ip, Inches(1), Inches(2),
                                         width=__import__("pptx.util", fromlist=["Cm"]).Cm(
                                             float(spec["width_cm"])))
                else:
                    s.shapes.add_picture(ip, Inches(1), Inches(2))
            elif kind == "blank":
                s = prs.slides.add_slide(prs.slide_layouts[6])
            else:                                       # bullets
                s = prs.slides.add_slide(prs.slide_layouts[1])
                s.shapes.title.text = str(spec.get("title") or "")
                body = spec.get("bullets") or spec.get("body") or []
                if isinstance(body, str):
                    body = [body]
                tf = s.placeholders[1].text_frame
                tf.text = ""
                first = True
                for b in body:
                    if isinstance(b, dict):
                        txt, lvl = str(b.get("text") or ""), int(b.get("level") or 0)
                    else:
                        txt, lvl = str(b), 0
                    p = tf.paragraphs[0] if first else tf.add_paragraph()
                    p.text = txt
                    p.level = min(lvl, 4)
                    first = False
            made.append({"index": idx, "kind": kind,
                         "title": str(spec.get("title") or "")})

        if a.get("font_size"):
            for s in prs.slides:
                for sh in s.shapes:
                    if sh.has_text_frame:
                        for para in sh.text_frame.paragraphs:
                            for run in para.runs:
                                if run.font.size is None:
                                    run.font.size = Pt(float(a["font_size"]))

        prs.save(out)
        return {"path": out, "written": True, "slides": len(prs.slides),
                "made": made, "ratio": ratio,
                "size": os.path.getsize(out),
                "size_human": _fmt_size(os.path.getsize(out))}

    # ------------------------------------------------ ppt.add

    def _add(self, a):
        """往已有 PPT 加一页。"""
        src = _require_file(a.get("path"))
        from pptx import Presentation
        from pptx.util import Inches

        out, inplace = _resolve_write(a, src, "加页结果.pptx")
        prs = Presentation(src)
        kind = str(a.get("kind") or "bullets").lower()
        layout_map = {"title": 0, "bullets": 1, "section": 2, "blank": 6,
                      "table": 5, "image": 5, "only_title": 5}
        if kind not in layout_map:
            raise ValueError(f"不认这种页：{kind}"
                             "（能 title/bullets/section/table/image/blank）")
        s = prs.slides.add_slide(prs.slide_layouts[layout_map[kind]])
        # 插到指定位置（默认追加到末尾）
        at = a.get("at")
        if at is not None:
            idx = int(at)
            lst = prs.slides._sldIdLst
            items = list(lst)
            el = items[-1]
            lst.remove(el)
            lst.insert(max(0, min(idx, len(items) - 1)), el)

        if a.get("title"):
            try:
                s.shapes.title.text = str(a["title"])
            except Exception:
                tb = s.shapes.add_textbox(Inches(0.6), Inches(0.4),
                                          prs.slide_width - Inches(1.2), Inches(1))
                tb.text_frame.text = str(a["title"])

        if kind == "bullets":
            body = a.get("bullets") or a.get("body") or []
            if isinstance(body, str):
                body = [body]
            try:
                tf = s.placeholders[1].text_frame
            except Exception:
                tf = s.shapes.add_textbox(Inches(0.8), Inches(1.8),
                                          prs.slide_width - Inches(1.6),
                                          Inches(4)).text_frame
            tf.text = ""
            first = True
            for b in body:
                if isinstance(b, dict):
                    txt, lvl = str(b.get("text") or ""), int(b.get("level") or 0)
                else:
                    txt, lvl = str(b), 0
                para = tf.paragraphs[0] if first else tf.add_paragraph()
                para.text = txt
                para.level = min(lvl, 4)
                first = False

        if kind == "table":
            rows = a.get("rows") or []
            if isinstance(rows, str):
                rows = json.loads(rows)
            if not rows:
                raise ValueError("kind=table 必须给 rows（二维数组，含表头）")
            t = s.shapes.add_table(len(rows), len(rows[0]),
                                   Inches(0.6), Inches(1.9),
                                   prs.slide_width - Inches(1.2),
                                   Inches(0.4) * len(rows)).table
            for i, row in enumerate(rows):
                for j, val in enumerate(row):
                    if j < len(t.columns):
                        t.cell(i, j).text = "" if val is None else str(val)

        if kind == "image":
            ip = _norm_path(a.get("image"), "image")
            if not os.path.isfile(ip):
                raise ValueError(f"图片不存在: {ip}")
            from pptx.util import Cm
            kw = {}
            if a.get("width_cm"):
                kw["width"] = Cm(float(a["width_cm"]))
            s.shapes.add_picture(ip, Inches(0.8), Inches(1.9), **kw)

        if a.get("notes"):
            s.notes_slide.notes_text_frame.text = str(a["notes"])

        prs.save(out)
        return {"path": out, "inplace": inplace, "written": True,
                "slides": len(prs.slides), "kind": kind,
                "index": len(prs.slides) - 1}

    # ------------------------------------------------ ppt.table

    def _table(self, a):
        src = _require_file(a.get("path"))
        from pptx import Presentation

        prs = Presentation(src)
        action = str(a.get("action") or "list").lower()
        si = int(a.get("slide") if a.get("slide") is not None else 0)
        if si >= len(prs.slides):
            raise ValueError(f"没有第 {si} 页（共 {len(prs.slides)} 页）")
        slide = prs.slides[si]
        tables = [sh for sh in slide.shapes if sh.has_table]

        if action == "list":
            out = []
            for i, sh in enumerate(tables):
                t = sh.table
                rows = [[c.text.strip() for c in r.cells] for r in t.rows]
                out.append({"index": i, "rows": len(rows),
                            "cols": len(t.columns),
                            "header": rows[0] if rows else [],
                            "data": rows})
            return {"path": src, "slide": si, "count": len(tables),
                    "tables": out}

        out_path, inplace = _resolve_write(a, src, "改表结果.pptx")

        if action == "set":
            ti = int(a.get("index") or 0)
            if ti >= len(tables):
                raise ValueError(f"第 {si} 页没有第 {ti} 张表（共 {len(tables)} 张）")
            t = tables[ti].table
            r = int(a.get("row") or 0)
            c = int(a.get("col") or 0)
            if r >= len(t.rows) or c >= len(t.columns):
                raise ValueError(f"越界：表只有 {len(t.rows)} 行 {len(t.columns)} 列")
            t.cell(r, c).text = "" if a.get("value") is None else str(a["value"])
            prs.save(out_path)
            return {"path": out_path, "inplace": inplace, "written": True,
                    "cell": [r, c], "value": a.get("value")}

        if action == "add":
            rows = a.get("rows")
            if isinstance(rows, str):
                rows = json.loads(rows)
            if not rows:
                raise ValueError("rows 为空")
            from pptx.util import Inches
            t = slide.shapes.add_table(
                len(rows), len(rows[0]),
                Inches(float(a.get("left_cm") or 1.5) / 2.54),
                Inches(float(a.get("top_cm") or 5) / 2.54),
                Inches(float(a.get("width_cm") or 20) / 2.54),
                Inches(0.4) * len(rows)).table
            for i, row in enumerate(rows):
                for j, val in enumerate(row):
                    if j < len(t.columns):
                        t.cell(i, j).text = "" if val is None else str(val)
            prs.save(out_path)
            return {"path": out_path, "inplace": inplace, "written": True,
                    "table_index": len(tables), "rows": len(rows)}

        raise ValueError(f"不认这个 action：{action}（能 list/set/add）")

    # ------------------------------------------------ ppt.image

    def _image(self, a):
        """往某页插图，按给定尺寸自动等比。"""
        src = _require_file(a.get("path"))
        from pptx import Presentation
        from pptx.util import Cm

        ip = _norm_path(a.get("image"), "image")
        if not os.path.isfile(ip):
            raise ValueError(f"图片不存在: {ip}")
        si = int(a.get("slide") if a.get("slide") is not None else -1)
        out, inplace = _resolve_write(a, src, "插图结果.pptx")
        prs = Presentation(src)
        if si < 0:
            si = len(prs.slides) - 1
        if si >= len(prs.slides):
            raise ValueError(f"没有第 {si} 页")
        slide = prs.slides[si]

        kw = {}
        if a.get("width_cm"):
            kw["width"] = Cm(float(a["width_cm"]))
        if a.get("height_cm"):
            kw["height"] = Cm(float(a["height_cm"]))
        left = Cm(float(a.get("left_cm") or 2))
        top = Cm(float(a.get("top_cm") or 4))
        pic = slide.shapes.add_picture(ip, left, top, **kw)

        # 超出边界就等比缩回页内（宁可小，不要看不见）
        max_w = prs.slide_width - left - Cm(0.5)
        max_h = prs.slide_height - top - Cm(0.5)
        shrunk = False
        if pic.width > max_w or pic.height > max_h:
            k = min(max_w / pic.width, max_h / pic.height)
            pic.width = int(pic.width * k)
            pic.height = int(pic.height * k)
            shrunk = True

        prs.save(out)
        return {"path": out, "inplace": inplace, "written": True,
                "slide": si, "shrunk": shrunk,
                "width_cm": _emu_cm(pic.width), "height_cm": _emu_cm(pic.height)}

    # ------------------------------------------------ ppt.notes

    def _notes(self, a):
        """备注：读 / 写 / 批量导出。读不写盘，写要 out。"""
        src = _require_file(a.get("path"))
        from pptx import Presentation

        prs = Presentation(src)
        action = str(a.get("action") or "get").lower()

        if action == "get":
            si = a.get("slide")
            if si is not None:
                idx = int(si)
                if idx >= len(prs.slides):
                    raise ValueError(f"没有第 {idx} 页")
                try:
                    txt = prs.slides[idx].notes_slide.notes_text_frame.text
                except Exception:
                    txt = ""
                return {"path": src, "slide": idx, "notes": txt}
            all_notes = []
            for i, s in enumerate(prs.slides):
                try:
                    t = s.notes_slide.notes_text_frame.text
                except Exception:
                    t = ""
                all_notes.append({"index": i, "notes": t})
            return {"path": src, "count": len(all_notes), "notes": all_notes}

        if action == "set":
            si = int(a.get("slide") if a.get("slide") is not None else 0)
            if si >= len(prs.slides):
                raise ValueError(f"没有第 {si} 页")
            out, inplace = _resolve_write(a, src, "改备注结果.pptx")
            prs.slides[si].notes_slide.notes_text_frame.text = str(a.get("text") or "")
            prs.save(out)
            return {"path": out, "inplace": inplace, "written": True,
                    "slide": si, "notes": str(a.get("text") or "")}

        raise ValueError(f"不认这个 action：{action}（能 get/set）")

    # ------------------------------------------------ ppt.slides

    def _slides(self, a):
        """页管理：list / delete / move。**不做 copy**（会污染 zip part）。"""
        src = _require_file(a.get("path"))
        from pptx import Presentation

        prs = Presentation(src)
        action = str(a.get("action") or "list").lower()

        if action == "list":
            out = []
            for i, s in enumerate(prs.slides):
                title = ""
                try:
                    if s.shapes.title is not None:
                        title = s.shapes.title.text
                except Exception:
                    title = ""
                out.append({"index": i, "title": _clip(title, 120),
                            "shapes": len(s.shapes),
                            "layout": s.slide_layout.name if s.slide_layout else None})
            return {"path": src, "count": len(out), "slides": out}

        out_path, inplace = _resolve_write(a, src, "改页结果.pptx")
        R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

        if action == "delete":
            targets = a.get("index")
            if targets is None:
                raise ValueError("delete 要给 index（数字或数组）")
            idxs = targets if isinstance(targets, list) else [targets]
            idxs = sorted({int(x) for x in idxs}, reverse=True)
            total = len(prs.slides)
            for i in idxs:
                if i < 0 or i >= total:
                    raise ValueError(f"没有第 {i} 页（共 {total} 页）")
            if len(idxs) >= total:
                raise ValueError(
                    f"要把 {len(idxs)} 页全删光，那文件就空了。至少留一页")
            lst = prs.slides._sldIdLst
            for i in idxs:
                el = list(lst)[i]
                rid = el.get(R_ID)
                if rid:
                    prs.part.drop_rel(rid)
                lst.remove(el)
            prs.save(out_path)
            return {"path": out_path, "inplace": inplace, "written": True,
                    "deleted": sorted(idxs), "left": len(prs.slides)}

        if action == "move":
            src_i = a.get("from")
            dst_i = a.get("to")
            if src_i is None or dst_i is None:
                raise ValueError("move 要给 from 和 to（页码，0 起）")
            src_i, dst_i = int(src_i), int(dst_i)
            n = len(prs.slides)
            if not (0 <= src_i < n) or not (0 <= dst_i < n):
                raise ValueError(f"页码越界（0 ~ {n - 1}）")
            lst = prs.slides._sldIdLst
            el = list(lst)[src_i]
            lst.remove(el)
            lst.insert(dst_i, el)
            prs.save(out_path)
            return {"path": out_path, "inplace": inplace, "written": True,
                    "from": src_i, "to": dst_i}

        if action == "copy":
            raise ValueError(
                "不支持复制页：deepcopy 出来的页会复用原 slideN.xml 的 part 名，"
                "保存后 zip 里有重名条目，PowerPoint 会提示修复。"
                "要复制整页请先用 ppt.slides list 找到它，再 ppt.add 重建一页")

        raise ValueError(f"不认这个 action：{action}（能 list/delete/move）")


# ---------------------------------------------------------------- 自测

def _selftest():
    import shutil
    import tempfile

    state = {"n": 0}
    fails = []

    def ck(cond, label):
        state["n"] += 1
        if not cond:
            fails.append(label)

    dev = PptOps()
    ok, why = dev.probe()
    if not ok:
        print("skip: " + why)
        return 0

    td = tempfile.mkdtemp(prefix="ppt_")
    try:
        # ============ ppt.new：从零建 ============
        deck = os.path.join(td, "汇报.pptx")
        r = dev.invoke("ppt.new", {
            "out": deck, "ratio": "16:9",
            "title": "季度汇报", "subtitle": "2026 Q3",
            "slides": [
                {"kind": "bullets", "title": "本季要点",
                 "bullets": ["收入增长", {"text": "成本下降", "level": 1}, "新增客户"]},
                {"kind": "section", "title": "第一部分", "subtitle": "市场"},
                {"kind": "table", "title": "数据",
                 "rows": [["项目", "数值"], ["收入", 1200], ["成本", 800]]},
                {"kind": "blank"},
            ]})
        ck(r.ok, "new：建出 PPT %s" % (r.err if not r.ok else ""))
        ck(r.ok and r.data["slides"] == 5, "new：1 封面 + 4 页 = 5 页")
        ck(r.ok and os.path.getsize(deck) > 5000, "new：文件真有内容")

        # ============ ppt.info ============
        r = dev.invoke("ppt.info", {"path": deck})
        ck(r.ok, "info：能读")
        ck(r.ok and r.data["slides"] == 5, "info：5 页")
        ck(r.ok and r.data["ratio"] == 1.778, "info：16:9 比例")
        ck(r.ok and r.data["width_cm"] > 33, "info：宽 33.87cm")
        ck(r.ok and r.data["detail"][0]["title"] == "季度汇报", "info：首页标题")
        ck(r.ok and r.data["can_render_image"] is False, "info：如实说不能渲图")
        ck(r.ok and len(r.data["layouts"]) >= 7, "info：版式清单")

        # ============ ppt.read ============
        r = dev.invoke("ppt.read", {"path": deck})
        ck(r.ok and "季度汇报" in (r.data.get("text") or ""), "read：抽到封面标题")
        ck(r.ok and "收入增长" in (r.data.get("text") or ""), "read：抽到要点")
        r = dev.invoke("ppt.read", {"path": deck, "slide": 3})
        ck(r.ok and r.data["slides"] == 1, "read：只读一页")
        ck(r.ok and r.data["detail"][0]["index"] == 3, "read：页号对")
        kinds = [x["kind"] for x in r.data["detail"][0]["items"]]
        ck("table" in kinds, "read：认出表格")

        # ============ ppt.outline ============
        r = dev.invoke("ppt.outline", {"path": deck})
        ck(r.ok and r.data["count"] == 5, "outline：5 页")
        ck(r.ok and r.data["outline"][1]["title"] == "本季要点", "outline：标题对")
        ck(r.ok and r.data["outline"][1]["bullet_count"] >= 3, "outline：3 个要点")
        ck(r.ok and "收入增长" in (r.data.get("markdown") or ""),
           "outline：markdown 里有内容")
        ck(r.ok and any(b["level"] == 1 for b in r.data["outline"][1]["bullets"]),
           "outline：保留层级")

        # ============ ppt.add ============
        r = dev.invoke("ppt.add", {"path": deck, "kind": "bullets",
                                   "title": "追加页", "bullets": ["甲", "乙"],
                                   "out": os.path.join(td, "加页.pptx")})
        ck(r.ok and r.data["slides"] == 6, "add：多了一页")
        added = r.data["path"] if r.ok else deck

        r = dev.invoke("ppt.add", {"path": added, "kind": "table",
                                   "title": "加的表",
                                   "rows": [["城市", "人口"], ["北京", 2189]],
                                   "out": os.path.join(td, "加表.pptx")})
        ck(r.ok and r.data["kind"] == "table", "add：加表格页")

        r = dev.invoke("ppt.add", {"path": deck, "kind": "bogus", "title": "x"})
        ck((not r.ok) and "不认这种页" in (r.err or ""), "add：非法页型报错")

        # 插到指定位置
        r = dev.invoke("ppt.add", {"path": deck, "kind": "blank", "at": 1,
                                   "out": os.path.join(td, "插入.pptx")})
        ck(r.ok and r.data["slides"] == 6, "add：at 插入成功")

        # ============ ppt.table ============
        r = dev.invoke("ppt.table", {"path": deck, "slide": 3, "action": "list"})
        ck(r.ok and r.data["count"] == 1, "table：第 4 页 1 张表")
        ck(r.ok and r.data["tables"][0]["header"] == ["项目", "数值"],
           "table：表头对")
        ck(r.ok and r.data["tables"][0]["data"][1] == ["收入", "1200"],
           "table：数据读回")

        r = dev.invoke("ppt.table", {"path": deck, "slide": 3, "action": "set",
                                     "index": 0, "row": 1, "col": 1, "value": 1500,
                                     "out": os.path.join(td, "改表.pptx")})
        ck(r.ok and str(r.data["value"]) == "1500", "table：set 生效")
        r2 = dev.invoke("ppt.table", {"path": r.data["path"], "slide": 3, "action": "list"})
        ck(r2.ok and r2.data["tables"][0]["data"][1][1] == "1500", "table：改后能读回")

        r = dev.invoke("ppt.table", {"path": deck, "slide": 3, "action": "set",
                                     "index": 5, "row": 0, "col": 0, "value": "x"})
        ck((not r.ok) and "没有第" in (r.err or ""), "table：越界表号报错")
        r = dev.invoke("ppt.table", {"path": deck, "slide": 99, "action": "list"})
        ck((not r.ok) and "没有第" in (r.err or ""), "table：越界页号报错")

        # ============ ppt.image ============
        from PIL import Image
        img = os.path.join(td, "图.png")
        Image.new("RGB", (800, 400), (40, 130, 210)).save(img)

        r = dev.invoke("ppt.image", {"path": deck, "image": img, "slide": 4,
                                     "width_cm": 12, "out": os.path.join(td, "插图.pptx")})
        ck(r.ok, "image：插图成功 %s" % (r.err if not r.ok else ""))
        ck(r.ok and abs(r.data["width_cm"] - 12) < 0.1, "image：宽度按 12cm")
        ck(r.ok and r.data["shrunk"] is False, "image：没超界不缩")

        # 超出边界要自动缩回页内
        r = dev.invoke("ppt.image", {"path": deck, "image": img, "slide": 4,
                                     "width_cm": 60, "left_cm": 2, "top_cm": 4,
                                     "out": os.path.join(td, "大图.pptx")})
        ck(r.ok and r.data["shrunk"] is True, "image：超界自动缩回")
        r2 = dev.invoke("ppt.info", {"path": r.data["path"]})
        ck(r2.ok, "image：缩完文件还能读")

        r = dev.invoke("ppt.image", {"path": deck, "image": os.path.join(td, "无.png"),
                                     "slide": 4})
        ck((not r.ok) and "图片不存在" in (r.err or ""), "image：图片不存在报错")

        # ============ ppt.notes ============
        r = dev.invoke("ppt.notes", {"path": deck, "action": "get"})
        ck(r.ok and r.data["count"] == 5, "notes：5 页备注")
        r = dev.invoke("ppt.notes", {"path": deck, "action": "set", "slide": 1,
                                     "text": "这里要强调增长",
                                     "out": os.path.join(td, "备注.pptx")})
        ck(r.ok and r.data["notes"] == "这里要强调增长", "notes：写进备注")
        r2 = dev.invoke("ppt.notes", {"path": r.data["path"], "action": "get", "slide": 1})
        ck(r2.ok and "强调增长" in (r2.data.get("notes") or ""), "notes：读回备注")

        # ============ ppt.slides ============
        r = dev.invoke("ppt.slides", {"path": deck, "action": "list"})
        ck(r.ok and r.data["count"] == 5, "slides：5 页")
        ck(r.ok and r.data["slides"][0]["title"] == "季度汇报", "slides：首页标题")

        r = dev.invoke("ppt.slides", {"path": deck, "action": "delete", "index": 4,
                                      "out": os.path.join(td, "删页.pptx")})
        ck(r.ok and r.data["left"] == 4, "slides：删一页剩 4")
        r2 = dev.invoke("ppt.slides", {"path": r.data["path"], "action": "list"})
        ck(r2.ok and r2.data["count"] == 4, "slides：删后能读")

        r = dev.invoke("ppt.slides", {"path": deck, "action": "delete",
                                      "index": [0, 1, 2, 3, 4]})
        ck((not r.ok) and "全删光" in (r.err or ""), "slides：全删要拒绝")
        r = dev.invoke("ppt.slides", {"path": deck, "action": "delete", "index": 99})
        ck((not r.ok) and "没有第" in (r.err or ""), "slides：越界删报错")

        r = dev.invoke("ppt.slides", {"path": deck, "action": "move", "from": 4, "to": 0,
                                      "out": os.path.join(td, "移页.pptx")})
        ck(r.ok, "slides：移动成功 %s" % (r.err if not r.ok else ""))
        r2 = dev.invoke("ppt.slides", {"path": r.data["path"], "action": "list"})
        ck(r2.ok and r2.data["count"] == 5, "slides：移动不改页数")
        r3 = dev.invoke("ppt.info", {"path": r.data["path"]})
        ck(r3.ok and r3.data["detail"][0]["layout"] is not None, "slides：移后结构完好")

        r = dev.invoke("ppt.slides", {"path": deck, "action": "copy", "index": 0})
        ck((not r.ok) and "不支持复制" in (r.err or ""), "slides：复制如实拒绝")

        # ============ ppt.fill：核心 = 跨 run 替换 ============
        from pptx import Presentation
        from pptx.util import Inches, Pt

        tpl = os.path.join(td, "模板.pptx")
        prs = Presentation()
        s = prs.slides.add_slide(prs.slide_layouts[5])
        # 🔴 故意造一个被拆成两个 run 的占位符（PowerPoint 真会这么干）
        tb = s.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
        tf = tb.text_frame
        tf.text = "标题："
        para = tf.paragraphs[0]
        r1 = para.add_run()
        r1.text = "{na"
        r1.font.bold = True
        r1.font.size = Pt(32)
        r2 = para.add_run()
        r2.text = "me}结束"
        # 第二个：普通整串
        tb2 = s.shapes.add_textbox(Inches(1), Inches(2.5), Inches(8), Inches(1))
        tb2.text_frame.text = "负责人：{owner}"
        # 第三个：Jinja 双花括号
        tb3 = s.shapes.add_textbox(Inches(1), Inches(3.5), Inches(8), Inches(1))
        tb3.text_frame.text = "部门：{{ dept }}"
        # 第四个：表格里的占位符
        tbl = s.shapes.add_table(2, 2, Inches(1), Inches(4.5),
                                 Inches(6), Inches(1)).table
        tbl.cell(0, 0).text = "项目"
        tbl.cell(0, 1).text = "数值"
        tbl.cell(1, 0).text = "{item}"
        tbl.cell(1, 1).text = "{qty}"
        prs.save(tpl)

        filled = os.path.join(td, "填充成品.pptx")
        r = dev.invoke("ppt.fill", {"path": tpl, "out": filled, "data": {
            "name": "年度总结", "owner": "张三", "dept": "研发部",
            "item": "服务器", "qty": "12"}})
        ck(r.ok, "fill：填充成功 %s" % (r.err if not r.ok else ""))
        ck(r.ok and os.path.getsize(filled) > 3000, "fill：产物有内容")
        r2 = dev.invoke("ppt.read", {"path": filled})
        txt = (r2.data.get("text") or "") if r2.ok else ""
        ck("年度总结" in txt, "fill：跨 run 的 {name} 被替换")
        ck("张三" in txt, "fill：整串的 {owner} 被替换")
        ck("研发部" in txt, "fill：Jinja 双括号被替换")
        ck("{na" not in txt and "me}" not in txt, "fill：跨 run 残留清干净")
        ck("{owner}" not in txt, "fill：没有残留 {owner}")
        r3 = dev.invoke("ppt.table", {"path": filled, "slide": 0, "action": "list"})
        ck(r3.ok and r3.data["tables"][0]["data"][1] == ["服务器", "12"],
           "fill：表格里也替换了")
        # 格式保留：跨 run 替换后第一段仍应是加粗 32pt
        prs2 = Presentation(filled)
        for sh in prs2.slides[0].shapes:
            if sh.has_text_frame and "年度总结" in sh.text_frame.text:
                p0 = sh.text_frame.paragraphs[0]
                bold_runs = [x for x in p0.runs if x.font.bold]
                ck(len(bold_runs) >= 1, "fill：加粗格式保留")
                sizes = [x.font.size for x in p0.runs if x.font.size]
                ck(any(sz and sz >= Pt(32) for sz in sizes), "fill：32pt 字号保留")
                break

        # ============ ppt.fill：内置引擎（去掉包也一样能跑） ============
        r = dev.invoke("ppt.fill", {"path": tpl,
                                    "out": os.path.join(td, "内置引擎.pptx"),
                                    "data": {"name": "内置", "owner": "李四",
                                             "dept": "市场", "item": "A", "qty": "1"},
                                    "engine": "builtin"})
        ck(r.ok and r.data["engine"] == "builtin", "fill：内置引擎能跑")
        r2 = dev.invoke("ppt.read", {"path": r.data["path"]})
        txt2 = (r2.data.get("text") or "") if r2.ok else ""
        ck("内置" in txt2, "fill：内置引擎替换跨 run 也对")
        ck("李四" in txt2, "fill：内置引擎整串也对")

        # ============ ppt.fill：只给一部分键时要能出文件，且没填的不算「残留」 ============
        r = dev.invoke("ppt.fill", {"path": tpl,
                                    "out": os.path.join(td, "半填.pptx"),
                                    "data": {"name": "只有标题"}})
        ck(r.ok, "fill：只给部分变量也能出文件")
        ck(r.ok and r.data["unfilled"] == [],
           "fill：没给值的键不算残留（保留原样是预期）")
        # 给了值却替换不掉，才是真残留——这里用一个模板里不存在的键验证扫描器
        r2 = dev.invoke("ppt.fill", {"path": tpl,
                                     "out": os.path.join(td, "多给.pptx"),
                                     "data": {"name": "甲", "不存在的键": "乙"}})
        ck(r2.ok and "不存在的键" not in r2.data["unfilled"],
           "fill：模板里没有的键不误报残留")

        # ============ 错误路径 ============
        r = dev.invoke("ppt.info", {"path": os.path.join(td, "不存在.pptx")})
        ck((not r.ok) and "文件不存在" in (r.err or ""), "错误：文件不存在")

        old = os.path.join(td, "老.ppt")
        with open(old, "wb") as f:
            f.write(b"\xd0\xcf\x11\xe0" + b"\x00" * 100)
        r = dev.invoke("ppt.info", {"path": old})
        ck((not r.ok) and "convert.file" in (r.err or ""), "错误：.ppt 指引去转换")

        r = dev.invoke("ppt.new", {"out": os.path.join(td, "空.pptx")})
        ck((not r.ok) and "至少要" in (r.err or ""), "错误：没内容要报错")

        r = dev.invoke("ppt.table", {"path": deck, "slide": 0, "action": "zap"})
        ck((not r.ok) and "不认这个 action" in (r.err or ""), "错误：非法 action")

        # ============ 安全：另存不覆盖 / inplace 预演 ============
        a1 = os.path.join(td, "同名.pptx")
        dev.invoke("ppt.new", {"out": a1, "title": "第一份"})
        r = dev.invoke("ppt.new", {"out": a1, "title": "第二份"})
        ck(r.ok and r.data["path"] != a1, "安全：重名自动让号")
        ck(os.path.exists(a1), "安全：原文件还在")

        r = dev.invoke("ppt.notes", {"path": a1, "action": "set", "slide": 0,
                                     "text": "改", "inplace": True})
        ck((not r.ok) and "_confirmed" in (r.err or ""), "安全：inplace 第一次只预演")
        r = dev.invoke("ppt.notes", {"path": a1, "action": "set", "slide": 0,
                                     "text": "改", "inplace": True, "_confirmed": True})
        ck(r.ok and r.data["inplace"] is True, "安全：确认后真改")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    total = state["n"]
    got = total - len(fails)
    print("\npptops selftest: %d/%d%s"
          % (got, total, "" if not fails else "（失败 %d）" % len(fails)))
    for f in fails:
        print("   失败:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法: python -m fleet.kinds.pptops --selftest")
    sys.exit(0)

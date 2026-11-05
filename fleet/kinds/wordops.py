"""文档层：Word 的**写**与模板填充。

为什么单独一台（2026-11-06）：
  `doc.convert` 把 docx 抽成文本，那是**读**；就像早先的 xlsx 一样，抽出来回不去。
  真正每天要用的是**写合同/报告/通知书**：一个模板 + 一份数据 → 一百份成品。
  这件事的轮子已经有了：`docxtpl 0.20.2`（Jinja2 语法，MIT），
  它替我们处理了 Word 最脏的部分——**占位符被拆到多个 run 里**
  （Word 会因为拼写检查、格式变化把 `{{ name }}` 切成 `{{ na` + `me }}`，
  纯字符串替换必然漏，docxtpl 在 patch_xml 时先把 run 合并再渲染）。

选型理由（不是随手挑的）：
  · `docxtpl` 而非手写 XML：它已被 `python-docx-template` 项目维护多年，0.20.2，
    核心就是「Jinja2 + python-docx」，表格整行循环 `{%tr%}`、段落条件 `{%p%}` 都现成。
  · 不引 `python-docx-template` 之外的东西：富文本/图片都在 docxtpl 里（RichText / InlineImage）。

🔴 最重要的一条设计决策：**默认 StrictUndefined**。
  Jinja2 默认把未定义变量渲染成**空字符串**——模板里变量名打错一个字母，
  合同上那一栏就静默变空白，签完字才发现。所以这里默认严格模式：
  缺变量直接报错，并告诉你是哪个。要放行必须显式 `missing="blank"`。

诚实边界（写进 docstring，不假装能做）：
  · **docx 没有「页」**。它是流式排版，页码由 Word 打开时算。所以本设备不做
    「第 3 页」这种操作，只有段落序号和「分页符」。要按页处理请先 `convert` 成 PDF。
  · **不改页数相关的版式**（分栏、节的分页规则只读不写）。
  · **不保留宏**（.docm 能读不能写）。
  · **公式（OMML）/ 图表 / SmartArt 不解析**，只报「这里有个对象」。
  · **修订与批注不合并**：读的时候能看到 `w:ins`/`w:del` 里的字，写的时候不生成修订。

铁律：默认**另存不覆盖**；重名自动让号（`报告_1.docx`）。原地改要 `inplace=true` + `_confirmed=true`。
"""
import datetime as _dt
import json
import os
import re

from ..core import BaseDevice, DeviceResult as R

#: 一次返回的最大段落数（再多就该分页取了）
MAX_PARAS = 20_000
#: 一句话的长度上限，超过就在 read 里截断（避免把整本书塞进上下文）
MAX_PARA_CHARS = 4_000
#: python-docx 能读的扩展名
READ_EXTS = (".docx", ".docm", ".dotx", ".dotm")
#: 能写的扩展名（docm/dotm 会丢宏，默认拒绝）
WRITE_EXTS = (".docx",)

#: 段落样式名 → 人的叫法
STYLE_HINT = {
    "Title": "标题", "Subtitle": "副标题", "Heading 1": "一级标题",
    "Heading 2": "二级标题", "Heading 3": "三级标题", "Heading 4": "四级标题",
    "Normal": "正文", "Quote": "引用", "List Paragraph": "列表项",
    "Caption": "图表题注", "TOC Heading": "目录标题",
}

#: 中文常用单位换算（docx 内部用 EMU，1 英寸 = 914400）
_EMU_PER_CM = 360000
_EMU_PER_PT = 12700


# ---------------------------------------------------------------- 小工具

def _norm_path(p, field="path"):
    """Windows 路径归一到 Python 能吃的形式（`/e/x` → `E:/x`）。"""
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
            "老的 .doc 是二进制格式，先走 convert.file 转成 docx")
    return p


def _unique(path):
    """重名自动让号：`a.docx` → `a_1.docx`。**永不覆盖**。"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{base}_{n}{ext}"):
        n += 1
    return f"{base}_{n}{ext}"


def _out_path(a, src=None, default_name="输出.docx"):
    """决定输出路径：给了 out 就用；没给就在源文件旁边加后缀。"""
    out = a.get("out")
    if out:
        out = _norm_path(out, "out")
        ext = os.path.splitext(out)[1].lower()
        if ext not in WRITE_EXTS:
            raise ValueError(f"只能写 {'/'.join(WRITE_EXTS)}，不写 {ext}")
    elif src:
        base, _ = os.path.splitext(src)
        out = base + "_out.docx"
    else:
        out = os.path.abspath(default_name)
    d = os.path.dirname(os.path.abspath(out))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    return out


def _resolve_write(a, src=None, default_name="输出.docx"):
    """处理 inplace：返回 (目标路径, 是否原地)。原地第一次只预演。"""
    if not a.get("inplace"):
        return _unique(_out_path(a, src, default_name)), False
    if src is None:
        raise ValueError("inplace 必须配合 path 用")
    if not a.get("_confirmed"):
        raise ValueError(
            f"原地改会直接覆盖 {os.path.basename(src)}。"
            "确认无误请加 _confirmed=true 再调一次（这次什么都没动）")
    return src, True


def _strip_tag(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _para_text(p_el):
    """把段落里所有 w:t 拼起来（**跨 run**）——这是读到真实文字的唯一办法。"""
    out = []
    for node in p_el.iter():
        if _strip_tag(node.tag) == "t" and node.text:
            out.append(node.text)
        elif _strip_tag(node.tag) == "tab":
            out.append("\t")
        elif _strip_tag(node.tag) == "br":
            out.append("\n")
    return "".join(out)


def _clip(s, n=MAX_PARA_CHARS):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:n] + f"…[截断，原长 {len(s)}]"


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


#: 占位符正则：`{{ var }}` 取值，`{% ... %}` 语句
_RE_VAR = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_RE_TAG = re.compile(r"\{%\s*(\w+)\b(.*?)%\}", re.S)
#: 从表达式里抠出根变量名（`it.name` → `it`，`user['x']` → `user`）
_RE_ROOT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*")


def _root_var(expr):
    """从 Jinja 表达式里取根变量名，取不到返回 None。"""
    expr = expr.strip()
    m = _RE_ROOT.match(expr)
    if not m:
        return None
    name = m.group(0)
    if name in ("true", "false", "none", "True", "False", "None", "not", "and", "or"):
        return None
    return name


# ---------------------------------------------------------------- 设备

class WordOps(BaseDevice):
    kind = "wordops"
    via = "local"

    def __init__(self, name="word:ops", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "document")

    def probe(self):
        try:
            import docx  # noqa: F401
        except Exception:
            return False, "没装 python-docx：pip install python-docx"
        try:
            import docxtpl  # noqa: F401
        except Exception:
            return False, "没装 docxtpl：pip install docxtpl"
        return True, ""

    def caps(self):
        return ("word.info", "word.read", "word.placeholders", "word.fill",
                "word.write", "word.append", "word.table", "word.style",
                "word.header", "word.meta")

    # ------------------------------------------------ 分派

    def invoke(self, cap, args=None):
        args = args or {}
        fn = {
            "word.info": self._info,
            "word.read": self._read,
            "word.placeholders": self._placeholders,
            "word.fill": self._fill,
            "word.write": self._write,
            "word.append": self._append,
            "word.table": self._table,
            "word.style": self._style,
            "word.header": self._header,
            "word.meta": self._meta,
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
        # 返回 (数据, 是否写盘)
        if isinstance(payload, tuple):
            data, wrote = payload
        else:
            data, wrote = payload, cap != "word.info" and cap != "word.read" \
                and cap != "word.placeholders"
        return R.success(cap, device=self.name, data=data,
                         side_effect=bool(data.get("written")) if isinstance(data, dict) else wrote,
                         via=self.via)

    # ------------------------------------------------ word.info

    def _info(self, a):
        p = _require_file(a.get("path"))
        from docx import Document

        d = Document(p)
        styles = {}
        paras = 0
        chars = 0
        imgs = 0
        for para in d.paragraphs:
            t = para.text
            paras += 1
            chars += len(t.replace(" ", "").replace("\u3000", ""))
            if para.style and para.style.name:
                styles[para.style.name] = styles.get(para.style.name, 0) + 1
        for tbl in d.tables:
            for row in tbl.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        chars += len(para.text.strip())
        # 图片：数 document.xml 里的 a:blip
        for blip in d.element.body.iter():
            if _strip_tag(blip.tag) == "blip":
                imgs += 1

        core = d.core_properties
        return {
            "path": p,
            "size": os.path.getsize(p),
            "size_human": _fmt_size(os.path.getsize(p)),
            "paragraphs": paras,
            "tables": len(d.tables),
            "images": imgs,
            "sections": len(d.sections),
            "chars": chars,
            "styles": dict(sorted(styles.items(), key=lambda kv: -kv[1])),
            "headings": [STYLE_HINT.get(k, k) for k in styles
                         if k.lower().startswith("heading") or k == "Title"],
            "page": {
                "width_cm": round(d.sections[0].page_width / _EMU_PER_CM, 2)
                if d.sections else None,
                "height_cm": round(d.sections[0].page_height / _EMU_PER_CM, 2)
                if d.sections else None,
                "orient": ("横向" if d.sections and
                           d.sections[0].page_width > d.sections[0].page_height
                           else "纵向") if d.sections else None,
            },
            "meta": {
                "title": core.title or None,
                "author": core.author or None,
                "created": str(core.created) if core.created else None,
                "modified": str(core.modified) if core.modified else None,
            },
            "supported_write": list(WRITE_EXTS),
        }

    # ------------------------------------------------ word.read

    def _read(self, a):
        p = _require_file(a.get("path"))
        from docx import Document

        d = Document(p)
        mode = str(a.get("mode") or "text").lower()
        with_tbl = bool(a.get("tables", True))
        limit = int(a.get("limit") or MAX_PARAS)
        start = int(a.get("offset") or 0)

        blocks = []
        hit_limit = False
        # 按 body 的真实顺序走（段落和表格是交错的，不能分两趟）
        bodies = d.element.body
        p_idx, t_idx = 0, 0
        for child in bodies.iterchildren():
            tag = _strip_tag(child.tag)
            if tag == "p":
                from docx.text.paragraph import Paragraph
                para = Paragraph(child, d)
                text = para.text
                style = para.style.name if para.style else ""
                # 空段落也算（保留排版信息），但可以跳过纯空行
                if not text.strip() and not a.get("keep_blank"):
                    p_idx += 1
                    continue
                block = {"type": "para", "index": p_idx,
                         "text": _clip(text), "style": style}
                if mode == "markdown" and style in STYLE_HINT:
                    hint = STYLE_HINT[style]
                    if hint.startswith("一") or hint.startswith("二") or \
                            hint.startswith("三") or hint.startswith("四"):
                        lvl = "一二三四".index(hint[0]) + 1
                        block["md"] = "#" * lvl + " " + text
                blocks.append(block)
                p_idx += 1
            elif tag == "tbl":
                if not with_tbl:
                    continue
                from docx.table import Table
                tbl = Table(child, d)
                rows = []
                for row in tbl.rows:
                    rows.append([c.text.strip() for c in row.cells])
                blocks.append({"type": "table", "index": t_idx, "rows": rows,
                               "shape": [len(rows), len(rows[0]) if rows else 0]})
                t_idx += 1
            if len(blocks) >= start + limit:
                hit_limit = True
                break

        picked = blocks[start:start + limit]
        out = {
            "path": p,
            "mode": mode,
            "blocks": picked,
            "returned": len(picked),
            # 截断时这只是「已数到的」块数，不是全文档总数——如实标，不假装知道
            "total_blocks": len(blocks),
            "total_blocks_exact": not hit_limit,
            "truncated": hit_limit or len(blocks) > start + limit,
            "note": "docx 是流式排版，没有「页」；这里按段落块顺序给，不是页码",
        }
        if mode == "text":
            out["text"] = "\n".join(
                b["text"] if b["type"] == "para"
                else " | ".join(str(x) for x in b["rows"][0])
                for b in picked)
        elif mode == "markdown":
            lines = []
            for b in picked:
                if b["type"] == "para":
                    lines.append(b.get("md") or b["text"])
                else:
                    rows = b["rows"]
                    if not rows:
                        continue
                    lines.append("| " + " | ".join(rows[0]) + " |")
                    lines.append("| " + " | ".join("---" for _ in rows[0]) + " |")
                    for r in rows[1:]:
                        lines.append("| " + " | ".join(r) + " |")
            out["markdown"] = "\n".join(lines)
        return out

    # ------------------------------------------------ word.placeholders

    def _placeholders(self, a):
        """列出模板里的所有占位符，并告诉调用方「fill 需要给哪些键」。"""
        p = _require_file(a.get("path"))
        from docx import Document

        d = Document(p)
        # 用段落完整文本（跨 run）来找，才认得出被 Word 拆开的 {{ name }}
        texts = []
        for para in d.paragraphs:
            texts.append(_para_text(para._p))
        for tbl in d.tables:
            for row in tbl.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        texts.append(_para_text(para._p))
        # 页眉页脚也要扫
        for sec in d.sections:
            for part in (sec.header, sec.footer):
                try:
                    for para in part.paragraphs:
                        texts.append(_para_text(para._p))
                except Exception:
                    pass

        blob = "\n".join(texts)
        vars_, tags, loops = [], [], []
        for m in _RE_VAR.finditer(blob):
            expr = m.group(1).strip()
            root = _root_var(expr)
            if not root:
                continue
            if root not in [v["name"] for v in vars_]:
                vars_.append({"name": root, "expr": expr})
        for m in _RE_TAG.finditer(blob):
            kind = m.group(1)
            rest = m.group(2).strip()
            tags.append({"tag": kind, "expr": rest,
                         "raw": "{%" + kind + rest + "%}"})
            if kind == "tr" or kind == "p":
                inner = rest
                sub = re.match(r"(for|if)\b(.*)", inner, re.S)
                if sub:
                    loops.append({"kind": sub.group(1), "expr": sub.group(2).strip()})
        has_jinja = bool(vars_ or tags)
        out = {
            "path": p,
            "is_template": has_jinja,
            "variables": vars_,
            "need_keys": [v["name"] for v in vars_],
            "tags": tags,
            "loops": loops,
            "count": len(vars_),
        }
        if not has_jinja:
            out["hint"] = ("这文件里没有 {{ }} 占位符，它是成品不是模板。"
                           "要做模板请把要替换的地方写成 {{ 变量名 }}")
        return out

    # ------------------------------------------------ word.fill

    def _fill(self, a):
        """模板填充：docxtpl + Jinja2。默认 StrictUndefined。"""
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
            raise ValueError("data 必须是对象（{变量名: 值}）")

        from docxtpl import DocxTemplate, InlineImage
        from docx.shared import Mm, Cm, Inches
        import jinja2

        out, inplace = _resolve_write(a, p, "填充结果.docx")
        tpl = DocxTemplate(p)

        # 图片：值写成 {"__image__": "路径", "width_mm": 40}
        imgs = a.get("images") or {}
        unit = Mm
        size_key = "width_mm"
        if a.get("unit") == "cm":
            unit, size_key = Cm, "width_cm"
        elif a.get("unit") == "in":
            unit, size_key = Inches, "width_in"
        for key, spec in (imgs or {}).items():
            if isinstance(spec, str):
                spec = {"path": spec}
            ip = _norm_path(spec.get("path"), f"images.{key}.path")
            if not os.path.isfile(ip):
                raise ValueError(f"图片不存在: {ip}")
            w = spec.get(size_key)
            ctx[key] = InlineImage(tpl, ip, width=unit(w)) if w else InlineImage(tpl, ip)

        # 🔴 默认严格：缺变量直接报，不静默变空
        missing_mode = str(a.get("missing") or "error").lower()
        if missing_mode == "blank":
            env = jinja2.Environment()
        else:
            env = jinja2.Environment(undefined=jinja2.StrictUndefined)

        # 先自查缺哪些键，给一条能直接照做的错误，而不是 Jinja 的裸报错
        ph = self._placeholders({"path": p})
        need = [k for k in ph["need_keys"] if k not in ctx]
        # 循环里的变量（items）才是真需要的；循环体内的 it 不算缺
        loop_vars = set()
        for lp in ph["loops"]:
            if lp["kind"] == "for":
                m = re.match(r"([A-Za-z_]\w*)\s+in\s+", lp["expr"])
                if m:
                    loop_vars.add(m.group(1))
        need = [k for k in need if k not in loop_vars]
        if need and missing_mode != "blank":
            raise ValueError(
                "模板里这些变量没给值: " + ", ".join(sorted(need)) +
                "。补齐 data 再调，或显式 missing=\"blank\" 允许留空")

        try:
            tpl.render(ctx, jinja_env=env)
        except jinja2.UndefinedError as e:
            raise ValueError(f"模板变量未定义: {e}。"
                             "要允许留空请显式 missing=\"blank\"")
        except jinja2.TemplateSyntaxError as e:
            raise ValueError(f"模板语法错误（第 {e.lineno} 行）: {e.message}")

        tpl.save(out)
        return {
            "path": out,
            "inplace": inplace,
            "written": True,
            "filled_keys": sorted(k for k in ctx if not k.startswith("_")),
            "images": list(imgs.keys()),
            "size": os.path.getsize(out),
            "size_human": _fmt_size(os.path.getsize(out)),
        }

    # ------------------------------------------------ word.write

    def _write(self, a):
        """从文本 / Markdown / 结构化块 生成一份新 docx。"""
        from docx import Document
        from docx.shared import Pt, Cm
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        out = _resolve_write(a)[0]
        d = Document()

        # 纸张与页边距
        sec = d.sections[0]
        if a.get("page") == "a4":
            sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
        for k, attr in (("margin_top_cm", "top_margin"), ("margin_bottom_cm", "bottom_margin"),
                        ("margin_left_cm", "left_margin"), ("margin_right_cm", "right_margin")):
            if a.get(k) is not None:
                setattr(sec, attr, Cm(float(a[k])))

        # 默认字体（中文字体要同时设 eastAsia，否则 Word 会用宋体兜底）
        font_name = a.get("font") or "微软雅黑"
        font_size = float(a.get("font_size") or 11)
        try:
            st = d.styles["Normal"]
            st.font.name = font_name
            st.font.size = Pt(font_size)
            rpr = st.element.get_or_add_rPr()
            rf = rpr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rFonts")
            if rf is None:
                from docx.oxml.ns import qn
                rf = rpr.makeelement(qn("w:rFonts"), {})
                rpr.append(rf)
            from docx.oxml.ns import qn
            rf.set(qn("w:eastAsia"), font_name)
        except Exception:
            pass

        title = a.get("title")
        if title:
            tp = d.add_heading(str(title), level=0)
            tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
            if a.get("subtitle"):
                sp = d.add_paragraph(str(a["subtitle"]))
                sp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        # 同时写进文档属性——标题栏里能看到，不是只当正文大字
        if title:
            d.core_properties.title = str(title)
        if a.get("author"):
            d.core_properties.author = str(a["author"])
        if a.get("subject"):
            d.core_properties.subject = str(a["subject"])
        if a.get("keywords"):
            d.core_properties.keywords = str(a["keywords"])

        text = a.get("text")
        md = a.get("markdown")
        blocks = a.get("blocks")

        def add_md_line(line):
            s = line.rstrip()
            if not s.strip():
                return
            m = re.match(r"^(#{1,6})\s+(.*)$", s)
            if m:
                d.add_heading(m.group(2).strip(), level=min(len(m.group(1)), 6))
                return
            m = re.match(r"^[-*+]\s+(.*)$", s)
            if m:
                d.add_paragraph(m.group(1).strip(), style="List Bullet")
                return
            m = re.match(r"^(\d+)[.)]\s+(.*)$", s)
            if m:
                d.add_paragraph(m.group(2).strip(), style="List Number")
                return
            m = re.match(r"^>\s*(.*)$", s)
            if m:
                d.add_paragraph(m.group(1).strip(), style="Quote")
                return
            if s.startswith("```"):
                return
            # **加粗** 行内
            p = d.add_paragraph()
            for seg in re.split(r"(\*\*[^*]+\*\*)", s):
                if seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
                    r = p.add_run(seg[2:-2])
                    r.bold = True
                else:
                    p.add_run(seg)

        if blocks:
            for b in blocks:
                kind = b.get("type") or "para"
                if kind == "heading":
                    d.add_heading(str(b.get("text") or ""), level=int(b.get("level") or 1))
                elif kind == "table":
                    rows = b.get("rows") or []
                    if not rows:
                        continue
                    t = d.add_table(rows=len(rows), cols=len(rows[0]))
                    t.style = b.get("style") or "Table Grid"
                    for i, row in enumerate(rows):
                        for j, val in enumerate(row):
                            if j < len(t.columns):
                                t.cell(i, j).text = "" if val is None else str(val)
                elif kind == "image":
                    ip = _norm_path(b.get("path"), "blocks.image.path")
                    if os.path.isfile(ip):
                        d.add_picture(ip, width=Cm(float(b.get("width_cm") or 12)))
                elif kind == "pagebreak":
                    d.add_page_break()
                else:
                    p = d.add_paragraph()
                    segs = re.split(r"(\*\*[^*]+\*\*)", str(b.get("text") or ""))
                    for seg in segs:
                        if seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
                            r = p.add_run(seg[2:-2])
                            r.bold = True
                        else:
                            p.add_run(seg)
        elif md:
            for line in str(md).splitlines():
                add_md_line(line)
        elif text is not None:
            for line in str(text).splitlines():
                if line.strip():
                    d.add_paragraph(line)
        elif not title:
            raise ValueError("没给内容：至少要 text / markdown / blocks 之一")

        d.save(out)
        return {
            "path": out, "written": True,
            "paragraphs": len(d.paragraphs),
            "tables": len(d.tables),
            "size": os.path.getsize(out),
            "size_human": _fmt_size(os.path.getsize(out)),
        }

    # ------------------------------------------------ word.append

    def _append(self, a):
        """往已有文档尾部追加：段落 / 标题 / 表格 / 图片 / 分页符。"""
        src = _require_file(a.get("path"))
        from docx import Document
        from docx.shared import Cm, Pt

        out, inplace = _resolve_write(a, src, "追加结果.docx")
        d = Document(src)
        added = {"para": 0, "heading": 0, "table": 0, "image": 0, "pagebreak": 0}

        items = a.get("items")
        if items is None:
            items = []
            if a.get("text"):
                items.append({"type": "para", "text": a["text"]})
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except Exception as e:
                raise ValueError(f"items 不是合法 JSON: {e}")
        if not isinstance(items, list):
            raise ValueError("items 必须是数组")
        if not items:
            raise ValueError("没给要追加的内容（items 为空）")

        for it in items:
            kind = (it.get("type") or "para").lower()
            if kind == "heading":
                d.add_heading(str(it.get("text") or ""), level=int(it.get("level") or 1))
                added["heading"] += 1
            elif kind == "table":
                rows = it.get("rows") or []
                if not rows:
                    continue
                t = d.add_table(rows=len(rows), cols=len(rows[0]))
                t.style = it.get("style") or "Table Grid"
                for i, row in enumerate(rows):
                    for j, val in enumerate(row):
                        if j < len(t.columns):
                            t.cell(i, j).text = "" if val is None else str(val)
                added["table"] += 1
            elif kind == "image":
                ip = _norm_path(it.get("path"), "items.image.path")
                if not os.path.isfile(ip):
                    raise ValueError(f"图片不存在: {ip}")
                if it.get("width_cm"):
                    d.add_picture(ip, width=Cm(float(it["width_cm"])))
                else:
                    d.add_picture(ip)
                added["image"] += 1
            elif kind == "pagebreak":
                d.add_page_break()
                added["pagebreak"] += 1
            else:
                p = d.add_paragraph()
                p.add_run(str(it.get("text") or ""))
                if it.get("bold"):
                    p.runs[-1].bold = True
                if it.get("size"):
                    p.runs[-1].font.size = Pt(float(it["size"]))
                added["para"] += 1

        d.save(out)
        return {"path": out, "inplace": inplace, "written": True,
                "added": added, "size": os.path.getsize(out),
                "size_human": _fmt_size(os.path.getsize(out))}

    # ------------------------------------------------ word.table

    def _table(self, a):
        """表格：list（读）/ add（加）/ set（改单元格）/ delete（删）。"""
        src = _require_file(a.get("path"))
        from docx import Document

        action = str(a.get("action") or "list").lower()
        d = Document(src)

        if action == "list":
            idx = a.get("index")
            out = []
            for i, tbl in enumerate(d.tables):
                if idx is not None and int(idx) != i:
                    continue
                rows = [[c.text.strip() for c in row.cells] for row in tbl.rows]
                out.append({"index": i, "rows": len(rows),
                            "cols": len(tbl.columns),
                            "header": rows[0] if rows else [],
                            "data": rows[:int(a.get("limit") or 200)]})
            return {"path": src, "count": len(d.tables), "tables": out}

        out, inplace = _resolve_write(a, src, "改表结果.docx")

        if action == "add":
            rows = a.get("rows")
            if isinstance(rows, str):
                try:
                    rows = json.loads(rows)
                except Exception as e:
                    raise ValueError(f"rows 不是合法 JSON: {e}")
            if not rows:
                raise ValueError("rows 为空：给二维数组 [[表头...],[数据...]]")
            t = d.add_table(rows=len(rows), cols=len(rows[0]))
            t.style = a.get("style") or "Table Grid"
            for i, row in enumerate(rows):
                for j, val in enumerate(row):
                    if j < len(t.columns):
                        t.cell(i, j).text = "" if val is None else str(val)
            d.save(out)
            return {"path": out, "inplace": inplace, "written": True,
                    "table_index": len(d.tables) - 1, "rows": len(rows),
                    "cols": len(rows[0])}

        if action == "set":
            ti = int(a.get("index") or 0)
            if ti >= len(d.tables):
                raise ValueError(f"没有第 {ti} 张表（共 {len(d.tables)} 张）")
            tbl = d.tables[ti]
            r = int(a.get("row") if a.get("row") is not None else 0)
            c = int(a.get("col") or 0)
            if r >= len(tbl.rows) or c >= len(tbl.columns):
                raise ValueError(f"越界：表只有 {len(tbl.rows)} 行 {len(tbl.columns)} 列")
            val = a.get("value")
            tbl.cell(r, c).text = "" if val is None else str(val)
            d.save(out)
            return {"path": out, "inplace": inplace, "written": True,
                    "cell": [r, c], "value": val}

        if action == "delete":
            ti = int(a.get("index") if a.get("index") is not None else -1)
            if ti < 0 or ti >= len(d.tables):
                raise ValueError(f"没有第 {ti} 张表（共 {len(d.tables)} 张）")
            tbl = d.tables[ti]
            tbl._element.getparent().remove(tbl._element)
            d.save(out)
            return {"path": out, "inplace": inplace, "written": True,
                    "deleted": ti, "left": len(d.tables)}

        raise ValueError(f"不认这个 action：{action}（能 list/add/set/delete）")

    # ------------------------------------------------ word.style

    def _style(self, a):
        """改样式：字体 / 字号 / 加粗 / 颜色 / 对齐 / 行距 / 首行缩进。"""
        src = _require_file(a.get("path"))
        from docx import Document
        from docx.shared import Pt, RGBColor, Cm
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        out, inplace = _resolve_write(a, src, "改样式结果.docx")
        d = Document(src)

        # 选区：全部 / 指定段落序号 / 指定样式名 / 包含某关键词的段落
        idxs = a.get("paragraphs")
        style_name = a.get("style_name")
        keyword = a.get("contains")
        picked = []
        for i, para in enumerate(d.paragraphs):
            if idxs is not None:
                want = [int(x) for x in idxs] if isinstance(idxs, list) else [int(idxs)]
                if i not in want:
                    continue
            if style_name and (not para.style or para.style.name != style_name):
                continue
            if keyword and keyword not in para.text:
                continue
            picked.append(para)
        if not picked and (idxs is not None or style_name or keyword):
            raise ValueError("选区没命中任何段落（检查 paragraphs / style_name / contains）")

        align_map = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
                     "right": WD_ALIGN_PARAGRAPH.RIGHT, "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}
        changed = 0
        for para in picked:
            if a.get("align") and str(a["align"]).lower() in align_map:
                para.alignment = align_map[str(a["align"]).lower()]
            if a.get("line_spacing") is not None:
                para.paragraph_format.line_spacing = float(a["line_spacing"])
            if a.get("space_before") is not None:
                para.paragraph_format.space_before = Pt(float(a["space_before"]))
            if a.get("space_after") is not None:
                para.paragraph_format.space_after = Pt(float(a["space_after"]))
            if a.get("indent_first_cm") is not None:
                para.paragraph_format.first_line_indent = Cm(float(a["indent_first_cm"]))
            if a.get("new_page_before"):
                para.paragraph_format.page_break_before = True
            for run in para.runs:
                if a.get("font"):
                    run.font.name = str(a["font"])
                    try:
                        from docx.oxml.ns import qn
                        run._element.get_or_add_rPr().rFonts.set(
                            qn("w:eastAsia"), str(a["font"]))
                    except Exception:
                        pass
                if a.get("size"):
                    run.font.size = Pt(float(a["size"]))
                if a.get("bold") is not None:
                    run.font.bold = bool(a["bold"])
                if a.get("italic") is not None:
                    run.font.italic = bool(a["italic"])
                if a.get("underline") is not None:
                    run.font.underline = bool(a["underline"])
                if a.get("color"):
                    c = str(a["color"]).lstrip("#")
                    run.font.color.rgb = RGBColor.from_string(c.upper())
            changed += 1

        d.save(out)
        return {"path": out, "inplace": inplace, "written": True,
                "paragraphs_changed": changed,
                "applied": {k: v for k, v in a.items()
                            if k in ("font", "size", "bold", "italic", "underline",
                                     "color", "align", "line_spacing", "space_before",
                                     "space_after", "indent_first_cm")}}

    # ------------------------------------------------ word.header

    def _header(self, a):
        """页眉 / 页脚 / 页码。"""
        src = _require_file(a.get("path"))
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        out, inplace = _resolve_write(a, src, "页眉结果.docx")
        d = Document(src)
        where = str(a.get("where") or "header").lower()
        if where not in ("header", "footer"):
            raise ValueError("where 只能是 header / footer")

        sec_idx = int(a.get("section") or 0)
        if sec_idx >= len(d.sections):
            raise ValueError(f"只有 {len(d.sections)} 节")
        sec = d.sections[sec_idx]
        part = sec.header if where == "header" else sec.footer
        if a.get("clear"):
            for para in list(part.paragraphs)[1:]:
                para._element.getparent().remove(para._element)
            if part.paragraphs:
                part.paragraphs[0].text = ""

        p = part.paragraphs[0] if part.paragraphs else part.add_paragraph()
        if a.get("text"):
            p.text = ""
            run = p.add_run(str(a["text"]))
            if a.get("size"):
                from docx.shared import Pt
                run.font.size = Pt(float(a["size"]))
            if a.get("font"):
                run.font.name = str(a["font"])

        if a.get("page_number"):
            # 页码是域代码（PAGE），不是普通文字：插 fldSimple
            from docx.oxml.ns import qn
            from docx.oxml import OxmlElement
            p.text = ""
            if a.get("prefix"):
                p.add_run(str(a["prefix"]))
            fld = OxmlElement("w:fldSimple")
            fld.set(qn("w:instr"), "PAGE")
            r = OxmlElement("w:r")
            t = OxmlElement("w:t")
            t.text = "1"
            r.append(t)
            fld.append(r)
            p._p.append(fld)
            if a.get("suffix"):
                p.add_run(str(a["suffix"]))

        if a.get("align"):
            p.alignment = {"left": WD_ALIGN_PARAGRAPH.LEFT,
                           "center": WD_ALIGN_PARAGRAPH.CENTER,
                           "right": WD_ALIGN_PARAGRAPH.RIGHT}.get(
                               str(a["align"]).lower(), WD_ALIGN_PARAGRAPH.CENTER)

        d.save(out)
        return {"path": out, "inplace": inplace, "written": True,
                "where": where, "section": sec_idx,
                "text": p.text, "page_number": bool(a.get("page_number"))}

    # ------------------------------------------------ word.meta

    def _meta(self, a):
        """文档属性：标题 / 作者 / 主题 / 关键词 / 备注。"""
        src = _require_file(a.get("path"))
        from docx import Document

        d = Document(src)
        core = d.core_properties
        action = str(a.get("action") or "get").lower()

        if action == "get":
            return {"path": src, "title": core.title, "author": core.author,
                    "subject": core.subject, "keywords": core.keywords,
                    "comments": core.comments, "category": core.category,
                    "last_modified_by": core.last_modified_by,
                    "revision": core.revision,
                    "created": str(core.created) if core.created else None,
                    "modified": str(core.modified) if core.modified else None}

        out, inplace = _resolve_write(a, src, "改属性结果.docx")
        for key in ("title", "author", "subject", "keywords", "comments", "category"):
            if a.get(key) is not None:
                setattr(core, key, str(a[key]))
        core.modified = _dt.datetime.now()
        d.save(out)
        return {"path": out, "inplace": inplace, "written": True,
                "title": core.title, "author": core.author, "subject": core.subject}


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

    dev = WordOps()
    ok, why = dev.probe()
    if not ok:
        print("skip: " + why)
        return 0

    td = tempfile.mkdtemp(prefix="word_")
    try:
        # ============ word.write：从 Markdown 建文档 ============
        md_file = os.path.join(td, "报告.docx")
        md = "\n".join([
            "# 季度报告",
            "这是正文第一段，含**加粗**内容。",
            "## 第一节",
            "- 要点甲",
            "- 要点乙",
            "1. 第一条",
            "> 引用一句话",
        ])
        r = dev.invoke("word.write", {"out": md_file, "markdown": md,
                                      "title": "2026 Q3", "subtitle": "内部资料"})
        ck(r.ok, "write：从 Markdown 生成 %s" % (r.err if not r.ok else ""))
        ck(r.ok and os.path.getsize(md_file) > 3000, "write：文件真有内容")
        ck(r.ok and r.data["paragraphs"] >= 7, "write：段落数 >= 7")

        # ============ word.info ============
        r = dev.invoke("word.info", {"path": md_file})
        ck(r.ok, "info：能读")
        ck(r.ok and r.data["sections"] == 1, "info：1 节")
        ck(r.ok and r.data["chars"] > 30, "info：字数 > 30")
        ck(r.ok and r.data["images"] == 0, "info：没有图片")
        ck(r.ok and len(r.data["styles"]) > 0, "info：样式清单存在")

        # ============ word.read ============
        r = dev.invoke("word.read", {"path": md_file})
        ck(r.ok, "read：能读")
        ck(r.ok and r.data["returned"] > 0, "read：有块返回")
        ck(r.ok and "季度报告" in (r.data.get("text") or ""), "read：正文含标题")
        ck(r.ok and r.data["blocks"][0]["type"] == "para", "read：第一块是段落")

        r = dev.invoke("word.read", {"path": md_file, "mode": "markdown"})
        ck(r.ok and (r.data.get("markdown") or "").count("#") >= 2,
           "read：markdown 模式带标题井号")

        r = dev.invoke("word.read", {"path": md_file, "mode": "text", "limit": 2})
        ck(r.ok and r.data["returned"] == 2, "read：limit 生效")
        ck(r.ok and r.data["truncated"] is True, "read：truncated 如实标")

        # ============ word.append ============
        r = dev.invoke("word.append", {"path": md_file, "items": [
            {"type": "heading", "text": "追加的一节", "level": 1},
            {"type": "para", "text": "追加的正文"},
            {"type": "table", "rows": [["项目", "数量"], ["甲", 1], ["乙", 2]]},
            {"type": "pagebreak"},
        ]})
        ck(r.ok, "append：追加成功 %s" % (r.err if not r.ok else ""))
        ck(r.ok and r.data["added"]["table"] == 1, "append：加了 1 张表")
        ck(r.ok and r.data["added"]["heading"] == 1, "append：加了 1 个标题")
        ck(r.ok and r.data["added"]["pagebreak"] == 1, "append：加了分页符")
        ap_file = r.data["path"] if r.ok else md_file

        # ============ word.table ============
        r = dev.invoke("word.table", {"path": ap_file, "action": "list"})
        ck(r.ok and r.data["count"] == 1, "table：1 张表")
        ck(r.ok and r.data["tables"][0]["header"] == ["项目", "数量"], "table：表头对")
        ck(r.ok and r.data["tables"][0]["data"][1] == ["甲", "1"],
           "table：数字读成字符串（docx 里本来就是文本）")

        r = dev.invoke("word.table", {"path": ap_file, "action": "add",
                                      "rows": [["城市", "人口"], ["北京", 2189]],
                                      "out": os.path.join(td, "两表.docx")})
        ck(r.ok and r.data["table_index"] == 1, "table：add 到第 2 张")
        two_file = r.data["path"] if r.ok else ap_file

        r = dev.invoke("word.table", {"path": two_file, "action": "set",
                                      "index": 1, "row": 1, "col": 1, "value": 21893095,
                                      "out": os.path.join(td, "改格.docx")})
        ck(r.ok and str(r.data["value"]) == "21893095", "table：set 生效")
        r2 = dev.invoke("word.table", {"path": r.data["path"], "action": "list", "index": 1})
        ck(r2.ok and r2.data["tables"][0]["data"][1][1] == "21893095", "table：改后能读回")

        r = dev.invoke("word.table", {"path": two_file, "action": "set", "index": 9,
                                      "row": 0, "col": 0, "value": "x"})
        ck((not r.ok) and "没有第" in (r.err or ""), "table：越界表号报错")
        r = dev.invoke("word.table", {"path": two_file, "action": "set", "index": 1,
                                      "row": 99, "col": 0, "value": "x"})
        ck((not r.ok) and "越界" in (r.err or ""), "table：越界行列报错")

        # ============ word.style ============
        r = dev.invoke("word.style", {"path": md_file, "contains": "正文第一段",
                                      "font": "思源宋体", "size": 14, "bold": True,
                                      "out": os.path.join(td, "改样式.docx")})
        ck(r.ok and r.data["paragraphs_changed"] >= 1, "style：命中 1 段")
        ck(r.ok and r.data["applied"]["size"] == 14, "style：回显 size")
        r = dev.invoke("word.style", {"path": md_file, "contains": "不存在的词", "bold": True})
        ck((not r.ok) and "没命中" in (r.err or ""), "style：选区为空要报错而不是静默")

        # ============ word.header ============
        r = dev.invoke("word.header", {"path": md_file, "where": "header",
                                       "text": "内部资料 · 请勿外传",
                                       "out": os.path.join(td, "页眉.docx")})
        ck(r.ok and "请勿外传" in (r.data.get("text") or ""), "header：页眉写进去")
        r = dev.invoke("word.header", {"path": md_file, "where": "footer",
                                       "page_number": True, "prefix": "第 ", "suffix": " 页",
                                       "out": os.path.join(td, "页码.docx")})
        ck(r.ok and r.data["page_number"] is True, "header：页码域插入")
        r = dev.invoke("word.header", {"path": md_file, "where": "banner", "text": "x"})
        ck((not r.ok) and "header / footer" in (r.err or ""), "header：非法位置报错")

        # ============ word.meta ============
        r = dev.invoke("word.meta", {"path": md_file, "action": "get"})
        ck(r.ok and r.data["title"] == "2026 Q3", "meta：读回写时设的标题")
        r = dev.invoke("word.meta", {"path": md_file, "action": "set",
                                     "author": "领航员", "keywords": "报告,季度",
                                     "out": os.path.join(td, "属性.docx")})
        ck(r.ok and r.data["author"] == "领航员", "meta：作者写进去")
        r2 = dev.invoke("word.meta", {"path": r.data["path"], "action": "get"})
        ck(r2.ok and r2.data["keywords"] == "报告,季度", "meta：关键词写进去")

        # ============ word.placeholders：模板 ============
        from docx import Document
        tpl_file = os.path.join(td, "合同模板.docx")
        doc = Document()
        doc.add_paragraph("甲方：{{ party_a }}")
        doc.add_paragraph("乙方：{{ party_b }}")
        doc.add_paragraph("金额：{{ amount }} 元")
        doc.add_paragraph("{% if urgent %}【加急】{% endif %}")
        t = doc.add_table(rows=1, cols=3)
        t.rows[0].cells[0].text = "项目"
        t.rows[0].cells[1].text = "数量"
        t.rows[0].cells[2].text = "单价"
        rr1 = t.add_row()
        rr1.cells[0].text = "{%tr for it in items %}"
        rr2 = t.add_row()
        rr2.cells[0].text = "{{ it.name }}"
        rr2.cells[1].text = "{{ it.qty }}"
        rr2.cells[2].text = "{{ it.price }}"
        rr3 = t.add_row()
        rr3.cells[0].text = "{%tr endfor %}"
        doc.save(tpl_file)

        r = dev.invoke("word.placeholders", {"path": tpl_file})
        ck(r.ok and r.data["is_template"] is True, "ph：认得出是模板")
        ck(r.ok and set(["party_a", "party_b", "amount"]).issubset(
            set(r.data["need_keys"])), "ph：列出三个变量")
        ck(r.ok and any(lp["kind"] == "for" for lp in r.data["loops"]),
           "ph：认出 for 循环")

        r = dev.invoke("word.placeholders", {"path": md_file})
        ck(r.ok and r.data["is_template"] is False, "ph：成品文档不是模板")
        ck(r.ok and "hint" in r.data, "ph：非模板要给提示")

        # ============ word.fill：正常填充 + 表格循环 ============
        filled = os.path.join(td, "合同成品.docx")
        r = dev.invoke("word.fill", {"path": tpl_file, "out": filled, "data": {
            "party_a": "甲公司", "party_b": "乙公司", "amount": 88000,
            "urgent": True,
            "items": [{"name": "服务费", "qty": 1, "price": 80000},
                      {"name": "材料费", "qty": 2, "price": 4000}],
        }})
        ck(r.ok, "fill：填充成功 %s" % (r.err if not r.ok else ""))
        ck(r.ok and os.path.getsize(filled) > 3000, "fill：产物有内容")
        r2 = dev.invoke("word.read", {"path": filled})
        txt = (r2.data.get("text") or "") if r2.ok else ""
        ck("甲公司" in txt and "乙公司" in txt, "fill：变量被替换")
        ck("88000" in txt, "fill：数字原样写入")
        ck("加急" in txt, "fill：if 条件生效")
        ck("{{" not in txt, "fill：没有残留占位符")
        r3 = dev.invoke("word.table", {"path": filled, "action": "list"})
        ck(r3.ok and r3.data["count"] == 1, "fill：表还在")
        if r3.ok and r3.data["tables"]:
            rows = r3.data["tables"][0]["data"]
            ck(len(rows) == 3, "fill：表 1 表头 + 2 数据行（循环对）")
            ck(rows[1][0] == "服务费", "fill：循环首行内容")
            ck(rows[2][1] == "2", "fill：循环第二行数量")

        # ============ word.fill：缺变量必须报错（不是静默变空） ============
        missing_out = os.path.join(td, "缺变量.docx")
        r = dev.invoke("word.fill", {"path": tpl_file, "out": missing_out,
                                     "data": {"party_a": "甲"}})
        ck(not r.ok, "fill：缺变量要失败")
        ck((r.err or "").find("party_b") >= 0, "fill：错误里点名缺哪个变量")
        ck(not os.path.exists(missing_out), "fill：失败时不留半成品")

        # ============ word.fill：显式放行才允许留空 ============
        blank = os.path.join(td, "留空.docx")
        r = dev.invoke("word.fill", {"path": tpl_file, "out": blank,
                                     "data": {"party_a": "甲"}, "missing": "blank"})
        ck(r.ok, "fill：missing=blank 放行 %s" % (r.err if not r.ok else ""))
        r2 = dev.invoke("word.read", {"path": blank})
        ck(r2.ok and "甲方：甲" in (r2.data.get("text") or ""), "fill：放行后能出文件")

        # ============ word.fill：模板语法错误要给人话 ============
        bad_tpl = os.path.join(td, "坏模板.docx")
        doc = Document()
        doc.add_paragraph("{% if x %}没闭合")
        doc.save(bad_tpl)
        r = dev.invoke("word.fill", {"path": bad_tpl,
                                     "out": os.path.join(td, "坏.docx"),
                                     "data": {"x": 1}})
        ck(not r.ok, "fill：坏模板要失败")
        ck((r.err or "").find("语法") >= 0 or "TemplateSyntax" in (r.err or ""),
           "fill：说明是语法错")

        # ============ word.fill：图片 ============
        try:
            from PIL import Image
            img_path = os.path.join(td, "图.png")
            Image.new("RGB", (200, 100), (30, 120, 200)).save(img_path)
            img_tpl = os.path.join(td, "带图模板.docx")
            doc = Document()
            doc.add_paragraph("logo：")
            doc.add_paragraph("{{ logo }}")
            doc.save(img_tpl)
            r = dev.invoke("word.fill", {"path": img_tpl,
                                         "out": os.path.join(td, "带图成品.docx"),
                                         "data": {},
                                         "images": {"logo": {"path": img_path,
                                                             "width_mm": 40}}})
            ck(r.ok, "fill：插图片 %s" % (r.err if not r.ok else ""))
            if r.ok:
                r2 = dev.invoke("word.info", {"path": r.data["path"]})
                ck(r2.ok and r2.data["images"] >= 1, "fill：产物里数得到图片")
        except ImportError:
            ck(True, "fill：跳过图片（没 Pillow）")

        # ============ 错误路径 ============
        r = dev.invoke("word.info", {"path": os.path.join(td, "不存在.docx")})
        ck((not r.ok) and "文件不存在" in (r.err or ""), "错误：文件不存在")

        old_doc = os.path.join(td, "老格式.doc")
        with open(old_doc, "wb") as f:
            f.write(b"\xd0\xcf\x11\xe0" + b"\x00" * 100)
        r = dev.invoke("word.info", {"path": old_doc})
        ck((not r.ok) and "convert.file" in (r.err or ""), "错误：.doc 指引去转换")

        r = dev.invoke("word.write", {"out": os.path.join(td, "x.docm"), "text": "hi"})
        ck((not r.ok) and ".docx" in (r.err or ""), "错误：不写 docm")

        r = dev.invoke("word.write", {"out": os.path.join(td, "空.docx")})
        ck((not r.ok) and "至少要" in (r.err or ""), "错误：没内容要报错")

        # ============ 另存不覆盖：重名让号 ============
        a1 = os.path.join(td, "同名.docx")
        dev.invoke("word.write", {"out": a1, "text": "第一份"})
        r = dev.invoke("word.write", {"out": a1, "text": "第二份"})
        ck(r.ok and r.data["path"] != a1, "安全：重名自动让号不覆盖")
        ck(r.ok and os.path.exists(a1), "安全：原文件还在")

        # ============ inplace 第一次只预演 ============
        r = dev.invoke("word.meta", {"path": a1, "action": "set",
                                     "author": "改", "inplace": True})
        ck((not r.ok) and "_confirmed" in (r.err or ""), "安全：inplace 第一次只预演")
        r = dev.invoke("word.meta", {"path": a1, "action": "set",
                                     "author": "改", "inplace": True, "_confirmed": True})
        ck(r.ok and r.data["inplace"] is True, "安全：确认后真改")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    total = state["n"]
    got = total - len(fails)
    print("\nwordops selftest: %d/%d%s"
          % (got, total, "" if not fails else "（失败 %d）" % len(fails)))
    for f in fails:
        print("   失败:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法: python -m fleet.kinds.wordops --selftest")
    sys.exit(0)

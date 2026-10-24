"""md → 手机可读的单文件 HTML（tools/md2mob.py）。

为什么要有这个工具
------------------
文档在电脑上是 md，在手机上是「能滚、能看清、能看图表」才叫文档。
拿手机看 md 的原始文本，表格会散、代码会折、架构图直接糊掉。

为什么不做成通用 Markdown 引擎
------------------------------
不引 CommonMark 那类全量解析器（几百 KB + 一次全量 parse），
因为这批文档实际只用到七种语法：标题、表格、代码块、列表、引用、分隔线、内联样式。
真需要脚注/公式时再换，不要一开始为"完备"付代价。

**关键设计：md 里允许内嵌 HTML 块（比如 <svg> 架构图）原样透传。**
这样「图」和「文字」都只有一份真源，不存在 md 与 html 两边各改一遍然后漂移。
"""
from __future__ import annotations

import html
import os
import re
import sys

CSS = """
:root{
  --ink:#0E1116; --panel:#161B22; --panel2:#1E252E; --line:#2A323C;
  --cyan:#4DD0E1; --amber:#E8B44A; --red:#E05A5A; --green:#57C08A;
  --text:#E6EDF3; --dim:#8B98A5;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; padding:0 0 60px; background:var(--ink); color:var(--text);
  font:16px/1.72 -apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans CJK SC",
       "Microsoft YaHei",sans-serif;
  word-break:break-word; overflow-wrap:anywhere;
}
.wrap{max-width:760px;margin:0 auto;padding:0 16px}
header{
  position:sticky;top:0;z-index:9;background:rgba(14,17,22,.94);
  backdrop-filter:blur(8px);border-bottom:1px solid var(--line);
  padding:10px 16px;
}
header .t{font-size:14px;font-weight:600;color:var(--cyan);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
header .s{font-size:11px;color:var(--dim);margin-top:2px}
h1{font-size:23px;line-height:1.35;margin:26px 0 12px;font-weight:700;
   border-left:3px solid var(--cyan);padding-left:11px}
h2{font-size:19px;margin:32px 0 12px;font-weight:650;color:#fff;
   border-bottom:1px solid var(--line);padding-bottom:8px}
h3{font-size:16px;margin:22px 0 9px;color:var(--cyan);font-weight:600}
h4{font-size:14.5px;margin:16px 0 7px;color:var(--amber)}
p{margin:11px 0;color:var(--text)}
a{color:var(--cyan);text-decoration:none;border-bottom:1px dotted rgba(77,208,225,.5)}
strong{color:#fff;font-weight:650}
em{color:var(--amber);font-style:normal}
code{
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:.875em;background:rgba(255,255,255,.08);color:#A8E7F0;
  padding:1.5px 5px;border-radius:4px;
}
pre{
  background:#0A0D11;border:1px solid var(--line);border-radius:10px;
  padding:12px 13px;overflow-x:auto;margin:14px 0;
  -webkit-overflow-scrolling:touch;
}
pre code{background:none;color:#C9D6E0;font-size:12.5px;line-height:1.62;padding:0}
blockquote{
  margin:14px 0;padding:9px 13px;background:var(--panel);
  border-left:3px solid var(--amber);border-radius:0 8px 8px 0;color:var(--dim);
}
blockquote p{margin:4px 0;color:var(--dim)}
hr{border:0;border-top:1px solid var(--line);margin:26px 0}
ul,ol{margin:11px 0;padding-left:22px}
li{margin:5px 0}
li::marker{color:var(--cyan)}

/* 表格：手机上不硬塞，改成横向可滑 + 首列吸住 */
.tablewrap{overflow-x:auto;margin:14px 0;border:1px solid var(--line);
  border-radius:10px;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:480px}
th{background:var(--panel2);color:var(--cyan);font-weight:600;
   padding:9px 11px;text-align:left;white-space:nowrap;
   position:sticky;top:0;border-bottom:1px solid var(--line)}
td{padding:9px 11px;border-bottom:1px solid rgba(42,50,60,.65);vertical-align:top}
tr:last-child td{border-bottom:0}
tbody tr:nth-child(even){background:rgba(255,255,255,.017)}
td:first-child{white-space:nowrap;color:#C9D6E0}

/* 内嵌 SVG 图：宽度自适应，不裁切 */
.svgbox{margin:16px 0;background:var(--panel);border:1px solid var(--line);
  border-radius:12px;padding:10px;overflow-x:auto;-webkit-overflow-scrolling:touch}
.svgbox svg{display:block;width:100%;height:auto;min-width:320px}

details{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:10px 13px;margin:12px 0}
summary{cursor:pointer;font-weight:600;color:var(--cyan);font-size:14px}
details[open] summary{margin-bottom:8px}

.tag{display:inline-block;font-size:11px;font-family:ui-monospace,monospace;
  padding:1px 6px;border-radius:4px;background:var(--panel2);color:var(--dim)}
footer{margin-top:44px;padding-top:14px;border-top:1px solid var(--line);
  font-size:11.5px;color:var(--dim);text-align:center}
@media (prefers-color-scheme:light){
  /* 保持深色：这是给夜里看的，浅色下反而刺眼 */
}
"""


def inline(s: str) -> str:
    """内联样式：先把占位符挖出来，再做转义与替换，避免转义把生成的标签吃掉。"""
    codes = []

    def keep_code(m):
        codes.append(m.group(1))
        return "\x00C%d\x00" % (len(codes) - 1)

    s = re.sub(r"`([^`]+)`", keep_code, s)
    s = html.escape(s, quote=False)

    # 链接 [text](url)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
               r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    # 粗体 **x**
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    # 斜体 *x*（避开 ** 已处理）
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
    # 分隔符：行首的 · 保留
    for i, c in enumerate(codes):
        s = s.replace("\x00C%d\x00" % i, "<code>%s</code>" % html.escape(c))
    return s


def convert(md: str, title: str = "", subtitle: str = "") -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out = []
    i = 0
    in_code = False
    code_buf = []
    code_lang = ""
    list_open = None            # "ul" / "ol" / None
    raw_buf = []                # 裸 HTML 块（含 <svg>）

    def close_list():
        nonlocal list_open
        if list_open:
            out.append("</%s>" % list_open)
            list_open = None

    def flush_raw():
        if raw_buf:
            body = "\n".join(raw_buf)
            if "<svg" in body:
                out.append('<div class="svgbox">%s</div>' % body)
            else:
                out.append(body)
            raw_buf.clear()

    while i < len(lines):
        ln = lines[i]

        # ---- 代码块 ----
        if ln.strip().startswith("```"):
            if not in_code:
                in_code, code_lang, code_buf = True, ln.strip()[3:].strip(), []
            else:
                in_code = False
                esc = html.escape("\n".join(code_buf))
                cls = ' data-lang="%s"' % html.escape(code_lang) if code_lang else ""
                out.append("<pre%s><code>%s</code></pre>" % (cls, esc))
            i += 1
            continue
        if in_code:
            code_buf.append(ln)
            i += 1
            continue

        # ---- 裸 HTML 块：从 < 开头的行累积到闭合 ----
        st = ln.strip()
        if st.startswith("<") and not st.startswith("<http"):
            # 收集直到配对闭合（简单平衡计数）
            tag = re.match(r"<([a-zA-Z][\w-]*)", st)
            if tag:
                name = tag.group(1)
                depth = 0
                while i < len(lines):
                    cur = lines[i]
                    depth += len(re.findall(r"<%s[\s>]" % re.escape(name), cur))
                    depth -= len(re.findall(r"</%s>" % re.escape(name), cur))
                    raw_buf.append(cur)
                    i += 1
                    if depth <= 0:
                        break
                close_list()
                flush_raw()
                continue

        # ---- 空行 ----
        if not st:
            close_list()
            i += 1
            continue

        # ---- 分隔线 ----
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", st):
            close_list()
            out.append("<hr>")
            i += 1
            continue

        # ---- 标题 ----
        m = re.match(r"^(#{1,4})\s+(.*)$", st)
        if m:
            close_list()
            n, txt = len(m.group(1)), m.group(2).strip()
            out.append("<h%d>%s</h%d>" % (n, inline(txt), n))
            i += 1
            continue

        # ---- 表格 ----
        if "|" in ln and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:*-]+\|", lines[i + 1]):
            close_list()
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i]); i += 1
            cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
            head = cells[0]
            body = [r for r in cells[1:] if not all(re.fullmatch(r":?-{2,}:?", c or "-") for c in r)]
            t = ['<div class="tablewrap"><table><thead><tr>']
            t += ["<th>%s</th>" % inline(c) for c in head]
            t.append("</tr></thead><tbody>")
            for r in body:
                t.append("<tr>" + "".join("<td>%s</td>" % inline(c) for c in r) + "</tr>")
            t.append("</tbody></table></div>")
            out.append("".join(t))
            continue

        # ---- 引用 ----
        if st.startswith(">"):
            close_list()
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].strip()); i += 1
            out.append("<blockquote>%s</blockquote>"
                       % "".join("<p>%s</p>" % inline(b) for b in buf if b))
            continue

        # ---- 列表 ----
        mu = re.match(r"^\s*[-*+]\s+(.*)$", ln)
        mo = re.match(r"^\s*\d+[.)]\s+(.*)$", ln)
        if mu or mo:
            want = "ul" if mu else "ol"
            if list_open != want:
                close_list()
                out.append("<%s>" % want)
                list_open = want
            out.append("<li>%s</li>" % inline((mu or mo).group(1)))
            i += 1
            continue

        # ---- 普通段落 ----
        close_list()
        buf = []
        while i < len(lines) and lines[i].strip() and not re.match(
                r"^(#{1,4}\s|```|>|\s*[-*+]\s|\s*\d+[.)]\s)", lines[i].strip()):
            if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", lines[i].strip()):
                break
            buf.append(lines[i].strip())
            i += 1
        if buf:
            out.append("<p>%s</p>" % inline(" ".join(buf)))

    close_list()
    flush_raw()

    head = ""
    if title:
        head = ('<header><div class="t">%s</div>%s</header>'
                % (html.escape(title),
                   '<div class="s">%s</div>' % html.escape(subtitle) if subtitle else ""))
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="theme-color" content="#0E1116">'
        "<title>%s</title><style>%s</style></head><body>%s"
        '<div class="wrap">%s</div>'
        '<footer>天网 · 手机版文档 · 由 md2mob 生成</footer>'
        "</body></html>"
    ) % (html.escape(title or "文档"), CSS, head, "\n".join(out))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("用法：python -m tools.md2mob <input.md> [output.html] [--title 标题]")
        return 2
    src = argv[0]
    dst = argv[1] if len(argv) > 1 and not argv[1].startswith("--") else \
        os.path.splitext(src)[0] + ".html"
    title = ""
    if "--title" in argv:
        title = argv[argv.index("--title") + 1]
    with open(src, encoding="utf-8") as f:
        md = f.read()
    if not title:
        m = re.search(r"^#\s+(.*)$", md, re.M)
        title = m.group(1).strip() if m else os.path.basename(src)
    out = convert(md, title=title, subtitle="天网 · 融合计划书")
    with open(dst, "w", encoding="utf-8") as f:
        f.write(out)
    print("已生成：%s（%.1f KB）" % (dst, os.path.getsize(dst) / 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

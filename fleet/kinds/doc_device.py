"""文档设备：把「各种格式的本地文件 → 可读文本」接进总线（按需转换，不预建索引）。

为什么用它而不是自己写解析（主人 2026-11-03）：
  「PDF 这个的话，你尽量去找开源项目，不要去直接硬造……能搞 PDF 啊各种格式的
    开源项目，把它能力接进来。」
  自造 PDF 解析是重复劳动且质量差（字体编码、双栏、表格全是坑）。选型：
  **Microsoft MarkItDown**（MIT，纯本地，CPU 即可）——一个入口吃 PDF / DOCX /
  PPTX / XLSX / HTML / CSV / JSON / EPUB / 图片元数据，统一转 Markdown。
  比它重的（docling / MinerU / marker）都要拉模型或吃 GPU，不符合本机 6GB 的现实。

🔴 边界（对齐主人的「按需解析」）：
  只做**单文件、调用时才转**。不扫描全库、不落索引、不缓存正文——
  论文库 500+ 篇如果全转一遍要几十分钟且占几十 MB，而真正要读的永远只是当下那几篇。

诚实边界：
  - 扫描版 PDF（图片）没有文字层，MarkItDown 抽不出正文 → 如实返回空并说明；
  - 依赖缺失时（没装 markitdown）cap 仍注册，invoke 返回可读的安装提示，不静默失败。
"""
import os

from ..core import BaseDevice, DeviceResult as R

#: 单文件大小上限：超过就拒（防止误传巨型文件把进程拖死）
MAX_BYTES = 200 * 1024 * 1024
#: 允许的扩展名（MarkItDown 覆盖的常见格式；不给的格式明确拒绝而不是猜）
EXTS = (".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".xls", ".html", ".htm",
        ".csv", ".tsv", ".json", ".xml", ".epub", ".txt", ".md", ".ipynb")


class DocDevice(BaseDevice):
    kind = "doclib"
    via = "local"

    def __init__(self, name="doc:convert", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "data")
        self._md = None

    def _engine(self):
        """懒加载 MarkItDown（导入本身要几百毫秒，别拖慢注册表构建）。"""
        if self._md is None:
            from markitdown import MarkItDown
            self._md = MarkItDown()
        return self._md

    # ---------------- 设备契约 ----------------

    def probe(self):
        try:
            import markitdown  # noqa: F401
        except Exception:
            return False, "没装 markitdown：pip install 'markitdown[pdf,docx,pptx,xlsx]'"
        return True, ""

    def caps(self):
        return ("doc.convert", "doc.read", "doc.formats")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "doc.convert":
            return self._convert(args, markdown=True)
        if cap == "doc.read":
            return self._convert(args, markdown=False)
        if cap == "doc.formats":
            return self._formats()
        return super().invoke(cap, args)

    # ---------------- 能力 ----------------

    def _formats(self):
        try:
            self._engine()
            ver = __import__("markitdown").__version__
        except Exception as e:
            return R.unavailable("doc.formats", "markitdown 不可用: %s" % e,
                                 device=self.name, via=self.via)
        return R.success("doc.formats", self.name, via=self.via, data={
            "engine": "MarkItDown %s" % ver,
            "license": "MIT",
            "exts": list(EXTS),
            "note": "按需转换，不建索引不缓存；扫描版 PDF 无文字层时返回空",
        })

    def _convert(self, args, markdown=True):
        path = (args.get("path") or "").strip().strip('"')
        max_chars = max(1000, min(int(args.get("max_chars") or 40000), 400000))
        if not path:
            return R.error("doc.convert", "缺 path（本地文件路径）", device=self.name, via=self.via)
        p = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(p):
            return R.error("doc.convert", "文件不存在: %s" % p, device=self.name, via=self.via)
        ext = os.path.splitext(p)[1].lower()
        if ext not in EXTS:
            return R.error("doc.convert", "不支持的扩展名 %s（支持 %s）" % (ext, ", ".join(EXTS)),
                           device=self.name, via=self.via)
        size = os.path.getsize(p)
        if size > MAX_BYTES:
            return R.error("doc.convert", "文件太大 %d MB（上限 %d MB）"
                           % (size // 1048576, MAX_BYTES // 1048576),
                           device=self.name, via=self.via)
        try:
            res = self._engine().convert(p)
        except Exception as e:
            return R.error("doc.convert", "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)
        text = res.text_content or ""
        cap = "doc.convert" if markdown else "doc.read"
        if not markdown:
            text = _strip_md(text)
        return R.success(cap, self.name, via=self.via, data={
            "path": p,
            "ext": ext,
            "bytes": size,
            "chars": len(text),
            "truncated": len(text) > max_chars,
            "text": text[:max_chars],
            "note": "" if text.strip() else "抽不到文字：可能是扫描版 PDF（无文字层）",
        })


def _strip_md(md):
    """把 Markdown 粗坯压成纯文本（够用即可：去标题井号/强调/图片/链接语法）。"""
    import re
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"^#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"[*_`]{1,3}", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

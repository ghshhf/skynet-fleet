"""图表层：把数据画成图，写进文件。

为什么单独一台（2026-11-06）：
  `sheet` 能算数字，但人看报表时先看形状——哪个月掉了、哪个区占大头、
  分布是偏的还是双峰。这些用一张表说半天，画出来一眼就完了。
  本机 matplotlib 早就装了，缺的只是把它接进总线。

🔴 两条不同的「图」，别混（这是我上一版计划犯错的地方）：
  · **表内原生图**（想让人改数就跟着变）→ 走 `sheet` 域的 openpyxl.chart，
    它写的是 Excel 的图表对象，不是图片。本设备**不管这个**。
  · **报告图**（要贴进文档、发给别人看）→ 走这里，渲染成 PNG。
  拿图片塞进 Excel 也能看，但数据一改图就废了，那是退而求其次。

边界（诚实写在前面）：
  · **只出 PNG**。SVG/PDF 要换后端，现在没做，别假装能。
  · **不做交互**。这是静态图，要缩放/悬浮提示得走前端那套。
  · **不猜数据含义**。哪列是 x、哪列是系列，按位置约定；猜错了会画出垃圾，
    所以每个能力都写清了「第几列是什么」，不靠启发式。
  · **中文字体靠系统**。找不到就退回默认并用方框显示，同时 `font_used` 如实
    报 `default`——不静默糊过去。

铁律：默认**另存不覆盖**，重名自动让号（`_1`、`_2`）。
"""
import csv
import os

from ..core import BaseDevice, DeviceResult as R

DEFAULT_FONTS = ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC",
                 "Source Han Sans SC", "WenQuanYi Zen Hei")

_PLT = None
_FONT = None
_FONT_CHECKED = False


def _mpl():
    """延迟导入 matplotlib，并把后端钉死成 Agg。

    为什么钉死：这台机器上跑没有窗口，默认后端会去连显示器，一 import 就崩。
    必须在 import pyplot **之前**设，之后再设没用。
    """
    global _PLT
    if _PLT is None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        _PLT = plt
    return _PLT


def _pick_font():
    """挑一个系统里真有的中文字体。挑不到就返回 None，别硬写一个不存在的名字。"""
    global _FONT, _FONT_CHECKED
    if _FONT_CHECKED:
        return _FONT
    _FONT_CHECKED = True
    try:
        from matplotlib import font_manager
        have = {f.name for f in font_manager.fontManager.ttflist}
        for name in DEFAULT_FONTS:
            if name in have:
                _FONT = name
                break
    except Exception:                                    # noqa: BLE001
        _FONT = None
    return _FONT


def _unique(path):
    """不覆盖：`a.png` 存在就让成 `a_1.png`。返回实际能用的路径。"""
    if not path:
        return path
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while os.path.exists("%s_%d%s" % (stem, i, ext)):
        i += 1
    return "%s_%d%s" % (stem, i, ext)


def _num(v):
    """把 CSV 里的 "1,234.5" 这类字符串也认成数字。认不出返回 None。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("，", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _load(a):
    """统一取数：二维数组 / csv / tsv。返回 (headers, rows)。

    行里保留原值（可能是字符串），要不要转数字交给具体图去决定——
    柱状图的 x 轴可以是文字，硬转成数字反而错。
    """
    data = a.get("data")
    if isinstance(data, str):                            # 当成文件路径
        p = data
        if not os.path.exists(p):
            raise ValueError("数据文件不存在：%s" % p)
        delim = "\t" if p.lower().endswith((".tsv", ".tab")) else ","
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            rows = [r for r in csv.reader(f, delimiter=delim) if r and any(x.strip() for x in r)]
    elif isinstance(data, list) and data:
        rows = [list(r) for r in data]
    else:
        raise ValueError("缺 data：给二维数组（第一行是表头），或 csv/tsv 路径")

    if len(rows) < 2:
        raise ValueError("数据只有 %d 行，画不了：至少 1 行表头 + 1 行数据" % len(rows))

    headers = [str(x) for x in rows[0]]
    body = rows[1:]
    width = max(len(headers), max(len(r) for r in body))
    headers += ["列%d" % (i + 1) for i in range(len(headers), width)]
    return headers, body


def _fig(a, nrows=1, ncols=1):
    """按像素尺寸开画布。matplotlib 的单位是英寸，所以要用 dpi 换算。"""
    plt = _mpl()
    w = int(a.get("width") or 1280)
    h = int(a.get("height") or 720)
    dpi = int(a.get("dpi") or 100)
    font = _pick_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font] + list(plt.rcParams["font.sans-serif"])
    plt.rcParams["axes.unicode_minus"] = False           # 负号别显示成方框
    fig, ax = plt.subplots(nrows=nrows, ncols=ncols, figsize=(w / dpi, h / dpi), dpi=dpi)
    return fig, ax, w, h, dpi, (font or "default")


def _save(fig, out, a):
    """存盘并关图。关图不是礼貌，是防泄漏：循环画 100 张不关会吃光内存。"""
    plt = _mpl()
    if not out:
        raise ValueError("缺 out：要给一个 .png 路径")
    out = os.path.abspath(out)
    if not out.lower().endswith(".png"):
        raise ValueError("只出 PNG：out 要以 .png 结尾（收到 %r）" % os.path.basename(out))
    d = os.path.dirname(out)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    real = _unique(out)
    fig.tight_layout()
    fig.savefig(real)
    plt.close(fig)
    return real


class ChartOps(BaseDevice):
    kind = "chartops"
    via = "local"

    def __init__(self, name="chart:ops", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "media")

    def probe(self):
        try:
            _mpl()
        except ImportError:
            return False, "没装 matplotlib（pip install matplotlib）"
        except Exception as exc:                         # noqa: BLE001
            return False, "matplotlib 起不来：%s" % exc
        return True, ""

    def caps(self):
        return ("chart.bar", "chart.line", "chart.pie",
                "chart.scatter", "chart.hist")

    def invoke(self, cap, args=None):
        a = args or {}
        fn = {
            "chart.bar": self._bar,
            "chart.line": self._line,
            "chart.pie": self._pie,
            "chart.scatter": self._scatter,
            "chart.hist": self._hist,
        }.get(cap)
        if fn is None:
            return R.error(cap, "未知能力 %r" % cap, device=self.name)
        ok, why = self.probe()
        if not ok:
            return R.unavailable(cap, why, device=self.name)
        try:
            data = fn(a)
        except ValueError as exc:
            return R.error(cap, str(exc), device=self.name)
        except Exception as exc:                         # noqa: BLE001
            return R.error(cap, "%s: %s" % (type(exc).__name__, exc), device=self.name)
        return R.success(cap, device=self.name, data=data, side_effect=True)

    # ------------------------------------------------------------ 柱状
    def _bar(self, a):
        headers, rows = _load(a)
        labels = [str(r[0]) for r in rows]
        series = []
        for ci in range(1, len(headers)):
            vals = [_num(r[ci]) if ci < len(r) else None for r in rows]
            if any(v is not None for v in vals):
                series.append((headers[ci], [v if v is not None else 0.0 for v in vals]))
        if not series:
            raise ValueError("没有可画的数值列：第 1 列当分类标签，第 2 列起要数字")

        fig, ax, w, h, dpi, font = _fig(a)
        n = len(series)
        width = 0.8 / n
        idx = range(len(labels))
        for i, (label, vals) in enumerate(series):
            ax.bar([x + (i - (n - 1) / 2) * width for x in idx], vals,
                   width=width, label=label)
        ax.set_xticks(list(idx))
        ax.set_xticklabels(labels, rotation=a.get("rotate", 0) or 0,
                           ha="right" if a.get("rotate") else "center")
        ax.set_xlabel(a.get("xlabel") or "")
        ax.set_ylabel(a.get("ylabel") or "")
        ax.set_title(a.get("title") or "")
        if n > 1:
            ax.legend()
        if a.get("grid", True):
            ax.grid(axis="y", alpha=0.3)
        real = _save(fig, a.get("out"), a)
        return {"path": real, "width": w, "height": h, "dpi": dpi,
                "points": len(labels) * n, "series": [s[0] for s in series],
                "font_used": font, "kind": "bar"}

    # ------------------------------------------------------------ 折线
    def _line(self, a):
        headers, rows = _load(a)
        labels = [str(r[0]) for r in rows]
        series = []
        for ci in range(1, len(headers)):
            vals = [_num(r[ci]) if ci < len(r) else None for r in rows]
            if any(v is not None for v in vals):
                series.append((headers[ci], [v if v is not None else float("nan") for v in vals]))
        if not series:
            raise ValueError("没有可画的数值列：第 1 列当 x 轴，第 2 列起要数字")

        fig, ax, w, h, dpi, font = _fig(a)
        for label, vals in series:
            ax.plot(range(len(labels)), vals, marker=a.get("marker") or "o",
                    linewidth=1.8, label=label)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=a.get("rotate", 0) or 0,
                           ha="right" if a.get("rotate") else "center")
        ax.set_xlabel(a.get("xlabel") or "")
        ax.set_ylabel(a.get("ylabel") or "")
        ax.set_title(a.get("title") or "")
        if len(series) > 1:
            ax.legend()
        if a.get("grid", True):
            ax.grid(alpha=0.3)
        real = _save(fig, a.get("out"), a)
        return {"path": real, "width": w, "height": h, "dpi": dpi,
                "points": len(labels) * len(series),
                "series": [s[0] for s in series], "font_used": font, "kind": "line"}

    # ------------------------------------------------------------ 饼图
    def _pie(self, a):
        headers, rows = _load(a)
        if len(headers) < 2:
            raise ValueError("饼图要两列：第 1 列是名称，第 2 列是数值")
        labels, vals = [], []
        for r in rows:
            v = _num(r[1]) if len(r) > 1 else None
            if v is None:
                continue
            labels.append(str(r[0]))
            vals.append(v)
        if not vals:
            raise ValueError("第 2 列没有数字，饼图没法画")
        if any(v < 0 for v in vals):
            raise ValueError("饼图不能有负数：占比图只对非负值有意义，改用 chart.bar")
        total = sum(vals)
        if total <= 0:
            raise ValueError("数值合计为 0，画出来是空饼")

        fig, ax, w, h, dpi, font = _fig(a)
        wedges, _texts, autotexts = ax.pie(
            vals, labels=labels, autopct=a.get("format") or "%1.1f%%",
            startangle=a.get("startangle", 90))
        if a.get("show_values"):
            for t, v in zip(autotexts, vals):
                t.set_text("%s\n%s" % (t.get_text(), ("%g" % v)))
        ax.set_title(a.get("title") or "")
        ax.axis("equal")                                 # 不设就是椭圆
        real = _save(fig, a.get("out"), a)
        return {"path": real, "width": w, "height": h, "dpi": dpi,
                "points": len(labels), "total": total,
                "font_used": font, "kind": "pie"}

    # ------------------------------------------------------------ 散点
    def _scatter(self, a):
        headers, rows = _load(a)
        xs, ys, cs = [], [], []
        color_on = a.get("color_col")
        for r in rows:
            x = _num(r[0]) if len(r) > 0 else None
            y = _num(r[1]) if len(r) > 1 else None
            if x is None or y is None:
                continue
            xs.append(x)
            ys.append(y)
            if color_on is not None and len(r) > int(color_on):
                cs.append(_num(r[int(color_on)]) or 0.0)
        if not xs:
            raise ValueError("散点要两列数字：第 1 列 x，第 2 列 y（都不是数字的会被跳过）")
        if len(xs) < 2:
            raise ValueError("只有 %d 个有效点，散点图看不出关系" % len(xs))

        fig, ax, w, h, dpi, font = _fig(a)
        kw = {"s": a.get("size", 36), "alpha": a.get("alpha", 0.75)}
        if cs:
            kw["c"] = cs
            kw["cmap"] = "viridis"
        ax.scatter(xs, ys, **kw)
        ax.set_xlabel(a.get("xlabel") or headers[0])
        ax.set_ylabel(a.get("ylabel") or headers[1])
        ax.set_title(a.get("title") or "")
        if a.get("grid", True):
            ax.grid(alpha=0.3)
        real = _save(fig, a.get("out"), a)
        return {"path": real, "width": w, "height": h, "dpi": dpi,
                "points": len(xs), "font_used": font, "kind": "scatter"}

    # ------------------------------------------------------------ 直方
    def _hist(self, a):
        headers, rows = _load(a)
        cols = a.get("columns")
        if cols:
            idxs = [int(c) for c in cols]
        else:
            idxs = [i for i in range(len(headers))
                    if sum(1 for r in rows if i < len(r) and _num(r[i]) is not None) >= 2]
        if not idxs:
            raise ValueError("没有一列够画直方（每列至少要 2 个数字）")

        fig, ax, w, h, dpi, font = _fig(a)
        bins = int(a.get("bins") or 20)
        plotted = []
        for i in idxs:
            vals = [v for v in (_num(r[i]) if i < len(r) else None for r in rows)
                    if v is not None]
            if len(vals) < 2:
                continue
            ax.hist(vals, bins=bins, alpha=a.get("alpha", 0.6),
                    label=headers[i], edgecolor="white", linewidth=0.6)
            plotted.append(headers[i])
        if not plotted:
            raise ValueError("选中的列里没有能画的数据")
        ax.set_xlabel(a.get("xlabel") or "")
        ax.set_ylabel(a.get("ylabel") or "频数")
        ax.set_title(a.get("title") or "")
        if len(plotted) > 1:
            ax.legend()
        if a.get("grid", True):
            ax.grid(axis="y", alpha=0.3)
        real = _save(fig, a.get("out"), a)
        return {"path": real, "width": w, "height": h, "dpi": dpi,
                "points": len(rows), "series": plotted, "bins": bins,
                "font_used": font, "kind": "hist"}


def _selftest():
    import shutil
    import tempfile

    state = {"n": 0}
    fails = []

    def ck(cond, label):
        state["n"] += 1
        if not cond:
            fails.append(label)

    dev = ChartOps()
    ok, why = dev.probe()
    if not ok:
        print("skip: " + why)
        return 0

    td = tempfile.mkdtemp(prefix="chart_")
    try:
        ck(len(dev.caps()) == 5, "caps：5 项")
        ck(all(c.startswith("chart.") for c in dev.caps()), "caps：全是 chart. 前缀")

        data = [["月份", "北区", "南区"],
                ["一月", 120, 80],
                ["二月", 150, "95"],
                ["三月", 90, 110]]

        # ---- bar ----
        out = os.path.join(td, "柱.png")
        r = dev.invoke("chart.bar", {"data": data, "out": out, "title": "季度对比"})
        ck(r.ok, "bar：能画 %s" % (r.err if not r.ok else ""))
        if r.ok:
            ck(os.path.getsize(r.data["path"]) > 2000, "bar：文件真有内容")
            ck(r.data["points"] == 6, "bar：6 个点（3 类 × 2 系列）")
            ck(r.data["series"] == ["北区", "南区"], "bar：系列名取表头")
            ck(r.data["kind"] == "bar", "bar：kind 对")
            ck(r.data["width"] == 1280 and r.data["height"] == 720, "bar：默认尺寸")
            ck(r.data["font_used"] != "", "bar：报了字体（没有也好歹说 default）")

        # ---- 不覆盖 ----
        r2 = dev.invoke("chart.bar", {"data": data, "out": out})
        ck(r2.ok and r2.data["path"] != out, "安全：重名自动让号")
        ck(os.path.exists(out), "安全：原文件还在")
        r3 = dev.invoke("chart.bar", {"data": data, "out": out})
        ck(r3.ok and r3.data["path"].endswith("_2.png"), "安全：第二次让成 _2")

        # ---- line ----
        r = dev.invoke("chart.line", {"data": data, "out": os.path.join(td, "线.png"),
                                      "width": 800, "height": 600})
        ck(r.ok, "line：能画 %s" % (r.err if not r.ok else ""))
        if r.ok:
            ck(r.data["width"] == 800 and r.data["height"] == 600, "line：自定义尺寸生效")
            ck(r.data["points"] == 6, "line：6 个点")

        # ---- pie ----
        pie_data = [["区域", "份额"], ["华北", 40], ["华东", "35"], ["华南", 25]]
        r = dev.invoke("chart.pie", {"data": pie_data, "out": os.path.join(td, "饼.png")})
        ck(r.ok, "pie：能画 %s" % (r.err if not r.ok else ""))
        if r.ok:
            ck(r.data["total"] == 100, "pie：合计 100（字符串 35 也认了）")
            ck(r.data["points"] == 3, "pie：3 块")

        r = dev.invoke("chart.pie", {"data": [["a", "b"], ["x", -5], ["y", 10]],
                                     "out": os.path.join(td, "负饼.png")})
        ck(not r.ok and "负数" in (r.err or ""), "pie：负数要拒绝并说原因")

        r = dev.invoke("chart.pie", {"data": [["a", "b"], ["x", 0], ["y", 0]],
                                     "out": os.path.join(td, "空饼.png")})
        ck(not r.ok and "0" in (r.err or ""), "pie：合计 0 要拒绝")

        # ---- scatter ----
        sc = [["x", "y"], [1, 2], [2, 4.5], [3, 6], [4, 8.2], [5, 11]]
        r = dev.invoke("chart.scatter", {"data": sc, "out": os.path.join(td, "散.png")})
        ck(r.ok, "scatter：能画 %s" % (r.err if not r.ok else ""))
        if r.ok:
            ck(r.data["points"] == 5, "scatter：5 个点")

        r = dev.invoke("chart.scatter", {"data": [["x", "y"], ["甲", "乙"], ["丙", "丁"]],
                                         "out": os.path.join(td, "散坏.png")})
        ck(not r.ok and "数字" in (r.err or ""), "scatter：全非数字要拒绝")

        # ---- hist ----
        hs = [["成绩"], [88], [92], [75], ["85"], [60], [95], [78], [82]]
        r = dev.invoke("chart.hist", {"data": hs, "out": os.path.join(td, "直.png"), "bins": 5})
        ck(r.ok, "hist：能画 %s" % (r.err if not r.ok else ""))
        if r.ok:
            ck(r.data["bins"] == 5, "hist：bins 生效")

        # ---- CSV 输入 ----
        csv_path = os.path.join(td, "数据.csv")
        with open(csv_path, "w", encoding="utf-8-sig") as f:
            f.write("季度,收入\nQ1,100\nQ2,150\n")
        r = dev.invoke("chart.bar", {"data": csv_path, "out": os.path.join(td, "csv柱.png")})
        ck(r.ok, "bar：能吃 csv 文件 %s" % (r.err if not r.ok else ""))
        if r.ok:
            ck(r.data["points"] == 2, "bar：csv 里 2 行")

        # ---- 错误路径 ----
        r = dev.invoke("chart.bar", {"data": data, "out": os.path.join(td, "x.jpg")})
        ck(not r.ok and "PNG" in (r.err or ""), "错误：非 png 后缀要拒绝")

        r = dev.invoke("chart.bar", {"data": data})
        ck(not r.ok and "out" in (r.err or ""), "错误：缺 out 要报错")

        r = dev.invoke("chart.bar", {"out": os.path.join(td, "无数据.png")})
        ck(not r.ok and "data" in (r.err or ""), "错误：缺 data 要报错")

        r = dev.invoke("chart.bar", {"data": [["只有表头"]], "out": os.path.join(td, "少.png")})
        ck(not r.ok and "行" in (r.err or ""), "错误：只有表头要拒绝")

        r = dev.invoke("chart.bar", {"data": os.path.join(td, "不存在.csv"),
                                     "out": os.path.join(td, "x.png")})
        ck(not r.ok and "不存在" in (r.err or ""), "错误：文件不存在要点明")

        r = dev.invoke("chart.bar", {"data": [["a", "b"], ["一", "非数"]],
                                     "out": os.path.join(td, "无数值.png")})
        ck(not r.ok and "数值" in (r.err or ""), "错误：没有数值列要拒绝")

        r = dev.invoke("chart.unknown")
        ck(not r.ok, "错误：未知能力")

        # ---- 子目录自动建 ----
        deep = os.path.join(td, "a", "b", "深.png")
        r = dev.invoke("chart.bar", {"data": data, "out": deep})
        ck(r.ok and os.path.exists(deep), "out：父目录不存在会自动建")

        # ---- 连续 20 张不泄漏（关图生效的话不会报 warning）----
        okn = 0
        for i in range(20):
            rr = dev.invoke("chart.line", {"data": data,
                                           "out": os.path.join(td, "loop%d.png" % i)})
            okn += 1 if rr.ok else 0
        ck(okn == 20, "批量：连续 20 张全成功（关图没漏）")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    total = state["n"]
    got = total - len(fails)
    print("\nchartops selftest: %d/%d%s"
          % (got, total, "" if not fails else "（失败 %d）" % len(fails)))
    for f in fails:
        print("   失败:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法: python -m fleet.kinds.chartops --selftest")
    sys.exit(0)

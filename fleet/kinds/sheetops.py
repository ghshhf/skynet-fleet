"""表格层：Excel 的结构化读写。

为什么单独一台（2026-11-06）：
  `doc.convert` 把 xlsx 抽成 Markdown 文本，那是**读**的一条死路——抽出来就回不去了。
  真正每天要用的是**改**：给一张表加一列表头、把三个月的表堆成一张、按 key 拼两张表、
  把公式导出来看看谁引用了谁、再导成 CSV 交给别的工具。
  openpyxl 35 万行/秒的读速够用，本机早就装了（3.1.5），缺的只是把它接进总线。

边界（诚实写在前面）：
  · **不算公式**。openpyxl 只存公式字符串，不跑计算引擎；`data_only=True` 读的是
    **Excel 上次保存时缓存的值**。用 openpyxl 生成、从没被 Excel 打开过的文件，
    缓存值是空的——这时 `sheet.formula` 只有 `cached` 会返回 null，如实说没算过。
  · **不读 .xls**（BIFF 老格式）。openpyxl 不支持，需要 xlrd；这里明确拒绝而不是猜。
    要老格式先走 `convert.file` 转成 xlsx。
  · **不保留 VBA**。xlsm 能被读，但另存会丢宏——所以默认拒绝写 xlsm，除非显式 `allow_macro_loss`。
  · **样式是「设」不是「保」**：读出来的样式信息只有字体名/字号/颜色/加粗这些常用的，
    主题色、条件格式、数据透视表不动（openpyxl 一保存就可能把它们弄丢）。

铁律：默认**另存不覆盖**。要原地改必须 `inplace=true`，且第一次只预演，加 `_confirmed=true` 才写。
"""
import csv
import datetime as _dt
import json
import os
import re

from ..core import BaseDevice, DeviceResult as R

#: 单次读/写允许触碰的单元格上限（超过就要求显式给 range）
MAX_CELLS = 400_000
#: 一次返回给上层的最大行数（再多就该分页了）
MAX_ROWS_RETURN = 5_000
#: 自动列宽的宽度上限（防止一句话把整张表撑爆）
MAX_COL_WIDTH = 60

#: openpyxl 能读的扩展名
READ_EXTS = (".xlsx", ".xlsm", ".xltx", ".xltm")
WRITE_EXTS = (".xlsx",)


# ---------------------------------------------------------------- 小工具

def _norm_path(p, field="path"):
    """Windows 路径归一到 Python 能吃的形式（`/e/x` → `E:/x`）。"""
    if not p:
        raise ValueError(f"缺 {field}")
    p = str(p).strip().strip('"').replace("\\", "/")
    m = re.match(r"^/([a-zA-Z])/", p)
    if m:
        p = m.group(1).upper() + ":/" + p[3:]
    return os.path.abspath(os.path.expanduser(p))


def _require_file(p):
    if not os.path.isfile(p):
        raise ValueError(f"文件不存在：{p}")
    ext = os.path.splitext(p)[1].lower()
    if ext not in READ_EXTS:
        if ext == ".xls":
            raise ValueError("openpyxl 读不了 .xls 老格式；先 convert.file 转成 .xlsx 再来")
        if ext == ".csv":
            raise ValueError("这是 CSV，不是工作簿；用 text.csv 或 sheet.write 的 csv 入参")
        raise ValueError(f"不是 Excel 工作簿（{ext or '无扩展名'}）；只认 "
                         + " / ".join(READ_EXTS))
    return p


def _col_letter(n):
    """1 → A, 27 → AA。"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _col_index(letters):
    """A → 1, AA → 27。"""
    n = 0
    for ch in letters.upper():
        if not ("A" <= ch <= "Z"):
            raise ValueError(f"列名不合法：{letters}")
        n = n * 26 + (ord(ch) - 64)
    return n


def _parse_cell(ref):
    """'B7' → (7, 2)。"""
    m = re.match(r"^\$?([A-Za-z]{1,3})\$?(\d+)$", str(ref).strip())
    if not m:
        raise ValueError(f"单元格地址不合法：{ref}")
    return int(m.group(2)), _col_index(m.group(1))


def _parse_range(text):
    """'A1:C10' → (r1, c1, r2, c2)；单格 'B7' → (7,2,7,2)。"""
    t = str(text).strip()
    if ":" in t:
        a, b = t.split(":", 1)
        r1, c1 = _parse_cell(a)
        r2, c2 = _parse_cell(b)
        return min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2)
    r, c = _parse_cell(t)
    return r, c, r, c


def _clean(v):
    """openpyxl 的值 → JSON 友好的值。"""
    if v is None:
        return None
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, _dt.timedelta):
        return v.total_seconds()
    if isinstance(v, float):
        if v != v:            # NaN
            return None
        if v in (float("inf"), float("-inf")):
            return str(v)
        if v.is_integer() and abs(v) < 1e15:
            return int(v)
        return round(v, 10)
    if isinstance(v, (int, bool, str)):
        return v
    return str(v)


def _disp_width(s):
    """显示宽度：CJK 算 2 格。openpyxl 的列宽单位≈一个 ASCII 字符。"""
    n = 0
    for ch in str(s):
        n += 2 if ord(ch) > 0x2E7F else 1
    return n


def _norm_color(c):
    c = str(c).strip().lstrip("#").upper()
    if len(c) == 6:
        c = "FF" + c
    if not re.match(r"^[0-9A-F]{8}$", c):
        raise ValueError(f"颜色要 #RRGGBB 或 AARRGGBB：{c}")
    return c


def _sheet_of(wb, name):
    if not name:
        return wb[wb.sheetnames[0]]
    if name not in wb.sheetnames:
        raise ValueError(f"没有这张表：{name}；现有 " + " / ".join(wb.sheetnames))
    return wb[name]


def _guard(rows, cols, where):
    if rows * cols > MAX_CELLS:
        raise ValueError(
            f"{where} 有 {rows} 行 × {cols} 列 ≈ {rows * cols} 格，超过单次上限 "
            f"{MAX_CELLS}；用 range 或 offset/limit 分段取")


def _infer_type(values):
    """一列值的类型推断（按优先级从强到弱）。"""
    seen = [v for v in values if v is not None and v != ""]
    if not seen:
        return "empty"
    kinds = set()
    for v in seen:
        if isinstance(v, bool):
            kinds.add("bool")
        elif isinstance(v, int):
            kinds.add("int")
        elif isinstance(v, float):
            kinds.add("float")
        elif isinstance(v, (int, float)):
            kinds.add("number")
        else:
            s = str(v).strip()
            if re.match(r"^-?\d+$", s):
                kinds.add("int")
            elif re.match(r"^-?\d+\.\d+$", s):
                kinds.add("float")
            else:
                kinds.add("text")
    if kinds <= {"int"}:
        return "int"
    if kinds <= {"int", "float", "number"}:
        return "float"
    if kinds == {"bool"}:
        return "bool"
    return "mixed" if len(kinds) > 1 else "text"


def _as_number(v):
    """能当数字看就返回数字，否则 None。

    从 CSV 灌进来的数全是字符串（"90"），若直接按 isinstance 过滤，
    整列会被判成「没有数字」而不给统计——那正是最需要统计的场景。
    这里只认纯数字形态，带千分位的也放过；"0012" 这种保留前导零的不动。
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if re.match(r"^-?\d+$", s) and not re.match(r"^-?0\d", s):
            return int(s)
        if re.match(r"^-?\d+\.\d+$", s):
            return float(s)
    return None


def _numeric_summary(col):
    nums = [n for n in (_as_number(v) for v in col) if n is not None]
    if not nums:
        return None
    return {"count": len(nums), "sum": round(sum(nums), 10),
            "mean": round(sum(nums) / len(nums), 10),
            "min": min(nums), "max": max(nums)}


def _load(path, data_only=False, read_only=False):
    from openpyxl import load_workbook
    try:
        return load_workbook(path, data_only=data_only, read_only=read_only)
    except Exception as e:
        raise ValueError(f"打不开这个工作簿：{e}")


def _read_table(ws, header_row=None, max_rows=MAX_ROWS_RETURN,
                min_row=1, min_col=1, max_col=None, max_row=None):
    """把一张表读成 (headers, rows, bounds)。header_row=None 表示没有表头。"""
    mr = max_row or ws.max_row
    mc = max_col or ws.max_column
    if mr < 1 or mc < 1:
        return ([], [], (0, 0, 0, 0))
    _guard(mr - min_row + 1, mc - min_col + 1, "这张表")
    grid = []
    for row in ws.iter_rows(min_row=min_row, max_row=mr,
                            min_col=min_col, max_col=mc, values_only=True):
        grid.append([_clean(v) for v in row])
    if not grid:
        return ([], [], (0, 0, 0, 0))

    headers = []
    body = grid
    if header_row:
        idx = header_row - min_row
        if 0 <= idx < len(grid):
            raw = grid[idx]
            headers = [str(c) if c is not None else f"列{i + 1}"
                       for i, c in enumerate(raw)]
            body = grid[idx + 1:]
    truncated = len(body) > max_rows
    if truncated:
        body = body[:max_rows]
    return (headers, body, (mr, mc, len(body), 1 if truncated else 0))


def _autofit(ws, min_col=1, max_col=None, sample=200):
    max_col = max_col or ws.max_column
    widths = {}
    for i, row in enumerate(ws.iter_rows(min_row=1, max_row=min(sample, ws.max_row),
                                         min_col=min_col, max_col=max_col,
                                         values_only=True), 1):
        for j, v in enumerate(row):
            if v is None:
                continue
            w = _disp_width(v)
            col = min_col + j
            if w > widths.get(col, 0):
                widths[col] = w
    for col, w in widths.items():
        ws.column_dimensions[_col_letter(col)].width = min(w + 2, MAX_COL_WIDTH)


def _to_matrix(data):
    """把各种「表」的输入统一成二维 list。"""
    if data is None:
        return None
    if isinstance(data, dict) and "rows" in data:
        data = data["rows"]
    if not isinstance(data, (list, tuple)):
        raise ValueError("data 要是二维数组（[[...],[...]]）")
    out = []
    for row in data:
        if isinstance(row, dict):
            out.append(row)
        elif isinstance(row, (list, tuple)):
            out.append(list(row))
        else:
            out.append([row])
    return out


def _rows_from_csv(path, encoding=None, delimiter=None):
    from .textops import _text_score  # 复用已有的编码打分，别写第二套
    raw = open(path, "rb").read()
    enc = encoding
    if not enc:
        best, best_score = "utf-8", -1.0
        for cand in ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16", "utf-16-le"):
            try:
                txt = raw.decode(cand)
            except Exception:
                continue
            score = _text_score(txt)
            if score > best_score:
                best, best_score = cand, score
        enc = best
    text = raw.decode(enc, errors="replace")
    if delimiter in (None, "", "auto"):
        first = text.splitlines()[0] if text else ""
        delimiter = max([",", ";", "\t", "|"], key=lambda d: first.count(d))
    return [row for row in csv.reader(text.splitlines(), delimiter=delimiter)]


# ---------------------------------------------------------------- 设备

class SheetOps(BaseDevice):
    kind = "sheetops"
    via = "local"

    def __init__(self, name="sheet:ops", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "document")

    def probe(self):
        try:
            import openpyxl  # noqa: F401
        except Exception:
            return False, "没装 openpyxl：pip install openpyxl"
        return True, ""

    def caps(self):
        return ("sheet.info", "sheet.read", "sheet.write", "sheet.edit",
                "sheet.append", "sheet.style", "sheet.formula", "sheet.sheets",
                "sheet.merge", "sheet.export")

    # ------------------------------------------------ 分派

    def invoke(self, cap, args=None):
        args = args or {}
        fn = {
            "sheet.info": self._info,
            "sheet.read": self._read,
            "sheet.write": self._write,
            "sheet.edit": self._edit,
            "sheet.append": self._append,
            "sheet.style": self._style,
            "sheet.formula": self._formula,
            "sheet.sheets": self._sheets,
            "sheet.merge": self._merge,
            "sheet.export": self._export,
        }.get(cap)
        if fn is None:
            return R.unsupported(cap, f"{self.name} 没有 {cap}",
                                             device=self.name, via=self.via)
        try:
            data = fn(args)
        except ValueError as e:
            return R.error(cap, str(e), device=self.name, via=self.via)
        except Exception as e:                                    # noqa: BLE001
            return R.error(cap, f"{type(e).__name__}: {e}",
                                        device=self.name, via=self.via)
        return R.success(cap, device=self.name, data=data,
                                      side_effect=cap not in ("sheet.info", "sheet.read",
                                                              "sheet.formula", "sheet.sheets",
                                                              "sheet.export"),
                                      via=self.via)

    # ------------------------------------------------ sheet.info

    def _info(self, a):
        p = _require_file(_norm_path(a.get("path")))
        wb = _load(p)
        out = []
        for name in wb.sheetnames:
            ws = wb[name]
            formulas, last = 0, None
            for row in ws.iter_rows(max_row=min(ws.max_row, 3000),
                                    max_col=min(ws.max_column, 60),
                                    values_only=True):
                for v in row:
                    if v is None:
                        continue
                    last = v
                    if isinstance(v, str) and v.startswith("="):
                        formulas += 1
            header = None
            for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 5),
                                    max_col=min(ws.max_column, 30), values_only=True):
                vals = [v for v in row if v is not None and str(v).strip() != ""]
                if vals:
                    header = [_clean(v) for v in vals]
                    break
            out.append({
                "name": name,
                "rows": ws.max_row,
                "cols": ws.max_column,
                "dimensions": ws.calculate_dimension(),
                "merged": len(ws.merged_cells.ranges),
                "formulas_sampled": formulas,
                "frozen": str(ws.freeze_panes) if ws.freeze_panes else None,
                "hidden": ws.sheet_state != "visible",
                "header_guess": header,
            })
        return {"path": p, "sheets": out, "count": len(out),
                "active": wb.active.title,
                "note": "rows/cols 是 openpyxl 记录的已用范围，含只有格式没内容的格子"}

    # ------------------------------------------------ sheet.read

    def _read(self, a):
        p = _require_file(_norm_path(a.get("path")))
        use_formula = bool(a.get("formula"))
        wb = _load(p, data_only=not use_formula)
        ws = _sheet_of(wb, a.get("sheet"))

        rng = a.get("range")
        if rng:
            r1, c1, r2, c2 = _parse_range(rng)
        else:
            r1, c1 = int(a.get("offset") or 0) + 1, 1
            if a.get("offset"):
                r1 = int(a["offset"]) + 1
            r2 = min(ws.max_row, r1 + int(a.get("limit") or MAX_ROWS_RETURN) - 1)
            c2 = ws.max_column
        limit = int(a.get("limit") or MAX_ROWS_RETURN)
        if r2 - r1 + 1 > limit:
            r2 = r1 + limit - 1

        header_row = a.get("header_row")
        if header_row in ("auto", True):
            header_row = r1
        header_row = int(header_row) if header_row not in (None, False) else None

        headers, rows, (mr, mc, n, trunc) = _read_table(
            ws, header_row=header_row, max_rows=limit, min_row=r1, min_col=c1,
            max_col=c2, max_row=r2)

        cols = []
        ncol = max([len(r) for r in rows] + [len(headers)] + [0])
        for j in range(ncol):
            col = [r[j] if j < len(r) else None for r in rows]
            desc = {"index": j, "letter": _col_letter(c1 + j),
                    "name": headers[j] if j < len(headers) else None,
                    "type": _infer_type(col),
                    "nulls": sum(1 for v in col if v in (None, ""))}
            if a.get("stats"):
                s = _numeric_summary(col)
                if s:
                    desc["stats"] = s
            cols.append(desc)

        return {"path": p, "sheet": ws.title, "range": f"{_col_letter(c1)}{r1}:"
                                                       f"{_col_letter(c2)}{r2}",
                "headers": headers, "rows": rows, "returned": n,
                "truncated": bool(trunc), "columns": cols,
                "mode": "formula" if use_formula else "value",
                "note": ("" if use_formula else
                         "没被 Excel 保存过的文件，公式格的缓存值是空的")}

    # ------------------------------------------------ 写：公共落盘

    def _save(self, wb, src, a, cap):
        """统一出口：决定写到哪儿，返回 (路径, 预演标记)。

        默认另存：`out` 给了就用它，没给就在源文件旁边加后缀。
        原地改必须 inplace=true，且要 _confirmed=true。
        """
        inplace = bool(a.get("inplace"))
        if inplace:
            if src is None:
                raise ValueError("inplace=true 但没有源文件（新建表请给 out）")
            if not a.get("_confirmed"):
                raise ValueError("inplace=true 会覆盖源文件；第一次只预演。"
                                 "确认真要覆盖就再加 _confirmed=true")
            return src, False
        out = a.get("out")
        if not out:
            base, ext = os.path.splitext(src or "新表.xlsx")
            out = f"{base}.{cap}{ext}"
        out = _norm_path(out, "out")
        ext = os.path.splitext(out)[1].lower()
        if ext not in WRITE_EXTS:
            if ext in (".xlsm", ".xltm") and not a.get("allow_macro_loss"):
                raise ValueError("另存成 xlsm 会丢宏；确认可以丢就加 allow_macro_loss=true")
            if ext != ".xlsx":
                raise ValueError(f"只能写成 .xlsx（给的是 {ext or '无扩展名'}）")
        if os.path.exists(out) and not a.get("overwrite"):
            stem, e = os.path.splitext(out)
            i = 1
            while os.path.exists(f"{stem}_{i}{e}"):
                i += 1
            out = f"{stem}_{i}{e}"
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        wb.save(out)
        return out, False

    # ------------------------------------------------ sheet.write

    def _write(self, a):
        from openpyxl import Workbook
        rows = _to_matrix(a.get("data"))
        if rows is None and a.get("csv"):
            rows = _rows_from_csv(_norm_path(a["csv"], "csv"), a.get("encoding"),
                                  a.get("delimiter"))
        if rows is None and a.get("json"):
            raw = open(_norm_path(a["json"], "json"), "rb").read().decode("utf-8-sig")
            obj = json.loads(raw)
            rows = _to_matrix(obj)
        if rows is None:
            raise ValueError("得给 data（二维数组）/ csv / json 三者之一")
        if not rows:
            raise ValueError("data 是空的，没东西可写")

        header = a.get("headers")
        if header in (True, "auto"):
            header = [str(v) if v is not None else f"列{i + 1}" for i, v in enumerate(rows[0])]
            rows = rows[1:]
        elif isinstance(header, (list, tuple)):
            header = [str(h) for h in header]
        else:
            header = None

        wb = Workbook()
        ws = wb.active
        ws.title = (a.get("sheet") or "Sheet1")[:31]

        r = 1
        if header:
            for j, h in enumerate(header, 1):
                ws.cell(row=1, column=j, value=h)
            r = 2
        ncol = max([len(x) for x in rows] + [len(header or [])] + [1])
        for row in rows:
            for j in range(ncol):
                v = row[j] if j < len(row) else None
                if v is None:
                    continue
                if isinstance(v, dict):
                    v = json.dumps(v, ensure_ascii=False)
                try:
                    ws.cell(row=r, column=j + 1, value=v)
                except Exception:
                    ws.cell(row=r, column=j + 1, value=str(v))
            r += 1

        if header and a.get("style_header", True):
            self._paint_header(ws, 1, 1, ncol)
        if a.get("autofit", True):
            _autofit(ws, 1, ncol)
        if a.get("freeze") in (True, "auto"):
            ws.freeze_panes = "A2" if header else "A1"
        elif isinstance(a.get("freeze"), str):
            ws.freeze_panes = a["freeze"]

        out, _ = self._save(wb, None, a, "written")
        return {"path": out, "rows": len(rows), "cols": ncol,
                "header": header, "bytes": os.path.getsize(out)}

    @staticmethod
    def _paint_header(ws, row, c1, c2, fill="305496", color="FFFFFF"):
        from openpyxl.styles import Alignment, Font, PatternFill
        for c in range(c1, c2 + 1):
            cell = ws.cell(row=row, column=c)
            cell.font = Font(bold=True, color=color)
            cell.fill = PatternFill("solid", fgColor=fill)
            cell.alignment = Alignment(horizontal="center", vertical="center")

    # ------------------------------------------------ sheet.edit

    def _edit(self, a):
        p = _require_file(_norm_path(a.get("path")))
        wb = _load(p)
        ws = _sheet_of(wb, a.get("sheet"))
        done = []

        if a.get("set") is not None:
            value = a["set"]
            targets = a.get("cells") or a.get("range") or a.get("cell")
            if not targets:
                raise ValueError("set 要给 cell 或 range（要写哪些格）")
            if isinstance(targets, str) and ":" not in targets and not a.get("range"):
                rr, cc = _parse_cell(targets)
                ws.cell(row=rr, column=cc, value=value)
                done.append(f"{targets}={value!r}")
            else:
                r1, c1, r2, c2 = _parse_range(targets)
                n = 0
                for rr in range(r1, r2 + 1):
                    for cc in range(c1, c2 + 1):
                        ws.cell(row=rr, column=cc, value=value)
                        n += 1
                done.append(f"{targets} ← {value!r} × {n} 格")

        if a.get("clear") is not None:
            r1, c1, r2, c2 = _parse_range(a["clear"])
            n = 0
            for rr in range(r1, r2 + 1):
                for cc in range(c1, c2 + 1):
                    if ws.cell(row=rr, column=cc).value is not None:
                        ws.cell(row=rr, column=cc).value = None
                        n += 1
            done.append(f"清空 {a['clear']} 共 {n} 格")

        if a.get("insert_rows"):
            spec = a["insert_rows"]
            idx, amount = (spec if isinstance(spec, (list, tuple)) else [spec, 1])
            ws.insert_rows(int(idx), int(amount) or 1)
            done.append(f"第 {idx} 行前插 {amount or 1} 行")
        if a.get("delete_rows"):
            spec = a["delete_rows"]
            idx, amount = (spec if isinstance(spec, (list, tuple)) else [spec, 1])
            ws.delete_rows(int(idx), int(amount) or 1)
            done.append(f"删第 {idx} 行起 {amount or 1} 行")
        if a.get("insert_cols"):
            spec = a["insert_cols"]
            idx, amount = (spec if isinstance(spec, (list, tuple)) else [spec, 1])
            ws.insert_cols(int(idx), int(amount) or 1)
            done.append(f"第 {idx} 列前插 {amount or 1} 列")
        if a.get("delete_cols"):
            spec = a["delete_cols"]
            idx, amount = (spec if isinstance(spec, (list, tuple)) else [spec, 1])
            ws.delete_cols(int(idx), int(amount) or 1)
            done.append(f"删第 {idx} 列起 {amount or 1} 列")

        if not done:
            raise ValueError("没给动作：set / clear / insert_rows / delete_rows / "
                             "insert_cols / delete_cols 至少一个")
        out, _ = self._save(wb, p, a, "edited")
        return {"path": out, "sheet": ws.title, "changes": done}

    # ------------------------------------------------ sheet.append

    def _append(self, a):
        p = _require_file(_norm_path(a.get("path")))
        rows = _to_matrix(a.get("rows") or a.get("data"))
        if not rows:
            raise ValueError("rows 是空的")
        wb = _load(p)
        ws = _sheet_of(wb, a.get("sheet"))
        start = ws.max_row + 1
        ncol = max(len(r) for r in rows)
        for i, row in enumerate(rows):
            for j in range(ncol):
                v = row[j] if j < len(row) else None
                if v is not None:
                    ws.cell(row=start + i, column=j + 1, value=v)
        out, _ = self._save(wb, p, a, "appended")
        return {"path": out, "sheet": ws.title, "appended": len(rows),
                "from_row": start, "to_row": start + len(rows) - 1}

    # ------------------------------------------------ sheet.style

    def _style(self, a):
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        p = _require_file(_norm_path(a.get("path")))
        wb = _load(p)
        ws = _sheet_of(wb, a.get("sheet"))
        rng = a.get("range")
        if not rng:
            raise ValueError("缺 range（要美化哪一块，比如 A1:F1）")
        r1, c1, r2, c2 = _parse_range(rng)

        font_kw, fill_kw, align_kw, border_kw = {}, None, {}, None
        if a.get("bold") is not None:
            font_kw["bold"] = bool(a["bold"])
        if a.get("italic") is not None:
            font_kw["italic"] = bool(a["italic"])
        if a.get("size"):
            font_kw["size"] = float(a["size"])
        if a.get("font"):
            font_kw["name"] = str(a["font"])
        if a.get("color"):
            font_kw["color"] = _norm_color(a["color"])
        if a.get("fill"):
            fill_kw = PatternFill("solid", fgColor=_norm_color(a["fill"]))
        if a.get("align"):
            align_kw["horizontal"] = a["align"]
        if a.get("wrap") is not None:
            align_kw["wrap_text"] = bool(a["wrap"])
        if a.get("valign"):
            align_kw["vertical"] = a["valign"]
        if a.get("border"):
            side = Side(style=a.get("border_style") or "thin",
                        color=_norm_color(a.get("border_color") or "BFBFBF"))
            border_kw = Border(left=side, right=side, top=side, bottom=side)

        touched = 0
        for rr in range(r1, r2 + 1):
            for cc in range(c1, c2 + 1):
                cell = ws.cell(row=rr, column=cc)
                if font_kw:
                    cell.font = Font(**{**self._font_dict(cell.font), **font_kw})
                if fill_kw is not None:
                    cell.fill = fill_kw
                if align_kw:
                    cur = cell.alignment
                    cell.alignment = Alignment(
                        horizontal=align_kw.get("horizontal", cur.horizontal),
                        vertical=align_kw.get("vertical", cur.vertical),
                        wrap_text=align_kw.get("wrap_text", cur.wrap_text))
                if border_kw is not None:
                    cell.border = border_kw
                if a.get("number_format"):
                    cell.number_format = a["number_format"]
                touched += 1

        if a.get("width"):
            for cc in range(c1, c2 + 1):
                ws.column_dimensions[_col_letter(cc)].width = float(a["width"])
        if a.get("autofit"):
            _autofit(ws, c1, c2)
        if a.get("row_height"):
            for rr in range(r1, r2 + 1):
                ws.row_dimensions[rr].height = float(a["row_height"])

        out, _ = self._save(wb, p, a, "styled")
        return {"path": out, "range": a["range"], "cells": touched}

    @staticmethod
    def _font_dict(f):
        d = {}
        for k in ("name", "size", "bold", "italic"):
            v = getattr(f, k, None)
            if v is not None:
                d[k] = v
        return d

    # ------------------------------------------------ sheet.formula

    def _formula(self, a):
        action = (a.get("action") or "list").lower()
        p = _require_file(_norm_path(a.get("path")))

        if action == "list":
            wb = _load(p)
            ws = _sheet_of(wb, a.get("sheet"))
            found = []
            for row in ws.iter_rows(max_row=min(ws.max_row, 20_000),
                                    max_col=min(ws.max_column, 100)):
                for cell in row:
                    v = cell.value
                    if isinstance(v, str) and v.startswith("="):
                        found.append({"cell": cell.coordinate, "formula": v[1:],
                                      "refs": sorted(set(re.findall(
                                          r"\$?[A-Z]{1,3}\$?\d+", v)))})
            return {"path": p, "sheet": ws.title, "count": len(found),
                    "formulas": found[:500],
                    "truncated": len(found) > 500}

        if action == "cached":
            wb = _load(p, data_only=True)
            ws = _sheet_of(wb, a.get("sheet"))
            rng = a.get("range")
            if rng:
                r1, c1, r2, c2 = _parse_range(rng)
            else:
                r1, c1, r2, c2 = 1, 1, min(ws.max_row, 200), min(ws.max_column, 30)
            vals, blanks = [], 0
            for rr in range(r1, r2 + 1):
                line = []
                for cc in range(c1, c2 + 1):
                    v = _clean(ws.cell(row=rr, column=cc).value)
                    if v is None:
                        blanks += 1
                    line.append(v)
                vals.append(line)
            return {"path": p, "sheet": ws.title, "values": vals,
                    "nulls": blanks,
                    "note": "读的是 Excel 上次保存时的缓存值；从没被 Excel 保存过就是空的"}

        if action == "trace":
            target = a.get("cell")
            if not target:
                raise ValueError("trace 要给 cell（看谁引用了它）")
            wb = _load(p)
            ws = _sheet_of(wb, a.get("sheet"))
            r, c = _parse_cell(target)
            letter = _col_letter(c)
            pat = re.compile(rf"(?<![A-Z]){re.escape(target.lstrip('$'))}"
                             rf"(?!\d)", re.I)
            users = []
            for row in ws.iter_rows(max_row=min(ws.max_row, 20_000),
                                    max_col=min(ws.max_column, 100)):
                for cell in row:
                    v = cell.value
                    if isinstance(v, str) and v.startswith("=") and pat.search(v):
                        users.append({"cell": cell.coordinate, "formula": v[1:]})
            return {"path": p, "sheet": ws.title, "target": target.upper(),
                    "referenced_by": users, "count": len(users),
                    "column": letter}

        if action == "set":
            f = a.get("formula")
            cells = a.get("cell") or a.get("range")
            if not f or not cells:
                raise ValueError("set 要给 cell/range 和 formula")
            wb = _load(p)
            ws = _sheet_of(wb, a.get("sheet"))
            expr = f if f.startswith("=") else "=" + f
            if ":" in str(cells):
                r1, c1, r2, c2 = _parse_range(cells)
                n = 0
                for rr in range(r1, r2 + 1):
                    for cc in range(c1, c2 + 1):
                        ws.cell(row=rr, column=cc, value=expr)
                        n += 1
            else:
                rr, cc = _parse_cell(cells)
                ws.cell(row=rr, column=cc, value=expr)
                n = 1
            out, _ = self._save(wb, p, a, "formula")
            return {"path": out, "cells": n, "formula": expr,
                    "note": "只写了公式字符串；数值要 Excel/计算器打开才算得出来"}

        raise ValueError(f"不认识的 action：{action}（list / cached / trace / set）")

    # ------------------------------------------------ sheet.sheets

    def _sheets(self, a):
        from copy import copy
        action = (a.get("action") or "list").lower()
        p = _require_file(_norm_path(a.get("path")))
        wb = _load(p)

        if action == "list":
            return {"path": p, "sheets": [
                {"index": i, "name": n, "state": wb[n].sheet_state,
                 "rows": wb[n].max_row, "cols": wb[n].max_column}
                for i, n in enumerate(wb.sheetnames)]}

        if action == "add":
            name = a.get("name")
            if not name:
                raise ValueError("add 要给 name")
            if name in wb.sheetnames:
                raise ValueError(f"表已存在：{name}")
            ws = wb.create_sheet(name[:31], a.get("index"))
            if a.get("from_file"):
                raise ValueError("跨文件复制用 sheet.merge（mode=stack）")
            out, _ = self._save(wb, p, a, "sheets")
            return {"path": out, "added": ws.title, "sheets": wb.sheetnames}

        if action in ("delete", "remove"):
            name = a.get("name")
            if not name:
                raise ValueError("delete 要给 name")
            if name not in wb.sheetnames:
                raise ValueError(f"没有这张表：{name}")
            if len(wb.sheetnames) == 1:
                raise ValueError("只剩一张表，删了工作簿就空了；要清内容用 sheet.edit clear")
            wb.remove(wb[name])
            out, _ = self._save(wb, p, a, "sheets")
            return {"path": out, "deleted": name, "sheets": wb.sheetnames}

        if action == "rename":
            old, new = a.get("name"), a.get("to")
            if not old or not new:
                raise ValueError("rename 要给 name（原名）和 to（新名）")
            if old not in wb.sheetnames:
                raise ValueError(f"没有这张表：{old}")
            if new in wb.sheetnames:
                raise ValueError(f"新名字已被占用：{new}")
            wb[old].title = new[:31]
            out, _ = self._save(wb, p, a, "sheets")
            return {"path": out, "from": old, "to": wb.sheetnames[
                wb.sheetnames.index(new[:31])], "sheets": wb.sheetnames}

        if action == "copy":
            src = a.get("name")
            if not src or src not in wb.sheetnames:
                raise ValueError(f"copy 要给存在的 name；现有 " + " / ".join(wb.sheetnames))
            new = wb.copy_worksheet(wb[src])
            if a.get("to"):
                new.title = str(a["to"])[:31]
            out, _ = self._save(wb, p, a, "sheets")
            return {"path": out, "copied_from": src, "to": new.title,
                    "sheets": wb.sheetnames}

        if action == "move":
            name = a.get("name")
            idx = a.get("index")
            if not name or idx is None:
                raise ValueError("move 要给 name 和 index")
            ws = wb[name]
            wb.move_sheet(ws, offset=int(idx) - wb.sheetnames.index(name))
            out, _ = self._save(wb, p, a, "sheets")
            return {"path": out, "sheets": wb.sheetnames}

        if action in ("hide", "unhide"):
            name = a.get("name")
            if not name:
                raise ValueError(f"{action} 要给 name")
            wb[name].sheet_state = "hidden" if action == "hide" else "visible"
            out, _ = self._save(wb, p, a, "sheets")
            return {"path": out, "sheet": name, "state": wb[name].sheet_state}

        raise ValueError(f"不认识的 action：{action}（list/add/delete/rename/"
                         f"copy/move/hide/unhide）")

    # ------------------------------------------------ sheet.merge

    def _merge(self, a):
        mode = (a.get("mode") or "stack").lower()
        sources = a.get("sources")
        if not sources or not isinstance(sources, (list, tuple)):
            raise ValueError("sources 要给列表：[{path, sheet, header_row}, ...]")

        frames = []
        for s in sources:
            if isinstance(s, str):
                s = {"path": s}
            p = _require_file(_norm_path(s.get("path")))
            wb = _load(p, data_only=not s.get("formula"))
            ws = _sheet_of(wb, s.get("sheet"))
            hr = s.get("header_row", 1)
            if hr in (None, 0, False):
                hr = None
            headers, rows, _b = _read_table(ws, header_row=hr,
                                            max_rows=s.get("limit") or MAX_ROWS_RETURN)
            frames.append({"path": p, "sheet": ws.title,
                           "headers": headers, "rows": rows,
                           "side": s.get("side")})

        if mode == "stack":
            all_cols, seen = [], set()
            for f in frames:
                for h in f["headers"]:
                    if h not in seen:
                        seen.add(h)
                        all_cols.append(h)
            if not all_cols:
                raise ValueError("这几个源都没识别出表头；确认 header_row 对，"
                                 "或先用 sheet.write 补表头")
            merged = []
            for f in frames:
                pos = {h: i for i, h in enumerate(f["headers"])}
                for row in f["rows"]:
                    merged.append([row[pos[h]] if h in pos and pos[h] < len(row) else None
                                   for h in all_cols])
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = (a.get("sheet") or "合并")[:31]
            for j, h in enumerate(all_cols, 1):
                ws.cell(row=1, column=j, value=h)
            for i, row in enumerate(merged, 2):
                for j, v in enumerate(row, 1):
                    if v is not None:
                        ws.cell(row=i, column=j, value=v)
            self._paint_header(ws, 1, 1, len(all_cols))
            if a.get("autofit", True):
                _autofit(ws, 1, len(all_cols))
            ws.freeze_panes = "A2"
            out, _ = self._save(wb, None, a, "merged")
            return {"path": out, "mode": mode, "rows": len(merged),
                    "cols": len(all_cols), "columns": all_cols,
                    "from": [{"path": f["path"], "sheet": f["sheet"],
                              "rows": len(f["rows"])} for f in frames]}

        if mode == "join":
            if len(frames) != 2:
                raise ValueError("join 模式需要正好两个源")
            key = a.get("key")
            if not key:
                raise ValueError("join 要给 key（按哪一列拼）")
            left, right = frames
            if key not in left["headers"]:
                raise ValueError(f"左边没有 {key} 这列；有 " + " / ".join(left["headers"]))
            if key not in right["headers"]:
                raise ValueError(f"右边没有 {key} 这列；有 " + " / ".join(right["headers"]))
            how = (a.get("how") or "left").lower()
            li, ri = left["headers"].index(key), right["headers"].index(key)
            rmap = {}
            for row in right["rows"]:
                if ri < len(row) and row[ri] is not None:
                    rmap.setdefault(str(row[ri]), row)
            cols = list(left["headers"]) + [h for h in right["headers"] if h != key]
            out_rows, hit = [], 0
            for row in left["rows"]:
                k = str(row[li]) if li < len(row) else ""
                rrow = rmap.get(k)
                if rrow is not None:
                    hit += 1
                if rrow is None and how == "inner":
                    continue
                tail = [None] * (len(right["headers"]) - 1) if rrow is None else \
                    [rrow[i] if i < len(rrow) else None
                     for i in range(len(rrow)) if i != ri]
                out_rows.append(list(row) + tail)
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = (a.get("sheet") or "连接")[:31]
            for j, h in enumerate(cols, 1):
                ws.cell(row=1, column=j, value=h)
            for i, row in enumerate(out_rows, 2):
                for j, v in enumerate(row, 1):
                    if v is not None:
                        ws.cell(row=i, column=j, value=v)
            self._paint_header(ws, 1, 1, len(cols))
            if a.get("autofit", True):
                _autofit(ws, 1, len(cols))
            out, _ = self._save(wb, None, a, "joined")
            return {"path": out, "mode": mode, "how": how, "key": key,
                    "rows": len(out_rows), "matched": hit,
                    "left": len(left["rows"]), "right": len(right["rows"]),
                    "columns": cols}

        raise ValueError(f"不认识的 mode：{mode}（stack / join）")

    # ------------------------------------------------ sheet.export

    def _export(self, a):
        p = _require_file(_norm_path(a.get("path")))
        to = (a.get("to") or "csv").lower()
        wb = _load(p, data_only=not a.get("formula"))
        names = [a["sheet"]] if a.get("sheet") else wb.sheetnames
        out_dir = _norm_path(a.get("out") or
                             (os.path.splitext(p)[0] + "_export"), "out")
        os.makedirs(out_dir, exist_ok=True)

        files = []
        for name in names:
            ws = wb[name]
            hr = a.get("header_row", 1)
            headers, rows, _b = _read_table(ws, header_row=hr if hr not in (0, False) else None,
                                            max_rows=a.get("limit") or MAX_ROWS_RETURN)
            safe = re.sub(r"[\\/:*?\"<>|]", "_", name)
            if to in ("csv", "tsv"):
                ext = ".csv" if to == "csv" else ".tsv"
                fp = os.path.join(out_dir, safe + ext)
                with open(fp, "w", encoding=a.get("encoding") or "utf-8-sig",
                          newline="") as f:
                    w = csv.writer(f, delimiter="," if to == "csv" else "\t")
                    if headers:
                        w.writerow(headers)
                    w.writerows(rows)
            elif to == "json":
                fp = os.path.join(out_dir, safe + ".json")
                payload = ([dict(zip(headers, r + [None] * (len(headers) - len(r))))
                            for r in rows] if headers else rows)
                with open(fp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            elif to in ("md", "markdown", "html"):
                fp = os.path.join(out_dir, safe + ("." + to if to != "markdown" else ".md"))
                cols = headers or [f"列{i + 1}" for i in range(
                    max([len(r) for r in rows] + [0]))]
                lines = []
                if to == "html":
                    lines.append("<table><thead><tr>"
                                 + "".join(f"<th>{c}</th>" for c in cols)
                                 + "</tr></thead><tbody>")
                    for r in rows:
                        lines.append("<tr>" + "".join(
                            f"<td>{'' if v is None else v}</td>" for v in r) + "</tr>")
                    lines.append("</tbody></table>")
                else:
                    lines.append("| " + " | ".join(str(c) for c in cols) + " |")
                    lines.append("|" + "|".join(["---"] * len(cols)) + "|")
                    for r in rows:
                        lines.append("| " + " | ".join(
                            "" if v is None else str(v) for v in r) + " |")
                with open(fp, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
            else:
                raise ValueError(f"不认识的 to：{to}（csv/tsv/json/md/html）")
            files.append({"sheet": name, "path": fp, "rows": len(rows),
                          "bytes": os.path.getsize(fp)})

        return {"from": p, "to": to, "dir": out_dir, "files": files,
                "count": len(files)}


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

    dev = SheetOps()
    ok, why = dev.probe()
    if not ok:
        print("skip: " + why)
        return 0

    td = tempfile.mkdtemp(prefix="sheet_")
    try:
        src = os.path.join(td, "源表.xlsx")

        # ---- sheet.write ----
        r = dev.invoke("sheet.write", {
            "out": src,
            "data": [["姓名", "工号", "部门", "工资"],
                     ["张三", 1001, "研发", 18500.5],
                     ["李四", 1002, "市场", 12000],
                     ["王五", None, "研发", 21000],
                     ["赵六", 1004, "行政", 9000]],
            "headers": "auto"})
        ck(r.ok, "write：写出一张表 %s" % (r.err if not r.ok else ""))
        ck(r.ok and r.data["rows"] == 4, "write：表头被摘走，剩 4 行")
        ck(r.ok and os.path.getsize(src) > 3000, "write：文件真有内容")

        # ---- sheet.info ----
        r = dev.invoke("sheet.info", {"path": src})
        ck(r.ok and r.data["count"] == 1, "info：一张表")
        ck(r.ok and r.data["sheets"][0]["header_guess"] == ["姓名", "工号", "部门", "工资"],
           "info：表头猜对")
        ck(r.ok and r.data["sheets"][0]["rows"] == 5, "info：5 行（含表头）")

        # ---- sheet.read ----
        r = dev.invoke("sheet.read", {"path": src, "header_row": "auto", "stats": True})
        ck(r.ok and r.data["headers"][0] == "姓名", "read：表头")
        ck(r.ok and r.data["returned"] == 4, "read：4 行数据")
        ck(r.ok and r.data["rows"][0][0] == "张三", "read：首行首列")
        ck(r.ok and r.data["rows"][0][3] == 18500.5, "read：浮点没被抹成字符串")
        ck(r.ok and r.data["rows"][2][1] is None, "read：空格仍是 None")
        ck(r.ok and r.data["columns"][1]["type"] == "int", "read：工号列推断为 int")
        ck(r.ok and r.data["columns"][1]["nulls"] == 1, "read：工号列 1 个空")
        ck(r.ok and r.data["columns"][3]["stats"]["sum"] == 60500.5,
           "read：工资合计对得上")
        ck(r.ok and r.data["columns"][0].get("stats") is None, "read：文本列不给数字统计")

        # ---- 区间读 ----
        r = dev.invoke("sheet.read", {"path": src, "range": "A1:B3", "header_row": 1})
        ck(r.ok and len(r.data["rows"]) == 2 and r.data["range"] == "A1:B3",
           "read：区间只取 2 行 2 列")
        ck(r.ok and r.data["headers"] == ["姓名", "工号"], "read：区间表头")

        # ---- sheet.edit ----
        r = dev.invoke("sheet.edit", {"path": src, "cell": "C3", "set": "产品"})
        ck(r.ok and r.data["path"] != src, "edit：默认另存，没动源文件")

        edited = r.data["path"]
        r2 = dev.invoke("sheet.read", {"path": edited, "header_row": "auto"})
        ck(r2.ok and r2.data["rows"][1][2] == "产品", "edit：改的那格真变了")

        r = dev.invoke("sheet.edit", {"path": src, "set": "x", "range": "D2:D5"})
        ck(r.ok, "edit：整块填值")
        r3 = dev.invoke("sheet.read", {"path": r.data["path"], "header_row": "auto"})
        ck(r3.ok and all(row[3] == "x" for row in r3.data["rows"]), "edit：4 格全填上")

        # ---- 原地改要二次确认 ----
        r = dev.invoke("sheet.edit", {"path": src, "cell": "A2", "set": "改", "inplace": True})
        ck((not r.ok) and "预演" in (r.err or ""), "edit：inplace 第一次只预演")
        r = dev.invoke("sheet.edit", {"path": src, "cell": "A2", "set": "改",
                                      "inplace": True, "_confirmed": True})
        ck(r.ok and r.data["path"] == src, "edit：确认后原地改")

        # ---- sheet.append ----
        r = dev.invoke("sheet.append", {"path": src, "rows": [["孙七", 1005, "研发", 15000]],
                                        "out": os.path.join(td, "追加.xlsx")})
        ck(r.ok and r.data["appended"] == 1 and r.data["from_row"] == 6,
           "append：接到第 6 行")
        rr = dev.invoke("sheet.read", {"path": r.data["path"], "header_row": "auto"})
        ck(rr.ok and rr.data["returned"] == 5, "append：读回来 5 行")
        ck(rr.ok and rr.data["rows"][-1][0] == "孙七", "append：末尾就是新加的")

        # ---- sheet.style ----
        r = dev.invoke("sheet.style", {"path": src, "range": "A1:D1", "bold": True,
                                       "fill": "#305496", "color": "FFFFFF",
                                       "align": "center", "border": True,
                                       "out": os.path.join(td, "美化.xlsx")})
        ck(r.ok and r.data["cells"] == 4, "style：4 格")
        from openpyxl import load_workbook
        wsb = load_workbook(r.data["path"])
        c0 = wsb.active["A1"]
        ck(c0.font.bold is True, "style：加粗生效")
        ck(c0.fill.fgColor.rgb.endswith("305496"), "style：底色生效")

        # ---- 数字格式 ----
        r = dev.invoke("sheet.style", {"path": src, "range": "D2:D5",
                                       "number_format": "¥#,##0.00",
                                       "out": os.path.join(td, "货币.xlsx")})
        ck(r.ok, "style：数字格式")

        # ---- sheet.formula ----
        fp = os.path.join(td, "公式.xlsx")
        dev.invoke("sheet.write", {"out": fp, "data": [[1, 2], [3, 4]],
                                   "headers": ["甲", "乙"]})
        r = dev.invoke("sheet.formula", {"path": fp, "action": "set",
                                         "cell": "A3", "formula": "=SUM(A1:A2)",
                                         "out": os.path.join(td, "公式2.xlsx")})
        ck(r.ok and r.data["cells"] == 1, "formula：写进一条公式")

        r = dev.invoke("sheet.formula", {"path": r.data["path"], "action": "list"})
        ck(r.ok and r.data["count"] == 1, "formula：列出 1 条")
        ck(r.ok and "A1" in r.data["formulas"][0]["refs"], "formula：引用被解析出来")

        r = dev.invoke("sheet.formula", {"path": r.data["path"], "action": "cached",
                                         "range": "A3:A3"})
        ck(r.ok and r.data["values"] == [[None]], "formula：没算过就是 null（诚实）")
        ck(r.ok and "缓存" in r.data["note"], "formula：说清为什么是空")

        r = dev.invoke("sheet.formula", {"path": r.data["path"], "action": "trace",
                                         "cell": "A1"})
        ck(r.ok and r.data["count"] == 1, "formula：trace 找到引用者")
        ck(r.ok and r.data["referenced_by"][0]["cell"] == "A3", "formula：引用者是 A3")

        # ---- 公式以 = 开头没写等号也认 ----
        r = dev.invoke("sheet.formula", {"path": fp, "action": "set", "cell": "B3",
                                         "formula": "SUM(B1:B2)",
                                         "out": os.path.join(td, "公式3.xlsx")})
        ck(r.ok and r.data["formula"].startswith("="), "formula：自动补等号")

        # ---- sheet.sheets ----
        r = dev.invoke("sheet.sheets", {"path": src, "action": "list"})
        ck(r.ok and len(r.data["sheets"]) == 1, "sheets：列出一张")

        r = dev.invoke("sheet.sheets", {"path": src, "action": "add", "name": "二月",
                                        "out": os.path.join(td, "多表.xlsx")})
        ck(r.ok and "二月" in r.data["sheets"], "sheets：加一张")

        multi = r.data["path"]
        r = dev.invoke("sheet.sheets", {"path": multi, "action": "rename",
                                        "name": "二月", "to": "Feb",
                                        "out": os.path.join(td, "改名.xlsx")})
        ck(r.ok and "Feb" in r.data["sheets"], "sheets：改名")

        single = os.path.join(td, "单表.xlsx")
        dev.invoke("sheet.write", {"out": single, "data": [["只有一张"]]})
        r = dev.invoke("sheet.sheets", {"path": single, "action": "delete",
                                        "name": "Sheet1", "out": os.path.join(td, "删表.xlsx")})
        ck((not r.ok) and "空" in (r.err or ""), "sheets：只剩一张时不让删")

        # ---- sheet.merge stack ----
        a1 = os.path.join(td, "一月.xlsx")
        a2 = os.path.join(td, "二月.xlsx")
        dev.invoke("sheet.write", {"out": a1, "headers": "auto",
                                   "data": [["月", "额"], ["一月", 100], ["一月", 200]]})
        dev.invoke("sheet.write", {"out": a2, "headers": "auto",
                                   "data": [["月", "额"], ["二月", 300]]})
        r = dev.invoke("sheet.merge", {"mode": "stack", "sources": [a1, a2],
                                       "out": os.path.join(td, "堆.xlsx")})
        ck(r.ok and r.data["rows"] == 3, "merge：堆出 3 行")
        ck(r.ok and r.data["cols"] == 2, "merge：2 列")

        rm = dev.invoke("sheet.read", {"path": r.data["path"], "header_row": "auto",
                                       "stats": True})
        ck(rm.ok and rm.data["columns"][1]["stats"]["sum"] == 600, "merge：金额没串行")

        # ---- 列不一样时按并集对齐 ----
        a3 = os.path.join(td, "三月.xlsx")
        dev.invoke("sheet.write", {"out": a3, "headers": "auto",
                                   "data": [["月", "额", "备注"], ["三月", 400, "补"]]})
        r = dev.invoke("sheet.merge", {"mode": "stack", "sources": [a1, a3],
                                       "out": os.path.join(td, "并集.xlsx")})
        ck(r.ok and r.data["cols"] == 3, "merge：列取并集")
        rm = dev.invoke("sheet.read", {"path": r.data["path"], "header_row": "auto"})
        ck(rm.ok and rm.data["rows"][0][2] is None, "merge：缺的列留空不串位")
        ck(rm.ok and rm.data["rows"][2][2] == "补", "merge：有值的落对列")

        # ---- sheet.merge join ----
        b1 = os.path.join(td, "人.xlsx")
        b2 = os.path.join(td, "部门表.xlsx")
        dev.invoke("sheet.write", {"out": b1, "headers": "auto",
                                   "data": [["工号", "姓名"], [1001, "张三"],
                                            [1002, "李四"], [1003, "王五"]]})
        dev.invoke("sheet.write", {"out": b2, "headers": "auto",
                                   "data": [["工号", "部门"], [1001, "研发"],
                                            [1003, "行政"]]})
        r = dev.invoke("sheet.merge", {"mode": "join", "key": "工号", "how": "left",
                                       "sources": [b1, b2], "out": os.path.join(td, "拼.xlsx")})
        ck(r.ok and r.data["rows"] == 3, "join：left 保 3 行")
        ck(r.ok and r.data["matched"] == 2, "join：命中 2 行")
        ck(r.ok and r.data["columns"] == ["工号", "姓名", "部门"], "join：列名去重")

        rj = dev.invoke("sheet.read", {"path": r.data["path"], "header_row": "auto"})
        ck(rj.ok and rj.data["rows"][1][2] is None, "join：李四没部门是空")
        ck(rj.ok and rj.data["rows"][2][2] == "行政", "join：王五对上行政")

        r = dev.invoke("sheet.merge", {"mode": "join", "key": "工号", "how": "inner",
                                       "sources": [b1, b2], "out": os.path.join(td, "内.xlsx")})
        ck(r.ok and r.data["rows"] == 2, "join：inner 只剩 2 行")

        # ---- sheet.export ----
        r = dev.invoke("sheet.export", {"path": src, "to": "csv",
                                        "out": os.path.join(td, "导出")})
        ck(r.ok and len(r.data["files"]) == 1, "export：一个 csv")
        with open(r.data["files"][0]["path"], "rb") as f:
            head = f.read(3)
        ck(head == b"\xef\xbb\xbf", "export：带 BOM，Excel 打开不乱码")

        r = dev.invoke("sheet.export", {"path": src, "to": "md",
                                        "out": os.path.join(td, "导出md")})
        ck(r.ok and os.path.getsize(r.data["files"][0]["path"]) > 20, "export：markdown")

        r = dev.invoke("sheet.export", {"path": src, "to": "json",
                                        "out": os.path.join(td, "导出json")})
        ck(r.ok, "export：json")
        with open(r.data["files"][0]["path"], encoding="utf-8") as f:
            obj = json.load(f)
        ck(isinstance(obj, list) and obj[0]["姓名"] == "张三", "export：json 按表头成了字典")

        # ---- CSV 进来 ----
        csvp = os.path.join(td, "带分号.csv")
        with open(csvp, "w", encoding="gb18030") as f:
            f.write("姓名;分数\n甲;90\n乙;85\n")
        r = dev.invoke("sheet.read", {"path": src, "range": "A1:B2"})
        ck(r.ok, "read：对照组")
        r = dev.invoke("sheet.write", {"csv": csvp, "headers": "auto",
                                       "out": os.path.join(td, "从csv来.xlsx")})
        ck(r.ok and r.data["rows"] == 2, "write：从 GBK+分号 CSV 建表")
        rr = dev.invoke("sheet.read", {"path": r.data["path"], "header_row": "auto",
                                       "stats": True})
        ck(rr.ok and rr.data["headers"] == ["姓名", "分数"], "write：分号被当分隔符")
        ck(rr.ok and rr.data["columns"][1]["stats"]["sum"] == 175, "write：分数读对")

        # ---- 错误路径 ----
        r = dev.invoke("sheet.info", {"path": os.path.join(td, "没有.xlsx")})
        ck((not r.ok) and "不存在" in (r.err or ""), "错误：文件不存在")

        r = dev.invoke("sheet.info", {})
        ck((not r.ok) and "缺 path" in (r.err or ""), "错误：缺 path")

        old = os.path.join(td, "老.xls")
        open(old, "wb").write(b"not really xls")
        r = dev.invoke("sheet.info", {"path": old})
        ck((not r.ok) and ".xls" in (r.err or ""), "错误：.xls 明确告诉换路子")

        r = dev.invoke("sheet.read", {"path": src, "sheet": "不存在"})
        ck((not r.ok) and "没有这张表" in (r.err or ""), "错误：表名不对时列出可选项")

        r = dev.invoke("sheet.read", {"path": src, "range": "1A1"})
        ck((not r.ok) and "不合法" in (r.err or ""), "错误：坏单元格地址")

        r = dev.invoke("sheet.edit", {"path": src})
        ck((not r.ok) and "没给动作" in (r.err or ""), "错误：edit 没给动作")

        r = dev.invoke("sheet.merge", {"mode": "join", "sources": [b1, b2]})
        ck((not r.ok) and "key" in (r.err or ""), "错误：join 缺 key")

        r = dev.invoke("sheet.formula", {"path": src, "action": "乱写"})
        ck((not r.ok) and "不认识的 action" in (r.err or ""), "错误：未知 action")

        r = dev.invoke("sheet.write", {"out": os.path.join(td, "空.xlsx"), "data": []})
        ck((not r.ok) and "空" in (r.err or ""), "错误：空数据不落盘")

        r = dev.invoke("sheet.export", {"path": src, "to": "pdf"})
        ck((not r.ok) and "csv/tsv" in (r.err or ""), "错误：未知导出格式")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    total = state["n"]
    got = total - len(fails)
    print("\nsheetops selftest: %d/%d%s"
          % (got, total, "" if not fails else "（失败 %d）" % len(fails)))
    for f in fails:
        print("   失败:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法: python -m fleet.kinds.sheetops --selftest")
    sys.exit(0)

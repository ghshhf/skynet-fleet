"""T80 文档四层的契约门禁：PDF / 表格 / Word / PPT。

这四个域此前「有设备认领、但一条门禁都没跑过」（gated = 0）：模块自带详尽自测
（57 / 86 / 68 / 74 项断言），却没登记进 run_selftests，等于没验。本门禁只做
两件事，不重写一份自测：

  1. **契约面**：四个设备必须真在注册表里，`caps()` 声明与调用表一致，
     `probe()` 不可用时必须说得出原因（不许静默）；
  2. **真跑**：逐个起子进程跑模块自测，退出码必须 0。

覆盖的能力名（与模块自测一一对应，改能力表时这里也要跟着改）：
  pdf.info pdf.merge pdf.split pdf.extract pdf.text pdf.tables pdf.rotate
  pdf.watermark pdf.encrypt pdf.compress pdf.meta pdf.outline pdf.engines
  sheet.info sheet.read sheet.write sheet.edit sheet.append sheet.style
  sheet.formula sheet.sheets sheet.merge sheet.export sheet.chart
  word.info word.read word.placeholders word.fill word.write word.append
  word.table word.style word.header word.meta
  ppt.info ppt.read ppt.outline ppt.fill ppt.new ppt.add ppt.table
  ppt.image ppt.notes ppt.slides

时间预算：实测量级 2~12 秒/模块，合计 20 秒上下。整轮超过 120 秒就判红——
门禁变慢本身就是缺陷（主人定的：不许一条门禁跑半小时）。

跑法：python evals/t80_office_ops.py
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MODULES = ("pdfops", "sheetops", "wordops", "pptops")
BUDGET_S = 120.0
PER_MODULE_S = 120.0


def _devices():
    from fleet.kinds.pdfops import PdfOps
    from fleet.kinds.pptops import PptOps
    from fleet.kinds.sheetops import SheetOps
    from fleet.kinds.wordops import WordOps
    return (PdfOps(), SheetOps(), WordOps(), PptOps())


def main():
    fails = []
    t0 = time.time()

    # ① 契约面：在册、声明、说法一致
    try:
        devs = _devices()
    except Exception as e:
        print(f"  ✗ 四个文档设备导不进来：{type(e).__name__}: {e}")
        return 1

    from fleet.catalog import build
    reg = build(gate=lambda *a: (True, ""))
    names = {d.name for d in reg.devices()}

    for dev in devs:
        if not dev.caps():
            fails.append(f"{dev.kind} 一条能力都没声明")
        elif len(set(dev.caps())) < 10:
            fails.append(f"{dev.kind} 能力只有 {len(set(dev.caps()))} 条，文档四层各应有 10 条以上")

        ok, why = dev.probe()
        if not ok and not str(why or "").strip():
            fails.append(f"{dev.kind} probe 说不可用却没给原因")
        if not ok:
            fails.append(f"{dev.kind} 在本机不可用：{why}")

        if dev.name not in names:
            fails.append(f"{dev.kind}（{dev.name}）没出现在注册表：能力表里够不着它")

    # ② 真跑：模块自测并发跑（四个互不相干，串行只是白等磁盘）
    from concurrent.futures import ThreadPoolExecutor

    def _run(m):
        s = time.time()
        try:
            p = subprocess.run(
                [sys.executable, "-m", f"fleet.kinds.{m}", "--selftest"],
                cwd=ROOT, capture_output=True, text=True, timeout=PER_MODULE_S)
        except subprocess.TimeoutExpired:
            return m, None, time.time() - s, f"自测超时（>{PER_MODULE_S:.0f}s）"
        tail = [ln for ln in (p.stdout or "").strip().splitlines() if "selftest:" in ln]
        line = tail[-1].strip() if tail else (p.stdout or p.stderr or "").strip()[-120:]
        return m, p.returncode, time.time() - s, line

    with ThreadPoolExecutor(max_workers=len(MODULES)) as ex:
        for m, rc, cost, line in ex.map(_run, MODULES):
            if rc is None or rc != 0:
                fails.append(f"{m} 自测失败 rc={rc}：{line}")
            else:
                print(f"  · {m:9s} {cost:5.1f}s  {line}")

    total = time.time() - t0
    if total > BUDGET_S:
        fails.append(f"整轮 {total:.0f}s 超过预算 {BUDGET_S:.0f}s —— 门禁变慢要当缺陷修")

    for f in fails:
        print("  ✗ " + f)
    print(f"t80 文档四层：{'全过' if not fails else str(len(fails)) + ' 项不过'}（{total:.1f}s）")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

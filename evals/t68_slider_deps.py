# -*- coding: utf-8 -*-
"""t68 · 缺 Pillow/numpy 时不许拖垮整个能力表

**这条门禁是给一个真实的坑立的碑。**

2026-11-03 核最后一环时发现：`fleet/slider_cv.py` 是全仓库**唯一**在模块顶层
硬 import numpy/PIL 的文件（vision.py / motion.py / host.py 全在函数内延迟 import）。
而 `catalog.py` 顶层就 `from .kinds.slider_device import SliderDevice`，slider_device
又顶层 import slider_cv —— 于是「这台机器没装 Pillow」不会只让滑块不可用，而是
一路炸到 `import fleet.catalog`：

    ModuleNotFoundError: No module named 'PIL'
    → 注册表建不起来 → /api/caps 空 → 手机工具箱全空 → 415 条能力一起哑

值得记的不是「漏了 try」，而是**一个可选依赖的位置，能决定整机的生死**。

覆盖：
  1. 静态：slider_cv 的 numpy/PIL import 必须在 try 块里（缩进），不许回到顶层
  2. 动态（正向对照）：依赖齐全时 deps_ok() 为真、detect 真能出数
  3. 动态（反面）：子进程拦截 PIL/numpy 后 `import fleet.catalog` 仍成功、
     415 条能力仍在、slider 五条声明仍在
  4. 反面里再验一次「拦截真的生效了」—— 否则第 3 条可能因为没拦住而假绿
  5. 缺依赖时报错必须是人话（含 Pillow/numpy 字样），不是 AttributeError: 'NoneType'
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet import catalog, slider_cv as SC               # noqa: E402

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


#: 缺依赖的对照脚本。**拦截必须自己先验证生效**，不然整条反面断言都是假绿。
PROBE = r'''
import sys, json
BLOCK = {"PIL", "numpy"}

class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCK:
            raise ImportError("blocked-for-test: " + name)
        return None

sys.meta_path.insert(0, Blocker())
for m in [k for k in list(sys.modules) if k.split(".")[0] in BLOCK]:
    del sys.modules[m]

out = {}
# ① 拦截生效了吗（防假绿）
try:
    import numpy  # noqa: F401
    out["blocked"] = False
except Exception:
    out["blocked"] = True

# ② 真正的断言：能力表必须还能 import
try:
    from fleet import catalog
    caps = list(catalog.CAPABILITIES)
    out["import_ok"] = True
    out["caps_total"] = len(caps)
    out["slider"] = [c.name for c in caps if str(c.name).startswith("slider.")]
except Exception as e:
    out["import_ok"] = False
    out["err"] = "%s: %s" % (type(e).__name__, e)

# ③ 缺依赖时调用要给一句人话
try:
    from fleet import slider_cv as SC
    out["deps_ok"] = SC.deps_ok()
    try:
        SC.detect("a.png", "b.png")
        out["call_err"] = ""
    except Exception as e:
        out["call_err"] = "%s: %s" % (type(e).__name__, e)
except Exception as e:
    out["deps_ok"] = None
    out["call_err"] = "IMPORT-FAIL %s: %s" % (type(e).__name__, e)

print(json.dumps(out, ensure_ascii=False))
'''


def main():
    # ---------------- 1. 静态：不许回到顶层硬 import
    src = open(os.path.join(ROOT, "fleet", "slider_cv.py"), encoding="utf-8").read()
    check(not re.search(r"(?m)^import numpy", src),
          "slider_cv.py 出现了顶层 `import numpy`（必须缩进进 try 块）")
    check(not re.search(r"(?m)^from PIL", src),
          "slider_cv.py 出现了顶层 `from PIL`（必须缩进进 try 块）")
    check("_DEPS_ERR" in src and "def _require" in src,
          "slider_cv.py 缺 _DEPS_ERR/_require 这套护栏")

    # 顺带把「仓库惯例」也钉住：这些模块本来就在函数内延迟 import，别哪天被"整理"上来
    for name in ("vision.py", "motion.py"):
        p = os.path.join(ROOT, "fleet", name)
        if not os.path.exists(p):
            continue
        s = open(p, encoding="utf-8").read()
        check(not re.search(r"(?m)^import numpy", s),
              "%s 出现了顶层 `import numpy`（仓库惯例是函数内延迟 import）" % name)
        check(not re.search(r"(?m)^from PIL", s),
              "%s 出现了顶层 `from PIL`（仓库惯例是函数内延迟 import）" % name)

    # ---------------- 2. 正向对照：依赖在时一切照旧
    check(SC.deps_ok(), "本机依赖齐全时 deps_ok() 应为真（实报缺：%s）" % (SC._DEPS_ERR or "-"))
    bg, tile = SC._synth("shadow", seed=11)[:2]
    try:
        d = SC.detect(bg, tile)
        check(isinstance(d, dict) and "x" in d and "confidence" in d,
              "依赖齐全时 detect 应返回含 x/confidence 的 dict（实得 %r）"
              % (sorted(d)[:6] if isinstance(d, dict) else type(d).__name__))
    except Exception as e:
        check(False, "依赖齐全时 detect 不该抛异常（%s: %s）" % (type(e).__name__, e))

    # ---------------- 3/4/5. 反面：拦住依赖，能力表不许跟着死
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        check(False, "缺依赖对照脚本自身跑挂了：%s" % (p.stderr or "")[-400:])
    else:
        import json
        try:
            o = json.loads(p.stdout.strip().splitlines()[-1])
        except Exception:
            o = {}
            check(False, "缺依赖对照脚本没吐 JSON：%r" % (p.stdout or "")[-300:])

        if o:
            check(o.get("blocked") is True,
                  "对照脚本里的 PIL/numpy 拦截没生效 —— 后面的断言都是假绿")
            check(o.get("import_ok") is True,
                  "缺 Pillow/numpy 时 `import fleet.catalog` 挂了：%s" % o.get("err"))
            check(o.get("caps_total") == len(catalog.CAPABILITIES),
                  "缺依赖时能力总数变了：%r vs 本机 %d"
                  % (o.get("caps_total"), len(catalog.CAPABILITIES)))
            check(sorted(o.get("slider") or []) == ["slider.auto", "slider.detect",
                                                    "slider.drag", "slider.solve_page",
                                                    "slider.track"],
                  "缺依赖时 slider 五条声明应仍在（实得 %r）" % (o.get("slider"),))
            check(o.get("deps_ok") is False,
                  "缺依赖时 deps_ok() 应为假（实得 %r）" % (o.get("deps_ok"),))
            err = str(o.get("call_err") or "")
            check("Pillow" in err or "numpy" in err,
                  "缺依赖时的报错要点名缺什么（实得 %r）" % err[:120])
            check("AttributeError" not in err,
                  "缺依赖时不该漏出 AttributeError（NoneType/'str' has no ... 都是同一类漏）"
                  "（实得 %r）" % err[:120])

    print()
    if FAIL:
        print("T68 红：%d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("PASS: 可选依赖不越界（顶层无硬 import / 缺 Pillow 时 415 条能力照在 / "
          "报错点名缺什么而非 AttributeError / 拦截自身经反向验证生效）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

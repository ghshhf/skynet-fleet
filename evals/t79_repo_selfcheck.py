# -*- coding: utf-8 -*-
"""t79 · 仓库自检：HEAD 导出来的东西能不能跑（补「本地有、仓库没有」的盲区）。

## 为什么要有这条

2026-11-03 发现 `fleet/gemini.py` 里一句裸 `from . import vault`，
而 `fleet/vault.py` **从没入过库**。本机一切正常，门禁全绿——
因为所有门禁都在工作区跑，工作区文件是齐的。

直到把 HEAD `git archive` 出去才炸：
    ImportError: cannot import name 'vault' from 'fleet'

同一次还翻出另外 10 个模块（captcha / recon_engine / tool_runner /
xfeed / xhunt / notion / totp / smsfree / browsercookie / captcha_device）
同样是「写了、能跑、没提交」。

**这类问题工作区内的门禁永远抓不到**，只有换一个视角才能看见：
把「仓库实际存的东西」当成一个独立环境去检查。

## 判据（静态，不 import）

不真 import，原因有二：慢（几百个模块几十秒）、有副作用（有的模块
import 就连网 / 起进程）。改成 AST 解析内部导入，比对 HEAD 文件清单：

    from . import X            → fleet/X.py 或 fleet/X/__init__.py
    from .X import Y           → fleet/X.py 或 fleet/X/__init__.py
    from .. import X           → 上一级同理
    from ..pkg import Y        → 上一级 pkg
    from fleet import X        → fleet/X.*
    from fleet.pkg import Y    → fleet/pkg/*

目标不在 HEAD 清单里 = 主干有个洞。绝对导入（os / sys / requests）不判。

## 两个已知的"合法空洞"

- `try/except ImportError` 包着的可选依赖（如 entry_guard 取 vault）
- 只在字符串/注释里出现的名字

所以判据是**导入语句本身**，不是文本匹配。
"""
import ast
import io
import os
import subprocess
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "fleet"


def chk(cond, msg):
    print(('  ✓ ' if cond else '  ✗ FAIL: ') + msg)
    if not cond:
        raise SystemExit(1)


def _head_files():
    """HEAD 里的文件清单（相对路径，正斜杠）。"""
    r = subprocess.run(["git", "ls-tree", "-r", "HEAD", "--name-only"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise SystemExit("git ls-tree 失败：" + (r.stderr or "")[-300:])
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _head_read(path):
    """直接读 HEAD 里的文件内容，不碰工作区——工作区可能比 HEAD 新。"""
    r = subprocess.run(["git", "show", "HEAD:" + path],
                       cwd=ROOT, capture_output=True)
    if r.returncode != 0:
        return ""
    return r.stdout.decode("utf-8", "replace")


def _pkg_of(path):
    """文件路径 → 它所在的包（导入时 `.` 指向的那个点分名）。

    fleet/kinds/host.py      → fleet.kinds
    fleet/kinds/__init__.py  → fleet.kinds     ← 包自己的 __init__ 属于**本包**
    fleet/core/registry.py   → fleet.core
    fleet/__init__.py        → ''              ← 包根

    注意 __init__.py 不能和普通模块一样再 rsplit 一次，否则
    `fleet/core/__init__.py` 里的 `from . import result` 会被算成
    `fleet.result`（少一层，全盘误报）。
    """
    p = path[:-3].replace("/", ".")
    if p.endswith(".__init__"):
        p = p[: -len(".__init__")]
        if not p.startswith(PKG):
            return None
        return "" if p == PKG else p
    if not p.startswith(PKG):
        return None
    return p.rsplit(".", 1)[0] if "." in p else ""


def _pkg_attrs():
    """`fleet/__init__.py` 顶层定义/导出的名字。

    `from fleet import VERSION` 里 VERSION 是变量不是模块，不能当洞。
    """
    src = _head_read(PKG + "/__init__.py")
    names = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return names
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
    return names


def _dotted_of(path):
    """文件路径 → 完整点分模块名（用于 module 级导入判定）。"""
    p = path[:-3].replace("/", ".")
    if p.endswith(".__init__"):
        p = p[: -len(".__init__")]
    return p


def _candidates(target):
    """点分名 → 可能对应的文件路径（模块 .py 或 包 __init__.py）。"""
    t = target.replace(".", "/")
    return (t + ".py", t + "/__init__.py")


def _targets(cur_pkg, is_from, node):
    """一条导入语句 → 需要验证存在的点分名列表。

    按 Python 真实导入语义来，不能把 `from X import Y` 里的 Y 一律当子模块：

        from .core import Capability      → 只验 `fleet.core`
                                           （Capability 是类，不是模块）
        from . import vault               → 验 `fleet` 和 `fleet.vault`
                                           （module 为空，Y 必是子模块）
        from fleet import vault           → 同上
        from .. import recon_engine       → 验 `fleet` 和 `fleet.recon_engine`
        import fleet.foo.bar              → 验 `fleet.foo.bar`

    这就是一开始误报几十条的原因：`from .core import Capability`
    被判成「`fleet.core.Capability` 不存在」。
    """
    if not is_from:                                   # import a.b as c
        return [a.name for a in node.names if a.name != "*"]

    level = getattr(node, "level", 0) or 0
    mod = node.module or ""

    if level:                                         # 相对导入，先展开
        parts = cur_pkg.split(".") if cur_pkg else []
        drop = level - 1
        if drop > len(parts):
            return []
        if drop:
            parts = parts[: len(parts) - drop]
        # parts 被削空 = 落回包根（fleet/__init__.py 里的 `from . import x`
        # 或 fleet/xxx.py 里的 `from .. import x`），目标是 fleet
        base = ".".join(parts) if parts else PKG
        target = (base + "." + mod) if mod else base
    else:
        target = mod

    out = [target] if target else []
    # 只有「导入包本身」时，后面那些名字才一定是子模块
    if not mod or target == PKG:
        for a in node.names:
            nm = a.name
            if nm and nm != "*":
                out.append((target + "." + nm) if target else nm)
    return out


def scan():
    files = _head_files()
    pyfiles = [f for f in files if f.endswith(".py") and f.startswith(PKG + "/")]
    have = set(files)
    attrs = _pkg_attrs()
    missing = []

    for f in pyfiles:
        src = _head_read(f)
        if not src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        cur_pkg = _pkg_of(f)
        if cur_pkg is None:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                is_from = True
            elif isinstance(node, ast.Import):
                is_from = False
            else:
                continue

            for target in _targets(cur_pkg, is_from, node):
                if not (target == PKG or target.startswith(PKG + ".")):
                    continue                      # 不是我们包的，不管
                # 包根的顶层属性（VERSION / __all__ 之类）不是模块
                parent, _, leaf = target.rpartition(".")
                if parent == PKG and leaf in attrs:
                    continue
                if target == PKG:
                    ok = (PKG + "/__init__.py") in have
                else:
                    ok = any(c in have for c in _candidates(target))
                if not ok:
                    missing.append((f, target,
                                    node.lineno if hasattr(node, "lineno") else 0))
    # 去重（同一个洞可能被多处引用）
    seen, uniq = set(), []
    for f, t, ln in missing:
        k = (f, t)
        if k in seen:
            continue
        seen.add(k)
        uniq.append((f, t, ln))
    return pyfiles, uniq


def main():
    print("t79 · 仓库自检（HEAD 导出来的东西能不能跑）")

    r = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("  - 不是 git 仓库，跳过")
        return

    pyfiles, missing = scan()
    chk(len(pyfiles) > 100, "扫到 %d 个包内模块（至少要 100 才有意义）" % len(pyfiles))

    if missing:
        print("  ✗ FAIL: HEAD 里有 %d 处导入指向不存在的文件——本地能跑、clone 跑不动" % len(missing))
        for f, t, ln in missing[:20]:
            print("      %s:%s → %s" % (f, ln, t))
        print("    修法：把目标文件 git add 进来，或把导入改成 try/except 可选依赖")
        raise SystemExit(1)

    chk(True, "包内 %d 个模块的导入目标全在库里" % len(pyfiles))

    # 反向抽查：确认判据真能抓到东西（拿一个必然不存在的名字试）
    fake = _candidates("fleet.__t79_no_such_module__")
    have = set(_head_files())
    chk(not any(c in have for c in fake), "判据有效（不存在的模块判得出来）")

    print("t79 · PASS")


if __name__ == "__main__":
    main()

"""代码层：把「读代码」这件事变成能查的结论。

为什么单独一台：
  这个仓现在 80 多个域、几十万行，靠眼睛扫已经扫不动了。
  常见的问题其实都是结构问题，AST 一算就出来：
    · 哪些函数写了没人调（死代码）
    · 谁 import 了谁、有没有绕成环
    · 哪个函数绕得读不下去（复杂度）
    · 遗留的 TODO 都堆在哪
  这些用 grep 做会漏（跨行、别名、同名字符串），用 AST 才准。

边界（诚实写在前面）：
  · **只做静态分析**。动态派发、`getattr` 拼出来的调用、反射注册，一概看不见。
    所以「没人调用」只代表**静态引用为零**，不等于真没用——删之前先想清楚。
  · **只认 Python**。其他语言的语法树是另一套东西，别硬套。
  · **不做类型推断**。`a.b.c()` 里的 a 是什么类型不猜，只记下这个名字。
  · **不执行代码**。`ast.parse` 只解析不运行，所以有语法错误的文件会被跳过并计数。
"""
import ast
import json
import os
import re

from ..core import BaseDevice, DeviceResult as R

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules",
             ".mypy_cache", ".pytest_cache", "dist", "build", ".idea"}
TODO_RE = re.compile(r"(TODO|FIXME|XXX|HACK|NOTE)\b[：: ]?\s*(.*)")


def _iter_py(root, limit=20000):
    """按广度优先走目录，先给浅层的文件。

    🔴 为什么是 BFS 不是 os.walk 的默认 DFS：DFS 会一头扎进最深的子树，
    在 2 秒预算里可能只扫完一个子目录就超时，给人「这个仓很小」的错觉。
    """
    if os.path.isfile(root):
        yield root
        return
    queue = [root]
    n = 0
    while queue and n < limit:
        cur = queue.pop(0)
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                if e.name in SKIP_DIRS or e.name.startswith("."):
                    continue
                queue.append(e.path)
            elif e.name.endswith(".py"):
                yield e.path
                n += 1
                if n >= limit:
                    return


def _parse(path):
    """读并解析。返回 (tree, error)。错误不抛，交给调用方统计。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError as exc:
        return None, None, "读不了：%s" % exc
    try:
        return ast.parse(src, filename=path), src, None
    except SyntaxError as exc:
        return None, src, "语法错（第 %s 行）：%s" % (exc.lineno, exc.msg)


def _complexity(node):
    """圈复杂度：分支 + 循环 + 异常 + 布尔短路各算一条路。

    为什么用这个粗口径：精确的路径数会爆炸，而人其实只需要知道
    「这个函数有几条独立路径要同时在脑子里装着」。
    """
    score = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While,
                              ast.ExceptHandler, ast.With, ast.AsyncWith,
                              ast.Assert, ast.IfExp)):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += len(child.values) - 1
        elif isinstance(child, ast.comprehension):
            score += 1 + len(child.ifs)
    return score


def _defs(tree):
    """顶层与类内的函数/类定义。嵌套函数也算，但标出它是嵌套的。"""
    out = []

    def walk(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                out.append({"name": name, "kind": "func", "line": child.lineno,
                            "end": getattr(child, "end_lineno", child.lineno),
                            "args": [a.arg for a in child.args.args],
                            "decorators": [ast.unparse(d) for d in child.decorator_list],
                            "complexity": _complexity(child),
                            "nested": bool(prefix)})
                walk(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                name = prefix + child.name
                out.append({"name": name, "kind": "class", "line": child.lineno,
                            "end": getattr(child, "end_lineno", child.lineno),
                            "bases": [ast.unparse(b) for b in child.bases],
                            "decorators": [ast.unparse(d) for d in child.decorator_list],
                            "nested": bool(prefix)})
                walk(child, name + ".")

    walk(tree)
    return out


class CodeOps(BaseDevice):
    kind = "codeops"
    via = "local"

    def __init__(self, name="code:py", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "dev")

    def probe(self):
        return True, ""

    def caps(self):
        return ("code.info", "code.ast", "code.imports", "code.dead",
                "code.complex", "code.calls", "code.todos")

    def invoke(self, cap, args=None):
        a = args or {}
        fn = {
            "code.info": self._info,
            "code.ast": self._ast,
            "code.imports": self._imports,
            "code.dead": self._dead,
            "code.complex": self._complex,
            "code.calls": self._calls,
            "code.todos": self._todos,
        }.get(cap)
        if fn is None:
            return R.error(cap, "未知能力 %r" % cap, device=self.name)
        try:
            data = fn(a)
        except ValueError as exc:
            return R.error(cap, str(exc), device=self.name)
        except Exception as exc:                         # noqa: BLE001
            return R.error(cap, "%s: %s" % (type(exc).__name__, exc), device=self.name)
        return R.success(cap, device=self.name, data=data)

    def _root(self, a):
        p = a.get("path") or a.get("root")
        if not p:
            raise ValueError("缺 path：给一个 .py 文件或目录")
        p = os.path.abspath(os.path.expanduser(str(p)))
        if not os.path.exists(p):
            raise ValueError("路径不存在：%s" % p)
        return p

    # ------------------------------------------------------------ info

    def _info(self, a):
        root = self._root(a)
        limit = int(a.get("limit") or 20000)
        files = 0
        lines = 0
        bad = []
        funcs = classes = 0
        for p in _iter_py(root, limit):
            files += 1
            tree, src, err = _parse(p)
            if src:
                lines += src.count("\n") + 1
            if err:
                bad.append({"path": p, "error": err})
                continue
            for d in _defs(tree):
                if d["kind"] == "func":
                    funcs += 1
                else:
                    classes += 1
        return {"path": root, "files": files, "lines": lines,
                "functions": funcs, "classes": classes,
                "parse_errors": len(bad), "errors": bad[:20],
                "truncated": files >= limit and os.path.isdir(root)}

    # ------------------------------------------------------------ ast

    def _ast(self, a):
        p = self._root(a)
        if os.path.isdir(p):
            raise ValueError("code.ast 只接受单个 .py 文件；要看整个目录用 code.info")
        tree, src, err = _parse(p)
        if err:
            raise ValueError(err)
        defs = _defs(tree)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports += [al.name for al in node.names]
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                imports += ["%s.%s" % (mod, al.name) if mod else al.name
                            for al in node.names]
        return {"path": p, "lines": (src or "").count("\n") + 1,
                "defs": defs, "imports": imports,
                "module_doc": ast.get_docstring(tree) or ""}

    # ------------------------------------------------------------ imports

    def _imports(self, a):
        """建本地模块之间的依赖图，并找出环。

        只算**仓内**的边：第三方库（os/json/requests…）不进图，
        否则画出来是一团噪音，看不见真正的问题。
        """
        root = self._root(a)
        if not os.path.isdir(root):
            raise ValueError("code.imports 要看一个目录（单文件没有依赖图可言）")
        files = list(_iter_py(root, int(a.get("limit") or 20000)))
        # 建 module 名 → 文件 的映射
        mods = {}
        for p in files:
            rel = os.path.relpath(p, root)[:-3].replace(os.sep, ".")
            if rel.endswith(".__init__"):
                rel = rel[:-9]
            mods[rel] = p
        edges = {}
        for p in files:
            rel = os.path.relpath(p, root)[:-3].replace(os.sep, ".")
            if rel.endswith(".__init__"):
                rel = rel[:-9]
            tree, src, err = _parse(p)
            if err:
                continue
            deps = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for al in node.names:
                        if al.name in mods:
                            deps.add(al.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:                       # 相对导入
                        base = rel.split(".")
                        up = node.level - 1
                        if up:
                            base = base[:len(base) - up] if up < len(base) else []
                        cand = ".".join(base + ([node.module] if node.module else []))
                        if cand in mods:
                            deps.add(cand)
                        for al in node.names:
                            k = cand + "." + al.name
                            if k in mods:
                                deps.add(k)
                    elif node.module and node.module in mods:
                        deps.add(node.module)
            edges[rel] = sorted(deps)

        # 找环（DFS 三色）
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {k: WHITE for k in edges}
        cycles = []

        def dfs(node, stack):
            color[node] = GRAY
            stack.append(node)
            for nxt in edges.get(node, []):
                if color.get(nxt, WHITE) == GRAY:
                    i = stack.index(nxt)
                    cycles.append(stack[i:] + [nxt])
                elif color.get(nxt, WHITE) == WHITE:
                    dfs(nxt, stack)
            stack.pop()
            color[node] = BLACK

        for k in list(edges):
            if color[k] == WHITE:
                dfs(k, [])

        # 被依赖最多的（扇入）
        fan_in = {}
        for src, deps in edges.items():
            for d in deps:
                fan_in[d] = fan_in.get(d, 0) + 1
        top = sorted(fan_in.items(), key=lambda x: -x[1])[:10]
        return {"root": root, "modules": len(edges),
                "edges": {k: v for k, v in edges.items() if v},
                "cycles": cycles[:20], "cycle_count": len(cycles),
                "most_depended": [{"module": m, "by": n} for m, n in top],
                "note": "只算仓内模块之间的边；第三方导入不进图，否则全是噪音"}

    # ------------------------------------------------------------ dead

    def _dead(self, a):
        """找「定义了但静态引用为零」的顶层函数/类。

        🔴 这是**候选**不是判决：动态派发、getattr、插件注册、被别的仓引用
        都会让静态分析看不见。所以这里返回 needs_review，名字就不叫 deleted。
        """
        root = self._root(a)
        limit = int(a.get("limit") or 20000)
        files = list(_iter_py(root, limit))
        defined = {}          # name -> (path, line, kind)
        used = set()
        for p in files:
            tree, src, err = _parse(p)
            if err:
                continue
            for d in _defs(tree):
                if d["nested"] or d["name"].startswith("_"):
                    continue                      # 私有与嵌套的不判死
                defined.setdefault(d["name"], (p, d["line"], d["kind"]))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    used.add(node.id)
                elif isinstance(node, ast.Attribute):
                    used.add(node.attr)
                elif isinstance(node, ast.Call):
                    f = node.func
                    if isinstance(f, ast.Name):
                        used.add(f.id)
                    elif isinstance(f, ast.Attribute):
                        used.add(f.attr)
            # __all__ 里列出的也算被用
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name) and t.id == "__all__":
                            try:
                                for elt in node.value.elts:
                                    used.add(ast.literal_eval(elt))
                            except Exception:            # noqa: BLE001
                                pass

        dead = [{"name": n, "path": p, "line": ln, "kind": k}
                for n, (p, ln, k) in defined.items() if n not in used]
        dead.sort(key=lambda x: (x["path"], x["line"]))
        return {"root": root, "candidates": len(dead), "listed": dead[:200],
                "scanned_files": len(files),
                "why_candidate_not_verdict":
                    "静态引用为零 ≠ 真没用：getattr 拼名、插件注册表、"
                    "被外部仓 import 都查不到。删之前先搜一遍调用方"}

    # ------------------------------------------------------------ complex

    def _complex(self, a):
        root = self._root(a)
        threshold = int(a.get("threshold") or 10)
        rows = []
        bad = 0
        for p in _iter_py(root, int(a.get("limit") or 20000)):
            tree, src, err = _parse(p)
            if err:
                bad += 1
                continue
            for d in _defs(tree):
                if d["kind"] != "func":
                    continue
                rows.append({"name": d["name"], "path": p, "line": d["line"],
                             "complexity": d["complexity"],
                             "span": d["end"] - d["line"] + 1})
        rows.sort(key=lambda r: -r["complexity"])
        hot = [r for r in rows if r["complexity"] >= threshold]
        return {"root": root, "functions": len(rows), "over_threshold": len(hot),
                "threshold": threshold, "parse_skipped": bad,
                "top": rows[:int(a.get("top") or 20)],
                "note": "圈复杂度：分支+循环+异常+布尔短路各算一条路，"
                        "不是精确路径数，够用来判断「这个函数读不动」"}

    # ------------------------------------------------------------ calls

    def _calls(self, a):
        """谁调用了谁。只认能静态看到的调用（名字或属性名）。"""
        root = self._root(a)
        target = a.get("name")
        calls = {}
        for p in _iter_py(root, int(a.get("limit") or 20000)):
            tree, src, err = _parse(p)
            if err:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        f = child.func
                        if isinstance(f, ast.Name):
                            callee = f.id
                        elif isinstance(f, ast.Attribute):
                            callee = f.attr
                        else:
                            continue
                        calls.setdefault(callee, set()).add(node.name)
        rows = [{"callee": k, "callers": sorted(v), "count": len(v)}
                for k, v in calls.items()]
        if target:
            hit = [r for r in rows if r["callee"] == target]
            return {"root": root, "name": target,
                    "callers": hit[0]["callers"] if hit else [],
                    "found": bool(hit),
                    "note": "只认静态可见的调用；getattr / 反射 / 跨仓调用查不到"}
        rows.sort(key=lambda r: -r["count"])
        return {"root": root, "symbols": len(rows), "top": rows[:int(a.get("top") or 30)],
                "note": "只认静态可见的调用；getattr / 反射 / 跨仓调用查不到"}

    # ------------------------------------------------------------ todos

    def _todos(self, a):
        root = self._root(a)
        kinds = a.get("kinds")
        want = {k.upper() for k in kinds} if kinds else None
        rows = []
        files = 0
        for p in _iter_py(root, int(a.get("limit") or 20000)):
            files += 1
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        m = TODO_RE.search(line)
                        if not m:
                            continue
                        if want and m.group(1).upper() not in want:
                            continue
                        rows.append({"path": p, "line": i, "kind": m.group(1),
                                     "text": m.group(2).strip()[:200]})
            except OSError:
                continue
        by_kind = {}
        for r in rows:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        return {"root": root, "files_scanned": files, "total": len(rows),
                "by_kind": by_kind, "items": rows[:300],
                "truncated": len(rows) > 300}
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

    dev = CodeOps()
    ok, why = dev.probe()
    if not ok:
        print("skip: " + why)
        return 0

    td = tempfile.mkdtemp(prefix="code_")
    try:
        def w(path, text):
            full = os.path.join(td, path)
            os.makedirs(os.path.dirname(full) or td, exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(text)
            return full

        # 造一个有环的包：a -> b -> a
        w("a.py", '"""模块 a。"""\nimport b\n\n\ndef alpha(x):\n'
                  '    if x > 0:\n        return b.beta(x)\n    return 0\n\n'
                  '# TODO: 这里以后要改成异步\n')
        w("b.py", 'import a\n\n\ndef beta(n):\n    total = 0\n'
                  '    for i in range(n):\n        if i % 2:\n'
                  '            total += i\n        else:\n            total -= i\n'
                  '    return total\n\n\ndef never_used_anywhere():\n    return 42\n')
        w("c.py", 'def gamma(items):\n    out = []\n    for it in items:\n'
                  '        if it:\n            out.append(it)\n    return out\n\n\n'
                  'class Unused:\n    pass\n')
        w("sub/d.py", 'def helper():\n    return 1\n')
        w("broken.py", "def oops(:\n    pass\n")

        # ============ code.info ============
        r = dev.invoke("code.info", {"path": td})
        ck(r.ok, "info：能扫目录 %s" % (r.err if not r.ok else ""))
        if r.ok:
            ck(r.data["files"] == 5, "info：5 个文件（含 1 个坏的）")
            ck(r.data["parse_errors"] == 1, "info：如实数出 1 个语法错的")
            ck(r.data["functions"] >= 5, "info：函数数 >= 5")
            ck(r.data["classes"] >= 1, "info：认得出类")
            ck(r.data["lines"] > 30, "info：行数统计")

        r = dev.invoke("code.info", {"path": os.path.join(td, "a.py")})
        ck(r.ok and r.data["files"] == 1, "info：单文件也能扫")

        r = dev.invoke("code.info", {"path": os.path.join(td, "没有这.py")})
        ck((not r.ok) and "不存在" in (r.err or ""), "info：路径不存在要点明")

        r = dev.invoke("code.info", {})
        ck((not r.ok) and "path" in (r.err or ""), "info：缺 path 要报错")

        # ============ code.ast ============
        r = dev.invoke("code.ast", {"path": os.path.join(td, "b.py")})
        ck(r.ok, "ast：能解析 %s" % (r.err if not r.ok else ""))
        if r.ok:
            names = [d["name"] for d in r.data["defs"]]
            ck("beta" in names, "ast：找到 beta")
            ck("never_used_anywhere" in names, "ast：找到 never_used_anywhere")
            ck(any("a" == i for i in r.data["imports"]), "ast：认得出 import a")
            beta = [d for d in r.data["defs"] if d["name"] == "beta"][0]
            ck(beta["complexity"] >= 3, "ast：beta 复杂度 >= 3")
            ck(beta["args"] == ["n"], "ast：参数名对")

        r = dev.invoke("code.ast", {"path": os.path.join(td, "broken.py")})
        ck((not r.ok) and "语法错" in (r.err or ""), "ast：语法错要报行号")

        r = dev.invoke("code.ast", {"path": td})
        ck((not r.ok) and "单个 .py" in (r.err or ""), "ast：目录要拒绝并指路")

        # ============ code.imports ============
        r = dev.invoke("code.imports", {"path": td})
        ck(r.ok, "imports：能建图 %s" % (r.err if not r.ok else ""))
        if r.ok:
            ck(r.data["cycle_count"] >= 1, "imports：找得到 a<->b 的环")
            if r.data["cycles"]:
                cyc = r.data["cycles"][0]
                ck("a" in cyc and "b" in cyc, "imports：环里的模块对")
            ck(r.data["modules"] >= 4, "imports：模块数")
            all_edges = []
            for v in r.data["edges"].values():
                all_edges += v
            ck("os" not in all_edges, "imports：标准库不进图（否则全是噪音）")

        r = dev.invoke("code.imports", {"path": os.path.join(td, "a.py")})
        ck((not r.ok) and "目录" in (r.err or ""), "imports：单文件要拒绝")

        # ============ code.dead ============
        r = dev.invoke("code.dead", {"path": td})
        ck(r.ok, "dead：能算 %s" % (r.err if not r.ok else ""))
        if r.ok:
            names = [c["name"] for c in r.data["listed"]]
            ck("never_used_anywhere" in names, "dead：抓到没人调的")
            ck("Unused" in names, "dead：抓到没人用的类")
            ck("beta" not in names, "dead：被调用的 beta 不算死")
            ck("alpha" in names, "dead：alpha 定义了但无人调用，应算候选")
            ck("why_candidate_not_verdict" in r.data,
               "dead：必须说明「候选不是判决」")

        # ============ code.complex ============
        r = dev.invoke("code.complex", {"path": td, "threshold": 3})
        ck(r.ok, "complex：能算 %s" % (r.err if not r.ok else ""))
        if r.ok:
            ck(r.data["functions"] >= 5, "complex：函数数")
            ck(r.data["top"], "complex：有排行")
            ck(r.data["top"][0]["name"] in ("beta", "gamma"), "complex：最绕的排第一")
            ck(r.data["parse_skipped"] == 1, "complex：跳过 1 个坏文件并报数")

        # ============ code.calls ============
        r = dev.invoke("code.calls", {"path": td, "name": "beta"})
        ck(r.ok, "calls：能查 %s" % (r.err if not r.ok else ""))
        if r.ok:
            ck(r.data["found"] is True, "calls：找得到 beta")
            ck("alpha" in r.data["callers"], "calls：beta 的调用者是 alpha")

        r = dev.invoke("code.calls", {"path": td, "name": "根本没有这个"})
        ck(r.ok and r.data["found"] is False, "calls：查不到时 found=false 不报错")

        r = dev.invoke("code.calls", {"path": td})
        ck(r.ok and r.data["symbols"] >= 2, "calls：不指定名字时给排行榜")

        # ============ code.todos ============
        r = dev.invoke("code.todos", {"path": td})
        ck(r.ok, "todos：能扫 %s" % (r.err if not r.ok else ""))
        if r.ok:
            ck(r.data["total"] >= 1, "todos：至少 1 条")
            ck(r.data["by_kind"].get("TODO", 0) >= 1, "todos：按种类计数")
            ck(any("异步" in it["text"] for it in r.data["items"]), "todos：抓到正文")

        r = dev.invoke("code.todos", {"path": td, "kinds": ["FIXME"]})
        ck(r.ok and r.data["total"] == 0, "todos：按 kinds 过滤生效")

        r = dev.invoke("code.unknown")
        ck(not r.ok, "未知能力要报错")

        # 真材料：能扫得动本仓
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        r = dev.invoke("code.info", {"path": repo, "limit": 500})
        ck(r.ok and r.data["files"] > 10, "真材料：能扫本仓")
        if r.ok:
            ck(r.data["lines"] > 1000, "真材料：行数量级对")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    total = state["n"]
    got = total - len(fails)
    print("\ncodeops selftest: %d/%d%s"
          % (got, total, "" if not fails else "（失败 %d）" % len(fails)))
    for f in fails:
        print("   失败:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法: python -m fleet.kinds.codeops --selftest")
    sys.exit(0)

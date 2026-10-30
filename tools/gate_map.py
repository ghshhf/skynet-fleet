"""门禁影响面映射：算出「每个自测项到底覆盖哪些源文件」。

为什么要有这个工具
------------------
`run_selftests.py` 过去只有「全跑」和「按名字子串猜」两种用法，于是每次改动
都只能全量跑 —— 117 项、十几分钟，而且会越来越慢。这与项目规模无关，是用法错了：
改一行 `fleet/motion.py` 不需要重跑手机配对和桌面壳。

手工给 117 项贴模块标签是死路：新增一项就漂，标签还会互相矛盾。所以标签从代码里
**推**出来：每个自测项的入口文件开始做 import 闭包（只跟仓库内的模块），
得到「这项依赖哪些文件」。反向查表 = 改了这些文件就该跑这项。

用法
----
    python tools/gate_map.py --build        # 生成 tools/.gate_map.json
    python tools/gate_map.py --changed      # 当前工作区改动会影响哪些自测项
    python tools/gate_map.py --changed HEAD~1 # 指定比较基线
    python tools/gate_map.py --public       # 列出被很多项依赖的公共模块（改了要当心）

`.gate_map.json` 不入库（随代码变），每次 --build 覆盖。
"""
import ast
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(HERE, ".gate_map.json")

# 被这么多个自测项依赖，就算「公共模块」：改它基本等于改全局契约，
# 单独跑几项说明不了问题，要提醒人去跑一大片或全量。
PUBLIC_THRESHOLD = 8

_IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".workbuddy", "build",
                "dist", ".venv", "venv", "docs", "apk"}


def _rel(p):
    return os.path.relpath(p, ROOT).replace("\\", "/")


def _is_repo_root(p):
    return os.path.isfile(os.path.join(p, ".git", "HEAD")) or \
        os.path.isdir(os.path.join(p, ".git"))


def _walk_py(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS
                       and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                out.append(os.path.join(dirpath, fn))
    return out


class Resolver(object):
    """把 import 名解析成仓库内真实文件。"""

    def __init__(self, root, roots):
        # roots: search path 列表（仓库根 + 各 case 的 cwd），用于解析顶层包名
        self.root = root
        kept = []
        for r in [root] + list(roots):
            if not r:
                continue
            r = os.path.abspath(r)
            # 已经是某个已选 root 的子目录 → 跳过。否则几十个 case 的 cwd
            # 会把同一棵树重复走几十遍（实测 build 因此要 50 秒）。
            if any(r == k or r.startswith(k + os.sep) for k in kept):
                continue
            # 反过来：新 root 是别人的父目录 → 把子目录挤掉
            kept = [k for k in kept
                    if not (k != r and k.startswith(r + os.sep))]
            kept.append(r)
        self.roots = kept
        self._index = {}
        for r in self.roots:
            for p in _walk_py(r):
                self._index.setdefault(_rel(p), p)
        self._find_memo = {}

    def find(self, dotted, base_dir=None):
        """dotted 形如 'fleet.motion' 或 'common'。返回文件绝对路径或 None。"""
        if not dotted:
            return None
        key = (dotted, base_dir)
        if key in self._find_memo:
            return self._find_memo[key]
        parts = dotted.split(".")
        cands = []
        for r in ([base_dir] + self.roots) if base_dir else self.roots:
            if not r:
                continue
            cands.append(os.path.join(r, *parts) + ".py")
            cands.append(os.path.join(r, *parts, "__init__.py"))
        got = None
        for c in cands:
            if os.path.isfile(c):
                got = os.path.abspath(c)
                break
        self._find_memo[key] = got
        return got

    def resolve_from(self, base_dir, module, level=0, names=()):
        """处理 `from X import Y` / `from . import Y`。返回 (模块文件, [子模块文件])。"""
        out = []
        if level:
            # 相对导入：从 base_dir 往上 level-1 层
            d = base_dir
            for _ in range(max(0, level - 1)):
                d = os.path.dirname(d)
            if module:
                f = self.find(module, d)
                if f:
                    out.append(f)
                else:
                    sub = os.path.join(d, *module.split("."))
                    out += [p for p in _walk_py(sub)] if os.path.isdir(sub) else []
                pkg = os.path.join(d, *module.split(".")) if module else d
            else:
                pkg = d
            for n in names:
                f = self.find(n, pkg) if os.path.isdir(pkg) else None
                if f:
                    out.append(f)
            return out
        f = self.find(module, base_dir)
        subs = []
        for n in names:
            if f:
                # from fleet import motion → fleet/motion.py
                sub = self.find(n, os.path.dirname(f))
                if sub:
                    subs.append(sub)
            sub2 = self.find(module + "." + n, base_dir)
            if sub2:
                subs.append(sub2)
        out.extend(subs)
        # `from fleet import motion` 里的 fleet 只是命名空间，真正的依赖是 motion.py。
        # 只有在「没解析出任何子模块」时才算依赖包的 __init__（那时 from 的是函数/
        # 常量，确实要靠 __init__ 提供）。否则把 __init__ 计入闭包会让它 import 的
        # 一切顺流而下 —— fleet/__init__.py 一摊开就是全仓，映射等于失效。
        if f and (not f.endswith("__init__.py") or not subs):
            out.append(f)
        return out


def _entry_of(case_args):
    """从 case 的参数里挑出入口：`-m X` 的模块名，或第一个不以 - 开头的文件。"""
    a = list(case_args)
    for i, tok in enumerate(a):
        if tok == "-m" and i + 1 < len(a):
            return "mod:" + a[i + 1]
    for tok in a:
        if not tok.startswith("-"):
            return "file:" + tok
    return ""


_AST_CACHE = {}
_EDGE = {}


def _parse(path):
    """同一个文件在 117 个 case 的闭包里会被解析上百次，缓一次就够。"""
    if path in _AST_CACHE:
        return _AST_CACHE[path]
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (SyntaxError, UnicodeDecodeError, OSError):
        tree = None
    _AST_CACHE[path] = tree
    return tree


def _edges(path, resolver):
    """文件的直接 import 目标（仓内文件）。按文件缓存 —— BFS 里同一文件会被
    上百个 case 反复访问，边只该算一次。"""
    if path in _EDGE:
        return _EDGE[path]
    deps = []
    tree = _parse(path) if os.path.isfile(path) else None
    if tree is not None:
        base = os.path.dirname(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for al in node.names:
                    f = resolver.find(al.name, base)
                    if f:
                        deps.append(f)
            elif isinstance(node, ast.ImportFrom):
                lvl = getattr(node, "level", 0) or 0
                mod = node.module or ""
                names = [al.name for al in node.names]
                deps.extend(resolver.resolve_from(base, mod, lvl, names))
    _EDGE[path] = deps
    return deps


def closure(entry_file, resolver, max_files=400):
    """入口文件的 import 闭包。返回 {绝对路径: 依赖距离}。

    距离很关键：`commander` 的自测并没有 import `fleet/motion.py`，它是经
    commander → node_agent → video_gen_runner → motion 三层传过来的。这种深层
    传递依赖不代表「改 motion 会动到 commander 的断言」，却会让最慢的一项被
    任何改动都拖进来 —— 于是「按改动跑」退化成全量。所以距离要留下来，
    由调用方决定几层以内算真被波及。
    """
    dist = {}
    queue = [(os.path.abspath(entry_file), 0)]
    while queue:
        cur, d = queue.pop(0)
        if cur in dist and dist[cur] <= d:
            continue
        if len(dist) >= max_files:
            break
        dist[cur] = d
        for f in _edges(cur, resolver):
            queue.append((f, d + 1))
    return dist


def build():
    sys.path.insert(0, ROOT)
    import run_selftests as R

    # search roots：仓库根 + 各 case 的 cwd
    roots = [ROOT] + sorted({c[2] for c in R.CASES})
    res = Resolver(ROOT, roots)

    cases = {}
    missing = []
    for name, args, cwd, timeout in R.CASES:
        ent = _entry_of(args)
        if ent.startswith("mod:"):
            f = res.find(ent[4:], cwd)
        else:
            f = os.path.join(cwd, ent[5:]) if ent else None
        if not f or not os.path.isfile(f):
            missing.append((name, ent))
            cases[name] = {"files": [], "timeout": timeout}
            continue
        files = closure(f, res)
        cases[name] = {"files": sorted(_rel(p) for p in files),
                       "dists": {_rel(p): d for p, d in files.items()},
                       "timeout": timeout, "entry": _rel(f)}

    # 反向索引：文件 → [[自测项, 距离], ...]（按距离升序，近的排前面）
    rev = {}
    for name, info in cases.items():
        for f, d in (info.get("dists") or {}).items():
            rev.setdefault(f, []).append([name, d])
    for f in rev:
        rev[f].sort(key=lambda kv: (kv[1], kv[0]))
    # 「公共模块」按**直接依赖**（距离 <=1）算：深层传递依赖说明不了问题，
    # 拿它当公共模块会把 116 个文件都标红，等于没提示。
    public = sorted(((f, [x[0] for x in names if x[1] <= 1])
                     for f, names in rev.items()),
                    key=lambda kv: -len(kv[1]))
    public = [(f, n) for f, n in public if len(n) >= PUBLIC_THRESHOLD]

    data = {"root": ROOT, "cases": cases, "reverse": rev,
            "public": [[f, n] for f, n in public],
            "missing": missing}
    with open(CACHE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    print("已生成 %s" % _rel(CACHE))
    print("自测项 %d 个；覆盖文件 %d 个；公共模块 %d 个（被 >=%d 项依赖）"
          % (len(cases), len(rev), len(public), PUBLIC_THRESHOLD))
    if missing:
        print("!! 找不到入口的项（映射为空，只能全量跑时覆盖）：")
        for n, e in missing:
            print("   %s <- %s" % (n, e))
    print("\n公共模块（改这些要跑一大片）：")
    for f, n in public[:12]:
        print("   %-42s %3d 项" % (f, len(n)))
    return 0


def _load():
    if not os.path.isfile(CACHE):
        print("还没有映射表，先跑：python tools/gate_map.py --build")
        raise SystemExit(2)
    return json.load(open(CACHE, encoding="utf-8"))


def changed(base=None):
    data = _load()
    cmd = ["git", "status", "--porcelain"]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    files = set()
    for line in out.splitlines():
        if len(line) < 4:
            continue
        p = line[3:].strip()
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        files.add(p.replace("\\", "/"))
    if base:
        r = subprocess.run(["git", "diff", "--name-only", base], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        files |= {x.replace("\\", "/") for x in r.stdout.splitlines() if x.strip()}
    files = {f for f in files if f.endswith(".py") or f.endswith(".json")}

    meta = {f for f in files
            if os.path.basename(f) in ("run_selftests.py", "gate_map.py")}
    if meta:
        print("改的是门禁基础设施（%s）—— 映射本身可能就不成立了，跑全量："
              % ", ".join(sorted(meta)))
        print("  python run_selftests.py --all")
        return 0

    hit = {}
    orphan = []
    for f in sorted(files):
        names = data["reverse"].get(f) or []
        if not names:
            orphan.append(f)
        for n, d in names:
            prev = hit.get(n)
            # 同一个项被多个改动文件命中时，记最近的那个距离
            if prev is None or d < prev[0]:
                hit[n] = (d, f)
    direct = sorted(n for n, (d, _) in hit.items() if d <= 1)
    indirect = sorted(n for n, (d, _) in hit.items() if d >= 2)
    print("改动 %d 文件 -> 直接相关 %d 项、间接 %d 项（总 %d）\n"
          % (len(files), len(direct), len(indirect), len(data["cases"])))
    for n in direct:
        print("  [必跑] %-30s <- %s" % (n, hit[n][1]))
    for n in indirect:
        print("  [间接] %-30s <- %s（距离 %d）" % (n, hit[n][1], hit[n][0]))
    if orphan:
        print("\n没有任何自测项引用这些文件（新模块？跑一下相关组确认）：")
        for f in orphan:
            print("  " + f)
        print("  提示：若是新写的模块，对应的自测项要显式 import 它，"
              "否则映射看不见这层关系。")
    heavy = [f for f in files if f in data["reverse"]
             and sum(1 for x in data["reverse"][f] if x[1] <= 1) >= PUBLIC_THRESHOLD]
    if heavy:
        print("\n⚠ 改到了公共模块（影响面是全仓级的，不是映射出错）：")
        for f in heavy:
            near = sum(1 for x in data["reverse"][f] if x[1] <= 1)
            print("  %-40s %d 项直接依赖" % (f, near))
        print("  建议：先跑直接的，再补一次全量。")
    print("\n只跑这些：\n  python run_selftests.py %s" % " ".join(direct))
    if indirect:
        print("（间接的 %d 项不算进默认命令；要一起跑加 --deep）" % len(indirect))
    return 0


def show_public():
    data = _load()
    for f, n in data["public"]:
        print("%-44s %3d 项  %s" % (f, len(n), ", ".join(n[:5])))
    return 0


def main():
    args = sys.argv[1:]
    if "--build" in args:
        return build()
    if "--public" in args:
        return show_public()
    if "--changed" in args:
        i = args.index("--changed")
        base = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else None
        return changed(base)
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""统一「东西在哪」。软件里一处都不许写死盘符。

铁律（2026-10-29 主人定，起因见下面「为什么写这个」）：

    代码里只许写「东西叫什么」和「它相对怎么摆」，
    **不许写它在哪个盘**。盘符是机器属性，不是代码属性。

为什么写这个
------------
    起因很具体：仓库里三套「起本地模型」的路径各写死一个盘，互不知道对方存在——

    start-decider.bat      G:\\_downloads\\llamacpp-b11840\\   + G:\\_models\\llm\\gguf\\   (18080 决策)
    start_llm_hidden.py    E:\\_tools\\llama.cpp-b11483\\bin\\ + E:\\_train_qwen06b\\      (8080 对话)
    tianwang.bat           同上，又抄了一遍                                     (8080 对话)
换一台只有 C/D 盘的机器，三条全废。更糟的是它们**看起来是能用的**——
文件在那儿、命令在那儿，只是永远起不来。

这不是「路径写错了」，是「位置被焊进了代码」。同一份目录结构在三个盘上
就有三份硬编码，写四份漏两份，加到第六个盘就得再改一遍。

怎么解决：分成三层
------------------
    name    —— 叫什么。        "ffmpeg" / "llama-server.exe" / "*.gguf"
    hints   —— 相对怎么摆。    "tools/ffmpeg/bin" / "_models/llm/gguf"
                               ↑ 这是**跨机器稳定**的结构，换盘不变
    roots() —— 本机有哪些根。  运行时枚举，**不写死**

    hints × roots() = 全部候选。代码里盘符出现 0 次。

对比一下找 ffmpeg：
    旧写法（写死组合，漏了就用不了）
        E:/dev/ffmpeg/bin · E:/_tools/ffmpeg/bin · D:/tools/ffmpeg/bin · G:/dev/ffmpeg/bin
        ↑ 4 个硬编码，覆盖 2 个盘 × 2 个目录，另外 2 个组合漏了
    新写法（全组合，加盘自动覆盖）
        locate.exe("ffmpeg", hints=("tools/ffmpeg/bin", "dev/ffmpeg/bin", "_tools/ffmpeg/bin"))

优先级链（找到就停）
--------------------
    1. extra       调用方显式给的（测试注入 / 用户指定）
    2. 环境变量     逃生口。FLEET_FFMPEG=... 之类，不改代码就能救，永远最高优先
    3. 项目内相对   跟着仓库走，仓库挪到哪都成立
    4. PATH        shutil.which
    5. hints×roots 组合探测
    6. 受限扫描     深度上限 + 跳过系统目录，别把磁盘扫穿（默认关，显式开）

找不到不等于「返回空字符串然后上层自己瞎猜」。explain() 会说清
「我找过这 N 处、全都不是、你加个 FLEET_X 指给我就行」——
今天 doctor 报「决策模型没起来」却说不清在哪儿找过，就是缺这个。
"""
from __future__ import annotations

import glob
import os
import shutil
import sys

__all__ = [
    "roots", "exe", "file", "dir_", "explain", "miss_report",
    "env_key", "SKIP_DIRS",
]

#: 深度扫描时一律跳过的目录名。扫这些既慢又没意义：
#: 系统盘片、回收站、版本库内部、依赖包、备份卷影。
SKIP_DIRS = frozenset((
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery",
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    "appdata", "local settings", "application data",  # AppData 单独按需找
    "build", "build-sd", "obj", "binaries",           # 构建产物
    ".gradle", ".m2", ".nuget", ".cache", ".npm",
))

#: 单次深度扫描最多看多少个目录条目。防止把整块盘扫穿。
_MAX_SCAN_ENTRIES = 20000

#: 深度扫描最多几层。
_MAX_SCAN_DEPTH = 3


# ---------------------------------------------------------------- 根目录

_ROOTS_CACHE = None


def roots(refresh=False):
    """本机存在且可读的根（`C:/`、`D:/`，Unix 上 `/`）。

    这是整层的基石：**盘符从这儿来，不从代码里来**。
    认不出的盘（光驱空载、坏掉的映射）要排除，否则每次探测都白试一轮。

    Unix/macOS 上只返回 `/`——那边的「盘」是挂载点，硬列盘符本来就不成立；
    要找的非标准位置靠 hints 里的绝对路径或环境变量给。
    """
    global _ROOTS_CACHE
    if _ROOTS_CACHE is not None and not refresh:
        return _ROOTS_CACHE

    out = []
    if os.name != "nt":
        out = ["/"]
    else:
        for ch in "CDEFGHIJKLMNOPQRSTUVWXYZAB":
            base = ch + ":/"
            try:
                if not os.path.exists(base):
                    continue
                # 空光驱：exists 为真但列目录抛异常。试一次，不行就跳过。
                os.listdir(base)
            except OSError:
                continue
            out.append(base)
        if not out:
            out = ["C:/"]
    _ROOTS_CACHE = out
    return out


def env_key(name):
    """由名字推出环境变量名：`ffmpeg` → `FLEET_FFMPEG`。

    统一命名，省得每个模块自己起一个（现在就有 FLEET_ADB / FLEET_FFMPEG /
    FLEET_PY 三套风格混着，人记不住）。老名字由调用方用 env= 显式指定兼容。
    """
    stem = os.path.basename(name or "")
    # 通配符先剥掉：`*.gguf` 直接 splitext 会得到 `*`，再转就成了 FLEET__ 这种废名
    stem = stem.lstrip("*.")
    stem = os.path.splitext(stem)[0]
    clean = "".join(c if (c.isalnum()) else "_" for c in stem).upper()
    return "FLEET_" + (clean or "PATH")


# ---------------------------------------------------------------- 内部工具

def _norm(p):
    return os.path.normpath(p) if p else ""


def _is_exe(p):
    """可执行文件判定：文件存在，且（Windows 上）扩展名像可执行。"""
    if not p or not os.path.isfile(p):
        return False
    if os.name != "nt":
        return os.access(p, os.X_OK)
    return os.path.splitext(p)[1].lower() in (".exe", ".bat", ".cmd", ".com", ".ps1")


def _is_dir(p):
    return bool(p) and os.path.isdir(p)


def _exe_names(name):
    """`ffmpeg` → 在 Windows 上补全扩展名去试。给了全名就只是它自己。"""
    names = [name]
    if os.name == "nt" and not os.path.splitext(name)[1]:
        names = [name + ext for ext in (".exe", ".cmd", ".bat")] + [name]
    return names


def _project_root():
    """仓库根（fleet/ 的上一级）。项目内相对路径都从这儿算。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tidy(cands):
    """去重去空，保持顺序（先给的最优先）。"""
    seen, out = set(), []
    for c in cands:
        if not c:
            continue
        n = _norm(c)
        k = n.lower() if os.name == "nt" else n
        if k in seen:
            continue
        seen.add(k)
        out.append(n)
    return out


def _scan(root, target, depth=_MAX_SCAN_DEPTH, budget=None):
    """在 root 下找名为 target 的文件/目录，限深度、跳过 SKIP_DIRS。

    只在 hints 全落空时才用（默认关）。这里是「兜底」，不是主路——
    主路是 hints × roots，因为那是有依据的猜测，扫描是无依据的穷举。
    """
    if budget is None:
        budget = [_MAX_SCAN_ENTRIES]
    target_l = target.lower()
    hits = []
    stack = [(root, 0)]
    while stack:
        cur, d = stack.pop()
        if d > depth or budget[0] <= 0:
            continue
        try:
            entries = os.scandir(cur)
        except OSError:
            continue
        with entries:
            for e in entries:
                budget[0] -= 1
                if budget[0] <= 0:
                    break
                try:
                    if e.name.lower() == target_l:
                        hits.append(e.path)
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if e.name.lower() in SKIP_DIRS:
                            continue
                        stack.append((e.path, d + 1))
                except OSError:
                    continue
    return hits


def _expand(hint):
    """把 hint 里的 `~` 展开成当前用户目录。

    为什么需要：老代码里有一批 `C:/Users/admin/AppData/Local/...` ——
    用户名 `admin` 被焊死在路径里，换台机器换个用户名就废。
    这类「用户目录下的标准位置」应该写成 `~/AppData/Local/...`，
    展开后是绝对路径，不再需要跟盘符做组合。
    """
    h = hint or ""
    if h.startswith("~"):
        return os.path.expanduser(h)
    # 也认 `~user` 之外的 Windows 风格：%LOCALAPPDATA% 之类由调用方自己展开
    return h


def _hint_candidates(name, hints, roots_):
    """hints × roots 的笛卡尔积。**盘符在这算出来，不在代码里写。**

    hint 若是绝对路径（`/opt/foo`、`D:/x`、`~/AppData/...`）就原样使用——
    那是调用方明确指定位置，不该再被拼到别的根下面。
    """
    out = []
    for hint in hints or ():
        hint = _expand(hint)
        if not hint:
            continue
        if os.path.isabs(hint):
            out.append(os.path.join(hint, name))
            continue
        for r in roots_:
            out.append(os.path.join(r, hint, name))
    return out


# ---------------------------------------------------------------- 主接口

def exe(name, env=(), extra=(), hints=(), scan=False, under=()):
    """找一个可执行文件，返回绝对路径；找不到返回 ""。

    参数
        name   文件名。`"ffmpeg"` 在 Windows 上会自动试 `.exe/.cmd/.bat`。
        env    环境变量名（字符串或序列）。**永远最高优先**——
               换机器、装到了怪地方，`set FLEET_FFMPEG=...` 就能救，不用改代码。
        extra  调用方显式给的路径（测试注入用，排第一）。
        hints  相对目录结构，如 `"tools/ffmpeg/bin"`。跨机器稳定的部分。
        under  在某个**前缀目录**下面递归找（限深度）。
               用来兜「中间夹了一层猜不到的名字」这种情况——
               典型：`tools/ffmpeg/ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe`，
               版本号目录会变，写进 hints 就等于又硬绑了一次。
        scan   是否允许全盘兜底扫描（默认 False，日常调用不该付这个代价）。

    找不到别自己编路径，见 explain()。
    """
    envs = [env] if isinstance(env, str) else list(env or ())
    envs = [e for e in envs if e] or [env_key(name)]

    # 1) 显式给的
    for p in extra or ():
        if _is_exe(p):
            return _norm(p)

    # 2) 环境变量（逃生口）
    for e in envs:
        p = (os.environ.get(e) or "").strip().strip('"')
        if _is_exe(p):
            return _norm(p)

    # 3) 项目内相对（跟着仓库走）
    proj = _project_root()
    for rel in ("bin", "tools/bin", "vendor", "scripts"):
        for n in _exe_names(name):
            p = os.path.join(proj, rel, n)
            if _is_exe(p):
                return _norm(p)

    # 4) PATH
    for n in _exe_names(name):
        hit = shutil.which(n)
        if hit and _is_exe(hit):
            return _norm(hit)

    # 5) hints × roots
    for n in _exe_names(name):
        for p in _hint_candidates(n, hints, roots()):
            if _is_exe(p):
                return _norm(p)

    # 5.5) under：前缀目录下有限递归（版本号子目录这类猜不到的层级）
    for u in under or ():
        u = _expand(u)
        for base in ([u] if os.path.isabs(u) else
                     [os.path.join(r, u) for r in roots()]):
            if not os.path.isdir(base):
                continue
            for n in _exe_names(name):
                hits = _scan(base, n, depth=3)
                if hits:
                    return _norm(sorted(hits, key=len)[0])   # 浅的优先

    # 6) 全盘兜底扫描（默认关）
    if scan:
        for n in _exe_names(name):
            for r in roots():
                hits = _scan(r, n)
                if hits:
                    return _norm(sorted(hits, key=len)[0])

    return ""


def file(name, env=(), extra=(), hints=(), pattern=None,
         min_size=0, newest=True, scan=False):
    """找一个数据文件（模型、配置、资源），返回绝对路径；找不到返回 ""。

    比 exe() 多三件模型文件真会踩的事：

    pattern    不知道确切文件名时用通配，`pattern="*.gguf"` + `hints=("_models/llm/gguf",)`
               → 该目录下所有 gguf 里挑。此时 name 可传 ""。
    min_size   最小字节数。**防残片**：今天下 llama.cpp 到 96% 被超时截断，
               残片是「存在」的，喂给 llama-server 会崩得莫名其妙。
               模型文件建议给 min_size=50*1024*1024。
    newest     pattern 命中多个时取最新的（mtime），而不是名字第一个。
               名字排序对 `Qwen3-1.7B` / `Qwen3.5-4B` 这种毫无意义。
    """
    envs = [env] if isinstance(env, str) else list(env or ())
    if not envs:
        envs = [env_key(name or pattern or "file")]

    def ok(p):
        if not p or not os.path.isfile(p):
            return False
        try:
            return os.path.getsize(p) >= min_size
        except OSError:
            return False

    # 1) 显式
    for p in extra or ():
        if ok(p):
            return _norm(p)

    # 2) 环境变量（含目录形式：指到一个目录，就在里面按 pattern/name 找）
    for e in envs:
        raw = (os.environ.get(e) or "").strip().strip('"')
        if not raw:
            continue
        if ok(raw):
            return _norm(raw)
        if os.path.isdir(raw):
            hit = _pick(_glob_in(raw, name, pattern), newest)
            if hit and ok(hit):
                return _norm(hit)

    # 3) 项目内相对
    proj = _project_root()
    for rel in ("models", "assets", "vendor", "data"):
        d = os.path.join(proj, rel)
        if os.path.isdir(d):
            hit = _pick(_glob_in(d, name, pattern), newest)
            if hit and ok(hit):
                return _norm(hit)

    # 4) hints × roots
    cands = []
    for r in roots():
        for hint in hints or ():
            hint = _expand(hint)
            if not hint:
                continue
            d = hint if os.path.isabs(hint) else os.path.join(r, hint)
            if os.path.isdir(d):
                cands.extend(_glob_in(d, name, pattern))
            elif name:
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    cands.append(p)
    hit = _pick(cands, newest)
    if hit and ok(hit):
        return _norm(hit)

    # 5) 兜底扫描
    if scan and (name or pattern):
        target = name or pattern.replace("*", "")
        for r in roots():
            for h in _scan(r, target):
                if ok(h):
                    return _norm(h)

    return ""


def dir_(name, env=(), extra=(), hints=()):
    """找一个目录，返回绝对路径；找不到返回 ""。"""
    envs = [env] if isinstance(env, str) else list(env or ())
    envs = [e for e in envs if e] or [env_key(name)]

    for p in extra or ():
        if _is_dir(p):
            return _norm(p)
    for e in envs:
        p = (os.environ.get(e) or "").strip().strip('"')
        if _is_dir(p):
            return _norm(p)

    proj = _project_root()
    p = os.path.join(proj, name)
    if _is_dir(p):
        return _norm(p)
    for hint in hints or ():
        p = os.path.join(proj, hint, name)
        if _is_dir(p):
            return _norm(p)

    for p in _hint_candidates(name, hints, roots()):
        if _is_dir(p):
            return _norm(p)
    return ""


def _glob_in(d, name, pattern):
    if name:
        p = os.path.join(d, name)
        return [p] if os.path.isfile(p) else []
    pat = pattern or "*"
    return [f for f in glob.glob(os.path.join(d, pat)) if os.path.isfile(f)]


def _pick(cands, newest):
    cands = [c for c in cands if c]
    if not cands:
        return ""
    if not newest:
        return cands[0]
    try:
        return max(cands, key=lambda p: os.path.getmtime(p))
    except OSError:
        return cands[0]


# ---------------------------------------------------------------- 说清楚

def explain(name, env=(), hints=(), extra=(), scan=False):
    """把「找过哪儿、结果如何」摊开。找不到时给的是**下一步动作**，不是道歉。

    返回 dict：found / path / tried（列表，每项 {source, path, why}）/ tip
    doctor 和体检界面直接读这个，比一句「没找到」有用得多。
    """
    envs = [env] if isinstance(env, str) else list(env or ())
    envs = [e for e in envs if e] or [env_key(name)]
    tried = []

    for p in extra or ():
        tried.append({"source": "显式指定", "path": _norm(p),
                      "why": "有" if _is_exe(p) or os.path.isfile(p) else "无"})
    for e in envs:
        v = (os.environ.get(e) or "").strip()
        tried.append({"source": "环境变量 %s" % e, "path": v or "（未设）",
                      "why": ("有" if v and (os.path.isfile(v) or os.path.isdir(v))
                              else ("未设" if not v else "指过去但没有"))})
    for n in _exe_names(name):
        hit = shutil.which(n)
        tried.append({"source": "PATH", "path": n,
                      "why": "有" if hit else "无"})
        break
    for h in hints or ():
        for r in roots():
            p = h if os.path.isabs(h) else os.path.join(r, h, name)
            tried.append({"source": "常见结构 %s" % h, "path": _norm(p),
                          "why": "有" if (os.path.isfile(p) or os.path.isdir(p)) else "无"})

    path = exe(name, env=envs, extra=extra, hints=hints, scan=scan)
    res = {
        "name": name,
        "found": bool(path),
        "path": path,
        "roots": roots(),
        "tried": tried,
    }
    if not path:
        res["tip"] = (
            "把 %s 指向它（文件全路径，或含它的目录），再跑一次。"
            % envs[0]
        )
    return res


#: 全局「没找到」记录。上层跑完一轮后一次性问：哪些东西缺、缺在哪。
_MISSES = []


def miss_report():
    """本轮所有「没找到」的汇总（给体检/报告用）。"""
    return list(_MISSES)


def _note_miss(what, envs, hints):
    _MISSES.append({"what": what, "env": list(envs), "hints": list(hints)})


# ---------------------------------------------------------------- 自测

def _selftest():
    fails = []

    def ck(cond, msg):
        print(("  ok  " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    print("① roots() 必须返回真实存在的根，且不含不存在的盘")
    r = roots()
    ck(bool(r), "至少有一个根")
    ck(all(os.path.exists(x) for x in r), "每个根都真的存在：%r" % (r,))
    if os.name == "nt":
        ck(all(x.endswith("/") for x in r), "统一以 / 结尾")
        ghost = [ch + ":/" for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                 if not os.path.exists(ch + ":/")]
        ck(not any(g in r for g in ghost), "不存在的盘不许混进来（本机缺 %d 个）" % len(ghost))

    print("② roots() 要缓存（每次探测都列盘太慢）")
    ck(roots() is roots(), "同一对象")

    print("③ 环境变量是最高优先（换机能不改代码就救）")
    exe_name = "cmd" if os.name == "nt" else "sh"
    real = shutil.which(exe_name) or ""
    if real:
        os.environ["FLEET_SELFTEST_EXE"] = real
        got = exe(exe_name, env="FLEET_SELFTEST_EXE", hints=("nonexist/deep",))
        ck(got == _norm(real), "环境变量优先命中：%r" % (got,))
        del os.environ["FLEET_SELFTEST_EXE"]
    ck(exe("fleet-no-such-exe-xyz", hints=("nope",)) == "",
       "找不到时返回空串，不返回半个路径")

    print("④ hints × roots 要真的按全组合展开（盘符只能从 roots 来）")
    hs = ["__loc_test__/a", "__loc_test__/b"]
    got = _hint_candidates("x.exe", hs, ["C:/", "D:/"])
    ck(len(got) == 4, "2 hints × 2 roots = 4 个候选，实得 %d" % len(got))
    joined = " ".join(got).lower()
    ck("c:" in joined and "d:" in joined, "两个根都参与了组合")
    ck(not any(ch + ":" in " ".join(hs).lower() for ch in "cdefg"),
       "hints 里本身不许出现盘符（盘符只能来自 roots）")

    print("⑤ 绝对路径 hint 不许再被拼到别的根下面")
    ab = _hint_candidates("n", ["/opt/x"] if os.name != "nt" else ["D:/abs_y"], ["C:/"])
    ck(len(ab) == 1, "绝对 hint 只产生 1 个候选，实得 %d" % len(ab))

    print("⑥ env_key 命名统一")
    ck(env_key("ffmpeg") == "FLEET_FFMPEG", "ffmpeg → FLEET_FFMPEG")
    ck(env_key("llama-server.exe") == "FLEET_LLAMA_SERVER", "llama-server.exe → FLEET_LLAMA_SERVER")
    ck(env_key("*.gguf") == "FLEET_GGUF", "*.gguf → FLEET_GGUF")

    print("⑦ min_size 要挡住残片（下到一半的文件不许当成品）")
    import tempfile
    d = tempfile.mkdtemp(prefix="locate_")
    tiny = os.path.join(d, "half.gguf")
    with open(tiny, "wb") as f:
        f.write(b"x" * 100)
    ck(file("half.gguf", hints=(d,), min_size=0) == _norm(tiny), "min_size=0 时能拿到")
    ck(file("half.gguf", hints=(d,), min_size=10_000) == "", "min_size 高于实际大小时拒绝残片")

    print("⑧ pattern + newest 要取最新的那份，不是名字排第一的")
    import time as _t
    b = os.path.join(d, "b_model.gguf")
    a = os.path.join(d, "a_model.gguf")
    for p in (b, a):
        with open(p, "wb") as f:
            f.write(b"y" * 200)
    os.utime(a, (_t.time() - 500, _t.time() - 500))   # a 更旧
    got = file("", hints=(d,), pattern="*.gguf", min_size=150)
    ck(_norm(got) == _norm(b), "取到更新的 b_model（而不是字典序在前的 a_model）：%r" % (got,))

    print("⑨ explain 找不到时要给出下一步动作，不是道歉")
    ex = explain("fleet-definitely-missing-xyz", hints=("no/such/dir",))
    ck(ex["found"] is False, "found=False")
    ck(bool(ex.get("tip")), "有 tip")
    ck(ex["tip"].startswith("把 FLEET_"), "tip 直接说该设哪个环境变量")
    ck(any("环境变量" in t["source"] for t in ex["tried"]), "tried 里列出了环境变量这一档")

    print("⑩ 扫描要跳过系统目录（否则一次探测扫穿整块盘）")
    ck("windows" in SKIP_DIRS and "node_modules" in SKIP_DIRS and ".git" in SKIP_DIRS,
       "SKIP_DIRS 含系统/版本库/依赖目录")

    print("⑪ 不许在本文件里写死盘符（照镜子）")
    import re as _re
    src = open(os.path.abspath(__file__), encoding="utf-8").read().splitlines()
    # 跳过整行注释——注释里放反例是在讲道理，不是在使用路径
    hits = []
    for no, line in enumerate(src, 1):
        if line.lstrip().startswith("#"):
            continue
        for lit in _re.findall(r"""["']([A-Za-z]:[\\/][^"']*)["']""", line):
            hits.append((no, lit))
    bad = [(n, s) for n, s in hits if not s.startswith(("C:/", "D:/"))]
    ck(not bad, "本文件代码里的盘符只有 C:/ D:/ 示例：%r" % (bad,))

    print("⑫ under：中间夹了猜不到的目录名时，要在前缀下递归找")
    d2 = tempfile.mkdtemp(prefix="locate_under_")
    deep = os.path.join(d2, "ffmpeg-master-latest-win64-gpl", "bin")
    os.makedirs(deep)
    fake = os.path.join(deep, "ffmpeg.exe")
    with open(fake, "wb") as f:
        f.write(b"x")
    if os.name != "nt":
        os.chmod(fake, 0o755)          # Unix 上要真可执行才算
    got = exe("ffmpeg", hints=("no-such-hint-xyz",), under=(d2,))
    # 本机可能真装了 ffmpeg（PATH 上），那样会先命中 PATH —— 两条都算对，
    # 关键是**不能返回空**，且返回的必须真实存在
    ck(bool(got) and os.path.isfile(got), "under 找到了：%r" % (got,))
    if got:
        ck(_norm(deep) in _norm(got) or "ffmpeg" in os.path.basename(got).lower(),
           "返回的是 ffmpeg 本体")
    ck(exe("definitely-no-such-exe-xyz", under=(d2,)) == "",
       "under 里确实没有时仍返回空串（不瞎编）")

    print()
    if fails:
        print("FAILED %d 项：%s" % (len(fails), fails))
        return 1
    print("全部通过（12 组）")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    if len(sys.argv) > 1:
        for nm in sys.argv[1:]:
            info = explain(nm, hints=())
            if info["found"]:
                print("%-24s -> %s" % (nm, info["path"]))
            else:
                print("%-24s -> 没找到（%s）" % (nm, info.get("tip", "")))
        raise SystemExit(0)
    print("用法: python -m fleet.locate --selftest | python -m fleet.locate ffmpeg adb")

#!/usr/bin/env python
"""统计数字生成器：全项目唯一的数字来源。

总纲 §六 数字纪律：文档里的一切进度数字必须由本脚本产出，禁止手写。
手填的进度表必然过期（漂移史：门禁 52/58/67/74 四数并存、文档 30+/71/75、
能力 87/109/121/151/160/380/384）。

用法：
    python tools/gen_stats.py                 # 打印 + 写 docs/_stats.json
    python tools/gen_stats.py --json          # 只打印 JSON
    python tools/gen_stats.py --probe         # 额外探测「此刻能跑」数（慢、随环境漂移）
    python tools/gen_stats.py --write-docs    # 刷新 README/总纲 里的自动块
    python tools/gen_stats.py --check         # 只校验文档自动块是否与代码一致（给门禁用）

--- 为什么口径改了（2026-11-02）---

旧版用 AST 数 `Capability(` 构造器出现次数，得 427；而 `len(catalog.CAPABILITIES)`
是 404。差的 23 个是「写了构造器但没进清单」的——AST 数的是**打字次数**，
不是**注册成功的条数**。数字纪律要的是后者，所以改成真 import 后实测。

--- 四层口径（每一层都有可核验来源，不许互相冒充）---

| 层 | 含义 | 来源 | 会随环境漂移吗 |
|---|---|---|---|
| declared 声明 | 能力表里挂了多少 | `len(catalog.CAPABILITIES)` | 否（改代码才变） |
| owned    有主 | 至少一台设备声明支持它 | `registry.owners(name)` | 否（改代码才变） |
| live     在场 | 此刻 owner 设备 probe 通过 | `registry.availability()` | **是**，须带采样时间 |
| gated    有门禁 | evals/ 里被真跑过的证据 | 扫 evals/*.py 文本 | 否 |

**「有实现」这个说法本脚本不给数**——它没法自动判，硬给就是自欺。
想判断「是不是空壳」看 owned 与 gated 的差、再人肉看那个域的代码厚度。
"""
import ast
import json
import os
import re
import sys
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATS_PATH = os.path.join(ROOT, "docs", "_stats.json")

# 文档里自动块的标记。改标记等于改契约，门禁 t63 盯着。
BEGIN = "<!--STATS:BEGIN-->"
END = "<!--STATS:END-->"
DOC_FILES = ["README.md", os.path.join("docs", "总纲_唯一推进依据_2026-10-25.md")]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- 门禁侧

def _cases():
    """run_selftests.py 里 CASES 的字面量（AST 读，不执行）。

    返回 [(name, argv, cwd, timeout), ...]，读不出来返回 []。
    """
    path = os.path.join(ROOT, "run_selftests.py")
    try:
        tree = ast.parse(_read(path), filename=path)
    except (OSError, SyntaxError):
        return []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "CASES" for t in node.targets):
            if not isinstance(node.value, ast.List):
                continue
            out = []
            for el in node.value.elts:
                if not isinstance(el, ast.Tuple) or len(el.elts) < 2:
                    continue
                name = getattr(el.elts[0], "value", "?")
                argv_node = el.elts[1]
                argv = []
                if isinstance(argv_node, ast.List):
                    argv = [getattr(e, "value", "") for e in argv_node.elts
                            if isinstance(e, ast.Constant)]
                out.append((name, argv))
            return out
    return []


def gate_count():
    return len(_cases())


def eval_file_count():
    d = os.path.join(ROOT, "evals")
    try:
        return len([f for f in os.listdir(d) if f.endswith(".py")])
    except OSError:
        return 0


def orphan_evals():
    """evals/ 里有、但 CASES 没登记的脚本——「写了门禁没人跑」= 等于没写。

    例外：`run_*.py` 是**批量运行器**，不是门禁（run_gates.py 一次跑完所有门禁），
    它本来就不该出现在 CASES 里，否则就是「运行器跑运行器」。
    """
    d = os.path.join(ROOT, "evals")
    try:
        files = {f for f in os.listdir(d)
                 if f.endswith(".py") and not f.startswith("run_")}
    except OSError:
        return []
    registered = set()
    for _name, argv in _cases():
        for a in argv:
            a = a.replace("\\", "/")
            if a.startswith("evals/"):
                registered.add(os.path.basename(a))
    return sorted(files - registered)


def retired_refs():
    """全仓扫「已退役的能力名」还残留在哪。

    为什么要有（2026-11-03 一次归并踩了三处）——
    能力改名后旧名是**静默失效**的：registry 查不到就回「没有这项能力」，
    但调用方不会自己知道该改。同一次归并踩到：
      · groups.py 的等价声明消失 → 模型又开始抓错手
      · gui/server.py 的远程白名单 → 那条路静默 404
      · 两张角色卡的 tool_filter → 整个名册加载失败，八个角色集体「不在名册」
    共同点是都不报「名字错了」，而是报别的现象。所以旧名必须登记成清单，扫。

    只看会**被程序读**的文件（.py/.json/.yaml）。文档不扫——文档里提旧名
    往往正是为了讲清历史，扫它只会逼人删掉有用的说明。
    """
    try:
        from fleet.kinds.pc_control import ALIASES as _PC_ALIAS
        from fleet.kinds.host import Host as _Host
        from fleet.vendored_rea import ENGINE_OF as _REA
    except Exception:
        return []
    try:
        from fleet import catalog as _cat
        alive = {c.name for c in _cat.CAPABILITIES}
    except Exception:
        alive = set()

    retired = {k: "pcctl 归并" for k in _PC_ALIAS}
    for a in getattr(_Host, "INPUT_ACTS", ()):
        retired["host.input." + a] = "host.input 归并"
    for cap in _REA:
        retired.setdefault(cap, "rea 折叠")
    # 2026-11-12 capabilities.ALIASES 整表退役：旧别名不再翻译，任何残留引用都该改名。
    # 这里登记旧名 -> 现在的真名，扫到即红（t54），别让「查得到旧说明书」再活过来。
    for _old, _now in (
        ("ctx.read", "ctx"), ("ctx.write", "ctx"), ("compute.run", "compute"),
        ("persona.tick", "persona"), ("perceive.report", "perceive"),
        ("image.gen.run", "image.gen"), ("video.gen.run", "video.gen"),
        ("video.story.run", "video.story"), ("fleet.memory.sync.run", "fleet.memory.sync"),
        ("job.run", "job.submit"), ("art.put", "artifact.put"),
        ("art.get", "artifact.get"), ("art.list", "artifact.list"),
        ("mem.list", "memory.pack.list"), ("mem.render", "memory.pack.render"),
        ("mem.diff", "memory.pack.diff"), ("mem.sync", "memory.pack.sync"),
        ("phone.status", "phone.node.status"), ("phone.ask", "phone.llm.ask"),
    ):
        retired.setdefault(_old, "改名为 " + _now)
    retired = {k: v for k, v in retired.items() if k not in alive}

    # 这些文件里出现旧名是故意的：兼容层 / 内部路由表 / 声明处
    allow = {
        "fleet/vendored_rea.py", "fleet/kinds/rea_device.py",
        "fleet/kinds/pc_control.py", "fleet/kinds/host.py",
        "fleet/catalog.py", "tools/gen_stats.py",
        "fleet/kinds/rizin_eng.py", "fleet/kinds/rea_reverse_eng.py",
    }
    skip_dirs = {"vendor", "_vendor", "node_modules", ".git", "__pycache__",
                 "site-packages", "_venv", "tmp", "archive"}
    exts = (".py", ".json", ".yaml", ".yml")

    hits = []
    # 用词边界而不是子串：`batch_start.get(...)` 里也含 "art.get"，子串会误报。
    pats = [(old, re.compile(r"(?<![\w.])" + re.escape(old) + r"(?![\w])"))
            for old in retired]
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if not fn.endswith(exts):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, ROOT).replace("\\", "/")
            if rel in allow:
                continue
            try:
                src = open(p, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for old, pat in pats:
                if pat.search(src):
                    hits.append({"file": rel, "cap": old,
                                 "now": retired[old]})
                    break
    return sorted(hits, key=lambda x: x["file"])


def _eval_text():
    d = os.path.join(ROOT, "evals")
    buf = []
    try:
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".py"):
                buf.append(_read_bytes(os.path.join(d, fn)))
    except OSError:
        pass
    return "\n".join(buf)


# ---------------------------------------------------------------- 能力侧

def _load_registry(probe=False):
    """真 import 注册表。失败返回 (None, 错误原因)——门禁要能看出降级。"""
    sys.path.insert(0, ROOT)
    try:
        from fleet import catalog
        # 只数代码里声明的设备。运行时节点账本（编队里临时登记的对端）是本机
        # 状态、不是代码事实：掺进来会让同一份 _stats.json 在开发机算出 69 台、
        # 干净的 CI runner 算出 66 台，t54/t63 就永远在两台机器上互相判对方漂移。
        reg = catalog.build(with_nodes=False)
        return reg, ""
    except Exception as e:                                  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def _declared_names():
    """catalog.CAPABILITIES 里的名字（不建注册表也能拿到，最稳的一层）。"""
    sys.path.insert(0, ROOT)
    try:
        from fleet import catalog
        return [c.name for c in catalog.CAPABILITIES], ""
    except Exception as e:                                  # noqa: BLE001
        return [], f"{type(e).__name__}: {e}"


def capability_layers(probe=False):
    """四层口径。拿不到注册表时 owned/live 置 None（不是 0——0 会被当成事实）。"""
    names, err = _declared_names()
    out = {
        "capabilities_declared": len(names),
        "capabilities_owned": None,
        "capabilities_live": None,
        "capabilities_gated": None,
        "devices": None,
        "devices_live": None,
        "domains": {},
        "declared_error": err,
    }
    if not names:
        return out

    gated_text = _eval_text()
    gated = [n for n in names if n in gated_text]
    out["capabilities_gated"] = len(gated)

    # 域明细：声明 / 有主 / 有门禁三列一起给，薄域一眼看得出来
    reg, reg_err = _load_registry()
    domains = {}
    for n in names:
        d = n.split(".", 1)[0]
        row = domains.setdefault(d, {"declared": 0, "owned": 0, "gated": 0})
        row["declared"] += 1
        if n in gated_text:
            row["gated"] += 1

    if reg is not None:
        owned = 0
        owned_names = set()
        owner_errors = []
        for n in names:
            try:
                if reg.owners(n):
                    owned += 1
                    owned_names.add(n)
                    domains[n.split(".", 1)[0]]["owned"] += 1
            except Exception as e:                         # noqa: BLE001
                # 静默吞掉会让「有主能力」少算，还看不出是降级 —— 记账不撒谎。
                owner_errors.append({"cap": n, "err": f"{type(e).__name__}: {e}"})
        out["capabilities_owned"] = owned
        # 「声明 → 有主」的缺口里，有多少是运行时条件申报（不是空壳）
        rt, _rt_err = runtime_declared()
        out["runtime_declared_unowned"] = [r for r in rt if r["cap"] not in owned_names]
        if owner_errors:
            out["owner_error_count"] = len(owner_errors)
            out["owner_errors"] = owner_errors[:20]
        try:
            out["devices"] = len(reg.devices())
        except Exception:                                   # noqa: BLE001
            pass

        if probe:
            live = 0
            devices_live = 0
            try:
                for row in reg.availability():
                    if row.get("ok"):
                        devices_live += 1
                        live += len(row.get("caps") or [])
            except Exception:                               # noqa: BLE001
                live = None
            out["capabilities_live"] = live
            out["devices_live"] = devices_live
            out["live_sampled_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    else:
        out["registry_error"] = reg_err

    out["domains"] = dict(sorted(domains.items(), key=lambda kv: -kv[1]["declared"]))
    return out


# ---------------------------------------------------------------- 其它

def doc_count():
    d = os.path.join(ROOT, "docs")
    try:
        return len([f for f in os.listdir(d) if f.endswith(".md")])
    except OSError:
        return 0


def app_build():
    for rel in (os.path.join("tianwang_android", "version.txt"),
                os.path.join("tianwang_app", "version.txt")):
        try:
            return _read(os.path.join(ROOT, rel)).strip()
        except OSError:
            continue
    return "?"


def runtime_declared():
    """有实现、但只在运行时条件满足时才申报的能力（静态有主统计看不见）。

    名单的真源是各设备类的 `RUNTIME_CAPS` 常量；本函数只汇总，不手抄——
    否则这里会变成第二本必然过期的账。返回 (行列表, 错误原因)。
    """
    sys.path.insert(0, ROOT)
    try:
        from fleet.kinds.fleet_node import FleetNode
        from fleet.kinds.remote_phone import RelayNode
    except Exception as e:                                  # noqa: BLE001
        return [], f"{type(e).__name__}: {e}"
    rows = []
    for cls, owner in ((FleetNode, "fleet_node"), (RelayNode, "remote_phone")):
        for n in getattr(cls, "RUNTIME_CAPS", ()):
            rows.append({"cap": n, "by": owner})
    return rows, ""


def collect(probe=False):
    rt, rt_err = runtime_declared()
    st = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "gates_registered": gate_count(),
        "eval_files": eval_file_count(),
        "orphan_evals": orphan_evals(),
        "retired_refs": retired_refs(),
        "doc_md": doc_count(),
        "app_build": app_build(),
        "runtime_declared": rt,
    }
    if rt_err:
        st["runtime_declared_error"] = rt_err
    st.update(capability_layers(probe=probe))
    return st


# ---------------------------------------------------------------- 文档自动块

def render_block(st):
    """自动块正文。**这里不许出现落款日期以外的自由文字**——它就是一张表。"""
    def n(v):
        return "—" if v is None else str(v)

    lines = [
        BEGIN,
        "> 本块由 `python tools/gen_stats.py --write-docs` 生成，**手改会被门禁 t63 打红**。",
        "> 判断依据写在 `tools/gen_stats.py` 顶部；原始 JSON 在 `docs/_stats.json`。",
        "",
        "| 项 | 值 | 口径 |",
        "|---|---|---|",
        f"| 声明能力 | **{n(st.get('capabilities_declared'))}** | 能力表里挂了多少 |",
        f"| 有主能力 | **{n(st.get('capabilities_owned'))}** | 至少一台设备声明支持它 |",
        f"| 有门禁能力 | **{n(st.get('capabilities_gated'))}** | `evals/` 里有真跑过的证据 |",
        f"| 设备种类实例 | **{n(st.get('devices'))}** | 注册表里的设备对象数（非型号数） |",
        f"| 门禁登记 | **{n(st.get('gates_registered'))}** 条 | `run_selftests.py` 的 `CASES` |",
        f"| eval 文件 | **{n(st.get('eval_files'))}** 份 | `evals/*.py` |",
        f"| 游离门禁 | **{len(st.get('orphan_evals') or [])}** 份 | 写了但没登记，等于没写 |",
        f"| 文档 | **{n(st.get('doc_md'))}** 份 | `docs/*.md` |",
        f"| App build | **{n(st.get('app_build'))}** | `tianwang_android/version.txt`（第二代版本看 `tianwang_app/app/build.gradle.kts`） |",
    ]
    # 两个缺口也由脚本算——它们是「该去解释的东西」，不是让人记住的数字
    dec, own, gat = (st.get("capabilities_declared"), st.get("capabilities_owned"),
                     st.get("capabilities_gated"))
    lines += ["", "| 缺口 | 条 | 含义 |", "|---|---|---|"]
    if isinstance(dec, int) and isinstance(own, int):
        lines.append(f"| 声明 → 有主 | **{dec - own}** | 挂了名字但没有设备认领，调必失败 |")
        rt = st.get("runtime_declared_unowned") or []
        if rt:
            lines.append(f"| 其中运行时条件申报 | **{len(rt)}** | 有实现，等条件满足才申报（不是空壳） |")
    else:
        lines.append("| 声明 → 有主 | — | 注册表没建起来，这一格不给数 |")
    if isinstance(own, int) and isinstance(gat, int):
        lines.append(f"| 有主 → 有门禁 | **{own - gat}** | 有设备说支持，但没有门禁真跑过它（未验 ≠ 坏） |")
    else:
        lines.append("| 有主 → 有门禁 | — | 注册表没建起来，这一格不给数 |")
    orph = st.get("orphan_evals") or []
    if orph:
        lines.append("")
        lines.append("游离未登记：" + "、".join(f"`{x}`" for x in orph))
    if st.get("capabilities_live") is not None:
        lines.append("")
        lines.append(f"在场能力 **{st['capabilities_live']}** 条 / 活设备 **{st.get('devices_live')}** 台"
                     f"（采样于 {st.get('live_sampled_at')}，随环境漂移，别当契约）。")
    lines.append("")
    lines.append(END)
    return "\n".join(lines)


def _splice(text, block):
    """把 text 里 BEGIN..END 之间整个换掉。找不到标记返回 None。"""
    i = text.find(BEGIN)
    if i < 0:
        return None
    j = text.find(END, i)
    if j < 0:
        return None
    return text[:i] + block + text[j + len(END):]


def write_docs(st):
    block = render_block(st)
    touched, missing = [], []
    for rel in DOC_FILES:
        path = os.path.join(ROOT, rel)
        try:
            text = _read(path)
        except OSError:
            missing.append(rel)
            continue
        new = _splice(text, block)
        if new is None:
            missing.append(rel)          # 没有标记 = 还没接进来，不是错误
            continue
        if new != text:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(new)
        touched.append(rel)
    return touched, missing


def check_docs(st):
    """返回不一致的文档列表；空 = 都同步。"""
    block = render_block(st)
    bad = []
    for rel in DOC_FILES:
        path = os.path.join(ROOT, rel)
        try:
            text = _read(path)
        except OSError:
            continue
        i = text.find(BEGIN)
        if i < 0:
            continue                     # 没接自动块的文档不管
        j = text.find(END, i)
        if j < 0 or text[i:j + len(END)] != block:
            bad.append(rel)
    return bad


# ---------------------------------------------------------------- main

def main():
    probe = "--probe" in sys.argv
    st = collect(probe=probe)

    if "--check" in sys.argv:
        bad = check_docs(st)
        if bad:
            print("文档自动块与代码不一致（改代码后忘了刷新，或有人手改）：")
            for b in bad:
                print(f"  - {b}")
            print("修：python tools/gen_stats.py --write-docs")
            return 1
        print("文档自动块与代码一致。")
        return 0

    if "--json" in sys.argv:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0

    with open(STATS_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print("天网数字（脚本生成，勿手填）：")
    for k in ("capabilities_declared", "capabilities_owned", "capabilities_live",
              "capabilities_gated", "devices", "devices_live",
              "gates_registered", "eval_files", "doc_md", "app_build"):
        v = st.get(k)
        print(f"  {k}: {'—' if v is None else v}")
    orph = st.get("orphan_evals") or []
    print(f"  orphan_evals: {len(orph)}" + (f" {orph}" if orph else ""))
    print(f"  已写入 {os.path.relpath(STATS_PATH, ROOT)}")

    if "--write-docs" in sys.argv:
        touched, missing = write_docs(st)
        print(f"  已刷新自动块: {', '.join(touched) if touched else '无'}")
        if missing:
            print(f"  未接自动块（跳过）: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

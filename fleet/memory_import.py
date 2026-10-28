"""记忆导入器：把「我写过的事」变成可回查的证据条目。

为什么要有这个（2026-10-22 实测）：
    技能进化要原料。但本仓的原料有两个来源，强度天差地别：
      · 能力真的跑过  （`exec:` 证据）—— 强
      · 记忆里记过     （`mem:`  证据）—— 弱
    而 2026-10-22 实测：journal 187 条里 `exec` 类证据 **0 条**，
    全仓的 `done` 也是 0 条 ⇒ 只靠执行证据，技能进化今天起不了步。

    记忆日志是**真实发生过的事**的书面沉淀（带日期、带踩坑记录、带实测数字）。
    它不是执行凭据，但它是「事实陈述」——比编的强，比执行凭据弱。
    所以导入时必须：
      ① 标成 `mem:` 级，**不许冒充** `exec:` 级；
      ② 每条都带原文件的行号，随时能回查原文；
      ③ 导入的是**原文摘录**，不许改写（改写 = 编造）。

    这就是「用我自己的一部分当测试」：WorkBuddy 自己的 39 个日志 + 8 个项目分册
    就是现成的、真实的、有日期的语料。

诚实边界（写在这里，防止以后自己骗自己）：
    导入的记忆**不能**证明「某个能力当时真的跑过」。
    它只能证明「当时记下了这么一件事」。技能验证门据此分级：
    一张技能至少要有一条 `exec:` 强证据，记忆只做补充。
"""
import os
import re
import sys

import journal as J


MEM_PREFIX = "mem:"

# 证据库的默认文件名。
# 为什么单独一个库，不塞进 fleet_journal.db：
#   两个库寿命与容量语义不同。
#   · fleet_journal.db = 对话与事件流水，cap=5000（"流水不是档案柜"）
#   · fleet_evidence.db = 机器可验证的证据（exec + mem），要长期留着给技能引用
#   混在一起的后果很具体：记忆条目用**当下时间**写入，1 万条记忆会立刻占满
#   5000 的上限，把仅存的 187 条真实对话流水全砍掉。证据把对话挤没了，
#   比没有证据更糟。
DEFAULT_DB = "fleet_evidence.db"

# 记忆根目录候选（按顺序找第一个存在的）
# 记忆根目录候选（**顺序不代表优先级**，最终按内容特征挑，见 _pick_memory_dir）
# 🔴 2026-10-29：原来这里第一行写死 `D:\.workbuddy\memory`，靠「第一个存在的」选中。
#    本机实测两处**都存在**却是两回事：
#      D:\.workbuddy\memory        天网项目记忆（10-23~10-28 连续日期 + MEMORY.md）
#      C:\Users\admin\.workbuddy\memory  WorkBuddy 用户级记忆（8 月散日志 + 会话 md）
#    靠位置/顺序永远选不准，只能靠内容判——谁的日志离现在最近，谁就是当前在用的。
DEFAULT_DIRS = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 ".workbuddy", "memory"),                      # ① 跟着仓库走
    os.path.join(os.path.expanduser("~"), ".workbuddy", "memory"),  # ② 标准位置
]
#: 历史布局：老机器把记忆放在 D 盘。仍然认，但不许它靠位置抢先。
_LEGACY_DIRS = [r"D:\.workbuddy\memory"]

#: 记忆目录里日期日志的文件名形状
_DATE_FILE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.md$")


def _activity(d):
    """这个目录「有多活跃」——里面最新那篇 YYYY-MM-DD.md 的日期。没有就 None。

    用日期文件而不是 mtime：mtime 会因为备份、解压、rsync 整体刷新，
    日期文件名则是写日志的人自己敲的，更接近「这份记忆活到哪天」。
    """
    try:
        names = os.listdir(d)
    except OSError:
        return None
    best = None
    for n in names:
        m = _DATE_FILE.match(n)
        if m:
            try:
                got = "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
            except Exception:
                continue
            if best is None or got > best:
                best = got
    return best


def _pick_memory_dir(explicit=None):
    """挑一个记忆目录。返回 (路径, 说明) ；都不可用返回 (None, "")。

    判定顺序：
        1. 显式给的（--dir / FLEET_MEMORY_DIR）—— 人说哪就是哪，不猜
        2. 候选里「有没有日期日志」→ 全是空目录就退回第一个存在的
        3. 有日期日志的按**最新日期**排序，取最近的

    第 3 步是关键：本机两个目录都在，但一个是项目的（10-28）、一个是
    用户级的（8 月）。按顺序取第一个会读错源，按最近日期取不会。
    """
    cands = []
    for c in ([explicit] if explicit else []) + \
             [os.environ.get("FLEET_MEMORY_DIR", "").strip()] + \
             DEFAULT_DIRS + _LEGACY_DIRS:
        if c and os.path.isdir(c) and c not in cands:
            cands.append(c)
    if not cands:
        return None, ""

    scored = [(d, _activity(d)) for d in cands]
    alive = [(d, a) for d, a in scored if a]
    if alive:
        alive.sort(key=lambda x: x[1], reverse=True)   # 日期大的在前
        best, when = alive[0]
        if len(alive) > 1:
            return best, "按最新日志 %s 选中（另有 %d 个候选，最新的更旧）" % (
                when, len(alive) - 1)
        return best, "最新日志 %s" % when

    # 一个日期日志都没有：退回第一个存在的，但要说清楚这是猜的
    return cands[0], "没有日期日志，按顺序取了第一个存在的"

# 单条最大长度（记忆条目普遍很长，落盘只留可读片段；全文回原文查）
MAX_ITEM_CHARS = 500

# 太短的条目不入库（"---"、"ok" 这类没有信息量）
MIN_ITEM_CHARS = 12

_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(.*\S)\s*$")
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_TABLE_RE = re.compile(r"^\s*\|")
_FENCE_RE = re.compile(r"^\s*```")


def _strip_code_fence(lines):
    """去掉代码块内部（``` 之间的行）—— 那是命令，不是结论。

    **保留原行号**：返回 [(原行号, 行文本)]。
    过滤后再 enumerate 会让行号整体前移，溯源就会指到原文的错地方——
    证据指向别的行，比没有证据更坏。
    """
    out = []
    inside = False
    for i, ln in enumerate(lines, start=1):
        if _FENCE_RE.match(ln):
            inside = not inside
            continue
        if not inside:
            out.append((i, ln))
    return out


def parse_items(text):
    """从一份 markdown 里抽出条目。返回 [(标题, 条目文本, 起行号)]。

    规则（刻意保守，宁可少抽也不要抽错）：
      · 列表项 `- ` / `* ` / `1. ` 才算一条；
      · 最近的 `##` 标题作为条目的归属标题；
      · 表格行、代码块、HTML 注释一律跳过；
      · 空标题时用文件里的第一个 `#` 标题兜底。
    """
    lines = _strip_code_fence(text.splitlines())
    out = []
    cur_head = ""
    for i, ln in lines:
        m = _HEAD_RE.match(ln)
        if m:
            cur_head = m.group(2)
            continue
        if _TABLE_RE.match(ln):
            continue
        m = _LIST_RE.match(ln)
        if not m:
            continue
        body = m.group(1).strip()
        if len(body) < MIN_ITEM_CHARS:
            continue
        out.append((cur_head, body, i))
    return out


def _iter_files(root, only=None):
    """产出 (绝对路径, 相对路径)。相对路径用于溯源。"""
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in sorted(dirnames) if not d.startswith(".")]
        for fn in sorted(filenames):
            if not fn.endswith(".md"):
                continue
            ap = os.path.join(dirpath, fn)
            rel = os.path.relpath(ap, root).replace("\\", "/")
            if only and not any(pat in rel for pat in only):
                continue
            yield ap, rel


def existing_traces(j):
    """已经导入过的 trace（幂等用）。"""
    return {r["trace"] for r in j.recent(hours=10 ** 9, limit=100000)
            if r["trace"] and r["trace"].startswith(MEM_PREFIX)}


def import_dir(root, j, only=None, max_items=0, dry=False):
    """把一个记忆目录导入成 `mem:` 级证据条目。

    返回统计 dict。幂等：同一个 (文件, 行号) 不会导入两次。
    """
    seen = existing_traces(j)
    stats = {"files": 0, "items": 0, "skipped_dup": 0, "too_short": 0, "written": 0}
    if not os.path.isdir(root):
        return dict(stats, error="记忆目录不存在：%s" % root)

    for ap, rel in _iter_files(root, only):
        stats["files"] += 1
        try:
            with open(ap, "r", encoding="utf-8") as f:
                text = f.read()
            # ts 用**文件修改时间**，不用导入时刻。
            # 理由：证据的时间必须说「这件事什么时候发生的」，
            # 用导入时刻等于把所有历史压到今天，以后没法按时间回溯，
            # 环切时也分不清哪条是陈年旧账。
            mtime = os.path.getmtime(ap)
        except Exception:
            continue
        items = parse_items(text)
        if max_items:
            items = items[:max_items]
        for head, body, line in items:
            stats["items"] += 1
            trace = "%s%s#L%d" % (MEM_PREFIX, rel, line)
            if trace in seen:
                stats["skipped_dup"] += 1
                continue
            seen.add(trace)
            snippet = body[:MAX_ITEM_CHARS]
            if head:
                snippet = "[%s] %s" % (head, snippet)
            if dry:
                stats["written"] += 1
                continue
            j.log("see", "workbuddy-memory", snippet,
                  topic="mem:%s" % rel, trace=trace, status=J.DONE_STATUS, ts=mtime)
            stats["written"] += 1
    return stats


def _selftest():
    """离线自测：解析、幂等、溯源、打码。"""
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    d = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(d, "project"))
        with open(os.path.join(d, "2026-10-22.md"), "w", encoding="utf-8") as f:
            f.write("# 2026-10-22\n\n"
                    "## 15:00 某次实测\n\n"
                    "- 实测结论：journal 187 条里 done 是 0 条，这个数字是数出来的不是猜的\n"
                    "- 短\n"
                    "| 表格 | 行 |\n"
                    "```\n"
                    "python -c 'x=1'\n"
                    "```\n"
                    "- 带密钥的行 --password hunter2 不该落明文\n")
        with open(os.path.join(d, "project", "天网.md"), "w", encoding="utf-8") as f:
            f.write("# 天网\n\n- 设备层门禁 58/58 通过，这个数字来自 p1_gate 实跑\n")

        j = J.Journal(path, cap=500)
        try:
            st = import_dir(d, j)
            assert st["files"] == 2, st
            assert st["written"] == 3, st        # 短条目与表格、代码块不算
            rows = j.recent(hours=10 ** 9)
            assert len(rows) == 3, rows
            assert all(r["status"] == J.DONE_STATUS for r in rows)
            assert all(r["kind"] == "see" for r in rows)
            assert all(r["trace"].startswith("mem:") for r in rows)
            # 溯源：能指回具体文件行（行号必须是**原文件**的行号：
            # 前面有代码块被跳过，条目仍在原行位置）
            tr = {r["trace"] for r in rows}
            assert "mem:2026-10-22.md#L5" in tr, tr
            assert "mem:2026-10-22.md#L11" in tr, tr
            assert "mem:project/天网.md#L3" in tr, tr
            # 幂等：再导一次不新增
            st2 = import_dir(d, j)
            assert st2["written"] == 0 and st2["skipped_dup"] == 3, st2
            assert j.count() == 3
            # 打码：密码不落明文
            all_body = " ".join(r["body"] for r in j.recent(hours=10 ** 9))
            assert "hunter2" not in all_body, all_body
            assert "已打码" in all_body, all_body
            print("[selftest] 解析（只取列表项，跳表格/代码块/短条目） OK")
            print("[selftest] 幂等（重复导入不新增） OK")
            print("[selftest] 溯源（trace 指到文件+行号） OK")
            print("[selftest] 密码打码（不落明文） OK")
            print("[selftest] PASS")
        finally:
            j.close_connection()
    finally:
        if os.path.exists(path):
            os.remove(path)
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def _main(argv):
    args = list(argv)
    root = None
    only = None
    max_items = 0
    dry = False
    db = DEFAULT_DB
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--dir":
            i += 1
            root = args[i]
        elif a == "--files":
            i += 1
            only = args[i].split(",")
        elif a == "--max-items":
            i += 1
            max_items = int(args[i])
        elif a == "--db":
            i += 1
            db = args[i]
        elif a == "--dry":
            dry = True
        elif a in ("-h", "--help"):
            print("用法：python -m fleet.memory_import [--dir <记忆目录>] "
                  "[--files a,b] [--max-items N] [--db <journal.db>] [--dry]")
            return 0
        i += 1
    picked, why = _pick_memory_dir(root)
    if picked is None:
        print("找不到记忆目录（--dir 指定，或 set FLEET_MEMORY_DIR=...）")
        return 2
    root = picked

    j = J.Journal(db, cap=100000)
    try:
        st = import_dir(root, j, only=only, max_items=max_items, dry=dry)
    finally:
        j.close_connection()
    print("记忆根目录：%s（%s）" % (root, why))
    print("文件 %d 个 / 抽到条目 %d 条 / 新写入 %d 条 / 重复跳过 %d 条" % (
        st["files"], st["items"], st["written"], st["skipped_dup"]))
    if st.get("error"):
        print("错误：%s" % st["error"])
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(_main(sys.argv[1:]))

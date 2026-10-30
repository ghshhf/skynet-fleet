# -*- coding: utf-8 -*-
"""门禁：代码里不许写死盘符。

为什么有这条（2026-10-29 主人点出来的）
--------------------------------------
「你把一些代码绑定到 D 盘之类的，就是类似于硬绑定。改成软绑定，只要有这个东西就行了。」

原话听着像是「D 盘路径写错了」，但根子不在盘符字母上，在**表达方式**上：
同一份目录结构在三个盘上就变成三份硬编码，写四份漏两份，加到第六个盘再改一遍。
踩过的原始证据：

    start-decider.bat    G:\\_downloads\\llamacpp-b11840\\  + G:\\_models\\...   (18080 决策)
    start_llm_hidden.py  E:\\_tools\\llama.cpp-b11483\\bin\\ + E:\\_train_qwen06b\\  (8080 对话)
    tianwang.bat         同一套又抄一遍                                    (8080 对话)
    voice.py 里找 ffmpeg  E:/dev · E:/_tools · D:/tools · G:/dev  ← 2盘×2目录=4组合，只列了4条

换到只有 C/D 盘的机器，这几条**全废，而且废得无声无息**：文件不在，
脚本只是"起不来"，界面上表现为"助理什么都不会用"。

解决办法在 `fleet/locate.py`：代码只写「东西叫什么」+「相对怎么摆」，
盘符运行时枚举（`roots()`），两者做全组合。**代码里一个盘符都不出现。**

这条门禁守的就是这个：防止改完以后又有人在某个角落里写回一个 `G:\\`。

白名单纪律
----------
下面的 `_ALLOW` 是存量。**它只许变短。**
往里面加新条目 = 又把位置焊进代码 = 你就该去用 `fleet/locate.py` 而不是改这张表。
门禁会主动报「白名单里这几条已经不存在了，删掉」——防止这张表变成垃圾场。
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


# ---------------------------------------------------------------- 扫描规则

#: 不走进去的目录：临时产物、构建中间物、依赖包、版本库内部
_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", "tmp", "build", "build-sd",
    "dist", "out", "shots", ".gradle", ".venv", "venv", "cards", "audio_out",
}

#: 只看这些扩展名。别的（png/jsonl 产物）不是「代码」
_EXTS = {".py", ".bat", ".cmd", ".ps1", ".sh", ".json", ".toml", ".cfg", ".ini"}

#: 跨机器稳定的系统标准位置——它们本来就该长这样，不算硬绑。
#: 注意不含 C:/Users/<用户名>/...：用户名也是焊进去的（这个坑踩过：
#: `C:/Users/admin/AppData/Local/Android/Sdk` 换台机器换个用户名就废）。
_SYSTEM = (
    "C:/Windows", "C:\\Windows",
    "C:/Program Files", "C:\\Program Files",
    "C:/Program Files (x86)", "C:\\Program Files (x86)",
    "/usr/", "/bin/", "/opt/", "/etc/", "/tmp/", "/dev/", "/proc/",
)

_LIT = re.compile(r"""["']([A-Za-z]:[\\/][^"'\n]*)["']""")

#: 存量白名单：(相对路径, 该文件里允许出现的盘符集合)
#: `"*"` = 这个文件整体豁免（目前只给门禁自测与运行时审计产物）。
_ALLOW = {
    # 门禁自测要造假路径来验行为，假路径本身没必要跟盘符较劲
    "evals/t14_mcp_and_skills.py": "*",
    "evals/t20_phone_image.py": "*",
    "evals/t23_html_render.py": "*",
    "evals/t45_no_hardcoded_drive.py": "*",      # 本文件（变异测试要造盘符）
    # locate.py 的自测要验「C:/ D:/ 才算示例」，它自己有第 ⑪ 组照镜子
    "fleet/locate.py": {"C:", "D:"},
    # 记忆目录的历史布局：不靠位置优先（按内容挑，见 _pick_memory_dir），
    # 留着是为了老机器仍能被认出来
    "fleet/memory_import.py": {"D:"},
    # 技能自进化留下的审计产物：运行时数据，不是源码
    "skills/self-improving-agent/_audit.json": "*",
    "skills_evolved/local-inspect/_audit.json": "*",
}


def _scan():
    """返回 [(相对路径, 行号, 字面量)]。整行注释跳过——那是讲道理用的反例。"""
    found = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
        # 点开头的隐藏文件是本机产物/缓存（如 tools/.gate_map.json），不是源码，
        # 里面存的本机绝对路径是正常的——它们不入 Git，也不代表「代码里写死了路径」
        filenames = [f for f in filenames if not f.startswith(".")]
        rel_dir = os.path.relpath(dirpath, ROOT).replace("\\", "/")
        if rel_dir.startswith("tools/uicheck/shots"):
            continue
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in _EXTS:
                continue
            rel = fn if rel_dir == "." else rel_dir + "/" + fn
            if _ALLOW.get(rel) == "*":
                continue
            try:
                text = io.open(os.path.join(dirpath, fn),
                               encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for no, line in enumerate(text.splitlines(), 1):
                s = line.strip()
                low = s.lower()
                if s.startswith("#") or low.startswith("rem ") or low == "rem":
                    continue
                for lit in _LIT.findall(line):
                    if lit.startswith(_SYSTEM):
                        continue
                    found.append((rel, no, lit))
    return found


def main():
    print("== ① 扫描：代码里还剩几处盘符（系统标准位置除外）")
    hits = _scan()
    by_file = {}
    for rel, no, lit in hits:
        by_file.setdefault(rel, []).append((no, lit))
    print("     命中 %d 处 / %d 个文件" % (len(hits), len(by_file)))

    print("\n== ② 白名单外的必须为零（这是本门禁的主判据）")
    bad = []
    for rel, items in by_file.items():
        allow = _ALLOW.get(rel)
        for no, lit in items:
            ok = bool(allow) and (allow == "*" or lit[:2] in allow)
            if not ok:
                bad.append("%s:%d  %s" % (rel, no, lit[:70]))
    for b in bad:
        print("     ✗ " + b)
    check(not bad,
          "没有白名单外的硬编码盘符" +
          ("" if not bad else "（%d 处，改用 fleet/locate.py）" % len(bad)))

    print("\n== ③ 白名单不许变成垃圾场：条目失效了就该删")
    stale = []
    for rel, allow in _ALLOW.items():
        if allow == "*":
            if not os.path.exists(os.path.join(ROOT, rel)):
                stale.append("%s（文件已不存在）" % rel)
            continue
        got = {lit[:2] for _n, lit in by_file.get(rel, [])}
        gone = set(allow) - got
        if not os.path.exists(os.path.join(ROOT, rel)):
            stale.append("%s（文件已不存在）" % rel)
        elif gone:
            stale.append("%s（%s 已不再出现）" % (rel, "/".join(sorted(gone))))
    for s in stale:
        print("     ! " + s)
    check(not stale, "白名单全部仍然必要" if not stale
          else "白名单有 %d 条可删" % len(stale))

    print("\n== ④ locate 必须能替代写死：给结构就能算出候选")
    sys.path.insert(0, ROOT)
    from fleet import locate as L
    cands = L._hint_candidates("ffmpeg.exe",
                               ("tools/ffmpeg/bin",), ["C:/", "D:/", "G:/"])
    check(len(cands) == 3, "1 个结构 × 3 个根 = 3 个候选，实得 %d" % len(cands))
    check(any(c.startswith("C:") for c in cands)
          and any(c.startswith("G:") for c in cands),
          "三个盘都在候选里（不再靠人肉列举）")

    print("\n== ⑤ roots() 不许返回本机不存在的盘")
    bad_roots = [r for r in L.roots() if not os.path.exists(r)]
    check(not bad_roots, "roots() 全是真的（%r）" % (L.roots(),))

    print("\n== ⑥ 变异测试：往干净文件里插一个硬编码盘符，门禁必须叫")
    probe_rel = "fleet/_drive_probe_tmp.py"
    probe = os.path.join(ROOT, probe_rel)
    try:
        io.open(probe, "w", encoding="utf-8").write(
            "MODEL = r'Q:/_models/llm/gguf/x.gguf'\n")
        again = _scan()
        caught = [h for h in again if h[0] == probe_rel]
        check(bool(caught),
              "新写的 Q:/ 盘符被抓到（%r）" % (caught[:1],))
    finally:
        if os.path.exists(probe):
            os.remove(probe)

    print()
    if fails:
        print("FAILED %d 项：" % len(fails))
        for f in fails:
            print("   - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""t75 · 文本与时钟基础层：编码判定、子进程解码、单调计时

这一道门禁锁的是**同一类问题反复出现的根源**，不是某个具体 bug。

为什么要有它
------------
2026-11-03 盘点，Windows 上这一类坑在历史里以完全相同的形态反复出现：

  · `tasklist` 输出 GBK → text=True 按 UTF-8 解 → 异常炸在后台线程 →
    stdout 变 None（不是 ""）→ 「接力器永不触发」
  · `netsh` 同上 → 「防火墙检测恒返回 None」
  · 中文 `.bat` 写成 UTF-8 → cmd 按 GBK 读 → 整行当命令执行
  · 无 BOM 的 `.ps1` → PowerShell 5.1 按 cp936 解 → 中文注释全乱
  · curl 在 GBK 终端传中文 → 服务端收到乱码 → 合成静音
  · 墙钟算超时 → NTP 校正后超时永不触发

而且**同一个 workaround 被独立写过四遍**（netaccel / tunnel / gw_up / netdiag），
四份的编码尝试顺序有三套不同写法，其中 tunnel 那份把 gbk 排在第一位——
GBK 合法区间极宽，任何 UTF-8 都能被它「假成功」解出来，所以那份的 utf-8 分支
永远走不到。这就是「每次局部修、下次换个地方再犯」的典型。

所以本门禁分两半：
  A. **能力本身对**（判定链、run 的语义、Deadline 的单调性、mojibake 修复的克制）
  B. **全仓不许再出现裸 text=True**（AST 静态扫，不是 grep —— grep 会被多行参数骗过去）

A 部分用到真实命令输出（netsh / tasklist），跑不了就降级为构造样本，不判假失败。
"""
import ast
import io
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fleet import clock, text  # noqa: E402

PASS = 0
FAIL = []


def check(cond, msg):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(msg)


def sec(t):
    print("\n--- %s" % t)


# ======================================================================
# A1. 判定链
# ======================================================================

def a1_sniff():
    sec("A1 · 编码判定链")

    # BOM 说了算
    check(text.sniff(b"\xef\xbb\xbf" + "中文".encode("utf-8"))["encoding"] == "utf-8-sig",
          "UTF-8 BOM 没认出来")
    check(text.sniff("中文".encode("utf-16"))["encoding"] == "utf-16-le",
          "UTF-16 BOM 没认出来")

    # 严格 UTF-8 优先（GB18030 的假成功是这一节的核心）
    u = "中文测试".encode("utf-8")
    got = text.sniff(u)["encoding"]
    check(got == "utf-8", "UTF-8 字节被判成 %s" % got)
    # 留证据：同样的字节能被 gb18030 解成功，但内容是错的
    try:
        wrong = u.decode("gb18030")
    except UnicodeDecodeError:
        wrong = None
    check(wrong is None or wrong != "中文测试",
          "前提不成立：这段 UTF-8 按 gb18030 竟然解对了")

    # GBK 走 gb18030，且内容正确
    g = "中文测试".encode("gbk")
    check(text.sniff(g)["encoding"] == "gb18030", "GBK 没判成 gb18030：%s" % text.sniff(g)["encoding"])
    check(text.decode(g) == "中文测试", "GBK 解码错：%r" % text.decode(g))

    # 纯 ASCII
    check(text.sniff(b"plain")["encoding"] == "ascii", "ASCII 判错")

    # 无 BOM 的中文 UTF-16：一个零字节都没有，靠 gb18030 假成功会解成 '-N噀Km諎'
    for enc in ("utf-16-le", "utf-16-be"):
        raw = "中文测试".encode(enc)
        got = text.sniff(raw)["encoding"]
        check(got == enc, "无 BOM %s 判成了 %s" % (enc, got))
        check(text.decode(raw) == "中文测试", "无 BOM %s 解出 %r" % (enc, text.decode(raw)))

    # decode 永不抛
    for bad in (b"\xff\xfe\xff", b"\x81", bytes(range(256)), b"", b"\x00" * 32):
        try:
            text.decode(bad)
        except Exception as e:
            check(False, "decode(%r) 抛了 %s" % (bad[:4], type(e).__name__))


# ======================================================================
# A2. 子进程：stdout 永不为 None
# ======================================================================

def a2_run():
    sec("A2 · 子进程解码")
    py = sys.executable

    # 这是本层存在的头号理由：原生 text=True 在解错时给 None，且 returncode 仍是 0
    gbk_code = ("import sys;"
                "sys.stdout.buffer.write('中文'.encode('gbk'));"
                "sys.stderr.buffer.write('错误'.encode('gbk'))")
    native = subprocess.run([py, "-c", gbk_code], capture_output=True, text=True)
    check(native.stdout is None,
          "对照组失效：原生 text=True 解 GBK 竟然没变 None（环境变了，请复核本门禁前提）")
    check(native.returncode == 0,
          "对照组失效：解错时 returncode 竟然非 0")

    r = text.run([py, "-c", gbk_code])
    check(isinstance(r.stdout, str), "text.run 的 stdout 不是 str：%r" % type(r.stdout))
    check(r.stdout == "中文", "text.run 解 GBK 输出不对：%r" % r.stdout)
    check(r.stderr == "错误", "text.run 解 GBK stderr 不对：%r" % r.stderr)
    check(r.raw_stdout == "中文".encode("gbk"), "raw_stdout 没保留原始字节")

    # UTF-8 输出照常
    r2 = text.run([py, "-c", "print('中文输出')"])
    check("中文输出" in r2.stdout, "UTF-8 输出解不对：%r" % r2.stdout)

    # 空输出是 ""，不是 None
    r3 = text.run([py, "-c", "pass"])
    check(r3.stdout == "" and r3.stderr == "", "空输出应为空串：%r/%r" % (r3.stdout, r3.stderr))

    # returncode 不吞
    r4 = text.run([py, "-c", "import sys;sys.exit(7)"])
    check(r4.returncode == 7, "返回码丢了：%r" % r4.returncode)

    # check=True 要抛
    try:
        text.run([py, "-c", "import sys;sys.exit(5)"], check=True)
        check(False, "check=True 没抛")
    except subprocess.CalledProcessError:
        check(True, "")

    # 显式 encoding 优先
    r5 = text.run([py, "-c", "print('é')"], encoding="utf-8")
    check("é" in r5.stdout, "显式 encoding 没生效：%r" % r5.stdout)

    # 额外参数透传（startupinfo / creationflags 之类）
    r6 = text.run([py, "-c", "print(1)"],
                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    check(r6.returncode == 0 and "1" in r6.stdout, "额外参数透传失败")

    # 真实系统命令（中文 Windows 上这是 GBK 的重灾区）；拿不到就降级
    real = [("netsh", ["netsh", "advfirewall", "show", "currentprofile"]),
            ("tasklist", ["tasklist", "/FI", "IMAGENAME eq python.exe"]),
            ("ipconfig", ["ipconfig"])]
    ok_any = False
    for name, cmd in real:
        try:
            rr = text.run(cmd, timeout=20)
        except Exception:
            continue
        if rr.returncode != 0 or not rr.stdout.strip():
            continue
        ok_any = True
        bad = [c for c in rr.stdout if 0xAC00 <= ord(c) <= 0xD7AF or 0xE000 <= ord(c) <= 0xF8FF]
        check(not bad, "%s 输出里出现韩文/私用区字符，说明解错了：%r" % (name, bad[:5]))
        check("None" not in repr(rr.stdout), "%s 的 stdout 是 None 的字符串形式" % name)
    if not ok_any:
        print("    ⚠ 三个系统命令都没拿到输出，真实样本检查降级（不判失败）")


# ======================================================================
# A3. mojibake 修复：修得回来，但不能乱修
# ======================================================================

def a3_mojibake():
    sec("A3 · 乱码修复")

    orig = "中文测试 café"
    broken = orig.encode("utf-8").decode("cp1252", "replace")
    fixed, changed, how = text.fix_mojibake(broken)
    check(changed and fixed == orig, "cp1252 mojibake 没修回来：%r → %r (%s)" % (broken, fixed, how))

    # **不许乱修**：这是 ftfy 反复强调的一条，比修不回来更重要
    for s in ("中文测试", "hello world", "日本語テスト", "价格 12.5 元", "a b c", ""):
        got, chg, _ = text.fix_mojibake(s)
        check(not chg and got == s, "正常文本被误修：%r → %r" % (s, got))

    # 含 cp1252 未定义字节时不许崩
    curly = "“引号”".encode("utf-8").decode("cp1252", "backslashreplace")
    try:
        text.fix_mojibake(curly)
        check(True, "")
    except Exception as e:
        check(False, "cp1252 未定义字节让 fix_mojibake 崩了：%s" % type(e).__name__)


# ======================================================================
# A4. 时钟
# ======================================================================

def a4_clock():
    sec("A4 · 单调计时")

    ci = {c["name"]: c for c in clock.clock_report()}
    check(ci["perf_counter"]["monotonic"], "perf_counter 竟然不单调")
    check(ci["perf_counter"]["resolution_ms"] < 1.0,
          "perf_counter 分辨率 %.3f ms，太粗" % ci["perf_counter"]["resolution_ms"])
    check(not ci["time"]["monotonic"], "time.time 竟然被标成单调")

    # 精度：本层不许退化成 15.625ms 的整数倍网格（那是 GetTickCount64 的特征）
    def work():
        s = 0
        for i in range(60000):
            s += i * i
        return s

    sams = []
    for _ in range(7):
        a = clock.now()
        work()
        sams.append((clock.now() - a) * 1000)
    check(all(x > 0 for x in sams), "计时出现 0ms：%r" % ["%.2f" % x for x in sams])
    on_grid = sum(1 for x in sams if abs(x % 15.625) < 0.05)
    check(on_grid < len(sams) - 1,
          "采样全落在 15.625ms 网格上，时钟可能退化成 GetTickCount64：%r" % sams)

    # Deadline：剩余单调递减、不为负、到期即 0
    dl = clock.Deadline(0.06)
    prev = dl.left()
    check(prev is not None and 0 < prev <= 0.06, "初始剩余异常：%r" % prev)
    for _ in range(3):
        clock.sleep(0.012)
        cur = dl.left()
        check(cur <= prev + 1e-9, "剩余没递减：%.6f → %.6f" % (prev, cur))
        check(cur >= 0, "剩余为负：%r" % cur)
        prev = cur
    clock.sleep(0.06)
    check(dl.expired, "过了预算还没到期")
    check(dl.left() == 0.0, "到期后 left 不是 0.0：%r" % dl.left())

    # 无限预算
    inf = clock.Deadline(None)
    check(inf.left() is None and not inf.expired, "无限预算语义不对")
    check(inf.slice(9) == 9, "无限预算 slice 没原样返回")

    # 0 预算当场到期（>= 不是 >）
    z = clock.Deadline(0)
    check(z.expired and z.left() == 0.0, "0 预算没当场到期")

    # slice 不超过剩余
    d2 = clock.Deadline(0.05)
    check(d2.slice(99) <= 0.05 + 1e-9, "slice 超过剩余")

    # Stopwatch：反复读不归零，lap 归零
    sw = clock.Stopwatch()
    a = sw.elapsed()
    clock.sleep(0.02)
    check(sw.elapsed() > a, "Stopwatch.elapsed 不递增")
    d1 = sw.lap()
    check(d1 >= 0.015, "lap 拿到 %.6f，太小" % d1)
    check(sw.lap() < 0.01, "lap 之后没归零")

    # fmt_dur 不许显示 0.0ms
    check(text and clock.fmt_dur(0.0004).endswith("µs"), "亚毫秒没走微秒")
    check(clock.fmt_dur(0.35) == "350ms", "0.35s 显示成 %r" % clock.fmt_dur(0.35))

    # Deadline 的可执行代码里不许出现墙钟（AST 查）
    tree = ast.parse(__import__("textwrap").dedent(__import__("inspect").getsource(clock.Deadline)))
    hits = [n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "time"
            and n.func.attr in ("time", "localtime")]
    check(not hits, "Deadline 里调了墙钟：%r" % hits)


# ======================================================================
# A5. 脚本合规
# ======================================================================

def a5_script():
    sec("A5 · bat / ps1 合规检查")
    d = tempfile.mkdtemp()

    p = os.path.join(d, "u8.bat")
    io.open(p, "wb").write("@echo off\r\nrem 中文\r\n".encode("utf-8"))
    rep = text.check_script(p)
    check(not rep["ok"], "UTF-8 的 .bat 没被拦下")

    p = os.path.join(d, "ok.bat")
    io.open(p, "wb").write(b"@echo off\r\nrem ascii\r\n")
    check(text.check_script(p)["ok"], "全 ASCII + CRLF 的 .bat 误报")

    p = os.path.join(d, "lf.bat")
    io.open(p, "wb").write(b"@echo off\nif 1==1 echo x\n")
    rep = text.check_script(p)
    check(not rep["ok"] and any("LF" in i or "行尾" in i for i in rep["issues"]),
          "LF 行尾的 .bat 没被拦下：%r" % rep["issues"])

    p = os.path.join(d, "nobom.ps1")
    io.open(p, "wb").write("# 中文注释\n".encode("utf-8"))
    rep = text.check_script(p)
    check(not rep["ok"] and "BOM" in rep["issues"][0], "无 BOM 的 .ps1 没被拦下")

    p = os.path.join(d, "bom.ps1")
    text.write(p, "# 中文注释", bom=True)
    check(text.check_script(p)["ok"], "带 BOM 的 .ps1 被误报：%r" % text.check_script(p))

    # 安全文件名
    check(text.safe_filename('a<b>c:d"e|f?g*h') == "a_b_c_d_e_f_g_h", "非法字符没清")
    check(text.safe_filename("CON") == "_CON", "保留名没处理")
    check(text.safe_filename("nul.txt") == "_nul.txt", "保留名带扩展名没处理")
    check(text.safe_filename("x" * 500) and len(text.safe_filename("x" * 500)) <= 120, "超长没截断")

    # 行尾
    check(text.detect_newline(b"a\r\nb") == "crlf", "CRLF 判错")
    check(text.detect_newline(b"a\r\nb\n") == "mixed", "混合行尾判错")
    check(text.normalize_newline("a\r\rb", "\n") == "a\n\nb", "裸 CR 处理错")

    # ANSI
    check(text.strip_ansi("\x1b[32m绿\x1b[0m") == "绿", "ANSI 没清")


# ======================================================================
# B. 全仓接线：不许再有裸 text=True（AST，不是 grep）
# ======================================================================

SUBPROC_APIS = {"run", "call", "check_call", "check_output", "Popen"}
SKIP_DIRS = {"__pycache__", ".git", "sandbox", "node_modules", "_inbox"}
SKIP_FILES = {"fleet" + os.sep + "text.py"}     # 本层自己的 docstring 会提到


def b_wiring():
    sec("B · 全仓接线（AST 扫裸 text=True）")
    offenders = []
    scanned = 0
    for base in ("fleet", "github_runner", "tools"):
        root_dir = os.path.join(ROOT, base)
        if not os.path.isdir(root_dir):
            continue
        for dp, dn, fn in os.walk(root_dir):
            dn[:] = [d for d in dn if d not in SKIP_DIRS]
            for f in fn:
                if not f.endswith(".py"):
                    continue
                p = os.path.join(dp, f)
                rel = os.path.relpath(p, ROOT)
                if rel in SKIP_FILES:
                    continue
                scanned += 1
                try:
                    src = io.open(p, encoding="utf-8").read()
                    tree = ast.parse(src)
                except Exception:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    fn_node = node.func
                    # 匹配 subprocess.run / subprocess.Popen / ...
                    if not (isinstance(fn_node, ast.Attribute) and fn_node.attr in SUBPROC_APIS):
                        continue
                    owner = fn_node.value
                    if not (isinstance(owner, ast.Name) and owner.id == "subprocess"):
                        continue
                    kw = {k.arg: k.value for k in node.keywords if k.arg}
                    is_text = False
                    for key in ("text", "universal_newlines"):
                        v = kw.get(key)
                        if isinstance(v, ast.Constant) and v.value is True:
                            is_text = True
                    if is_text and "encoding" not in kw:
                        offenders.append("%s:%d subprocess.%s(text=True) 没有 encoding"
                                         % (rel, node.lineno, fn_node.attr))

    check(scanned > 50, "只扫到 %d 个文件，扫描路径可能不对" % scanned)
    check(not offenders,
          "还有 %d 处裸 text=True（解错时 stdout 会变 None，不报错）：\n      %s"
          % (len(offenders), "\n      ".join(offenders[:12])))
    print("    扫了 %d 个 .py，裸 text=True %d 处" % (scanned, len(offenders)))


# ======================================================================
# C. 历史事故回归：四个 workaround 的编码顺序不许再写反
# ======================================================================

def c_order():
    sec("C · 不许把 gbk 排在 utf-8 前面")
    # 反例：按 gbk 先试，UTF-8 中文会被「假成功」解成乱码，utf-8 分支永远走不到
    u = "中文测试".encode("utf-8")
    try:
        fake = u.decode("gbk")
    except UnicodeDecodeError:
        fake = None
    if fake is not None:
        check(fake != "中文测试", "前提不成立：GBK 竟然解对了 UTF-8")
    # 正例：本层的判定链能正确处理
    check(text.decode(u) == "中文测试", "本层没能把 UTF-8 解回来")


# ======================================================================

def main():
    print("t75 · 文本与时钟基础层")
    a1_sniff()
    a2_run()
    a3_mojibake()
    a4_clock()
    a5_script()
    b_wiring()
    c_order()

    print("\n" + "=" * 60)
    if FAIL:
        print("失败 %d 项 / 通过 %d 项" % (len(FAIL), PASS))
        for f in FAIL[:20]:
            print("  ✘", f)
        return 1
    print("t75 全绿：%d 项断言通过" % PASS)
    return 0


if __name__ == "__main__":
    sys.exit(main())

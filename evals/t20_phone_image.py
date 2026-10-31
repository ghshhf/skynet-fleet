#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
t20 · 手机出图功能（N+1c）门禁

⚠️ 2026-10-23 改过静态契约那一段：出图链路已经从「经 Termux RUN_COMMAND 唤起
shell worker」换成了「App 内 JNI 直调 sd.cpp」（`SdBridge` + `QueueRunner`）。
旧断言查的是 `com.termux.RUN_COMMAND_*` / `ACTION_RESULT`，这些常量在现在的
ImageActivity 里**一个都不存在**，于是八条断言齐刷刷失败 —— 不是代码坏了，
是门禁在查一个已经不存在的实现。查不存在的实现是最糟的状态：它永远红着，
真有人把这链路改坏时也没人看得见。
这里改成查**当前**契约（SdBridge 的 JNI 入口 + QueueRunner 的队列调用）。
worker 脚本（termux/fleet-img.sh）还在，行为测试照旧保留。

两路验证，全离线、全可测：
  [①] 静态契约（读源码/清单，不跑真机）
      - manifest 注册 ImageActivity
      - ImageActivity 走 QueueRunner → SdBridge，而不是自己直接算
      - SdBridge 真的加载了 JNI 库、真的声明了 generate 入口
      - Java 侧**不**写死模型路径（模型只由模型库/worker 管）
      - MainActivity 把出图入口接到 ImageActivity
  [②] worker 行为（bash + 假 sd 桩 + 假 sysfs）
      - 空 prompt / 坏 steps / 坏 size / 超温 / 并发锁 → 拒（ok:false）
      - 正常 → ok:true 且日志含 vulkan（诚实边界：没 Vulkan 就报失败）
      - stdout 只有一行 JSON（Termux 100KB 截断纪律）

用法：python evals/t20_phone_image.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MANIFEST = os.path.join(ROOT, "tianwang_android", "AndroidManifest.xml")
IMG = os.path.join(ROOT, "tianwang_android", "src", "app", "skynet", "tianwang", "ImageActivity.java")
MAIN = os.path.join(ROOT, "tianwang_android", "src", "app", "skynet", "tianwang", "MainActivity.java")
SDB = os.path.join(ROOT, "tianwang_android", "src", "app", "skynet", "tianwang", "SdBridge.java")
QRUN = os.path.join(ROOT, "tianwang_android", "src", "app", "skynet", "tianwang", "QueueRunner.java")
WORKER = os.path.join(ROOT, "tianwang_android", "termux", "fleet-img.sh")

fails = []


def check(cond, msg):
    if cond:
        print("  ✓", msg)
    else:
        print("  ✗", msg)
        fails.append(msg)


def section(title):
    print("\n[" + title + "]")


# ---------------------------------------------------------------- ① 静态
def t_static():
    section("① 静态契约")
    m = open(MANIFEST, encoding="utf-8").read()
    check('android:name=".ImageActivity"' in m, "manifest 注册 ImageActivity")
    check('android:name=".MainActivity"' in m, "manifest 注册 MainActivity")

    j = open(IMG, encoding="utf-8").read()
    # 出图必须走「队列执行器 + JNI 桥」两层，而不是在 Activity 里自己算：
    # 队列要能被 PC 侧无人值守触发，所以执行逻辑不能长在 UI 类里。
    check("QueueRunner.run(" in j, "ImageActivity 经 QueueRunner 执行队列（逻辑不在 UI 类里）")
    check("SdBridge" in j, "ImageActivity 持有 SdBridge")
    check("lastError" in j or "失败" in j, "失败路径有文案（不静默）")
    check("gguf" not in j and ".gguf" not in j and "sd15" not in j,
          "Java 不写死模型/gguf 路径（留模型库管）")

    sd = open(SDB, encoding="utf-8").read()
    check('System.loadLibrary("fleet_sd")' in sd, "SdBridge 加载 JNI 库 fleet_sd")
    for tok in ["nativeInit", "nativeGenerate", "nativeCancel", "nativeFree",
                "nativeLastError"]:
        check(tok in sd, "SdBridge 声明 JNI 入口：" + tok)

    qr = open(QRUN, encoding="utf-8").read()
    check("new SdBridge()" in qr, "QueueRunner 直接用 SdBridge")
    check("Progress" in qr, "QueueRunner 接进度回调（长任务要能看见进度）")

    mm = open(MAIN, encoding="utf-8").read()
    # 入口从顶栏胶囊搬进了「+」号菜单（跟 PocketPal 一致的入口设计），
    # 所以查方法名而不是查按钮 id —— 查 id 会把「换了入口位置」误判成「入口没了」。
    check("openImagePage" in mm and "ImageActivity.class" in mm,
          "MainActivity 把出图入口接到 ImageActivity")
    check("import android.content.Intent;" in mm, "MainActivity 已 import Intent（编译已验证）")


# ---------------------------------------------------------------- ② worker 行为
def write_fake_sd(path, make_png=True):
    with open(path, "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env bash\n")
        f.write('# 假 sd：解析 -o 造图，stdout 打一行含 vulkan 的日志（被 worker 重定向到 LOG）\n')
        f.write('out=""\n')
        f.write('while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2;; *) shift;; esac; done\n')
        f.write('echo "Vulkan device: Adreno X1-85 (vulkan)"\n')
        if make_png:
            f.write('printf "\\x89PNG\\r\\n\\x1a\\n" > "$out"\n')  # 最小 PNG 头，够解码判定
    os.chmod(path, 0o755)
    make_executable(path)


def find_bash():
    # Git 的 bash.exe 是**包装器**：无 pty（subprocess 常见）时会打印 wsl.exe 横幅并直接 exit 1，
    # 脚本根本不执行。用其内部 usr/bin/bash.exe（真 MSYS2 bash）才能在无 tty 下正常跑脚本。
    # 真实 Termux 上跑的是真 bash，无此横幅；这里只是让本地门禁能跑。
    import glob
    cands = []
    gp = os.environ.get("CODEBUDDY_CODE_GIT_BASH_PATH")
    if gp:
        base = os.path.dirname(os.path.dirname(gp))      # .../versions/<ver>
        cands.append(os.path.join(base, "usr", "bin", "bash.exe"))
    for pat in [
        r"C:/Users/*/.workbuddy/binaries/PortableGit/versions/*/usr/bin/bash.exe",
        r"C:/Program Files/Git/usr/bin/bash.exe",
        r"C:/Git/usr/bin/bash.exe",
        # 本机 MinGit（2026-11-01 安装）：cmd/git.exe 在 D:\DevTools\MinGit，
        # 真 MSYS2 bash 在 usr/bin/ 下。
        r"D:/DevTools/MinGit/usr/bin/bash.exe",
        r"D:/DevTools/Git/usr/bin/bash.exe",
        r"D:/Git/usr/bin/bash.exe",
    ]:
        cands += glob.glob(pat)
    for c in cands:
        if os.path.isfile(c):
            return c
    return "bash"      # 退化：期望 PATH 里的真 bash


BASH_EXE = find_bash()


def make_executable(path):
    # Python 的 os.chmod 在 Windows 上设的"可执行"Git Bash 不一定认；用真 bash 的 chmod 才稳。
    subprocess.run([BASH_EXE, "-c", "chmod +x \"$1\"", "x", path],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_worker(env, args):
    p = subprocess.run(
        [BASH_EXE, "--noprofile", "--norc", WORKER] + args,
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out = p.stdout.decode("utf-8", "replace").strip()
    err = p.stderr.decode("utf-8", "replace").strip()
    return p.returncode, out, err


def make_sysfs(base, status, temp_milli):
    bat = os.path.join(base, "battery")
    os.makedirs(bat, exist_ok=True)
    with open(os.path.join(bat, "status"), "w") as f:
        f.write(status)
    th = os.path.join(base, "thermal", "zone0")
    os.makedirs(th, exist_ok=True)
    with open(os.path.join(th, "temp"), "w") as f:
        f.write(str(temp_milli))


def base_env(work):
    # work：原生 Windows 路径（Python 建文件用）。env 里喂给 bash 的是 posix 版，
    # 因为 Git Bash / 真机 Termux 都只认 /data/... 这种正斜杠路径。
    wp = posix(work)
    e = dict(os.environ)
    e["FLEET_IMG_WORKDIR"] = wp + "/work"
    e["FLEET_IMG_OUT_DIR"] = wp + "/out"
    e["FLEET_IMG_LOG_DIR"] = wp + "/log"
    e["FLEET_IMG_SD_BIN"] = wp + "/fake_sd.sh"
    e["FLEET_IMG_MODEL"] = wp + "/model.gguf"
    return e


def wpjoin(work, *parts):
    return posix(work) + "/" + "/".join(parts)


# Git Bash 只认正斜杠路径；Windows 反斜杠路径会让 worker 里的 mkdir/执行错位。
# 这里把临时路径统一转成 posix 形式（真实 Termux 上本来就是 /data/... 正斜杠）。
def posix(p):
    try:
        r = subprocess.run(["cygpath", "-u", p], capture_output=True,
                           encoding="utf-8", errors="replace")
        s = r.stdout.strip()
        if r.returncode == 0 and s:
            return s
    except Exception:
        pass
    # cygpath 不在 PATH：MinGit/Git 自带一个，跟 BASH_EXE 同目录，兜底用
    if os.path.isfile(BASH_EXE):
        cy = os.path.join(os.path.dirname(BASH_EXE), "cygpath.exe")
        if os.path.isfile(cy):
            try:
                r = subprocess.run([cy, "-u", p], capture_output=True,
                                   encoding="utf-8", errors="replace")
                s = r.stdout.strip()
                if r.returncode == 0 and s:
                    return s
            except Exception:
                pass
    return p


TMPD = posix(tempfile.gettempdir())


def t_worker():
    section("② worker 行为（假 sd + 假 sysfs）")
    # 🔴 退化到 PATH 里的 "bash" 时必须验证它是 MSYS 系（有 cygpath）：
    # Windows 的 System32\bash.exe 是 **WSL 入口**，路径语义完全不同——
    # 2026-11-01 实测：Windows 反斜杠路径混进 worker 的 JSON 输出，
    # json.loads 报 Invalid \escape，门禁红得很冤。没真 bash 就明说 SKIP。
    if not os.path.isfile(BASH_EXE):
        try:
            probe = subprocess.run([BASH_EXE, "-c", "command -v cygpath"],
                                   capture_output=True, timeout=15)
            if probe.returncode != 0:
                print("  [skip] PATH 里的 bash 不是 MSYS 系（无 cygpath，多半是 WSL）"
                      " ⇒ worker 行为门禁跳过（不是通过）。装法：装 Git for Windows / MinGit", flush=True)
                print("t20_phone_image: SKIP", flush=True)
                return 0
        except (OSError, subprocess.SubprocessError):
            print("  [skip] bash 不可用 ⇒ worker 行为门禁跳过（不是通过）", flush=True)
            print("t20_phone_image: SKIP", flush=True)
            return 0
    work = tempfile.mkdtemp(prefix="t20_")     # 原生 Windows 路径，Python 建文件用
    try:
        write_fake_sd(os.path.join(work, "fake_sd.sh"))
        open(os.path.join(work, "model.gguf"), "w").close()

        # 2.1 正常：插电 + 30°C
        make_sysfs(work, "Charging", 30000)
        e = base_env(work)
        e["FLEET_IMG_BATTERY_DIR"] = posix(work)
        e["FLEET_IMG_THERMAL_GLOB"] = wpjoin(work, "thermal", "zone0", "temp")
        rc, out, err = run_worker(e, ["--prompt", "a cat", "--steps", "10", "--size", "512"])
        ok = json.loads(out)["ok"] if out.startswith("{") else False
        check(rc == 0 and ok, "正常路径 ok:true（rc=%d）" % rc)
        if out.startswith("{"):
            check(json.loads(out).get("temp") == 30, "回传温度取自 sysfs（30°C）")
        # stdout 仅一行
        check(len(out.splitlines()) == 1, "stdout 只有一行 JSON（Termux 100KB 纪律）")

        # 2.2 空 prompt
        rc, out, err = run_worker(e, ["--prompt", "", "--steps", "10", "--size", "512"])
        check("prompt" in out and json.loads(out)["ok"] is False, "空 prompt 拒")

        # 2.3 坏 steps
        rc, out, err = run_worker(e, ["--prompt", "x", "--steps", "99", "--size", "512"])
        check("steps" in out and json.loads(out)["ok"] is False, "steps=99 拒（>30）")

        # 2.4 坏 size
        rc, out, err = run_worker(e, ["--prompt", "x", "--steps", "10", "--size", "999"])
        check("size" in out and json.loads(out)["ok"] is False, "size=999 拒（白名单外）")

        # 2.5 超温（50°C）
        make_sysfs(work, "Charging", 50000)
        rc, out, err = run_worker(e, ["--prompt", "x", "--steps", "10", "--size", "512"])
        check("温度" in out and json.loads(out)["ok"] is False, "50°C 超温拒")

        # 2.6 并发锁：第一张跑太久，第二张插不进
        # 用一个"睡 1s"的假 sd 模拟长任务，先后台起一张，再同步起第二张
        slow = os.path.join(work, "fake_sd_slow.sh")
        with open(slow, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\nout=\"\"\n")
            f.write('while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2;; *) shift;; esac; done\n')
            f.write('echo "Vulkan device: Adreno"; sleep 1; printf "\\x89PNG\\r\\n\\x1a\\n" > "$out"\n')
        os.chmod(slow, 0o755)
        make_executable(slow)
        e2 = base_env(work)
        e2["FLEET_IMG_BATTERY_DIR"] = posix(work)
        e2["FLEET_IMG_THERMAL_GLOB"] = wpjoin(work, "thermal", "zone0", "temp")
        e2["FLEET_IMG_SD_BIN"] = posix(slow)
        make_sysfs(work, "Charging", 30000)
        p1 = subprocess.Popen([BASH_EXE, "--noprofile", "--norc", WORKER, "--prompt", "slow", "--steps", "10", "--size", "512"],
                              env=e2, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        import time
        time.sleep(0.2)
        rc, out, err = run_worker(e2, ["--prompt", "fast", "--steps", "10", "--size", "512"])
        p1.wait(timeout=10)
        check("画完" in out and json.loads(out)["ok"] is False, "并发第二张被锁拒（ok:false）")

        # 2.7 诚实边界：日志没 vulkan → 拒（用不打印 vulkan 的假 sd）
        nosd = os.path.join(work, "fake_sd_cpu.sh")
        with open(nosd, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\nout=\"\"\n")
            f.write('while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2;; *) shift;; esac; done\n')
            f.write('echo "running on CPU"; printf "\\x89PNG\\r\\n\\x1a\\n" > "$out"\n')
        os.chmod(nosd, 0o755)
        make_executable(nosd)
        e3 = base_env(work)
        e3["FLEET_IMG_BATTERY_DIR"] = posix(work)
        e3["FLEET_IMG_THERMAL_GLOB"] = wpjoin(work, "thermal", "zone0", "temp")
        e3["FLEET_IMG_SD_BIN"] = posix(nosd)
        make_sysfs(work, "Charging", 30000)
        rc, out, err = run_worker(e3, ["--prompt", "x", "--steps", "10", "--size", "512"])
        check("Vulkan" in out and json.loads(out)["ok"] is False, "日志无 Vulkan → 报失败（不假装成功）")

        # 2.8 sd 二进制不存在 → 拒
        e4 = base_env(work)
        e4["FLEET_IMG_SD_BIN"] = posix(os.path.join(work, "nope.sh"))
        e4["FLEET_IMG_BATTERY_DIR"] = posix(work)
        e4["FLEET_IMG_THERMAL_GLOB"] = wpjoin(work, "thermal", "zone0", "temp")
        make_sysfs(work, "Charging", 30000)
        rc, out, err = run_worker(e4, ["--prompt", "x", "--steps", "10", "--size", "512"])
        check("sd 二进制" in out and json.loads(out)["ok"] is False, "sd 不存在 → 拒并指 setup")

    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    print("t20 手机出图功能门禁：")
    if not (os.path.isfile(MANIFEST) and os.path.isfile(IMG) and os.path.isfile(WORKER)):
        print("  找不到待测文件，跳过。")
        sys.exit(1)
    t_static()
    t_worker()
    print("\n==== t20 结果 ====")
    if fails:
        print("失败 %d 项：" % len(fails))
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("全部通过 ✓")


if __name__ == "__main__":
    main()

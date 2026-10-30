# -*- coding: utf-8 -*-
"""手机 NPU 引擎直驱（**不经过 App**）。

为什么要有这一层
================

之前天网是「借 App 的能力」：控制口 8808 由 App 的 RemoteHostService 提供，
出图要靠 App 起进程、App 决定分辨率。能用，但意味着：

  1. App 不在前台 / 被 force-stop / MIUI 拦了非导出 service（实测就是如此），
     整条能力就断；
  2. 引擎真正的命令行参数（--type / --patch / --upscaler_mode / --convert）
     都被 App 包住了，我们只能用 App 允许的那几个选择。

真相：local-dream 的 App 只是个壳，它 exec 的是自带的 native 可执行文件
`libstable_diffusion_core.so`（sd15cpu/sd15npu/sdxl/anima）和
`libdit_engine.so`（zimage / klein / qwen21 / krea2turbo）。**这就是那个引擎**，
它本身是个命令行 HTTP 服务，能自己在 adb shell 里跑起来。

本模块把它据为己有。

踩过的坑（都是实测）
====================

- **SELinux 域决定能不能用 NPU。** 以 `run-as <pkg>` 身份跑，域是 runas_app，
  QNN 起 DSP 时 `createUnsignedPD failed, 0x3`（权限拒绝）；
  换成 `adb shell`（u:r:shell:s0）就正常。同样的二进制、同样的库，换个域天差地别。
- **模型在 App 私有目录，shell 身份读不到。** 用
  `run-as pkg sh -c 'cd files && tar cf - models runtime_libs' | tar xf - -C <公共目录>`
  跨身份管道拷出来。反过来不行（run-as 写不进 /data/local/tmp）。
- **QNN 的 HTP 独占。** 同时刻只能有一个 diffusion 进程，换模型/分辨率必须 kill 旧的。
- **分辨率不是请求参数，是启动参数。** SD1.5 NPU 的 unet.bin 按分辨率切，
  非 512 要传 `--patch WxH.patch`（App 命名规则：正方形 768.patch、
  长方形 512x768.patch）。不传就出彩色噪声。

环境变量
========
FLEET_LOCAL_DREAM_ENGINE  生成口地址 "host:port"（直驱总开关，默认 8082）
FLEET_ENGINE_ROOT         设备工作目录（默认 /data/local/tmp/ld）
FLEET_ENGINE_PKG          App 包名（默认 io.github.xororz.localdream）
FLEET_ADB                 adb 路径
"""


import os
import subprocess
import time

DEFAULT_ROOT = "/data/local/tmp/ld"
DEFAULT_PKG = "io.github.xororz.localdream"
EXEC_SD = "libstable_diffusion_core.so"
EXEC_DIT = "libdit_engine.so"
SD_TYPES = ("sd15cpu", "sd15npu", "sdxl", "sdxlmnn", "anima")
DIT_TYPES = ("zimage", "klein", "qwen21", "krea2turbo")
STATE_PREFIX = "engine_state"


def adb_path():
    """adb 可执行文件。找不到就早点说清楚，别让 subprocess 抛原生异常。"""
    import shutil
    p = os.environ.get("FLEET_ADB") or shutil.which("adb")
    if not p:
        raise RuntimeError(
            "找不到 adb：把它放进 PATH，或设 FLEET_ADB=<adb.exe 绝对路径>")
    return p


def sh(*args, **kw):
    """跑一条 adb 命令。返回 (rc, stdout, stderr)。"""
    timeout = kw.get("timeout", 60)
    input_bytes = kw.get("input_bytes")
    cmd = [adb_path()]
    cmd.extend([a for a in args if a is not None])
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           input=input_bytes)
        return (p.returncode,
                (p.stdout or b"").decode("utf-8", "replace"),
                (p.stderr or b"").decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        return -9, "", "timeout"


def write_device_file(path, content, mode="755"):
    """把文本写进设备。stdin 走 `adb shell cat >` 是二进制安全的；
    反过来用 `adb shell cat file` 读会经过 pty 把换行变成 CRLF，不能用于二进制。
    """
    data = content.encode("utf-8") if isinstance(content, str) else content
    return sh("shell", "cat > '%s' && chmod %s '%s'" % (path, mode, path),
              timeout=120, input_bytes=data)


def root():
    return os.environ.get("FLEET_ENGINE_ROOT") or DEFAULT_ROOT


def pkg():
    return os.environ.get("FLEET_ENGINE_PKG") or DEFAULT_PKG


def native_lib_dir():
    """App 安装包的 arm64 库目录（引擎二进制在里面）。"""
    rc, out, err = sh("shell", "pm path %s" % pkg(), timeout=30)
    base = ""
    for ln in out.splitlines():
        if ln.startswith("package:"):
            base = ln[len("package:"):].strip()
            break
    if not base:
        raise RuntimeError("取不到 %s 的包路径（装了吗？）：%s" % (pkg(), err.strip()))
    return base.rsplit("/", 1)[0] + "/lib/arm64"


def ensure_root(timeout=1200):
    """确保设备上有 models/ 与 runtime_libs/；缺就从 App 私有目录跨身份拷贝出来。"""
    r = root()
    rc, out, _ = sh("shell",
                    "ls '%s/models' >/dev/null 2>&1 && echo yes || echo no" % r,
                    timeout=30)
    if "yes" in out:
        return True, r
    home = "/data/user/0/%s/files" % pkg()
    script = ("mkdir -p '%s' && run-as %s sh -c 'cd %s && "
              "tar cf - models runtime_libs' | tar xf - -C '%s' && "
              "chmod -R 777 '%s'" % (r, pkg(), home, r, r))
    rc, out, err = sh("shell", script, timeout=timeout)
    if "tar:" in err or rc != 0:
        raise RuntimeError(
            "从 App 私有目录拷模型失败：%s（debug 包里没有 models 目录时就会这样）"
            % ((err.strip() or out.strip())[-200:]))
    return True, r


def models():
    rc, out, _ = sh("shell", "ls '%s/models' 2>/dev/null" % root(), timeout=30)
    return [x.strip() for x in out.split() if x.strip()]


def patches(model):
    """模型支持的分辨率 → patch 文件名。"""
    rc, out, _ = sh("shell",
                    "ls '%s/models/%s' 2>/dev/null | grep '.patch$'"
                    % (root(), model), timeout=30)
    res = {}
    for name in (x.strip() for x in out.split() if x.strip()):
        stem = name[:-len(".patch")]
        w, _, h = stem.partition("x") if "x" in stem else (stem, "", stem)
        if w.isdigit() and h.isdigit():
            res[(int(w), int(h) if h else int(w))] = name
    return res


def patch_for(model, width, height):
    """按 App 规则挑 patch（正方形优先 <size>.patch）。返回绝对路径或 None。"""
    w, h = int(width), int(height)
    if w == 512 and h == 512:
        return None
    pm = patches(model)
    key = (w, h)
    if key in pm:
        return "%s/models/%s/%s" % (root(), model, pm[key])
    if w == h and (w, w) in pm:
        return "%s/models/%s/%s" % (root(), model, pm[(w, w)])
    return None


def exec_for(type_):
    return EXEC_DIT if str(type_) in DIT_TYPES else EXEC_SD


def _state_path(port):
    return "%s/%s.%d.json" % (root(), STATE_PREFIX, int(port))


def read_state(port):
    rc, out, _ = sh("shell", "cat '%s' 2>/dev/null" % _state_path(port),
                    timeout=20)
    if rc != 0 or not out.strip():
        return None
    import json
    try:
        return json.loads(out)
    except ValueError:
        return None


def _write_state(port, state):
    import json
    try:
        write_device_file(_state_path(port), json.dumps(state))
    except Exception:
        pass


def _alive(pid):
    rc, out, _ = sh("shell", "test -d /proc/%d && echo 1 || echo 0" % int(pid),
                    timeout=20)
    return out.strip().startswith("1")


def pid_of_engine(type_=None):
    """正在跑的引擎进程 pid 列表。"""
    names = [exec_for(type_)] if type_ else [EXEC_SD, EXEC_DIT]
    out_all = []
    for name in names:
        rc, out, _ = sh("shell", "ps -A -o PID,NAME 2>/dev/null | grep -F '%s'"
                        % name, timeout=30)
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) >= 2 and parts[0].isdigit():
                out_all.append(int(parts[0]))
    return out_all


def stop_all():
    """杀掉设备上的引擎进程。换模型/换分辨率前必须做——HTP 是独占的。"""
    killed = []
    for pid in pid_of_engine():
        sh("shell", "kill -9 %d 2>/dev/null" % pid, timeout=20)
        killed.append(pid)
    sh("shell", "rm -f %s/%s.*.json" % (root(), STATE_PREFIX), timeout=20)
    if killed:
        time.sleep(1.5)  # 等 HTP 释放；不等的话新进程 dev create 会失败
    return killed


def endpoint(host=None, port=None):
    """从 FLEET_LOCAL_DREAM_ENGINE 解析 (host, port)。"""
    if host and port:
        return host, int(port)
    raw = (os.environ.get("FLEET_LOCAL_DREAM_ENGINE") or "").strip()
    if not raw:
        raise RuntimeError(
            "引擎直驱未配置：设 FLEET_LOCAL_DREAM_ENGINE=<手机IP>:<端口>（默认 8082）")
    if raw.startswith("http://") or raw.startswith("https://"):
        raw = raw.split("://", 1)[1]
    raw = raw.rstrip("/")
    h, _, p = raw.partition(":")
    return h, int(p) if p.isdigit() else 8082


def enabled():
    return bool((os.environ.get("FLEET_LOCAL_DREAM_ENGINE") or "").strip())


def health(host=None, port=None, timeout=3):
    import urllib.request as u
    h, p = endpoint(host, port)
    try:
        op = u.build_opener(u.ProxyHandler({}))
        with op.open(u.Request("http://%s:%d/health" % (h, p)),
                     timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def wait_health(host=None, port=None, timeout=180):
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        if health(host, port, timeout=3):
            return True
        _t.sleep(1.0)
    return False


def _start_sh(model, width, height, port, type_, extra=None):
    """拼启动脚本。返回 shell 脚本文本。"""
    lib = native_lib_dir()
    r = root()
    ex = "%s/%s" % (lib, exec_for(type_))
    lines = [
        "#!/system/bin/sh",
        # LD_LIBRARY_PATH 必须带 App lib 目录：QNN 五个库是 App 资产，不在系统库路径
        "export LD_LIBRARY_PATH=%s:%s/runtime_libs:/vendor/lib64:/system/lib64"
        % (lib, r),
        # ADSP_LIBRARY_PATH 少了 /vendor/dsp/cdsp 就拿不到 skel → QnnDevice_create 0x36b1
        'export ADSP_LIBRARY_PATH="%s/runtime_libs;/vendor/dsp/cdsp;'
        '/vendor/lib/rfsa/adsp;/system/lib/rfsa/adsp;"' % r,
        'export DSP_LIBRARY_PATH="%s/runtime_libs;/vendor/dsp/cdsp"' % r,
    ]
    cmd = [ex, "--type", type_,
           "--model_dir", "%s/models/%s" % (r, model),
           "--lib_dir", "%s/runtime_libs" % r,
           "--port", str(int(port)), "--listen_all"]
    p = patch_for(model, width, height)
    if p:
        cmd += ["--patch", p]
    if extra:
        cmd += list(extra)
    lines.append("exec " + " ".join('"%s"' % c for c in cmd))
    return "\n".join(lines) + "\n"


def start(model=None, width=512, height=512, port=None, host=None,
          type_="sd15npu", timeout=180, force=False):
    """拉起引擎并保证它在 mouth pi 服务。返回 dict(ok,model,width,height,port,pid,err)。"""
    h, port = endpoint(host, port)
    try:
        ensure_root()
        ms = models()
    except Exception as e:
        return {"ok": False, "model": model or "", "port": port,
                "err": "%s: %s" % (type(e).__name__, e)}
    if not ms:
        return {"ok": False, "model": model or "", "port": port,
                "err": "设备上没有模型（%s/models 为空）" % root()}
    model = model or ms[0]
    if model not in ms:
        return {"ok": False, "model": model, "port": port,
                "err": "没有模型 %r；可用：%s" % (model, ", ".join(ms))}
    if (int(width), int(height)) != (512, 512) and not patch_for(model, width, height):
        sizes = ", ".join("%dx%d" % k for k in sorted(patches(model)))
        return {"ok": False, "model": model, "port": port,
                "err": "%s 没有 %dx%d 的 patch，只有：%s" % (model, width, height, sizes)}

    st = read_state(port)
    if (st and not force and st.get("model") == model
            and int(st.get("width") or 0) == int(width)
            and int(st.get("height") or 0) == int(height)
            and st.get("type") == type_ and st.get("pid") and _alive(st["pid"])
            and health(h, port)):
        return {"ok": True, "model": model, "width": int(width),
                "height": int(height), "port": port, "pid": st["pid"],
                "err": "", "reused": True}

    stop_all()
    script = "%s/start_%d.sh" % (root(), int(port))
    log = "%s/engine_%d.log" % (root(), int(port))
    try:
        write_device_file(script, _start_sh(model, width, height, port, type_))
    except Exception as e:
        return {"ok": False, "model": model, "port": port,
                "err": "写启动脚本失败：%s" % e}

    # adb shell 会因为后台进程持有 stdout 而不返回，超时不代表失败，亲自确认进程
    sh("shell", "cd '%s' && nohup sh %s > %s 2>&1 &" % (root(), script, log),
       timeout=15)

    import time as _t
    deadline = _t.time() + timeout
    pid = None
    while _t.time() < deadline:
        pids = pid_of_engine(type_)
        if pids:
            pid = pids[0]
        if pid and health(h, port, timeout=3):
            break
        _t.sleep(1.0)
    if not pid:
        return {"ok": False, "model": model, "port": port,
                "err": "引擎进程没起来：%s" % tail_log(port, 8)}
    if not health(h, port, timeout=5):
        return {"ok": False, "model": model, "port": port,
                "err": "进程起来了但 %d 没监听：%s" % (port, tail_log(port, 10))}
    _write_state(port, {"model": model, "width": int(width),
                        "height": int(height), "port": int(port),
                        "type": type_, "pid": pid, "ts": int(_t.time())})
    return {"ok": True, "model": model, "width": int(width),
            "height": int(height), "port": port, "pid": pid, "err": ""}


def tail_log(port, n=8):
    rc, out, _ = sh("shell", "tail -n %d '%s/engine_%d.log' 2>/dev/null"
                    % (int(n), root(), int(port)), timeout=30)
    return " / ".join(x.strip() for x in out.splitlines() if x.strip())[-500:]


def generate(payload, host=None, port=None, timeout=300):
    """发一次 /generate，返回 (bytes, meta)。SSE 流里读 type=complete 那条。"""
    import base64 as _b
    import json as _j
    import urllib.request as u
    h, p = endpoint(host, port)
    op = u.build_opener(u.ProxyHandler({}))
    req = u.Request("http://%s:%d/generate" % (h, p),
                    data=_j.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"})
    img = None
    meta = {}
    with op.open(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                d = _j.loads(line[5:].strip())
            except ValueError:
                continue
            if d.get("type") == "complete":
                img = d.get("image")
                meta = d
                break
    if not img:
        raise RuntimeError("引擎没回访图像；最后一条消息：%s"
                           % str(meta)[:200])
    return _b.b64decode(img), {k: v for k, v in meta.items() if k != "image"}


def _selftest():
    """纯逻辑自检（不碰真机、不联网）。"""
    fails = []
    n = [0]

    def chk(name, cond, extra=""):
        n[0] += 1
        print(("  ok   " if cond else "  FAIL ") + name +
              ("" if cond else "   <- %s" % extra))
        if not cond:
            fails.append(name)

    print("== patch 命名规则（抄 BackendService.kt:548） ==")
    real_patches = globals()["patches"]
    old_root = os.environ.get("FLEET_ENGINE_ROOT")
    globals()["patches"] = lambda m: {(768, 768): "768.patch",
                                      (512, 768): "512x768.patch",
                                      (768, 512): "768x512.patch",
                                      (1024, 768): "1024x768.patch"}
    os.environ["FLEET_ENGINE_ROOT"] = "/tmp/ld"
    R = "/tmp/ld"
    try:
        chk("512x512 不需要 patch（unet.bin 本来就是 512 网格）",
            patch_for("m", 512, 512) is None)
        chk("正方形 768 用 768.patch",
            patch_for("m", 768, 768) == R + "/models/m/768.patch",
            str(patch_for("m", 768, 768)))
        chk("长方形 512x768 用 512x768.patch（不能拿 768.patch 顶）",
            patch_for("m", 512, 768) == R + "/models/m/512x768.patch",
            str(patch_for("m", 512, 768)))
        chk("1024x768 用 1024x768.patch",
            patch_for("m", 1024, 768) == R + "/models/m/1024x768.patch")
        chk("没有的分辨率返回 None（避免硬跑出噪声）",
            patch_for("m", 640, 640) is None)
    finally:
        globals()["patches"] = real_patches
        if old_root:
            os.environ["FLEET_ENGINE_ROOT"] = old_root
        else:
            os.environ.pop("FLEET_ENGINE_ROOT", None)

    print("\n== 启动脚本拼装 ==")
    old_root = os.environ.get("FLEET_ENGINE_ROOT")
    old_libdir = globals()["native_lib_dir"]
    os.environ["FLEET_ENGINE_ROOT"] = R
    globals()["native_lib_dir"] = lambda: "/data/app/x/lib/arm64"
    real_pf = globals()["patch_for"]
    try:
        globals()["patch_for"] = lambda m, w, h: (
            None if (int(w) == 512 and int(h) == 512)
            else "%s/models/%s/%dx%d.patch" % (R, m, int(w), int(h)))
        s = _start_sh("absolutereality", 512, 512, 8082, "sd15npu")
        chk("--listen_all 必须有（否则只有手机自身能连）", "--listen_all" in s)
        # 注意：命令行里每个参数都被独立加引号（空格路径安全），所以断言按 token 比
        chk("--port 落进命令行", '"--port" "8082"' in s, s)
        chk("--model_dir 指向拷出来的模型而非 App 私有目录",
            '"--model_dir" "/tmp/ld/models/absolutereality"' in s, s)
        chk("--lib_dir 指向 runtime_libs（QNN 五个库在这）",
            '"--lib_dir" "/tmp/ld/runtime_libs"' in s, s)
        chk("LD_LIBRARY_PATH 含 App lib + runtime_libs + /vendor/lib64",
            "LD_LIBRARY_PATH=/data/app/x/lib/arm64:/tmp/ld/runtime_libs:"
            "/vendor/lib64" in s, s)
        chk("ADSP_LIBRARY_PATH 含 /vendor/dsp/cdsp（少它就是 0x36b1）",
            "/vendor/dsp/cdsp" in s)
        chk("LD_LIBRARY_PATH 也含 runtime_libs（自己抽的 skel 在这）",
            "runtime_libs" in s)
        chk("512x512 不带 --patch", "--patch" not in s)
        chk("用 exec（pid 即引擎自身，好杀干净）", s.count("exec ") == 1)

        s2 = _start_sh("anythingv5", 512, 768, 8082, "sd15npu")
        chk("非 512 自动带 --patch 512x768.patch",
            '"--patch" "/tmp/ld/models/anythingv5/512x768.patch"' in s2, s2)

        s3 = _start_sh("z-image", 512, 512, 8083, "zimage")
        chk("DiT 类型用 libdit_engine.so（不是同一个二进制）",
            EXEC_DIT in s3 and EXEC_SD not in s3)
    finally:
        globals()["patch_for"] = real_pf
        globals()["native_lib_dir"] = old_libdir
        if old_root:
            os.environ["FLEET_ENGINE_ROOT"] = old_root
        else:
            os.environ.pop("FLEET_ENGINE_ROOT", None)

    print("\n== endpoint 解析 ==")
    old_env = os.environ.get("FLEET_LOCAL_DREAM_ENGINE")
    try:
        os.environ["FLEET_LOCAL_DREAM_ENGINE"] = "192.168.1.5:8082"
        chk("host:port 解析", endpoint() == ("192.168.1.5", 8082), str(endpoint()))
        chk("enabled() 为真", enabled() is True)
        os.environ["FLEET_LOCAL_DREAM_ENGINE"] = "http://10.0.0.2"
        chk("裸 host 默认 8082", endpoint() == ("10.0.0.2", 8082), str(endpoint()))
        os.environ.pop("FLEET_LOCAL_DREAM_ENGINE", None)
        chk("没配时 enabled() 为假", enabled() is False)
    finally:
        if old_env is not None:
            os.environ["FLEET_LOCAL_DREAM_ENGINE"] = old_env

    print("\n%d/%d 通过" % (n[0] - len(fails), n[0]))
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    if "--models" in sys.argv:
        ensure_root()
        for m in models():
            print(m, " 支持分辨率:", ", ".join(
                "%dx%d" % k for k in sorted(patches(m))) or "512x512")
        raise SystemExit(0)
    if "--start" in sys.argv:
        i = sys.argv.index("--start")
        m = sys.argv[i + 1] if len(sys.argv) > i + 1 else None
        w = int(sys.argv[i + 2]) if len(sys.argv) > i + 2 else 512
        h = int(sys.argv[i + 3]) if len(sys.argv) > i + 3 else 512
        import json as _j
        print(_j.dumps(start(m, w, h), ensure_ascii=False))
        raise SystemExit(0)
    if "--stop" in sys.argv:
        print("killed:", stop_all())
        raise SystemExit(0)
    print(__doc__)

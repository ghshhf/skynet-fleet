"""天网 · 语音合成后端。手机本地优先，云 TTS 兜底。

为什么要有这一层
----------------
`fleet/storyboard.py` 原来直接调 `edge_tts` —— 那是微软的在线接口，断网就哑，
而且每条台词都要出网。漫剧一次几十分钟、几十条台词，这条路既慢又不该依赖。

现在手机自己能合成：`sherpa-onnx`（Android bionic 静态二进制）+ MeloTTS 中文模型，
实测 8 Gen 1 上 RTF 0.48（4.5 秒语音用 2.18 秒算完），**全程离线**。

为什么不是"复用出图那个 NPU 引擎"
----------------------------------
查过了，两条路本质不同：
  · 出图引擎 `libstable_diffusion_core.so` 的 NPU 路径走 QNN HTP，它要创建
    **unsigned PD**（`DspTransport.createUnsignedPD`）。实测在 `runas_app` 域下
    返回 0x3 = EPERM —— 这是 SELinux 层面的限制，只有 App 自己的进程
    （`untrusted_app` 域）能建。所以「从 shell 直接驱动 NPU」这条路对任意模型都
    不成立，不是我们没写对。
  · TTS 不需要 NPU：模型才 170MB、计算量小，CPU 上已经比实时快一倍。
    硬塞进 NPU 反而要过 QNN 转换、还可能掉精度（CosyVoice3-MNN 的记录里，
    整模型上 NPU 会导致 token 坍缩，只能把单个 q_proj 放上去，收益 5%）。

所以这里的选择是「用对的引擎」而不是「用同一个引擎」：
出图吃 NPU（算力瓶颈在那），TTS 吃 CPU（瓶颈不在那）。

后端优先级
----------
  1. `phone_sherpa`  手机本地 sherpa-onnx + MeloTTS（离线、快、免费）
  2. `pc_sherpa`     PC 本地 sherpa-onnx（同一套模型，调试用）
  3. `edge`          edge-tts 云（有网、音色多，作兜底）

用法
----
    from fleet import tts_runner
    tts_runner.deploy()                      # 一次性部署（幂等）
    r = tts_runner.run("那天夜里，海上起雾了。")
    print(r["path"], r["sec"], r["backend"])
"""
import os
import re
import shutil
import subprocess
import tempfile
import time

# ---- 手机端部署位置 ----------------------------------------------------
PHONE_BIN = "/data/local/tmp/sn-tts"
PHONE_LIBCXX = "/data/local/tmp/libc++_shared.so"
PHONE_MODEL_DIR = "/data/local/tmp/melo"
PHONE_WORK = "/data/local/tmp/tts_work"

# ---- PC 端模型（与手机同一套，调试/兜底用）-----------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PC_MODEL_CANDIDATES = [
    os.path.join(ROOT, "_contrib", "tts", "vits-melo-tts-zh_en"),
    os.path.expanduser("~/.fleet/tts/vits-melo-tts-zh_en"),
]

# 手机端 s6 默认音色 0 = 中文女声；1 = 英文（MeloTTS zh_en 双语模型）
DEFAULT_SID = 0


def _sh(cmd, timeout=120, check=False):
    """跑一条 adb / 本地命令，返回 (rc, out, err)。不抛异常。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        if check and p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "").strip()[:400])
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return -9, "", "timeout after %ss" % timeout
    except FileNotFoundError as e:
        return -1, "", "找不到命令: %s" % e


def phone_ready():
    """手机端能不能直接合成。返回 (bool, 原因)。"""
    rc, out, _ = _sh(["adb", "devices"], timeout=15)
    if rc != 0 or "\tdevice" not in out:
        return False, "adb 没连上设备"
    rc, out, _ = _sh(["adb", "shell",
                      "ls %s %s 2>/dev/null | wc -l" % (PHONE_BIN, PHONE_MODEL_DIR + "/model.onnx")],
                     timeout=20)
    if rc != 0:
        return False, "adb shell 不可用"
    # 期望两个路径都存在 → wc -l 给 2
    try:
        n = int((out or "0").strip().splitlines()[-1])
    except (ValueError, IndexError):
        n = 0
    if n < 2:
        return False, "手机端还没部署（跑 tts_runner.deploy()）"
    return True, ""


def _q(text):
    """把文本塞进单引号里，内部单引号按 sh 规则转义。"""
    return "'" + str(text).replace("'", "'\\''") + "'"


def phone_sherpa(text, out_path=None, sid=DEFAULT_SID, speed=1.0, threads=4,
                 timeout=180):
    """手机本地合成。返回 (wav 路径, 秒数, 日志)。失败抛 RuntimeError。

    文本不直接进命令行 —— 中文标点、引号、$ 都可能被 shell 吃掉。
    做法是写一个一次性脚本推上去执行，文本在脚本里以单引号形式出现。
    """
    ok, why = phone_ready()
    if not ok:
        raise RuntimeError(why)

    stamp = int(time.time() * 1000)
    if out_path is None:
        out_path = os.path.join(tempfile.gettempdir(), "tts_%d.wav" % stamp)
    remote_wav = "%s/o_%d.wav" % (PHONE_WORK, stamp)

    script = (
        "#!/system/bin/sh\n"
        "mkdir -p %s\n"
        "export LD_LIBRARY_PATH=/data/local/tmp\n"
        "%s --vits-model=%s/model.onnx --vits-lexicon=%s/lexicon.txt "
        "--vits-tokens=%s/tokens.txt --vits-data-dir=%s/espeak-ng-data "
        "--output-filename=%s --num-threads=%d --sid=%d --vits-length-scale=%f "
        "%s\n"
        "echo EXIT=$?\n"
    ) % (PHONE_WORK, PHONE_BIN, PHONE_MODEL_DIR, PHONE_MODEL_DIR,
         PHONE_MODEL_DIR, PHONE_MODEL_DIR, remote_wav, int(threads),
         int(sid), float(1.0 / max(0.1, float(speed))), _q(text))

    fd, local_script = tempfile.mkstemp(suffix=".sh")
    os.close(fd)
    try:
        with open(local_script, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
        rc, out, err = _sh(["adb", "push", local_script, "/data/local/tmp/tts_run.sh"],
                           timeout=60)
        if rc != 0:
            raise RuntimeError("推脚本失败: %s" % (err or out).strip()[:200])
        rc, out, err = _sh(["adb", "shell", "sh /data/local/tmp/tts_run.sh"],
                           timeout=timeout)
    finally:
        try:
            os.remove(local_script)
        except OSError:
            pass

    log = (out or "") + (err or "")
    if rc != 0 or "EXIT=" not in log or "EXIT=0" not in log:
        raise RuntimeError("手机合成失败: %s" % " ".join(log.strip().splitlines()[-3:])[:300])

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    rc, out, err = _sh(["adb", "pull", remote_wav, out_path], timeout=60)
    if rc != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) < 1024:
        raise RuntimeError("拉回音频失败: %s" % (err or out).strip()[:200])
    _sh(["adb", "shell", "rm -f %s" % remote_wav], timeout=20)

    sec = wav_seconds(out_path)
    if sec <= 0.05:
        raise RuntimeError("拉回的音频只有 %.3fs，视为失败" % sec)
    return out_path, sec, log


def pc_model_dir():
    for d in PC_MODEL_CANDIDATES:
        if os.path.isfile(os.path.join(d, "model.onnx")):
            return d
    return None


def pc_sherpa(text, out_path=None, sid=DEFAULT_SID, speed=1.0, threads=4):
    """PC 本地合成（同一套模型）。没有模型/没装库就抛 RuntimeError。"""
    d = pc_model_dir()
    if not d:
        raise RuntimeError("PC 上没有 MeloTTS 模型（找过: %s）"
                           % ", ".join(PC_MODEL_CANDIDATES))
    try:
        import sherpa_onnx
    except ImportError:
        raise RuntimeError("没装 sherpa_onnx（pip install sherpa-onnx）")

    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=os.path.join(d, "model.onnx"),
                tokens=os.path.join(d, "tokens.txt"),
                lexicon=os.path.join(d, "lexicon.txt"),
                dict_dir=os.path.join(d, "dict"),
                data_dir=os.path.join(d, "espeak-ng-data")),
            num_threads=int(threads)),
        max_num_sentences=2)
    tts = sherpa_onnx.OfflineTts(cfg)
    a = tts.generate(text, sid=int(sid), speed=float(speed))
    if not a.samples:
        raise RuntimeError("合成结果为空（文本转 token 失败？）")
    if out_path is None:
        out_path = os.path.join(tempfile.gettempdir(),
                                "tts_%d.wav" % int(time.time() * 1000))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    import wave
    import numpy as np
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(a.sample_rate)
        w.writeframes((np.array(a.samples) * 32767).astype("int16").tobytes())
    return out_path, len(a.samples) / float(a.sample_rate), "pc sherpa-onnx"


def edge_tts_backend(text, out_path=None, voice="zh-CN-YunxiNeural", speed=1.0):
    """云兜底（要联网）。声音更自然，但每条台词都出网。"""
    try:
        from fleet import storyboard as _sb
    except ImportError:
        import storyboard as _sb
    if out_path is None:
        out_path = os.path.join(tempfile.gettempdir(),
                                "tts_%d.mp3" % int(time.time() * 1000))
    p = _sb.tts(text, out_path, voice=voice)
    return p, wav_seconds(p), "edge-tts"


def wav_seconds(path):
    """拿音频时长（wav 直接读头，其它格式退回 ffprobe）。失败返回 0。"""
    try:
        import wave
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        pass
    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        rc, out, err = _sh([ff, "-i", path], timeout=30)
        m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err or out)
        if m:
            h, mm, s = m.groups()
            return int(h) * 3600 + int(mm) * 60 + float(s)
    except Exception:
        pass
    return 0.0


def parse_stats(log):
    """从 sherpa-onnx 日志里抓耗时与 RTF。拿不到就返回空 dict。"""
    out = {}
    m = re.search(r"Elapsed seconds:\s*([\d.]+)", log or "")
    if m:
        out["elapsed"] = float(m.group(1))
    m = re.search(r"Audio duration:\s*([\d.]+)", log or "")
    if m:
        out["audio"] = float(m.group(1))
    m = re.search(r"Real-time factor \(RTF\):[^\n]*=\s*([\d.]+)", log or "")
    if m:
        out["rtf"] = float(m.group(1))
    return out


def _default_backend():
    """手机 > PC > 云。手机能用就用手机，这条是「手机自己会说话」的意义所在。"""
    ok, _ = phone_ready()
    if ok:
        return "phone_sherpa"
    if pc_model_dir():
        return "pc_sherpa"
    return "edge"


def run(text, out_path=None, voice=None, speed=1.0, backend=None, sid=None):
    """合成一段语音。

    返回 {"ok", "path", "sec", "backend", "rtf", "err"}。任何异常都不外抛 ——
    漫剧流程里 TTS 挂了应该退回「无声成片」，而不是整条片报废。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "path": "", "sec": 0.0, "backend": "",
                "rtf": 0.0, "err": "文本为空"}
    backend = backend or _default_backend()
    sid = DEFAULT_SID if sid is None else int(sid)
    t0 = time.time()
    log = ""
    try:
        if backend == "phone_sherpa":
            path, sec, log = phone_sherpa(text, out_path, sid=sid, speed=speed)
        elif backend == "pc_sherpa":
            path, sec, log = pc_sherpa(text, out_path, sid=sid, speed=speed)
        elif backend == "edge":
            path, sec, log = edge_tts_backend(text, out_path, voice=voice or
                                              "zh-CN-YunxiNeural", speed=speed)
        else:
            raise RuntimeError("未知后端 %r（可选 phone_sherpa/pc_sherpa/edge）" % backend)
        st = parse_stats(log)
        return {"ok": True, "path": path, "sec": round(float(sec), 3),
                "backend": backend, "rtf": st.get("rtf", 0.0),
                "wall": round(time.time() - t0, 2), "err": ""}
    except Exception as e:
        # 手机挂了退 PC，PC 挂了退云 —— 但只退一次，不无限转圈
        if backend != "edge":
            try:
                path, sec, log = edge_tts_backend(text, out_path, speed=speed)
                return {"ok": True, "path": path, "sec": round(float(sec), 3),
                        "backend": "edge(fallback)", "rtf": 0.0,
                        "wall": round(time.time() - t0, 2),
                        "err": "首选 %s 失败后兜底：%s" % (backend, e)}
            except Exception:
                pass
        return {"ok": False, "path": "", "sec": 0.0, "backend": backend,
                "rtf": 0.0, "wall": round(time.time() - t0, 2),
                "err": "%s: %s" % (type(e).__name__, e)}


# ---------------------------------------------------------------- 部署

def deploy(bin_path=None, model_dir=None, libcxx=None, force=False):
    """把二进制 + libc++_shared + 模型推到手机。幂等，已存在就跳过。

    三个文件从哪来（都写在返回值里，方便查）：
      · sn-tts        sherpa-onnx 的 android-aarch64-termux-static 里的
                      `sherpa-onnx-offline-tts`
      · libc++_shared Termux bootstrap 的 lib/ 下（NDK r26+ 版本；Android 系统
                      自带的太老，缺 __hash_memory 符号，二进制直接起不来）
      · melo 模型     vits-melo-tts-zh_en（中英双语，170MB onnx）
    """
    dl = os.path.join(ROOT, "_contrib", "tts")
    src_bin = bin_path or os.path.join(
        dl, "sherpa-onnx-v2.0.1-android-aarch64-termux-static", "bin",
        "sherpa-onnx-offline-tts")
    src_lib = libcxx or os.path.join(dl, "termuxlib", "lib", "libc++_shared.so")
    src_model = model_dir or os.path.join(dl, "vits-melo-tts-zh_en")

    done, skipped = [], []
    for src, dst, label in ((src_bin, PHONE_BIN, "sn-tts"),
                            (src_lib, PHONE_LIBCXX, "libc++_shared.so")):
        if not os.path.isfile(src):
            return {"ok": False, "err": "本地缺 %s（%s）" % (label, src),
                    "deployed": done, "skipped": skipped}
        rc, out, _ = _sh(["adb", "shell", "ls %s 2>/dev/null" % dst], timeout=15)
        if rc == 0 and dst in (out or "") and not force:
            skipped.append(label)
            continue
        rc, out, err = _sh(["adb", "push", src, dst], timeout=120)
        if rc != 0:
            return {"ok": False, "err": "推 %s 失败: %s" % (label, (err or out)[:200]),
                    "deployed": done, "skipped": skipped}
        done.append(label)
    _sh(["adb", "shell", "chmod 755 %s" % PHONE_BIN], timeout=15)

    if not os.path.isdir(src_model):
        return {"ok": False, "err": "本地缺模型目录 %s" % src_model,
                "deployed": done, "skipped": skipped}
    rc, out, _ = _sh(["adb", "shell", "ls %s/model.onnx 2>/dev/null" % PHONE_MODEL_DIR],
                     timeout=15)
    if rc == 0 and "model.onnx" in (out or "") and not force:
        skipped.append("melo 模型")
    else:
        rc, out, err = _sh(["adb", "push", src_model, PHONE_MODEL_DIR], timeout=900)
        if rc != 0:
            return {"ok": False, "err": "推模型失败: %s" % (err or out)[:200],
                    "deployed": done, "skipped": skipped}
        done.append("melo 模型")
    _sh(["adb", "shell", "mkdir -p %s" % PHONE_WORK], timeout=15)

    ok, why = phone_ready()
    return {"ok": ok, "err": why, "deployed": done, "skipped": skipped}


# ---------------------------------------------------------------- 自测

def _selftest():
    fails = []
    n = [0]

    def chk(name, cond, extra=""):
        n[0] += 1
        print(("  ok   " if cond else "  FAIL ") + name + (
            "" if cond else "   <- %s" % extra))
        if not cond:
            fails.append(name)

    print("-- 文本转义（中文标点/引号/$ 都不能被 shell 吃掉）")
    chk("普通中文原样", _q("海上起雾了。") == "'海上起雾了。'")
    chk("单引号被转义", _q("it's") == "'it'\\''s'", _q("it's"))
    chk("$ 不会被展开", "$HOME" not in _q("$HOME").replace("'$HOME'", ""),
        _q("$HOME"))
    chk("反引号不触发命令替换", "`" in _q("`id`"))

    print("-- 日志解析（从 sherpa 输出里抓耗时/RTF）")
    log = ("Number of threads: 4\nElapsed seconds: 2.177 s\n"
           "Audio duration: 4.505 s\nReal-time factor (RTF): 2.177/4.505 = 0.483\n")
    st = parse_stats(log)
    chk("elapsed 2.177", abs(st.get("elapsed", 0) - 2.177) < 1e-6, st)
    chk("audio 4.505", abs(st.get("audio", 0) - 4.505) < 1e-6, st)
    chk("rtf 0.483", abs(st.get("rtf", 0) - 0.483) < 1e-6, st)
    chk("空日志不炸", parse_stats("") == {})
    chk("脏日志不炸", parse_stats("garbage") == {})

    print("-- wav 时长（读头，不靠外部工具）")
    tmp = tempfile.mkdtemp()
    try:
        import wave
        p = os.path.join(tmp, "s.wav")
        with wave.open(p, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000)  # 1 秒静音
        chk("1 秒 wav 读出 1.0", abs(wav_seconds(p) - 1.0) < 0.01, wav_seconds(p))
        chk("非音频文件返回 0", wav_seconds(os.path.join(tmp, "nope.txt")) == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("-- 空文本/未知后端 如实报错，不抛")
    r = run("", backend="phone_sherpa")
    chk("空文本 ok=False", r["ok"] is False and "空" in r["err"], r)
    r = run("测试", backend="nonsense_backend")
    chk("未知后端 ok=False", r["ok"] is False and "未知后端" in r["err"], r)

    print("-- 后端选择：有手机用手机，没手机退 PC/云")
    ok, why = phone_ready()
    print("     phone_ready=%r why=%r" % (ok, why))
    b = _default_backend()
    chk("默认后端在允许集合内",
        b in ("phone_sherpa", "pc_sherpa", "edge"), b)
    if not ok:
        chk("手机不可用时不会选 phone_sherpa", b != "phone_sherpa", b)

    print("\n%d/%d 通过" % (n[0] - len(fails), n[0]))
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--deploy" in sys.argv:
        print(deploy(force="--force" in sys.argv))
        sys.exit(0)
    if "--say" in sys.argv:
        i = sys.argv.index("--say")
        text = sys.argv[i + 1] if i + 1 < len(sys.argv) else "你好，我是大肥鱼。"
        print(run(text))
        sys.exit(0)
    print(__doc__)

"""天网 · 语音合成后端。手机本地优先，云 TTS 兜底。

为什么要有这一层
----------------
`fleet/storyboard.py` 原来直接调 `edge_tts` —— 那是微软的在线接口，断网就哑，
而且每条台词都要出网。漫剧一次几十分钟、几十条台词，这条路既慢又不该依赖。

现在手机自己能合成：`sherpa-onnx`（Android bionic 静态二进制）跑 CPU，
**全程离线**。

为什么不是"复用出图那个 NPU 引擎"
----------------------------------
查过了，两条路本质不同：
  · 出图引擎 `libstable_diffusion_core.so` 的 NPU 路径走 QNN HTP，它要创建
    **unsigned PD**（`DspTransport.createUnsignedPD`）。实测在 `runas_app` 域下
    返回 0x3 = EPERM —— 这是 SELinux 层面的限制，只有 App 自己的进程
    （`untrusted_app` 域）能建。所以「从 shell 直接驱动 NPU」这条路对任意模型都
    不成立，不是我们没写对。
  · TTS 不需要 NPU：几百 MB 的小模型、几百 GFLOPs，CPU 上已经够快。
    硬塞进 NPU 反而要过 QNN 转换、还可能掉精度（CosyVoice3-MNN 的记录里，
    整模型上 NPU 会导致 token 坍缩，只能把单个 q_proj 放上去，收益 5%）。

所以这里的选择是「用对的引擎」而不是「用同一个引擎」：
出图吃 NPU（算力瓶颈在那），TTS 吃 CPU（瓶颈不在那）。

三条 engine 里真正的瓶颈是**模型加载，不是合成**
------------------------------------------------
实测 8 Gen 1 上同一句台词：

    sherpa 自己打印：Elapsed 0.635 s（纯合成）
    真实墙钟      ：4.65 s（含进程启动 + 170MB onnx 解析）

**差出来的 4 秒一句都没花在合成上。** 40 条台词就是白白浪费两分半。
所以「批量」这件事的价值不在并行计算，在**少起 39 次进程** ——
`phone_batch()` 一次把 N 条推上去跑完、再一次性拉回来，就是这个意思。

后端优先级
----------
  1. `phone_sherpa`  手机本地 sherpa-onnx（离线、免费、最贴近"自己在说话"）
  2. `pc_sherpa`     PC 本地 sherpa-onnx（同一套模型，调试用）
  3. `edge`          edge-tts 云（有网、音色多，作兜底）

用法
----
    from fleet import tts_runner
    tts_runner.deploy("matcha")                # 一次性部署某个引擎（幂等）
    r = tts_runner.run("那天夜里，海上起雾了。", engine="matcha")
    print(r["path"], r["sec"], r["backend"])
"""
import os
import re
import json
import shutil
import subprocess
import tempfile
import time

import numpy as np

# ---- 手机端部署位置 ----------------------------------------------------
PHONE_BIN = "/data/local/tmp/sn-tts"
PHONE_LIBCXX = "/data/local/tmp/libc++_shared.so"
PHONE_WORK = "/data/local/tmp/tts_work"

# ---- proot 侧（IndexTTS2 这类"要跑 Python + torch"的引擎）--------------
# 为什么不塞进 sherpa：sherpa-onnx 只吃 onnx，IndexTTS2 是 PyTorch 权重 +
# 自回归 GPT + flow-matching 声码器，转换代价远大于收益。手机没有 root，
# 走 proot（Termux 版）在 shell 域里跑 aarch64 Ubuntu + torch CPU 轮子，
# 全程 adb 可驱动、不需要点屏幕。
PROOT_ROOT = "/data/local/tmp/proot"      # 手机上的真实路径
PROOT_UB = PROOT_ROOT + "/ub.sh"          # 启动器：proot -r ubuntu ... /bin/bash -c
PROOT_JOBS = PROOT_ROOT + "/jobs"         # 任务目录（文本 json / 产出 wav）
PROOT_MODELS = PROOT_ROOT + "/models"
PROOT_HOST = "/host"                      # proot 内看到的 /data/local/tmp


def _in_proot(path):
    """把手机真实路径翻成 proot 内路径。/data/local/tmp 在 proot 里是 /host。"""
    p = str(path)
    if p.startswith("/data/local/tmp/"):
        return PROOT_HOST + p[len("/data/local/tmp"):]
    raise ValueError("只能映射 /data/local/tmp 下的路径: %r" % (path,))

# ---- 引擎表：加一个新模型只改这里，不用再抄一遍拼参字符串 --------------
# kind 决定 CLI 参数怎么拼；phone_dir 是手机上的模型目录；
# refs/ref_dir 给「克隆类」引擎（zipv）用 —— 给一段参考音频 + 参考文本即可换音色。
#
# ---- 为什么只有三个引擎（2026-10-31 主人定：砍到 ≤3，砍谁要有数字）----
# 原来五个，同一句「1947年那个夏天，海上起了雾。」在真机上各跑一遍（backend 均为
# phone_sherpa，没有走云兜底）：
#     melo    音频 2.113s  RTF 0.482
#     matcha  音频 3.576s  RTF 0.119
#     zhll    音频 3.327s  RTF 0.314
#     zipv    音频 2.855s  RTF 1.366
#   · melo 砍：同样 15 个字只出 2.1 秒（7.1 字/秒），快的部分是**把「1947」吞了**；
#     而且 RTF 0.482 比 matcha 还慢 4 倍——吞字又慢，一项都不占优。
#   · zhll 砍：16kHz 电话音质（>8kHz 能量≈0），RTF 0.314 也没比 matcha 快，
#     唯一卖点是多说话人，但角色音色用 zipv 克隆更划算。
# 留下的是**质量梯度**而不是并列选项：0.12（快档）→ 1.37（自然/克隆）→ 33（精品）。
# 要恢复某个引擎：从 git 历史取回它的 ENGINES 条目，并按 selftest 第 ⑦ 条改期望列表。
ENGINES = {
    "matcha": {
        "kind": "matcha",
        "phone_dir": "/data/local/tmp/matcha",
        "pc_dir": "matcha-icefall-zh-baker",
        "vocoder": "hifigan_v2.onnx",
        "sr": 22050,
        "rule_fsts": ["number.fst", "date.fst", "phone.fst"],
        "note": "中文 Baker 女声，不吞字；需外挂声码器",
        "default_sid": 0,
    },
    "zipv": {
        "kind": "zipvoice",
        "phone_dir": "/data/local/tmp/zipv",
        "pc_dir": "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia",
        "vocoder": "vocos_24khz.onnx",
        "sr": 24000,
        "ref_dir": "test_wavs",
        "refs": {
            "news-female": "各位村民, 大家新年好! 近期, 湖北省武汉市等多个地区",
            "news-female-2": "本台消息, 中共中央国务院, 近日印发关于构建数据基础制度, 更好发挥数据要素作用的意见.",
            "leijun-1": "那还是36年前, 1987年. 我呢考上了武汉大学的计算机系.",
        },
        "num_steps": 4,
        "note": "distill-flow-matching 克隆音色，24kHz，最自然；CPU 慢(RTF~3~5)，靠批量预合成消解",
        "default_sid": 0,
    },
    "indextts2": {
        "kind": "proot",
        "phone_dir": PROOT_MODELS + "/indextts2",
        "pc_dir": "",
        "sr": 22050,
        "ref_wav": "/host/proot/ref_zh.wav",
        "diff_steps": 25,
        "threads": 4,
        "note": "IndexTTS2 1.5B 精品档，proot+torch 跑 CPU；RTF~33，一次加载跑 N 条；拿速度换质量",
        "default_sid": 0,
    },
}

# 中文场景默认的「快档」引擎（实时/近实时），批量慢档请显式传 engine="zipv"
DEFAULT_ENGINE = "matcha"

# ---- PC 端模型（与手机同一套，调试/兜底用）-----------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _tts_dir():
    """PC 上 TTS 模型根目录。模型可能不在仓库内（常见放在仓外的 _contrib/tts），
    所以按候选逐个找，找到即返回。deploy/pc_synth 都用它，避免硬编码死路径。"""
    cands = [
        os.path.join(ROOT, "_contrib", "tts"),
        os.path.join(os.path.dirname(ROOT), "_contrib", "tts"),
        os.path.expanduser("~/.fleet/tts"),
        "D:/code/_contrib/tts",
    ]
    for c in cands:
        if os.path.isdir(c):
            return c
    return cands[0]


# 手机端默认音色 0 = 中文女声（matcha 是单说话人 Baker 女声；
# zipv 的说话人由 ref 决定，不看 sid）。
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


def phone_ready(engine=DEFAULT_ENGINE):
    """手机端能不能合成指定引擎。返回 (bool, 原因)。"""
    e = ENGINES.get(engine)
    if not e:
        return False, "未知引擎 %r" % engine
    rc, out, _ = _sh(["adb", "devices"], timeout=15)
    if rc != 0 or "\tdevice" not in out:
        return False, "adb 没连上设备"

    if e["kind"] == "proot":
        # proot 引擎要的是：启动器 + 批量脚本 + 模型配置 + 参考音频，四样齐活
        probe = ("test -f %s && test -f %s/batch.py && test -f %s/config.yaml "
                 "&& test -f %s && echo READY"
                 % (PROOT_UB, PROOT_ROOT, e["phone_dir"],
                    e["ref_wav"].replace(PROOT_HOST, "/data/local/tmp")))
        rc, out, _ = _sh(["adb", "shell", probe], timeout=20)
        if rc != 0:
            return False, "adb shell 不可用"
        if "READY" not in (out or ""):
            return False, ("手机端 proot 环境不全（缺 ub.sh / batch.py / 模型 / 参考音频），"
                           "见 docs 或 _contrib/proot/setup_tts.sh")
        return True, ""

    # 二进制 + 模型目录都在才算就绪
    rc, out, _ = _sh(["adb", "shell", "ls %s %s 2>/dev/null | wc -l"
                      % (PHONE_BIN, e["phone_dir"])], timeout=20)
    if rc != 0:
        return False, "adb shell 不可用"
    try:
        n = int((out or "0").strip().splitlines()[-1])
    except (ValueError, IndexError):
        n = 0
    if n < 2:
        return False, "手机端还没部署引擎 %r（跑 tts_runner.deploy(%r)）" % (engine, engine)
    return True, ""


def _q(text):
    """把文本塞进单引号里，内部单引号按 sh 规则转义。"""
    return "'" + str(text).replace("'", "'\\''") + "'"


def _phone_args(e, out_wav, sid=0, speed=1.0, ref=None, ref_text=None, threads=4):
    """按引擎 kind 拼出 sherpa-onnx-offline-tts 的参数列表（不含文本）。

    之所以提成函数，是因为三种模型的 CLI 参数长得完全不同：
    vits 用 --vits-* + --speed；matcha 用 --matcha-* + --matcha-length-scale；
    zipv 用 --zipvoice-* 还要 --reference-audio/--reference-text。写死在脚本里
    每加一个模型就得抄一遍。
    """
    d = e["phone_dir"]
    args = [PHONE_BIN, "--num-threads=%d" % int(threads),
            "--output-filename=%s" % out_wav]

    if e["kind"] == "vits":
        args += ["--vits-model=%s/model.onnx" % d,
                 "--vits-lexicon=%s/lexicon.txt" % d,
                 "--vits-tokens=%s/tokens.txt" % d,
                 "--sid=%d" % int(sid),
                 "--speed=%s" % float(speed)]
        if e.get("rule_fsts"):
            fst = ",".join("%s/%s" % (d, f) for f in e["rule_fsts"])
            args.append("--tts-rule-fsts=%s" % fst)

    elif e["kind"] == "matcha":
        args += ["--matcha-acoustic-model=%s/%s" % (d, e.get("acoustic", "model-steps-3.onnx")),
                 "--matcha-vocoder=%s/%s" % (d, e["vocoder"]),
                 "--matcha-lexicon=%s/lexicon.txt" % d,
                 "--matcha-tokens=%s/tokens.txt" % d,
                 "--matcha-length-scale=%f" % float(1.0 / max(0.1, float(speed)))]
        if e.get("rule_fsts"):
            fst = ",".join("%s/%s" % (d, f) for f in e["rule_fsts"])
            args.append("--tts-rule-fsts=%s" % fst)

    elif e["kind"] == "zipvoice":
        ref = ref or next(iter(e.get("refs", {})), None)
        ref_text = ref_text or e.get("refs", {}).get(ref)
        if not ref or not ref_text:
            raise RuntimeError("zipvoice 需要参考音色 ref/ref_text")
        args += ["--zipvoice-encoder=%s/encoder.int8.onnx" % d,
                 "--zipvoice-decoder=%s/decoder.int8.onnx" % d,
                 "--zipvoice-vocoder=%s/%s" % (d, e["vocoder"]),
                 "--zipvoice-tokens=%s/tokens.txt" % d,
                 "--zipvoice-lexicon=%s/lexicon.txt" % d,
                 "--reference-audio=%s/%s/%s.wav" % (d, e.get("ref_dir", "test_wavs"), ref),
                 # 参考文本含中文标点/空格，必须按 sh 规则加引号，否则被 shell 截断成多参
                 "--reference-text=%s" % _q(ref_text),
                 "--num-steps=%d" % int(e.get("num_steps", 4))]

    else:
        raise RuntimeError("引擎 kind=%r 暂不支持" % e["kind"])
    return args


def _proot_batch(items, e, threads=None, diff=None, warm=1, timeout=3600):
    """在 proot 里**一次加载模型**跑完所有条目，再整目录拉回。

    和 sherpa 那条路的本质区别 —— 也是"批量"真正的价值所在：
    `phone_batch()` 对 sherpa 是 N 次进程启动，每次都要重新加载模型，批量
    只省了 adb 往返；这里是一个 Python 进程跑 N 条，**加载只发生一次**。
    IndexTTS2 实测加载 32s，100 条就是省下 53 分钟。

    还有一层：proot 里预热 page cache 后 GPT 自回归少掉缺页，同一条文本的
    RTF 从 66 掉到 33（实测 294s → 141s），这是翻倍，不是省零头。

    items: [(text, out_path, sid, speed, ref, ref_text), ...]
    返回 [(ok, path, sec, err), ...]，顺序与 items 一致。
    """
    stamp = str(int(time.time() * 1000))
    remote_dir = "%s/%s" % (PROOT_JOBS, stamp)
    remote_json = remote_dir + ".json"
    texts = [it[0] if isinstance(it, (list, tuple)) else it for it in items]

    fd, local_json = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    fd, local_script = tempfile.mkstemp(suffix=".sh")
    os.close(fd)
    try:
        with open(local_json, "w", encoding="utf-8") as f:
            json.dump(texts, f, ensure_ascii=False)

        rc, out, err = _sh(["adb", "push", local_json, remote_json], timeout=60)
        if rc != 0:
            msg = "推文本失败: %s" % (err or out).strip()[:200]
            return [(False, "", 0.0, msg) for _ in items]

        th = int(threads or e.get("threads", 4))
        ds = int(diff or e.get("diff_steps", 25))
        inner = ("export IDX_THREADS=%d IDX_DIFF_STEPS=%d IDX_WARM=%d; "
                 "cd /root && python3 /host/proot/batch.py %s %s %s"
                 % (th, ds, int(warm), e["ref_wav"],
                    _in_proot(remote_dir), _in_proot(remote_json)))
        script = ("#!/system/bin/sh\n"
                  "mkdir -p %s %s\n"
                  "cd %s\n"
                  "sh %s '%s'\n"
                  "echo EXIT=$?\n"
                  % (remote_dir, PROOT_JOBS, PROOT_ROOT, PROOT_UB, inner))
        with open(local_script, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)

        rc, out, err = _sh(["adb", "push", local_script,
                            "/data/local/tmp/proot_job.sh"], timeout=60)
        if rc != 0:
            msg = "推脚本失败: %s" % (err or out).strip()[:200]
            return [(False, "", 0.0, msg) for _ in items]
        rc, out, err = _sh(["adb", "shell", "sh /data/local/tmp/proot_job.sh"],
                           timeout=timeout)
    finally:
        for p in (local_json, local_script):
            try:
                os.remove(p)
            except OSError:
                pass

    log = (out or "") + (err or "")
    if "EXIT=0" not in log:
        tail = " ".join(log.strip().splitlines()[-4:])[:400]
        return [(False, "", 0.0, "proot 批量失败: %s" % tail) for _ in items]

    local_root = tempfile.mkdtemp()
    _sh(["adb", "pull", remote_dir, local_root], timeout=300)
    results = []
    for i, it in enumerate(items):
        local = (list(it) + [None] * 6)[1]
        if not local:
            local = os.path.join(tempfile.gettempdir(), "tts_%s_%d.wav" % (stamp, i))
        src = None
        for cand in (os.path.join(local_root, stamp, "seg_%03d.wav" % i),
                     os.path.join(local_root, "seg_%03d.wav" % i)):
            if os.path.isfile(cand) and os.path.getsize(cand) >= 1024:
                src = cand
                break
        if src:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
                shutil.copy(src, local)
                results.append((True, local, wav_seconds(local), ""))
            except Exception as ex:
                results.append((False, "", 0.0, "落盘失败: %s" % ex))
        else:
            results.append((False, "", 0.0, "远端未产出 seg_%03d.wav" % i))
    _sh(["adb", "shell", "rm -rf %s %s" % (remote_dir, remote_json)], timeout=30)
    shutil.rmtree(local_root, ignore_errors=True)
    return results


def phone_synth(text, out_path=None, engine=DEFAULT_ENGINE, sid=None, speed=1.0,
                ref=None, ref_text=None, threads=4, timeout=180):
    """手机本地合成（任意引擎）。返回 (wav 路径, 秒数, 日志)。失败抛 RuntimeError。

    文本不直接进命令行 —— 中文标点、引号、$ 都可能被 shell 吃掉。
    做法是写一个一次性脚本推上去执行，文本在脚本里以单引号形式出现。
    """
    e = ENGINES.get(engine)
    if not e:
        raise RuntimeError("未知引擎 %r" % engine)
    ok, why = phone_ready(engine)
    if not ok:
        raise RuntimeError(why)
    sid = e.get("default_sid", 0) if sid is None else int(sid)

    stamp = int(time.time() * 1000)
    if out_path is None:
        out_path = os.path.join(tempfile.gettempdir(), "tts_%d.wav" % stamp)
    remote_wav = "%s/o_%d.wav" % (PHONE_WORK, stamp)

    if e["kind"] == "proot":
        # proot 引擎不走 sherpa CLI。单条也走批量那条路（内部就是 N=1），
        # 免得维护两套拼参逻辑 —— 代价是每次都要付 32s 加载，所以单条慎用。
        ok1, path, sec, err = _proot_batch(
            [(text, out_path, sid, speed, ref, ref_text)], e,
            threads=threads, timeout=max(timeout, 600))[0]
        if not ok1:
            raise RuntimeError("手机合成失败[%s]: %s" % (engine, err))
        return path, sec, "proot"

    args = _phone_args(e, remote_wav, sid, speed, ref, ref_text, threads)
    script = (
        "#!/system/bin/sh\n"
        "mkdir -p %s\n"
        "export LD_LIBRARY_PATH=/data/local/tmp\n"
        "%s %s\n"
        "echo EXIT=$?\n"
    ) % (PHONE_WORK, " ".join(args), _q(text))

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
        raise RuntimeError("手机合成失败[%s]: %s"
                           % (engine, " ".join(log.strip().splitlines()[-3:])[:300]))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    rc, out, err = _sh(["adb", "pull", remote_wav, out_path], timeout=60)
    if rc != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) < 1024:
        raise RuntimeError("拉回音频失败: %s" % (err or out).strip()[:200])
    _sh(["adb", "shell", "rm -f %s" % remote_wav], timeout=20)

    sec = wav_seconds(out_path)
    if sec <= 0.05:
        raise RuntimeError("拉回的音频只有 %.3fs，视为失败" % sec)
    return out_path, sec, log


def phone_batch(items, engine=DEFAULT_ENGINE, threads=4, timeout=900):
    """一次性把多条台词推到手机跑完，再一次性拉回。

    items: [(text, out_path_local, sid_or_None, speed_or_1.0, ref_or_None, ref_text_or_None), ...]
    返回 [(ok, path, sec, err), ...]，顺序与 items 一致。

    价值在哪：每条 sherpa 合成前都要重新加载模型（实测加载占单句墙钟 ~85%，
    8 Gen 1 上真正合成 0.6s、墙钟 4.6s）。批量**省不掉加载**，但能把
    N 次「push 脚本 + shell + pull 音频」的 adb 往返压成 1 次往返 + 1 次整目录
    拉回。模型越慢越划算 —— zipv 一条几十秒，批量几乎零额外开销。
    """
    e = ENGINES.get(engine)
    if not e:
        return [(False, "", 0.0, "未知引擎 %r" % engine) for _ in items]
    ok, why = phone_ready(engine)
    if not ok:
        return [(False, "", 0.0, why) for _ in items]

    if e["kind"] == "proot":
        # 真正的"一次加载跑 N 条"。默认超时给足：100 条 × RTF 33 是大头。
        return _proot_batch(items, e, threads=threads,
                            timeout=max(timeout, 3600))

    stamp = int(time.time() * 1000)
    remote_dir = "%s/batch_%d" % (PHONE_WORK, stamp)
    lines = ["#!/system/bin/sh", "mkdir -p %s" % remote_dir,
             "export LD_LIBRARY_PATH=/data/local/tmp"]
    remote_paths = []
    for i, it in enumerate(items):
        text, local, sid, speed, ref, ref_text = (list(it) + [None] * 6)[:6]
        sid = e.get("default_sid", 0) if sid is None else int(sid)
        rwav = "%s/%d.wav" % (remote_dir, i)
        remote_paths.append((rwav, local))
        args = _phone_args(e, rwav, sid, speed or 1.0, ref, ref_text, threads)
        lines.append("%s %s" % (" ".join(args), _q(text)))
    lines.append("echo BATCH_DONE")
    script = "\n".join(lines) + "\n"

    fd, local_script = tempfile.mkstemp(suffix=".sh")
    os.close(fd)
    try:
        with open(local_script, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
        rc, out, err = _sh(["adb", "push", local_script, "/data/local/tmp/tts_batch.sh"],
                           timeout=60)
        if rc != 0:
            return [(False, "", 0.0, "推脚本失败: %s" % (err or out).strip()[:200])
                    for _ in items]
        rc, out, err = _sh(["adb", "shell", "sh /data/local/tmp/tts_batch.sh"],
                           timeout=timeout)
    finally:
        try:
            os.remove(local_script)
        except OSError:
            pass

    # 一次性把整目录拉回本地临时区，再按编号分发到各自目标路径
    local_root = tempfile.mkdtemp()
    _sh(["adb", "pull", remote_dir, local_root], timeout=180)
    results = []
    for i, (rwav, local) in enumerate(remote_paths):
        src = None
        for cand in (os.path.join(local_root, "%d.wav" % i),
                     os.path.join(local_root, os.path.basename(remote_dir), "%d.wav" % i)):
            if os.path.isfile(cand) and os.path.getsize(cand) >= 1024:
                src = cand
                break
        if src:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
                shutil.copy(src, local)
                results.append((True, local, wav_seconds(local), ""))
            except Exception as ex:
                results.append((False, "", 0.0, "落盘失败: %s" % ex))
        else:
            results.append((False, "", 0.0, "远端未产出 %d.wav" % i))
    _sh(["adb", "shell", "rm -rf %s" % remote_dir], timeout=20)
    shutil.rmtree(local_root, ignore_errors=True)
    return results


def phone_sherpa(text, out_path=None, sid=DEFAULT_SID, speed=1.0, threads=4,
                 timeout=180):
    """向后兼容：默认 matcha（melo 已于 2026-10-31 砍掉，见 ENGINES 上方注释）。"""
    return phone_synth(text, out_path, engine="matcha", sid=sid, speed=speed,
                       threads=threads, timeout=timeout)


def _pc_synth(text, out_path=None, engine="matcha", sid=0, speed=1.0, ref=None,
              ref_text=None, threads=4):
    """PC 端用 Python 版 sherpa_onnx 合成（引擎无关）。没有模型/库就抛 RuntimeError。

    这是调试 / 无手机时的等价替代：CLI 参数拼法和手机端 `_phone_args` 一一对应，
    保证两边行为一致。手机端有的引擎这里都能跑（vits / matcha / zipvoice）。
    """
    e = ENGINES.get(engine)
    if not e:
        raise RuntimeError("未知引擎 %r" % engine)
    try:
        import sherpa_onnx
    except ImportError:
        raise RuntimeError("没装 sherpa_onnx（pip install sherpa-onnx）")
    dl = _tts_dir()
    d = os.path.join(dl, e["pc_dir"])
    if not os.path.isdir(d):
        raise RuntimeError("PC 上没有 %s 模型（找过 %s）" % (engine, d))

    # 克隆类（zipvoice）：先把参考音频读成样本数组
    ref_samples, ref_sr = None, 48000
    if e["kind"] == "zipvoice":
        ref = ref or next(iter(e.get("refs", {})), None)
        ref_text = ref_text or e.get("refs", {}).get(ref)
        if not ref or not ref_text:
            raise RuntimeError("zipvoice 需要参考音色 ref/ref_text")
        rp = os.path.join(d, e.get("ref_dir", "test_wavs"), "%s.wav" % ref)
        import wave as _w
        with _w.open(rp, "rb") as wf:
            ref_sr = wf.getframerate()
            ref_samples = np.frombuffer(wf.readframes(wf.getnframes()),
                                       dtype="int16").astype(np.float32) / 32768.0

    if e["kind"] == "vits":
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
    elif e["kind"] == "matcha":
        voc = os.path.join(dl, e["vocoder"])
        if not os.path.isfile(voc):
            raise RuntimeError("缺 matcha 声码器 %s" % voc)
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                matcha=sherpa_onnx.OfflineTtsMatchaModelConfig(
                    acoustic_model=os.path.join(d, e.get("acoustic", "model-steps-3.onnx")),
                    vocoder=voc,
                    tokens=os.path.join(d, "tokens.txt"),
                    lexicon=os.path.join(d, "lexicon.txt"),
                    dict_dir=os.path.join(d, "dict")),
                num_threads=int(threads)),
            rule_fsts=",".join(os.path.join(d, f) for f in e.get("rule_fsts", [])),
            max_num_sentences=2)
    elif e["kind"] == "zipvoice":
        voc = os.path.join(dl, e["vocoder"])
        if not os.path.isfile(voc):
            raise RuntimeError("缺 zipvoice 声码器 %s" % voc)
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                zipvoice=sherpa_onnx.OfflineTtsZipvoiceModelConfig(
                    encoder=os.path.join(d, "encoder.int8.onnx"),
                    decoder=os.path.join(d, "decoder.int8.onnx"),
                    vocoder=voc,
                    tokens=os.path.join(d, "tokens.txt"),
                    lexicon=os.path.join(d, "lexicon.txt")),
                num_threads=int(threads)),
            max_num_sentences=2)
    else:
        raise RuntimeError("引擎 kind=%r 暂不支持（PC）" % e["kind"])

    tts = sherpa_onnx.OfflineTts(cfg)
    if e["kind"] == "zipvoice":
        a = tts.generate(text, ref_text, ref_samples, ref_sr, float(speed),
                         int(e.get("num_steps", 4)))
    else:
        a = tts.generate(text, sid=int(sid), speed=float(speed))
    if not a.samples:
        raise RuntimeError("合成结果为空（文本转 token 失败？）")
    if out_path is None:
        out_path = os.path.join(tempfile.gettempdir(),
                                "tts_%d.wav" % int(time.time() * 1000))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    import wave as _w
    with _w.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(a.sample_rate)
        w.writeframes((np.array(a.samples) * 32767).astype("int16").tobytes())
    return out_path, len(a.samples) / float(a.sample_rate), "pc sherpa-onnx/%s" % engine


def pc_sherpa(text, out_path=None, sid=DEFAULT_SID, speed=1.0, threads=4):
    """向后兼容：默认 matcha。新代码请用 _pc_synth。"""
    return _pc_synth(text, out_path, engine="matcha", sid=sid, speed=speed,
                     threads=threads)


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
    """手机 > PC > 云。手机上只要有任意已部署引擎就先用手机。"""
    # 按 ENGINES 的定义顺序试：matcha（快档，最可能已部署）排第一，
    # 不要写死引擎名清单——那正是「砍了引擎却忘了改这里」的来源。
    for eng in ENGINES:
        ok, _ = phone_ready(eng)
        if ok:
            return "phone_sherpa"
    for eng in ENGINES:
        if os.path.isdir(os.path.join(ROOT, "_contrib", "tts", ENGINES[eng]["pc_dir"])):
            return "pc_sherpa"
    return "edge"


def run(text, out_path=None, voice=None, speed=1.0, backend=None, sid=None,
        engine=None, ref=None, ref_text=None):
    """合成一段语音。

    返回 {"ok", "path", "sec", "backend", "rtf", "err"}。任何异常都不外抛 ——
    漫剧流程里 TTS 挂了应该退回「无声成片」，而不是整条片报废。

    engine 选手机/PC 本地用哪个引擎（"matcha" 默认；"zipv" 克隆音色要配 ref/ref_text）；
    voice 只在走云兜底（edge）时当云音色名用。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "path": "", "sec": 0.0, "backend": "",
                "rtf": 0.0, "err": "文本为空"}
    backend = backend or _default_backend()
    engine = engine or DEFAULT_ENGINE
    sid = DEFAULT_SID if sid is None else int(sid)
    t0 = time.time()
    log = ""
    try:
        if backend == "phone_sherpa":
            path, sec, log = phone_synth(text, out_path, engine=engine, sid=sid,
                                        speed=speed, ref=ref, ref_text=ref_text)
        elif backend == "pc_sherpa":
            path, sec, log = _pc_synth(text, out_path, engine=engine, sid=sid,
                                       speed=speed, ref=ref, ref_text=ref_text)
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
        # 本地挂了退云兜底一次，不无限转圈
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

def deploy(engine="matcha", bin_path=None, model_dir=None, libcxx=None, force=False):
    """把二进制 + libc++_shared + 某引擎的模型推到手机。幂等，已存在就跳过。

    三个文件从哪来（都写在返回值里，方便查）：
      · sn-tts        sherpa-onnx 的 android-aarch64-termux-static 里的
                      `sherpa-onnx-offline-tts`
      · libc++_shared Termux bootstrap 的 lib/ 下（NDK r26+ 版本；Android 系统
                      自带的太老，缺 __hash_memory 符号，二进制直接起不来）
      · 模型          由 engine 决定（见 ENGINES[engine]["pc_dir"]）。克隆类模型
                      （zipv）会额外把外挂声码器、参考音频一起补推上去。

    用法：
        tts_runner.deploy("matcha")    # 默认，快档（RTF 0.12，不吞字）
        tts_runner.deploy("zipv")      # flow-matching 克隆音色，24kHz（RTF 1.37）
        tts_runner.deploy("indextts2") # 精品档，proot+torch（RTF 33，只建议配合 prewarm）
    每个引擎都有一套独立模型目录，互不冲突，可以全推。
    """
    e = ENGINES.get(engine)
    if not e:
        return {"ok": False, "err": "未知引擎 %r（可选: %s）"
                % (engine, ", ".join(ENGINES)),
                "deployed": [], "skipped": []}
    dl = _tts_dir()
    src_bin = bin_path or os.path.join(
        dl, "sherpa-onnx-v2.0.1-android-aarch64-termux-static", "bin",
        "sherpa-onnx-offline-tts")
    src_lib = libcxx or os.path.join(dl, "termuxlib", "lib", "libc++_shared.so")
    src_model = model_dir or os.path.join(dl, e["pc_dir"])

    done, skipped = [], []
    # 二进制 + lib 各引擎共用，先确保到位
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

    # 模型目录
    if not os.path.isdir(src_model):
        return {"ok": False, "err": "本地缺模型目录 %s" % src_model,
                "deployed": done, "skipped": skipped}
    rc, out, _ = _sh(["adb", "shell", "ls %s 2>/dev/null" % e["phone_dir"]], timeout=15)
    if rc == 0 and e["phone_dir"].split("/")[-1] in (out or "").split() and not force:
        skipped.append("%s 模型" % engine)
    else:
        rc, out, err = _sh(["adb", "push", src_model + "/.", e["phone_dir"] + "/"],
                           timeout=900)
        if rc != 0:
            return {"ok": False, "err": "推模型失败: %s" % (err or out)[:200],
                    "deployed": done, "skipped": skipped}
        done.append("%s 模型" % engine)

    # 克隆类引擎：外挂声码器若在模型目录外（dl 根下），单独补推
    if e.get("vocoder"):
        vp = os.path.join(dl, e["vocoder"])
        if os.path.isfile(vp):
            _sh(["adb", "push", vp, "%s/%s" % (e["phone_dir"], e["vocoder"])],
                timeout=300)

    _sh(["adb", "shell", "mkdir -p %s" % PHONE_WORK], timeout=15)

    ok, why = phone_ready(engine)
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

    print("-- 引擎面：只留三档质量梯度（2026-10-31 定：砍到 ≤3）")
    chk("引擎正好三个", set(ENGINES) == {"matcha", "zipv", "indextts2"}, sorted(ENGINES))
    chk("默认引擎是快档 matcha", DEFAULT_ENGINE == "matcha", DEFAULT_ENGINE)
    for gone in ("melo", "zhll"):
        chk("%s 已下线" % gone, gone not in ENGINES, sorted(ENGINES))
    # 砍引擎最典型的连带事故：字典删了，音色库还指着它 —— 要到跑某句台词才炸。
    try:
        from fleet import voicebank as _vb
    except ImportError:
        import voicebank as _vb
    bad = {k: v["engine"] for k, v in _vb.VOICE_REGISTRY.items()
           if v["engine"] not in ENGINES}
    chk("音色库没有指向已下线引擎", not bad, bad)

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

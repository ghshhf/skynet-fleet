#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁 t50：本地中文语音合成（多引擎 + 音色银行）。

**钉住四件真事**：

  ① **引擎表驱动**：加模型只改 `tts_runner.ENGINES`，不抄拼参代码。
     每个引擎（melo/matcha/zhll/zipv）都要能拼出合法 CLI 参数。

  ② **克隆音色可行**：zipv 这类引擎靠 `ref + ref_text` 换角色，
     没有 ref/ref_text 必须明确报错，不许静默退化。

  ③ **批量预合成 + 按 id 取用**：`voicebank.prewarm` 把整批台词合成进缓存，
     `voicebank.take` 命中缓存应秒回（同一 text+voice 永远同一路径）。

  ④ **零 wifi 依赖**：本地 sherpa-onnx 合成，断网可跑。

**全离线**：合成走本地模型，不联网。
"""
import os
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAIL = []
SKIP = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  x ", msg)
        FAIL.append(msg)


def skip(msg):
    print("  --", msg)
    SKIP.append(msg)


def main():
    from fleet import tts_runner as tr
    from fleet import voicebank as vb

    # ---------- ① 引擎表：每个引擎都能拼出合法参数 ----------
    # 从 ENGINES 派生而不是写死引擎名：写死过一版（melo/matcha/zhll/zipv），
    # 砍引擎时这里就成了"漏改的第四处"。引擎表变，门禁自动跟着变。
    for eng in tr.ENGINES:
        e = tr.ENGINES[eng]
        ref = list(e.get("refs", {}))[0] if e.get("refs") else None
        rtxt = list(e.get("refs", {}).values())[0] if e.get("refs") else None
        try:
            a = tr._phone_args(e, "/tmp/x.wav", e.get("default_sid", 0), 1.0,
                               ref=ref, ref_text=rtxt, threads=4)
            ok = any("sn-tts" in s for s in a) and any("--" in s for s in a)
            check(ok, "引擎 %s 参数拼装合法（%d 个参数）" % (eng, len(a)))
        except Exception as ex:
            check(False, "引擎 %s 参数拼装抛错: %s" % (eng, ex))

    # ---------- ①b proot 引擎（indextts2）：不走 sherpa CLI，单独验分流与路径映射 ----
    # 它不能进上面那个循环 —— _phone_args 是拼 sherpa 参数的，proot 引擎进去必抛错。
    e_px = tr.ENGINES.get("indextts2")
    if e_px is None:
        check(False, "引擎表缺 indextts2（proot 精品档）")
    else:
        check(e_px.get("kind") == "proot", "indextts2 的 kind=proot（不走 sherpa CLI）")
        check(e_px.get("ref_wav", "").startswith("/host/"),
              "indextts2 的 ref_wav 用 proot 内路径（/host/...）")
        try:
            mapped = tr._in_proot("/data/local/tmp/proot/jobs/1")
            check(mapped == "/host/proot/jobs/1",
                  "proot 路径映射 /data/local/tmp -> /host（%s）" % mapped)
        except Exception as ex:
            check(False, "proot 路径映射抛错: %s" % ex)
        try:
            tr._in_proot("/sdcard/x")
            check(False, "非 /data/local/tmp 路径应拒绝映射（实际放行了）")
        except ValueError:
            check(True, "非 /data/local/tmp 路径明确拒绝映射")
        b = vb._backend_for(vb.VOICE_REGISTRY.get("精品-女", {"engine": "indextts2"}))
        check(b in ("phone_proot", "unavailable"),
              "精品-女 后端只会是 phone_proot/unavailable，不会悄悄掉进 PC（实际 %s）" % b)

    # ---------- ② zipv 缺 ref/ref_text 必须报错（引擎自身也没默认参考音色时）----------
    e_noref = {"kind": "zipvoice", "phone_dir": "/tmp/x"}
    try:
        tr._phone_args(e_noref, "/tmp/x.wav", 0, 1.0, ref=None, ref_text=None)
        check(False, "zipv 无 ref/ref_text 时报错（实际没报）")
    except RuntimeError:
        check(True, "zipv 无 ref/ref_text 明确报错")

    # ---------- ③ 音色银行：注册表 + 缓存路径确定性 ----------
    vs = vb.list_voices()
    check(len(vs) >= 3, "音色银行至少登记 3 个音色（实际 %d）" % len(vs))
    for name, spec in vs.items():
        check("engine" in spec, "音色 %s 有 engine 字段" % name)

    p1 = vb._cache_path("新闻女", "灯是在1947年点起来的。", 1.0)
    p2 = vb._cache_path("新闻女", "灯是在1947年点起来的。", 1.0)
    p3 = vb._cache_path("新闻女", "一共闪了三下。", 1.0)
    p4 = vb._cache_path("新闻女", "灯是在1947年点起来的。", 0.9)
    check(p1 == p2, "同一 text+voice+speed 路径稳定一致（缓存可命中）")
    check(p1 != p3, "不同台词 → 不同缓存路径")
    check(p1 != p4, "不同 speed → 不同缓存路径")

    # ---------- ④ 真实合成（PC 端，模型在 _contrib/tts 时生效；否则跳过）----------
    tmp = tempfile.mkdtemp(prefix="fleet-t50-")
    pc_matcha = os.path.join(tr._tts_dir(), "matcha-icefall-zh-baker")
    if os.path.isdir(pc_matcha):
        try:
            out = os.path.join(tmp, "m.wav")
            p, sec, _ = tr._pc_synth("测试一句话。", out, engine="matcha", threads=4)
            with wave.open(p, "rb") as w:
                ok_fmt = (w.getnchannels() == 1 and w.getsampwidth() == 2
                          and w.getnframes() > 0)
            check(ok_fmt, "PC 端 matcha 真合成出 16bit 单声道 WAV（%.2fs）" % sec)
        except Exception as ex:
            check(False, "PC 端 matcha 合成失败: %s" % ex)
    else:
        skip("PC 无 melo 模型（_contrib/tts/vits-melo-tts-zh_en），跳过真实合成")

    # ---------- voicebank 预合成 + 命中（PC 端可用时）----------
    if os.path.isdir(pc_melo):
        try:
            lines = ["灯是在1947年点起来的。", "一共闪了三下。"]
            res = vb.prewarm("旁白-女", lines, backend="pc_sherpa")
            ok_all = all(p and os.path.isfile(p) for p in res.values())
            check(ok_all, "voicebank.prewarm 把整批台词合成进缓存（%d 条）" % len(lines))
            hit = vb.take("旁白-女", lines[0])
            check(os.path.isfile(hit), "voicebank.take 命中已缓存台词")
        except Exception as ex:
            check(False, "voicebank 预合成/取用失败: %s" % ex)
    else:
        skip("PC 无本地模型，跳过 voicebank 真实预合成")

    print("\nFAIL=%d SKIP=%d" % (len(FAIL), len(SKIP)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

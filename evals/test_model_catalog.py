"""model_catalog 单测（全离线，不联网）。

覆盖：结构合法 / 已知档位齐全 / SD 档含 sampler·cfg·steps / URL 为 HF resolve 直链 /
按 kind 过滤 / 自定义缺省不污染目录。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet import model_catalog as MC

FAIL = []


def check(cond, msg):
    print(("  [ok] " if cond else "  [X]  ") + msg)
    if not cond:
        FAIL.append(msg)


print("① 结构合法")
check(MC.is_valid(), "is_valid() 通过（kind/URL/SD 必填项）")
check(len(MC.all()) >= 8, "目录至少 8 个档位，实有 %d" % len(MC.all()))


print("② 已知档位齐全")
for name in ("sd15-q8_0.gguf", "sd21-turbo-q8_0.gguf", "sdxl-turbo-q8_0.gguf",
             "qwen2.5-4b-instruct-q4_k_m.gguf", "gemma-2-4b-it-q4_k_m.gguf",
             "beijixing-v7-q8_0.gguf", "qwen3-vl-4b-instruct-q4_k_m.gguf",
             "mmproj-qwen3vl-4b-instruct-q8_0.gguf"):
    check(MC.get(name) is not None, "含档位 " + name)


print("③ SD 档必填项")
for n in ("sd15-q8_0.gguf", "sd21-turbo-q8_0.gguf", "sdxl-turbo-q8_0.gguf"):
    m = MC.get(n)
    check(m["kind"] == MC.SD, n + " kind=sd")
    check(m["sampler"] not in (None, ""), n + " 有 sampler")
    check(m["cfg"] is not None, n + " 有 cfg")
    check(isinstance(m["steps"], tuple) and len(m["steps"]) == 3, n + " 三档步数")
    check(m["native_size"] in (512, 1024), n + " 原生分辨率 512/1024")


print("④ URL 为 HF resolve 直链")
for n, m in MC.all().items():
    check(str(m["url"]).startswith("https://huggingface.co/")
          and "/resolve/" in str(m["url"]), n + " 是 HF resolve 直链")


print("⑤ 按 kind 过滤")
sd = MC.by_kind(MC.SD)
llm = MC.by_kind(MC.LLM)
vl = MC.by_kind(MC.VL)
check(len(sd) == 3, "SD 3 个（SD1.5 / Turbo / SDXL），实有 %d" % len(sd))
check(len(llm) == 3, "LLM 3 个（Qwen2.5-4B / Gemma-2-4B / 北极星-V7），实有 %d" % len(llm))
check(len(vl) == 2, "VL 2 个（Qwen3-VL-4B / mmproj），实有 %d" % len(vl))
check(MC.by_kind(MC.CUSTOM) == {}, "自定义不在内置目录里")


print("⑥ 已知体积写实填")
check(MC.get("sd15-q8_0.gguf")["size_bytes"] == 1_763_578_176, "SD1.5 体积写实")
check(MC.get("beijixing-v7-q8_0.gguf")["size_bytes"] == 639_446_720, "北极星-V7 体积写实")


print("model_catalog: ", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL))
sys.exit(1 if FAIL else 0)

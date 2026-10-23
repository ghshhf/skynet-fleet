"""video.gen 能力壳离线门禁（P3）。

锁死三件事：
  ① video_gen_runner.run 默认后端未配置时如实说「未配置」，不静默交空；
  ② 空 prompt 被拒；
  ③ 注入后端时正常回 path；参数（duration/fps/seed）被规整成 int。
另验：根 capabilities.describe 能给出 video.gen 说明书；common 消息类型存在。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import common
import capabilities
import video_gen_runner


def _ok_backend(prompt, duration, fps, size, seed, model):
    return f"/sdcard/Movies/fleet/{seed or 0}.mp4"


def test_unconfigured():
    r = video_gen_runner.run("海浪")
    assert r["ok"] is False, r
    assert "未配置" in r["err"], r
    print("  [ok] 默认后端未配置 -> ok=False + 未配置原因")


def test_empty_prompt():
    r = video_gen_runner.run("   ")
    assert r["ok"] is False and "prompt" in r["err"], r
    print("  [ok] 空 prompt 被拒")


def test_injected_backend_and_coercion():
    # duration/fps/seed 可能是字符串（来自 JSON），必须规整成 int 再交给后端
    r = video_gen_runner.run("日落", duration="3", fps="24", seed="-1", backend=_ok_backend)
    assert r["ok"] is True, r
    assert r["path"] == "/sdcard/Movies/fleet/-1.mp4", r
    print("  [ok] 注入后端正常回 path，字符串参数被规整成 int")


def test_spec_and_msgtypes():
    spec = capabilities.describe(["video.gen"]).get("video.gen")
    assert spec and spec["title"], spec
    assert any(p["name"] == "prompt" and p["required"] for p in spec["params"]), spec
    assert common.VIDEO_GEN == "video_gen"
    assert common.VIDEO_GEN_RESULT == "video_gen_result"
    print("  [ok] video.gen 说明书存在且 prompt 必填；common 消息类型存在")


def main():
    test_unconfigured()
    test_empty_prompt()
    test_injected_backend_and_coercion()
    test_spec_and_msgtypes()
    print("video_gen:  PASS")


if __name__ == "__main__":
    main()

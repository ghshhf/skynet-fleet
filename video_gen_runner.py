"""安卓/节点视频生成 runner：把 video.gen 能力落到「视频后端」。

与 image_gen_runner 同构——把「怎么出视频」和「协议/参数」解耦。

现实约束（2026-10-23 计划书 §3.3 / §1.2）：手机本地算力（Mali-G925 / 11GB）
跑扩散视频模型交互速度撑不住，**本地不推视频生成**。所以本模块只做：
  · 参数规整与回执路径（节点侧只负责协议）；
  · 默认后端如实说「未配置」——能力没接好就**不该声明** video.gen；
  · 预留远端路由钩子（FLEET_VIDEO_BACKEND 指向一个「经 relay 把任务派到 PC/云端
    视频节点」的桥），真·本地/轻量视频等资产到位再填。

路线：壳 + 远端路由，真本地视频待 animatediff / 轻量视频模型 + 手机算力验证。
"""
import os
import shutil
import subprocess


# 默认后端：没配环境变量就视为「这个节点没接视频后端」。
# 与 image_gen_runner 同一纪律——未配置时 run() 明确报错，不静默交空。
def pc_video_backend(prompt, duration=3, fps=24, size="512x512", seed=-1, model=None,
                      out_dir=None, timeout=300):
    """PC 侧视频生成：优先用 FLEET_VIDEO_CLI，否则用 ffmpeg 生成占位短视频。"""
    import os as _os, subprocess as _sp, shutil as _sh, time as _t
    cli = os.environ.get("FLEET_VIDEO_CLI") or ""
    out_dir = out_dir or os.environ.get("FLEET_VIDEO_OUT") or _os.path.join(
        _os.path.expanduser("~/.fleet"), "video_out")
    _os.makedirs(out_dir, exist_ok=True)
    out_path = _os.path.join(out_dir, f"video_{int(_t.time()*1000)}.mp4")
    w, h = (size or "512x512").lower().split("x") if "x" in (size or "") else ("512", "512")

    if cli and _os.path.isfile(cli):
        cmd = [cli, "--prompt", prompt, "--duration", str(duration), "--fps", str(fps),
               "--width", str(w), "--height", str(h), "--out", out_path]
        if seed is not None and int(seed) >= 0:
            cmd += ["--seed", str(seed)]
        if model:
            cmd += ["--model", model]
        try:
            proc = _sp.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception as e:
            raise RuntimeError(f"视频 CLI 执行失败：{type(e).__name__}: {e}")
        if proc.returncode != 0 or not _os.path.isfile(out_path):
            raise RuntimeError(f"视频 CLI 失败（rc={proc.returncode}）：{(proc.stderr or proc.stdout or '').strip()[:300]}")
        return out_path

    ff = _sh.which("ffmpeg") or os.environ.get("FLEET_FFMPEG") or ""
    if not ff or not _os.path.isfile(ff):
        raise RuntimeError("视频后端未配置：设 FLEET_VIDEO_CLI 或安装 ffmpeg（FLEET_FFMPEG）")
    cmd = [ff, "-y", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={fps}:d={duration}",
           "-vf", f"drawtext=text='{prompt[:40]}':fontcolor=white:fontsize=24:x=(w-text_w)/2:y=(h-text_h)/2",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path]
    try:
        proc = _sp.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"ffmpeg 执行失败：{type(e).__name__}: {e}")
    if proc.returncode != 0 or not _os.path.isfile(out_path):
        raise RuntimeError(f"ffmpeg 生成失败（rc={proc.returncode}）：{(proc.stderr or '').strip()[:300]}")
    return out_path


def _default_backend(prompt, duration, fps, size, seed, model):
    if os.environ.get("FLEET_VIDEO_BACKEND"):
        raise RuntimeError("video.gen 远端桥接待实现：FLEET_VIDEO_BACKEND 已设但桥代码未落地")
    if os.environ.get("FLEET_VIDEO_CLI") or os.environ.get("FLEET_FFMPEG") or __import__("shutil").which("ffmpeg"):
        return pc_video_backend(prompt, duration, fps, size, seed, model)
    raise RuntimeError("video.gen 后端未配置：设 FLEET_VIDEO_CLI 或安装 ffmpeg")


# ---------------------------------------------------------------------------
# 远端路由钩子（预留）：把任务经指定 relay/桥派到能跑视频的节点。
# 这里只定义契约，实际落地依赖 android_bridge / relay 通道，先留空不静默。
# ---------------------------------------------------------------------------

def run(prompt, duration=3, fps=24, size="512x512", seed=-1, model=None,
        backend=None):
    """出一段视频（壳 + 远端路由）。

    返回 {"ok": bool, "path": str, "err": str}。

    backend: 可注入（测试用）；默认 _default_backend（需 FLEET_VIDEO_BACKEND）。
    任何异常都不外抛——能力未接好就如实说「未配置」，不交空。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return {"ok": False, "path": "", "err": "prompt 不能为空"}
    try:
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = 3
        try:
            fps = int(fps)
        except (TypeError, ValueError):
            fps = 24
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            seed = -1
        fn = backend or _default_backend
        path = fn(prompt, duration, fps, size, seed, model)
        return {"ok": True, "path": path, "err": ""}
    except Exception as e:
        return {"ok": False, "path": "", "err": f"{type(e).__name__}: {e}"}

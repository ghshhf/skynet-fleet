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
def _default_backend(prompt, duration, fps, size, seed, model):
    backend = os.environ.get("FLEET_VIDEO_BACKEND")
    if not backend:
        raise RuntimeError(
            "video.gen 后端未配置：设 FLEET_VIDEO_BACKEND（视频后端地址/路由）"
            "或注入 backend 再调用；手机本地不跑视频扩散，未配时不应声称能出视频")
    # TODO(路线甲·视频壳): 经 FLEET_VIDEO_BACKEND 把任务路由到 PC/云端视频节点，
    # 反向走 relay 把成片回传。此处先抛明确未实现，等后端落地。
    raise NotImplementedError(
        f"视频后端桥接待实现（backend={backend}）；参数已就绪 "
        f"prompt={prompt!r} duration={duration} fps={fps} size={size} seed={seed}")


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

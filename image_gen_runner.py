"""安卓出图 runner：把 image.gen 能力落到手机上的 SdBridge/QueueRunner。

节点侧被调用时，先规整参数，再交给「后端」真正出图。本模块把「怎么出图」和
「协议/参数」解耦——后端可注入（测试用），节点侧只负责参数 + 结果回执。

真·手机后端需要 android_bridge 真连上手机（见 android_bridge/）。没配时 run()
明确报错「后端未配置」，而不是静默交空——能力没接好就**不该声明** image.gen。

路线甲·阶段2 收口时，_default_backend 经 android_bridge 调 SdBridge.enqueue(
prompt, neg, size, steps, seed, cfg, model)，手机把 PNG 落到 Pictures/fleet/ 并回传路径。
"""
import os
import shutil
import subprocess


# 默认后端：连手机 SdBridge。没配环境变量就视为「这个节点没接手机出图能力」。
# 调用方（node_agent / commander）应据此在注册/派发时**不声明/不派发** image.gen，
# 而不是声明了又交空。
def _default_backend(prompt, neg, size, steps, seed, cfg, model):
    bridge = os.environ.get("FLEET_ANDROID_BRIDGE")
    if not bridge:
        raise RuntimeError(
            "image.gen 后端未配置：设 FLEET_ANDROID_BRIDGE（android_bridge 地址）"
            "或注入 backend 再调用；本节点未接手机出图能力时不应声明 image.gen")
    # TODO(路线甲·阶段2收口): 经 android_bridge 调 SdBridge.enqueue，返回手机侧
    # Pictures/fleet/<seed>.png 路径。此处先抛明确未实现，等桥接代码落地。
    raise NotImplementedError(
        f"android_bridge 出图桥接待实现（bridge={bridge}）；参数已就绪 "
        f"prompt={prompt!r} size={size} steps={steps} seed={seed}")


# ---------------------------------------------------------------------------
# PicGo 出图自动上传（预留接口，CLI 未装时如实说）
#
# 流程：image.gen 产出 PNG（手机/PC）后，可选经 picgo CLI 上传到配好的图床，
#       返回上传后的 URL。PicGo CLI 缺失时 run(upload=True) 明确报错，
#       不静默返回空路径——与项目「失败带原因」铁律一致。
# 预留配置：FLEET_PICGO_BED（图床名，如 "github"/"smms" 等，与 picgo list 一致）
# ---------------------------------------------------------------------------

def _picgo_cli_path():
    """找 picgo 可执行文件。没装返回 ""。"""
    return shutil.which("picgo") or ""


def picgo_available():
    """PicGo CLI 是否可用。供上层（commander/node_agent）决定是否声明上传能力。"""
    return bool(_picgo_cli_path())


def picgo_upload(local_path, bed=None):
    """经 PicGo CLI 上传一张图，返回图床外链 URL。

    实现：`picgo upload <path> [-b <bed>]`，解析 stdout 中的 URL。
    PicGo 未安装时抛 RuntimeError，消息说清怎么装。
    """
    cli = _picgo_cli_path()
    if not cli:
        raise RuntimeError(
            "PicGo CLI 未安装：npm i -g picgo @picgo/core，或把 picgo 可执行文件加到 PATH"
            "；图床需在 picgo 里预先配好（picgo set-bed ...）")
    if not os.path.isfile(local_path):
        raise FileNotFoundError(f"本地图不存在：{local_path}")
    cmd = [cli, "upload", local_path]
    if bed:
        cmd += ["-b", bed]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:
        raise RuntimeError(f"PicGo 执行失败：{type(e).__name__}: {e}")
    if out.returncode != 0:
        raise RuntimeError(f"PicGo 上传失败（rc={out.returncode}）：{out.stderr.strip() or out.stdout.strip()}")
    # picgo 输出常含 URL，取第一行像 URL 的 token
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith("http://") or line.startswith("https://"):
            return line
    raise RuntimeError(f"PicGo 未返回可识别的 URL：{out.stdout.strip()}")


# ---------------------------------------------------------------------------
# PC 后端：stable-diffusion.cpp 的 sd-cli（FLEET_SD_CLI 指向可执行文件）。
# 未配 env 时如实说「未配置」，不静默交空；配了就 shell 出图。
# ---------------------------------------------------------------------------
def pc_sd_backend(prompt, neg="", size="512x512", steps=20, seed=-1, cfg=7.0, model=None,
                  out_dir=None, timeout=300):
    """经 stable-diffusion.cpp sd-cli 在 PC 上出图，返回 PNG 路径。

    配置：FLEET_SD_CLI（sd 可执行文件路径）；可选 FLEET_SD_MODEL（模型路径，
    或通过 model 参数传入）；FLEET_SD_OUT（输出目录，默认 ~/.fleet/sd_out）。
    未配置时抛 RuntimeError，消息说清怎么配。
    """
    cli = os.environ.get("FLEET_SD_CLI") or ""
    if not cli or not os.path.isfile(cli):
        raise RuntimeError(
            "PC 出图后端未配置：设 FLEET_SD_CLI 指向 stable-diffusion.cpp 的 sd 可执行文件"
            "（并设 FLEET_SD_MODEL 或传 model）；本节点未接 PC 出图能力时不应声明 image.gen")
    model_path = model or os.environ.get("FLEET_SD_MODEL") or ""
    if not model_path or not os.path.isfile(model_path):
        raise RuntimeError(
            "PC 出图缺少模型：设 FLEET_SD_MODEL 或传 model 指向 .gguf/.safetensors 模型文件")
    out_dir = out_dir or os.environ.get("FLEET_SD_OUT") or os.path.join(
        os.path.expanduser("~/.fleet"), "sd_out")
    os.makedirs(out_dir, exist_ok=True)
    import time as _t
    out_path = os.path.join(out_dir, f"sd_{int(_t.time()*1000)}.png")
    w, h = (size or "512x512").lower().split("x") if "x" in (size or "") else ("512", "512")
    cmd = [cli, "-m", model_path, "-p", prompt, "-o", out_path,
           "-W", str(w), "-H", str(h), "-s", str(steps), "--cfg-scale", str(cfg)]
    if neg:
        cmd += ["-n", neg]
    if seed is not None and int(seed) >= 0:
        cmd += ["--seed", str(seed)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"sd-cli 执行失败：{type(e).__name__}: {e}")
    if proc.returncode != 0 or not os.path.isfile(out_path):
        raise RuntimeError(
            f"sd-cli 出图失败（rc={proc.returncode}）："
            f"{(proc.stderr or proc.stdout or '').strip()[:300]}")
    return out_path


def run(prompt, neg="", size="512x512", steps=20, seed=-1, cfg=7.0, model=None,
        backend=None, upload=False, bed=None):
    """出一张图，可选经 PicGo 上传到图床。

    返回 {"ok": bool, "path": str, "url": str, "err": str}。

    backend: 可注入（测试用）；默认 _default_backend（需 android_bridge）。
    upload=True 时，生成成功后经 PicGo CLI 上传，把外链填入 url；
    PicGo 未安装或上传失败，err 带原因，url 为空——不静默交空。
    任何异常都不外抛。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return {"ok": False, "path": "", "url": "", "err": "prompt 不能为空"}
    try:
        fn = backend or _default_backend
        path = fn(prompt, neg, size, steps, seed, cfg, model)
        result = {"ok": True, "path": path, "url": "", "err": ""}
        if upload:
            try:
                result["url"] = picgo_upload(path, bed=bed)
            except Exception as e:
                # 上传失败不影响「出图成功」本身，但把原因写进 err
                result["err"] = f"picgo_upload: {type(e).__name__}: {e}"
        return result
    except Exception as e:
        return {"ok": False, "path": "", "url": "", "err": f"{type(e).__name__}: {e}"}

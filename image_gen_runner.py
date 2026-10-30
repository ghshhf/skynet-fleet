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
def _android_bridge_backend(prompt, neg, size, steps, seed, cfg, model):
    """经 android_bridge（手机 SdBridge）出图：HTTP POST 到桥地址，返回 PNG 路径。

    桥协议（约定）：POST {bridge}/gen  body JSON {prompt,neg,size,steps,seed,cfg,model}
    响应：{"ok":bool,"path":str,"err":str}（path 为手机侧 Pictures/fleet/<id>.png，
    PC 侧随后可经 adb pull 拉回；若桥直接返回 base64 PNG 则写入临时文件）。
    未配桥/桥不可达时抛 RuntimeError，消息说清原因，不静默交空。
    """
    import json as _j
    import urllib.request as _u
    bridge = os.environ.get("FLEET_ANDROID_BRIDGE") or ""
    if not bridge:
        raise RuntimeError(
            "image.gen 手机后端未配置：设 FLEET_ANDROID_BRIDGE（如 http://127.0.0.1:18765）"
            "并启动 android_bridge；或配 FLEET_SD_CLI 走 PC 后端")
    url = bridge.rstrip("/") + "/gen"
    payload = _j.dumps({"prompt": prompt, "neg": neg, "size": size, "steps": steps,
                        "seed": seed, "cfg": cfg, "model": model}).encode("utf-8")
    try:
        req = _u.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with _u.urlopen(req, timeout=600) as resp:
            data = _j.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"android_bridge 调用失败：{type(e).__name__}: {e}")
    if not data.get("ok"):
        raise RuntimeError(f"android_bridge 出图失败：{data.get('err') or 'unknown'}")
    path = data.get("path") or ""
    # 若桥返回 base64 PNG，落盘到 FLEET_SD_OUT 或临时目录
    if not path and data.get("png_b64"):
        import base64 as _b, os as _os, tempfile as _t
        out_dir = os.environ.get("FLEET_SD_OUT") or os.path.join(
            os.path.expanduser("~/.fleet"), "sd_out")
        _os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"bridge_{int(__import__('time').time()*1000)}.png")
        with open(out_path, "wb") as f:
            f.write(_b.b64decode(data["png_b64"]))
        path = out_path
    if not path:
        raise RuntimeError("android_bridge 未返回 path 或 png_b64")
    return path


def _default_backend(prompt, neg, size, steps, seed, cfg, model):
    """默认后端调度：手机 NPU 优先，手机 sd.cpp 桥次之，PC sd-cli 兜底。

    优先级按速度排：local-dream 走骁龙 HTP，同一 prompt 比 sd.cpp 的
    CPU/Vulkan 快数倍；桥次之（能跑但慢）；PC 最慢但显存大、模型全。
    """
    # 0) 手机 NPU。**两条路**：有 FLEET_LOCAL_DREAM_ENGINE 就自己拉起 native 引擎
    #    （不需要 App 在前台、不需要 host mode）；否则借 App 的 8808 控制口。
    host_or_direct = os.environ.get("FLEET_LOCAL_DREAM")
    if not host_or_direct:
        try:
            from fleet import engine as _eng
            if _eng.enabled():
                host_or_direct = "direct"
        except Exception:
            host_or_direct = ""
    if host_or_direct:
        return localdream_backend(prompt, neg, size, steps, seed, cfg, model)
    # 1) 手机桥（android_bridge → SdBridge）
    if os.environ.get("FLEET_ANDROID_BRIDGE"):
        return _android_bridge_backend(prompt, neg, size, steps, seed, cfg, model)
    # 2) PC sd-cli 兜底
    if os.environ.get("FLEET_SD_CLI"):
        return pc_sd_backend(prompt, neg, size, steps, seed, cfg, model)
    # 3) 均未配：如实说
    raise RuntimeError(
        "image.gen 后端未配置：设 FLEET_LOCAL_DREAM=<手机IP>（local-dream 的 QNN NPU，最快）"
        "或 FLEET_ANDROID_BRIDGE（手机 SdBridge 桥）或 FLEET_SD_CLI（PC stable-diffusion.cpp）；"
        "本节点未接出图能力时不应声明 image.gen")


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


# ---------------------------------------------------------------------------
# 手机出图后端：local-dream（骁龙 QNN NPU）。
#
# 与上面 android_bridge 那条路的区别：那条走 sd.cpp（CPU/Vulkan），这条直接
# 用 HTP 跑 diffusion，同一个 prompt 快数倍；代价是模型得先在手机的 local-dream
# 里下好，且 native 侧有全局生成互斥，一个进程同时只出一张。
#
# 手机开 host mode 后起两个口：
#   8808 控制口：/info /models /select /status /stop
#   8081 生成口：/generate（SSE）/tokenize /upscale
# 本函数只碰这两个口，不改手机侧任何代码——所以它既能给天网当后端，也能单独
# 当 local-dream 的脚本客户端用。
# ---------------------------------------------------------------------------
LOCAL_DREAM_CONTROL_PORT = 8808
LOCAL_DREAM_GENERATION_PORT = 8081


def _ld_endpoints():
    """解析 FLEET_LOCAL_DREAM → (host, control_port, generation_port)。

    接受 "192.168.1.5"、"http://192.168.1.5"、"192.168.1.5:8081"。
    带端口时视为**生成口**（最常改的就是这个），控制口由
    FLEET_LOCAL_DREAM_CONTROL 覆盖，默认 8808。

    直驱模式（设了 FLEET_LOCAL_DREAM_ENGINE=host:port）时生成口换成我们自己的
    引擎端口 —— App 的 8808 控制口就完全不需要了。
    """
    try:
        from fleet import engine as _eng
        if _eng.enabled():
            eh, ep = _eng.endpoint()
            return eh, 0, ep
    except Exception:
        pass
    raw = (os.environ.get("FLEET_LOCAL_DREAM") or "").strip()
    if not raw:
        raise RuntimeError(
            "手机出图后端未配置：设 FLEET_LOCAL_DREAM=<手机IP>（手机 local-dream 里开 host mode 后"
            "会显示地址）；或设 FLEET_ANDROID_BRIDGE 走 sd.cpp 桥、FLEET_SD_CLI 走 PC")
    if raw.startswith("http://") or raw.startswith("https://"):
        raw = raw.split("://", 1)[1]
    raw = raw.rstrip("/")
    host, _, port = raw.partition(":")
    gen = int(port) if port.isdigit() else LOCAL_DREAM_GENERATION_PORT
    ctl_raw = (os.environ.get("FLEET_LOCAL_DREAM_CONTROL") or "").strip()
    ctl = int(ctl_raw) if ctl_raw.isdigit() else LOCAL_DREAM_CONTROL_PORT
    if not host:
        raise RuntimeError(f"FLEET_LOCAL_DREAM 解析不出主机名：{os.environ.get('FLEET_LOCAL_DREAM')!r}")
    return host, ctl, gen


def _ld_opener():
    # 局域网地址绝不能走代理：环境里 HTTP_PROXY 一旦指向死端口，urllib 会把
    # 192.168.x.x 也塞给代理，报 WinError 10061，现场看着像「手机离线」。
    import urllib.request as _u
    return _u.build_opener(_u.ProxyHandler({}))


def _ld_json(opener, url, timeout, payload=None):
    import json as _j
    import urllib.request as _u
    data = None
    headers = {}
    if payload is not None:
        data = _j.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = _u.Request(url, data=data, headers=headers)
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        return _j.loads(raw)
    except ValueError:
        raise RuntimeError(f"{url} 返回的不是 JSON：{raw[:200]}")


def _ld_select(opener, host, ctl, model, width, height):
    """POST /select；遇到 404 model not found 先刷新目录再试一次。

    后推上去的模型（adb 直接塞进 files/models/）不在 App 内存里的模型表里，
    /select 会回 404。RemoteHostService 的 /models 是**全量重扫**，注释里
    写明「让 host mode 启动后才下载/导入的模型可见」——所以刷新一次就能看见。
    不刷的话，自动化场景（没人手点 App）会一直报「model not found」。
    """
    url = f"http://{host}:{ctl}/select"
    payload = {"model_id": model, "width": width, "height": height}
    try:
        return _ld_json(opener, url, 30, payload)
    except Exception as e:
        name = type(e).__name__
        if "HTTPError" not in name and "404" not in str(e):
            raise
        try:
            _ld_json(opener, f"http://{host}:{ctl}/models", 180)
        except Exception:
            pass
        return _ld_json(opener, url, 30, payload)


def _ld_ensure_model(opener, host, ctl, model, width, height, wait_s=300):
    """确保手机侧加载的是 model 且跑在 width×height。返回实际生效的 model id。"""
    import time as _t
    try:
        from fleet import engine as _eng
        direct = _eng.enabled()
    except Exception:
        direct = False
    if direct:
        # 直驱：自己拉起 native 引擎进程（App 不需要在前台，也不需要 host mode）
        info = _eng.start(model, int(width), int(height), port=None, host=host,
                          timeout=wait_s)
        if not info.get("ok"):
            raise RuntimeError("手机 NPU 引擎起不来：%s" % (info.get("err") or "未知"))
        return info.get("model") or model or ""

    status = _ld_json(opener, f"http://{host}:{ctl}/status", 15)
    serving = status.get("serving_model_id")
    # 分辨率是**后端启动参数**，不是请求参数：SD1.5 NPU 的 unet.bin 按分辨率切过，
    # 非 512 要在拉起进程时传 `--patch 768.patch`。所以宽高一变就必须重新 select，
    # 哪怕模型还是同一个——拿 512 的权重跑 768 的 latent，出来的是纯噪声（实测）。
    same_size = status.get("width") == width and status.get("height") == height
    if serving and not same_size:
        model = model or serving
    if not model or (serving == model and same_size):
        if status.get("state") == "error":
            raise RuntimeError(
                f"手机后端处于错误态：{status.get('message') or '未知'}"
                f"（模型 {status.get('error_model_id') or '?'}）")
        return serving or ""

    # /select 是异步的：它只是把加载请求交给 BackendService，进程重启并加载
    # 权重要几十秒，必须轮询 /status 到 running 才能发 /generate，否则会打空。
    sel = _ld_select(opener, host, ctl, model, width, height)
    if isinstance(sel, dict) and sel.get("error"):
        raise RuntimeError(f"手机 /select 拒绝：{sel.get('error')}")

    deadline = _t.time() + wait_s
    last = ""
    while _t.time() < deadline:
        st = _ld_json(opener, f"http://{host}:{ctl}/status", 15)
        state = st.get("state") or ""
        last = state
        if (state == "running" and st.get("serving_model_id") == model
                and st.get("width") == width and st.get("height") == height):
            return model
        if state == "error":
            raise RuntimeError(
                f"手机加载模型失败：{st.get('message') or '未知'}"
                f"（模型 {st.get('error_model_id') or model}）")
        _t.sleep(1.0)
    raise RuntimeError(
        f"等手机加载模型 {model} 超过 {wait_s}s，最后状态={last or '?'}；"
        "模型太大或没下载完时会出现这种情况")


def _ld_wait_health(opener, host, gen, timeout=150):
    """等生成口真的在听。

    /status 先变 running、8081 后起来（进程还要加载 800MB 权重），中间那几秒
    请求会被直接断掉。不探这一步，报出来的是「拒连」，看着像手机掉线。
    """
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        try:
            with opener.open(f"http://{host}:{gen}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        _t.sleep(1.0)
    raise RuntimeError(
        f"手机 {host}:{gen} 等 {timeout}s 还没开始监听——/status 说 running，"
        "但后端进程可能还在加载权重，或者已经退出了（看手机 logcat 的 BackendService）")


def _ld_stream_generate(opener, url, payload, timeout):
    """POST /generate 并逐行读 SSE，返回 (image_b64, meta)。

    local-dream 的 complete 事件长这样：
        event: complete
        data: {"type":"complete","image":"<base64>","format":"png","seed":…}
    """
    import json as _j
    import urllib.request as _u
    body = _j.dumps(payload).encode("utf-8")
    req = _u.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    event = ""
    with opener.open(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                obj = _j.loads(data)
            except ValueError:
                continue
            if event == "error" or obj.get("type") == "error":
                raise RuntimeError(f"手机出图报错：{obj.get('message') or '未知'}")
            if event == "complete" or obj.get("type") == "complete":
                image = obj.get("image") or ""
                if not image:
                    raise RuntimeError("手机 complete 事件里没有 image 字段")
                return image, obj
    raise RuntimeError("手机 SSE 流结束了但没有收到 complete 事件（出图被中断或客户端超时）")


def localdream_backend(prompt, neg="", size="512x512", steps=20, seed=-1, cfg=7.0,
                       model=None, out_dir=None, timeout=900):
    """经手机 local-dream（QNN NPU）出图，把 PNG 落到本机，返回路径。

    配置：FLEET_LOCAL_DREAM=<手机IP>；可选 FLEET_LOCAL_DREAM_CONTROL（默认 8808）、
    FLEET_SD_OUT（输出目录）。model 给定时会先 /select 并等加载完成。
    未配置或手机不可达时抛 RuntimeError，消息说清原因——不静默交空。
    """
    import base64 as _b
    import time as _t
    host, ctl, gen = _ld_endpoints()
    opener = _ld_opener()
    try:
        from fleet import engine as _eng
        direct = _eng.enabled()
    except Exception:
        direct = False

    if not direct:
        # 走 App 模式才需要 App 的控制口；直驱模式下手机侧没有 8808
        try:
            info = _ld_json(opener, f"http://{host}:{ctl}/info", 10)
        except Exception as e:
            raise RuntimeError(
                f"连不上手机控制口 {host}:{ctl}（{type(e).__name__}: {e}）——"
                "多半是 host mode 没开，或手机不在同一网段")
        if info.get("app") != "localdream":
            raise RuntimeError(
                f"{host}:{ctl} 不是 local-dream 的控制口（/info 返回 app={info.get('app')!r}）")

    w, h = 512, 512
    if size and "x" in str(size).lower():
        try:
            w, h = (int(v) for v in str(size).lower().split("x", 1))
        except ValueError:
            raise RuntimeError(f"size 解析失败：{size!r}（要 WxH，如 512x512）")

    _ld_ensure_model(opener, host, ctl, model, w, h)
    _ld_wait_health(opener, host, gen)

    payload = {
        "prompt": prompt,
        "negative_prompt": neg or "",
        "steps": int(steps),
        "cfg": float(cfg),
        "width": w,
        "height": h,
        # 默认是 "raw"（原始像素），必须显式要 png，否则拿到的不是 PNG 文件
        "output_format": "png",
        "show_diffusion_process": False,
    }
    if seed is not None and int(seed) >= 0:
        payload["seed"] = int(seed)

    try:
        image_b64, meta = _ld_stream_generate(
            opener, f"http://{host}:{gen}/generate", payload, timeout)
    except Exception as e:
        # 8081 是 native 后端进程自己监听的：没加载模型就没这个端口。
        # 直接 POST 拿到的 RemoteDisconnected 看着像断网，实际是后端没起——
        # 和 App 那句「设备可能不受支持」同一个病：报现象不报病因。
        name, text = type(e).__name__, str(e)
        if "RemoteDisconnected" in name or "Remote end closed" in text:
            raise RuntimeError(
                f"手机 :{gen} 拒连——8081 只有 native 后端加载模型后才会监听，"
                "/status 是 idle 时它就是死的。先 /select（或手机上点进模型页）再生成。"
                f"（原始错误 {name}: {text}）")
        raise

    out_dir = out_dir or os.environ.get("FLEET_SD_OUT") or os.path.join(
        os.path.expanduser("~"), ".fleet", "sd_out")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(
        out_dir, f"localdream_{int(_t.time() * 1000)}_{meta.get('seed', 'na')}.png")
    try:
        with open(out_path, "wb") as f:
            f.write(_b.b64decode(image_b64))
    except Exception as e:
        raise RuntimeError(f"手机出图成功但写盘失败：{type(e).__name__}: {e}")
    if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"手机出图成功但落盘为空：{out_path}")
    return out_path


def _ld_upscalers(opener, host, ctl):
    """GET /models 拿已装放大模型的 path（手机上权重文件的绝对路径）。"""
    d = _ld_json(opener, f"http://{host}:{ctl}/models", 180)
    return d.get("upscalers") or []


def localdream_upscale(image_path, upscaler="realistic", out_dir=None, timeout=600):
    """手机 QNN NPU 4 倍放大，返回落盘路径。

    /upscale 跟 /generate 不是一个协议：**body 直接是裸 RGB 字节**
    （长度必须等于 W*H*3），尺寸和权重路径走 header（X-Image-Width/Height/
    X-Upscaler-Path），回的是 image/jpeg + X-Output-Width/Height/Duration-Ms。

    为什么这条路线值得单列：512×512 出图 4s + 放大 4s = **8 秒拿到 2048×2048**，
    比直接出 1024×768（38s）又大又快。这是手机出图真正的性价比路线。

    前置条件：/upscale 复用生成进程里的 QNN runtime，host mode 刚起、
    还没 /select 过任何模型时，native 会抛 "QNN runtime is not initialized"。
    """
    import time as _t

    import urllib.request as _u
    from PIL import Image

    host, ctl, gen = _ld_endpoints()
    opener = _ld_opener()
    if not image_path or not os.path.isfile(image_path):
        raise RuntimeError(f"找不到要放大的图：{image_path!r}")

    im = Image.open(image_path).convert("RGB")
    w, h = im.size
    body = im.tobytes()

    ups = _ld_upscalers(opener, host, ctl)
    if not ups:
        raise RuntimeError(
            "手机上没装放大模型。要下 upscaler 权重（约 24MB）："
            "xororz/upscaler 的 4x_UltraSharpV2_Lite/upscaler_<芯片>.bin，"
            "放到 files/models/upscaler_realistic/upscaler.bin")

    target = None
    want = (upscaler or "").strip()
    for u in ups:
        uid = str(u.get("id") or "")
        if not want or uid == want or uid == "upscaler_" + want or want in uid:
            target = u
            break
    if target is None:
        target = ups[0]
    up_path = str(target.get("path") or "")
    if not up_path:
        raise RuntimeError(f"放大模型 {target.get('id')} 没有可用的权重路径")

    req = _u.Request(
        f"http://{host}:{gen}/upscale", data=body,
        headers={"Content-Type": "application/octet-stream",
                 "X-Image-Width": str(w), "X-Image-Height": str(h),
                 "X-Upscaler-Path": up_path},
        method="POST")
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read()
            out_w = resp.headers.get("X-Output-Width")
            out_h = resp.headers.get("X-Output-Height")
            dur = resp.headers.get("X-Duration-Ms")
    except Exception as e:
        name, text = type(e).__name__, str(e)
        if "RemoteDisconnected" in name or "Remote end closed" in text:
            raise RuntimeError(
                f"手机 :{gen} 拒连——8081 只有 native 后端起来才会监听。"
                "先 /select 一个模型（或手机上点进模型页）再放大")
        blob = ""
        try:
            blob = e.read().decode("utf-8", "replace")  # HTTPError 带 body
        except Exception:
            pass
        if "runtime is not initialized" in blob or "not initialized" in blob:
            raise RuntimeError(
                "手机 QNN runtime 还没初始化：/upscale 复用生成进程的 runtime，"
                "先 /select 一个模型把后端拉起来再放大")
        raise RuntimeError(f"手机放大失败（{name}）：{blob[:200] or text[:200]}")

    if not data:
        raise RuntimeError("手机放大返回了空 body")

    out_dir = out_dir or os.environ.get("FLEET_SD_OUT") or os.path.join(
        os.path.expanduser("~"), ".fleet", "sd_out")
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(image_path))[0]
    out_path = os.path.join(out_dir, f"{stem}_x4_{int(_t.time() * 1000)}.jpg")
    try:
        with open(out_path, "wb") as f:
            f.write(data)
    except Exception as e:
        raise RuntimeError(f"放大成功但写盘失败：{type(e).__name__}: {e}")
    if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"放大产物落盘为空：{out_path}")

    # 服务端回的尺寸是权威值，顺带回填给调用方（调用方常拿不到 header）
    localdream_upscale.last = {
        "src": f"{w}x{h}", "out": f"{out_w or '?'}x{out_h or '?'}",
        "server_ms": dur, "upscaler": target.get("id"),
    }
    return out_path


def run_upscale(image_path, upscaler="realistic", out_dir=None, backend=None):
    """放大一张图（4x）。返回 {"ok", "path", "src_size", "out_size", "server_ms", "err"}。"""
    try:
        if not image_path:
            return {"ok": False, "path": "", "src_size": "", "out_size": "",
                    "server_ms": "", "err": "image_path 不能为空"}
        fn = backend or localdream_upscale
        if backend is None:
            p = fn(image_path, upscaler, out_dir)
            meta = getattr(localdream_upscale, "last", {}) or {}
        else:
            # 注入的替身没有 header 信息，别把上一次真跑留下的 last 当它的
            p = fn(image_path)
            meta = {}
        return {"ok": True, "path": p,
                "src_size": meta.get("src", ""), "out_size": meta.get("out", ""),
                "server_ms": meta.get("server_ms", ""), "err": ""}
    except Exception as e:
        return {"ok": False, "path": "", "src_size": "", "out_size": "",
                "server_ms": "", "err": f"{type(e).__name__}: {e}"}


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


# ---------------------------------------------------------------------------
# 自测：python image_gen_runner.py --selftest
#
# 不起真机。用两个本地 HTTP 服务冒充 local-dream 的 8808 / 8081，把
# /info -> /status -> /select -> /generate(SSE) 整条链路真跑一遍。
# 手机不在手边时，这是唯一能验证「协议拼错了没有」的办法。
# ---------------------------------------------------------------------------
# 1x1 PNG：用来确认落盘的是真图片，不是空文件、也不是 base64 原文
_FAKE_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
                 "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
# 放大端点回的是 JPEG，用固定常量而不是在 handler 里现场编码——
# handler 里抛异常会直接断连接，urllib 报 RemoteDisconnected，看着像「手机拒连」。
_FAKE_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQ"
    "DQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQU"
    "FBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEA"
    "AAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEI"
    "I0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4"
    "eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo"
    "6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3"
    "AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
    "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPE"
    "xcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDmqKKK+1Pkj//Z")


def _selftest():
    import base64 as _b
    import json as _j
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    bad = []

    def check(cond, msg):
        if cond:
            print("  [ok]", msg)
        else:
            print("  [!!]", msg)
            bad.append(msg)

    def section(t):
        print("\n[" + t + "]")

    # 假手机侧的可变状态
    st = {"serving": None, "state": "idle", "mode": "ok", "payload": None,
          "selects": 0, "width": None, "height": None,
          "known": {"sd15-npu"}, "models_calls": 0, "up": None,
          "upscalers": [{"id": "upscaler_realistic",
                         "path": "/data/user/0/pkg/files/models/upscaler_realistic/upscaler.bin"}]}

    class Ctl(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200):
            body = _j.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/info":
                return self._send({"app": "localdream", "protocol": 1,
                                   "version": "fake", "device": "FakePhone"})
            if self.path == "/status":
                return self._send({
                    "serving_model_id": st["serving"], "state": st["state"],
                    "message": None, "error_model_id": None,
                    "width": st["width"], "height": st["height"]})
            if self.path == "/models":
                # 真机是全量重扫模型目录：重扫后「后推上去的模型」才可见
                st["models_calls"] += 1
                st["known"].add("late-model")
                return self._send({"models": [], "useImg2img": True,
                                   "upscalers": st["upscalers"]})
            return self._send({"error": "not found"}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = _j.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            if self.path == "/select":
                mid = body.get("model_id")
                if mid not in st["known"]:
                    return self._send({"error": "model not found"}, 404)
                st["selects"] += 1
                st["serving"] = mid
                st["width"] = body.get("width")
                st["height"] = body.get("height")
                st["state"] = "running"
                return self._send({"ok": True})
            return self._send({"error": "not found"}, 404)

    class Gen(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            ok = self.path == "/health"
            body = b"{}"
            self.send_response(200 if ok else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if self.path == "/upscale":
                raw = self.rfile.read(n) if n else b""
                st["up"] = {"bytes": len(raw),
                            "w": self.headers.get("X-Image-Width"),
                            "h": self.headers.get("X-Image-Height"),
                            "path": self.headers.get("X-Upscaler-Path")}
                if st["mode"] == "up_no_runtime":
                    body = b'{"error":{"message":"QNN runtime is not initialized for upscaling"}}'
                    self.send_response(400)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                w = int(st["up"]["w"] or 0)
                h = int(st["up"]["h"] or 0)
                if n != w * h * 3:
                    self.send_response(400)
                    self.end_headers()
                    return
                body = _b.b64decode(_FAKE_JPEG_B64)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("X-Output-Width", str(w * 4))
                self.send_header("X-Output-Height", str(h * 4))
                self.send_header("X-Duration-Ms", "7")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            try:
                st["payload"] = _j.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                st["payload"] = {}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def ev(name, obj):
                line = "event: %s\ndata: %s\n\n" % (name, _j.dumps(obj))
                self.wfile.write(line.encode("utf-8"))

            if st["mode"] == "error":
                ev("error", {"type": "error", "message": "boom"})
                return
            if st["mode"] == "noimage":
                ev("complete", {"type": "complete"})
                return
            ev("progress", {"type": "progress", "step": 1, "total": 20})
            ev("complete", {"type": "complete", "image": _FAKE_PNG_B64,
                            "format": "png", "seed": st["payload"].get("seed", 0)})

    ctl = HTTPServer(("127.0.0.1", 0), Ctl)
    gen = HTTPServer(("127.0.0.1", 0), Gen)
    for s in (ctl, gen):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    ctl_port, gen_port = ctl.server_address[1], gen.server_address[1]

    saved = {k: os.environ.get(k) for k in
             ("FLEET_LOCAL_DREAM", "FLEET_LOCAL_DREAM_CONTROL", "FLEET_SD_OUT",
              "FLEET_ANDROID_BRIDGE", "FLEET_SD_CLI", "HTTP_PROXY", "HTTPS_PROXY")}
    tmp = tempfile.mkdtemp(prefix="ld_selftest_")
    try:
        # ---- 1. 未配置：必须报「未配置」，不能静默交空
        section("未配置时如实报错")
        for k in ("FLEET_LOCAL_DREAM", "FLEET_ANDROID_BRIDGE", "FLEET_SD_CLI"):
            os.environ.pop(k, None)
        r = run("a cat")
        check(r["ok"] is False and "未配置" in r["err"], "没配后端 -> ok:false 且说未配置")
        check(r["path"] == "", "失败时 path 为空，不交假路径")

        # ---- 2. 空 prompt 先被拦，不去打网络
        section("空 prompt")
        r = run("   ")
        check(r["ok"] is False and "prompt" in r["err"], "空 prompt 被拦")

        # ---- 3. 正常链路
        section("正常链路 /info -> /status -> /generate")
        os.environ["FLEET_LOCAL_DREAM"] = "127.0.0.1:%d" % gen_port
        os.environ["FLEET_LOCAL_DREAM_CONTROL"] = str(ctl_port)
        os.environ["FLEET_SD_OUT"] = tmp
        # 故意把代理指向死端口：局域网地址若被塞进代理，这条必然失败
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:1"

        r = run("a red apple", neg="blurry", size="512x768", steps=12, seed=42, cfg=6.5)
        check(r["ok"] is True, "出图成功（且没被 HTTP_PROXY 带走）: %s" % (r["err"] or ""))
        path = r["path"]
        check(bool(path) and os.path.isfile(path), "返回的路径真实存在")
        if os.path.isfile(path):
            with open(path, "rb") as f:
                head = f.read(8)
            check(head == b"\x89PNG\r\n\x1a\n", "落盘文件是合法 PNG（不是文本/base64 原文）")
            check(os.path.getsize(path) > 0, "文件非空")

        p = st["payload"] or {}
        check(p.get("prompt") == "a red apple", "prompt 原样送到手机")
        check(p.get("negative_prompt") == "blurry", "negative_prompt 送到手机")
        check(p.get("width") == 512 and p.get("height") == 768, "WxH 正确拆分")
        check(p.get("output_format") == "png",
              "显式要 png（默认 raw，拿到的就不是图片文件）")
        check(p.get("seed") == 42, "seed 透传")
        check(p.get("show_diffusion_process") is False, "关掉逐步预览（省带宽）")

        # ---- 4. seed<0 不传，交给手机随机
        section("seed 语义")
        st["payload"] = None
        r = run("x", seed=-1)
        check(r["ok"] is True and "seed" not in (st["payload"] or {}),
              "seed=-1 时不传该字段（-1 到手机是 unsigned 溢出）")

        # ---- 5. 指定模型会触发 /select
        section("模型切换")
        before = st["selects"]
        r = run("x", model="sd15-npu")
        check(r["ok"] is True, "带 model 出图成功")
        check(st["selects"] == before + 1, "/select 被调用一次")
        check(st["serving"] == "sd15-npu", "手机侧 serving_model_id 已切到目标模型")
        before = st["selects"]
        r = run("x", model="sd15-npu")
        check(st["selects"] == before, "同模型第二次不再 /select（不重启进程）")

        # ---- 5b. 后推的模型：404 时先 /models 重扫再试（自动化场景没人手点 App）
        section("后推模型的可见性")
        st["models_calls"] = 0
        r = run("x", model="late-model")
        check(r["ok"] is True, "后推的模型能 select 上: %s" % (r["err"] or ""))
        check(st["models_calls"] == 1, "/models 被调用一次做全量重扫")
        check(st["serving"] == "late-model", "重扫后 serving 切到该模型")

        # ---- 5c. 放大：裸 RGB 协议 + header 契约 + 错误透出
        section("放大 4x（/upscale 二进制协议）")
        src = os.path.join(tmp, "up_src.png")
        from PIL import Image as _Im
        _Im.new("RGB", (64, 48), (200, 60, 60)).save(src)
        st["up"] = None
        r = run_upscale(src)
        check(r["ok"] is True, "放大成功: %s" % (r["err"] or ""))
        check(r["path"].lower().endswith(".jpg") and os.path.isfile(r["path"]),
              "回的是 JPEG 且落盘")
        check(r["out_size"] == "256x192", "服务端 X-Output-* 被读回（实际 %s）" % r["out_size"])
        up = st["up"] or {}
        check(up.get("bytes") == 64 * 48 * 3,
              "body 是裸 RGB（长度 %s，应为 %d）" % (up.get("bytes"), 64 * 48 * 3))
        check(up.get("w") == "64" and up.get("h") == "48", "宽高走 header")
        check("upscaler_realistic" in str(up.get("path")), "权重路径来自 /models 的 path")

        st["upscalers"] = []
        r = run_upscale(src)
        check(r["ok"] is False and "没装放大模型" in r["err"],
              "没装放大模型 -> 明确说没装: %s" % r["err"][:60])
        st["upscalers"] = [{"id": "upscaler_realistic",
                            "path": "/data/user/0/pkg/files/models/upscaler_realistic/upscaler.bin"}]

        st["mode"] = "up_no_runtime"
        r = run_upscale(src)
        check(r["ok"] is False and "runtime" in r["err"],
              "QNN 未初始化 -> 说清要先 /select: %s" % r["err"][:60])
        st["mode"] = "ok"

        r = run_upscale("/no/such.png")
        check(r["ok"] is False and "找不到" in r["err"], "缺图如实报错")

        # ---- 6. 手机侧报错要透出原因
        section("失败带原因")
        st["mode"] = "error"
        r = run("x")
        check(r["ok"] is False and "boom" in r["err"], "SSE error 事件的 message 被透出")
        st["mode"] = "noimage"
        r = run("x")
        check(r["ok"] is False and "image" in r["err"], "complete 缺 image 时报错")
        st["mode"] = "ok"

        # ---- 7. 连不上时不能说「出图了」
        section("手机不可达")
        os.environ["FLEET_LOCAL_DREAM"] = "127.0.0.1:1"
        os.environ["FLEET_LOCAL_DREAM_CONTROL"] = "1"
        r = run("x")
        check(r["ok"] is False and ("连不上" in r["err"] or "拒绝" in r["err"]),
              "连不上 -> 明确说连不上，而不是空结果")

        # ---- 8. 端口/主机解析
        section("地址解析")
        os.environ["FLEET_LOCAL_DREAM"] = "http://192.168.1.9:9000"
        os.environ["FLEET_LOCAL_DREAM_CONTROL"] = "9001"
        h, c, g = _ld_endpoints()
        check((h, c, g) == ("192.168.1.9", 9001, 9000),
              "http:// 前缀与自定义端口被正确解析")
        os.environ["FLEET_LOCAL_DREAM"] = "192.168.1.9"
        os.environ.pop("FLEET_LOCAL_DREAM_CONTROL", None)
        h, c, g = _ld_endpoints()
        check((h, c, g) == ("192.168.1.9", 8808, 8081), "只给 IP 时用 8808/8081 默认口")
    finally:
        for s in (ctl, gen):
            s.shutdown()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print()
    if bad:
        print("FAILED %d 项：" % len(bad))
        for m in bad:
            print("  -", m)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    import sys as _sys
    if "--selftest" in _sys.argv:
        _sys.exit(_selftest())
    print(__doc__)

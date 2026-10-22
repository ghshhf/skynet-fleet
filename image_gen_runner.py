"""安卓出图 runner：把 image.gen 能力落到手机上的 SdBridge/QueueRunner。

节点侧被调用时，先规整参数，再交给「后端」真正出图。本模块把「怎么出图」和
「协议/参数」解耦——后端可注入（测试用），节点侧只负责参数 + 结果回执。

真·手机后端需要 android_bridge 真连上手机（见 android_bridge/）。没配时 run()
明确报错「后端未配置」，而不是静默交空——能力没接好就**不该声明** image.gen。

路线甲·阶段2 收口时，_default_backend 经 android_bridge 调 SdBridge.enqueue(
prompt, neg, size, steps, seed, cfg, model)，手机把 PNG 落到 Pictures/fleet/ 并回传路径。
"""
import os


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


def run(prompt, neg="", size="512x512", steps=20, seed=-1, cfg=7.0, model=None,
        backend=None):
    """出一张图。返回 {"ok": bool, "path": str, "err": str}。

    backend: 可注入（测试用）；默认 _default_backend（需 android_bridge）。
    任何异常都不外抛——能力层要把「失败带原因」贯彻到底，不许静默交空。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return {"ok": False, "path": "", "err": "prompt 不能为空"}
    try:
        fn = backend or _default_backend
        path = fn(prompt, neg, size, steps, seed, cfg, model)
        return {"ok": True, "path": path, "err": ""}
    except Exception as e:
        return {"ok": False, "path": "", "err": f"{type(e).__name__}: {e}"}

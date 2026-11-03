# -*- coding: utf-8 -*-
"""天网 · 验证码设备：承载 captcha.* 能力。

2026-11-02 主人交办：「关于验证，滑块验证啊之类的各种各样验证，搜索开源项目，
把它融入到咱们自己的能力里面。」

能力边界的判断依据（当天搜索 + 实测，别凭印象改）
--------------------------------------------------
现代验证体系的重心已经从「考你看见什么」挪到「考你是什么环境」：
    · 扭曲文字  人类 50-86%，AI ~99.8%   → 纯感知题，对机器已经失效
    · 图片点选  人类 71-85%，AI ~100%    → 同上，难题只是难为真人
    · 行为遥测  轨迹/节奏/停留           → 这才是现在的真门槛
    · 无感评分  reCAPTCHA v3 / Turnstile → 不出题，没法「解」，只能「养」

所以本设备只做三件事，且明确**不做第四件**：
    做：认字（text）、找缺口（slider）、找点位（detect）、按人样拖（drag）
    不做：**不碰无感评分**。那是账号信誉 + IP + 环境一致性的长期账，
          不是一道题，硬解没有意义，只会把账号做脏。

为什么「找点位」和「认字」要分开两个能力
------------------------------------------
ddddocr 的 det 只给框、不给类别。点选类验证（「依次点击 花 树 鱼」）需要
「定位 + 语义匹配」两步，第二步它做不了。所以 detect 只交付「图里有哪些
可点目标」，选哪个由上层按语义决定（人能看，模型能判，别在这里假装能）。
"""
from ..core import BaseDevice, DeviceResult as R
from ..core.registry import Capability, Param
from .. import captcha as C

try:
    from .. import browser_reg as B
except Exception:
    B = None


def _check_lib():
    try:
        import ddddocr  # noqa: F401
        import cv2      # noqa: F401
        return True, ""
    except Exception as e:
        return False, "缺少依赖 ddddocr/opencv：%s（pip install ddddocr）" % e


CAPTCHA_CAPABILITIES = [
    Capability("captcha.text",
               "识别字符/数字/中文验证码图片 → 文本（本地 ONNX，不联网）",
               params=(Param("image", "str", True, "图片路径 / dataURL / base64"),
                       Param("charset", "str", doc="限定字符集可提高准确率，如 0123456789")),
               side_effect=False, check=_check_lib(),
               note="[CAP] 纯本地推理；不上传任何图片"),

    Capability("captcha.slider",
               "滑块缺口定位 → 返回缺口左上角坐标（已校正 ddddocr 的中心点偏差）",
               params=(Param("gap", "str", True, "滑块小图路径"),
                       Param("background", "str", True, "背景大图路径"),
                       Param("coord", "str", doc="center（默认，ddddocr>=1.5）/ topleft（老版本）")),
               side_effect=False, check=_check_lib(),
               note="[CAP] 只算不拖；真正拖动走 captcha.drag"),

    Capability("captcha.detect",
               "点选类验证：检测图里所有可点目标框（只给位置，不给类别）",
               params=(Param("image", "str", True, "图片路径 / dataURL / base64"),
                       Param("min_score", "json", doc="置信度下限，默认 0.3")),
               side_effect=False, check=_check_lib(),
               note="[CAP] 语义匹配需上层做；这里不假装能替人判断该点哪个"),

    Capability("captcha.drag",
               "按人类轨迹拖拽滑块手柄（真实鼠标事件，不是 JS 赋值）",
               params=(Param("selector", "str", True, "拖动手柄的选择器"),
                       Param("distance", "json", doc="拖动距离（像素）"),
                       Param("gap_x", "json", doc="缺口左边沿 x（与 handle_x 配对使用）"),
                       Param("handle_x", "json", doc="手柄初始左边沿 x"),
                       Param("port", "int", doc="调试端口，默认 9222"),
                       Param("jitter", "json", doc="纵向抖动幅度，默认 2"),
                       Param("dry", "bool", doc="只算轨迹不真拖，用于调试")),
               side_effect=True, check=_check_lib(),
               note="[CAP] 有副作用：向页面发真实鼠标按下/移动/抬起"),
]


def _pick(args, *keys):
    for k in keys:
        if args.get(k) is not None:
            return args[k]
    return None


class CaptchaDevice(BaseDevice):
    kind = "captcha"
    via = "local"

    def __init__(self, name="captcha:local", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault(
            "note", "验证码能力（识别 + 人样拖拽）；本地 ddddocr，不联网不按次计费")

    def probe(self):
        ok, why = _check_lib()
        if not ok:
            return False, why
        return True, "ddddocr + opencv 就绪"

    def caps(self):
        return tuple(c.name for c in CAPTCHA_CAPABILITIES)

    def invoke(self, cap, args=None):
        args = args or {}
        if cap not in self.caps():
            return R.unsupported(cap, "captcha 设备没有这个能力",
                                 device=self.name, via=self.via)
        try:
            if cap == "captcha.text":
                img = _pick(args, "image", "path")
                if not img:
                    return R.error(cap, "image 必填", device=self.name, via=self.via)
                d = C.solve_text(img, charset=args.get("charset"))
                return R.success(cap, self.name, via=self.via, data=d)

            if cap == "captcha.slider":
                gap, bg = args.get("gap"), args.get("background")
                if not gap or not bg:
                    return R.error(cap, "gap 与 background 都必填",
                                   device=self.name, via=self.via)
                d = C.solve_slider(gap, bg, coord=args.get("coord") or "center")
                return R.success(cap, self.name, via=self.via, data=d)

            if cap == "captcha.detect":
                img = _pick(args, "image", "path")
                if not img:
                    return R.error(cap, "image 必填", device=self.name, via=self.via)
                d = C.detect_objects(img, min_score=float(args.get("min_score") or 0.3))
                return R.success(cap, self.name, via=self.via, data=d)

            if cap == "captcha.drag":
                if B is None:
                    return R.unavailable(cap, "浏览器注册引擎不可用",
                                         device=self.name, via=self.via)
                payload = dict(
                    port=int(args.get("port") or 9222),
                    selector=args.get("selector"),
                    distance=args.get("distance"),
                    gap_x=args.get("gap_x"),
                    handle_x=args.get("handle_x"),
                    jitter=args.get("jitter", 2.0),
                    dry=bool(args.get("dry")),
                    seed=args.get("seed"),
                )
                r = B.run_verb("drag", payload, timeout=int(args.get("timeout") or 120))
                if r.get("ok"):
                    return R.success(cap, self.name, via=self.via, data=r)
                if r.get("timeout"):
                    return R.timeout(cap, r.get("err") or "拖拽超时",
                                     device=self.name, via=self.via)
                return R.error(cap, r.get("err") or "拖拽失败",
                               device=self.name, via=self.via)
        except Exception as e:
            return R.error(cap, "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)
        return R.unsupported(cap, "未实现的验证码能力", device=self.name, via=self.via)

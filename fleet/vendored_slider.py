# -*- coding: utf-8 -*-
"""天网 · 滑块验证能力（人机验证第一战场）
================================================
为什么滑块单独成族，不塞进 register.* 或 protocol.*：

    滑块是**唯一一类"人类能做、机器难做"的验证**。它卡的不是身份、不是会话，
    是「你像不像人」。所以它需要的零件和别处完全不同：
    看图（缺口在哪）+ 造轨迹（怎么拖）+ 行为采集（拖的过程像不像人）。

    把它当 register.* 的一个子步骤，就只会得到"点一下滑块"这种没用的能力。

四级零件，本族只做前两级（后两级依赖外部版本）：

    P1  视觉   找缺口在哪              ← slider.detect（本族，纯算法）
    P1  行为   怎么把滑块拖过去        ← slider.track / slider.drag（本族）
    P2  识别   这是哪家的墙            ← slider.wall（待做：现在 wall_state 只认 Cloudflare DOM，
                                          遇极验/腾讯直接判"没墙"，等于闭眼撞上去）
    P3  穿透   滑块在 iframe 里        ← slider.frame（待做：没有跨 iframe 的坐标换算）
    P4  协议   极验 w / 腾讯 collect   ← 不在本族（要跑厂商官方 JS，单独一轮）

能力族 slider.*（5 个）：

  slider.detect     给背景图 + 滑块图，算出缺口在哪、要拖多远、有多大把握
  slider.track      给距离，生成一条像人的轨迹（贝塞尔 + Fitts + 钟形速度 + 过冲 + 手抖）
  slider.drag       把轨迹真发出去（CDP 逐点 dispatchMouseEvent，不是瞬移）
  slider.auto       detect + track + drag 串起来：给两张图和滑块选择器，一步到位
  slider.solve_page 打真实页面：认墙 → 定位（含 iframe）→ 截图 → 算缺口 → 拖 → **再认墙**

边界（写死）：

  - **找不到就说找不到**。distinguish `strong` / `medium` / `weak` 三档，
    weak 意味着「我猜的」——调用方应当重试换图，而不是硬提交。
    滑块输一次只是换张图，乱提交一次可能触发风控降级。
  - **不做反检测**。本族只负责"看见"和"会拖"，指纹/环境伪装在底座层
    （P0 换 Patchright 那件事），两者别混。
  - **不碰无感验证**。Turnstile / reCAPTCHA v3 后台打分，没有滑块给你拖，
    没有"解"只有"养"。本族对它们无能为力，这是如实说明。
"""
from .core import Capability, Param


SLIDER_CAPABILITIES = [
    Capability(
        "slider.detect",
        "给背景图 + 滑块图，算出缺口位置、需要拖多远、以及这个判断有多大把握",
        params=(Param("bg", "str", required=True,
                      doc="背景图：本地路径 / base64 / data-url / http(s) URL"),
                Param("tile", "str", required=True,
                      doc="滑块图（带 alpha 的小图）：同上"),
                Param("viewport_width", "float",
                      doc="页面上的显示宽度（CSS px）。给了就自动换算 2x 图的坐标 —— "
                          "极验/腾讯背景常是 680px 图显示成 340px，不换算会差一倍"),
                Param("probe", "str",
                      doc="调试用：'y,x' 真值坐标，会额外返回该点的得分与排名")),
        note="[SLIDER] 只读：纯本地图像运算，不联网、不动页面"),

    Capability(
        "slider.track",
        "给拖动距离，生成一条像人的轨迹（贝塞尔路径 + Fitts 定时长 + 钟形速度 + 过冲回修 + 手抖）",
        params=(Param("distance", "float", required=True, doc="要拖的像素距离"),
                Param("seed", "int", doc="随机种子；给了就可复现（门禁用）"),
                Param("tile_width", "float", doc="滑块宽度，用于 Fitts 定律，默认 44")),
        note="[SLIDER] 只读：纯计算，不发送任何事件"),

    Capability(
        "slider.drag",
        "把轨迹真发出去：按住滑块 → 逐点拖动 → 松开（CDP Input.dispatchMouseEvent）",
        params=(Param("distance", "float", required=True, doc="要拖的像素距离"),
                Param("handle", "str", doc="滑块元素选择器；不传=用鼠标当前位置起手"),
                Param("port", "int", doc="CDP 调试端口，默认 9222"),
                Param("seed", "int", doc="随机种子"),
                Param("dry_run", "bool", doc="true=只生成事件序列不发送（默认 false）"),
                Param("executor", "json",
                      doc="可注入的执行器（门禁用）：callable(events, meta) -> dict；"
                          "注入后完全不走浏览器")),
        side_effect=True,
        note="[SLIDER] 有副作用：会真实拖动页面上的滑块"),

    Capability(
        "slider.auto",
        "一条龙：看两张图算缺口 → 生成轨迹 → 拖过去（失败/不确定就明确说不确定）",
        params=(Param("bg", "str", required=True, doc="背景图：路径/base64/URL"),
                Param("tile", "str", required=True, doc="滑块图：路径/base64/URL"),
                Param("handle", "str", doc="滑块元素选择器"),
                Param("viewport_width", "float", doc="页面显示宽度，用于坐标换算"),
                Param("port", "int", doc="CDP 端口，默认 9222"),
                Param("seed", "int", doc="随机种子"),
                Param("min_confidence", "str",
                      doc="可接受的最低把握：weak/medium/strong，默认 medium。"
                          "低于它的判断直接返回不提交"),
                Param("dry_run", "bool", doc="true=只算不拖"),
                Param("executor", "json", doc="可注入的执行器（门禁用）")),
        side_effect=True,
        note="[SLIDER] 有副作用：会真实拖动页面上的滑块"),

    Capability(
        "slider.solve_page",
        "打真实页面的一条龙：认墙 → 定位滑块（含跨域 iframe）→ 截图算缺口 → 拖动 → "
        "**再认墙确认到底过没过**",
        params=(Param("port", "int", doc="CDP 端口，默认 9222"),
                Param("vendor", "str", doc="指定厂商 geetest/tencent/aliyun；不传=自动认"),
                Param("min_confidence", "str", doc="最低把握，默认 medium"),
                Param("dry_run", "bool", doc="true=算出来但不拖"),
                Param("seed", "int", doc="随机种子"),
                Param("settle", "float", doc="拖完后等页面反应的秒数，默认 2.5"),
                Param("page", "json", doc="可注入 page 对象（门禁用，注入后不开浏览器）"),
                Param("executor", "json", doc="可注入的执行器（门禁用）")),
        side_effect=True,
        note="[SLIDER] 有副作用：会在真实页面上拖动滑块并提交。"
             "**passed 三态（true/false/null）如实上报，不把「拖完了」当「过了」**"),
]

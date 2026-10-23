# -*- coding: utf-8 -*-
"""agent_tools —— 手机助手的工具面：决策 schema + 服务端执行器。

为什么是这个形态（2026-10-24 定，全部有实测依据）：

1. 决策 = 7 选 1 枚举（6 工具 + none），走 `response_format: json_schema`。
   实测（Qwen3-1.7B，关思考）：16/16 全对、平均 5.1s。小模型守得住的
   约束形式只有 schema 枚举——OpenAI tool_calls 协议它发不出来（0.6B/1.7B
   都试过），多轮 agent loop 会崩（BFCL V4：4B 多轮 22%）。

2. 执行 = 服务端调能力注册表（caller callable），模型永远不直接碰 adb。
   模型输出的参数一律视为不可信：arg 只进白名单字段，执行层再校验。

3. 输出预算：工具结果一律 ≤ MAX_RESULT_CHARS(1200) 字符。
   一次搜索塞 31 万字撑爆 4096 上下文的教训（HTTP 400），参数层再防一次。

4. 规则短路：纯代码/算术/翻译/寒暄不进决策——省 4~8s 延迟，
   也补 0.6B/1.7B「不该调时乱调」的短板（BFCL：0.6B 只有 76.5%）。
"""
import json
import re
import time
import urllib.request

#: 决策模型地址。默认手机 llama-server（adb forward tcp:18080 tcp:8080）。
#: 可用环境变量覆盖；取不到时工具面整体静默跳过（对话照常答）。
DECIDER_URL = "http://127.0.0.1:18080"

#: 工具结果回填进 prompt 的上限（字符）。
MAX_RESULT_CHARS = 1200

#: 决策调用超时（秒）。手机 1.7B 实测平均 5.1s，超了就当没有工具面。
DECIDE_TIMEOUT = 10.0

# ---------------------------------------------------------------- 决策

DECIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {
            "type": "string",
            "enum": ["web_search", "app_launch", "notify_read",
                     "screenshot_describe", "device_status",
                     "wifi_survey", "none"],
        },
        "arg": {"type": "string"},
    },
    "required": ["tool", "arg"],
}

_TOOLS_LINE = (
    "可选工具：\n"
    "- web_search：联网搜索最新事实/价格/新闻/版本号\n"
    "- app_launch：打开手机上的某个应用\n"
    "- notify_read：读取手机最近的通知（谁发消息了、几个未读）\n"
    "- screenshot_describe：看一眼当前屏幕上显示的内容\n"
    "- device_status：查手机电量、温度、运行状态\n"
    "- wifi_survey：扫描附近的 Wi-Fi 网络及信号\n"
    "- none：以上都不需要\n"
)

DECIDE_SYSTEM = (
    "你是手机助手的调度器。根据用户消息判断该调用哪个工具，"
    "需要参数的填在 arg（如搜索词、应用名），不需要参数的 arg 留空。\n"
    + _TOOLS_LINE +
    "纯代码、算术、翻译、闲聊一律选 none。只输出 JSON。"
)

#: 规则短路：命中的问题直接 none，不花 5 秒问模型。
#: 刻意收窄（只挡高频且无歧义的），宁多问模型也别错杀真需求。
_RE_NONE = re.compile(
    r"写.{0,12}(函数|脚本|代码|程序)|python|java\b|javascript|"
    r"翻译|译成|怎么读|怎么说|"
    r"^(你好|您好|嗨|hi|hello|在吗|谢谢|再见|晚安|早上好)"
    r"|^\s*\d+\s*[+\-*/×÷]\s*\d+[\s=？?]*$",
    re.I,
)


def obvious_none(question):
    """规则短路：True = 明确不需要任何工具。"""
    q = (question or "").strip()
    if not q:
        return True
    return bool(_RE_NONE.search(q))


def decide(question, url=None, timeout=DECIDE_TIMEOUT):
    """问决策模型：该用哪个工具。返回 {"tool": ..., "arg": ...}。

    返回值约定（调用方必须区分三种）：
      · {"tool": "none"}                —— 模型说不用工具
      · {"tool": "<名字>", "arg": ...}  —— 用这个工具
      · None                            —— 决策层不可用（连不上/超时/输出非法），
                                           调用方按「无工具面」处理，不要重试
    """
    q = (question or "").strip()
    if not q:
        return {"tool": "none", "arg": ""}
    if obvious_none(q):
        return {"tool": "none", "arg": ""}

    payload = {
        "messages": [
            {"role": "system", "content": DECIDE_SYSTEM},
            {"role": "user", "content": q},
        ],
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_object", "schema": DECIDE_SCHEMA},
        "temperature": 0.1,
        "max_tokens": 160,
    }
    base = (url or DECIDER_URL).rstrip("/")
    try:
        req = urllib.request.Request(
            base + "/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
        txt = (d["choices"][0]["message"].get("content") or "").strip()
        o = json.loads(txt)
        tool = o.get("tool")
        if tool not in DECIDE_SCHEMA["properties"]["tool"]["enum"]:
            return None
        return {"tool": tool, "arg": (o.get("arg") or "").strip()}
    except Exception:
        # 决策层挂了不拖垮对话：这是「暂时状态」，调用方按无工具面继续。
        return None


# ---------------------------------------------------------------- 执行

def _clip(text, limit=MAX_RESULT_CHARS):
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[:limit] + f"…（已截断，原长 {len(t)} 字符）"


def _d(result):
    """DeviceResult.as_dict() → data 字段（缺省空 dict）。"""
    if not isinstance(result, dict):
        return {}
    return result.get("data") or {}


def _render_app_launch(r, arg):
    if r.get("ok"):
        fg = _d(r).get("foreground") or ""
        extra = f"（已到前台：{fg}）" if fg else ""
        return f"已打开「{arg}」{extra}"
    return f"打开「{arg}」失败：{r.get('err') or '未知原因'}"


def _render_notify(r):
    if not r.get("ok"):
        return f"读通知失败：{r.get('err') or '未知原因'}"
    rows = _d(r).get("notifications") or []
    n = _d(r).get("count", len(rows))
    if not rows:
        return "当前没有通知。"
    lines = [f"共 {n} 条通知："]
    for i, row in enumerate(rows, 1):
        if isinstance(row, dict):
            title = row.get("title") or row.get("pkg") or ""
            text = row.get("text") or ""
            when = row.get("when") or row.get("time") or ""
            seg = f"[{i}] {title}" + (f"｜{when}" if when else "")
            if text:
                seg += f"：{text}"
        else:
            seg = f"[{i}] {row}"
        lines.append(seg)
    return "\n".join(lines)


def _render_screen(r):
    if not r.get("ok"):
        return f"读屏幕失败：{r.get('err') or '未知原因'}"
    d = _d(r)
    fg = d.get("foreground") or ""
    labels = [x for x in (d.get("labels") or []) if x][:30]
    if not labels and not fg:
        return "屏幕上没有读到可读内容（可能是锁屏或全图片界面）。"
    lines = []
    if fg:
        lines.append(f"前台应用：{fg}")
    if labels:
        lines.append("屏幕上的文字：" + " | ".join(labels))
    return "\n".join(lines)


def _render_thermal(r):
    if not r.get("ok"):
        return f"读设备状态失败：{r.get('err') or '未知原因'}"
    d = _d(r)
    parts = []
    b = d.get("battery") or {}
    if isinstance(b, dict) and b.get("level") is not None:
        parts.append(f"电量 {b.get('level')}%")
    h = d.get("hottest") or {}
    if isinstance(h, dict) and h.get("c") is not None:
        parts.append(f"最热部位 {h.get('name')} {h.get('c')}℃")
    if d.get("cpu_headroom_c") is not None:
        parts.append(f"距降频线 {d['cpu_headroom_c']}℃")
    u = d.get("cpu_util_pct")
    if u is not None:
        parts.append(f"CPU 利用率 {u}%")
    if d.get("thermal_status_cn"):
        parts.append(f"热状态 {d['thermal_status_cn']}")
    v = d.get("verdict") or ""
    out = "；".join([p for p in parts if p])
    if v:
        out += f"\n{v}"
    return out or "设备状态读到了，但没有可报告的字段。"


def _render_wifi(r):
    if not r.get("ok"):
        return f"扫 Wi-Fi 失败：{r.get('err') or '未知原因'}"
    d = _d(r)
    nets = d.get("networks") or d.get("nets") or d.get("rows") or []
    if not nets:
        # 数据键名兜底：直接把 data 摘要给出去，别谎报「没有网络」
        keys = {k: v for k, v in d.items() if not isinstance(v, (dict, list))}
        return "扫描完成，但没有解析出网络列表。原始字段：" + _clip(json.dumps(
            keys, ensure_ascii=False), 300)
    lines = [f"扫到 {len(nets)} 个网络："]
    for i, n in enumerate(nets[:15], 1):
        if isinstance(n, dict):
            ssid = n.get("ssid") or "(隐藏)"
            sig = n.get("level", n.get("signal", ""))
            line = f"[{i}] {ssid}"
            if sig != "":
                line += f"（信号 {sig}）"
            if n.get("secure") or n.get("capabilities"):
                line += " 加密"
        else:
            line = f"[{i}] {n}"
        lines.append(line)
    return "\n".join(lines)


def _render_web(res):
    """web_search：复用 webtool 的搜索+正文抽取，render_block 出资料块。"""
    import webtool as WEB
    out = WEB.render_block(res, max_chars=MAX_RESULT_CHARS)
    return out


#: 工具名 → (注册表能力名, 渲染器, 是否有副作用)
EXECUTORS = {
    "app_launch":          ("app.launch", _render_app_launch, True),
    "notify_read":         ("notify.read", _render_notify, False),
    "screenshot_describe": ("screen.read", _render_screen, False),
    "device_status":       ("thermal.read", _render_thermal, False),
    "wifi_survey":         ("radio.wifi.survey", _render_wifi, False),
}


def execute(tool, arg, caller):
    """执行工具。caller(cap, args) -> DeviceResult.as_dict()。

    返回 (label, text)：label 是给 SSE/日志看的短标签，
    text 是回填给模型的资料（已限长）。失败永远返回文字说明，不抛异常——
    工具失败也是「事实」，模型应该拿到它并告诉用户。
    """
    if tool == "web_search":
        q = (arg or "").strip()
        if not q:
            return ("web_search", "（没给搜索词）")
        try:
            import webtool as WEB
            res = WEB.cached_research(q)
            return ("web_search", _render_web(res))
        except Exception as e:
            return ("web_search", f"搜索失败：{type(e).__name__}: {e}（暂时状态，可稍后重试）")

    spec = EXECUTORS.get(tool)
    if spec is None:
        return (tool, f"未知工具 {tool}（内部错误）")
    cap, render, side_effect = spec
    args = {}
    if tool == "app_launch":
        if not (arg or "").strip():
            return (cap, "（没说要打开哪个应用）")
        args = {"target": arg.strip()}
    try:
        r = caller(cap, args)
    except Exception as e:
        return (cap, f"调用 {cap} 异常：{type(e).__name__}: {e}")
    return (cap, _clip(render(r, arg) if render.__code__.co_argcount > 1 else render(r)))


# ---------------------------------------------------------------- 资料块

def render_tool_block(label, text):
    """工具结果 → system 资料块。规则与 webtool 资料块一致：标题不算事实。"""
    return (
        f"\n\n【工具 {label} 的实时结果】\n"
        f"{text}\n"
        f"（以上是刚从设备/网络取得的实时数据，回答时优先引用；"
        f"结果里的应用名/标题不算事实，正文才算）"
    )


# ---------------------------------------------------------------- 自测

def _selftest():
    import io
    from contextlib import redirect_stdout
    fails = []

    def check(name, cond):
        print(("OK  " if cond else "XX  ") + name)
        if not cond:
            fails.append(name)

    # 1. 规则短路
    for q in ("写一个 Python 函数反转字符串", "你好", "1+1 = ?", "把这段话翻译成英文",
              "谢谢"):
        check(f"短路 none：{q[:16]}", obvious_none(q)["tool"] if isinstance(
            obvious_none(q), dict) else obvious_none(q))
    for q in ("比特币现在多少钱", "打开微信", "谁给我发消息了", "手机烫不烫"):
        check(f"不短路：{q[:16]}", not obvious_none(q))

    # 2. decide 对不可达端点返回 None（不抛）
    t0 = time.time()
    r = decide("比特币现在多少钱", url="http://127.0.0.1:1", timeout=1.5)
    check("不可达端点 → None", r is None)
    check("不可达端点快速返回", time.time() - t0 < 5)

    # 3. 执行器渲染（fake caller）
    fake_notify = {"ok": True, "code": "ok", "data": {"count": 2, "notifications": [
        {"title": "微信", "text": "张三: 明天见", "when": "2分钟前"},
        {"title": "邮件", "text": "账单已出", "when": "1小时前"}]}}
    label, text = execute("notify_read", "", lambda cap, args: fake_notify)
    check("notify 渲染条数", "共 2 条" in text and "张三" in text)

    fake_screen = {"ok": True, "code": "ok", "data": {
        "foreground": "com.example/clock", "labels": ["10:24", "闹钟", "设置"]}}
    _, text = execute("screenshot_describe", "", lambda cap, args: fake_screen)
    check("screen 渲染前台与标签", "com.example" in text and "闹钟" in text)

    fake_thermal = {"ok": True, "code": "ok", "data": {
        "battery": {"level": 66}, "hottest": {"name": "CPU", "c": 43.2},
        "cpu_headroom_c": 41.8, "cpu_util_pct": 12, "thermal_status_cn": "正常",
        "verdict": "一切正常"}}
    _, text = execute("device_status", "", lambda cap, args: fake_thermal)
    check("thermal 渲染电量/温度", "66%" in text and "43.2℃" in text and "正常" in text)

    fake_wifi = {"ok": True, "code": "ok", "data": {"networks": [
        {"ssid": "Home-5G", "level": -52, "secure": True},
        {"ssid": "CoffeeShop", "level": -71}]}}
    _, text = execute("wifi_survey", "", lambda cap, args: fake_wifi)
    check("wifi 渲染列表", "Home-5G" in text and "CoffeeShop" in text)

    fail_call = {"ok": False, "code": "unavailable",
                 "err": "没有可用的手机序列（adb 无设备）", "data": {}}
    _, text = execute("notify_read", "", lambda cap, args: fail_call)
    check("失败也返回文字不抛异常", "失败" in text and "adb" in text)

    # 4. 截断
    long_r = {"ok": True, "code": "ok", "data": {"notifications": [
        {"title": "t" * 2000, "text": "x" * 2000, "when": ""}]}}
    _, text = execute("notify_read", "", lambda cap, args: long_r)
    check("结果限长 ≤1200+尾注", len(text) <= MAX_RESULT_CHARS + 60)

    # 5. 资料块
    blk = render_tool_block("notify.read", "共 1 条通知：…")
    check("资料块含来源与规则", "工具 notify.read 的实时结果" in blk and "标题不算事实" in blk)

    # 6. app_launch 参数进白名单字段
    seen = {}
    def cap_recorder(cap, args):
        seen.update({"cap": cap, "args": args})
        return {"ok": True, "code": "ok", "data": {"foreground": "com.tencent.mm"}}
    _, text = execute("app_launch", "微信", cap_recorder)
    check("app_launch 参数映射 target", seen == {"cap": "app.launch",
                                                 "args": {"target": "微信"}}
          and "com.tencent.mm" in text)

    print(f"\n{'FAIL ' + str(len(fails)) if fails else 'ALL PASS'}: {fails or ''}")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())

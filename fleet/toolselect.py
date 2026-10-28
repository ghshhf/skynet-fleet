# -*- coding: utf-8 -*-
"""工具面分域（两级决策的第 1 级）。

## 为什么要有这一层

2026-10-29 实测（Intel 核显 · 全程 CPU · 决策超时线 25s）：

    工具数   tokens   耗时
    3        491      5.6s
    10       943      8.2s
    20       1583    14.9s
    58       6271    56.2s   ← 超时 2 倍多，永远失败

CPU 上 prompt 处理约 60 tok/s、生成约 2 tok/s，58 个工具的说明一次性塞进去
物理上算不完。所以不是「模型选不对」，是「来不及答」。把候选压到域内
6~13 个之后，同一台机器 4 秒左右就能答完。

## 分两级不是为了省 token，是为了**出错时能定位**

一级只回域（6 选 1），二级才在域内选工具。哪一级错了看返回就能分清：
域错了是「规则/域名有问题」，域对工具错是「域内描述重叠」。一级混在一起
只能看到一个错误答案，改起来全靠猜。

## 规则的定位：能不花钱就别花

规则命中就直连，0 延迟、可复现、能写测试。规则不敢下判断（打平/没命中）
才去问模型选域——6 个短标签约 100 tokens，比 58 个工具便宜十倍。
**规则不是「优先」，是「兜底的确定部分」**：宁可不判，也不瞎判。
"""
import re

#: 域 → (一句话说明, 域内工具)。说明给「问模型选域」那一级看，必须短。
#: 域内工具顺序不重要，模型是按描述选的，不是按位置（位置偏置实测存在，
#: 所以不要靠调整顺序来「修」准确率——那是在骗测试）。
DOMAINS = {
    "看": ("看屏幕/画面：截图、拍照、认字、控件坐标", [
        "screen_look", "screenshot_describe", "tree_read", "pc_look",
        "pc_screenshot", "phone_see"]),
    "查": ("查状态和清单：电量、内存、进程、窗口、应用、通知、剪贴板、在线设备、产物", [
        "device_status", "pc_sysinfo", "pc_proc_list", "pc_windows",
        "apps_list", "app_foreground", "notify_read", "node_status",
        "artifact_list", "job_list", "clipboard_read", "phone_sense",
        "relay_status"]),
    "探": ("扫描/探测网络与周边：WiFi、蜂窝、蓝牙、局域网主机、暴露面、端口服务", [
        "wifi_survey", "radio_cell", "bt_scan", "net_scan", "scene_scan",
        "net_survey", "proximity_estimate", "proximity_track",
        "proximity_calibrate", "wallprobe", "ssh_banner", "mqtt_probe"]),
    "动": ("执行动作：打开应用、点按、输入文字、红外遥控/试码、开关热点、跑场景、布防", [
        "app_launch", "input_tap", "input_type", "ir_command", "ir_trial",
        "hotspot_on", "hotspot_off", "hotspot_roam", "scene_run",
        "sentinel_arm"]),
    "外": ("上外网查事实：搜索、域名/证书/WHOIS、暴露面测绘", [
        "web_search", "osint_crt", "osint_dns", "osint_rdap", "osint_shodan"]),
    "拆": ("解析一段信号、流量或文件：红外脉冲、BLE 广播、固件、协议、比特流", [
        "re_ir", "re_ble", "re_binary", "re_proto", "re_bits",
        "re_bits_extract", "re_ask", "net_dissect"]),
    "记": ("记录与哨兵：生活流记录/日报、环境与生活异常检查", [
        "life_log", "life_digest", "life_sentinel", "sentinel_check"]),
}

#: 规则关键词。权重 = 词长（长词更具体，压过短词的噪声命中）。
#: 刻意**不**收「搜索」——它会把「在搜索框里输入…」拉进「外」域。
RULES = {
    "看": ["截个图", "截屏", "截图", "长什么样", "写了什么", "什么字", "画面",
           "拍照", "拍一张", "坐标", "控件", "屏幕", "界面", "弹窗"],
    "查": ["多少电", "电量", "剩余电量", "内存", "cpu", "进程", "哪些程序",
           "窗口", "装了哪些", "哪些应用", "前台", "消息", "通知", "在线",
           "产物", "剪贴板", "光线", "温度", "磁盘", "存储", "占用"],
    "探": ["wifi", "wi-fi", "信号", "蓝牙", "局域网", "机器", "扫描", "搜到",
           "周围", "附近", "端口", "ssh", "mqtt", "摄像头", "暴露面", "校准"],
    "动": ["打开", "启动", "关掉", "关闭", "开一下", "输入", "打字", "点一下",
           "调到", "遥控", "布防", "试码", "记一笔"],
    "外": ["多少钱", "币价", "新闻", "最新", "联网", "域名", "子域名", "whois",
           "证书", "天气", "股价", "汇率", "shodan"],
    "拆": ["脉冲", "编码", "广播", "固件", "二进制", "协议", "解码", "解析",
           "ble", "比特", "流量", "抓包"],
    "记": ["生活流", "哨兵", "陌生人", "日报", "这周我都在", "有没有人进来"],
}

#: 判定门槛：最高分至少 2 分（一个双字词），且严格高于第二名。
#: 打平说明这句话两个域都像——这种时候闭嘴，交给模型。
MIN_SCORE = 2


def _score(q):
    low = q.lower()
    out = {}
    for dom, words in RULES.items():
        hits = [w for w in words if w in low]
        if hits:
            out[dom] = (sum(len(w) for w in hits), hits)
    return out


def guess_domain(q):
    """规则定域。敢判就返回 (域, 命中词)，不敢判返回 (None, 命中词)。

    返回第二项始终是命中词，方便 explain / 排障时说清「凭什么这么判」。
    只是「并列/太弱」两种情况下**不要**把决定权直接交给模型——见
    `candidate_domains`：并列时把两个域合起来问一次，比让模型猜域更稳。
    """
    doms, _hits, _why = candidate_domains(q)
    if len(doms) == 1:
        return doms[0], _hits.get(doms[0], [])
    return None, _hits.get((doms or [None])[0], [])


def candidate_domains(q):
    """规则给出候选域：返回 (域列表, 各域命中词, 判断依据)。

    三种形态，调用方按长度分支：
      · 长度 1 —— 高置信，直接用这个域；
      · 长度 2+ —— 打平，`决定权在域内选工具那一级`，把这两域的工具合起来问；
      · 长度 0 —— 规则无话可说（没命中/太弱），才去问模型「哪一类」。
    """
    sc = _score(q or "")
    if not sc:
        return [], {}, "无命中"
    ranked = sorted(sc.items(), key=lambda kv: -kv[1][0])
    top = ranked[0][1][0]
    hits = {k: v[1] for k, v in ranked}
    if top < MIN_SCORE:
        return [], hits, "命中太弱"
    tied = [k for k, v in ranked if v[0] == top]
    if len(tied) == 1:
        return tied, hits, "高置信"
    return tied, hits, "并列"


def tools_of(domain):
    return list(DOMAINS[domain][1])


def domain_of_tool(name):
    for dom, (_label, tools) in DOMAINS.items():
        if name in tools:
            return dom
    return None


def explain(q):
    """一句话说清这次分域的判断依据，给人看的。"""
    doms, hits, why = candidate_domains(q)
    if len(doms) == 1:
        return "规则判定「%s」域（%s：%s）" % (
            doms[0], why, "、".join(hits.get(doms[0], [])))
    if len(doms) > 1:
        return "规则并列（%s）→ 合并 %s 两域一起问" % (
            why, "、".join(doms))
    return "规则%s → 问模型哪一类" % why


def selftest():
    n = 0
    # 每个工具必须且只能属于一个域，否则分域就是漏的
    seen = {}
    for dom, (_l, tools) in DOMAINS.items():
        for t in tools:
            assert t not in seen, "工具 %s 同时属于 %s 和 %s" % (t, seen[t], dom)
            seen[t] = dom
    n += 1
    print("✓ 域内工具无重复（共 %d 个）" % len(seen))
    cases = [
        ("手机还剩多少电", "查"),
        ("把电脑屏幕截个图存成文件", "看"),
        ("周围能搜到几个 wifi", "探"),
        ("帮我打开微信", "动"),
        ("今天比特币多少钱", "外"),
        ("这段 BLE 广播里有什么", "拆"),
        ("在搜索框里输入今天天气", None),   # 打平 → 不判，交模型
        ("嗯", None),
    ]
    for q, want in cases:
        got, hits = guess_domain(q)
        assert got == want, "%s → 得 %r 期望 %r（命中 %s）" % (q, got, want, hits)
        n += 1
    print("✓ %d 组规则判定符合预期" % len(cases))
    print("✓ 分域自测 %d 项全过" % n)


if __name__ == "__main__":
    selftest()

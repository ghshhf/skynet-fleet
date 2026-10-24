"""零依赖 Prometheus 指标暴露（学 ollama Fork 的 P0 可观测性纪律）。

只做最小可用集：Counter / Gauge / Histogram，render() 输出 Prometheus 文本格式
（text/plain; version=0.0.4）。不引 prometheus_client——铁律：纯 CPU 文本处理零第三方依赖。

为什么要有它：刚做好的免费多端 failover（pollinations/ovh/llm7 降级链 + 冷却 + 健康门控）
现在是黑盒——哪个后端慢、哪个在冷却、failover 切了几刀、令牌花多少，全看不见。
自主发散模式下这是「盲飞」，所以把关键路径全埋点。

用法：
    from fleet import metrics as M
    M.ai_request_total.inc({"backend": "pollinations", "result": "ok"})
    M.ai_failure_total.inc({"backend": "ovh", "kind": "rate_limit"})
    M.ai_fallback_total.inc()
    M.ai_latency.observe(0.62, {"backend": "pollinations"})
    M.ai_healthy_backends.set(len(AB.healthy_backends()))
    print(M.render())
"""
import time
from collections import defaultdict


_PREFIX = "fleet_"


def _esc(s):
    """Prometheus label value 转义：反斜杠与双引号。"""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _fmt(v):
    """浮点渲染：整数不带 .0，其余按原值（Prometheus 要的是数字文本）。"""
    if v == int(v):
        return str(int(v))
    return repr(float(v))


def _label_str(labelnames, labels):
    if not labelnames:
        return ""
    parts = ['%s="%s"' % (n, _esc(labels.get(n, ""))) for n in labelnames]
    return "{" + ",".join(parts) + "}"


class _Base:
    def __init__(self, name, kind, help_text, labelnames=()):
        self.name = _PREFIX + name
        self.kind = kind
        self.help = help_text
        self.labelnames = tuple(labelnames)
        self._series = {}        # labels_key(tuple) -> 当前值
        self._order = []         # 保持 series 出现顺序（渲染稳定）

    def _key(self, labels):
        if not self.labelnames:
            return ()
        return tuple(str(labels.get(n, "")) for n in self.labelnames)

    def _add(self, labels, delta):
        k = self._key(labels)
        if k not in self._series:
            self._order.append(k)
            self._series[k] = 0.0
        self._series[k] += delta

    def _set(self, labels, value):
        k = self._key(labels)
        if k not in self._series:
            self._order.append(k)
        self._series[k] = float(value)

    def render(self, out):
        out.append("# HELP %s %s" % (self.name, self.help))
        out.append("# TYPE %s %s" % (self.name, self.kind))
        for k in self._order:
            labels = dict(zip(self.labelnames, k)) if self.labelnames else {}
            out.append("%s%s %s" % (self.name, _label_str(self.labelnames, labels),
                                    _fmt(self._series[k])))


class Counter(_Base):
    def __init__(self, name, help_text, labelnames=()):
        super().__init__(name, "counter", help_text, labelnames)

    def inc(self, labels=None, value=1):
        self._add(labels or {}, value)


class Gauge(_Base):
    def __init__(self, name, help_text, labelnames=()):
        super().__init__(name, "gauge", help_text, labelnames)

    def set(self, value, labels=None):
        self._set(labels or {}, value)

    def inc(self, labels=None, value=1):
        self._add(labels or {}, value)

    def dec(self, labels=None, value=1):
        self._add(labels or {}, -value)


class Histogram(_Base):
    """直方图：固定桶 + _sum/_count。bucket 标签 __le 是动态的，单独存。"""

    def __init__(self, name, help_text, labelnames=(),
                 buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)):
        super().__init__(name, "histogram", help_text, labelnames)
        self.buckets = tuple(buckets)
        # (label_key, bucket) -> 计数；label_key -> (sum, count)
        self._buckets = defaultdict(float)
        self._agg = defaultdict(lambda: [0.0, 0.0])   # [sum, count]

    def observe(self, value, labels=None):
        labels = labels or {}
        k = self._key(labels)
        if k not in self._order:
            self._order.append(k)
        for b in self.buckets:
            if value <= b:
                self._buckets[(k, b)] += 1
        a = self._agg[k]
        a[0] += value
        a[1] += 1

    def render(self, out):
        out.append("# HELP %s %s" % (self.name, self.help))
        out.append("# TYPE %s histogram" % self.name)
        seen = set()
        for k in self._order:
            seen.add(k)
            labels = dict(zip(self.labelnames, k)) if self.labelnames else {}
            base = _label_str(self.labelnames, labels)
            for b in self.buckets:
                cnt = self._buckets.get((k, b), 0.0)
                out.append("%s_bucket%s %s" % (
                    self.name, _join_le(base, b), _fmt(cnt)))
            s, c = self._agg.get(k, [0.0, 0.0])
            out.append("%s_bucket%s %s" % (self.name, _join_le(base, "+Inf"), _fmt(c)))
            out.append("%s_sum%s %s" % (self.name, base, _fmt(s)))
            out.append("%s_count%s %s" % (self.name, base, _fmt(c)))
        # 兜底：observe 先于任何 _order 进来的情况（理论上 _order 已覆盖）
        for k in list(self._agg):
            if k not in seen:
                seen.add(k)


def _join_le(base, le):
    # base 形如 {backend="x"}；Prometheus 要求 le 作为标签塞进同一对花括号里。
    # 拼到 } 外面（{backend="x"},le="5"）是非法格式，抓取端会整条丢指标。
    # 无标签时也不能漏花括号（le="5" 同样是非法行）。
    if base:
        if base.endswith("}"):
            return "%s,%s}" % (base[:-1], _le(le))
        return "%s,%s" % (base, _le(le))
    return "{%s}" % _le(le)


def _le(le):
    return 'le="%s"' % _esc(le)


# ---------------------------------------------------------------- 指标实例
# 顺序即渲染顺序：先计数器，再直方图，最后瞬时量。

ai_request_total = Counter(
    "ai_requests_total",
    "AI 后端请求数（按后端与结果：ok/empty/err）", ("backend", "result"))
ai_failure_total = Counter(
    "ai_failures_total",
    "AI 后端失败数（按后端与失败类型：transient/rate_limit/auth/request/empty）",
    ("backend", "kind"))
ai_cooldown_total = Counter(
    "ai_cooldown_total",
    "AI 后端进入冷却次数（按后端与触发类型）", ("backend", "kind"))
ai_fallback_total = Counter(
    "ai_fallback_switches_total",
    "AI 降级链切到下一条候选端点的次数（首条即成功则不计数）")
ai_latency = Histogram(
    "ai_latency_seconds",
    "AI 单端响应延迟（秒，从发请求到拿到正文）", ("backend",))
ai_healthy_backends = Gauge(
    "ai_healthy_backends", "当前健康（未冷却、可进轮转）的 AI 后端数")
ai_cooldown_active = Gauge(
    "ai_cooldown_active", "当前处于冷却中的 AI 后端数")

_REGISTRY = [ai_request_total, ai_failure_total, ai_cooldown_total,
             ai_fallback_total, ai_latency, ai_healthy_backends, ai_cooldown_active]


def render():
    """输出 Prometheus 文本格式（text/plain; version=0.0.4）。"""
    out = []
    for m in _REGISTRY:
        m.render(out)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- 自测

def _selftest():
    # Counter 增 + 标签
    ai_request_total.inc({"backend": "pollinations", "result": "ok"}, 3)
    ai_request_total.inc({"backend": "pollinations", "result": "ok"})
    ai_failure_total.inc({"backend": "ovh", "kind": "rate_limit"})
    ai_fallback_total.inc()
    ai_latency.observe(0.3, {"backend": "pollinations"})
    ai_latency.observe(7.0, {"backend": "pollinations"})
    ai_latency.observe(0.05, {"backend": "ovh"})
    ai_healthy_backends.set(2)
    ai_cooldown_active.set(1)

    txt = render()
    assert "fleet_ai_requests_total{backend=\"pollinations\",result=\"ok\"} 4" in txt, txt
    assert "fleet_ai_failures_total{backend=\"ovh\",kind=\"rate_limit\"} 1" in txt, txt
    assert "fleet_ai_fallback_switches_total 1" in txt, txt
    # 直方图：pollinations 有 0.3 与 7.0 两个样本，ovh 有 0.05
    assert "fleet_ai_latency_seconds_bucket{backend=\"pollinations\",le=\"5\"} 1" in txt, txt
    assert "fleet_ai_latency_seconds_bucket{backend=\"pollinations\",le=\"10\"} 2" in txt, txt
    assert "fleet_ai_latency_seconds_sum{backend=\"pollinations\"} " in txt, txt
    assert "fleet_ai_latency_seconds_count{backend=\"pollinations\"} 2" in txt, txt
    assert "fleet_ai_healthy_backends 2" in txt, txt
    assert "fleet_ai_cooldown_active 1" in txt, txt
    # 标签值转义
    ai_request_total.inc({"backend": 'a"b', "result": "ok"})
    assert 'backend=\"a\\"b\"' in render(), render()
    print("fleet.metrics 自测通过（7 类指标 + 直方图 + 转义）")


if __name__ == "__main__":
    _selftest()

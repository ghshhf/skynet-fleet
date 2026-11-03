# -*- coding: utf-8 -*-
"""天网 · 网络诊断知识库（故障模式沉淀与持久化）

对应计划书 V1.1 第六节（知识库 / 故障诊断器）+ 任务 D「从故障转为能力」。
与 netprobe.py 分工：
    netprobe 负责「采集 + 状态机 + 单次诊断」（无状态）；
    本模块负责「知识库的加载 / 落盘 / 累积」，让诊断结果可跨进程沉淀为
    已验证错误模式（现象 → 证据 → 原因 → 修复 → 适用条件 → 回归）。

不依赖任何外部库；KB 落 `fleet/_netdiag_kb.json`，incident 流水落
`fleet/_netdiag_incidents.jsonl`（追加写，天然支持并发追加）。
"""
import os
import json
import time

__all__ = [
    "ensure_loaded", "lookup", "record_incident", "seed_categories",
    "KB_PATH", "INCIDENT_PATH",
]

# 与 netprobe 的错误分类对齐。无 httpx 时 netprobe.classify_error 退化为文本嗅探，
# 但落到 KB 的 category 仍走这套枚举（unknown 兜底）。
SEED_KB = {
    "dns": ("DNS 解析失败：域名无法解析为主机地址", "high",
            ["核对域名拼写", "本机查 DNS：nslookup <域名> / ipconfig /flushdns",
             "若目标在代理后，确认是否需走特定通道解析"]),
    "connection": ("连接被拒 / 网络不可达：TCP 三次握手未建立", "high",
                   ["确认目标主机在线、端口开放", "检查防火墙 / 代理 / 本机出口",
                    "若为内网地址确认是否在同一网络"]),
    "timeout_connect": ("连接超时：握手在预算内未完成", "med",
                        ["目标响应慢或链路拥塞", "增大 timeout 或走代理重试",
                         "确认不是被限流 / 反爬拦截"]),
    "timeout_read": ("读取超时：连接已建但响应迟迟不来", "med",
                    ["服务端处理慢 / 挂起", "检查是否触发反爬返回空响应",
                     "降低并发或改用分块读取"]),
    "tls": ("TLS / 证书错误：加密握手失败", "high",
            ["确认系统根证书完整（本机曾删过 Windows Update 组件，证书库可能不全）",
             "目标是否自签 / 过期", "代理是否做了中间人拦截"]),
    "http_status": ("HTTP 状态码错误（4xx/5xx）", "med",
                    ["4xx = 客户端问题（鉴权 / 参数 / 频率 / 权限）",
                     "5xx = 服务端问题", "务必读响应体定位具体原因，别只看状态码"]),
    "protocol": ("协议错误：响应不符合 HTTP 规范", "med",
                 ["目标可能不是 HTTP 服务", "代理返回了非预期内容",
                  "检查端口 / 协议是否匹配"]),
    "proxy": ("代理错误：出口代理拒绝或失败", "high",
              ["确认代理地址 / 凭据", "换代理或直连对比", "查代理侧日志"]),
    "http_error": ("HTTP 层错误（非上述具体类别）", "med",
                   ["保留原始异常文本", "用更底层抓包拿证据"]),
    "unknown": ("未能从异常归类失败原因（证据不足）", "low",
                ["保留原始异常文本", "用更底层抓包拿证据", "不要臆造原因"]),
    # 计划书核心：结果未知 ≠ 失败。请求已发出但响应不确定，盲目重试有副作用。
    "unknown_result": ("结果未知：请求已发出但响应未确认，无法确定服务端是否处理", "med",
                       ["不要盲目重试（有副作用的操作先查状态）",
                        "用幂等查询接口确认服务端状态",
                        "若是无副作用读操作，可安全重试一次"]),
}

_HERE = os.path.dirname(os.path.abspath(__file__))
KB_PATH = os.path.join(_HERE, "_netdiag_kb.json")
INCIDENT_PATH = os.path.join(_HERE, "_netdiag_incidents.jsonl")

_cache = {"kb": None, "mtime": 0.0}


def ensure_loaded(force=False):
    """加载 KB 到内存缓存；首次或文件不存在时以 SEED_KB 播种并落盘。

    返回 dict：{category: (reason, confidence, [steps])}。
    """
    global _cache
    try:
        st = os.stat(KB_PATH)
        if not force and _cache["kb"] is not None and st.st_mtime <= _cache["mtime"]:
            return _cache["kb"]
    except OSError:
        pass
    kb = {}
    if os.path.exists(KB_PATH):
        try:
            with open(KB_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for k, v in raw.get("patterns", {}).items():
                kb[k] = (v.get("reason", ""), v.get("confidence", "low"),
                         list(v.get("next_steps", [])))
        except Exception:
            kb = {}
    if not kb:  # 首次 / 损坏 → 播种
        kb = {k: (r, c, list(s)) for k, (r, c, s) in SEED_KB.items()}
        _save_kb(kb)
    _cache["kb"] = kb
    try:
        _cache["mtime"] = os.stat(KB_PATH).st_mtime
    except OSError:
        _cache["mtime"] = 0.0
    return kb


def _save_kb(kb):
    patterns = {k: {"reason": r, "confidence": c, "next_steps": list(s)}
                for k, (r, c, s) in kb.items()}
    tmp = KB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "patterns": patterns}, f,
                  ensure_ascii=False, indent=2)
    os.replace(tmp, KB_PATH)  # 原子替换，避免半截文件


def lookup(category):
    """查 category 的诊断模板。命中返回 (reason, confidence, steps)，
    未命中（新类别）落到 unknown 模板，绝不臆造原因。"""
    kb = ensure_loaded()
    if category in kb:
        return kb[category]
    return kb.get("unknown", SEED_KB["unknown"])


def register_pattern(category, reason, confidence, steps):
    """把新发现的错误模式登记进 KB 并落盘（任务 D 沉淀）。"""
    if not category:
        return False
    kb = ensure_loaded()
    kb[category] = (reason, confidence, list(steps))
    _save_kb(kb)
    _cache["mtime"] = 0.0  # 强制下次 reload
    return True


def seed_categories():
    """返回内置种子类别清单（供能力注册 / 文档使用）。"""
    return list(SEED_KB.keys())


def record_incident(trace_dict, diagnosis, resolution=None, task_id=None):
    """把一次故障沉淀为 incident 流水（追加写）。

    trace_dict 来自 RequestTrace.to_dict()；diagnosis 来自 netprobe.diagnose()。
    返回写入的行 dict。resolution 由上层在「已验证修复」后回填（任务 D 的闭环）。
    """
    row = {
        "ts": time.time(),
        "task_id": task_id,
        "url": trace_dict.get("url"),
        "final_url": trace_dict.get("final_url"),
        "status": trace_dict.get("status"),
        "state": trace_dict.get("state"),
        "category": diagnosis.get("category"),
        "reason": diagnosis.get("reason"),
        "confidence": diagnosis.get("confidence"),
        "resolution": resolution,
    }
    line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    with open(INCIDENT_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return row


def load_incidents(limit=None):
    """读回 incident 流水（最新在前）。文件不存在返回 []。"""
    if not os.path.exists(INCIDENT_PATH):
        return []
    out = []
    with open(INCIDENT_PATH, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    out.reverse()
    return out[:limit] if limit else out

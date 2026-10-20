"""免费外部算力中继 · 最小实现（分支 rfc/free-compute-relay）。

把免 key 的 LLM 端点当作编队兜底算力源：本地无 AI 节点或任务轻时，
经统一接口外发（含隐私过滤 + 缓存 + 多源故障切换）。

依赖：仅标准库（urllib）。真实端点/鉴权以项目既有侦察结果为准，此处为骨架。
"""
import hashlib
import json
import time
import urllib.parse
import urllib.request

# 免 key 端点登记表（示意；实际 URL/参数按各自文档补）
ENDPOINTS = [
    {"name": "pollinations", "url": "https://text.pollinations.ai/", "health": 1.0},
    {"name": "llm7", "url": "https://api.llm7.io/v1/chat/completions", "health": 1.0},
    {"name": "ovh", "url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions", "health": 1.0},
]

_CACHE = {}


def _privacy_ok(text: str) -> bool:
    """隐私闸：含本机路径/账号/密钥特征则拒绝外发。"""
    import re
    bad = [
        r"[A-Za-z]:\\\\",                 # Windows 绝对路径
        r"/home/", r"/Users/",            # Unix 家目录
        r"(?i)(api[_-]?key|token|password|secret)\\s*[:=]",
        r"\\b\\d{17}[\\dXx]\\b",            # 身份证
        r"\\b1[3-9]\\d{9}\\b",              # 手机号
    ]
    return not any(re.search(p, text) for p in bad)


def probe():
    """按健康度返回可用端点名（此处不真探测，按登记顺序）。"""
    return [e["name"] for e in sorted(ENDPOINTS, key=lambda x: -x["health"])]


def _cache_key(prompt, model):
    return hashlib.sha256((model or "").encode() + b"::" + prompt.encode()).hexdigest()


def chat(prompt, model=None, timeout=40):
    """外发请求，成功返回文本。失败逐源切换。含缓存与隐私闸。"""
    if not _privacy_ok(prompt):
        return None, "隐私闸拒绝：含敏感信息，禁止外发（请走内网 AI 节点）"
    ck = _cache_key(prompt, model)
    if ck in _CACHE:
        return _CACHE[ck], "cache"

    for ep in sorted(ENDPOINTS, key=lambda x: -x["health"]):
        try:
            if ep["name"] == "pollinations":
                url = ep["url"] + urllib.parse.quote(prompt)
                req = urllib.request.Request(url, headers={"User-Agent": "skynet-fleet"})
                body = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
                _CACHE[ck] = body
                return body, ep["name"]
            else:
                payload = json.dumps({
                    "model": model or "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                }).encode()
                req = urllib.request.Request(ep["url"], data=payload,
                                             headers={"Content-Type": "application/json",
                                                      "User-Agent": "skynet-fleet"})
                r = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
                txt = r["choices"][0]["message"]["content"]
                _CACHE[ck] = txt
                return txt, ep["name"]
        except Exception as e:
            ep["health"] *= 0.5
            _last_err = str(e)
            continue
    return None, "所有免费端点均失败"


if __name__ == "__main__":
    print(chat("用一句话说明什么是递归")[0])

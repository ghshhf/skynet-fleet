"""天网 · 模型目录（PC 侧 awareness）。

镜像 Android 端 ModelManager 的 SPECS：模型文件名 → 元数据。
用途：node_agent --models 上报手机已装 + 可下；commander 能查「某节点能下什么 / 已装什么」。

规则（与 Android 端一致，2026-10-23）：
  - kind ∈ {sd, llm, vl, custom}
  - sd 模型带 sampler/cfg/steps/native_size；llm/vl 这些字段为 None（由 llama.cpp 引擎管，
    本轮回只管下载/摆放/登记，真跑 LLM 对话见下一轮）
  - size_bytes 已知的写实填，未知填 0（下载时以 HF Content-Length 为准）
  - url 一律 Hugging Face resolve 直链（手机经代理直连，不堆镜像）
"""
from typing import Dict, Any, Optional

SD = "sd"
LLM = "llm"
VL = "vl"
CUSTOM = "custom"

CATALOG: Dict[str, Dict[str, Any]] = {
    "sd15-q8_0.gguf": {
        "label": "SD1.5 q8_0（通用，20 步起）", "kind": SD,
        "url": "https://huggingface.co/second-state/stable-diffusion-v1-5-GGUF/"
               "resolve/main/stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf",
        "size_bytes": 1_763_578_176, "sampler": "euler_a", "cfg": 7.0,
        "native_size": 512, "steps": (12, 20, 28),
    },
    "sd21-turbo-q8_0.gguf": {
        "label": "SD2.1 Turbo q8_0（少步，4~8 步）", "kind": SD,
        "url": "https://huggingface.co/gpustack/stable-diffusion-v2-1-turbo-GGUF/"
               "resolve/main/stable-diffusion-v2-1-turbo-Q8_0.gguf",
        "size_bytes": 2_322_705_024, "sampler": "lcm", "cfg": 1.0,
        "native_size": 512, "steps": (4, 6, 8),
    },
    "sdxl-turbo-q8_0.gguf": {
        "label": "SDXL-Turbo q8_0（1024 原生，1~4 步）", "kind": SD,
        "url": "https://huggingface.co/city96/sdxl-turbo-gguf/"
               "resolve/main/sdxl-turbo-q8_0.gguf",
        "size_bytes": 0, "sampler": "lcm", "cfg": 1.0,
        "native_size": 1024, "steps": (1, 2, 4),
    },
    "qwen2.5-4b-instruct-q4_k_m.gguf": {
        "label": "Qwen2.5-4B-Instruct（4B LLM）", "kind": LLM,
        "url": "https://huggingface.co/Qwen/Qwen2.5-4B-Instruct-GGUF/"
               "resolve/main/qwen2.5-4b-instruct-q4_k_m.gguf",
        "size_bytes": 0, "sampler": None, "cfg": None,
        "native_size": None, "steps": None,
    },
    "gemma-2-4b-it-q4_k_m.gguf": {
        "label": "Gemma-2-4B-it（4B LLM）", "kind": LLM,
        "url": "https://huggingface.co/unsloth/gemma-2-4b-it-GGUF/"
               "resolve/main/gemma-2-4b-it-Q4_K_M.gguf",
        "size_bytes": 0, "sampler": None, "cfg": None,
        "native_size": None, "steps": None,
    },
    "beijixing-v7-q8_0.gguf": {
        # 北极星-V7：中文 LLM，主人手机上已装（639MB Q8_0）。URL 仅作占位，
        # 真要重下用截图里那个列表；size_bytes 写实填。
        "label": "北极星-V7 Q8_0（中文 LLM，已在你手机上）", "kind": LLM,
        "url": "https://huggingface.co/coqui/artifacts/"
               "resolve/main/beijixing-v7-q8_0.gguf",
        "size_bytes": 639_446_720, "sampler": None, "cfg": None,
        "native_size": None, "steps": None,
    },
    "qwen3-vl-4b-instruct-q4_k_m.gguf": {
        "label": "Qwen3-VL-4B-Instruct（4B 多模态 VL）", "kind": VL,
        "url": "https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/"
               "resolve/main/qwen3-vl-4b-instruct-q4_k_m.gguf",
        "size_bytes": 2_497_281_664, "sampler": None, "cfg": None,
        "native_size": None, "steps": None,
    },
    "mmproj-qwen3vl-4b-instruct-q8_0.gguf": {
        "label": "mmproj-Qwen3VL-4B（视觉投影）", "kind": VL,
        "url": "https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/"
               "resolve/main/mmproj-qwen3vl-4b-instruct-q8_0.gguf",
        "size_bytes": 453_974_304, "sampler": None, "cfg": None,
        "native_size": None, "steps": None,
    },
}

VALID_KINDS = {SD, LLM, VL, CUSTOM}


def get(name: str) -> Optional[Dict[str, Any]]:
    return CATALOG.get(name)


def all() -> Dict[str, Dict[str, Any]]:
    return CATALOG


def by_kind(kind: str) -> Dict[str, Dict[str, Any]]:
    return {n: m for n, m in CATALOG.items() if m["kind"] == kind}


def is_valid() -> bool:
    """结构自检：kind 合法、URL 是 HF resolve 直链、SD 档带 sampler/cfg。"""
    for n, m in CATALOG.items():
        if m["kind"] not in VALID_KINDS:
            return False
        if not str(m["url"]).startswith("https://huggingface.co/"):
            return False
        if m["kind"] == SD:
            if not m.get("sampler") or m.get("cfg") is None:
                return False
            if not (isinstance(m.get("steps"), tuple) and len(m["steps"]) == 3):
                return False
    return True


if __name__ == "__main__":
    import json
    print(json.dumps({"count": len(CATALOG), "valid": is_valid(),
                      "by_kind": {k: len(by_kind(k)) for k in (SD, LLM, VL)}},
                     ensure_ascii=False, indent=2))

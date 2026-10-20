"""共享算力：节点侧本地推理接口（她三段式 ③ 重活/大模型 = 电脑/盒子那双手）。

真实推理走 llm.py 的多后端适配层（stdlib-only，改环境变量即切换，业务代码零改动）：
  - FLEET_LLM_CMD  自定义命令行（ollama run / llama-cli ...）
  - FLEET_LLM_URL  本地 OpenAI 兼容端点（llama.cpp server / MLC / vLLM / Ollama）
  - 都没有         占位实现（backend=placeholder），链路仍可端到端跑通，不静默失败

因此「弱设备不装模型」这件事是成立的：算力集中在 Box/手机/电脑那几个节点，
其余设备只经 Commander 路由调用，不重复下载权重。

用法（节点侧被 Commander 调用）：
  from compute_runner import run, backend, selftest
  out = run(model="polaris-0.4b", prompt="帮我写段去重的代码")
  python compute_runner.py --selftest
"""
import llm as llm_mod


def backend():
    """当前生效的推理后端（交给 llm.py 判断，改环境变量即切换）。"""
    return llm_mod.backend()


def run(model, prompt, model_dir="models"):
    """在本节点上跑一次推理，返回文本。

    model_dir 保留参数：真实部署时由 llama.cpp 加载 model_dir 下的权重，
    具体路径经 FLEET_LLM_CMD / FLEET_LLM_URL 配置，不在这里硬编码。
    """
    text, _backend = llm_mod.complete(prompt, model=model)
    return text


def selftest():
    """离线自检：占位后端也能端到端返回，不吞 prompt、不静默空串。"""
    b = backend()
    out = run(None, "用一句话说明你是什么")
    assert isinstance(out, str) and out.strip(), out
    assert "占位" in out or b != "placeholder", (b, out)
    print(f"[selftest] 后端={b}（无环境变量时为 placeholder，设 FLEET_LLM_CMD/URL 即接真模型）")
    print(f"[selftest] 推理返回: {out[:60]}…")
    print("[selftest] PASS")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        selftest()
    else:
        print(f"backend={backend()}")
        print(run(None, " ".join(a for a in sys.argv[1:]) or "你好"))

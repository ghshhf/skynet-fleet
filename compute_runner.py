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
    """离线自检：任一后端都能端到端返回，不吞 prompt、不静默空串。"""
    b = backend()
    out, actual = llm_mod.complete("用一句话说明你是什么")
    assert isinstance(out, str) and out.strip(), out
    # 🔴 2026-10-22：口径对齐——必须用 **complete 返回的实际后端**，
    #    不是 backend() 的名义值。llm.py 引入 mock 层后二者会分裂：
    #    名义 placeholder（没配本地端点）+ 实际 mock（FLEET_USE_MOCK 默认开），
    #    老断言拿名义 placeholder 对 mock 输出 ⇒ 必红。
    #    现在：实际不是 placeholder（真接了本地/mock），或确实走了占位
    #    （文案里必须带「占位」二字，不许静默）。
    assert actual != "placeholder" or "占位" in out, (b, actual, out)
    print(f"[selftest] 名义后端={b} 实际后端={actual}"
          f"（无环境变量时走 mock 模拟层，设 FLEET_LLM_CMD/URL 即接真模型）")
    print(f"[selftest] 推理返回: {out[:60]}")
    print("[selftest] PASS")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        selftest()
    else:
        print(f"backend={backend()}")
        print(run(None, " ".join(a for a in sys.argv[1:]) or "你好"))

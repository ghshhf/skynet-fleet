"""共享算力：节点侧本地推理接口（她三段式 ③ 重活/大模型 = 电脑/盒子那双手）。

真实模型（北极星 0.4B / Qwen3-4B 等，跑手机或盒子或电脑）未来在此接入 llama.cpp/MLC；
此处提供统一接口 + 占位实现，证明「Commander 路由计算任务 -> 合适节点执行」链路已打通。

用法（节点侧被 Commander 调用）：
  from compute_runner import run
  out = run(model="polaris-0.4b", prompt="帮我写段去重的代码")
"""
def run(model, prompt, model_dir="models"):
    """占位推理。真实部署：在此加载 model_dir 下的权重并生成。"""
    return (f"[compute_runner] model={model or '默认'} prompt={prompt!r} "
            f"→ 真实推理请在 model_dir={model_dir} 放置权重并由 llama.cpp/MLC 接入；"
            f"路由链路已就绪")

"""共享算力：节点侧本地推理接口（她三段式 ③ 重活/大模型 = 电脑/盒子那双手）。

真实推理统一走 llm.py（多后端：FLEET_LLM_CMD / FLEET_LLM_URL / 占位）。
「她」到底跑在哪台设备不重要：哪台接了后端，Commander 就把重活路由到哪台。

用法（节点侧被 Commander 调用）：
  from compute_runner import run
  out = run(model="polaris-0.4b", prompt="帮我写段去重的代码")
"""
from llm import complete


def run(model, prompt, model_dir="models", system=None):
    """统一接口：委托 llm.py。后端由环境变量决定，此处不改业务代码。"""
    text, _backend = complete(prompt, model=model, system=system)
    return text

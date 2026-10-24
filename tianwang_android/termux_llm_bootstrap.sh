#!/data/data/com.termux/files/usr/bin/bash
# 天网手机端本地模型引擎引导脚本（MCB）
#
# 用法（在 Termux 里）：
#   bash termux_llm_bootstrap.sh        # 只装引擎，模型自己放（推荐先跑这个）
#   bash termux_llm_bootstrap.sh model  # 装引擎 + 顺手下载 Qwen3-1.7B-Q8（≈1.75GB）
#
# 装完引擎后，起服务（App 本地模式默认就打这个地址/端口）：
#   llama-server -m ~/models/Qwen3-1.7B-Q8_0.gguf \
#       --host 127.0.0.1 --port 8080 --alias local \
#       --ctx-size 4096 -ngl 0
# 然后回天网 App → 设置 → 打开「本地模型模式」开关即可离线聊。
#
# 为什么用 1.7B 而不是手机里那个 4B：
#   4B 纯 CPU 约 6~7 tok/s，1.7B-Q8 约 30 tok/s 且决策/读资料准确率实测 6/6、零编造。
#   4B 的 Qwen3VL 是视觉模型，当纯文本对话引擎并不划算。

set -e
echo "==> [1/3] 更新包索引并安装 llama-cpp（Termux 自带 llama-server 二进制）"
pkg update -y
pkg install -y llama-cpp

echo "==> [2/3] 确认 llama-server 可用"
if command -v llama-server >/dev/null 2>&1; then
    echo "    llama-server 已就位：$(command -v llama-server)"
else
    echo "    !! 没找到 llama-server，包名可能变了，手动试：pkg install llama-cpp"
    exit 1
fi

if [ "$1" = "model" ]; then
    echo "==> [3/3] 下载 Qwen3-1.7B-Q8_0（≈1.75GB，走 HuggingFace 镜像）"
    mkdir -p ~/models
    cd ~/models
    # 优先用 HF 镜像，墙内更稳；没有就退回官方
    MIRROR="${HF_ENDPOINT:-https://hf-mirror.com}"
    URL="$MIRROR/Qwen/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q8_0.gguf"
    echo "    从 $URL 拉取…"
    curl -L -o Qwen3-1.7B-Q8_0.gguf "$URL" || \
        curl -L -o Qwen3-1.7B-Q8_0.gguf \
            https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q8_0.gguf
    echo "    下载完成：$(ls -lh Qwen3-1.7B-Q8_0.gguf | awk '{print $5}')"
else
    echo "==> [3/3] 跳过模型下载（你要自己放模型）。把 .gguf 放到 ~/models/ 即可。"
    mkdir -p ~/models
fi

echo
echo "==> 完成。起服务："
echo "    llama-server -m ~/models/Qwen3-1.7B-Q8_0.gguf --host 127.0.0.1 --port 8080 --alias local --ctx-size 4096 -ngl 0"
echo "    然后天网 App → 设置 → 打开「本地模型模式」。"

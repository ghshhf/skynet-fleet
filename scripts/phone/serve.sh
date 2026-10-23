#!/data/data/com.termux/files/usr/bin/sh
# 手机算力节点：把 llama-server 起成常驻服务（模型只加载一次 ≈1 秒）
#
# 为什么是 server 不是 cli：
#   cli 每次冷启动 7 秒、输出混着 banner 和 prompt 回显、还要防 pty 交互；
#   server 一次加载、之后每次请求毫秒级（实测 TTFT 129ms），且是 OpenAI 兼容 JSON。
#   同一份权重可以同时服务「不同人格」——人格在记忆包里，换包不用重启。
#
# 用法：sh serve.sh [模型相对路径] [端口] [vision]
#   vision = 挂视觉投影（能看图，但实测 prefill 慢 4 倍：4B 长 prompt 90.6s → 23.0s）
#   默认不挂 —— 纯文本对话不该为「能不能看图」付这个代价
MODEL="${1:-models/北极星-V7-Q8_0.gguf}"
PORT="${2:-8080}"
VISION="${3:-}"
MMPROJ=""
if [ "$VISION" = "vision" ]; then
  MMPROJ="--mmproj $HOME/models/mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf"
fi
mkdir -p "$HOME/skynet"
if [ ! -f "$HOME/$MODEL" ] && [ ! -f "$MODEL" ]; then
  echo "MODEL_MISSING $MODEL"; exit 4
fi
M="$MODEL"; [ -f "$HOME/$MODEL" ] && M="$HOME/$MODEL"

pkill -f "llama-server -m" 2>/dev/null
sleep 1
nohup llama-server -m "$M" $MMPROJ --host 0.0.0.0 --port "$PORT" \
  -t 6 -c 4096 --no-warmup > "$HOME/skynet/llama-server.log" 2>&1 &
echo "pid=$!"
for i in 1 2 3 4 5 6 7 8; do
  sleep 1
  H=$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health")
  [ "$H" = "200" ] && { echo "ready in ${i}s model=$M port=$PORT"; exit 0; }
done
echo "NOT_READY"; tail -5 "$HOME/skynet/llama-server.log"; exit 5

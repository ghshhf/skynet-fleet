#!/data/data/com.termux/files/usr/bin/sh
# 手机算力节点：把 llama-server 起成常驻服务（模型只加载一次 ≈1 秒）
#
# 为什么是 server 不是 cli：
#   cli 每次冷启动 7 秒、输出混着 banner 和 prompt 回显、还要防 pty 交互；
#   server 一次加载、之后每次请求毫秒级（实测 TTFT 129ms），且是 OpenAI 兼容 JSON。
#   同一份权重可以同时服务「不同人格」——人格在记忆包里，换包不用重启。
#
# 调参依据（红米 25060RK16C / 天玑 9400 / 8 核：1×X925 + 3×X4 + 4×A720）
# llama-bench pp512 / tg128 实测，北极星-V7 Q8_0（qwen3 0.6B）：
#     t=2  fa=0  48.9 / 33.4      t=2  fa=1  35.9 / 25.1
#     t=4  fa=0  78.2 / 44.8      t=4  fa=1  89.6 / 51.1   ← 生成最快
#     t=6  fa=0  90.9 / 38.9      t=6  fa=1 108.7 / 44.1   ← 预填充最快
#     t=8  fa=0 101.7 / 35.0      t=8  fa=1 103.8 / 35.4
#   结论一：**生成只要 4 线程**。6/8 线程把小核拉进来抢内存带宽，逐 token 反而更慢
#           （51.1 → 44.1 → 35.4）。逐 token 是带宽活，不是算力活。
#   结论二：**FlashAttention 必须开**。同线程数下 tg 普遍 +13%~+15%，还省 KV 内存；
#           只有 t=2 这种极端配置才因实现开销倒挂（25.1 < 33.4，别用 2 线程）。
#   结论三：预填充与生成的最优线程数不同 → 用 -tb 分开：
#           -t 4（逐 token）+ -tb 6（批量/prefill），两头都吃满。
#   所以默认 -t 4 -tb 6 -fa on。不是拍脑袋，是上表挑出来的。
#
# 为什么这里没有 GPU / -ngl：#   试过了，是负收益。自己编了 Vulkan 后端跑通 Mali-G925，实测（llama-bench，同 build）：
#     Qwen3-0.6B Q8_0   纯CPU pp190.5 / tg48.0   纯GPU pp66.6 / tg42.8
#     Qwen3VL-4B  Q4_K  纯CPU pp 31.8 / tg12.1   纯GPU pp 8.2 / tg 8.3
#     混合 -ngl 8/14/20 → tg 只有 5.9 / 7.6 / 10.8（跨 CPU↔GPU 边界每层同步）
#   GPU 在 fa on/off、ubatch 64/512 各种组合下都卡在同一平台，是 shader 吞吐上限。
#   要做 GPU 得换 ARM 的 KleidiAI/Mali 分支，upstream 不行。详见
#   docs/手机GPU加速_实测结论_2026-10-23.md
#   ⚠ 顺带记一个坑：llama-bench 里 -ngl 0 不等于纯 CPU，必须再加 -dev none，
#     否则 Vulkan 仍在调度里，tg 从 48.0 掉到 3.8。
#
# 为什么必须 --jinja（实测 2026-10-23）：
#   不加它，llama-server 不走 GGUF 内嵌的 chat template，而是退回**启发式解析器**，
#   那个解析器不认识模型自己的工具调用分隔符，于是 `tools` 字段被整个忽略、
#   工具调用被当成普通文本吐出来。表现就是「模型明明会调工具，但 tool_calls 永远为 null」。
#   加了之后 4B（Qwen3VL）稳定产出合法 tool_calls；0.6B（北极星-V7）仍然不出 ——
#   它是微调/蒸馏版，本身没保留 function calling 能力，这不是配置能救的。
#   结论：--jinja 是标配，对支持的模型必需、对不支持的模型无害。
#
# 用法：sh serve.sh [模型相对路径] [端口] [vision]
#   vision = 挂视觉投影（能看图，但实测 prefill 慢 4 倍：4B 长 prompt 90.6s → 23.0s）
#   默认不挂 —— 纯文本对话不该为「能不能看图」付这个代价
#
# 环境变量（不改脚本就能调）：
#   THREADS=4    逐 token 生成线程数（大核数，超 4 必亏）
#   TB=6         批量/prefill 线程数（可以多，预填充是算力活）
#   FA=on        FlashAttention：on / off / auto
#   CTX=4096     上下文长度
#   KVQ=0        设 1 则 KV cache 用 q8_0（省一半 KV 内存，长上下文更稳）
#   EXTRA=""     追加任意 llama-server 参数（原样透传）
MODEL="${1:-models/北极星-V7-Q8_0.gguf}"
PORT="${2:-8080}"
VISION="${3:-}"

THREADS="${THREADS:-4}"
TB="${TB:-6}"
FA="${FA:-on}"
CTX="${CTX:-4096}"
KVQ="${KVQ:-0}"
EXTRA="${EXTRA:-}"

MMPROJ=""
if [ "$VISION" = "vision" ]; then
  MMPROJ="--mmproj $HOME/models/mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf"
fi
KVARGS=""
if [ "$KVQ" = "1" ]; then
  KVARGS="-ctk q8_0 -ctv q8_0"
fi

mkdir -p "$HOME/skynet"
if [ ! -f "$HOME/$MODEL" ] && [ ! -f "$MODEL" ]; then
  echo "MODEL_MISSING $MODEL"; exit 4
fi
M="$MODEL"; [ -f "$HOME/$MODEL" ] && M="$HOME/$MODEL"

pkill -f "llama-server -m" 2>/dev/null
sleep 1
# 记一行「这次到底用什么参数起的」——调参最怕不知道当下生效的是哪套
echo "cfg model=$(basename "$M") port=$PORT t=$THREADS tb=$TB fa=$FA ctx=$CTX kvq=$KVQ extra=[$EXTRA]" \
  > "$HOME/skynet/llama-server.cfg"
nohup llama-server -m "$M" $MMPROJ --host 0.0.0.0 --port "$PORT" \
  -t "$THREADS" -tb "$TB" -fa "$FA" -c "$CTX" $KVARGS $EXTRA \
  --jinja --no-warmup > "$HOME/skynet/llama-server.log" 2>&1 &
echo "pid=$!"
for i in 1 2 3 4 5 6 7 8; do
  sleep 1
  H=$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health")
  [ "$H" = "200" ] && { echo "ready in ${i}s model=$M port=$PORT"; exit 0; }
done
echo "NOT_READY"; tail -5 "$HOME/skynet/llama-server.log"; exit 5

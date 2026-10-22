#!/usr/bin/env bash
# fleet-img.sh — 天网手机出图 worker（Termux 侧，运行于被控手机）
#
# 契约见 docs/手机出图功能_动手范围_2026-10-22.md §3.2
# 设计纪律：
#   - fail-closed：任一闸门不通过 → 一行 {"ok":false,"err":"原因"} + 非零 exit
#   - stdout 只许**一行 JSON**（Termux RUN_COMMAND 回传上限 100KB，sd 日志全走日志文件）
#   - 测试注入点（环境变量覆盖，门禁用假 sysfs 测闸门）：
#       FLEET_IMG_SD_BIN / FLEET_IMG_MODEL / FLEET_IMG_BATTERY_DIR /
#       FLEET_IMG_THERMAL_GLOB / FLEET_IMG_MAX_TEMP /
#       FLEET_IMG_OUT_DIR / FLEET_IMG_WORKDIR / FLEET_IMG_LOG_DIR / FLEET_IMG_BACKEND
set -u

# ---- 路径（可覆盖）----
SD_BIN="${FLEET_IMG_SD_BIN:-$HOME/sd.cpp/bin/sd}"
MODEL="${FLEET_IMG_MODEL:-$HOME/sd.cpp/models/sd15-q8_0.gguf}"
BATTERY_DIR="${FLEET_IMG_BATTERY_DIR:-/sys/class/power_supply}"
THERMAL_GLOB="${FLEET_IMG_THERMAL_GLOB:-/sys/class/thermal/thermal_zone*/temp}"
MAX_TEMP="${FLEET_IMG_MAX_TEMP:-42}"
WORKDIR="${FLEET_IMG_WORKDIR:-$HOME/fleet}"
OUT_DIR="${FLEET_IMG_OUT_DIR:-$HOME/storage/shared/Pictures/fleet}"
LOG_DIR="${FLEET_IMG_LOG_DIR:-$WORKDIR/logs}"
LOCK="$WORKDIR/.fleet-img.lock"
# 后端：默认强制 Vulkan（Adreno/Immortalis）。若编译未带 Vulkan，setup 阶段就会失败，不会到这。
BACKEND="${FLEET_IMG_BACKEND:-diffusion=vulkan,vae=vulkan,te=vulkan}"

# ---- 唯一 stdout 出口 ----
emit() { printf '%s\n' "$1"; }

# ---- 失败出口（一行 JSON + exit 2）----
fail() {
  emit "{\"ok\":false,\"err\":\"$1\"}"
  exit 2
}

# ---- 参数解析 ----
PROMPT=""; STEPS=10; SEED=""; SIZE=512
while [ $# -gt 0 ]; do
  case "$1" in
    --prompt) PROMPT="${2:-}"; shift 2;;
    --steps)  STEPS="${2:-}"; shift 2;;
    --seed)   SEED="${2:-}"; shift 2;;
    --size)   SIZE="${2:-}"; shift 2;;
    *) fail "未知参数: $1";;
  esac
done

# ---- 闸门 1：二进制 / 模型 存在 ----
[ -x "$SD_BIN" ] || fail "sd 二进制不存在或不可执行：$SD_BIN（先跑 setup-termux-img.sh）"
[ -f "$MODEL" ]  || fail "模型不存在：$MODEL（先跑 setup-termux-img.sh）"

# ---- 闸门 2：prompt 长度 1–500 ----
len=${#PROMPT}
if [ "$len" -lt 1 ] || [ "$len" -gt 500 ]; then
  fail "prompt 长度需在 1–500 字符（当前 $len）"
fi

# ---- 闸门 3：steps 整数 1–30 ----
case "$STEPS" in
  ''|*[!0-9]*) fail "steps 须为 1–30 的整数（当前「$STEPS」）";;
esac
[ "$STEPS" -ge 1 ] && [ "$STEPS" -le 30 ] || fail "steps 须 1–30（当前 $STEPS）"

# ---- 闸门 4：size 白名单 512|768|1024 ----
case "$SIZE" in
  512|768|1024) ;;
  *) fail "size 须 512|768|1024（当前 $SIZE）";;
esac

# ---- 闸门 5：seed 空或整数（允许负号）----
if [ -n "$SEED" ]; then
  case "$SEED" in
    ''|*[!0-9-]*) fail "seed 须为整数或留空（当前「$SEED」）";;
  esac
fi

# ---- 闸门 6：温度上限 ----
max_temp() {
  local m=0 t f
  for f in $THERMAL_GLOB; do
    [ -r "$f" ] || continue
    t="$(cat "$f" 2>/dev/null)" || continue
    # 内核给的是毫摄氏度；0 表示不可读，跳过
    [ "$t" -gt 0 ] 2>/dev/null || continue
    t=$(( t / 1000 ))
    [ "$t" -gt "$m" ] && m=$t
  done
  echo "$m"
}
TEMP="$(max_temp)"
if [ "$TEMP" -gt "$MAX_TEMP" ] 2>/dev/null; then
  fail "机身温度 ${TEMP}°C > 上限 ${MAX_TEMP}°C"
fi

# ---- 闸门 7：并发锁（第二张未完 → 拒）----
# 用 mkdir 原子锁（POSIX 跨平台，不依赖 flock：Termux / CI / Git Bash 都可用）。
# 陈旧锁（>30 分钟）视为上次崩溃残留，自动清掉再抢一次。
mkdir -p "$WORKDIR" || fail "无法创建工作目录 $WORKDIR"
acquire_lock() {
  if mkdir "$LOCK" 2>/dev/null; then
    return 0
  fi
  if [ -d "$LOCK" ]; then
    local age=0 now
    now="$(date +%s)"
    local mtime
    mtime="$(stat -c %Y "$LOCK" 2>/dev/null || echo 0)"
    age=$(( now - (mtime + 0) ))
    if [ "$age" -gt 1800 ]; then
      rmdir "$LOCK" 2>/dev/null && mkdir "$LOCK" 2>/dev/null && return 0
    fi
  fi
  return 1
}
acquire_lock || fail "上一张还没画完（锁 $LOCK 被占用）"
trap 'rmdir "$LOCK" 2>/dev/null' EXIT INT TERM

# ---- 准备输出 ----
mkdir -p "$OUT_DIR" "$LOG_DIR" || fail "无法创建输出目录"
ts="$(date +%Y%m%d-%H%M%S)"
rand="$(awk 'BEGIN{srand();printf "%04d",int(rand()*10000)}')"
OUT="$OUT_DIR/fleet-$ts-$rand.png"
LOG="$LOG_DIR/fleet-$ts-$rand.log"
[ -n "$SEED" ] || SEED=-1   # sd.cpp: seed<0 → 随机

# ---- 执行（日志全走文件，stdout 只留最终 JSON）----
start_ms="$(date +%s%3N)"
"$SD_BIN" -m "$MODEL" \
  -p "$PROMPT" \
  -n "low quality, blurry, deformed, watermark, text, extra fingers" \
  -W "$SIZE" -H "$SIZE" \
  --steps "$STEPS" \
  -s "$SEED" \
  -o "$OUT" \
  --backend "$BACKEND" \
  --vae-tiling \
  >"$LOG" 2>&1
rc=$?
end_ms="$(date +%s%3N)"

# 锁随脚本退出由 trap 自动 rmdir，这里无需手动释放

if [ "$rc" -ne 0 ] || [ ! -f "$OUT" ]; then
  fail "sd 执行失败（rc=$rc），日志：$LOG"
fi

# ---- 诚实边界：Vulkan 是否真生效 ----
if ! grep -qiE "vulkan|adreno|mali|gpu" "$LOG"; then
  emit "{\"ok\":false,\"err\":\"日志未发现 Vulkan 设备（疑仍在 CPU 上跑）。日志：$LOG\"}"
  exit 2
fi

emit "{\"ok\":true,\"path\":\"$OUT\",\"ms\":$((end_ms-start_ms)),\"temp\":$TEMP,\"steps\":$STEPS,\"seed\":$SEED,\"size\":$SIZE}"

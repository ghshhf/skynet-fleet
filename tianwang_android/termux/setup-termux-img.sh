#!/usr/bin/env bash
# setup-termux-img.sh — 天网手机出图一键部署（Termux 侧）
#
# 在手机 Termux 里跑一次即可：装依赖 → 编译 stable-diffusion.cpp(Vulkan) → 下模型 → 校验。
# 纪律：
#   - 大文件（模型 ~1.1GB）下载**默认不自动**，需 FLEET_IMG_AGREE_DOWNLOAD=1 或交互确认
#   - 编译失败 / Vulkan 没生效 → 明确报错并给排查步骤，不静默
#   - 幂等：已装/已编译/已下载则跳过
set -u

SD_DIR="$HOME/sd.cpp"
MODELS_DIR="$SD_DIR/models"
BIN="$SD_DIR/bin/sd"
MODEL="${FLEET_IMG_MODEL:-$MODELS_DIR/sd15-q8_0.gguf}"
AGREE="${FLEET_IMG_AGREE_DOWNLOAD:-0}"

log()  { printf '\033[36m[setup]\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[warn ]\033[0m %s\n' "$1"; }
die()  { printf '\033[31m[error]\033[0m %s\n' "$1" >&2; exit 1; }

# ---- 0. 前置：必须在 Termux 里、allow-external-apps ----
if [ ! -d "$HOME" ] || [ -z "${TERMUX_VERSION:-}" ]; then
  die "请在 Termux 应用内运行本脚本（未发现 TERMUX_VERSION 环境变量）"
fi
if ! grep -q "allow-external-apps=true" "$HOME/.termux/termux.properties" 2>/dev/null; then
  warn "Termux 未开启 allow-external-apps。请在 ~/.termux/termux.properties 加 allow-external-apps=true 后重启 Termux，否则天网 App 无法调用本脚本。"
  warn "可用：echo 'allow-external-apps=true' >> ~/.termux/termux.properties"
fi

# ---- 0.5 把脚本摆到 App 调用的固定路径 $HOME/fleet/ ----
mkdir -p "$HOME/fleet"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/fleet-img.sh" ]; then
  cp "$SCRIPT_DIR/fleet-img.sh" "$HOME/fleet/fleet-img.sh"
  chmod +x "$HOME/fleet/fleet-img.sh"
  log "已部署 worker → $HOME/fleet/fleet-img.sh"
fi
cp "$0" "$HOME/fleet/setup-termux-img.sh" 2>/dev/null || true
chmod +x "$HOME/fleet/setup-termux-img.sh"

# ---- 1. 依赖 ----
log "安装依赖（cmake/ninja/clang/vulkan/python/git）…"
pkg update -y >/dev/null 2>&1 || die "pkg update 失败（网络？）"
pkg install -y cmake ninja clang vulkan-loader-android vulkan-headers python git \
  || die "依赖安装失败"

# ---- 2. 编译 sd.cpp（Vulkan 后端）----
if [ -x "$BIN" ]; then
  log "sd 已编译，跳过（$BIN）"
else
  log "克隆 stable-diffusion.cpp …"
  rm -rf "$SD_DIR/src"
  git clone --depth 1 https://github.com/leejet/stable-diffusion.cpp "$SD_DIR/src" \
    || die "克隆失败（网络/磁盘）"
  mkdir -p "$SD_DIR/build" && cd "$SD_DIR/build"
  log "cmake 配置（SD_VULKAN=ON）…"
  cmake -G Ninja -DCMAKE_BUILD_TYPE=Release -DSD_VULKAN=ON "$SD_DIR/src" \
    || die "cmake 配置失败（是否漏 vulkan-loader-android？）"
  log "编译（可能数分钟）…"
  cmake --build . --config Release -j"$(nproc)" \
    || die "编译失败"
  mkdir -p "$SD_DIR/bin"
  # 找产物（不同版本产物名可能变）
  if [ -f bin/sd ]; then
    cp bin/sd "$BIN"
  elif [ -f sd ]; then
    cp sd "$BIN"
  else
    die "编译产物 sd 未找到，请检查 build 输出"
  fi
  log "sd 已编译：$BIN"
fi

# ---- 3. 模型（SD1.5 q8_0，约 1.1GB）----
if [ -f "$MODEL" ]; then
  log "模型已存在，跳过：$MODEL"
else
  if [ "$AGREE" != "1" ]; then
    warn "模型下载约 1.1GB。确认下载？(需 FLEET_IMG_AGREE_DOWNLOAD=1 或非交互已确认)"
    printf '输入 yes 继续：'
    read -r ans
    [ "$ans" = "yes" ] || die "已取消模型下载"
  fi
  mkdir -p "$MODELS_DIR"
  # SD1.5 量化 gguf（社区转换版）。URL 以实际可用镜像为准，此处用 HuggingFace 占位，
  # 真机执行前需在主人的许可下替换为可信源。
  URL="https://huggingface.co/city96/stable-diffusion-v1-5-gguf/resolve/main/sd15_q8_0.gguf"
  log "下载模型（约 1.1GB）…"
  if command -v curl >/dev/null 2>&1; then
    curl -L -o "$MODEL" "$URL" || die "模型下载失败"
  else
    wget -O "$MODEL" "$URL" || die "模型下载失败"
  fi
  [ -s "$MODEL" ] || die "模型文件为空"
  log "模型已就位：$MODEL"
fi

# ---- 4. 校验 Vulkan 真生效 ----
log "校验 Vulkan 后端…"
mkdir -p "$SD_DIR/verify"
OUT="$SD_DIR/verify/vk-check.png"
LOG="$SD_DIR/verify/vk-check.log"
"$BIN" -m "$MODEL" -p "a red apple" -n "" -W 256 -H 256 --steps 4 -s 1 \
  -o "$OUT" --backend diffusion=vulkan,vae=vulkan,te=vulkan --vae-tiling \
  >"$LOG" 2>&1
rc=$?
if [ "$rc" -ne 0 ] || ! grep -qiE "vulkan|adreno|mali|gpu" "$LOG"; then
  warn "校验未确认 Vulkan 生效（rc=$rc）。常见坑："
  warn "  1) Termux 自带 Mesa wrapper 与系统 Vulkan ICD 冲突 → SIGSEGV/软件光栅"
  warn "     排查：env | grep -i vk；确认用的是系统 /system/lib64/libvulkan.so"
  warn "  2) 编译时未真正带 Vulkan（检查 cmake 输出是否有 Vulkan 后端）"
  warn "  详细日志：$LOG"
  die "Vulkan 校验未通过，出图会在 CPU 上跑（极慢且非预期）。"
fi
log "Vulkan 校验通过：日志含 Vulkan 设备名"
log "部署完成。可用：bash ~/fleet/fleet-img.sh --prompt '一只猫' --steps 10 --size 512"

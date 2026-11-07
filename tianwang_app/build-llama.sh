#!/usr/bin/env bash
# 天网 · 本地推理 native 库构建（llama.cpp → arm64-v8a 单 .so）
#
# 为什么不用 Gradle externalNativeBuild：
#   Gradle 那层对自定义 NDK / 工具链不透明，出了问题只能看一屏 CMake 噪声。
#   这里用独立 cmake + NDK 工具链编出 libfleet_llama.so，丢进 jniLibs，
#   Gradle 只管打包。Java 侧一行 loadLibrary("fleet_llama")，无 .so 加载顺序问题。
#
# 2026-11-07 改成「可复现构建」：
#   · NDK / cmake / ninja 全靠探测，不再写死 G:\sdk 这种只在一台机器上成立的路径；
#   · 仓库里没有 llama.cpp 源码时，按固定 tag 自动浅克隆到 third_party/llama.cpp；
#   · 编之前先校验上游 API 与 llama_jni.cpp 对得上（上游改签名要立刻报错，
#     而不是等 APK 装机后 loadLibrary 失败）。
#
# 用法：
#   ./build-llama.sh                       # 默认 arm64-v8a，自动探测环境
#   ANDROID_NDK_HOME=~/Android/Sdk/ndk/27.2.12479018 ./build-llama.sh
#   LLAMA_REF=b9500 ./build-llama.sh       # 临时换上游提交/标签
#   ABIS="arm64-v8a armeabi-v7a" ./build-llama.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ABIS="${ABIS:-arm64-v8a}"
API="${ANDROID_API:-24}"
LLAMA_REF="${LLAMA_REF:-v0.9.0}"
LLAMA_SRC="${LLAMA_SRC_DIR:-$HERE/third_party/llama.cpp}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 8)}"

say() { printf '%s\n' "$*"; }
die() { printf '[错误] %s\n' "$*" >&2; exit 1; }

# ---- 定位 cmake / ninja ----
find_bin() {
  local name="$1"; shift
  local c
  for c in "$@"; do
    [ -n "$c" ] || continue
    if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then printf '%s' "$c"; return 0; fi
  done
  return 1
}
CMAKE="$(find_bin cmake /g/Python312/Scripts/cmake "$(command -v cmake || true)" || true)"
NINJA="$(find_bin ninja \
    /g/winlibs-*/mingw64/bin/ninja \
    "$(command -v ninja || true)" || true)"
[ -n "$CMAKE" ] || die "找不到 cmake（装一个，或把 cmake 加进 PATH）"
[ -n "$NINJA" ] || die "找不到 ninja（装一个，或把 ninja 加进 PATH）"

# ---- 定位 NDK：环境变量优先，其次在常见 SDK 目录里挑版本最高的 ----
pick_ndk() {
  local d
  for d in "${ANDROID_NDK_HOME:-}" "${ANDROID_NDK_ROOT:-}" "${ANDROID_NDK:-}"; do
    [ -f "$d/build/cmake/android.toolchain.cmake" ] && { printf '%s' "$d"; return 0; }
  done
  local roots=("${ANDROID_HOME:-}" "${ANDROID_SDK_ROOT:-}" "$HOME/Android/Sdk" /tmp/android-sdk /g/sdk)
  local root best=""
  for root in "${roots[@]}"; do
    [ -n "$root" ] || continue
    [ -d "$root/ndk" ] || continue
    for d in "$root"/ndk/*; do
      [ -f "$d/build/cmake/android.toolchain.cmake" ] || continue
      if [ -z "$best" ] || [ "$(basename "$d")" \> "$(basename "$best")" ]; then
        best="$d"
      fi
    done
  done
  [ -n "$best" ] && printf '%s' "$best"
}
NDK="$(pick_ndk || true)"
[ -n "$NDK" ] || die '找不到 NDK。设 ANDROID_NDK_HOME，或把 NDK 放进 $ANDROID_HOME/ndk/'
say "NDK=$NDK"

# ---- 准备 llama.cpp 源码：缺就按固定 tag 浅克隆 ----
if [ ! -f "$LLAMA_SRC/CMakeLists.txt" ]; then
  mkdir -p "$(dirname "$LLAMA_SRC")"
  say "本地没有 llama.cpp 源码，按 $LLAMA_REF 克隆到 $LLAMA_SRC ..."
  git clone --depth 1 --branch "$LLAMA_REF" \
      https://github.com/ggml-org/llama.cpp.git "$LLAMA_SRC" \
    || die "克隆 llama.cpp 失败（离线就先把源码放到 $LLAMA_SRC，或设 LLAMA_SRC_DIR）"
fi

# ---- 校验上游 API 与 llama_jni.cpp 一致 ----
# llama_jni.cpp 目前用 5 参数的 llama_sampler_init_penalties(n_vocab, ...) 与 GBNF。
# 上游改签名时这里必须立刻失败，否则会编出一个「能装上、一跑就崩」的 .so。
grep -q 'llama_vocab_n_tokens' "$LLAMA_SRC/include/llama.h" \
  || die "llama.cpp 太旧：缺 llama_vocab_n_tokens（请用 LLAMA_REF=v0.9.0 或更新）"
grep -A6 'llama_sampler_init_penalties' "$LLAMA_SRC/include/llama.h" | grep -q 'n_vocab' \
  || die "llama.cpp 的 penalties 签名变了，先同步 llama_jni.cpp 的 rebuildSampler()"
grep -q 'llama_sampler_init_grammar' "$LLAMA_SRC/include/llama.h" \
  || die "llama.cpp 缺 GBNF 采样器（llama_sampler_init_grammar）"

m2u() { cygpath -m "$1" 2>/dev/null || printf '%s' "$1"; }
BUILD_ROOT="$HERE/build/native"

for ABI in $ABIS; do
  BUILD="$BUILD_ROOT/$ABI"
  rm -rf "$BUILD"
  say "-------- 配置 $ABI（android-$API）--------"
  "$CMAKE" -S "$HERE/app/src/main/cpp" -B "$BUILD" -G Ninja \
      -DLLAMA_SRC_DIR="$(m2u "$LLAMA_SRC")" \
      -DCMAKE_TOOLCHAIN_FILE="$(m2u "$NDK/build/cmake/android.toolchain.cmake")" \
      -DANDROID_ABI="$ABI" \
      -DANDROID_PLATFORM="android-$API" \
      -DANDROID_STL=c++_static \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_MAKE_PROGRAM="$(m2u "$NINJA")"

  say "-------- 编译 $ABI（-j$JOBS）--------"
  "$CMAKE" --build "$BUILD" -j "$JOBS"

  OUT="$HERE/app/src/main/jniLibs/$ABI"
  mkdir -p "$OUT"
  # strip：Release 仍带符号表，46MB → ~12MB，APK 没必要背
  STRIP="$(find_bin llvm-strip "$NDK"/toolchains/llvm/prebuilt/*/bin/llvm-strip* || true)"
  if [ -n "$STRIP" ]; then
    "$STRIP" --strip-unneeded "$BUILD/libfleet_llama.so" 2>/dev/null || true
  fi
  cp "$BUILD/libfleet_llama.so" "$OUT/libfleet_llama.so"
  say "产物: $OUT/libfleet_llama.so  $(wc -c < "$OUT/libfleet_llama.so" | tr -d ' ') 字节"
done

say '完成。Gradle 会把 jniLibs 打进 APK；Kotlin 侧 loadLibrary("fleet_llama") 即可。'
say '提示：jniLibs/*.so 是构建产物，不要提交进 git；换机器重跑本脚本即可。'

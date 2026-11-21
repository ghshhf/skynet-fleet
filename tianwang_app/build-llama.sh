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

# ---- KleidiAI 源码预置（2026-11-15）----
# llama.cpp 的 GGML_CPU_KLEIDIAI 会用 FetchContent 从 github.com/releases 拉
# kleidiai-*-src.tar.gz；本机系统直连出口对 github.com:443 走 HTTP/2 会断流
# （status 16），而 api.github.com 的同 tag tarball 正常。
# 改用 API 通道取源码解压到固定目录，再用 FETCHCONTENT_SOURCE_DIR_KLEIDIAI
# 指过去：CMake 不下载、不校验 URL_HASH，换机器重跑本脚本也能编。
# 关掉 KleidiAI 时这段自动跳过，不产生任何网络动作。
KLEIDIAI_ARG=()
if grep -qE 'set\(GGML_CPU_KLEIDIAI[[:space:]]+ON' "$HERE/app/src/main/cpp/CMakeLists.txt"; then
    KAI_DIR="$HERE/third_party/kleidiai-src"
    if [ ! -f "$KAI_DIR/CMakeLists.txt" ]; then
        mkdir -p "$HERE/third_party/_cache"
        KAI_TGZ="$HERE/third_party/_cache/kleidiai-v1.32.0.tar.gz"
        if [ ! -s "$KAI_TGZ" ]; then
            say "KleidiAI 源码缺失，从 GitHub API 取 v1.32.0 tag ..."
            curl -fL --max-time 180 -o "$KAI_TGZ" \
                "https://api.github.com/repos/ARM-software/kleidiai/tarball/v1.32.0" \
                || die "下载 KleidiAI 失败（也可手动把源码放到 $KAI_DIR）"
        fi
        rm -rf "$KAI_DIR"; mkdir -p "$KAI_DIR"
        tar -xzf "$KAI_TGZ" -C "$KAI_DIR" --strip-components=1 \
            || die "解压 KleidiAI 失败"
    fi
    KLEIDIAI_ARG=(-DFETCHCONTENT_SOURCE_DIR_KLEIDIAI="$(m2u "$KAI_DIR")")
fi

# ---- Vulkan shader 编译器 ----
# ggml-vulkan 的 CMake 用 find_package(Vulkan COMPONENTS glslc REQUIRED)，
# 默认只认系统 VulkanSDK；Android 交叉编译时把编译器显式顶上去即可。
#
# 🔴 2026-11-15：NDK r26d 自带 glslc 是 shaderc v2022.3 / SPIR-V 1.0，
#    coopmat / integer-dot / bf16 / fp8 七个扩展全被判 "not supported"，
#    量化专用 shader 变体根本没编进去 —— 这正是 Mali-G925 上 batch GEMM
#    固定慢 ~13.5s 的土壤。改用 shaderc v2026.5-dev 让变体真正编出来。
#    获取方式：python third_party/glslc-new/fetch_glslc.py
#    （Google shaderc 官方 Windows 预编译包 232MB，脚本走 HTTP Range 只取
#     glslc.exe + shaderc_shared.dll 两个文件，避免大文件直连被重置）。
#    删掉 third_party/glslc-new/ 就是回退到 NDK 旧编译器，一行都不用改。
VULKAN_ARGS=()
GLSLC="$HERE/third_party/glslc-new/glslc.exe"
if [ ! -x "$GLSLC" ]; then
    GLSLC="$NDK/shader-tools/windows-x86_64/glslc.exe"
fi
if [ -x "$GLSLC" ]; then
    export PATH="$(dirname "$GLSLC"):$PATH"
    VULKAN_ARGS+=(-DVulkan_GLSLC_EXECUTABLE="$(m2u "$GLSLC")")
    say "glslc: $GLSLC ($("$GLSLC" --version 2>/dev/null | head -1))"
fi
# NDK 自带的旧拆分版 hpp 与 1.3.237 C 头混用会枚举重复定义，
# 优先用工程内同版本的官方 Vulkan-Hpp；缺了才回退 NDK。
VULKAN_HPP="$HERE/third_party/vulkan-hpp-src"
if [ ! -f "$VULKAN_HPP/vulkan/vulkan.hpp" ]; then
    VULKAN_HPP="$NDK/sources/third_party/vulkan/src/include"
fi
if [ -f "$VULKAN_HPP/vulkan/vulkan.hpp" ]; then
    VULKAN_ARGS+=(-DVULKAN_HPP_INCLUDE_DIR="$(m2u "$VULKAN_HPP")")
fi

# ggml-vulkan 还要 find_package(SPIRV-Headers CONFIG REQUIRED)。它是纯头文件包，
# 用 host 侧 cmake 装到工程内固定目录即可，不参与 Android 交叉编译。
SPIRV_ARGS=()
if grep -qE 'set\(GGML_VULKAN[[:space:]]+ON' "$HERE/app/src/main/cpp/CMakeLists.txt"; then
    SPIRV_SRC="$HERE/third_party/SPIRV-Headers"
    SPIRV_TGZ="$HERE/third_party/_cache/spirv-headers.tar.gz"
    if [ ! -f "$SPIRV_SRC/CMakeLists.txt" ]; then
        mkdir -p "$(dirname "$SPIRV_TGZ")"
        if [ ! -s "$SPIRV_TGZ" ]; then
            say "SPIRV-Headers 缺失，从 GitHub API 取源码 ..."
            curl -fL --max-time 180 -o "$SPIRV_TGZ" \
                "https://api.github.com/repos/KhronosGroup/SPIRV-Headers/tarball/main" \
                || die "下载 SPIRV-Headers 失败"
        fi
        rm -rf "$SPIRV_SRC"; mkdir -p "$SPIRV_SRC"
        tar -xzf "$SPIRV_TGZ" -C "$SPIRV_SRC" --strip-components=1 || die "解压 SPIRV-Headers 失败"
    fi
    SPIRV_INSTALL="$HERE/third_party/spirv-headers-install"
    SPIRV_CFG="$SPIRV_INSTALL/share/cmake/SPIRV-Headers/SPIRV-HeadersConfig.cmake"
    if [ ! -f "$SPIRV_CFG" ]; then
        say "安装 SPIRV-Headers（host 侧，仅头文件）..."
        "$CMAKE" -S "$(m2u "$SPIRV_SRC")" -B "$(m2u "$HERE/build/spirv-headers-host")" -G Ninja \
            -DCMAKE_INSTALL_PREFIX="$(m2u "$SPIRV_INSTALL")" \
            -DCMAKE_MAKE_PROGRAM="$(m2u "$NINJA")" >/dev/null
        "$CMAKE" --install "$(m2u "$HERE/build/spirv-headers-host")" >/dev/null
    fi
    SPIRV_ARGS+=(-DSPIRV-Headers_DIR="$(m2u "$SPIRV_INSTALL/share/cmake/SPIRV-Headers")")
    # upstream 只 find_package(SPIRV-Headers)，没把 include 挂到 ggml-vulkan target，
    # 但 ggml-vulkan/ggml-vulkan-types.h 直接 #include <spirv/unified1/spirv.hpp>。
    # 顶层 CMakeLists 靠这个变量补一路 BEFORE PRIVATE include。
    SPIRV_ARGS+=(-DSPIRV_HPP_INCLUDE_DIR="$(m2u "$SPIRV_INSTALL/include")")
fi

for ABI in $ABIS; do
  BUILD="$BUILD_ROOT/$ABI"
  # 2026-11-15：默认仍是「干净重编」以保证可复现，但允许 KEEP_BUILD=1 走增量。
  # 开 Vulkan 后每个 .comp 要 glslc → .spv → .cpp → .o 两轮编译，155 个着色器
  # 加 18 秒的宿主 vulkan-shaders-gen，全删重建一次就是几十秒；改 Kotlin / 只调
  # JNI 时这些产物一个都不该重编。CMake 自己会检测 CMakeLists / 编译参数变化并
  # 触发必要的重配，所以增量不会编出过期产物。
  if [ "${KEEP_BUILD:-0}" = "1" ] && [ -f "$BUILD/build.ninja" ]; then
    say "-------- 增量配置 $ABI（KEEP_BUILD=1，复用 shader/微内核产物）--------"
  else
    rm -rf "$BUILD"
    say "-------- 配置 $ABI（android-$API，全量）--------"
  fi
  "$CMAKE" -S "$HERE/app/src/main/cpp" -B "$BUILD" -G Ninja \
      -DLLAMA_SRC_DIR="$(m2u "$LLAMA_SRC")" \
      -DCMAKE_TOOLCHAIN_FILE="$(m2u "$NDK/build/cmake/android.toolchain.cmake")" \
      -DANDROID_ABI="$ABI" \
      -DANDROID_PLATFORM="android-$API" \
      -DANDROID_STL=c++_static \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_MAKE_PROGRAM="$(m2u "$NINJA")" \
      "${KLEIDIAI_ARG[@]}" \
      "${VULKAN_ARGS[@]}" \
      "${SPIRV_ARGS[@]}"

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

#!/usr/bin/env bash
# 编手机本地 LLM 的 .so（arm64-v8a，llama.cpp，CPU）。
#
# 产出 tianwang_android/jni/build-llm/libfleet_llm.so，主 build.sh 会把它塞进 APK。
# 引擎内嵌进 App，模型由 ModelManager 管（App 内下载），不再依赖 Termux 或
# 任何外部服务——这正是「手机下好模型却调不动」的根治。
#
# 用法：
#   bash jni/build-llm.sh                       # 用默认源码目录
#   LLAMA_SRC=/path/to/llama.cpp bash jni/build-llm.sh
#
# llama.cpp 不 vendoring 进本仓库（源码 + ggml 子模块几百 MB）。需要时：
#   git clone --depth 1 --filter=blob:none --no-checkout https://github.com/ggerganov/llama.cpp
#   cd llama.cpp && git checkout && git submodule update --init --recursive --depth 1
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NDK="${ANDROID_NDK_HOME:-/d/DevTools/Android/ndk/27.0.12077973}"
LLAMA_SRC="${LLAMA_SRC:-/d/tmp/llama.cpp}"
TOOLCHAIN="$NDK/build/cmake/android.toolchain.cmake"
STRIP="$NDK/toolchains/llvm/prebuilt/windows-x86_64/bin/llvm-strip.exe"

ABI="arm64-v8a"
API_LEVEL="26"
JOBS="$(nproc 2>/dev/null || echo 8)"

for f in "$TOOLCHAIN" "$STRIP" "$LLAMA_SRC/include/llama.h" "$LLAMA_SRC/ggml/include/ggml.h"; do
  [ -e "$f" ] || { echo "缺依赖：$f" >&2
    echo "  没有 llama 源码就：LLAMA_SRC=/path/to/llama.cpp bash jni/build-llm.sh" >&2
    exit 1; }
done

CMAKE_COMMON=(
  -G Ninja
  -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN"
  -DANDROID_ABI="$ABI"
  -DANDROID_PLATFORM="android-$API_LEVEL"
  -DCMAKE_BUILD_TYPE=Release
  # GGML_NATIVE=OFF：交叉编译不能按本机 CPU 生成指令集
  -DGGML_NATIVE=OFF
  # 纯 CPU：先保证在所有机器都能跑（GPU 收益见 whisper_jni 注释，先要能用）
  -DGGML_OPENMP=OFF
  -DGGML_VULKAN=OFF
  -DGGML_BLAS=OFF
)

# ---- ① llama.cpp 静态库（llama + ggml* 一起出） ----
STATIC="$LLAMA_SRC/build-static"
if [ ! -f "$STATIC/src/libllama.a" ]; then
  echo "[1/3] 编 llama.cpp 静态库 ..."
  cmake -B "$STATIC" -S "$LLAMA_SRC" "${CMAKE_COMMON[@]}" \
    -DBUILD_SHARED_LIBS=OFF \
    -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
    -DLLAMA_BUILD_SERVER=OFF -DLLAMA_BUILD_TOOLS=OFF >/dev/null
  cmake --build "$STATIC" --target llama -- -j"$JOBS" >/dev/null
else
  echo "[1/3] 复用已有 llama 静态库"
fi

# 确认四个 .a 都齐了（llama.cpp 版本不同可能换名字，缺哪个报哪个）
for a in "$STATIC/src/libllama.a" "$STATIC/ggml/src/libggml.a" \
         "$STATIC/ggml/src/libggml-cpu.a" "$STATIC/ggml/src/libggml-base.a"; do
  [ -f "$a" ] || { echo "缺静态库：$a（llama.cpp 版本变了？改 build-llm.sh 的库名）" >&2; exit 1; }
done

# ---- ② JNI 包装层 ----
echo "[2/3] 编 libfleet_llm.so ..."
cmake -B "$HERE/build-llm" -S "$HERE" "${CMAKE_COMMON[@]}" -DLLAMA_DIR="$LLAMA_SRC" >/dev/null
cmake --build "$HERE/build-llm" --target fleet_llm -- -j"$JOBS" >/dev/null

# ---- ③ strip ----
echo "[3/3] strip ..."
"$STRIP" --strip-unneeded "$HERE/build-llm/libfleet_llm.so"

SIZE="$(wc -c < "$HERE/build-llm/libfleet_llm.so" | tr -d ' ')"
echo "--------"
echo "产物：jni/build-llm/libfleet_llm.so  ${SIZE} 字节"
echo "下一步：bash build.sh  （会把它合进 APK 的 lib/$ABI/）"

#!/usr/bin/env bash
# 编手机本地语音的 .so（arm64-v8a）。
#
# 产出 tianwang_android/jni/build/libfleet_whisper.so，主 build.sh 会把它塞进 APK。
#
# 为什么单独一个脚本：
#   1. whisper.cpp 编一次 1.5 分钟，改 UI 时没必要陪着编；
#   2. 产物已经 strip 到 1.6MB，直接复用它比在 CI 里现编更快、更稳。
#
# 用法：
#   bash jni/build.sh                      # 用默认源码目录
#   WHISPER_SRC=/path/to/whisper.cpp bash jni/build.sh
#
# whisper.cpp 是 MIT 许可，源码不 vendoring 进本仓库（52MB，且有大半是
# examples/tests 用不上）。需要时：
#   git clone --depth 1 https://github.com/ggerganov/whisper.cpp
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NDK="${ANDROID_NDK_HOME:-/d/DevTools/Android/ndk/27.0.12077973}"
WHISPER_SRC="${WHISPER_SRC:-/d/tmp/whisper.cpp}"
TOOLCHAIN="$NDK/build/cmake/android.toolchain.cmake"
STRIP="$NDK/toolchains/llvm/prebuilt/windows-x86_64/bin/llvm-strip.exe"

ABI="arm64-v8a"
API_LEVEL="26"
JOBS="$(nproc 2>/dev/null || echo 8)"

for f in "$TOOLCHAIN" "$STRIP" "$WHISPER_SRC/src/whisper.cpp"; do
  [ -e "$f" ] || { echo "缺依赖：$f" >&2
    echo "  没有 whisper 源码就：git clone --depth 1 https://github.com/ggerganov/whisper.cpp $WHISPER_SRC" >&2
    exit 1; }
done

CMAKE_COMMON=(
  -G Ninja
  -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN"
  -DANDROID_ABI="$ABI"
  -DANDROID_PLATFORM="android-$API_LEVEL"
  -DCMAKE_BUILD_TYPE=Release
  # GGML_NATIVE=OFF：交叉编译时不能按"本机 CPU"生成指令集，否则会在手机上
  # 执行到编译它的那台机器才有的指令直接崩。
  -DGGML_NATIVE=OFF
  -DGGML_OPENMP=OFF
  -DGGML_VULKAN=OFF
)

# ---- ① whisper.cpp 静态库（只编一次，已存在就跳过） ----
STATIC="$WHISPER_SRC/build-static"
if [ ! -f "$STATIC/src/libwhisper.a" ]; then
  echo "[1/3] 编 whisper.cpp 静态库 ..."
  cmake -B "$STATIC" -S "$WHISPER_SRC" "${CMAKE_COMMON[@]}" \
    -DBUILD_SHARED_LIBS=OFF \
    -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_EXAMPLES=OFF -DWHISPER_BUILD_SERVER=OFF \
    >/dev/null
  cmake --build "$STATIC" --target whisper -- -j"$JOBS" >/dev/null
else
  echo "[1/3] 复用已有 whisper 静态库"
fi

# ---- ② JNI 包装层 ----
echo "[2/3] 编 libfleet_whisper.so ..."
cmake -B "$HERE/build" -S "$HERE" "${CMAKE_COMMON[@]}" -DWHISPER_DIR="$WHISPER_SRC" >/dev/null
cmake --build "$HERE/build" -- -j"$JOBS" >/dev/null

# ---- ③ strip ----
# 不 strip 是 13.9MB，strip 后 1.6MB：差的全是符号表，手机上一点用没有。
echo "[3/3] strip ..."
"$STRIP" --strip-unneeded "$HERE/build/libfleet_whisper.so"

SIZE="$(wc -c < "$HERE/build/libfleet_whisper.so" | tr -d ' ')"
echo "--------"
echo "产物：jni/build/libfleet_whisper.so  ${SIZE} 字节"
echo "下一步：bash build.sh  （会把它合进 APK 的 lib/$ABI/）"

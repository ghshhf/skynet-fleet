#!/usr/bin/env bash
# 天网 · 本地推理 native 库构建（llama.cpp → arm64-v8a 单 .so）
#
# 不用 Gradle externalNativeBuild：本机 G:\sdk 没有 SDK 版 cmake，且 Gradle
# 那层对自定义 NDK/工具链不透明。改成老工程同款路线——独立 cmake + NDK 工具链
# 编出 libfleet_llama.so，丢进 app/src/main/jniLibs，让 Gradle 直接打包。
# Java 侧只 loadLibrary("fleet_llama")，无 .so 加载顺序问题。
#
# 依赖（本机实测路径，找不到会明确报错）：
#   NDK   → /g/sdk/ndk/android-ndk-r26d
#   CMake → pip 的那份（Python312/Scripts/cmake）
#   Ninja → winlibs mingw 里那份
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NDK="${ANDROID_NDK:-/g/sdk/ndk/android-ndk-r26d}"
LLAMA_SRC="${LLAMA_SRC_DIR:-/g/dev/llama.cpp-fork}"
ABI="arm64-v8a"
API=24
JOBS="$(nproc 2>/dev/null || echo 8)"

# ---- 定位 CMake / Ninja（候选制，任一可用即可）----
find_bin() {
  local name="$1"; shift
  for c in "$@"; do
    if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then printf '%s' "$c"; return 0; fi
  done
  return 1
}
CMAKE="$(find_bin cmake /g/Python312/Scripts/cmake "$(command -v cmake 2>/dev/null || true)" || true)"
NINJA="$(find_bin ninja \
    "/g/winlibs-x86_64-posix-seh-gcc-16.1.0-mingw-w64ucrt-14.0.0-r3/mingw64/bin/ninja" \
    "$(command -v ninja 2>/dev/null || true)" || true)"

[ -n "$CMAKE" ] || { echo "找不到 cmake" >&2; exit 1; }
[ -n "$NINJA" ] || { echo "找不到 ninja" >&2; exit 1; }
[ -f "$NDK/build/cmake/android.toolchain.cmake" ] || { echo "NDK 工具链不在: $NDK" >&2; exit 1; }
[ -f "$LLAMA_SRC/CMakeLists.txt" ] || { echo "llama.cpp 源码不在: $LLAMA_SRC" >&2; exit 1; }

echo "CMAKE=$CMAKE"
echo "NINJA=$NINJA"
echo "NDK=$NDK"
echo "LLAMA_SRC=$LLAMA_SRC"

BUILD="$HERE/app/src/main/cpp/build-android-$ABI"
rm -rf "$BUILD"

"$CMAKE" -S "$HERE/app/src/main/cpp" -B "$BUILD" \
    -DLLAMA_SRC_DIR="$(cygpath -m "$LLAMA_SRC" 2>/dev/null || echo "$LLAMA_SRC")" \
    -DCMAKE_TOOLCHAIN_FILE="$(cygpath -m "$NDK/build/cmake/android.toolchain.cmake" 2>/dev/null || echo "$NDK/build/cmake/android.toolchain.cmake")" \
    -DANDROID_ABI="$ABI" \
    -DANDROID_PLATFORM="android-$API" \
    -DANDROID_STL=c++_static \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_MAKE_PROGRAM="$(cygpath -m "$NINJA" 2>/dev/null || echo "$NINJA")" \
    -G Ninja

echo "-------- 开始编译（arm64-v8a，-j$JOBS）--------"
"$CMAKE" --build "$BUILD" -j "$JOBS"

OUT="$HERE/app/src/main/jniLibs/$ABI"
mkdir -p "$OUT"
# strip 调试符号：Release 构建仍带 -g，46MB → 约 12MB，APK 没必要背符号表
STRIP="$NDK/toolchains/llvm/prebuilt/windows-x86_64/bin/llvm-strip.exe"
if [ -f "$STRIP" ]; then "$STRIP" --strip-unneeded "$BUILD/libfleet_llama.so" 2>/dev/null || true; fi
cp "$BUILD/libfleet_llama.so" "$OUT/libfleet_llama.so"
SIZE="$(wc -c < "$OUT/libfleet_llama.so" | tr -d ' ')"
echo "--------"
echo "产物: $OUT/libfleet_llama.so  ${SIZE} 字节"
echo "Gradle 会把 jniLibs 打进 APK；Kotlin 侧 loadLibrary(\"fleet_llama\") 即可。"

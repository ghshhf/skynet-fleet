#!/usr/bin/env bash
# 编手机本地出图的 .so（arm64-v8a，Vulkan 后端）。
#
# 产出 tianwang_android/jni/build/libfleet_sd.so，主 build.sh 会把它塞进 APK。
#
# 为什么单独一个脚本（同 jni/build.sh 的理由）：
#   1. sd.cpp + ggml 编一次要几分钟，改 UI 时没必要陪着编；
#   2. 产物 strip 后直接复用，比每次现编快、比 CI 现编稳。
#
# 用法：
#   bash jni/build-sd.sh                      # 用默认源码目录
#   SD_SRC=/path/to/stable-diffusion.cpp bash jni/build-sd.sh
#
# stable-diffusion.cpp 是 MIT 许可，源码不 vendoring 进本仓库（几百 MB，且有大半是
# examples/docs 用不上）。需要时：
#   git clone --depth 1 --recurse-submodules https://github.com/leejet/stable-diffusion.cpp
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NDK="${ANDROID_NDK_HOME:-/d/DevTools/Android/ndk/27.0.12077973}"
SD_SRC="${SD_SRC:-/d/tmp/stable-diffusion.cpp}"
TOOLCHAIN="$NDK/build/cmake/android.toolchain.cmake"
STRIP="$NDK/toolchains/llvm/prebuilt/windows-x86_64/bin/llvm-strip.exe"

ABI="arm64-v8a"
# 28 而不是 26：ggml-vulkan-buffers.cpp 用了 std::aligned_alloc，Android 的 libc
# 到 API 28（Android 9）才提供它，26/27 编出来是
# "reference to unresolved using declaration"（libc++ 那句 using 被 _LIBCPP_USING_IF_EXISTS
# 挡掉）。代价：这条 .so 要求 Android 9+；whisper 那条仍是 26，互不影响。
# TODO(minSdk)：manifest 现在还是 minSdkVersion=26，要不要跟着抬到 28 待主人拍板；
# 不抬的话需要在 Java 侧按 Build.VERSION.SDK_INT >= 28 挡一道再 loadLibrary。
API_LEVEL="28"
JOBS="$(nproc 2>/dev/null || echo 8)"

# Vulkan 三件套全部从 NDK 里拿（不装 LunarG SDK，几百 MB 不值当）：
#   glslc   —— NDK 自带 shader-tools/windows-x86_64/glslc.exe
#   头/库   —— NDK sysroot 里就有 vulkan.h 和 libvulkan.so
#   SPIRV-Headers —— ggml-vulkan 的 CMake 硬性 find_package，单独拉一份配置好
# 用缓存变量预置（find_path/find_library/find_program 见到已存在的缓存值就跳过搜索），
# 不依赖 VULKAN_SDK 环境变量，也不往 PATH 里塞东西。
GLSLC="$NDK/shader-tools/windows-x86_64/glslc.exe"
# 头文件用 Khronos 的 Vulkan-Headers，不用 NDK sysroot 的：sysroot 只有 C 头
# vulkan.h，而 ggml-vulkan 的 ggml-vulkan-types.h 要 #include <vulkan/vulkan.hpp>
# （C++ 绑定头），NDK 不带，编到一半才炸 'vulkan/vulkan.hpp' file not found。
VK_INC="${VULKAN_HEADERS_DIR:-/d/tmp/Vulkan-Headers/include}"
VK_LIB="$NDK/toolchains/llvm/prebuilt/windows-x86_64/sysroot/usr/lib/aarch64-linux-android/$API_LEVEL/libvulkan.so"
SPIRV_PREFIX="${SPIRV_HEADERS_PREFIX:-/d/tmp/SPIRV-Headers/install}"

for f in "$GLSLC" "$VK_INC/vulkan/vulkan.h" "$VK_INC/vulkan/vulkan.hpp" "$VK_LIB" "$SPIRV_PREFIX/share/cmake/SPIRV-Headers/SPIRV-HeadersConfig.cmake"; do
  [ -e "$f" ] || { echo "缺依赖：$f" >&2
    echo "  SPIRV-Headers 没有就：git clone https://github.com/KhronosGroup/SPIRV-Headers /d/tmp/SPIRV-Headers" >&2
    echo "  然后 cmake -B /d/tmp/SPIRV-Headers/build -S /d/tmp/SPIRV-Headers -DCMAKE_INSTALL_PREFIX=/d/tmp/SPIRV-Headers/install && cmake --install /d/tmp/SPIRV-Headers/build" >&2
    echo "  Vulkan-Headers 没有就：git clone --depth 1 https://github.com/KhronosGroup/Vulkan-Headers /d/tmp/Vulkan-Headers" >&2
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
  # Vulkan 是这条路的全部意义：CPU 出 SD1.5 一张要几分钟，没有 GPU 加速这个
  # 功能就没有存在理由。sd.cpp 见 SD_VULKAN 会自动带 GGML_VULKAN。
  # 选 Vulkan 而不是 Adreno/NPU 私有栈（QNN / MediaPipe）的理由是跨厂商：
  # 目标机 K80 Ultra 是天玑 9400+（Immortalis-G925，见 SdBridge.java 注释），
  # 走 Vulkan 就不绑死高通，换别的机器不用重做。
  -DSD_VULKAN=ON
  -DSD_BUILD_EXAMPLES=OFF
  -DSD_BUILD_TESTS=OFF
  # NDK 的 android-legacy.toolchain.cmake 会把 CMAKE_FIND_ROOT_PATH_MODE_PACKAGE
  # 设成 ONLY——只搜 sysroot，宿主机上的 SPIRV-Headers 再找不到。改成 BOTH，
  # 让 find_package 两边都看（本次只有 SPIRV-Headers/Vulkan 走 find_package，
  # 都是宿主侧工具，不会误捞 target 库）。
  -DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=BOTH
  # Vulkan/SPIRV 预置（见上）
  -DVulkan_GLSLC_EXECUTABLE="$GLSLC"
  -DVulkan_INCLUDE_DIR="$VK_INC"
  -DVulkan_LIBRARY="$VK_LIB"
  -DCMAKE_PREFIX_PATH="$SPIRV_PREFIX"
  # SPIRV-Headers 的 include（spirv/unified1/spirv.hpp）：ggml 只 find_package
  # 不挂 include 目录，靠 jni/CMakeLists.txt 里那句 include_directories 补。
  -DSPIRV_INCLUDE_DIR="$SPIRV_PREFIX/include"
)

# jni/CMakeLists.txt 会把 SD_SRC 整棵树 add_subdirectory 进来（llmedge 同款），
# 编出稳定-diffusion 静态库再链进 fleet_sd。
echo "[1/2] 配置并编 libfleet_sd.so ..."
cmake -B "$HERE/build-sd" -S "$HERE" "${CMAKE_COMMON[@]}" -DSD_DIR="$SD_SRC" >/dev/null
cmake --build "$HERE/build-sd" --target fleet_sd -- -j"$JOBS" >/dev/null

echo "[2/2] strip ..."
# 不 strip 会带上全部符号表；strip 只去调试信息，JNI 导出符号不受影响。
"$STRIP" --strip-unneeded "$HERE/build-sd/libfleet_sd.so"

SIZE="$(wc -c < "$HERE/build-sd/libfleet_sd.so" | tr -d ' ')"
echo "--------"
echo "产物：jni/build-sd/libfleet_sd.so  ${SIZE} 字节"
echo "下一步：bash build.sh  （会把它合进 APK 的 lib/$ABI/）"

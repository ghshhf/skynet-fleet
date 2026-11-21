cmake_minimum_required(VERSION 3.18)
# 顶层显式开 C：ggml 里混着 C 源（老工程踩过「No known features for C compiler」）
project(fleet_llama C CXX)

# llama.cpp 源码目录由 build-llama.sh 用 -DLLAMA_SRC_DIR= 传入。
# 默认指向仓库内 third_party（脚本会自动浅克隆到这里），不再写死某台 Windows 机器的盘符。
if(NOT DEFINED LLAMA_SRC_DIR)
    set(LLAMA_SRC_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../../../third_party/llama.cpp")
endif()
message(STATUS "fleet_llama: LLAMA_SRC_DIR=${LLAMA_SRC_DIR}")

# 只留库，别把上游 examples/tests/tools 拖进来。
# COMMON 我们一行都没用到（JNI 直接调 libllama），关掉能少编一堆东西。
#
# Vulkan GPU 后端（2026-11-15 开）：真机有 vulkan.mali.so + libOpenCL.so + APU 驱动，
# 后端本身一直具备条件，之前是被这一行硬关掉。开了之后 CPU/GPU 由 Kotlin 侧
# Dev.plan() 按「模型体积 vs 可用内存」选：11GB 模型在 11GB 内存上不会推给 GPU
# （UMA 下权重复制进不可回收 buffer 必被 LMK 杀），小模型才交给 GPU。
# 是否真的更快由实测决定，不由注释决定。
set(LLAMA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TESTS   OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TOOLS   OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_COMMON  OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_APP     OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_MTMD    OFF CACHE BOOL "" FORCE)
set(LLAMA_CURL          OFF CACHE BOOL "" FORCE)
set(BUILD_SHARED_LIBS   OFF CACHE BOOL "" FORCE)
set(GGML_NATIVE         OFF CACHE BOOL "" FORCE)
set(GGML_OPENMP         OFF CACHE BOOL "" FORCE)
set(GGML_VULKAN         ON  CACHE BOOL "" FORCE)

# ---- ARM CPU 矩阵内核（2026-11-15）----
# 交叉编译时 GGML_NATIVE=OFF 且不给 -march，编译器就落在 armv8-a 基线：
# __ARM_FEATURE_DOTPROD / __ARM_FEATURE_MATMUL_INT8 均未定义，ggml 直接把
# 快内核编译掉。二进制能跑、结果正确，只是慢数倍，且全程没有任何告警。
# 真机 25060RK16C（天玑 9400+ / Cortex-X925+X4+A720）的 /proc/cpuinfo 实测带
# asimddp(dotprod)、i8mm、sve2、svei8mm、bf16，无 sme —— 所以 KleidiAI 会在
# 运行时按 SME2 -> I8MM -> DotProd 的优先级命中 I8MM。
# 只作用于 ggml-cpu 这个 target，不动全局编译参数；零额外内存、零兼容性代价。
set(GGML_CPU_ARM_ARCH   "armv8.2-a+dotprod+i8mm+fp16" CACHE STRING "" FORCE)
set(GGML_CPU_KLEIDIAI   ON  CACHE BOOL "" FORCE)

# EXCLUDE_FROM_ALL：llama.cpp 自带的 examples/tests 不跟着我们的 .so 一起编
add_subdirectory(${LLAMA_SRC_DIR} ${CMAKE_CURRENT_BINARY_DIR}/llama.cpp EXCLUDE_FROM_ALL)

# NDK 的 sysroot 只带 Vulkan C 头（vulkan/vulkan.h）；C++ 绑定 vulkan.hpp 放在
# NDK 的 sources/third_party/vulkan/src/include 里，FindVulkan 不会自动加。
# ggml-vulkan 用 Vulkan::Vulkan，只给了 C 头路径，所以这里显式补一路。
if (GGML_VULKAN AND DEFINED VULKAN_HPP_INCLUDE_DIR AND TARGET ggml-vulkan)
    target_include_directories(ggml-vulkan BEFORE PRIVATE "${VULKAN_HPP_INCLUDE_DIR}")
endif()
# ggml-vulkan-types.h 还直接 include <spirv/unified1/spirv.hpp>，但 upstream 只
# find_package 了这个包、没把它的 include 挂到 ggml-vulkan target 上。
if (GGML_VULKAN AND DEFINED SPIRV_HPP_INCLUDE_DIR AND TARGET ggml-vulkan)
    target_include_directories(ggml-vulkan BEFORE PRIVATE "${SPIRV_HPP_INCLUDE_DIR}")
endif()

# 薄 JNI + 上游静态链成单 .so（同 fleet_whisper / fleet_sd 套路）：
# Java 侧只 loadLibrary("fleet_llama")，无 .so 加载顺序问题。
set(FLEET_LLAMA_SOURCES llama_jni.cpp)
# Android 的 libvulkan.so stub 缺 Vulkan 1.1 符号，补一层薄转发（见文件内注释）。
if (GGML_VULKAN AND ANDROID)
    list(APPEND FLEET_LLAMA_SOURCES vk_android_shim.cpp)
endif()
add_library(fleet_llama SHARED ${FLEET_LLAMA_SOURCES})

target_include_directories(fleet_llama PRIVATE
    "${LLAMA_SRC_DIR}/include"
    "${LLAMA_SRC_DIR}/ggml/include")

target_link_libraries(fleet_llama PRIVATE
    llama
    ggml
    # JNI 里直接读 GGUF 元数据（层数/训练上下文）——gguf_* 在 ggml-base，
    # 静态链接必须显式列出，否则混合模式的「按比例切层」链接不过。
    ggml-base
    android
    log)

target_compile_options(fleet_llama PRIVATE -O3 -fvisibility=hidden -ffunction-sections -fdata-sections)
target_link_options(fleet_llama PRIVATE -Wl,--gc-sections -Wl,--exclude-libs,ALL)

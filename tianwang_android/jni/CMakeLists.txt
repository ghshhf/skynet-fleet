cmake_minimum_required(VERSION 3.18)
# 同时开 C：sd.cpp/ggml 里混着 C 源，ggml 自己的 project() 会在子目录开 C，
# 但顶层没开 → generate 阶段给顶层记 C compile features 时编译器 ID 是空的，
# 直接报 "No known features for C compiler"。顶层显式开掉就没这事。
project(fleet_jni C CXX)

# 编队 · 手机端 native 库统一入口。
# 现有两个目标：fleet_whisper（语音识别）、fleet_sd（本地出图）。
# 都是「薄 JNI + 上游源码静态链成单 .so」路线：Java 侧只有一行
# loadLibrary，没有 .so 依赖顺序问题。
#
# 两段各自独立：build.sh 只传 WHISPER_DIR，build-sd.sh 只传 SD_DIR，
# 缺哪个就跳过哪段（否则编出图时还得先准备一份 whisper 源码）。

if(NOT DEFINED WHISPER_DIR AND NOT DEFINED SD_DIR)
    message(FATAL_ERROR "至少要传一个：-DWHISPER_DIR=<whisper.cpp 源码目录> 或 -DSD_DIR=<stable-diffusion.cpp 源码目录>")
endif()

# ---------------------------------------------------------------- whisper
# 把 whisper.cpp 的全部源码静态链进这一个 .so。
#
# 为什么不按官方那样出 libwhisper.so + libggml*.so 四件套：
#   Android 的 System.loadLibrary 要自己保证依赖顺序（ggml-base → ggml →
#   ggml-cpu → whisper），少一个就 UnsatisfiedLinkError，而且报错只说"找不到符号"，
#   不说是谁没加载。合成一个 .so 后 Java 侧只有一行 loadLibrary，没有顺序问题。
#
# WHISPER_DIR 由 build.sh 传进来（whisper.cpp 源码目录，含 build-static 产出的 .a）。

if(DEFINED WHISPER_DIR)

add_library(fleet_whisper SHARED whisper_jni.cpp)

target_include_directories(fleet_whisper PRIVATE
    "${WHISPER_DIR}/include"
    "${WHISPER_DIR}/ggml/include")

# 链接顺序有讲究：被依赖的放后面（whisper → ggml → ggml-cpu → ggml-base）
target_link_libraries(fleet_whisper PRIVATE
    "${WHISPER_DIR}/build-static/src/libwhisper.a"
    "${WHISPER_DIR}/build-static/ggml/src/libggml.a"
    "${WHISPER_DIR}/build-static/ggml/src/libggml-cpu.a"
    "${WHISPER_DIR}/build-static/ggml/src/libggml-base.a"
    android
    log)

target_compile_options(fleet_whisper PRIVATE -O3 -fvisibility=hidden -ffunction-sections -fdata-sections)
target_link_options(fleet_whisper PRIVATE -Wl,--gc-sections -Wl,--exclude-libs,ALL)

endif()  # WHISPER_DIR

# ---------------------------------------------------------------- sd（出图）
# stable-diffusion.cpp 走 add_subdirectory 整棵树拉进来（llmedge 同款路线），
# 而不是先编一份 .a 再链：它的依赖是 ggml + zip + onig + utf8proc 四个静态库，
# PRIVATE 依赖手工列全容易漏，让 CMake 自己传播链接行更稳。
# SD_VULKAN=ON 由 build-sd.sh 传入（sd.cpp 的 CMake 会自动带 GGML_VULKAN=ON）。
# EXCLUDE_FROM_ALL：sd.cpp 自带的 examples/tests 不跟着编。

if(DEFINED SD_DIR)

# ggml-vulkan 只 find_package(SPIRV-Headers)，并没把它的 include 目录挂到编译
# 目标上，于是 ggml-vulkan-types.h 里的 #include <spirv/unified1/spirv.hpp>
# 直接找不到。顶层 include_directories 会被后面 add_subdirectory 的子目录继承，
# 这里补一次就够，不用去改上游源码。
if(DEFINED SPIRV_INCLUDE_DIR)
    include_directories("${SPIRV_INCLUDE_DIR}")
endif()

add_subdirectory("${SD_DIR}" "${CMAKE_CURRENT_BINARY_DIR}/sdcpp" EXCLUDE_FROM_ALL)

add_library(fleet_sd SHARED sd_jni.cpp)

target_include_directories(fleet_sd PRIVATE "${SD_DIR}/include")

# vulkan：ggml-vulkan 自己会 find_package(Vulkan) 并挂上进链接行；
# z：PNG 编码用 NDK 自带 zlib，不引第三方头文件。
target_link_libraries(fleet_sd PRIVATE
    stable-diffusion
    android
    log
    z)

target_compile_options(fleet_sd PRIVATE -O3 -fvisibility=hidden -ffunction-sections -fdata-sections)
target_link_options(fleet_sd PRIVATE -Wl,--gc-sections -Wl,--exclude-libs,ALL)

endif()  # SD_DIR

# ---------------------------------------------------------------- llama (本地 LLM 对话)
# 和 whisper 同路：把 llama.cpp 全部源码静态链进单个 .so，Java 侧只有一行
# loadLibrary，没有 .so 依赖顺序问题。
# LLAMA_DIR 由 build-llm.sh 传入（llama.cpp 源码目录，含 build-static 产出的 .a）。

if(DEFINED LLAMA_DIR)

add_library(fleet_llm SHARED llm_jni.cpp)

target_include_directories(fleet_llm PRIVATE
    "${LLAMA_DIR}/include"
    "${LLAMA_DIR}/ggml/include")

# 链接顺序有讲究：被依赖的放后面（llama → ggml → ggml-cpu → ggml-base）
target_link_libraries(fleet_llm PRIVATE
    "${LLAMA_DIR}/build-static/src/libllama.a"
    "${LLAMA_DIR}/build-static/ggml/src/libggml.a"
    "${LLAMA_DIR}/build-static/ggml/src/libggml-cpu.a"
    "${LLAMA_DIR}/build-static/ggml/src/libggml-base.a"
    android
    log)

target_compile_options(fleet_llm PRIVATE -O3 -fvisibility=hidden -ffunction-sections -fdata-sections)
target_link_options(fleet_llm PRIVATE -Wl,--gc-sections -Wl,--exclude-libs,ALL)

endif()  # LLAMA_DIR

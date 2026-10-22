cmake_minimum_required(VERSION 3.18)
project(fleet_whisper CXX)

# 把 whisper.cpp 的全部源码静态链进这一个 .so。
#
# 为什么不按官方那样出 libwhisper.so + libggml*.so 四件套：
#   Android 的 System.loadLibrary 要自己保证依赖顺序（ggml-base → ggml →
#   ggml-cpu → whisper），少一个就 UnsatisfiedLinkError，而且报错只说"找不到符号"，
#   不说是谁没加载。合成一个 .so 后 Java 侧只有一行 loadLibrary，没有顺序问题。
#
# WHISPER_DIR 由 build.sh 传进来（whisper.cpp 源码目录，含 build-static 产出的 .a）。

if(NOT DEFINED WHISPER_DIR)
    message(FATAL_ERROR "需要 -DWHISPER_DIR=<whisper.cpp 源码目录>")
endif()

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

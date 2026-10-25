cmake_minimum_required(VERSION 3.18)
# 顶层显式开 C：ggml 里混着 C 源（老工程踩过「No known features for C compiler」）
project(fleet_llama C CXX)

# llama.cpp 源码目录由 build-llama.sh 用 -DLLAMA_SRC_DIR= 传入；给个默认值兜底
if(NOT DEFINED LLAMA_SRC_DIR)
    set(LLAMA_SRC_DIR "G:/dev/llama.cpp-fork")
endif()
message(STATUS "fleet_llama: LLAMA_SRC_DIR=${LLAMA_SRC_DIR}")

# 只留库，别把上游 examples/tests/tools 拖进来；首版纯 CPU（不开 Vulkan）
set(LLAMA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TESTS   OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TOOLS   OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_COMMON  ON  CACHE BOOL "" FORCE)
set(LLAMA_CURL          OFF CACHE BOOL "" FORCE)
set(BUILD_SHARED_LIBS   OFF CACHE BOOL "" FORCE)
set(GGML_NATIVE         OFF CACHE BOOL "" FORCE)
set(GGML_OPENMP         OFF CACHE BOOL "" FORCE)
set(GGML_VULKAN         OFF CACHE BOOL "" FORCE)

# EXCLUDE_FROM_ALL：llama.cpp 自带的 examples/tests 不跟着我们的 .so 一起编
add_subdirectory(${LLAMA_SRC_DIR} ${CMAKE_CURRENT_BINARY_DIR}/llama.cpp EXCLUDE_FROM_ALL)

# 薄 JNI + 上游静态链成单 .so（同 fleet_whisper / fleet_sd 套路）：
# Java 侧只 loadLibrary("fleet_llama")，无 .so 加载顺序问题。
add_library(fleet_llama SHARED llama_jni.cpp)

target_include_directories(fleet_llama PRIVATE
    "${LLAMA_SRC_DIR}/include"
    "${LLAMA_SRC_DIR}/ggml/include")

target_link_libraries(fleet_llama PRIVATE
    llama
    ggml
    android
    log)

target_compile_options(fleet_llama PRIVATE -O3 -fvisibility=hidden -ffunction-sections -fdata-sections)
target_link_options(fleet_llama PRIVATE -Wl,--gc-sections -Wl,--exclude-libs,ALL)

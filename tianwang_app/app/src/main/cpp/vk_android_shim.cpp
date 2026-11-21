// Android Vulkan 1.1 符号补齐层（2026-11-15）
//
// 背景：NDK 编译期用的 libvulkan.so stub 只导出 Vulkan 1.0 核心符号，
// vkGetPhysicalDeviceFeatures2 这类 1.1 的 instance 级函数必须由
// vkGetInstanceProcAddr 动态解析。llama.cpp 的 ggml-vulkan 绝大多数调用
// 都走了 Vulkan-Hpp 的 DispatchLoaderDynamic，但仍有 3 处直接调用
// vkGetPhysicalDeviceFeatures2（ggml-vulkan.cpp:4509 / 5146 / 16478），
// 交叉编译到 Android 时 --no-undefined 会报 undefined symbol。
//
// 运行时加载的是系统 /system/lib64/libvulkan.so（全功能 loader，导出 1.1 符号），
// 缺的只是链接期那张 stub 表。这里补一个同名转发函数：不改上游一行源码，
// 链接期给符号，运行期把调用转给真实 loader。
//
// 用 dlopen("libvulkan.so") 明确取系统库句柄，而不是 dlsym(RTLD_DEFAULT, ...)：
// 后者可能命中本 .so 里这个同名 shim 自己，造成无限递归。

#include <vulkan/vulkan.h>

#include <dlfcn.h>
#include <cstring>
#include <android/log.h>

namespace {

using Features2Fn = void (VKAPI_ATTR *)(VkPhysicalDevice, VkPhysicalDeviceFeatures2 *);

Features2Fn resolve_features2() {
    void *h = dlopen("libvulkan.so", RTLD_NOW | RTLD_LOCAL);
    if (!h) {
        __android_log_print(ANDROID_LOG_WARN, "FleetVkShim",
                            "dlopen(libvulkan.so) 失败：%s", dlerror());
        return nullptr;
    }
    // 首选：系统 loader 直接导出的 1.1 实现
    if (auto p = reinterpret_cast<Features2Fn>(dlsym(h, "vkGetPhysicalDeviceFeatures2"))) {
        __android_log_print(ANDROID_LOG_INFO, "FleetVkShim",
                            "vkGetPhysicalDeviceFeatures2 由系统 libvulkan.so 直接导出");
        return p;
    }
    // 次选：标准动态解析路径（loader 会返回可用的 trampoline）
    if (auto gipa = reinterpret_cast<PFN_vkGetInstanceProcAddr>(
            dlsym(h, "vkGetInstanceProcAddr"))) {
        if (auto p = reinterpret_cast<Features2Fn>(
                gipa(VK_NULL_HANDLE, "vkGetPhysicalDeviceFeatures2"))) {
            __android_log_print(ANDROID_LOG_INFO, "FleetVkShim",
                                "vkGetPhysicalDeviceFeatures2 经 vkGetInstanceProcAddr 解析成功");
            return p;
        }
        __android_log_print(ANDROID_LOG_WARN, "FleetVkShim",
                            "vkGetInstanceProcAddr 未返回 vkGetPhysicalDeviceFeatures2");
    } else {
        __android_log_print(ANDROID_LOG_WARN, "FleetVkShim",
                            "系统 loader 里也找不到 vkGetInstanceProcAddr");
    }
    __android_log_print(ANDROID_LOG_WARN, "FleetVkShim",
                        "系统 Vulkan loader 没有 vkGetPhysicalDeviceFeatures2，设备能力按不支持处理");
    return nullptr;
}

} // namespace

extern "C" VKAPI_ATTR void VKAPI_CALL
vkGetPhysicalDeviceFeatures2(VkPhysicalDevice physicalDevice,
                             VkPhysicalDeviceFeatures2 *pFeatures) {
    static Features2Fn real = resolve_features2();
    if (real) {
        real(physicalDevice, pFeatures);
        return;
    }
    // 兜底：把能力位清零，别让上游读到未初始化的栈内存而误判设备支持
    // cooperative matrix / shaderInt8 之类，进而走进跑不起来的着色器路径。
    if (pFeatures) {
        memset(&pFeatures->features, 0, sizeof(pFeatures->features));
    }
}

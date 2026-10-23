/* 最小 Vulkan 枚举：只看「Termux 的 loader 能不能摸到 Mali 驱动」。
 * 不渲染、不算，纯粹 create instance → enumerate physical device。
 * 这是编 llama.cpp Vulkan 后端之前必须先过的一关：
 * 过不了就说明问题在驱动/ABI，不在 llama.cpp。 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <vulkan/vulkan.h>

int main(void) {
    VkApplicationInfo ai = {0};
    ai.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
    ai.pApplicationName = "vkprobe";
    ai.apiVersion = VK_API_VERSION_1_1;

    VkInstanceCreateInfo ci = {0};
    ci.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
    ci.pApplicationInfo = &ai;

    VkInstance inst = VK_NULL_HANDLE;
    VkResult r = vkCreateInstance(&ci, NULL, &inst);
    if (r != VK_SUCCESS) { printf("CREATE_INSTANCE_FAIL %d\n", r); return 1; }

    uint32_t n = 0;
    vkEnumeratePhysicalDevices(inst, &n, NULL);
    printf("physical_devices=%u\n", n);
    if (!n) return 2;

    VkPhysicalDevice *pd = calloc(n, sizeof(*pd));
    vkEnumeratePhysicalDevices(inst, &n, pd);
    for (uint32_t i = 0; i < n; i++) {
        VkPhysicalDeviceProperties p;
        vkGetPhysicalDeviceProperties(pd[i], &p);
        printf("dev[%u] name=%s type=%d api=%u.%u.%u driver=%u\n", i, p.deviceName,
               p.deviceType, VK_VERSION_MAJOR(p.apiVersion),
               VK_VERSION_MINOR(p.apiVersion), VK_VERSION_PATCH(p.apiVersion),
               p.driverVersion);
        printf("        maxWorkgroupInv=%u maxComputeShared=%u\n",
               p.limits.maxComputeWorkGroupInvocations,
               p.limits.maxComputeSharedMemorySize);
        /* 只打最稳的两个 limit：Termux 的 vulkan-headers 裁得比较狠，
         * maxStorageBufferRange / maxMemoryAllocationSize 这些成员不一定在，
         * 而「最大工作组线程数 + 共享内存」才是判断 compute 能不能跑大矩阵的核心。 */
        printf("        maxStorageBufRange=%u MB\n",
               (unsigned)(p.limits.maxStorageBufferRange >> 20));

        uint32_t en = 0;
        vkEnumerateDeviceExtensionProperties(pd[i], NULL, &en, NULL);
        VkExtensionProperties *ep = calloc(en, sizeof(*ep));
        vkEnumerateDeviceExtensionProperties(pd[i], NULL, &en, ep);
        const char *want[] = {"VK_KHR_cooperative_matrix", "VK_KHR_16bit_storage",
                              "VK_KHR_8bit_storage", "VK_KHR_shader_float16_int8",
                              "VK_EXT_subgroup_size_control", "VK_KHR_buffer_device_address"};
        for (int w = 0; w < 6; w++) {
            int hit = 0;
            for (uint32_t e = 0; e < en; e++)
                if (!strcmp(ep[e].extensionName, want[w])) hit = 1;
            printf("        ext %-36s %s\n", want[w], hit ? "YES" : "no");
        }
        printf("        extensions_total=%u\n", en);
        free(ep);
    }
    vkDestroyInstance(inst, NULL);
    return 0;
}

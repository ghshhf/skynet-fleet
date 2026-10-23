// 编队 · 手机本地出图的 JNI 薄封装（stable-diffusion.cpp）。
//
// 照 whisper_jni.cpp 的规矩：只做「装模型 / 跑推理 / 回结果」三件事，
// 不在这里做业务判断——参数校验、尺寸白名单、确认闸都在 Java 侧，
// 出问题时必须在 UI 上看得见原因，埋在 C++ 里等于看不见。
//
// 与 whisper 的区别只有两点：
//   1. 推理以分钟计，必须有进度回调，否则 UI 像卡死；
//   2. 结果是 RGB 位图，PNG 编码在这里做（用 NDK 自带的 zlib，
//      不引第三方头文件，少一个 vendoring 维护面）。
//
// 诚实边界：这个文件不判断「Vulkan 到底用上没」。它只把
// sd_get_system_info() 的原文交回去（里面有 Vulkan 设备计数），
// 用没用上由调用方展示、由验收记录说话，不在这里假装。

#include <jni.h>
#include <android/log.h>
#include <zlib.h>

#include <cstdarg>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "stable-diffusion.h"

#define LOG_TAG "fleet-sd"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN, LOG_TAG, __VA_ARGS__)

// ---------------------------------------------------------------- 工具

static JavaVM *g_vm = nullptr;

static std::string g_last_error;
static std::string g_used_backend = "(未加载)";   // 实际生效的后端，出问题时一眼看清
static jobject g_progress_listener = nullptr;   // 全局引用，随进程
static jmethodID g_progress_method = nullptr;

static void set_error(const char *fmt, ...) {
    char buf[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    g_last_error = buf;
    LOGE("%s", buf);
}

// Java 侧用 int 传采样方法（避免 JNI 枚举/包名的耦合），这里做白名单映射：
// 越界一律回 EULER_A，且在日志里记下来——静默换方法会让"为什么画成这样"查不出来。
static sample_method_t to_sample_method(int v) {
    switch (v) {
        case 0:  return EULER_SAMPLE_METHOD;
        case 2:  return HEUN_SAMPLE_METHOD;
        case 3:  return DPM2_SAMPLE_METHOD;
        case 4:  return DPMPP2S_A_SAMPLE_METHOD;
        case 5:  return DPMPP2M_SAMPLE_METHOD;
        case 6:  return DPMPP2Mv2_SAMPLE_METHOD;
        case 7:  return IPNDM_SAMPLE_METHOD;
        case 8:  return IPNDM_V_SAMPLE_METHOD;
        case 9:  return LCM_SAMPLE_METHOD;          // SD-Turbo / LCM 用这个
        case 10: return DDIM_TRAILING_SAMPLE_METHOD;
        case 11: return TCD_SAMPLE_METHOD;
        case 1:  return EULER_A_SAMPLE_METHOD;      // 默认档
        default:
            LOGW("未知采样方法 %d，回退 EULER_A", v);
            return EULER_A_SAMPLE_METHOD;
    }
}

static const char *sample_method_name(sample_method_t m) {
    switch (m) {
        case EULER_SAMPLE_METHOD:            return "EULER";
        case EULER_A_SAMPLE_METHOD:          return "EULER_A";
        case HEUN_SAMPLE_METHOD:             return "HEUN";
        case DPM2_SAMPLE_METHOD:             return "DPM2";
        case DPMPP2S_A_SAMPLE_METHOD:        return "DPMPP2S_A";
        case DPMPP2M_SAMPLE_METHOD:          return "DPMPP2M";
        case DPMPP2Mv2_SAMPLE_METHOD:        return "DPMPP2Mv2";
        case IPNDM_SAMPLE_METHOD:            return "IPNDM";
        case IPNDM_V_SAMPLE_METHOD:          return "IPNDM_V";
        case LCM_SAMPLE_METHOD:              return "LCM";
        case DDIM_TRAILING_SAMPLE_METHOD:    return "DDIM_TRAILING";
        case TCD_SAMPLE_METHOD:              return "TCD";
        default:                             return "?";
    }
}

// 进度回调：generate_image 在调用线程上同步触发，AttachCurrentThread
// 拿到的就是调用方那个 JNIEnv，不会新建附着。
// 分阶段计时的依据（实测出来的规律，不是文档写的）：
// sd.cpp 的进度回调里，去噪段 step 递增到某个上限后，VAE 段会**从 0 重新开始**计数。
// 所以"step 比上一次小"= 换段了。没有这个判断就只能拿到一个总时间，
// 而 512² 上一张图里 VAE 单独能吃掉 134 秒——不分段就永远不知道慢在哪。
static int g_last_step = -1;
static long long g_denoise_ms = -1;      // 去噪段耗时，-1 = 还没切段
static long long g_gen_t0 = 0;

static long long now_ms() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

/**
 * 把 sd.cpp / ggml 自己的日志接进 logcat。
 *
 * 没有这个回调时我们是瞎的：sd.cpp 默认往 stderr 打，而 Android 上 stderr
 * 根本不进 logcat。像「某个算子 Vulkan 不支持、回退 CPU」这种决定性能的信息
 * 全在那些日志里——看不到就只能靠猜，而猜已经错过一次（以为 Vulkan 用上了）。
 * DEBUG 级丢弃：加载一次几百行，会把有用的 INFO 冲掉。
 */
static void sd_log_to_logcat(sd_log_level_t level, const char *text, void *) {
    if (text == nullptr) return;
    if (level == SD_LOG_DEBUG) return;
    int prio = ANDROID_LOG_INFO;
    if (level == SD_LOG_WARN) prio = ANDROID_LOG_WARN;
    else if (level == SD_LOG_ERROR) prio = ANDROID_LOG_ERROR;
    // 按行切：__android_log_print 对超长单行会截断，而 sd.cpp 常一次给一大段
    const char *p = text;
    while (*p != '\0') {
        const char *nl = strchr(p, '\n');
        if (nl == nullptr) {
            __android_log_print(prio, LOG_TAG, "%s", p);
            break;
        }
        size_t n = (size_t)(nl - p);
        if (n > 0) {
            std::string line(p, n);
            __android_log_print(prio, LOG_TAG, "%s", line.c_str());
        }
        p = nl + 1;
    }
}

static void sd_progress_trampoline(int step, int steps, float time, void *data) {
    (void)data;
    // time 是 sd.cpp 给的累计耗时，单位未文档化，先原样打出来再用实测校准。
    // 没有这条日志就只能靠人肉眼盯屏幕数进度——真机调优不能这么干。
    LOGI("step %d/%d  time=%.1f", step, steps, time);
    // 每次回退都刷新：实测一次生成里有三段回调（例：686 步 → 2 步 → 140 步），
    // 最后一段才是 VAE，所以"最后一次回退"才是 VAE 的起点。只记第一次会把
    // 真正的去噪段算进 VAE（实测会得出"去噪 3.5s"这种明显错的数）。
    if (g_last_step >= 0 && step < g_last_step) {
        g_denoise_ms = now_ms() - g_gen_t0;
        LOGI("分段：%.1fs 处换段（step %d→%d）", g_denoise_ms / 1000.0, g_last_step, step);
    }
    g_last_step = step;
    if (g_progress_listener == nullptr || g_progress_method == nullptr) return;
    JNIEnv *env = nullptr;
    if (g_vm->AttachCurrentThread(&env, nullptr) != JNI_OK || env == nullptr) return;
    env->CallVoidMethod(g_progress_listener, g_progress_method, (jint)step, (jint)steps);
}

// ---------------------------------------------------------------- PNG 编码
// 手写最小 PNG：8bit、RGB(2)/RGBA(6)、单 IDAT、filter 0。
// sd_image_t 给的是裸位图，不编码 Java 侧还得再过一遍，没必要。

static void put_u32be(std::vector<uint8_t> &v, uint32_t x) {
    v.push_back((uint8_t)(x >> 24));
    v.push_back((uint8_t)(x >> 16));
    v.push_back((uint8_t)(x >> 8));
    v.push_back((uint8_t)x);
}

static void png_chunk(std::vector<uint8_t> &out, const char *type,
                      const uint8_t *data, size_t len) {
    put_u32be(out, (uint32_t)len);
    size_t crc_start = out.size();
    out.insert(out.end(), type, type + 4);
    if (len > 0 && data != nullptr) out.insert(out.end(), data, data + len);
    uLong crc = crc32(0L, Z_NULL, 0);
    crc = crc32(crc, out.data() + crc_start, (uInt)(4 + len));
    put_u32be(out, (uint32_t)crc);
}

// 失败返回空 vector（调用方转 null + 读 lastError）
static std::vector<uint8_t> encode_png(const sd_image_t &img) {
    static const uint8_t SIG[8] = {0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'};
    if (img.data == nullptr || img.width == 0 || img.height == 0) return {};
    if (img.channel != 3 && img.channel != 4) return {};

    uint8_t color_type = img.channel == 4 ? 6 : 2;
    size_t bpp = img.channel;
    size_t row_bytes = (size_t)img.width * bpp;

    // 原始数据 = 每行前面加一个 filter 字节 0
    std::vector<uint8_t> raw;
    raw.reserve((row_bytes + 1) * img.height);
    for (uint32_t y = 0; y < img.height; y++) {
        raw.push_back(0);
        const uint8_t *row = img.data + (size_t)y * row_bytes;
        raw.insert(raw.end(), row, row + row_bytes);
    }

    // zlib 压缩（level 6：出图不是热路径，压小点省流量）
    uLongf bound = compressBound((uLong)raw.size());
    std::vector<uint8_t> idat(bound);
    if (compress2(idat.data(), &bound, raw.data(), (uLong)raw.size(), 6) != Z_OK) return {};
    idat.resize(bound);

    uint8_t ihdr[13];
    ihdr[0] = (uint8_t)(img.width >> 24);  ihdr[1] = (uint8_t)(img.width >> 16);
    ihdr[2] = (uint8_t)(img.width >> 8);   ihdr[3] = (uint8_t)(img.width);
    ihdr[4] = (uint8_t)(img.height >> 24); ihdr[5] = (uint8_t)(img.height >> 16);
    ihdr[6] = (uint8_t)(img.height >> 8);  ihdr[7] = (uint8_t)(img.height);
    ihdr[8] = 8;            // bit depth
    ihdr[9] = color_type;   // 2=RGB 6=RGBA
    ihdr[10] = 0;           // compression
    ihdr[11] = 0;           // filter
    ihdr[12] = 0;           // interlace

    std::vector<uint8_t> out;
    out.reserve(64 + idat.size());
    out.insert(out.end(), SIG, SIG + 8);
    png_chunk(out, "IHDR", ihdr, sizeof(ihdr));
    png_chunk(out, "IDAT", idat.data(), idat.size());
    png_chunk(out, "IEND", nullptr, 0);
    return out;
}

// ---------------------------------------------------------------- JNI

extern "C" JNIEXPORT jint JNICALL
JNI_OnLoad(JavaVM *vm, void *reserved) {
    (void)reserved;
    g_vm = vm;
    // 越早越好：模型加载阶段的日志（算子支持情况、后端选择）都在 sd.cpp 自己的
    // 输出里，晚一步就漏掉最关键的那些行。
    sd_set_log_callback(sd_log_to_logcat, nullptr);
    return JNI_VERSION_1_6;
}

extern "C" {

JNIEXPORT jstring JNICALL
Java_app_skynet_tianwang_SdBridge_nativeSystemInfo(JNIEnv *env, jclass) {
    const char *info = sd_get_system_info();
    return env->NewStringUTF(info != nullptr ? info : "");
}

// 设备清单：一行一个 `名字<TAB>描述`，名字就是 backend 参数能填的值。
// 没有它我们只能猜「Vulkan 到底叫 Vulkan0 还是别的」——猜错就静默退回 CPU。
static std::string list_devices_str() {
    size_t need = sd_list_devices(nullptr, 0);
    if (need == 0) return std::string();
    std::string buf;
    buf.resize(need + 1, '\0');
    size_t got = sd_list_devices(&buf[0], buf.size());
    buf.resize(got < buf.size() ? got : buf.size());
    return buf;
}

static void log_devices() {
    std::string s = list_devices_str();
    LOGI("可用后端设备(%zu 字节)：\n%s", s.size(), s.c_str());
}

JNIEXPORT jstring JNICALL
Java_app_skynet_tianwang_SdBridge_nativeListDevices(JNIEnv *env, jclass) {
    return env->NewStringUTF(list_devices_str().c_str());
}

JNIEXPORT void JNICALL
Java_app_skynet_tianwang_SdBridge_nativeSetProgressListener(JNIEnv *env, jclass, jobject listener) {
    if (g_progress_listener != nullptr) {
        env->DeleteGlobalRef(g_progress_listener);
        g_progress_listener = nullptr;
        g_progress_method = nullptr;
    }
    if (listener == nullptr) return;
    jclass cls = env->GetObjectClass(listener);
    if (cls == nullptr) return;
    g_progress_method = env->GetMethodID(cls, "onProgress", "(II)V");
    env->DeleteLocalRef(cls);
    if (g_progress_method == nullptr) {
        set_error("进度监听器没有 onProgress(int,int) 方法");
        return;
    }
    g_progress_listener = env->NewGlobalRef(listener);
    sd_set_progress_callback(sd_progress_trampoline, nullptr);
}

JNIEXPORT jlong JNICALL
Java_app_skynet_tianwang_SdBridge_nativeInit(JNIEnv *env, jclass, jstring jpath,
                                       jint threads, jboolean vaeTiling, jstring jbackend,
                                       jstring jtaesd) {
    (void)vaeTiling;   // 见 nativeGenerate：tiling 是每次生成可配的，不是加载时可配的
    if (jpath == nullptr) { set_error("模型路径为空"); return 0; }
    const char *path = env->GetStringUTFChars(jpath, nullptr);
    if (path == nullptr) { set_error("模型路径取不到 UTF-8"); return 0; }
    const char *backend = nullptr;
    if (jbackend != nullptr) backend = env->GetStringUTFChars(jbackend, nullptr);
    if (backend == nullptr) backend = "";
    const char *taesdRaw = jtaesd != nullptr ? env->GetStringUTFChars(jtaesd, nullptr) : nullptr;
    const char *taesd = (taesdRaw != nullptr && taesdRaw[0] != '\0') ? taesdRaw : nullptr;

    // 每次加载都把环境打一遍：Vulkan 用没用上必须能事后查证，不能靠猜。
    const char *sysinfo = sd_get_system_info();
    LOGI("系统信息：%s", sysinfo != nullptr ? sysinfo : "(null)");
    log_devices();

    sd_ctx_params_t cp;
    sd_ctx_params_init(&cp);
    cp.model_path = path;
    cp.n_threads = threads > 0 ? threads : 4;
    // 实测（2026-10-23，天玑9400+/Mali-G925，512² SD1.5）：
    //   =true  → VAE 解码 134 秒，整张 10 步 472 秒；
    //   =false → 触发 sd_probe_conv_direct() 在 Vulkan 上逐个 shape 探测，
    //            **探测本身就吃掉 9 分半且仍未出图**（12.5 分钟没跑完一张 2 步的图）。
    // 原因见 src/pipeline/diffusion_engine.cpp 的 conv_direct_for()：写 true 会直接
    // return，跳过探测；留 false 就走探测分支，而 Vulkan 分支要建多张 probe graph。
    // ⇒ 结论：这里必须写 true。探测的代价远大于它想省下的那一点。
    cp.vae_conv_direct = true;
    cp.backend = backend[0] != '\0' ? backend : nullptr;
    cp.params_backend = nullptr;  // 权重跟主后端走
    // TAESD = 蒸馏过的小解码器。实测（2026-10-23）原 VAE 解码 512² 要 **134 秒**，
    // 占整张图的大头；TAESD 只有 9.3MB，代价是解码画质略软。有这个文件就用，
    // 没有就退回原 VAE——不做"静默降级"，日志里打清楚。
    cp.taesd_path = taesd;
    LOGI("taesd=%s", taesd != nullptr ? taesd : "(不用，走原 VAE)");

    LOGI("new_sd_ctx：backend=%s threads=%d", cp.backend ? cp.backend : "(默认/CPU)",
         (int)cp.n_threads);
    sd_ctx_t *ctx = new_sd_ctx(&cp);

    // 指定后端失败就退回默认（CPU）再来一次，而不是直接失败：
    // 出图是"能出"优先，慢可以接受，起不来才叫坏。
    if (ctx == nullptr && cp.backend != nullptr) {
        LOGE("backend=%s 加载失败，退回默认后端重试", cp.backend);
        cp.backend = nullptr;
        ctx = new_sd_ctx(&cp);
    }

    env->ReleaseStringUTFChars(jpath, path);
    if (jbackend != nullptr && backend != nullptr) env->ReleaseStringUTFChars(jbackend, backend);
    if (jtaesd != nullptr && taesdRaw != nullptr) env->ReleaseStringUTFChars(jtaesd, taesdRaw);
    if (ctx == nullptr) {
        set_error("sd 模型加载失败（文件损坏/量化格式不支持/内存不足），见 logcat fleet-sd");
        return 0;
    }
    g_used_backend = cp.backend ? cp.backend : "CPU(默认/退回)";
    LOGI("sd 模型已加载：%s（threads=%d backend=%s）", path, (int)cp.n_threads,
         g_used_backend.c_str());
    return reinterpret_cast<jlong>(ctx);
}

/**
 * prompt 必填、neg 可空串。width/height/steps/seed/cfg 的白名单在 Java 侧，
 * 这里只兜底负数。vaeTiling=true 时开 VAE 分块（低内存机型防 OOM）。
 * 返回 PNG 字节；失败返回 nullptr，原因在 nativeLastError()。
 */
JNIEXPORT jbyteArray JNICALL
Java_app_skynet_tianwang_SdBridge_nativeGenerate(JNIEnv *env, jclass, jlong handle,
                                           jstring jprompt, jstring jneg,
                                           jint width, jint height, jint steps,
                                           jlong seed, jfloat cfg, jboolean vaeTiling,
                                           jint sampleMethod) {
    sd_ctx_t *ctx = reinterpret_cast<sd_ctx_t *>(handle);
    if (ctx == nullptr) { set_error("上下文未初始化（模型没加载上）"); return nullptr; }
    if (width <= 0 || height <= 0 || steps <= 0) {
        set_error("参数非法：width/height/steps 必须为正");
        return nullptr;
    }
    // 队列必踩：cancel() 设的标记不会自己清，不复位的话被中断后的第二张
    // 会在开跑前就返回失败，表现是"队列里第一张以后全废"。每张开头清一次。
    sd_cancel_generation(ctx, SD_CANCEL_RESET);

    const char *prompt = jprompt == nullptr ? "" : env->GetStringUTFChars(jprompt, nullptr);
    const char *neg = jneg == nullptr ? "" : env->GetStringUTFChars(jneg, nullptr);
    if (prompt == nullptr || neg == nullptr) {
        if (jprompt != nullptr && prompt != nullptr) env->ReleaseStringUTFChars(jprompt, prompt);
        set_error("prompt/negative prompt 取不到 UTF-8");
        return nullptr;
    }

    sd_img_gen_params_t gp;
    sd_img_gen_params_init(&gp);
    gp.prompt = prompt;
    gp.negative_prompt = neg;
    gp.width = (uint32_t)width;
    gp.height = (uint32_t)height;
    gp.seed = (int64_t)seed;
    gp.batch_count = 1;
    sd_sample_params_init(&gp.sample_params);
    gp.sample_params.sample_steps = steps;
    // cfg <= 0 才用默认：SD-Turbo/LCM 这类"无 CFG"模型必须传 1.0，
    // 沿用 7.0 会把少步模型直接画糊。
    gp.sample_params.guidance.txt_cfg = cfg > 0 ? cfg : 7.0f;
    // 采样方法由上层按模型给：SD1.5 常规=EULER_A，LCM/Turbo=LCM。
    // 硬编码一种的话，少步模型永远出不了它该有的效果。
    gp.sample_params.sample_method = to_sample_method(sampleMethod);
    if (vaeTiling) {
        gp.vae_tiling_params.enabled = true;   // 尺寸留 0 = 库默认 256
    }

    sd_image_t *images = nullptr;
    int num_images = 0;
    g_last_step = -1;
    g_denoise_ms = -1;
    g_gen_t0 = now_ms();
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    LOGI("开始生成：%dx%d steps=%d cfg=%.1f sampler=%s backend=%s",
         width, height, steps, cfg, sample_method_name(gp.sample_params.sample_method),
         g_used_backend.c_str());
    bool ok = generate_image(ctx, &gp, &images, &num_images);
    clock_gettime(CLOCK_MONOTONIC, &t1);
    double secs = (double)(t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9;
    // 分不出段就如实说"未分段"，不要拿总时间除以步数假装那是 s/步。
    if (g_denoise_ms >= 0) {
        LOGI("生成结束：ok=%d 总 %.2fs = 去噪 %.1fs + VAE %.1fs（%.2fs/步）",
             (int)ok, secs, g_denoise_ms / 1000.0, secs - g_denoise_ms / 1000.0,
             g_denoise_ms / 1000.0 / (double)steps);
    } else {
        LOGI("生成结束：ok=%d 总 %.2fs（未分段：%.2fs/步 仅供参考）",
             (int)ok, secs, secs / (double)steps);
    }

    env->ReleaseStringUTFChars(jprompt, prompt);
    env->ReleaseStringUTFChars(jneg, neg);

    if (!ok || images == nullptr || num_images <= 0) {
        set_error("generate_image 失败（OOM/参数组合不被模型支持/被取消），见 logcat fleet-sd");
        return nullptr;
    }

    // 先把要用的信息抄出来，再释放 sd 侧位图（释放后不能再碰）
    uint32_t w = images[0].width, h = images[0].height;
    uint32_t ch = images[0].channel;
    std::vector<uint8_t> png = encode_png(images[0]);
    for (int i = 0; i < num_images; i++) {
        if (images[i].data != nullptr) free(images[i].data);
    }
    free(images);

    if (png.empty()) {
        set_error("PNG 编码失败（channel=%u，只支持 3/4）", (unsigned)ch);
        return nullptr;
    }

    jbyteArray out = env->NewByteArray((jsize)png.size());
    if (out == nullptr) { set_error("NewByteArray 失败"); return nullptr; }
    env->SetByteArrayRegion(out, 0, (jsize)png.size(), (const jbyte *)png.data());
    LOGI("出图完成：%ux%u ch=%u，PNG %zu 字节", (unsigned)w, (unsigned)h, (unsigned)ch, png.size());
    return out;
}

JNIEXPORT void JNICALL
Java_app_skynet_tianwang_SdBridge_nativeCancel(JNIEnv *, jclass, jlong handle) {
    sd_ctx_t *ctx = reinterpret_cast<sd_ctx_t *>(handle);
    if (ctx != nullptr) sd_cancel_generation(ctx, SD_CANCEL_ALL);
}

JNIEXPORT void JNICALL
Java_app_skynet_tianwang_SdBridge_nativeFree(JNIEnv *, jclass, jlong handle) {
    sd_ctx_t *ctx = reinterpret_cast<sd_ctx_t *>(handle);
    if (ctx != nullptr) {
        free_sd_ctx(ctx);
        LOGI("sd 模型已释放");
    }
}

JNIEXPORT jstring JNICALL
Java_app_skynet_tianwang_SdBridge_nativeLastError(JNIEnv *env, jclass) {
    return env->NewStringUTF(g_last_error.c_str());
}

}  // extern "C"

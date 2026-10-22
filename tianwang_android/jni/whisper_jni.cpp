// 编队 · 手机本地语音识别的 JNI 薄封装。
//
// 只做三件事：把模型装进内存、把 PCM 交给 whisper、把分段文字拼回来。
// 刻意不在这里做任何业务判断（不判空、不猜语言、不重试）——
// 那些出问题时必须在 Java 侧能看见原因，埋在 C++ 里就等于看不见。
//
// 为什么砍掉 GPU：K80 Ultra 是 Adreno，走 Vulkan 需要额外后端且各家驱动版本参差，
// 而 whisper base/small 在 arm64 CPU（NEON）上已经够快。先要能用，再谈更快。

#include <jni.h>
#include <android/log.h>
#include <cstring>
#include <string>
#include <vector>

#include "whisper.h"

#define LOG_TAG "fleet-whisper"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

extern "C" {

JNIEXPORT jstring JNICALL
Java_app_skynet_tianwang_Whisper_nativeVersion(JNIEnv *env, jclass) {
    return env->NewStringUTF(whisper_version());
}

JNIEXPORT jlong JNICALL
Java_app_skynet_tianwang_Whisper_nativeLoad(JNIEnv *env, jclass, jstring jpath) {
    if (jpath == nullptr) return 0;
    const char *path = env->GetStringUTFChars(jpath, nullptr);
    if (path == nullptr) return 0;

    whisper_context_params cp = whisper_context_default_params();
    cp.use_gpu = false;              // 见文件头：先保证在所有机器上都能跑
    whisper_context *ctx = whisper_init_from_file_with_params(path, cp);
    if (ctx == nullptr) {
        LOGE("模型装不进来：%s", path);
    } else {
        LOGI("模型已加载：%s", path);
    }
    env->ReleaseStringUTFChars(jpath, path);
    return reinterpret_cast<jlong>(ctx);
}

/**
 * pcm: 16000Hz / 单声道 / 16bit 小端，和 WavRecorder 的输出一致。
 * 返回：识别出的整段文字（已按分段拼好、去掉首尾空白）；失败返回空串。
 *
 * 为什么返回空串而不抛异常：调用方是 UI 线程外的工作线程，
 * 这里抛 Throwable 会跨 JNI 边界丢失栈，反而不如让它拿空串 + 自己去打日志。
 */
JNIEXPORT jstring JNICALL
Java_app_skynet_tianwang_Whisper_nativeTranscribe(JNIEnv *env, jclass,
                                                  jlong handle, jshortArray jpcm,
                                                  jint nThreads, jstring jlang) {
    whisper_context *ctx = reinterpret_cast<whisper_context *>(handle);
    if (ctx == nullptr || jpcm == nullptr) return env->NewStringUTF("");

    jsize n = env->GetArrayLength(jpcm);
    if (n <= 0) return env->NewStringUTF("");

    // short[] → float[-1,1]。whisper 只吃 float，且这个换算不能省：
    // 直接把 short 当 float 用会得到 30000 量级的数，模型只会输出乱码。
    std::vector<float> pcm(n);
    jshort *raw = env->GetShortArrayElements(jpcm, nullptr);
    if (raw == nullptr) return env->NewStringUTF("");
    for (jsize i = 0; i < n; i++) {
        pcm[i] = static_cast<float>(raw[i]) / 32768.0f;
    }
    env->ReleaseShortArrayElements(jpcm, raw, JNI_ABORT);

    whisper_full_params p = whisper_full_default_params(WHISPER_SAMPLING_GREEDY);
    p.print_progress = false;
    p.print_realtime = false;
    p.print_timestamps = false;
    p.print_special = false;
    p.translate = false;             // 要的是原文，不是英文翻译
    p.no_context = true;             // 每次独立识别，别让上一句污染这一句
    p.single_segment = false;        // 长句要分段，拼起来才是完整一句
    p.suppress_blank = true;
    p.n_threads = nThreads > 0 ? nThreads : 4;

    const char *lang = jlang == nullptr ? nullptr : env->GetStringUTFChars(jlang, nullptr);
    if (lang != nullptr && lang[0] != '\0') {
        p.language = lang;           // nullptr 或 "" 表示自动检测
    } else {
        p.language = nullptr;
    }

    int rc = whisper_full(ctx, p, pcm.data(), static_cast<int>(pcm.size()));
    if (lang != nullptr) env->ReleaseStringUTFChars(jlang, lang);

    if (rc != 0) {
        LOGE("whisper_full 失败 rc=%d", rc);
        return env->NewStringUTF("");
    }

    std::string out;
    int segs = whisper_full_n_segments(ctx);
    for (int i = 0; i < segs; i++) {
        const char *t = whisper_full_get_segment_text(ctx, i);
        if (t != nullptr) out += t;
    }

    // 去掉首尾空白（whisper 常在句首补一个空格）
    size_t b = out.find_first_not_of(" \t\r\n");
    size_t e = out.find_last_not_of(" \t\r\n");
    out = (b == std::string::npos) ? std::string() : out.substr(b, e - b + 1);

    return env->NewStringUTF(out.c_str());
}

JNIEXPORT void JNICALL
Java_app_skynet_tianwang_Whisper_nativeFree(JNIEnv *, jclass, jlong handle) {
    whisper_context *ctx = reinterpret_cast<whisper_context *>(handle);
    if (ctx != nullptr) {
        whisper_free(ctx);
        LOGI("模型已释放");
    }
}

}  // extern "C"

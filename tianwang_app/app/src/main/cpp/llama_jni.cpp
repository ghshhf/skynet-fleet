// 天网 · 本地推理 JNI 桥（薄封装，静态链 llama.cpp 单 .so）
//
// 🔴 为什么「原生 + 集成」而不是借别人 app / 外部 server：
//   用户要的是「一个 APP 解决所有，未来好升级」。所以 llama.cpp 直接编进
//   天网 app（arm64-v8a 单 .so），Java 侧只一行 loadLibrary，没有 .so
//   加载顺序问题（沿用老工程 fleet_whisper / fleet_sd 的薄 JNI + 静态链套路）。
//
// 首版纯 CPU（n_gpu_layers=0）：天玑 9400 跑 4B Q4 够用，且不引入 Vulkan
// 着色器交叉编译的复杂度。GPU 卸载留到下一版（GGML_VULKAN=ON）。
#include <jni.h>
#include <string>
#include <vector>
#include <android/log.h>

#include "llama.h"

#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  "LlamaJNI", __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, "LlamaJNI", __VA_ARGS__)

struct LlamaSession {
    llama_model*   model = nullptr;
    llama_context* ctx   = nullptr;
    llama_sampler* smpl  = nullptr;
};

static std::string jstr2std(JNIEnv* env, jstring js) {
    if (!js) return std::string();
    const char* p = env->GetStringUTFChars(js, nullptr);
    std::string s = p ? p : "";
    if (p) env->ReleaseStringUTFChars(js, p);
    return s;
}

extern "C" {

// 返回会话句柄（jlong），0 = 失败
JNIEXPORT jlong JNICALL
Java_app_skynet_tianwang_next_LlamaNative_nativeLoadModel(
        JNIEnv* env, jobject /*thiz*/, jstring jpath, jint nCtx, jint nThreads) {
    std::string path = jstr2std(env, jpath);
    auto* s = new LlamaSession();

    llama_model_params mp = llama_model_default_params();
    mp.n_gpu_layers = 0;                       // 首版纯 CPU
    s->model = llama_model_load_from_file(path.c_str(), mp);
    if (!s->model) { LOGE("模型加载失败: %s", path.c_str()); delete s; return 0; }

    llama_context_params cp = llama_context_default_params();
    cp.n_ctx         = (uint32_t)(nCtx > 0 ? nCtx : 4096);
    cp.n_threads     = nThreads > 0 ? nThreads : 4;
    cp.n_threads_batch = cp.n_threads;
    s->ctx = llama_init_from_model(s->model, cp);
    if (!s->ctx) { LOGE("上下文创建失败"); llama_model_free(s->model); delete s; return 0; }

    auto sp = llama_sampler_chain_default_params();
    s->smpl = llama_sampler_chain_init(sp);
    llama_sampler_chain_add(s->smpl, llama_sampler_init_temp(0.7f));
    llama_sampler_chain_add(s->smpl, llama_sampler_init_top_k(40));
    llama_sampler_chain_add(s->smpl, llama_sampler_init_top_p(0.9f, 1));
    llama_sampler_chain_add(s->smpl, llama_sampler_init_dist(0x5EED1234u));

    LOGI("模型就绪: %s (ctx=%d threads=%d)", path.c_str(), (int)cp.n_ctx, cp.n_threads);
    return reinterpret_cast<jlong>(s);
}

JNIEXPORT void JNICALL
Java_app_skynet_tianwang_next_LlamaNative_nativeFreeModel(
        JNIEnv* /*env*/, jobject, jlong handle) {
    auto* s = reinterpret_cast<LlamaSession*>(handle);
    if (!s) return;
    if (s->smpl)  llama_sampler_free(s->smpl);
    if (s->ctx)   llama_free(s->ctx);
    if (s->model) llama_model_free(s->model);
    delete s;
}

// callback 需实现 void onDelta(String)
// roles/contents 等长，按顺序就是多轮对话；模板由 llama_chat_apply_template 统一套，
// Kotlin 侧只负责把 ChatMsg 翻成 (role, content) 对，不自己拼模板。
JNIEXPORT jstring JNICALL
Java_app_skynet_tianwang_next_LlamaNative_nativeChatMessages(
        JNIEnv* env, jobject /*thiz*/, jlong handle, jobjectArray jroles, jobjectArray jcontents,
        jint maxTokens, jobject callback) {
    auto* s = reinterpret_cast<LlamaSession*>(handle);
    if (!s || !s->ctx || !s->model) return env->NewStringUTF("");
    LOGI("[1] 进入 chat handle=%p", (void*) s);

    jsize n = env->GetArrayLength(jroles);
    if (n <= 0) return env->NewStringUTF("");
    LOGI("[2] 消息数=%d", (int) n);
    std::vector<llama_chat_message> msgs;
    msgs.reserve((size_t)n);
    std::vector<std::string> keep;   // 托底字符串；reserve(n*2) 后不扩容，c_str() 指针全程有效
    keep.reserve((size_t)n * 2);
    for (jsize i = 0; i < n; ++i) {
        jstring jr = (jstring) env->GetObjectArrayElement(jroles, i);
        jstring jc = (jstring) env->GetObjectArrayElement(jcontents, i);
        keep.push_back(jstr2std(env, jr));
        keep.push_back(jstr2std(env, jc));
        if (jr) env->DeleteLocalRef(jr);
        if (jc) env->DeleteLocalRef(jc);
        llama_chat_message m;
        m.role    = keep[keep.size() - 2].c_str();
        m.content = keep[keep.size() - 1].c_str();
        msgs.push_back(m);
    }

    const llama_vocab* vocab = llama_model_get_vocab(s->model);

    LOGI("[3] 清 KV…");
    llama_memory_clear(llama_get_memory(s->ctx), true);   // 每轮前清 KV，历史由 prompt 全量重放
    LOGI("[4] KV 已清");

    // 模型没内置模板时 tmpl 是 nullptr，直接丢给 llama_chat_apply_template
    // 有可能解引用空指针（Qwen3 自带模板，但别的 GGUF 不一定），所以先判。
    const char* tmpl = llama_model_chat_template(s->model, nullptr);
    LOGI("[5] tmpl=%s", tmpl ? "有" : "无");
    std::string prompt;
    if (tmpl) {
        std::vector<char> buf(24576);
        int need = llama_chat_apply_template(tmpl, msgs.data(), n, true, buf.data(), (int32_t)buf.size());
        if (need >= (int)buf.size()) {
            buf.resize((size_t)need + 1);
            need = llama_chat_apply_template(tmpl, msgs.data(), n, true, buf.data(), (int32_t)buf.size());
        }
        if (need > 0) prompt.assign(buf.data(), (size_t)need);
    }
    if (prompt.empty()) {
        // 回退：裸拼（不假装有模板）
        for (auto& m : msgs) { prompt += m.role; prompt += ": "; prompt += m.content; prompt += "\n"; }
        prompt += "assistant: ";
    }

    int np = -llama_tokenize(vocab, prompt.c_str(), (int)prompt.size(), nullptr, 0, true, true);
    LOGI("[6] prompt=%zu 字节 / %d token", prompt.size(), np);
    if (np <= 0) return env->NewStringUTF("");
    std::vector<llama_token> toks((size_t)np);
    if (llama_tokenize(vocab, prompt.c_str(), (int)prompt.size(), toks.data(), np, true, true) < 0)
        return env->NewStringUTF("");

    jclass cbCls   = callback ? env->GetObjectClass(callback) : nullptr;
    jmethodID onDelta = cbCls ? env->GetMethodID(cbCls, "onDelta", "(Ljava/lang/String;)V") : nullptr;
    LOGI("[7] 回调=%s", onDelta ? "已绑定" : "没有（不影响出字）");

    std::string out;
    std::string pending;                 // 未凑齐完整 UTF-8 的字节尾巴
    char piece[512];
    llama_token id = 0;                  // 🔴 必须在循环外：下面的 batch 只存指针

    // 🔴 `llama_batch_get_one` 返回的 batch **不拥有内存**（token 指向外部），
    // 所以绝不能 `llama_batch_free` —— 它会 free 掉 vector 数据/栈变量，
    // 造成堆破坏、当场 SIGSEGV（2026-10-25 实测崩在 nativeChatMessages）。
    // 官方 simple-chat 从头到尾没调过 free，就是因为它不需要。
    llama_batch batch = llama_batch_get_one(toks.data(), (int32_t)np);
    int generated = 0;
    while (generated < maxTokens) {
        const int n_ctx  = (int) llama_n_ctx(s->ctx);
        const int n_used = (int) llama_memory_seq_pos_max(llama_get_memory(s->ctx), 0) + 1;
        if (n_used + batch.n_tokens > n_ctx) { LOGI("上下文满了（used=%d batch=%d ctx=%d）", n_used, batch.n_tokens, n_ctx); break; }

        if (generated == 0) LOGI("[8] decode prompt（%d token）…", (int) batch.n_tokens);
        if (llama_decode(s->ctx, batch) != 0) { LOGE("decode 失败"); break; }
        if (generated == 0) LOGI("[9] prompt 已解码，开始采样");

        id = llama_sampler_sample(s->smpl, s->ctx, -1);
        llama_sampler_accept(s->smpl, id);
        if (llama_vocab_is_eog(vocab, id)) break;

        int pn = llama_token_to_piece(vocab, id, piece, sizeof(piece), 0, false);
        if (pn > 0) {
            pending.append(piece, (size_t)pn);

            // 只回调到最后一个完整 UTF-8 字符边界，防中文被 BPE 拆半字节糊屏
            size_t emit = 0, k = 0;
            while (k < pending.size()) {
                unsigned char c = (unsigned char) pending[k];
                size_t len = (c >= 0xF0) ? 4 : (c >= 0xE0) ? 3 : (c >= 0xC0) ? 2 : 1;
                if (k + len > pending.size()) break;
                k += len; emit = k;
            }
            if (emit > 0) {
                std::string chunk = pending.substr(0, emit);
                out += chunk;
                pending.erase(0, emit);
                if (onDelta) {
                    jstring js = env->NewStringUTF(chunk.c_str());
                    env->CallVoidMethod(callback, onDelta, js);
                    env->DeleteLocalRef(js);
                }
            }
        }

        ++generated;
        if (generated % 8 == 0) LOGI("[10] 已生成 %d token", generated);
        batch = llama_batch_get_one(&id, 1);
    }

    LOGI("生成结束: %d token, %zu 字节", generated, out.size());
    return env->NewStringUTF(out.c_str());
}

} // extern "C"

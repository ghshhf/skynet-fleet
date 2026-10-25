// 天网 · 本地推理 JNI 桥（薄封装，静态链 llama.cpp 单 .so）
//
// 🔴 为什么「原生 + 集成」而不是借别人 app / 外部 server：
//   用户要的是「一个 APP 解决所有，未来好升级」。所以 llama.cpp 直接编进
//   天网 app（arm64-v8a 单 .so），Java 侧只一行 loadLibrary，没有 .so
//   加载顺序问题（沿用老工程 fleet_whisper / fleet_sd 的薄 JNI + 静态链套路）。
//
// 🔴 2026-10-25 三条硬原则（全部来自真机踩坑 + 主人定调）：
//   1) **不许写死设备参数**。线程数、ctx、GPU 层数一律由调用方按实测设备算好传进来；
//      本文件只在传入值非法时才用「安全兜底」，且兜底值必须是逻辑下限而不是某台机器的值。
//   2) **多轮对话必须复用 KV**。旧版每轮 llama_memory_clear + 全量重放，第三轮起
//      光 prefill 就要几十秒——表现为「越聊越卡」，不是模型慢，是我们自己把活重做了一遍。
//   3) **生成必须可中止**。手机上没有 ESC 键，模型一旦开始胡写，用户只能等或杀进程。
//
// 首版纯 CPU：GPU 能力由 nativeGpuInfo() 如实上报，没有后端就老实返回 0，
// 让上层回落 CPU —— 不假装有、也不假装没有。
#include <jni.h>
#include <string>
#include <vector>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <android/log.h>

#include "llama.h"
#include "ggml-backend.h"
#include "gguf.h"

#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  "LlamaJNI", __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, "LlamaJNI", __VA_ARGS__)

struct LlamaSession {
    llama_model*   model = nullptr;
    llama_context* ctx   = nullptr;
    llama_sampler* smpl  = nullptr;

    // 🔴 前缀复用账本：KV cache 里当前存着哪些 token（按位置顺序）。
    //    每轮和新 prompt 求最长公共前缀，只 decode 差额 —— 这是「越聊越快」的关键。
    std::vector<llama_token> cached;

    int nLayer = 0;        // 模型层数，从 GGUF 读；混合模式按比例切，不写死
    int nCtxTrain = 0;     // 模型训练上下文长度，用来夹住用户要的 ctx
};

// 中止标志：全局单例。手机端同一时刻只有一路生成，为它引入会话级原子量不值。
static std::atomic<bool> g_abort{false};

// 上一轮统计（首字毫秒 / 总毫秒 / token 数）。Kotlin 侧读它算 tok/s。
static std::atomic<long long> g_firstMs{-1};
static std::atomic<long long> g_totalMs{0};
static std::atomic<int>       g_tokens{0};

static std::string jstr2std(JNIEnv* env, jstring js) {
    if (!js) return std::string();
    const char* p = env->GetStringUTFChars(js, nullptr);
    std::string s = p ? p : "";
    if (p) env->ReleaseStringUTFChars(js, p);
    return s;
}

/**
 * 从 GGUF 头直接读层数/训练长度，只为「按比例切层」服务。
 *
 * 为什么不先加载模型再问 llama_model_n_layer：层分配发生在加载那一刻，
 * 加载完再知道层数就晚了（改不了）。GGUF 元数据只读头部，几毫秒。
 * 读不到就返回 0，调用方把 gpuPercent 当「全有/全无」处理（保险方向）。
 */
static void readGgufMeta(const char* path, int* outLayer, int* outCtxTrain) {
    *outLayer = 0;
    *outCtxTrain = 0;
    struct gguf_init_params gp = { /*.no_alloc=*/ true, /*.ctx=*/ nullptr };
    struct gguf_context* g = gguf_init_from_file(path, gp);
    if (!g) return;
    const int64_t nkv = gguf_get_n_kv(g);
    for (int64_t i = 0; i < nkv; ++i) {
        const char* key = gguf_get_key(g, i);
        if (!key) continue;
        const std::string k(key);
        const bool isLayer = k.size() > 12 && k.compare(k.size() - 12, 12, ".block_count") == 0;
        const bool isCtx   = k.size() > 15 && k.compare(k.size() - 15, 15, ".context_length") == 0;
        if (!isLayer && !isCtx) continue;
        if (gguf_get_kv_type(g, i) != GGUF_TYPE_UINT32) continue;
        const int v = (int) gguf_get_val_u32(g, i);
        if (isLayer) *outLayer = v;
        if (isCtx)   *outCtxTrain = v;
    }
    gguf_free(g);
}

extern "C" {

/**
 * GPU 能力如实上报，格式 "设备数|设备名"。
 * 没有编 GPU 后端时设备数=0 —— 上层据此回落 CPU，而不是盲目把层推给不存在的设备。
 */
JNIEXPORT jstring JNICALL
Java_app_skynet_tianwang_next_LlamaNative_nativeGpuInfo(JNIEnv* env, jobject /*thiz*/) {
    llama_backend_init();          // 幂等；保证后端已注册，否则设备枚举恒为 0
    std::string name;
    int gpu = 0;
    const size_t n = ggml_backend_dev_count();
    for (size_t i = 0; i < n; ++i) {
        ggml_backend_dev_t dev = ggml_backend_dev_get(i);
        if (!dev) continue;
        if (ggml_backend_dev_type(dev) == GGML_BACKEND_DEVICE_TYPE_GPU) {
            if (gpu == 0) name = ggml_backend_dev_name(dev) ? ggml_backend_dev_name(dev) : "gpu";
            ++gpu;
        }
    }
    char buf[256];
    snprintf(buf, sizeof(buf), "%d|%s", gpu, name.c_str());
    return env->NewStringUTF(buf);
}

/**
 * 加载模型。返回会话句柄（0 = 失败）。
 *
 * [gpuPercent] 0=纯CPU，100=全部层，1..99=按层数比例（层数从 GGUF 读）。
 * 这里不判断「这台机器该用什么」—— 那是 Kotlin 侧 Device/Dev.plan() 的事，
 * native 只负责忠实执行。设备差异不允许漏进 native 常量。
 */
JNIEXPORT jlong JNICALL
Java_app_skynet_tianwang_next_LlamaNative_nativeLoadModel(
        JNIEnv* env, jobject /*thiz*/, jstring jpath, jint nCtx, jint nThreads, jint gpuPercent) {
    std::string path = jstr2std(env, jpath);
    llama_backend_init();          // 幂等
    auto* s = new LlamaSession();

    int ggufLayer = 0, ggufCtx = 0;
    readGgufMeta(path.c_str(), &ggufLayer, &ggufCtx);
    s->nLayer = ggufLayer;
    s->nCtxTrain = ggufCtx;

    // 百分比 → 层数
    int ngl;
    const int pct = gpuPercent < 0 ? 0 : (gpuPercent > 100 ? 100 : gpuPercent);
    if (pct <= 0) {
        ngl = 0;
    } else if (pct >= 100 || ggufLayer <= 0) {
        ngl = 999;                       // 999 = 「能放多少放多少」，llama.cpp 自有上限
    } else {
        ngl = (int) ((long long) ggufLayer * pct / 100);
        if (ngl < 1) ngl = 1;
    }

    llama_model_params mp = llama_model_default_params();
    mp.n_gpu_layers = ngl;
    s->model = llama_model_load_from_file(path.c_str(), mp);
    if (!s->model) { LOGE("模型加载失败: %s", path.c_str()); delete s; return 0; }

    // 真层数以模型自报为准（GGUF 可能没读到）
    if (s->nLayer <= 0) s->nLayer = llama_model_n_layer(s->model);
    if (s->nCtxTrain <= 0) s->nCtxTrain = (int) llama_model_n_ctx_train(s->model);

    llama_context_params cp = llama_context_default_params();
    // ctx 上限跟模型训练长度走，别让用户要 32K 却塞给一个只会 8K 的模型
    uint32_t want = (uint32_t) (nCtx > 0 ? nCtx : 4096);
    if (s->nCtxTrain > 0 && want > (uint32_t) s->nCtxTrain) want = (uint32_t) s->nCtxTrain;
    cp.n_ctx = want;
    // 线程数由调用方按设备性能核数算好；这里只在明显非法时兜底到 1
    cp.n_threads       = nThreads > 0 ? nThreads : 1;
    cp.n_threads_batch = cp.n_threads;
    s->ctx = llama_init_from_model(s->model, cp);
    if (!s->ctx) {
        LOGE("上下文创建失败（ctx=%u threads=%d）", want, cp.n_threads);
        llama_model_free(s->model);
        delete s;
        return 0;
    }

    auto sp = llama_sampler_chain_default_params();
    s->smpl = llama_sampler_chain_init(sp);
    // 🔴 重复惩罚必须在最前（2026-10-26 加）。
    //    原先链里只有 temp → top_k → top_p → dist，**没有任何重复惩罚**。
    //    小模型（尤其 1B 以下）没有这道约束时，会卡在同一个词/同一句话上原地
    //    打转 —— 用户看到的就是「无限循环」。这不是模型坏，是采样器缺了一道闸。
    //    last_n=64：只惩罚最近 64 个 token，短到能抓住局部循环、长到不会把
    //    整段回答的词都压平（设 -1 = 全上下文，长回答会越写越僵）。
    //    顺序照 llama.cpp 的惯例：penalties → top_k → top_p → temp → dist。
    llama_sampler_chain_add(s->smpl, llama_sampler_init_penalties(64, 1.1f, 0.0f, 0.0f));
    llama_sampler_chain_add(s->smpl, llama_sampler_init_top_k(40));
    llama_sampler_chain_add(s->smpl, llama_sampler_init_top_p(0.9f, 1));
    llama_sampler_chain_add(s->smpl, llama_sampler_init_temp(0.7f));
    llama_sampler_chain_add(s->smpl, llama_sampler_init_dist(0x5EED1234u));

    // 模板有没有、支不支持思考开关 —— 「为什么这个模型答非所问」第一个要看的东西。
    // 有模板 ≠ 是对话模型：base 检查点也可能带着 instruct 模板（SmolLM2 系列就是），
    // 套上去结构合法、语义是空的，表现成「把问题原样复读一遍」。
    {
        const char* t = llama_model_chat_template(s->model, nullptr);
        const bool hasTmpl = (t && *t);
        const bool canThink = hasTmpl && std::string(t).find("enable_thinking") != std::string::npos;
        LOGI("[tmpl] 内嵌模板=%s 思考开关=%s", hasTmpl ? "有" : "无（会退化成纯文本续写）",
             canThink ? "有" : "无");
    }

    LOGI("模型就绪: %s (ctx=%u threads=%d 层=%d gpu=%d%%→ngl=%d 训练ctx=%d)",
         path.c_str(), (unsigned) llama_n_ctx(s->ctx), cp.n_threads,
         s->nLayer, pct, ngl, s->nCtxTrain);
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

/** 请求中止当前生成。下一个 token 边界就退出，已生成的内容照样返回。 */
JNIEXPORT void JNICALL
Java_app_skynet_tianwang_next_LlamaNative_nativeAbort(JNIEnv*, jobject) {
    g_abort.store(true);
}

/** 上一轮统计："firstMs,totalMs,tokens"（firstMs=-1 表示还没出首字）。 */
JNIEXPORT jstring JNICALL
Java_app_skynet_tianwang_next_LlamaNative_nativeLastStats(JNIEnv* env, jobject) {
    char buf[128];
    snprintf(buf, sizeof(buf), "%lld,%lld,%d",
             (long long) g_firstMs.load(), (long long) g_totalMs.load(), (int) g_tokens.load());
    return env->NewStringUTF(buf);
}

/**
 * 生成一轮回复，逐段回调 onDelta（已对齐 UTF-8 边界，中文不会被拆半字节）。
 *
 * [noThink] 只对「模板里带 enable_thinking 的模型」生效（Qwen3 混合思考系）：
 *   在该模板里，assistant 开头插入空的 ` thinking\n\n\n\n` 就等于告诉模型「跳过思考」。
 *   这是模板本身的写法，不是我们发明的小动作；不满足条件的模型一律原样透传。
 */
JNIEXPORT jstring JNICALL
Java_app_skynet_tianwang_next_LlamaNative_nativeChatMessages(
        JNIEnv* env, jobject /*thiz*/, jlong handle, jobjectArray jroles, jobjectArray jcontents,
        jint maxTokens, jboolean noThink, jobject callback) {
    auto* s = reinterpret_cast<LlamaSession*>(handle);
    if (!s || !s->ctx || !s->model) return env->NewStringUTF("");

    g_abort.store(false);
    g_firstMs.store(-1);
    g_totalMs.store(0);
    g_tokens.store(0);

    jsize n = env->GetArrayLength(jroles);
    if (n <= 0) return env->NewStringUTF("");

    std::vector<llama_chat_message> msgs;
    msgs.reserve((size_t) n);
    std::vector<std::string> keep;
    keep.reserve((size_t) n * 2);
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
    const char* tmpl = llama_model_chat_template(s->model, nullptr);

    std::string prompt;
    if (tmpl) {
        std::string t(tmpl);
        // 模板支持 enable_thinking 才做「抑制思考」改写，否则一个字都不动
        const bool canSuppress = noThink && t.find("enable_thinking") != std::string::npos;

        // 🔴 抑制思考的第一道闸：Qwen3 训练时教过的软开关 `/no_think`。
        // 实测证据（2026-10-26）：0.6B 在小尺寸下**不遵守**「空 think 块 = 直接答」这个结构约定，
        // 仍会自说自话想一大段；而 `/no_think` 追加到 user 末尾后它立刻直接答。
        // 所以软开关为主、结构约定为辅，两个一起上。
        if (canSuppress) {
            for (jsize i = n - 1; i >= 0; --i) {
                if (std::string(msgs[(size_t) i].role) != "user") continue;
                // 直接改 keep 里那份字符串（不 push 新元素 ⇒ 不会 realloc ⇒ 指针仍然有效）
                keep[(size_t) i * 2 + 1] += " /no_think";
                msgs[(size_t) i].content = keep[(size_t) i * 2 + 1].c_str();
                LOGI("[think] 已追加 /no_think 软开关");
                break;
            }
        }

        std::vector<char> buf(32768);
        int need = llama_chat_apply_template(tmpl, msgs.data(), n, true, buf.data(), (int32_t) buf.size());
        if (need >= (int) buf.size()) {
            buf.resize((size_t) need + 1);
            need = llama_chat_apply_template(tmpl, msgs.data(), n, true, buf.data(), (int32_t) buf.size());
        }
        if (need > 0) prompt.assign(buf.data(), (size_t) need);

        if (canSuppress && !prompt.empty()) {
            // Qwen3 模板在 add_generation_prompt 时输出 "<|im_start|>assistant\n"；
            // 紧跟一段空的 think 块 = 「不用想了，直接答」。
            const std::string anchor = "<|im_start|>assistant\n";
            const size_t at = prompt.rfind(anchor);
            if (at != std::string::npos && at + anchor.size() == prompt.size()) {
                // 与 Qwen3 模板 `{{- ' thinking\n\n\n\n' }}` 逐字节一致：
                // 空 think 块 = 思考已结束，模型直接给答案。
                prompt += " thinking\n\n\n\n";
                LOGI("[think] 已抑制思考链");
            }
        }
    }
    if (prompt.empty()) {
        for (auto& m : msgs) { prompt += m.role; prompt += ": "; prompt += m.content; prompt += "\n"; }
        prompt += "assistant: ";
    }

    int np = -llama_tokenize(vocab, prompt.c_str(), (int) prompt.size(), nullptr, 0, true, true);
    if (np <= 0) return env->NewStringUTF("");
    std::vector<llama_token> toks((size_t) np);
    if (llama_tokenize(vocab, prompt.c_str(), (int) prompt.size(), toks.data(), np, true, true) < 0)
        return env->NewStringUTF("");

    // ---------- KV 前缀复用：只 decode 与上次不同的那一段 ----------
    size_t common = 0;
    const size_t nCached = s->cached.size();
    while (common < nCached && common < (size_t) np && s->cached[common] == toks[common]) ++common;
    if (common < nCached) {
        // 丢掉从 common 往后的一切（含上次生成残留）
        llama_memory_seq_rm(llama_get_memory(s->ctx), 0, (llama_pos) common, -1);
        s->cached.resize(common);
    }
    LOGI("[kv] prompt=%d token, 复用前缀=%zu, 需新算=%d", np, common, (int) ((size_t) np - common));

    jclass cbCls = callback ? env->GetObjectClass(callback) : nullptr;
    jmethodID onDelta = cbCls ? env->GetMethodID(cbCls, "onDelta", "(Ljava/lang/String;)V") : nullptr;

    const long long tGen = (long long) (std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count());

    // 新算的那段：必须手填 pos（llama_batch_get_one 会从 0 开始编号，跟前缀冲突）
    const int nNew = np - (int) common;
    if (nNew > 0) {
        llama_batch b = llama_batch_init(nNew, 0, 1);
        for (int i = 0; i < nNew; ++i) {
            b.token[i]    = toks[(size_t) common + i];
            b.pos[i]      = (llama_pos) (common + i);
            b.n_seq_id[i] = 1;
            b.seq_id[i][0] = 0;
            b.logits[i]   = (i == nNew - 1) ? 1 : 0;   // 只要最后一个位置的 logits
        }
        b.n_tokens = nNew;
        if (llama_decode(s->ctx, b) != 0) {
            LOGE("prefill decode 失败");
            llama_batch_free(b);
            // 🔴 光清账本不够（2026-10-26 修）：KV 里的脏 cell 不清，
            //    下一轮 common=0 时又不会调用 seq_rm，于是**每一轮都失败**，
            //    一次失败把整个会话彻底废掉。这里必须连 KV 一起清干净。
            llama_memory_seq_rm(llama_get_memory(s->ctx), 0, 0, -1);
            s->cached.clear();
            return env->NewStringUTF("");
        }
        llama_batch_free(b);
    }
    s->cached.assign(toks.begin(), toks.end());

    std::string out, pending;
    char piece[512];
    llama_token id = 0;
    int generated = 0;
    int eog = 0, ctxFull = 0, aborted = 0;
    llama_pos pos = (llama_pos) s->cached.size();

    // 🔴 头部擦除（2026-10-26 实测）：即使已经 `/no_think` + 空 think 块双保险，
    //    Qwen3-0.6B 有时仍会在**正文最前面**再吐一个 `</think>` 外加两个换行。
    //    用户看到的就是一句话被「</think>」顶在头上 —— 小模型的结构不听话，
    //    代码这边兜住：只剥**最开头**那一次；正文中间出现的是内容，一个字节都不动。
    //    为避免流式先吐后删的闪烁，头部先缓冲到能判定为止。
    const std::string TE = "</think>";
    std::string head;
    bool headDone = false;

    auto emitVisible = [&](const std::string& chunk) {
        std::string visible;
        if (headDone) {
            visible = chunk;
        } else {
            head += chunk;
            if (head.size() < TE.size() && TE.compare(0, head.size(), head) == 0) {
                return;                       // 还可能是 </think>，再等一个 piece
            }
            if (head.compare(0, TE.size(), TE) == 0) {
                head.erase(0, TE.size());
            }
            // 顺带吃掉紧跟的空白（模型通常给的是 </think>\n\n）
            size_t i = 0;
            while (i < head.size() &&
                   (head[i] == '\n' || head[i] == '\r' || head[i] == ' ' || head[i] == '\t')) ++i;
            head.erase(0, i);
            headDone = true;
            visible = head;
            head.clear();
        }
        if (visible.empty()) return;
        if (g_firstMs.load() < 0) {
            long long now = (long long) (std::chrono::duration_cast<std::chrono::milliseconds>(
                    std::chrono::steady_clock::now().time_since_epoch()).count());
            g_firstMs.store(now - tGen);
        }
        out += visible;
        if (onDelta) {
            jstring js = env->NewStringUTF(visible.c_str());
            env->CallVoidMethod(callback, onDelta, js);
            env->DeleteLocalRef(js);
        }
    };

    while (generated < maxTokens) {
        if (g_abort.load()) { aborted = 1; break; }
        if ((size_t) pos >= llama_n_ctx(s->ctx)) { ctxFull = 1; break; }

        id = llama_sampler_sample(s->smpl, s->ctx, -1);
        llama_sampler_accept(s->smpl, id);
        if (llama_vocab_is_eog(vocab, id)) { eog = 1; break; }

        int pn = llama_token_to_piece(vocab, id, piece, sizeof(piece), 0, false);
        if (pn > 0) {
            pending.append(piece, (size_t) pn);
            size_t emit = 0, k = 0;
            while (k < pending.size()) {
                unsigned char c = (unsigned char) pending[k];
                size_t len = (c >= 0xF0) ? 4 : (c >= 0xE0) ? 3 : (c >= 0xC0) ? 2 : 1;
                if (k + len > pending.size()) break;
                k += len; emit = k;
            }
            if (emit > 0) {
                std::string chunk = pending.substr(0, emit);
                pending.erase(0, emit);
                emitVisible(chunk);
            }
        }

        ++generated;
        s->cached.push_back(id);        // 生成的 token 也记进账本，下轮直接复用

        // 单 token 续写：pos 必须显式给，否则会覆盖前缀
        llama_batch one = llama_batch_init(1, 0, 1);
        one.token[0]    = id;
        one.pos[0]      = pos;
        one.n_seq_id[0] = 1;
        one.seq_id[0][0] = 0;
        one.logits[0]   = 1;
        one.n_tokens    = 1;
        if (llama_decode(s->ctx, one) != 0) { llama_batch_free(one); LOGE("decode 失败"); break; }
        llama_batch_free(one);
        ++pos;

        if (generated % 8 == 0) LOGI("[gen] %d token", generated);
    }

    long long tEnd = (long long) (std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count());
    g_totalMs.store(tEnd - tGen);
    g_tokens.store(generated);

    // 🔴 把 KV 回滚到「只有 prompt」的边界，账本同步 —— 这一步是关键。
    //
    //    生成期间每个采样出来的 token 都被 decode 进了 KV，所以生成结束时
    //    KV 里的位置是 [0, np+generated)，而账本 `cached` 只有 np 个 prompt token。
    //    **两边长度不一致**，而这个函数只拿账本算 `common`：
    //      · 下一轮历史一变（用户换了句话、或前端把上一轮回答裁短了），
    //        新 prompt 的 token 从 pos=np 开始就和 KV 里残留的生成内容对不上；
    //      · 账本说「从 np 开始都是新的」，于是 `common == nCached` ⇒
    //        `llama_memory_seq_rm` 根本不会被调用 ⇒ 旧 cell 没删，直接在
    //        同一批位置上叠着写。
    //    真机日志里那两次 `prefill decode 失败` 就是这么来的
    //    （00:02:11 中止过一次，00:02:17 / 00:02:43 两次 prefill 全挂），
    //    而且**坏一次就再也起不来**：账本被清空但 KV 还脏着，下一轮 common=0，
    //    还是不删 KV，继续失败 —— 用户看到的就是「这个模型根本不能用」。
    //
    //    修法是立一条不变式：**KV ≡ 账本 ≡ prompt token**，任何时候都成立。
    //    代价是下一轮要重算「上一轮回答 + 新问题」那几十个 token（毫秒级），
    //    换来的是前缀复用依然有效、且永远不会读到错位的位置。
    llama_memory_seq_rm(llama_get_memory(s->ctx), 0, (llama_pos) np, -1);
    s->cached.assign(toks.begin(), toks.end());

    LOGI("生成结束: %d token, %zu 字节, stop=%s", generated, out.size(),
         aborted ? "abort" : eog ? "eog" : ctxFull ? "ctxFull" : "maxTokens");
    return env->NewStringUTF(out.c_str());
}

} // extern "C"

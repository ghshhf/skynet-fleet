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
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <unistd.h>
#include <sys/resource.h>
#include <android/log.h>

#include "llama.h"
#include "ggml-backend.h"
#include "gguf.h"
#if defined(GGML_USE_VULKAN)
#include "ggml-vulkan.h"
#endif

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

/**
 * 2026-11-15：推理参数 A/B 的运行时开关。
 *
 * 全部默认 0 = **完全保持旧行为**，所以这层加进来不改变任何已有结果。
 * 由 self_model_tune 无头设置、卸载会话后下次加载生效；一次只动一个变量做对照，
 * 不把「猜出来的最优组合」一次焊进代码（主人 11-12 定的「不叠 buff」）。
 */
struct Tune {
    std::atomic<int> kvQ{0};          // 0=F16(默认) 1=Q8_0 2=Q4_0
    std::atomic<int> fa{0};           // 0=AUTO(默认) 1=ON 2=OFF
    std::atomic<int> nBatch{0};       // 0=上游默认 2048
    std::atomic<int> nUBatch{0};      // 0=上游默认 512
    std::atomic<int> threads{0};      // 0=按 Dev.plan
    std::atomic<int> threadsBatch{0};
    std::atomic<int> offloadKqv{0};   // 0=上游默认 true, 1=强制开, -1=强制关
    std::atomic<int> opOffload{0};
    std::atomic<int> loadMode{0};     // 0=AUTO 1=MMAP 2=MLOCK 3=MMAP_MLOCK 4=DIRECT_IO 5=NONE
    std::atomic<int> lazyMode{0};     // 0=默认 1=OFF 2=AUTO 3=ON
    // 2026-11-15 真机 A/B 定案：Mali-G925 上一次 batch GEMM 固定 ~13.5s，
    // 拆成逐 token 只要 1.5s/48 tok（首字 14.1s → 1.7s，约 9 倍）。
    // 默认走逐 token；留 0 是给「有 cooperative matrix / int8 dot 的 GPU」换机回退用。
    std::atomic<int> prefillMode{1};  // 0=一次 batch 1=逐 token（默认）
    // 每次 llama_decode 都有固定提交/同步开销；小批次提交更省，但批次一大
    // 会踩回 Mali 的慢 batch GEMM。真机 A/B + 交替复验（各 6 次稳态）：
    //   chunk 1  31.2~31.6 ms/tok（中位 31.5）
    //   chunk 4  25.8~27.7 ms/tok（中位 27.5）  ← 区间不重叠，稳定 -12.7%
    //   chunk 8  开始抖动；chunk 16/32 直接回到 10~26 s 的慢 GEMM
    // ⇒ 临界点在 8~16 之间，4 是安全甜点，固化为默认。
    std::atomic<int> prefillChunk{4};
} g_tune;

// 分段时间（毫秒）：加载 / prefill / 逐 token 生成。
// 之前只有「总耗时 14 秒」一个数，无法判断慢在缺页、prefill 还是解码 ——
// 这是回答「还能不能更快」必须有的证据。
static std::atomic<long long> g_loadMs{0};
static std::atomic<long long> g_prefillMs{0};
static std::atomic<long long> g_decodeMs{0};
static std::atomic<int>       g_promptTok{0};

static long long nowMs() {
    return (long long) std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
}

/**
 * 2026-11-15 深夜：追查「每轮固定 ~14 秒」的内核级快照。
 *
 * 墙钟只能告诉我们「慢」，分不清是 CPU/GPU 真在算，还是在等缺页换入、
 * 等 GPU fence。所以一轮生成前后各取一次 /proc/self：
 *   utime/stime  → CPU 真正烧了多少毫秒（若是等 I/O/GPU，这里几乎不涨）
 *   majflt       → 主缺页次数（zram 换入会在这里暴涨）
 *   read_bytes   → 真从块设备读了什么
 *   VmSwap       → 本进程被换出多少
 * 这是「看清楚那 14 秒是什么」而不是继续猜的唯一办法。
 */
struct ProcSnap {
    long long utime = 0, stime = 0, majflt = 0, rchar = 0, readBytes = 0;
    long long vmRssKb = 0, vmSwapKb = 0;
};
static long long g_clkTck = 0;

static ProcSnap readProcSnap() {
    if (g_clkTck <= 0) {
        long t = sysconf(_SC_CLK_TCK);
        g_clkTck = (t > 0 ? t : 100);
    }
    ProcSnap p{};
    FILE* f = fopen("/proc/self/stat", "r");
    if (f) {
        char b[2048];
        if (fgets(b, sizeof(b), f)) {
            char* rp = strrchr(b, ')');       // comm 里可能有空格/括号，从右找
            if (rp) {
                char* q = rp + 1;
                while (*q == ' ') ++q;
                while (*q && *q != ' ') ++q;  // state 是字母，strtoll 吃不动，先跳过
                long long v[24] = {0};
                int n = 1;                    // v[0] = state 占位，索引与字段号对齐
                while (*q && n < 24) {
                    while (*q == ' ') ++q;
                    if (!*q) break;
                    char* prev = q;
                    v[n++] = strtoll(q, &q, 10);
                    if (q == prev) break;     // 防非数字字段卡死
                }
                // majflt(field12)=v[9], utime(field14)=v[11], stime(field15)=v[12]
                if (n > 12) { p.majflt = v[9]; p.utime = v[11]; p.stime = v[12]; }
            }
        }
        fclose(f);
    }
    f = fopen("/proc/self/io", "r");
    if (f) {
        char k[64]; long long v;
        while (fscanf(f, "%63s %lld", k, &v) == 2) {
            if (strcmp(k, "rchar:") == 0) p.rchar = v;
            else if (strcmp(k, "read_bytes:") == 0) p.readBytes = v;
        }
        fclose(f);
    }
    f = fopen("/proc/self/status", "r");
    if (f) {
        char b[256];
        while (fgets(b, sizeof(b), f)) {
            long long v = 0;
            if (sscanf(b, "VmRSS: %lld", &v) == 1) p.vmRssKb = v;
            else if (sscanf(b, "VmSwap: %lld", &v) == 1) p.vmSwapKb = v;
        }
        fclose(f);
    }
    return p;
}

/** 工作线程真实 CPU 时间（getrusage 比 /proc/self/stat 可靠，不会把多线程算漏/算错）。 */
static long long threadCpuMs(bool sys) {
    struct rusage ru{};
    if (getrusage(RUSAGE_THREAD, &ru) != 0) return 0;
    const struct timeval& t = sys ? ru.ru_stime : ru.ru_utime;
    return (long long) t.tv_sec * 1000 + t.tv_usec / 1000;
}
static std::atomic<long long> g_cpuThreadUserMs{0};
static std::atomic<long long> g_cpuThreadSysMs{0};
static std::atomic<int>       g_prefillCalls{0};
static std::atomic<int>       g_prefillRetry{0};
static std::atomic<int>       g_graphReallocDiff{0};

static std::atomic<long long> g_firstSampleMs{0};  // 第一次 sampler_sample 耗时 = 等 GPU 队列
static std::atomic<long long> g_firstDecodeMs{0};  // 第一次单 token decode 耗时
static std::atomic<long long> g_cpuUserMs{0};
static std::atomic<long long> g_cpuSysMs{0};
static std::atomic<long long> g_majflt{0};
static std::atomic<long long> g_readBytes{0};
static std::atomic<long long> g_vmRssKb{0};
static std::atomic<long long> g_vmSwapKb{0};

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

/**
 * 重建采样链。每轮都重建，因为 grammar 只随「单次请求」变化：
 * 工具调用重试那一轮用 GBNF 强制 JSON，普通聊天则一个字节都不约束。
 *
 * 🔴 关键点：grammar 放进采样链，是在 logits 层**直接屏蔽不合法 token**，
 *    不是「生成完再校验」。0.6B~3B 的小模型没有能力自己守住输出格式，
 *    只能在源头约束 —— 这也是开源社区（android-agent / llama.cpp server）
 *    让本地小模型稳定做 tool call 的通行做法。
 *
 * 语法解析失败时 llama_sampler_init_grammar 返回 NULL，本轮退回无约束：
 * 宁可让它自由回答，也不要让整轮生成直接失败。
 */
static void rebuildSampler(LlamaSession* s, const std::string& grammar) {
    if (s->smpl) { llama_sampler_free(s->smpl); s->smpl = nullptr; }
    const llama_vocab* vocab = llama_model_get_vocab(s->model);
    auto sp = llama_sampler_chain_default_params();
    s->smpl = llama_sampler_chain_init(sp);

    // 🔴 重复惩罚必须在最前（2026-10-26 加）。
    //    原先链里只有 temp → top_k → top_p → dist，**没有任何重复惩罚**。
    //    小模型（尤其 1B 以下）没有这道约束时，会卡在同一个词/同一句话上原地
    //    打转 —— 用户看到的就是「无限循环」。这不是模型坏，是采样器缺了一道闸。
    //    last_n=64：只惩罚最近 64 个 token，短到能抓住局部循环、长到不会把
    //    整段回答的词都压平（设 -1 = 全上下文，长回答会越写越僵）。
    //    顺序照 llama.cpp 的惯例：penalties → grammar → top_k → top_p → temp → dist。
    llama_sampler_chain_add(s->smpl,
        llama_sampler_init_penalties(llama_vocab_n_tokens(vocab), 64, 1.1f, 0.0f, 0.0f));

    if (!grammar.empty()) {
        llama_sampler* g = llama_sampler_init_grammar(vocab, grammar.c_str(), "root");
        if (g) {
            llama_sampler_chain_add(s->smpl, g);
            LOGI("[gbnf] 已启用语法约束（%zu 字节）", grammar.size());
        } else {
            LOGE("[gbnf] 语法解析失败，本轮退回无约束");
        }
    }

    llama_sampler_chain_add(s->smpl, llama_sampler_init_top_k(40));
    llama_sampler_chain_add(s->smpl, llama_sampler_init_top_p(0.9f, 1));
    llama_sampler_chain_add(s->smpl, llama_sampler_init_temp(0.7f));
    llama_sampler_chain_add(s->smpl, llama_sampler_init_dist(0x5EED1234u));
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
        // 2026-11-15：Vulkan 后端在 UMA 手机（Mali/Immortalis/Adreno）上上报的是
        // IGPU 而不是 GPU —— ggml_backend_vk_device_get_type() 按 is_integrated_gpu
        // 分流。只数 GPU 会让「后端和设备都在」被误报成 0，Dev.plan() 因此永远
        // 回落 CPU。手机本来就没有独显，IGPU 就是这台机器唯一的 GPU。
        const auto t = ggml_backend_dev_type(dev);
        if (t == GGML_BACKEND_DEVICE_TYPE_GPU || t == GGML_BACKEND_DEVICE_TYPE_IGPU) {
            if (gpu == 0) name = ggml_backend_dev_name(dev) ? ggml_backend_dev_name(dev) : "gpu";
            ++gpu;
        }
    }
    char buf[256];
    snprintf(buf, sizeof(buf), "%d|%s", gpu, name.c_str());
    return env->NewStringUTF(buf);
}

/**
 * 后端诊断（2026-11-15）：把「哪些后端注册了、各自认出几个设备」原样回执出来。
 * 存在的理由：GPU 报 0 时，光靠 gpuInfo() 一个数字分不清是「后端没编进来」
 * 「实例初始化抛异常」还是「设备类型被过滤」。这个接口一次给全，不靠猜。
 */
JNIEXPORT jstring JNICALL
Java_app_skynet_tianwang_next_LlamaNative_nativeBackendDiag(JNIEnv* env, jobject /*thiz*/) {
    llama_backend_init();
    std::string out;
    char line[512];

    const size_t regs = ggml_backend_reg_count();
    snprintf(line, sizeof(line), "后端注册数=%zu\n", regs);
    out += line;
    for (size_t r = 0; r < regs; ++r) {
        ggml_backend_reg_t reg = ggml_backend_reg_get(r);
        if (!reg) continue;
        const char* rn = ggml_backend_reg_name(reg);
        snprintf(line, sizeof(line), "  reg[%zu] %s : dev=%zu\n",
                 r, rn ? rn : "?", ggml_backend_reg_dev_count(reg));
        out += line;
    }

    const size_t devs = ggml_backend_dev_count();
    snprintf(line, sizeof(line), "设备总数=%zu\n", devs);
    out += line;
    for (size_t i = 0; i < devs; ++i) {
        ggml_backend_dev_t dev = ggml_backend_dev_get(i);
        if (!dev) continue;
        const char* dn = ggml_backend_dev_name(dev);
        const char* dd = ggml_backend_dev_description(dev);
        snprintf(line, sizeof(line), "  dev[%zu] %s type=%d : %s\n",
                 i, dn ? dn : "?", (int) ggml_backend_dev_type(dev), dd ? dd : "");
        out += line;
    }
#if defined(GGML_USE_VULKAN)
    snprintf(line, sizeof(line), "Vulkan 视角设备数=%d\n", ggml_backend_vk_get_device_count());
    out += line;
#else
    out += "Vulkan 视角：本构建未开 GGML_VULKAN\n";
#endif
    return env->NewStringUTF(out.c_str());
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
    // A/B：加载方式。MLOCK 在 Android 上可能被 RLIMIT_MEMLOCK 拒掉，
    // 失败会让 llama_model_load_from_file 返回 nullptr —— 那也是一条真实结论。
    switch (g_tune.loadMode.load()) {
        case 1: mp.load_mode = LLAMA_LOAD_MODE_MMAP;       break;
        case 2: mp.load_mode = LLAMA_LOAD_MODE_MLOCK;      break;
        case 3: mp.load_mode = LLAMA_LOAD_MODE_MMAP_MLOCK; break;
        case 4: mp.load_mode = LLAMA_LOAD_MODE_DIRECT_IO;  break;
        case 5: mp.load_mode = LLAMA_LOAD_MODE_NONE;       break;
        default: break;   // 0 = 上游 AUTO
    }
    switch (g_tune.lazyMode.load()) {
        case 1: mp.lazy_mode = LLAMA_LAZY_MODE_OFF;  break;
        case 2: mp.lazy_mode = LLAMA_LAZY_MODE_AUTO; break;
        case 3: mp.lazy_mode = LLAMA_LAZY_MODE_ON;   break;
        default: break;
    }
    const long long tLoad0 = nowMs();
    s->model = llama_model_load_from_file(path.c_str(), mp);
    g_loadMs.store(nowMs() - tLoad0);
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
    cp.no_perf         = false;   // 打开上游计时，才能拿到 graph 重分配次数

    // ---- A/B 参数（全部默认 0 = 上游默认，不改已有行为）----
    const int kv = g_tune.kvQ.load();
    if (kv == 1)      { cp.type_k = GGML_TYPE_Q8_0; cp.type_v = GGML_TYPE_Q8_0; }
    else if (kv == 2) { cp.type_k = GGML_TYPE_Q4_0; cp.type_v = GGML_TYPE_Q4_0; }
    const int fa = g_tune.fa.load();
    if (fa == 1)      cp.flash_attn_type = LLAMA_FLASH_ATTN_TYPE_ENABLED;
    else if (fa == 2) cp.flash_attn_type = LLAMA_FLASH_ATTN_TYPE_DISABLED;
    if (g_tune.nBatch.load()  > 0) cp.n_batch  = (uint32_t) g_tune.nBatch.load();
    if (g_tune.nUBatch.load() > 0) cp.n_ubatch = (uint32_t) g_tune.nUBatch.load();
    if (g_tune.threads.load() > 0) cp.n_threads = g_tune.threads.load();
    if (g_tune.threadsBatch.load() > 0) cp.n_threads_batch = g_tune.threadsBatch.load();
    const int okqv = g_tune.offloadKqv.load();
    if (okqv != 0) cp.offload_kqv = (okqv > 0);
    const int oop = g_tune.opOffload.load();
    if (oop != 0) cp.op_offload = (oop > 0);
    LOGI("[tune] 生效: ctx=%u threads=%d/%d batch=%u/%u kv=%s fa=%d okqv=%d op=%d load=%lldms",
         cp.n_ctx, cp.n_threads, cp.n_threads_batch, cp.n_batch, cp.n_ubatch,
         kv == 1 ? "q8_0" : kv == 2 ? "q4_0" : "f16", (int) cp.flash_attn_type,
         (int) cp.offload_kqv, (int) cp.op_offload, (long long) g_loadMs.load());

    s->ctx = llama_init_from_model(s->model, cp);
    if (!s->ctx) {
        LOGE("上下文创建失败（ctx=%u threads=%d）", want, cp.n_threads);
        llama_model_free(s->model);
        delete s;
        return 0;
    }

    // 采样链不在这里建：grammar 是「单轮」参数，见 rebuildSampler()。

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

/** 无头设置 A/B 参数（全部 0 = 恢复上游默认）。返回当前生效配置。 */
JNIEXPORT jstring JNICALL
Java_app_skynet_tianwang_next_LlamaNative_nativeSetTuning(
        JNIEnv* env, jobject, jint kvQ, jint fa, jint nBatch, jint nUBatch,
        jint threads, jint threadsBatch, jint offloadKqv, jint opOffload,
        jint loadMode, jint lazyMode, jint prefillMode, jint prefillChunk) {
    g_tune.kvQ.store(kvQ);
    g_tune.fa.store(fa);
    g_tune.nBatch.store(nBatch);
    g_tune.nUBatch.store(nUBatch);
    g_tune.threads.store(threads);
    g_tune.threadsBatch.store(threadsBatch);
    g_tune.offloadKqv.store(offloadKqv);
    g_tune.opOffload.store(opOffload);
    g_tune.loadMode.store(loadMode);
    g_tune.lazyMode.store(lazyMode);
    g_tune.prefillMode.store(prefillMode);
    g_tune.prefillChunk.store(prefillChunk > 0 ? prefillChunk : 1);
    char buf[512];
    snprintf(buf, sizeof(buf),
             "kvQ=%d fa=%d batch=%d/%d threads=%d/%d offload_kqv=%d op_offload=%d load=%d lazy=%d prefill=%d/chunk=%d",
             kvQ, fa, nBatch, nUBatch, threads, threadsBatch,
             offloadKqv, opOffload, loadMode, lazyMode, prefillMode,
             g_tune.prefillChunk.load());
    LOGI("[tune] %s", buf);
    return env->NewStringUTF(buf);
}

/** 上一轮的三段耗时。慢在哪一段，看这个就够，不用再猜。 */
JNIEXPORT jstring JNICALL
Java_app_skynet_tianwang_next_LlamaNative_nativePerfDiag(JNIEnv* env, jobject) {
    const long long load = g_loadMs.load();
    const long long pre = g_prefillMs.load();
    const long long dec = g_decodeMs.load();
    const int np = g_promptTok.load();
    const int nt = g_tokens.load();
    char buf[1024];
    snprintf(buf, sizeof(buf),
             "加载 %lld | prefill %lld/%dtok | 解码 %lld/%dtok | 首字 %lld | 合计 %lld ms || "
             "首次采样等待 %lld ms | 首decode %lld ms | 进程CPU %lld+%lld ms | "
             "线程CPU %lld+%lld ms | prefill调用 %d(重试%d) | graph重分配 %d | "
             "主缺页 +%lld | 块读 +%.1f MB | RSS %.0f MB / Swap %.0f MB",
             load, pre, np, dec, nt,
             (long long) g_firstMs.load(), (long long) g_totalMs.load(),
             (long long) g_firstSampleMs.load(), (long long) g_firstDecodeMs.load(),
             (long long) g_cpuUserMs.load(), (long long) g_cpuSysMs.load(),
             (long long) g_cpuThreadUserMs.load(), (long long) g_cpuThreadSysMs.load(),
             (int) g_prefillCalls.load(), (int) g_prefillRetry.load(),
             (int) g_graphReallocDiff.load(),
             (long long) g_majflt.load(), g_readBytes.load() / 1048576.0,
             g_vmRssKb.load() / 1024.0, g_vmSwapKb.load() / 1024.0);
    return env->NewStringUTF(buf);
}

/**
 * 生成一轮回复，逐段回调 onDelta（已对齐 UTF-8 边界，中文不会被拆半字节）。
 *
 * [noThink] 只对「模板里带 enable_thinking 的模型」生效（Qwen3 混合思考系）：
 *   在该模板里，assistant 开头插入空的 ` thinking\n\n\n\n` 就等于告诉模型「跳过思考」。
 *   这是模板本身的写法，不是我们发明的小动作；不满足条件的模型一律原样透传。
 *
 * [grammar] GBNF 文本（空 = 无约束）。只用于「工具调用格式没守住时重试一次」，
 *   普通聊天绝不传 —— 全程强制 JSON 会把正常回答毁掉。
 */
JNIEXPORT jstring JNICALL
Java_app_skynet_tianwang_next_LlamaNative_nativeChatMessages(
        JNIEnv* env, jobject /*thiz*/, jlong handle, jobjectArray jroles, jobjectArray jcontents,
        jint maxTokens, jboolean noThink, jstring jgrammar, jobject callback) {
    auto* s = reinterpret_cast<LlamaSession*>(handle);
    if (!s || !s->ctx || !s->model) return env->NewStringUTF("");

    g_abort.store(false);
    g_firstMs.store(-1);
    g_totalMs.store(0);
    g_tokens.store(0);
    g_firstSampleMs.store(0);
    g_firstDecodeMs.store(0);
    g_cpuUserMs.store(0);
    g_cpuSysMs.store(0);
    g_majflt.store(0);
    g_readBytes.store(0);
    g_cpuThreadUserMs.store(0);
    g_cpuThreadSysMs.store(0);
    g_prefillCalls.store(0);
    g_prefillRetry.store(0);
    g_graphReallocDiff.store(0);

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
    // 每轮重建采样链：GBNF 只影响本轮，不能把上一轮的工具约束带到下一轮聊天。
    rebuildSampler(s, jstr2std(env, jgrammar));
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

    // ---------- KV 前缀复用（版本 68：账本一致化 + 精准截断） ----------
    // 🔴 66/67 版为了消除 rc=-1 用「kvLen != common 就全清」，代价是每轮把
    //    模板固定前缀（真机约 35 token）也一起重算：逐 token 下 1.5 s 里约七成
    //    是重复劳动。日志实证：[kv] KV末尾=50/54 ≠ 可复用前缀=35 → 全清。
    //
    //    本版立一条不变式：KV ≡ 账本 ≡ 已 decode 的 token 序列（含上一轮回答）。
    //      · 生成结束不再回滚 KV，回答留在里面，前缀才能越过模板继续复用；
    //      · 下一轮按最长公共前缀 common 精准截断 [common, ∞)，只重算新增部分；
    //      · 每一步都读回 seq_pos_max 验证；截断没回退就 clear 全清兜底，
    //        绝不把 pos[0]=common 提交给一个还停在旧末尾的 KV（rc=-1 的根因）。
    //      · iswa/共享 cells 路径下 seq_rm 可能早退，所以「验证」不是可选项。
    llama_memory_t mem = llama_get_memory(s->ctx);
    auto kvLenOf = [&]() -> size_t {
        const llama_pos m = llama_memory_seq_pos_max(mem, 0);
        return m >= 0 ? (size_t) (m + 1) : 0;
    };
    const uint32_t nCtx = llama_n_ctx(s->ctx);

    size_t kvLen = kvLenOf();
    size_t common = 0;
    const size_t nCached = s->cached.size();
    if (kvLen == nCached) {
        while (common < nCached && common < (size_t) np && s->cached[common] == toks[common]) ++common;
    } else {
        LOGI("[kv] 账本=%zu ≠ KV末尾=%zu → 本轮弃用前缀", nCached, kvLen);
    }

    // 上下文快满 / prompt 与账本完全相同（没有新 token 可提交）时，重开一轮最稳。
    const bool ctxTight = (kvLen + (size_t) (np - (int) common) + 8 >= nCtx);
    bool hardClear = (kvLen != nCached) || ctxTight || common == (size_t) np;

    // 🔴 版本 69 收敛：版本 68 真机实测 `seq_rm(common, -1)` 在 LFM2 上完全无效
    //    （[kv] 截断 前=50 后=50）。根因是 hybrid memory：
    //    llama_memory_hybrid::seq_rm 先调 recurrent 侧，而 recurrent 只允许回滚到
    //    「最后一次 ubatch 快照」范围内，rollback=15 > rs_max 就 return false，
    //    hybrid 随即短路，attention 侧根本没删（llama-memory-recurrent.cpp:199-217）。
    //    ⇒ 这类模型不能按任意位置截断，只有 p0=0 的全清有效。
    //    所以：kvLen == common（真·多轮严格前缀）直接增量；否则一次 clear，
    //    不再浪费一次注定失败的 seq_rm。
    if (!hardClear && kvLen > common) {
        LOGI("[kv] KV末尾=%zu > 可复用前缀=%zu，hybrid 不能部分回滚 → clear 后重算",
             kvLen, common);
        hardClear = true;
    }

    if (hardClear) {
        llama_memory_clear(mem, false);   // 只清位置账本，不擦 buffer（数据会被覆盖）
        kvLen = kvLenOf();
        s->cached.clear();
        common = 0;
        if (kvLen != 0) LOGI("[kv] clear 后 KV末尾仍=%zu → 交给全量重算自愈分支", kvLen);
    }
    LOGI("[kv] prompt=%d token, 账本/KV=%zu/%zu, 复用前缀=%zu, 需新算=%d, ctx=%u",
         np, s->cached.size(), kvLen, common, np - (int) common, (unsigned) nCtx);

    jclass cbCls = callback ? env->GetObjectClass(callback) : nullptr;
    jmethodID onDelta = cbCls ? env->GetMethodID(cbCls, "onDelta", "(Ljava/lang/String;)V") : nullptr;

    const long long tGen = nowMs();
    const ProcSnap p0 = readProcSnap();   // 本轮开始时的 CPU/缺页/换出账本
    const long long cpuU0 = threadCpuMs(false), cpuS0 = threadCpuMs(true);

    // 新算的那段：必须手填 pos（llama_batch_get_one 会从 0 开始编号，跟前缀冲突）
    const int nNew = np - (int) common;
    auto prefill = [&](int start, int count) -> bool {
        const long long tpAll = nowMs();
        if (g_tune.prefillMode.load() == 1) {
            // 逐 token prefill：Mali 上一次 batch GEMM(mul_mm) 实测要 ~13.5s，
            // 而单 token graph(mul_mv) 只要 7~98ms。把 prefill 拆成 token 流，
            // 顺带天然满足「Y = X+1」的位置连续性要求。
            // 版本 69：改成按 prefillChunk 分块提交，A/B 找「小批次仍走快路径」的
            // 临界点 —— 每次 llama_decode 都有固定开销，1 token 未必最优。
            const int chunk = std::max(1, g_tune.prefillChunk.load());
            int calls = 0;
            for (int i = 0; i < count; i += chunk) {
                const int n = std::min(chunk, count - i);
                llama_batch b = llama_batch_init(n, 0, 1);
                for (int j = 0; j < n; ++j) {
                    b.token[j]     = toks[(size_t) start + i + j];
                    b.pos[j]       = (llama_pos) (start + i + j);
                    b.n_seq_id[j]  = 1;
                    b.seq_id[j][0] = 0;
                    b.logits[j]    = (i + j == count - 1) ? 1 : 0;
                }
                b.n_tokens = n;
                const int rc = llama_decode(s->ctx, b);
                llama_batch_free(b);
                ++calls;
                if (rc != 0) {
                    LOGI("[prefill] 分块#%d(chunk=%d n=%d) rc=%d 累计=%lldms",
                         calls, chunk, n, rc, nowMs() - tpAll);
                    return false;
                }
            }
            g_prefillCalls.fetch_add(calls);
            LOGI("[prefill] 分块 chunk=%d start=%d count=%d 调用=%d 全 rc=0 cost=%lldms",
                 chunk, start, count, calls, nowMs() - tpAll);
            return true;
        }
        llama_batch b = llama_batch_init(count, 0, 1);
        for (int i = 0; i < count; ++i) {
            b.token[i]     = toks[(size_t) start + i];
            b.pos[i]       = (llama_pos) (start + i);
            b.n_seq_id[i]  = 1;
            b.seq_id[i][0] = 0;
            b.logits[i]    = (i == count - 1) ? 1 : 0;   // 只要最后一个位置的 logits
        }
        b.n_tokens = count;
        const long long tp = nowMs();
        const int rc = llama_decode(s->ctx, b);
        LOGI("[prefill] start=%d count=%d rc=%d cost=%lldms", start, count, rc, nowMs() - tp);
        g_prefillCalls.fetch_add(1);
        llama_batch_free(b);
        return rc == 0;
    };
    if (nNew > 0 && !prefill((int) common, nNew)) {
        g_prefillRetry.fetch_add(1);
        // 🔴 真机 25060RK16C / LFM2-8B-A1B 实测：第一轮 552 token 能生成 512 token，
        //    第二轮起「复用前缀=33」时 prefill decode 必失败，用户看到的是
        //    「模型回了空串」，而且清账本后下一轮还是复用同一段脏前缀，永久坏掉。
        //    修法：前缀失败就清干净 KV + 账本，全量重算一次（只多算几十 token）；
        //    只有全量也失败才认输。不返回空串，会话自愈。
        LOGE("前缀 prefill 失败，清 KV 全量重算");
        llama_memory_clear(mem, false);
        s->cached.clear();
        common = 0;
        if (!prefill(0, np)) {
            LOGE("全量 prefill 仍失败");
            llama_memory_clear(mem, false);
            s->cached.clear();
            return env->NewStringUTF("");
        }
    }
    s->cached.assign(toks.begin(), toks.end());
    // prefill 段到此结束：从 tGen 到这里包含模板/tokenize/新算前缀，不含模型加载。
    g_prefillMs.store(nowMs() - tGen);
    g_promptTok.store(nNew > 0 ? nNew : 0);

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

        const bool firstStep = (generated == 0);
        const long long tSample = firstStep ? nowMs() : 0;
        // Vulkan 是异步提交：prefill 的 llama_decode 只管下令，真正等 GPU 干完
        // 往往发生在第一次要读 logits 的 sampler_sample 里。单独量它，才能判断
        // 那 14 秒是「算得慢」还是「提交完在等」。
        id = llama_sampler_sample(s->smpl, s->ctx, -1);
        if (firstStep) g_firstSampleMs.store(nowMs() - tSample);
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
        const long long tDec = firstStep ? nowMs() : 0;
        const int drc = llama_decode(s->ctx, one);
        if (firstStep) g_firstDecodeMs.store(nowMs() - tDec);
        if (drc != 0) {
            llama_batch_free(one);
            s->cached.pop_back();       // 账本回滚：没 decode 成功的 token 不能留在账本里
            LOGE("decode 失败（账本已回滚 1 token）");
            break;
        }
        llama_batch_free(one);
        ++pos;

        if (generated % 8 == 0) LOGI("[gen] %d token", generated);
    }

    long long tEnd = (long long) (std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count());
    g_totalMs.store(tEnd - tGen);
    g_decodeMs.store(tEnd - tGen - g_prefillMs.load());
    g_tokens.store(generated);

    const ProcSnap p1 = readProcSnap();
    const long long tck = g_clkTck > 0 ? g_clkTck : 100;
    g_cpuUserMs.store((p1.utime - p0.utime) * 1000 / tck);
    g_cpuSysMs.store((p1.stime - p0.stime) * 1000 / tck);
    g_majflt.store(p1.majflt - p0.majflt);
    g_readBytes.store(p1.readBytes - p0.readBytes);
    g_vmRssKb.store(p1.vmRssKb);
    g_vmSwapKb.store(p1.vmSwapKb);
    g_cpuThreadUserMs.store(threadCpuMs(false) - cpuU0);
    g_cpuThreadSysMs.store(threadCpuMs(true) - cpuS0);
    const auto perf = llama_perf_context(s->ctx);
    g_graphReallocDiff.store(perf.n_reallocs_diff);
    LOGI("[diag] sample_wait=%lldms first_decode=%lldms proc_cpu=%lld+%lldms "
         "thread_cpu=%lld+%lldms prefill=%d(retry%d) realloc_diff=%d majflt=+%lld "
         "read=+%.1fMB rss=%.0fMB swap=%.0fMB",
         (long long) g_firstSampleMs.load(), (long long) g_firstDecodeMs.load(),
         (long long) g_cpuUserMs.load(), (long long) g_cpuSysMs.load(),
         (long long) g_cpuThreadUserMs.load(), (long long) g_cpuThreadSysMs.load(),
         (int) g_prefillCalls.load(), (int) g_prefillRetry.load(),
         (int) g_graphReallocDiff.load(),
         (long long) g_majflt.load(), g_readBytes.load() / 1048576.0,
         g_vmRssKb.load() / 1024.0, g_vmSwapKb.load() / 1024.0);

    // 🔴 版本 68：不再把 KV 回滚到「只有 prompt」。
    //    66 版删掉生成结果，是为了躲 rc=-1；现在下一轮会自己按 common 精准截断
    //    并逐项验证，把回答留在 KV 里才能让前缀越过模板、一直复用到回答末尾。
    //    不变式：KV ≡ 账本 ≡ prompt + 已成功 decode 的生成 token。
    //    一旦不成立（异常中断等），立刻清干净，下一轮从零起，绝不带着脏 KV 继续。
    const size_t kvEnd = kvLenOf();
    if (kvEnd != s->cached.size()) {
        LOGI("[kv] 结束不一致：账本=%zu KV末尾=%zu → 清干净", s->cached.size(), kvEnd);
        llama_memory_clear(mem, false);
        s->cached.clear();
    } else {
        LOGI("[kv] 结束一致：账本/KV=%zu，保留 %d 个生成 token 供下轮复用",
             kvEnd, generated);
    }

    LOGI("生成结束: %d token, %zu 字节, stop=%s", generated, out.size(),
         aborted ? "abort" : eog ? "eog" : ctxFull ? "ctxFull" : "maxTokens");
    return env->NewStringUTF(out.c_str());
}

} // extern "C"

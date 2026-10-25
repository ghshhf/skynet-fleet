// 电脑侧验证程序：与 llama_jni.cpp 的 nativeChatMessages 走**完全相同的 API 序列**，
// 只是把 JNI 换成 stdout。
//
// 用途：在电脑上几秒钟验完「模板 → tokenize → decode → 采样 → UTF-8 拼接」，
// 不必每次编译 APK、装机、抓 logcat 才知道对不对。
// 修 JNI 逻辑时先改这里、本地跑通，再同步到 llama_jni.cpp。
//
// 编译（仓库根 /g/dev/llama.cpp-fork）：
//   g++ -O2 -std=c++17 -I include -I ggml/include \
//       E:/xmanbian/skynet-fleet/tianwang_app/tools/verify_host.cpp \
//       -L build/bin -lllama -lggml -lggml-base -lggml-cpu -o build/bin/verify_host.exe
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <chrono>

#ifdef _WIN32
#include <windows.h>
#endif

#include "llama.h"

static long long now_ms() {
    using namespace std::chrono;
    return duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
}

// Windows 命令行参数按 ANSI(GBK) 进来，直接喂给 llama 会变乱码。
// 所以支持 `@文件路径`：从 UTF-8 文件读 prompt（JNI 侧走 GetStringUTFChars，没这问题）。
static std::string read_file(const char* p) {
    FILE* f = fopen(p, "rb");
    if (!f) { fprintf(stderr, "[warn] 读不到 prompt 文件: %s\n", p); return std::string(); }
    std::string s; char buf[4096]; size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) s.append(buf, n);
    fclose(f);
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r')) s.pop_back();
    return s;
}

int main(int argc, char** argv) {
#ifdef _WIN32
    SetConsoleOutputCP(CP_UTF8);
#endif
    if (argc < 2) {
        fprintf(stderr, "用法: verify_host <model.gguf> [prompt] [maxTokens]\n");
        return 2;
    }
    const char* path = argv[1];
    std::string userStr = (argc > 2 && argv[2][0] != '-') ? ((argv[2][0] == '@') ? read_file(argv[2] + 1) : std::string(argv[2]))
                                                          : std::string("你好，用一句话介绍你自己。");
    if (userStr.empty()) userStr = "你好，用一句话介绍你自己。";
    int maxTokens = (argc > 3 && argv[3][0] != '-') ? atoi(argv[3]) : 96;

    // 具名参数：--threads N / --gpu N / --ctx N / --clear-kv
    // 手机和电脑的核数、GPU 完全不同，写死会把「机器差异」误读成「程序差异」。
    int nThreads = 6;     // 默认沿用手机端配置，电脑请显式 --threads 16
    int nGpuLayers = 0;   // 0 = 纯 CPU；-1 = 全部层 offload
    int nCtx = 8192;
    bool clearKv = false;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--threads" && i + 1 < argc)      nThreads = atoi(argv[++i]);
        else if (a == "--gpu" && i + 1 < argc)     nGpuLayers = atoi(argv[++i]);
        else if (a == "--ctx" && i + 1 < argc)     nCtx = atoi(argv[++i]);
        else if (a == "--clear-kv")                clearKv = true;
    }

    llama_backend_init();

    long long t0 = now_ms();
    llama_model_params mp = llama_model_default_params();
    mp.n_gpu_layers = nGpuLayers;
    llama_model* model = llama_model_load_from_file(path, mp);
    if (!model) { fprintf(stderr, "[FAIL] 模型加载失败: %s\n", path); return 1; }
    printf("[load] %lld ms\n", now_ms() - t0);

    llama_context_params cp = llama_context_default_params();
    cp.n_ctx = nCtx;
    cp.n_threads = nThreads;
    cp.n_threads_batch = nThreads;
    llama_context* ctx = llama_init_from_model(model, cp);
    if (!ctx) { fprintf(stderr, "[FAIL] ctx 创建失败\n"); return 1; }
    printf("[cfg] threads=%d gpu_layers=%d ctx=%d gpu_offload_support=%s\n",
           nThreads, nGpuLayers, (int) llama_n_ctx(ctx),
           llama_supports_gpu_offload() ? "YES" : "NO(本次构建无 GPU 后端)");

    llama_sampler* smpl = llama_sampler_chain_init(llama_sampler_chain_default_params());
    llama_sampler_chain_add(smpl, llama_sampler_init_temp(0.7f));
    llama_sampler_chain_add(smpl, llama_sampler_init_top_k(40));
    llama_sampler_chain_add(smpl, llama_sampler_init_top_p(0.9f, 1));
    llama_sampler_chain_add(smpl, llama_sampler_init_dist(0x5EED1234u));

    const llama_vocab* vocab = llama_model_get_vocab(model);

    llama_chat_message m;
    m.role = "user";
    m.content = userStr.c_str();
    const char* tmpl = llama_model_chat_template(model, nullptr);
    printf("[tmpl] %s\n", tmpl ? "模型自带" : "无（回退裸拼）");

    std::string prompt;
    if (tmpl) {
        std::vector<char> buf(24576);
        int need = llama_chat_apply_template(tmpl, &m, 1, true, buf.data(), (int32_t)buf.size());
        if (need > 0) {
            prompt.assign(buf.data(), (size_t)need);
        } else if (need < 0) {
            printf("[tmpl] apply 返回 %d（套不上，回退）\n", need);
        }
    }
    if (prompt.empty()) prompt = std::string("user: ") + userStr + "\nassistant: ";
    printf("[prompt] %s\n", prompt.c_str());

    int np = -llama_tokenize(vocab, prompt.c_str(), (int)prompt.size(), nullptr, 0, true, true);
    printf("[promptTokens] %d\n", np);
    if (np <= 0) { fprintf(stderr, "[FAIL] tokenize 探测失败\n"); return 1; }
    std::vector<llama_token> toks((size_t)np);
    if (llama_tokenize(vocab, prompt.c_str(), (int)prompt.size(), toks.data(), np, true, true) < 0) {
        fprintf(stderr, "[FAIL] tokenize 失败\n");
        return 1;
    }

    // ↓↓↓ 以下与 llama_jni.cpp 修复后的循环逐行对应 ↓↓↓
    // 可选：模拟 JNI 里的「每轮前清 KV」。用 --clear-kv 打开，
    // 用来验证这句是不是卡死的元凶。
    if (clearKv) {
        printf("[kv] clearing...\n"); fflush(stdout);
        llama_memory_clear(llama_get_memory(ctx), true);
        printf("[kv] cleared\n"); fflush(stdout);
    }
    llama_token id = 0;                       // 必须在循环外：batch 只存指针
    llama_batch batch = llama_batch_get_one(toks.data(), (int32_t)np);
    int generated = 0;
    long long tGen = now_ms();
    long long firstMs = -1;
    std::string out, pending;
    char piece[512];
    int eog = 0, ctxFull = 0;

    while (generated < maxTokens) {
        const int n_ctx  = (int) llama_n_ctx(ctx);
        const int n_used = (int) llama_memory_seq_pos_max(llama_get_memory(ctx), 0) + 1;
        if (n_used + batch.n_tokens > n_ctx) { ctxFull = 1; break; }
        if (llama_decode(ctx, batch) != 0) { printf("[decode] 失败\n"); break; }

        id = llama_sampler_sample(smpl, ctx, -1);
        llama_sampler_accept(smpl, id);
        if (llama_vocab_is_eog(vocab, id)) { eog = 1; break; }

        int pn = llama_token_to_piece(vocab, id, piece, sizeof(piece), 0, false);
        if (pn > 0) {
            pending.append(piece, (size_t)pn);
            size_t emit = 0, k = 0;
            while (k < pending.size()) {
                unsigned char c = (unsigned char) pending[k];
                size_t len = (c >= 0xF0) ? 4 : (c >= 0xE0) ? 3 : (c >= 0xC0) ? 2 : 1;
                if (k + len > pending.size()) break;
                k += len; emit = k;
            }
            if (emit > 0) {
                if (firstMs < 0) firstMs = now_ms() - tGen;
                out += pending.substr(0, emit);
                pending.erase(0, emit);
            }
        }
        ++generated;
        batch = llama_batch_get_one(&id, 1);
    }
    long long genMs = now_ms() - tGen;

    printf("\n=== 结果 ===\n");
    printf("out=[%s]\n", out.c_str());
    printf("first=%lld ms  total=%lld ms  tokens=%d  %.2f tok/s  stop=%s\n",
           firstMs, genMs, generated,
           genMs > 0 ? generated * 1000.0 / genMs : 0.0,
           eog ? "eog" : ctxFull ? "ctx-full" : "maxTokens");

    llama_sampler_free(smpl);
    llama_free(ctx);
    llama_model_free(model);
    llama_backend_free();
    return out.empty() ? 1 : 0;
}

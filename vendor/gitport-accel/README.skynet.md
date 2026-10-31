# gitport-accel · 天网网络加速子系统

> 上游来源：`github.com/zhidian-cmd/gitport-github` v0.5.2 · Apache-2.0（同目录 `LICENSE` 已保留）
> 用途：**把"代理麻烦"从天网及所有 ghshhf 项目的开发流里彻底摘掉**——GFW 对 GitHub 的限速/干扰（DNS 污染、SNI 点名 RST、边缘 IP 漂移）不再当主脑。

---

## 一、它到底是什么

一个 **Rust 写的 GitHub 专用本地加速代理**（双端口：`127.0.0.1:7810` 代理 + `:7811` 管理页）。
**注意：它不是翻墙工具，是 GitHub 抗干扰/加速通道。** 在已经翻墙的环境里，它的价值是「更快更稳的 GitHub 专用路径」（绕过 SNI 点名、加速 clone/push、抗边缘漂移），不是替代翻墙。

核心打法（从源码提炼的 8 块真资产）：

| 资产 | 源码位置 | 作用 |
|---|---|---|
| ① GitHub 真实 IP 表 | `src/pool/seeds.json` | **最硬资产**：GLB `140.82.x` / Fastly `185.199.x` / Azure `4.249.x` 真实边缘 IP 写死本地，**根本不查 DNS**（污染免疫）。伪装腿全配 `sni:"baidu.com"` |
| ② Chrome TLS 指纹 | `src/pool/dial.rs` | BoringSSL（Chrome 同款）：GREASE + Chrome 默认套件/扩展序 + ALPN h2 + SNI 与校验名解耦 |
| ③ ClientHello 首包分片 | `src/pool/dial.rs` `SplitHello` | byedpi 同款：CONNECT 把 ClientHello 拆 2~3 段、随机间隔发，DPI 拼不齐 SNI |
| ④ 串发拨号引擎 | `src/pool/mod.rs` `serial_dial_skip` | IP 逐个试，握手上≠成功（要到真数据才算），失败 IP 沉表尾，整圈全灭冷却 30s |
| ⑤ 路由/白名单 | `src/config.rs` + `src/pool/route.rs` | 域名分组、直连白名单、按 IP 段认领族（末段 154/153/133 / Azure 前缀） |
| ⑥ h2 连接池 + EWMA 择优 | `src/server/upstream.rs` | 每 host ≤2 连接、按实测耗时择优、每 8 次轮转探索；双通道 `slot` 隔离 |
| ⑦ 镜像链 ECT 竞速 | `src/server/mitm/mirror.rs` | 大文件直连 vs 镜像同台，按"预计最早下完"动态选路 |
| ⑧ 遥测拦截 + 本地 MITM | `src/server/mitm/mod.rs` | GitHub 前端 75% 请求是 Datadog 遥测，本地读掉回 204；可选 MITM 假名腿 |

---

## 二、为什么进天网（落点）

天网（`device-fleet`）及所有 ghshhf 项目（crypto_stocks / ollama / LocalSend / millionFund）开发时，
git clone、cargo、经 GitHub 托管的依赖全得跟 GitHub 打交道——慢、被墙、卡一半。
gitport 把这件事从「天天操心」变成「后台无感」。

**落点决策**：作为 `device-fleet/vendor/gitport-accel/` 的 vendor 子目录（保留上游 LICENSE/NOTICE）。
理由：本路线要改白名单（见第三节），改上游 fork 会让 `device-fleet` 的 git 历史夹带第三方脏改动；
vendor 副本 + 保留来源注释，既拿到能力又可控。

---

## 三、配置驱动蓝图（加站点 = 改配置，大部分不改代码）

> 这是本路线相对上游的核心改动：**把硬编码的 GitHub 白名单/段号抽成 `sites.toml`**。
> 上游 `config.rs` 把 `GROUPS` / `WHITELIST_HOSTS` / `DIRECT_HOSTS` 写死，`route.rs` 把
> `FAMILY_WL_154/153/133/_AZURE` 四个段族写死。改造目标：新增一个国外网站只改 `sites.toml`。

### 3.1 `sites.toml` 设计（见 `SITES_EXAMPLE.toml`）

```toml
[[sites]]
name = "github"            # 内置第一个站点
groups = ["github.com", "*.github.com", "api.github.com"]   # 进池域名（* = 通配子域）
[whitelist]                # 真名+分片腿：host -> seg（IP 末段 / Azure 前缀）
"docs.github.com" = "154"
"support.github.com" = "133"
"copilot-proxy.githubusercontent.com" = "4.249"
[direct]                   # 不归任何池、不经代理（系统 DNS 直连）
"uploads.github.com" = ""
"objects-origin.githubusercontent.com" = ""
[mirrors]                  # 内容域 host -> 镜像链（大/小文件分治，源码已有 CONTENT_HOSTS 表）
"raw.githubusercontent.com" = ["https://v4.gh-proxy.org"]
```

新增站点示例（同样是 GitHub 生态 / Fastly 系，直接复用现有段族，纯配置）：

```toml
[[sites]]
name = "my-fastly-site"
groups = ["cdn.example.com", "*.example.com"]
[whitelist]
"api.example.com" = "154"   # 复用 Fastly .154 段 -> 现有 FAMILY_WL_154 自动认领
```

### 3.2 改造点（需改的两处，均在工具链就绪后实施）

1. **`src/config.rs`**：`GROUPS` / `WHITELIST_HOSTS` / `DIRECT_HOSTS` 三个 `&'static` 常量
   改为 `OnceLock<Vec<...>>` 运行时加载；`pool_for` / `is_whitelist` / `whitelist_seg` /
   `is_direct` / `pooled_domains` 改读加载后的内存数据，查不到回落 GitHub 内置默认。
2. **`src/pool/route.rs`**：`POOL_ROUTES` 里的 `FAMILY_WL_*` 由 `sites.toml` 的 `whitelist`
   seg 值**动态生成**（每个 seg 值 → 一个 `FamilyRoute`，`serves` 闭包按该 seg 匹配 IP）。
   这样 `whitelist_seg` 返回任意 seg 都能找到族。

> 边界：若新站点用**全新 IP 段**（非 154/153/133/Azure），`route.rs` 需补一行族条目（一行闭包）。
> GitHub 生态 + Fastly 系站点基本全自动；这是"加东西为主、极小代码量为辅"。

---

## 四、天网集成步骤

1. **编译**（需 Rust stable + cargo + C 编译器；`boring`/`ring` 还需 perl/make/yasm）：
   ```bash
   cd device-fleet/vendor/gitport-accel && cargo build --release
   # 产物 target/release/gitport-github(.exe)
   ```
   > 本机（停车场岗机）**未装 Rust 工具链**（无 cargo），此步未在此机验证；需在装有工具链的环境编译。
2. **常驻**：作为天网桌面端（`tools/build_gui.py`）的子进程随桌面端启动，或独立系统托盘常驻
   （gitport 自带 tray）。监听 `127.0.0.1:7810`（代理）+ `:7811`（管理页）。
3. **开发环境接入**：天网所有项目的 git / cargo / npm / pip 设
   `HTTP_PROXY=127.0.0.1:7810` `HTTPS_PROXY=127.0.0.1:7810`。
   只接管 GitHub 系域名，其余流量原样直连。

---

## 五、本机现状与限制

- 本机（天网桌面端开发机）**无 Rust 工具链**，`cargo build` 未在此验证；代码与资产已落地，编译待工具链就绪。
- git 联网本机现走 `karing` 代理（`127.0.0.1:3067` 监听）；WorkBuddy 沙箱默认拦截 git 直连联网，
  git 类操作需 `dangerouslyDisableSandbox` 或走代理。**gitport 解决的是 GFW 对 GitHub 的干扰，不是替代翻墙。**
- 数据资产 `seeds.json`（IP 表）随仓库走，上游消失也能重建。

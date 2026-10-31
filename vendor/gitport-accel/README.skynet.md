# gitport-accel · 天网网络加速子系统（最终版）

> 上游来源：`github.com/zhidian-cmd/gitport-github` v0.5.2 · Apache-2.0（同目录 `LICENSE` 已保留）
> 用途：**把"代理麻烦"从天网及所有 ghshhf 项目的开发流里彻底摘掉**——GFW 对 GitHub 的限速/干扰（DNS 污染、SNI 点名 RST、边缘 IP 漂移）不再当主脑。
> **最终版扩展**：从 GitHub-only 升级为**通用国外站点加速**（YouTube / Twitter / HuggingFace / arXiv / 任意国外网站 / 模型下载 / 国外论文），加站点 = 改 `sites.toml`。

---

## 一、它到底是什么

一个 **Rust 写的本地加速代理**（双端口：`127.0.0.1:7810` 代理 + `:7811` 管理页）。
**注意：它不是翻墙工具，是"能直连但被 DPI 掐"的国外站点的抗干扰/加速通道。**
在已经翻墙的环境里，它的价值是「更快更稳的专用路径」（绕过 SNI 点名、加速 clone/push、抗边缘漂移）；
在没翻墙的环境里，它让 GitHub 这类"能直连但被干扰"的站点恢复可用——但**对真正被墙的站点无能为力**（那需要远端节点，不是本地代理的活）。

核心打法（从源码提炼的 8 块真资产）：

| 资产 | 源码位置 | 作用 |
|---|---|---|
| ① 真实 IP 表 / 种子 | `src/pool/seeds.json` + `src/site.rs` `seeds` | **最硬资产**：GitHub GLB `140.82.x` / Fastly `185.199.x` / Azure `4.249.x` 真实边缘 IP 写死本地，**根本不查 DNS**（污染免疫）。站点扩展后每个站点自带 `seeds` IP 表 |
| ② Chrome TLS 指纹 | `src/pool/dial.rs` | BoringSSL（Chrome 同款）：GREASE + Chrome 默认套件/扩展序 + ALPN h2 + SNI 与校验名解耦 |
| ③ ClientHello 首包分片 | `src/pool/dial.rs` `SplitHello` + `src/server/mod.rs` `bidir_copy` | byedpi 同款：CONNECT 把 ClientHello 拆 2~3 段、随机间隔发，DPI 拼不齐 SNI；分片开关按站点画像 `split_hello` 可配 |
| ④ 串发拨号引擎 | `src/pool/mod.rs` `serial_dial_skip` | IP 逐个试，握手上≠成功（要到真数据才算），失败 IP 沉表尾，整圈全灭冷却 30s；站点池在 `serial_load` 注入 seeds 后复用同一引擎 |
| ⑤ 路由/白名单 | `src/config.rs` `pool_for` + `src/pool/route.rs` | 直连 → 白名单(edge) → 站点池 → GitHub groups → Tunnel；域名分组、直连白名单 |
| ⑥ h2 连接池 + EWMA 择优 | `src/server/upstream.rs` | 每 host ≤2 连接、按实测耗时择优、每 8 次轮转探索；双通道 `slot` 隔离 |
| ⑦ 镜像链 ECT 竞速 | `src/server/mitm/mirror.rs` | 大文件直连 vs 镜像同台，按"预计最早下完"动态选路 |
| ⑧ 遥测拦截 + 本地 MITM | `src/server/mitm/mod.rs` | GitHub 前端 75% 请求是 Datadog 遥测，本地读掉回 204；可选 MITM 假名腿 |

---

## 二、为什么进天网（落点）

天网（`device-fleet`）及所有 ghshhf 项目（crypto_stocks / ollama / LocalSend / millionFund）开发时，
git clone、cargo、经 GitHub 托管的依赖全得跟 GitHub 打交道——慢、被墙、卡一半。
gitport 把这件事从「天天操心」变成「后台无感」。多站点扩展后，**看 YouTube、推特、HuggingFace 下模型、arXiv 读论文**
这类"能直连但被干扰"的需求，也能走同一条本地通道，而不是常开 karing 这类全局翻墙。

**落点决策**：作为 `device-fleet/vendor/gitport-accel/` 的 vendor 子目录（保留上游 LICENSE/NOTICE）。
理由：本路线要改白名单/加站点，改上游 fork 会让 `device-fleet` 的 git 历史夹带第三方脏改动；
vendor 副本 + 保留来源注释，既拿到能力又可控。天网侧集成封装在 `fleet/netaccel.py`（启停/代理/PAC 路由）。

---

## 三、多站点配置驱动（最终版：加站点 = 改 toml，零代码）

> 相对上游的核心改动：**把硬编码的 GitHub 白名单/段号抽成 `sites.toml`，并新增 `src/site.rs` 多站点画像系统。**
> 上游 `config.rs` 把 `GROUPS` / `WHITELIST_HOSTS` / `DIRECT_HOSTS` 写死，`route.rs` 把
> `FAMILY_WL_154/153/133/_AZURE` 四个段族写死——这套仍管 GitHub 主站；
> 新站点走 `site.rs` 的 `domains/seeds/sni_disguise/split_hello`，与 GitHub 池**并存不冲突**（同一 `sites.toml`，各自取所需字段）。

### 3.1 两套并存的数据模型

| 系统 | 字段（toml） | 管什么 | 拨号方式 |
|---|---|---|---|
| 旧 `sites.rs`（GitHub 专用） | `groups` / `whitelist` / `direct` / `mirrors` | GitHub 主站及 Fastly 系 | 现有 `FAMILY_WL_*` 段族串发 |
| 新 `site.rs`（多站点） | `domains` / `seeds` / `sni_disguise` / `split_hello` / `direct` | YouTube/Twitter/HF/arXiv/任意国外站 | 站点池串发（IP 表直连，污染免疫） |

### 3.2 新增一个国外站点（纯配置，零代码）

```toml
[[sites]]
name = "my-site"                 # 站点池名（与 GitHub 的 core/api/edge 隔离，游标互不污染）
domains = ["example.com", "*.example.com"]   # 域名模式，* = 通配子域
seeds = [                                 # IP 种子表（DNS 污染免疫；系统不查 DNS）
    { ip = "93.184.216.34" },              # 无 sni = 用站点级默认
    { ip = "93.184.216.35", sni = "baidu.com" }  # 该 IP 伪装 SNI（校验名仍按目标 host）
]
sni_disguise = "baidu.com"      # 站点级默认伪装 SNI（种子自带 sni 优先）
split_hello = true              # 对该站点开启 ClientHello 首包分片（抗 SNI-RST）
direct = ["status.example.com"] # 直连白名单（不经代理，交系统 DNS）
```

**路由命运**：
- 有 `seeds` 的站点 → 进「站点专用池」（名=站点名），`connect_upstream` 走串发拨 IP 表（污染免疫）。
- 无 `seeds` 但有 `domains` 的站点 → 走通用 `Tunnel`（CONNECT 透传 + 全局分片兜底，最低可用性）。
- `direct` 列出的 host → 系统 DNS 直连，不经代理。

### 3.3 内置默认站点（见 `src/site.rs` `default_profiles`）

`github`（由旧系统管，此处 only 兜底 direct）、`youtube`、`twitter`、`huggingface`、`arxiv`。
YouTube/Twitter 默认 `split_hello=true`（对抗 SNI-RST）；HuggingFace/arXiv 默认 `split_hello=false`（直连稳）。
IP 种子是写死的「已知好 IP」，需随目标站点边缘 IP 变动而更新——这是该类工具的天花板，无自动发现。

---

## 四、天网集成层（`fleet/netaccel.py`）

gitport 启动参数：`-headless`（无窗常驻）/ `-tray`（托盘）/ `-port N` / `-webui N`。

`netaccel.py` 把 gitport 从「手动双击」升级为天网可管控通道，**核心是 PAC 路由**——
只让登记的外国站点走 7810，国内站点强制 DIRECT（否则会拖垮国内站，这是 gitport 非翻墙的本质约束）：

```bash
python fleet/netaccel.py status            # 看 gitport 在不在、代理开没开
python fleet/netaccel.py start             # 启动 gitport(headless) + 接管 git 代理
python fleet/netaccel.py stop              # 停 gitport + 清 git 代理
python fleet/netaccel.py pac > gitport.pac # 导出 PAC（仅受管域名走 7810）
python fleet/netaccel.py system-proxy on gitport.pac   # 系统代理指向 PAC
python fleet/netaccel.py system-proxy off  # 关系统代理
python fleet/netaccel.py --selftest        # 5 项自测（域名/PAC/端口/系统代理）
```

集成要点：
- **git 代理**：`git config --global http/https.proxy` 指向 7810，只影响 GitHub 系（PAC 维度则是全受管域名）。
- **系统代理**：写 `HKCU\...\Internet Settings` 的 `AutoConfigURL` 指向 PAC 文件，**不改防火墙**（防火墙是系统级动作，单独走 netpath）。
- **无依赖**：零第三方库，Windows 用 `winreg`/`taskkill`，非 Win 退化为提示手动配置。
- **启停健壮**：按端口找 PID 杀，失败按 exe 名兜底；启动轮询 7810 监听才算成功。

---

## 五、编译与部署

```bash
# 封装好的脚本（环境变量已固化：gnu 目标/MinGW gcc/Go/LLVM/BINDGEN/rsproxy 镜像）
D:\tools\build_gitport.bat            # release 构建
D:\tools\build_gitport.bat debug      # 快速验证编译链
# 产物: target\x86_64-pc-windows-gnu\release\gitport-github.exe
```
产物目录需带齐运行时：`WebView2Loader.dll`（从 `target/.../build/webview2-com-sys-*/out/x64/` 复制）+ 4 个 MinGW 运行时 DLL。
（参考 `D:/.workbuddy/memory/2026-10-31-gitport编译与工具链修复.md` 的完整踩坑记录。）

依赖链：`boring=4`/`tokio-boring`（BoringSSL，需 Go 代码生成 + libclang bindgen）、`ring`（需 NASM）、
`wry`/`tao`/`tray-icon`（WebView2 管理页）。目标 `x86_64-pc-windows-gnu`，链接器用 Strawberry MinGW 的 `gcc`。

---

## 六、本机现状与限制

- 本机 Rust 工具链已修复并常用化：rustup 默认 `stable-x86_64-pc-windows-gnu`（rustc 1.99.1），链接器 MinGW `gcc`；Go 1.27.3（`D:\tools\go`）；LLVM 18.1.8 libclang（bindgen）。
- **gitport 解决的是 GFW 对"能直连站点"的干扰，不是替代翻墙。** 真正被墙的站点需远端节点（clash/sing-box/v2ray 路线），不在此工具职责内。
- 多站点扩展经 129 个单元测试验证（路由/站点/串发全部通过）；调试期偶发的 certgen MITM 握手崩溃为沙箱环境问题，与本次改动无关。
- 数据资产 `seeds.json` + `site.rs` 内置种子随仓库走，上游消失也能重建。

---

## 七、类似开源项目调研（参考，见 `REFERENCE_PROJECTS.md`）

| 项目 | 路线 | 与 gitport 关系 |
|---|---|---|
| byedpi / GoodbyeDPI | 报文层 ClientHello/SNI 分片（无代理/路由/IP 池） | 分片技术同源；gitport 用应用层 `bidir_copy` 分段替代 WinDivert 驱动 |
| gecit | eBPF MSS 钳制强割 + 低 TTL 假包 | Linux 部署时的分片增强项（Win 无 eBPF） |
| dev-sidecar | 本地代理 + DNS 优选 + **国内镜像**兜底 | 使命最近，但走镜像而非直连 IP 池 |
| FastGithub | GitHub 专用 SNI 改写 + IP 优选 | GitHub 池思路来源 |
| clash / sing-box / v2ray | 通用规则路由（依赖远端节点） | 路由模型借鉴；gitport 把 outbound 实现成本地 IP 池串发 |

**gitport 的差异化护城河**：本地零远端依赖 + Chrome TLS 指纹 + h2 连接池 + MITM 化解 403，
这套组合在「能直连但被 DPI 掐」的国情下，比纯镜像或纯分片都稳。

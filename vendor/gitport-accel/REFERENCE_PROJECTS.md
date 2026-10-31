# 类似开源项目调研（gitport 多站点扩展参考）

> 调研时间：2026-10-31｜目的：为 gitport 从「GitHub 专用」扩展到「通用国外网站加速」找可借鉴架构。
> 结论先行：**没有任何一个现成项目同时做到「本地代理 + 直连 IP 池串发 + ClientHello 分片 + 多站点配置化」**。
> gitport 的独特位置是这几条线的杂交体。下面按「技术路线」而非「名气」分类。

---

## 一、报文层对抗（ClientHello / SNI 分片）—— gitport 分片腿的同款源头

### 1. GoodbyeDPI（ValdikSS，Apache-2.0，~28.8k★）
- **路线**：Windows-only，基于 **WinDivert**（内核态之下的用户态包拦截驱动）。
- **手法**：把 TLS ClientHello 的首个数据报**在 SNI 处 TCP 分片**，使 DPI 中盒要么重组要么漏看主机名；外加伪造包（低 TTL / 错校验和 / 错序列号）、HTTP Host 大小写混淆、`--frag-by-sni` 精准在 SNI 前切一刀、`--reverse-frag` 逆序发包（对付不兼容分段 ClientHello 的服务器）。
- **不做的**：不代理、不路由、不碰 DNS 优选、不改路由表。纯报文形态改造。
- **对我们的价值**：分片 + 逆序 + SNI 前切点是 gitport `HELLO_SPLIT` 的直接技术祖先。但 WinDivert 驱动在 gitport 的 MinGW 链接链里是负担，gitport 选了应用层 `bidir_copy` 分段而非内核驱动，等价效果、零驱动。

### 2. byedpi（hufrea / metorus，GoodbyeDPI 的 Linux/macOS 移植）
- **路线**：无驱动，纯用户态 socket 操纵。**`-–split 1+s`**（在 SNI 偏移处切分）就是 gitport 分片腿的逐字节对应物；另有 `--disorder`（乱序）、`--fake`（假首段 + 低 TTL 使服务器收不到重传打乱）、`--fake-sni`、`--tlsrec`（把 ClientHello 拆成多条 TLS 记录）。
- **不做的**：同样不代理、不路由、无 IP 池。它一次只改一个连接的报文。
- **对我们的价值**：确认「应用层分段即可骗过 SNI-RST」这条路在 Linux/Win 都通，gitport 没必要上驱动。

### 3. gecit（eBPF sock_ops，较新思路）
- **路线**：Linux 内核 eBPF `sock_ops`，在三次握手完成、app 还没发数据那一刻，**把 TCP_MAXSEG 钳到 88 字节** → 内核自动把 ClientHello 切小段；再发一个低 TTL（=8）的假 ClientHello（SNI 填 www.google.com），假包够到 DPI 但到不了服务器，DPI 误判放行、真包溜过。零 bulk 开销。
- **局限**：需 root、仅 Linux、macOS 无 eBPF 只能退化到 TUN+CONNECT 代理。
- **对我们的价值**：MSS 钳制强割是比「应用层 sleep 分段」更稳的 ClientHello 分片手段。可作为 gitport 在 Linux 部署时的增强项（Windows 无 eBPF，仍用 bidir_copy 分段）。

---

## 二、本地代理 + 多站点加速（gitport 的「使命」最接近者）

### 4. dev-sidecar（docmirror，service-mesh sidecar 概念）
- **路线**：本地代理，拦截打不开的站点 → **代理到国内加速镜像/CDN**；DNS 优选（按网络状况解析最佳 IP 规避污染）；镜像失败自动切备用；GitHub 加速（SNI 改写，明确「感谢 FastGithub 提供思路」）、Stack Overflow（ajax.google.com→CDN）、npm、pip。
- **与 gitport 的根本分歧**：dev-sidecar **走国内镜像**，gitport **走直连 IP 池**。镜像路线在「被墙」站点上受限（镜像本身可能被墙或没镜像），直连 IP 池则对「能直连但被 DPI 掐」的站点更有效。
- **对我们的价值**：多站点分类加速的产品形态参考（域名分组、镜像兜底、测速切换）；gitport 可借鉴其「站点分组 + 兜底」的工程组织，但底层换成 IP 池串发。

### 5. FastGithub（dotnetcore）
- **路线**：GitHub 专用，SNI 改写 + IP 优选直连。dev-sidecar 的 GitHub 加速思路来源。
- **对我们的价值**：证明「IP 优选 + SNI 改写」对 GitHub 直连有效——gitport 的 GitHub 池（白名单族 + SNI 伪装 baidu.com）是同一思路的工程化加强版（还加了串发 + 403 化解）。

### 6. gh-proxy / hubproxy
- **路线**：纯 URL 改写镜像代理（github.com → gh-proxy.com/https://...），服务端部署，非本地。
- **对我们的价值**：大文件镜像链的「服务端同台竞速」思路可借鉴（gitport 已有 Mirror 路由 + ECT 竞速）。

---

## 三、通用规则路由框架（「正确做法」参照，但依赖远端节点）

### 7. clash / sing-box / v2ray
- **路线**：规则路由（域名/IP/CIDR/GeoIP → 出站）、多出站、TUN 模式、可装插件。
- **与 gitport 的根本分歧**：它们是**全功能代理/VPN 客户端**，必须配远端节点才能翻墙；gitport 不依赖任何远端节点，**直连 + 本地 IP 池 + 报文对抗**就能打通 GitHub 及可直连站点。
- **对我们的价值**：规则路由的「分站点到不同 outbound」模型可借鉴到 gitport 的 `pool_for` / `master_route`（已经是这个模型了）；但 gitport 把「outbound」实现成「本地 IP 池串发」而非「远端服务器」。

---

## 四、对 gitport 最终版的具体借鉴清单

| 来源 | 借鉴点 | gitport 落点 |
|---|---|---|
| byedpi/GoodbyeDPI | SNI 前 ClientHello 分片、逆序发包 | `server/mod.rs` `bidir_copy` 分段（已做，全局 `HELLO_SPLIT`） |
| gecit | MSS 钳制强割 ClientHello | 未来 Linux 部署增强项（Win 用分段） |
| dev-sidecar | 多站点分组、镜像兜底、测速切换 | `site.rs` 站点画像 + `route.rs` 站点池（已做）；镜像链 ECT 竞速（已有） |
| FastGithub | IP 优选 + SNI 改写直连 | GitHub 池白名单族 + SNI 伪装（已做） |
| clash/sing-box | 规则路由分出站 | `pool_for` / `master_route`（已是该模型，outbound=IP 池） |

**gitport 的差异化护城河**：本地零远端依赖 + Chrome TLS 指纹 + h2 连接池 + MITM 化解 GitHub 403，这套组合在「能直连但被 DPI 掐」的国情下，比纯镜像或纯分片都稳。扩展多站点时，**有 seeds 的站点走 IP 池串发，无 seeds 的站点走通用隧道 + 分片兜底**——这就是最终版的核心策略。

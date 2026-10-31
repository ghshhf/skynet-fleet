//! 站点能力画像：把 gitport 从「GitHub 专用」推广为「任意国外网站加速代理」。
//!
//! 每个国外站点一份 SiteProfile：域名模式、IP 种子表（DNS 污染免疫）、
//! SNI 伪装、ClientHello 分片参数、直连/镜像。加一个站点 = 在 sites.toml
//! 加一个 [[sites]] 条目，绝大多数不改代码。
//!
//! 路由优先级：direct（系统直连）> site 池（串发拨号，带 IP 种子）>
//! site 隧道（通用 CONNECT，带 TLS 指纹+分片）> 未知域隧道（纯 CONNECT）。

use serde::Deserialize;
use std::path::PathBuf;
use std::sync::OnceLock;

/// 单条 IP 种子：IP + 伪装 SNI（与校验名解耦，证书恒按目标 host 校验）。
#[derive(Debug, Clone, Deserialize, Default)]
pub struct IpSeed {
    pub ip: String,
    #[serde(default)]
    pub sni: String,
}

/// 站点能力画像。
#[derive(Debug, Clone, Deserialize, Default)]
pub struct SiteProfile {
    pub name: String,
    /// 域名模式（"*.x" 通配子域，否则全等），用于识别站点归属。
    #[serde(default)]
    pub domains: Vec<String>,
    /// IP 种子表（串发拨号候选），DNS 污染免疫。
    #[serde(default)]
    pub seeds: Vec<IpSeed>,
    /// SNI 伪装域名：空串 = 真名 SNI；非空 = 种子无独立 SNI 时的站点级默认伪装。
    #[serde(default)]
    pub sni_disguise: String,
    /// 是否对该站点开启 ClientHello 首包分片（byedpi 同款）。YouTube/Twitter 需开；
    /// HuggingFace/arXiv 等 Cloudflare 系关掉更稳（不分片）。
    #[serde(default)]
    pub split_hello: bool,
    /// 直连白名单：不归任何池、不经代理，交系统 DNS。
    #[serde(default)]
    pub direct: Vec<String>,
}

/// toml 反序列化目标。
#[derive(Debug, Clone, Deserialize, Default)]
struct SitesFile {
    #[serde(default)]
    sites: Vec<SiteDef>,
}

#[derive(Debug, Clone, Deserialize, Default)]
struct SiteDef {
    #[serde(default)]
    name: String,
    #[serde(default)]
    domains: Vec<String>,
    #[serde(default)]
    seeds: Vec<IpSeed>,
    #[serde(default)]
    sni_disguise: String,
    #[serde(default)]
    split_hello: bool,
    #[serde(default)]
    direct: Vec<String>,
}

/// 运行时站点画像表。
pub struct SiteTable {
    /// 按注册顺序保留；匹配时逐个试，先匹配先胜。
    pub profiles: Vec<SiteProfile>,
    /// 直连域名扁平表（全站点合并，去重）。
    pub direct_hosts: Vec<String>,
}

static SITE_TABLE: OnceLock<SiteTable> = OnceLock::new();

/// 定位 sites.toml：exe 同目录优先，否则当前目录。
fn toml_path() -> Option<PathBuf> {
    if let Some(dir) = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|p| p.to_path_buf()))
    {
        let p = dir.join("sites.toml");
        if p.exists() {
            return Some(p);
        }
    }
    let cwd = std::env::current_dir().ok()?;
    let p = cwd.join("sites.toml");
    p.exists().then_some(p)
}

/// 归并：内置默认（GitHub）+ sites.toml 追加/覆盖。
fn load_merged() -> SiteTable {
    let mut profiles: Vec<SiteProfile> = default_profiles();

    if let Some(path) = toml_path() {
        if let Ok(text) = std::fs::read_to_string(&path) {
            if let Ok(f) = toml::from_str::<SitesFile>(&text) {
                for def in &f.sites {
                    // 旧 sites.rs（GitHub 专用）的 [[sites]] 条目只含 groups/whitelist，
                    // 没有 domains/seeds——那是给 GitHub 分段扩展用的，不属于本系统。
                    // 跳过，避免用空 domains 覆盖内置 github 画像。
                    if def.domains.is_empty() && def.seeds.is_empty() {
                        continue;
                    }
                    let profile = SiteProfile {
                        name: def.name.clone(),
                        domains: def.domains.clone(),
                        seeds: def.seeds.clone(),
                        sni_disguise: def.sni_disguise.clone(),
                        split_hello: def.split_hello,
                        direct: def.direct.clone(),
                    };
                    // 按 name 覆盖同名内置 profile，否则追加。
                    if let Some(existing) = profiles.iter_mut().find(|p| p.name == def.name) {
                        *existing = profile;
                    } else {
                        profiles.push(profile);
                    }
                }
            } else {
                eprintln!("[site] 解析 {path:?} 失败，回退内置默认");
            }
        }
    }

    // 直连表从各站点画像的 direct 合并（含内置默认 + toml 覆盖/追加后的最终结果）。
    let mut direct_hosts: Vec<String> = default_direct_hosts();
    for p in &profiles {
        for h in &p.direct {
            if !direct_hosts.contains(h) {
                direct_hosts.push(h.clone());
            }
        }
    }

    SiteTable {
        profiles,
        direct_hosts,
    }
}

/// 全局站点画像表（首次访问时懒加载）。
pub fn global_site_table() -> &'static SiteTable {
    SITE_TABLE.get_or_init(load_merged)
}

/// 查找 host 归属的站点画像（先精确域名，后通配符）。
pub fn site_for_host(host: &str) -> Option<&'static SiteProfile> {
    let table = global_site_table();
    // 精确匹配优先。
    for p in &table.profiles {
        if p.domains.iter().any(|d| d == host) {
            return Some(p);
        }
    }
    // 通配符匹配（"*.x"）。
    for p in &table.profiles {
        if p.domains.iter().any(|d| pattern_match(host, d)) {
            return Some(p);
        }
    }
    None
}

/// 域名是否归某个站点管辖（用于路由分流）。
pub fn host_belongs_to_site(host: &str) -> bool {
    site_for_host(host).is_some()
}

/// 站点是否开启 ClientHello 分片。
pub fn site_split_hello(host: &str) -> Option<bool> {
    site_for_host(host).map(|p| p.split_hello)
}

/// 是否直连（不经代理）。
pub fn is_site_direct(host: &str) -> bool {
    global_site_table()
        .direct_hosts
        .iter()
        .any(|h| h == host)
}

/// `*.x` 匹配子域，否则全等。
fn pattern_match(host: &str, pat: &str) -> bool {
    match pat.strip_prefix("*.") {
        Some(suffix) => host
            .strip_suffix(suffix)
            .is_some_and(|h| h.ends_with('.')),
        None => host == pat,
    }
}

/// 内置默认站点：GitHub（与上游 config.rs 逐字一致，保证开箱即用）。
fn default_profiles() -> Vec<SiteProfile> {
    vec![
        SiteProfile {
            name: "github".into(),
            domains: vec![
                "github.com".into(),
                "*.github.com".into(),
                "api.github.com".into(),
                "githubapp.com".into(),
                "*.githubapp.com".into(),
                "*.githubassets.com".into(),
                "github.io".into(),
                "*.github.io".into(),
                "*.githubusercontent.com".into(),
            ],
            seeds: vec![],
            sni_disguise: "baidu.com".into(),
            split_hello: false,
            direct: vec![
                "objects-origin.githubusercontent.com".into(),
                "uploads.github.com".into(),
                "viewscreen.githubusercontent.com".into(),
                "api.mcp.github.com".into(),
            ],
        },
        SiteProfile {
            name: "youtube".into(),
            domains: vec![
                "youtube.com".into(),
                "*.youtube.com".into(),
                "ytimg.com".into(),
                "*.ytimg.com".into(),
                "googlevideo.com".into(),
                "*.googlevideo.com".into(),
                "ggpht.com".into(),
                "*.ggpht.com".into(),
                "googleapis.com".into(),
                "*.googleapis.com".into(),
            ],
            // ⚠️ 实测 (2026-10-31)：Google 系 IP 段在国内是**黑洞级封锁**，不是 SNI 封锁。
            //    随机采样 133 个 IP（142.250/142.251/172.217/216.58/74.125）测 :443，
            //    结果 0/133 可通（8.8.8.8:53 通，说明不是本机问题，是 Google 443/80 被整段掐）。
            //    ⇒ 分片、SNI 伪装、串发 IP 池**全部无效**，这条路物理不通。
            //    seeds 故意留空：避免串发去试一堆死 IP，让用户白等二十几秒才失败。
            //    要走 YouTube 只能靠远端节点（clash/sing-box + 机场），不是本工具的活。
            //    domains 保留：将来接上远端节点时，这层识别可直接当路由标记用。
            seeds: vec![],
            sni_disguise: "www.google.com".into(),
            split_hello: true,
            direct: vec![],
        },
        SiteProfile {
            name: "twitter".into(),
            domains: vec![
                "twitter.com".into(),
                "*.twitter.com".into(),
                "x.com".into(),
                "*.x.com".into(),
                "t.co".into(),
                "*.t.co".into(),
                "twimg.com".into(),
                "*.twimg.com".into(),
            ],
            // ⚠️ 实测 (2026-10-31)：X/Twitter 同样是 IP 段黑洞。
            //    6 个 IP（104.244.42.1/65/129/193、199.16.156.20、162.159.140.229）测 :443
            //    全部 TimeoutError，连 TCP 都建不起来，根本走不到 TLS 那一层。
            //    ⇒ 与 YouTube 同理，seeds 留空，只保留域名识别能力。
            seeds: vec![],
            sni_disguise: "abs.twimg.com".into(),
            split_hello: true,
            direct: vec![],
        },
        SiteProfile {
            name: "huggingface".into(),
            domains: vec![
                "huggingface.co".into(),
                "*.huggingface.co".into(),
                "cdn-us.huggingface.co".into(),
                "*.cdn-us.huggingface.co".into(),
                "hf.co".into(),
                "*.hf.co".into(),
            ],
            // ⚠️ 实测 (2026-10-31，多轮对抗测试)：HF 是「DNS 污染 + SNI 封锁 + 有状态 DPI」三重。
            //    证据链：
            //    ① IP 本身可达：cdn-lfs-us-1.hf.co / cas-bridge.xethub.hf.co 直连建 TLS 返 403。
            //    ② 同 IP 换 SNI 结果不同：3.170.19.119 + SNI=huggingface.co → RST；
            //       同 IP + SNI=baidu.com → TLS-Alert（握手层通）⇒ 确系 SNI 判定。
            //    ③ 但分片**全部无效**：首刀 1B/5B/16B、切 SNI 中段、三段式、gap 5/30/100ms
            //       —— 无一例外 RST；乱序发送则全部超时（被丢弃）。
            //    ④ 决定性证据：首刀 1B 后**等 3 秒**再发余下 —— 仍 RST。
            //       ⇒ 对端做完整 TCP 流重组，重组窗口 >3s，分片/乱序/延时全绕不过。
            //    ⇒ 本工具对 HF 无解。seeds 留空避免串发撞死 IP 白等。
            //    正解：国内镜像 hf-mirror.com（实测 API 200/0.58s、小文件 200/1.15s、
            //    大文件 1.63MB/s 波动大）或 ModelScope（实测 0.27s）。
            //    设置 HF_ENDPOINT=https://hf-mirror.com 即可，详见 fleet/netaccel.py。
            //    domains 保留：将来接远端节点时可直接当路由标记。
            seeds: vec![],
            sni_disguise: "".into(),
            split_hello: false,
            direct: vec![],
        },
        SiteProfile {
            name: "arxiv".into(),
            domains: vec![
                "arxiv.org".into(),
                "*.arxiv.org".into(),
                "arxiv.org".into(),
            ],
            seeds: vec![
                // 实测 (2026-10-31)：arXiv 早已从 Berkeley(131.243.x) 迁到 Fastly。
                // 这 4 个 IP 全部 TCP:443 可达（197~228ms）。
                // 且 arXiv 本身**国内直连就通**（实测 https://arxiv.org/ 200 / 37KB / 1.7s），
                // 画像存在的意义只是「偶尔 DNS 抖动时走 IP 池兜底」，不是必需。
                IpSeed { ip: "151.101.2.132".into(), sni: "arxiv.org".into() },
                IpSeed { ip: "151.101.66.132".into(), sni: "arxiv.org".into() },
                IpSeed { ip: "151.101.130.132".into(), sni: "arxiv.org".into() },
                IpSeed { ip: "151.101.194.132".into(), sni: "arxiv.org".into() },
            ],
            sni_disguise: "".into(),
            // 直连就通，无需分片（分片只在被 SNI RST 时才有意义）。
            split_hello: false,
            direct: vec![],
        },
    ]
}

fn default_direct_hosts() -> Vec<String> {
    vec![
        "objects-origin.githubusercontent.com".into(),
        "uploads.github.com".into(),
        "viewscreen.githubusercontent.com".into(),
        "api.mcp.github.com".into(),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_has_github_and_youtube() {
        let t = global_site_table();
        assert!(t.profiles.iter().any(|p| p.name == "github"));
        assert!(t.profiles.iter().any(|p| p.name == "youtube"));
    }

    #[test]
    fn site_for_host_youtube() {
        let p = site_for_host("www.youtube.com").unwrap();
        assert_eq!(p.name, "youtube");
        assert_eq!(p.sni_disguise, "www.google.com");
        assert!(p.split_hello);
    }

    #[test]
    fn site_for_host_github() {
        let p = site_for_host("api.github.com").unwrap();
        assert_eq!(p.name, "github");
        assert_eq!(p.sni_disguise, "baidu.com");
    }

    #[test]
    fn site_for_host_unknown() {
        assert!(site_for_host("example.com").is_none());
    }

    #[test]
    fn pattern_wildcard() {
        assert!(pattern_match("api.github.com", "*.github.com"));
        assert!(!pattern_match("github.com", "*.github.com"));
        assert!(!pattern_match("xgithub.com", "*.github.com"));
        assert!(pattern_match("www.youtube.com", "*.youtube.com"));
    }

    /// YouTube/Twitter 是 IP 黑洞（实测 0/133、0/6），seeds **故意为空**。
    /// 别「顺手修成非空」——那只会让串发去撞一堆死 IP，用户白等二十几秒。
    #[test]
    fn site_seeds_youtube_intentionally_empty() {
        let p = site_for_host("www.youtube.com").unwrap();
        assert!(p.seeds.is_empty(), "YouTube 是 IP 黑洞，seeds 必须为空");
        assert_eq!(p.sni_disguise, "www.google.com");
        assert!(p.split_hello);
    }

    /// HuggingFace 是「DNS 污染 + SNI 封锁 + 有状态 DPI 重组」三重封锁。
    /// 分片/乱序/延时全实测无效（首刀 1B 等 3s 仍 RST），seeds **故意为空**。
    /// 正解走 hf-mirror / ModelScope，别把种子加回来。
    #[test]
    fn site_seeds_huggingface_intentionally_empty() {
        let p = site_for_host("huggingface.co").unwrap();
        assert!(p.seeds.is_empty(), "HF 分片无效，seeds 必须为空");
        assert!(!p.split_hello, "HF 分片已被实测证伪，不应再开");
        assert_eq!(p.name, "huggingface");
    }

    /// arXiv 直连就通，种子只作 DNS 抖动时兜底。
    #[test]
    fn site_seeds_arxiv_present_no_split() {
        let p = site_for_host("arxiv.org").unwrap();
        assert!(!p.seeds.is_empty());
        assert!(!p.split_hello, "arXiv 直连就通，不需要分片");
    }

    #[test]
    fn is_site_direct_github() {
        assert!(is_site_direct("uploads.github.com"));
        assert!(!is_site_direct("www.youtube.com"));
    }
}

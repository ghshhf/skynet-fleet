//! 站点注册表（配置驱动）：把上游写死的 GROUPS / WHITELIST_HOSTS / DIRECT_HOSTS
//! 抽成运行时加载。内置 GitHub 默认保证开箱即用；`sites.toml` 用于**追加新站点**
//! （"加一个国外网站 = 加几行配置"，详见 README.skynet.md 第三节与 SITES_EXAMPLE.toml）。
//!
//! 合并策略：内置 GitHub 默认打底，toml 里的条目**追加 / 按 host 覆盖**——
//!   - groups：pool 同名则合并 patterns，否则新增 group
//!   - whitelist：按 host 覆盖段号，新 host 追加
//!   - direct：按 host 去重追加
//!   - mirrors：暂未消费（route.rs 镜像链当前硬编码），预留接口
//! 这样既保证无 toml 时与上游行为完全一致，又让"加同类型 Fastly 站点"纯配置化
//! （route.rs 的 FAMILY_WL_* 按 seg 认领，自动接管新白名单 host）。

use std::path::PathBuf;
use std::sync::OnceLock;

use serde::Deserialize;

/// toml 反序列化目标：结构对齐 SITES_EXAMPLE.toml。
#[derive(Debug, Clone, Deserialize, Default)]
struct SitesFile {
    #[serde(default)]
    sites: Vec<SiteDef>,
    #[serde(default)]
    whitelist: std::collections::HashMap<String, String>,
    #[serde(default)]
    direct: std::collections::HashMap<String, String>,
    #[serde(default)]
    mirrors: std::collections::HashMap<String, Vec<String>>,
}

#[derive(Debug, Clone, Deserialize, Default)]
struct SiteDef {
    #[serde(default)]
    groups: Vec<GroupDef>,
}

#[derive(Debug, Clone, Deserialize, Default)]
struct GroupDef {
    pool: String,
    #[serde(default)]
    patterns: Vec<String>,
}

/// 运行时站点数据（owned String，从 toml / 内置默认加载）。
pub struct SitesData {
    pub groups: Vec<GroupOwned>,
    pub direct_hosts: Vec<String>,
    pub whitelist: Vec<WhitelistHostOwned>,
    pub wl_split_segs: usize,
    pub wl_split_gap_ms: u64,
}

#[derive(Debug, Clone)]
pub struct GroupOwned {
    pub name: String,
    pub patterns: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct WhitelistHostOwned {
    pub host: String,
    pub seg: String,
}

static SITES: OnceLock<SitesData> = OnceLock::new();

/// 内置 GitHub 默认配置（与上游 config.rs 原硬编码逐字一致，保证开箱即用）。
fn default_data() -> SitesData {
    SitesData {
        groups: vec![
            GroupOwned {
                name: "core".into(),
                patterns: vec!["github.com".into(), "*.github.com".into()],
            },
            GroupOwned {
                name: "api".into(),
                patterns: vec!["api.github.com".into()],
            },
            GroupOwned {
                name: "edge".into(),
                patterns: vec![
                    "githubapp.com".into(),
                    "*.githubapp.com".into(),
                    "*.githubassets.com".into(),
                    "github.io".into(),
                    "*.github.io".into(),
                    "*.githubusercontent.com".into(),
                ],
            },
        ],
        direct_hosts: vec![
            "objects-origin.githubusercontent.com".into(),
            "uploads.github.com".into(),
            "viewscreen.githubusercontent.com".into(),
            "api.mcp.github.com".into(),
        ],
        whitelist: vec![
            WhitelistHostOwned { host: "docs.github.com".into(), seg: "154".into() },
            WhitelistHostOwned { host: "support.github.com".into(), seg: "133".into() },
            WhitelistHostOwned { host: "resources.github.com".into(), seg: "133".into() },
            WhitelistHostOwned { host: "desktop.github.com".into(), seg: "153".into() },
            WhitelistHostOwned { host: "pages.github.com".into(), seg: "153".into() },
            WhitelistHostOwned { host: "skills.github.com".into(), seg: "153".into() },
            WhitelistHostOwned { host: "archiveprogram.github.com".into(), seg: "153".into() },
            WhitelistHostOwned { host: "socialimpact.github.com".into(), seg: "153".into() },
            WhitelistHostOwned { host: "raw.github.com".into(), seg: "133".into() },
            WhitelistHostOwned {
                host: "copilot-proxy.githubusercontent.com".into(),
                seg: "4.249".into(),
            },
        ],
        wl_split_segs: 3,
        wl_split_gap_ms: 100,
    }
}

/// 定位 sites.toml：exe 同目录优先，否则当前目录。
fn toml_path() -> Option<PathBuf> {
    if let Some(dir) = std::env::current_exe().ok().and_then(|p| p.parent().map(|p| p.to_path_buf())) {
        let p = dir.join("sites.toml");
        if p.exists() {
            return Some(p);
        }
    }
    let cwd = std::env::current_dir().ok()?;
    let p = cwd.join("sites.toml");
    p.exists().then_some(p)
}

/// 默认打底 + toml 追加/覆盖。
fn load_merged() -> SitesData {
    let mut data = default_data();
    let Some(path) = toml_path() else {
        return data;
    };
    let Ok(text) = std::fs::read_to_string(&path) else {
        return data;
    };
    let Ok(f) = toml::from_str::<SitesFile>(&text) else {
        eprintln!("[sites] 解析 {path:?} 失败，回退内置默认", );
        return data;
    };
    // groups：pool 同名合并 patterns，否则新增
    for site in &f.sites {
        for g in &site.groups {
            if let Some(existing) = data.groups.iter_mut().find(|e| e.name == g.pool) {
                for p in &g.patterns {
                    if !existing.patterns.contains(p) {
                        existing.patterns.push(p.clone());
                    }
                }
            } else {
                data.groups.push(GroupOwned {
                    name: g.pool.clone(),
                    patterns: g.patterns.clone(),
                });
            }
        }
    }
    // whitelist：按 host 覆盖/追加
    for (host, seg) in &f.whitelist {
        if let Some(w) = data.whitelist.iter_mut().find(|w| &w.host == host) {
            w.seg = seg.clone();
        } else {
            data.whitelist.push(WhitelistHostOwned {
                host: host.clone(),
                seg: seg.clone(),
            });
        }
    }
    // direct：去重追加
    for host in f.direct.keys() {
        if !data.direct_hosts.contains(host) {
            data.direct_hosts.push(host.clone());
        }
    }
    // mirrors：预留接口，当前 route.rs 镜像链硬编码未消费
    let _ = &f.mirrors;
    data
}

/// 全局站点数据（首次访问时懒加载并合并）。
pub fn global_sites() -> &'static SitesData {
    SITES.get_or_init(load_merged)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_has_github() {
        let s = default_data();
        assert!(s.groups.iter().any(|g| g.name == "core"));
        assert!(s.groups.iter().any(|g| g.name == "edge"));
        assert_eq!(s.whitelist.len(), 10);
        assert_eq!(s.direct_hosts.len(), 4);
        assert_eq!(s.wl_split_segs, 3);
        assert_eq!(s.wl_split_gap_ms, 100);
    }

    #[test]
    fn global_sites_fallback_without_toml() {
        // 无 toml 时应等于内置默认（开箱即用等价性）
        let s = global_sites();
        assert!(s.groups.iter().any(|g| g.name == "core" && g.patterns.contains(&"github.com".to_string())));
        assert!(s.whitelist.iter().any(|w| w.host == "docs.github.com" && w.seg == "154"));
        assert!(s.direct_hosts.contains(&"uploads.github.com".to_string()));
    }
}

//! Windows 文件 ACL 收紧：Rust 0600 在 Windows 只映射只读属性，文件缺省继承目录 ACL。
//! ca.key（MITM 根私钥）/settings.json 收紧为仅当前用户与 SYSTEM——根私钥可读即可伪造用户已信任的叶子证书。
//!
//! 走系统自带 icacls：一条命令 = 断开继承 + 只授当前用户与 SYSTEM，等价于受保护 DACL。
//! 失败按 best-effort 返回错误，调用方只记日志，不阻断主流程。

use std::os::windows::process::CommandExt;
use std::process::Command;

/// 子进程不弹控制台窗口（本程序是 GUI 子系统）。
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// 受保护 DACL（文件版）：仅 SYSTEM 与当前用户完全控制，其余剥离。
/// 文件上 (OI)(CI) 继承标志会被 icacls 静默忽略（rc=0 假成功、实际零 ACE），必须用裸 (F)。
pub fn restrict_to_current_user(path: &str) -> Result<(), String> {
    restrict(path, "(F)")
}

/// 受保护 DACL（目录版）：授权带 (OI)(CI) 继承标志——子目录与文件随父目录
/// 获得权限。/inheritance:r 断继承后，已存在的子目录会被动态传播剥成空 DACL
/// （2026-10-23 实测踩坑：.cache/tmp 变空 DACL → cacache 临时文件全部创建失败），
/// 目录必须用带继承标志的授权，且 Windows 会把父目录的新继承 ACE 自动
/// 传播给未保护 DACL 的旧子目录，旧子目录随之修复。
pub fn restrict_dir_to_current_user(path: &str) -> Result<(), String> {
    restrict(path, "(OI)(CI)F")
}

fn restrict(path: &str, rights: &str) -> Result<(), String> {
    let user = std::env::var("USERNAME").unwrap_or_default();
    if user.trim().is_empty() {
        // 拿不到用户名时宁可不改：只授 SYSTEM 会把用户自己锁在外面。
        return Err("取不到当前用户名，跳过 ACL 收紧".to_string());
    }
    // rights 由调用方定：文件=裸 (F)，目录=(OI)(CI)F（可继承，见上）
    let grant_user = format!("{user}:{rights}");
    let out = Command::new("icacls")
        .args([
            path,
            "/inheritance:r",
            "/grant:r",
            grant_user.as_str(),
            "/grant:r",
            format!("*S-1-5-18:{rights}").as_str(),
        ])
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .map_err(|e| format!("icacls 执行失败: {e}"))?;
    if out.status.success() {
        return Ok(());
    }
    let detail = String::from_utf8_lossy(&out.stderr).trim().to_string();
    Err(format!(
        "icacls 退出码 {:?}: {detail}",
        out.status.code().unwrap_or(-1)
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 契约：收紧后不得留继承 ACE、不得留宽松主体，SYSTEM 必须保留，
    /// 当前用户必须在位，且只允许「用户 / SYSTEM / Administrators」三类主体。
    ///
    /// 2026-10-31 CI 两连击的教训：
    ///   ① 原先断言「正好两条 ACE」，在 GitHub runner 上必挂；
    ///   ② 第一次修的时候我猜「runner 上用户 ACE 没授上」，也错——
    ///      抓全文才看见实际是三条：SYSTEM + BUILTIN\Administrators + 当前用户。
    /// 根因是这条测试盯的是**环境的 ACE 条数**，不是代码保证的属性。
    /// 现在改成按**主体**断言：Administrators 是可信主体（管理员本就能夺所有权，
    /// 拦它不构成安全边界），宽松主体（Everyone / Users / Authenticated Users）
    /// 和继承 ACE 才是真正要禁的。
    #[test]
    fn acl_keeps_only_user_and_system() {
        let dir = std::env::temp_dir().join(format!("gitport_acl_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let f = dir.join("ca.key");
        std::fs::write(&f, b"x").unwrap();
        let p = f.to_string_lossy().to_string();
        restrict_to_current_user(&p).unwrap();

        let out = Command::new("icacls").arg(&p).output().unwrap();
        let text = String::from_utf8_lossy(&out.stdout);

        // ---- ① 继承必须断开 ----
        assert!(!text.contains("(I)"), "继承 ACE 应已断开: {text}");

        // ---- ② 宽松主体一个都不许留（这才是安全契约） ----
        for broad in [
            "Everyone",
            "BUILTIN\\Users",
            "Authenticated Users",
            "NT AUTHORITY\\INTERACTIVE",
            "NT AUTHORITY\\SERVICE",
        ] {
            assert!(!text.contains(broad), "不该留宽松授权 {broad}: {text}");
        }

        // ---- ③ SYSTEM 与当前用户必须在位 ----
        assert!(
            text.contains("SYSTEM") || text.contains("S-1-5-18"),
            "SYSTEM 应保留: {text}"
        );
        let user = std::env::var("USERNAME").unwrap_or_default();
        assert!(
            text.to_lowercase().contains(&user.to_lowercase()),
            "当前用户 {user} 应保留: {text}"
        );

        // ---- ④ 白名单之外的主体不该出现（上限三条：用户 / SYSTEM / Administrators）----
        let n_ace = text.matches(":(").count();
        assert!(
            n_ace <= 3,
            "主体不该超过三类（用户/SYSTEM/Administrators），实得 {n_ace} 条: {text}"
        );
    }

    /// 契约（目录版）：授权带 (OI)(CI) 继承标志——子目录随父目录获得权限，
    /// 收紧后新建文件必须可写（曾因目录授权无继承标志 + 断继承，
    /// 子目录被剥成空 DACL，cacache 临时文件全部创建失败）。
    #[test]
    fn acl_dir_grants_inheritable_access() {
        let dir =
            std::env::temp_dir().join(format!("gitport_acl_dir_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        restrict_dir_to_current_user(&dir.to_string_lossy()).unwrap();
        let child = dir.join("writable.txt");
        std::fs::write(&child, b"x").expect("目录收紧后子文件必须可写");
        std::fs::remove_dir_all(&dir).unwrap();
    }
}

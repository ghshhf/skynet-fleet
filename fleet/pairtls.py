"""配对 TLS（N+1b 第 2 步）：自签证书生成 + SHA-256 指纹。

为什么不用 cryptography 库现成生成：依赖表里 cryptography 是声明了，
但本机解释器没装（2026-10-22 实测 pip list 无此项）。TLS 传输用 stdlib
`ssl` 零新增依赖；证书生成走系统 openssl CLI——Git for Windows 自带
（usr/bin/openssl.exe）。**找不到 openssl 就明说找不到**，配对模式
拒绝启用，不静默降级成明文。

指纹（cert_fp） = 证书 DER 的 SHA-256 十六进制（64 字符）。配对链接
携带前 8 位，主控端连上前比对——防中间人冒充。服务端在会话里记全量
指纹，N+1b-2 的受控端点按它钉扎。

全部门禁离线可测：gen_selfsigned 依赖外部 CLI 的部分单独隔出来，
没 openssl 时门禁打印「跳过 TLS 活线（缺 openssl）」而不是假装过。
"""
import hashlib
import os
import shutil
import subprocess
import tempfile


class PairTlsError(RuntimeError):
    """证书相关失败。err 必须是人能照着做的话（缺什么、去哪找）。"""


def _openssl_env():
    """openssl CLI 一启动就读 OPENSSL_CONF；没设时落到它**编译期内嵌**的
    OPENSSLDIR——那可能是构建机的盘符（2026-11-01 实测：Strawberry 的
    openssl 3.6.1 内嵌 Z:/extlib/...，本机没 Z 盘 ⇒ 任何命令都先
    fopen(Z:...) 失败）。对策：备一份最小 conf（req 要的
    distinguished_name 空段），只在「用户没设」或「设的文件不存在」时
    接管；用户自己的合法配置优先。返回要注入的路径或 None。"""
    cur = os.environ.get("OPENSSL_CONF")
    if cur and os.path.isfile(cur):
        return None
    path = os.path.join(tempfile.gettempdir(), "fleet-openssl-min.cnf")
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("[req]\ndistinguished_name = dn\n[dn]\n")
    return path


#: openssl 相对怎么摆。**不写盘符**：盘符由 fleet/locate.py 运行时枚举，
#: 与这些结构做全组合。Git for Windows 的 usr/bin 是主源，
#: WorkBuddy 自带 PortableGit 也认——各机器装哪不一样，不该靠硬编码列。
_OPENSSL_HINTS = (
    "Git/usr/bin",
    "Program Files/Git/usr/bin",
    "Program Files (x86)/Git/usr/bin",
    "Program Files/WorkBuddy/resources/vendor/PortableGit/usr/bin",
    "Git/bin",
)


def find_openssl():
    """找 openssl.exe。返回路径；找不到返回 None（调用方负责明说）。

    顺序：FLEET_OPENSSL 环境变量 → PATH → hints×本机盘符 → MSYS 的 /bin/openssl。
    """
    from . import locate as L
    return L.exe("openssl", env="FLEET_OPENSSL", hints=_OPENSSL_HINTS) or _msys_openssl()


def _msys_openssl():
    """MSYS 视角的 /bin/openssl（Git Bash 里有，Windows 路径要换算）。"""
    try:
        r = subprocess.run(["cygpath", "-w", "/bin/openssl"],
                           capture_output=True, timeout=5)
        if r.returncode == 0:
            w = r.stdout.decode("utf-8", "replace").strip().strip('"')
            if w and os.path.isfile(w):
                return w
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _run(argv):
    conf = _openssl_env()
    env = dict(os.environ, OPENSSL_CONF=conf) if conf else None
    r = subprocess.run(argv, capture_output=True, timeout=30, env=env)
    if r.returncode != 0:
        raise PairTlsError("openssl 失败（rc=%d）：%s"
                           % (r.returncode,
                              r.stderr.decode("utf-8", "replace")[:300]))
    return r.stdout


def gen_selfsigned(out_dir, san_ips=("127.0.0.1",), days=3650):
    """在 out_dir 生成自签证书。返回 {cert, key, fp, cn}。

    san_ips：subjectAltName 的 IP 列表（配对监听器绑的地址都要进，
    否则主控端按 IP 连会被 TLS 主机名校验拒——自签场景关掉主机名校验、
    只钉指纹，但 SAN 还是该写全，不给将来留坑）。
    失败抛 PairTlsError，原因照原样带回（不静默）。
    """
    ossl = find_openssl()
    if not ossl:
        raise PairTlsError(
            "找不到 openssl.exe（找过 PATH、常见 Git 安装目录、cygpath）。"
            "配对模式需要它生成自签证书；请安装 Git for Windows 后重试，"
            "或先不启用配对（明文仅限 127.0.0.1 本机自用）。")
    os.makedirs(out_dir, exist_ok=True)
    cert = os.path.join(out_dir, "pair_cert.pem")
    key = os.path.join(out_dir, "pair_key.pem")
    cn = "fleet-pair-%s" % hashlib.sha256(
        os.urandom(16)).hexdigest()[:12]
    san = ",".join("IP:%s" % ip for ip in san_ips) or "IP:127.0.0.1"
    _run([ossl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
          "-keyout", key, "-out", cert, "-days", str(int(days)),
          "-subj", "/CN=%s" % cn, "-addext",
          "subjectAltName=%s" % san])
    if not (os.path.isfile(cert) and os.path.isfile(key)):
        raise PairTlsError("openssl 报成功但证书文件不在：%s / %s" % (cert, key))
    return {"cert": cert, "key": key, "fp": cert_fingerprint(cert), "cn": cn}


def cert_fingerprint(cert_path):
    """证书 DER 的 SHA-256 十六进制（64 字符）。与 openssl x509 -fingerprint
    -sha256 的输出同值（门禁交叉验过两条路径一致）。"""
    der = _run([find_openssl() or "openssl", "x509",
                "-in", cert_path, "-outform", "der"])
    return hashlib.sha256(der).hexdigest()

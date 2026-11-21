// 天网 · gitport PAC：仅登记的外国站点走本地加速代理，其余直连。
function FindProxyForURL(url, host) {
    if (shExpMatch(host, "github.com")) { return "PROXY 127.0.0.1:7810"; }
    if (shExpMatch(host, "*.github.com") || shExpMatch(host, "github.com")) { return "PROXY 127.0.0.1:7810"; }
    if (shExpMatch(host, "githubusercontent.com")) { return "PROXY 127.0.0.1:7810"; }
    if (shExpMatch(host, "*.githubusercontent.com") || shExpMatch(host, "githubusercontent.com")) { return "PROXY 127.0.0.1:7810"; }
    if (shExpMatch(host, "arxiv.org")) { return "PROXY 127.0.0.1:7810"; }
    if (shExpMatch(host, "*.arxiv.org") || shExpMatch(host, "arxiv.org")) { return "PROXY 127.0.0.1:7810"; }
    if (shExpMatch(host, "export.arxiv.org")) { return "PROXY 127.0.0.1:7810"; }
    if (shExpMatch(host, "api.github.com")) { return "PROXY 127.0.0.1:7810"; }
    if (shExpMatch(host, "githubapp.com")) { return "PROXY 127.0.0.1:7810"; }
    if (shExpMatch(host, "*.githubapp.com") || shExpMatch(host, "githubapp.com")) { return "PROXY 127.0.0.1:7810"; }
    if (shExpMatch(host, "*.githubassets.com") || shExpMatch(host, "githubassets.com")) { return "PROXY 127.0.0.1:7810"; }
    if (shExpMatch(host, "github.io")) { return "PROXY 127.0.0.1:7810"; }
    if (shExpMatch(host, "*.github.io") || shExpMatch(host, "github.io")) { return "PROXY 127.0.0.1:7810"; }
    return "DIRECT";
}

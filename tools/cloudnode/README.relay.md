# wsrelay · 用自己的门牌号做出口

## 一句话

把一台**墙外**的免费机器挂在**没被点名的域名**下，本机的流量经 WebSocket 送到它那里再出去。
本机不需要装任何代理客户端，热点下的手机填一个代理地址就完事。

## 为什么必须是这个形状

2026-10-31 实测结论（`tools/platform_domain_probe.py`）：

| 类别 | 结果 |
|---|---|
| 公共 Invidious 镜像 13 个 | **全黑洞**（TCP 超时） |
| 免费平台的部署域名随机子域 | **10 个通**：github.io / pythonanywhere / netlify.app / up.railway.app / hf.space / replit.app / koyeb.app / onrender.com / glitch.me / surge.sh |

两条放在一起，规律只剩一个解释：
**公开且有流量价值的域名会被点名封锁；平台级根域名封了会误伤几百万正常站点，所以不封。**

所以「免费代理总失效」的真正原因是「公开 = 进名单」，不是速度也不是钱。
反过来，**你自己部署出来的随机子域名，不在任何名单上**。

## 为什么用 WebSocket

PaaS 只给 HTTP 反向代理，而代理需要裸 TCP（CONNECT）。把 TCP 装进 WS 就解决了——
所有 PaaS 都支持 WebSocket 长连接。

```
本机 proxy.py ──WS(TLS)──→ wsrelay（墙外）──TCP──→ 目标站
             SNI=xxx.onrender.com（没被点名）
```

## 部署（以 Render 为例，10 分钟）

1. 仓库推到 GitHub（公开仓库的 Actions/额度不限）。
2. Render 控制台 → **New → Blueprint** → 选本仓库（它会读 `tools/cloudnode/render.yaml`）。
3. 部署完拿到地址，形如 `https://skynet-wsrelay-abcd.onrender.com`。
4. 先验证它活着（浏览器打开应该看到 `{"ok":true,"service":"skynet-wsrelay"}`）。
5. 本机接上：

```bash
# 单机用：代理监听 7822，上游走隧道
python tools/cloudnode/proxy.py --port 7822 --upstream-ws wss://skynet-wsrelay-abcd.onrender.com/tunnel

# 热点网关用（热点下的设备填 192.168.137.1:7822 即可）
set FLEET_WSRELAY=wss://skynet-wsrelay-abcd.onrender.com/tunnel
python fleet/netaccel.py gateway
```

换平台只改 `startCommand`（Procfile 已备好），`wsrelay.py` 一行不用动。

## 免费档的真实限制（照实写）

| 限制 | 影响 |
|---|---|
| 512MB 内存 / 0.1 CPU | 够转发，不够转码 |
| 15 分钟无请求休眠，冷启动约 30 秒 | 看视频没问题（一直在传），当日常浏览器代理会频繁等唤醒 |
| 每月 750 小时 | 单实例基本够 |
| 出口是数据中心 IP | YouTube 可能要求登录验证——**这是 YouTube 的策略，不是隧道的缺陷**，换任何云主机都一样 |

## 已验证 / 未验证

已验证（本机可复现）：
- `wsrelay.py --selftest` 3/3：WS↔TCP 40B 完整往返；
- 经隧道访问 `https://pypi.org` → 200，证书校验通过；
- 经隧道访问 `https://github.com` → 000。**这条不是失败，是证明**：
  中转在本地时出墙仍旧撞墙，说明成败只取决于中转机器在不在墙外；
- 隧道挂掉时代理自动回落（pypi 仍 200/0.67s），不会让热点整体断网。

未验证（需要一台真实的墙外机器）：
- 部署到 Render 后能否访问 YouTube——**取决于那台机器的出口 IP 是否被 YouTube 判定为可疑数据中心**。
  这一条只能在部署后实测，任何"应该可以"的说法都不作数。

## 安全提醒

这个隧道是**开放代理**：任何知道地址的人都能拿它上网。
上线后建议：
- 在 `ws_handshake` 之后加一个 `?token=` 校验（几行代码的事）；
- 或者只在自己用的时段开，用完就删服务。

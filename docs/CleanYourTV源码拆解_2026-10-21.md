# CleanYourTV 源码拆解（2026-10-21）

> 目标仓库：`https://github.com/Gathin23/cleanyourtv`（原名 tv-vibehack）
> 本地只读副本：`D:/code/device-fleet/tmp/cleanyourtv/`（`git clone --depth 1`，**未做任何改动**）
> 真实协议实现处：`adb_shell==0.4.4` 源码，位于 `D:/code/device-fleet/tmp/adbshell_dl/adb_shell-0.4.4/`
> 报告目的：为 `D:/code/device-fleet` 的 `fleet/kinds/adb_wifi.py` 提供可直接照做的实现依据。

---

## 0. 结论先行

### 0.1 ⚠️ 纠正一：`tools/tv.py` **不是**自研的「零依赖纯 Python ADB 客户端」

- `tools/tv.py` 全文只有 **92 行**，是一个**薄封装**；真正的 ADB 协议实现全部来自 PyPI 第三方包 `adb-shell==0.4.4`。
  - 来源：`tools/tv.py:14-16`
    ```python
    from adb_shell.adb_device import AdbDeviceTcp
    from adb_shell.auth.keygen import keygen
    from adb_shell.auth.sign_pythonrsa import PythonRSASigner
    ```
- 依赖清单 `tools/requirements.txt`：`adb-shell==0.4.4`、`rsa==4.9.1`、`cryptography>=46.0.7` —— **3 个外部依赖**，不是零依赖。
- `README.md:246` 把它描述为 “Pure-Python ADB client”，**措辞不严谨**（是「纯 Python 调用」，但依赖 `adb_shell`）。
- **对本项目的意义**：「这个仓库自带了一个可以直接抄的零依赖 ADB 客户端」——**该前提不成立**。但收益是：协议实现有成熟、Apache-2.0、已被大量生产使用的参考（`adb_shell`），我们不需要从零手搓协议。

### 0.2 ⚠️ 纠正二：ADB 协议细节与常见预设**全部不同**（照着写代码的部分）

| 项 | 任务书预设 | **源码实测真相** | 来源 |
|---|---|---|---|
| header 大小 | 8 字节 | **24 字节**（6 × uint32） | `adb_shell/constants.py:101` `MESSAGE_FORMAT = b'<6I'` |
| 字节序 | 大端 | **小端** | 同上 |
| magic | 固定 `0x4e584e43` | **`command ^ 0xFFFFFFFF`**（每条命令不同） | `adb_shell/adb_message.py:157` |
| data_check | `zlib.crc32` | **payload 全部字节求和 & 0xFFFFFFFF** | `adb_shell/adb_message.py:42-72`（第 72 行 `return total & 0xFFFFFFFF`） |
| `host:connect:<ip>:<port>` | 需实现 | **不存在**：直连设备 5555 端口，不经过 adb server(5037) | `adb_shell/adb_device.py:1474-1508`（`AdbDeviceTcp`） |

> `0x4e584e43` 这个数字本身没错——它恰好是 **CNXN 这条命令的 wire 整数值**（`'C' | 'N'<<8 | 'X'<<16 | 'N'<<24`），但它是 `command` 字段，**不是** `magic` 字段。`adb_shell/constants.py:86` 的 `ID_TO_WIRE` 就是这么算的。

### 0.3 ⚠️ 纠正三：这个仓库真正值钱的**不是 ADB 协议，而是「安全作业纪律」**

1. **停用前先落盘恢复记录**，记录写失败就中止，**绝不动设备**（`tools/disable.sh:125-128`）。
2. **package 状态三分法** `absent` / `disabled` / `enabled`，恢复时只 `pm enable` 原本 enabled 的（`tools/recovery.py:146-149`、`tools/recovery.py:236-240`）。
3. **三道「会砖机」的护栏**：setupwraith（会永久掉 Wi-Fi）、Home owner（会 crash-loop system_server）、`TV_INPUT_HARDWARE`（HDMI 永久黑屏，**不可逆**）。

这三件套与本项目「失败必须带原因」「保留可逆性」的硬要求高度同构，**值得借鉴设计**（重写代码，不逐字抄）。

### 0.4 落地结论（明确建议）

> **不要自研完整 ADB 客户端，也不要纯靠 `adb.exe`。**
> 采用**双传输层**：`adb.exe` 优先（装了 platform-tools 的机器上最稳、错误信息最全），`adb_shell` 作为 **fallback**（无 adb.exe / macOS 被 Local Network 隐私拦截 / 需要免依赖打包时）。两者之上写**统一的能力层**（`adb.wifi.*`），不把协议细节泄漏到能力层。
>
> 理由：ADB 协议本体的坑（magic 逐命令异值、checksum 是字节和不是 CRC32、AUTH 时序、Android 7 前的 4096 字节 open payload 上限、filesync 状态机）**每一个都会静默失败**，自研验证成本远高于引入一个 Apache-2.0 成熟依赖；而 `adb.exe` 在 Windows 上不一定存在、不能假设用户装了、错误信息不可控（退出码 + stderr 文本），不满足团队「失败必须带原因」的硬要求。详见第 8 章。

---

## 1. ADB 协议完整实现（照着写代码的部分）

> 权威来源：`adb_shell 0.4.4` 源码（Apache-2.0），与 AOSP `system/core/adb` 一致。
> 字节布局由 `adb_shell/constants.py` + `adb_shell/adb_message.py` 交叉确认；magic、checksum、`A_VERSION_SKIP_CHECKSUM` 三处易错点另用 AOSP `adb/adb.h` 与 `adb/transport.cpp` 校验。

### 1.1 报文头：24 字节 = 6 × uint32，小端

来源：`adb_shell/constants.py:101`（`MESSAGE_FORMAT = b'<6I'`）、`adb_shell/constants.py:116`（`MESSAGE_SIZE = struct.calcsize(MESSAGE_FORMAT)` = 24）。

| 偏移 | 长度 | 字段 | 含义 |
|---|---|---|---|
| 0 | 4 | `command` | 4 个 ASCII 字节，如 `b'CNXN'`。wire 上是**小端 uint32** |
| 4 | 4 | `arg0` | 语义随 command 变化（见 1.4） |
| 8 | 4 | `arg1` | 语义随 command 变化（见 1.4） |
| 12 | 4 | `data_length` | 后随 payload 字节数 |
| 16 | 4 | `data_check` | payload 校验值，见 1.2 |
| 20 | 4 | `magic` | **`command ^ 0xFFFFFFFF`** |

`adb_shell/adb_message.py:162-171`（打包）与 `:157`（magic）：

```python
self.magic = self.command ^ 0xFFFFFFFF
...
def pack(self):
    return struct.pack(constants.MESSAGE_FORMAT, self.command, self.arg0, self.arg1,
                       len(self.data), self.checksum, self.magic)
```

**AOSP 侧交叉验证**：AOSP `adb/adb.h` 定义 `A_VERSION = 0x01000001`、`A_VERSION_SKIP_CHECKSUM = 0x01000001`、`A_VERSION_MIN = 0x01000000`；AOSP `adb/transport.cpp` 的 `check_header()` 用 `calculate_apacket_checksum()` 校验 payload，用 `command ^ 0xffffffff` 校验 magic。**与 `adb_shell` 完全一致。**

**发送顺序**（`adb_shell/adb_device.py:503-524` `_send`）：先 `bulk_write(header)`，再 `bulk_write(payload)`——**两次独立写**。所以读端必须先读满 24 字节 header，再按 `data_length` 读满 payload，**不能假设一次 recv 拿到整包**。

### 1.2 `data_check`（checksum）是「字节求和」，不是 CRC32

来源：`adb_shell/adb_message.py:42-72`，其注释原文：

> “The checksum is just a sum of all the bytes. I swear.”

等价伪代码（`adb_message.py:56-72`）：

```python
def checksum(data: bytes) -> int:
    # bytearray: total = sum(data)
    # bytes (py3): total = sum(data)     # data[i] 直接是 int
    total = sum(data)
    return total & 0xFFFFFFFF
```

（`adb_message.py:58` 对 bytearray 直接 `sum(data)`；`:66` 对 py3 bytes 也是 `sum(data)`；`:72` 取掩码。Python 2 兼容分支在 `:64`。）

**AOSP 侧**：`adb/transport.cpp` 的 `calculate_apacket_checksum()` 同样是「所有字节求和 & 0xFFFFFFFF」。

**版本与 checksum 豁免（重要）**：
- `adb_shell` 用 `VERSION = 0x01000000`（`adb_shell/constants.py:40`），即 **A_VERSION_MIN**（老版本），所以它**仍然计算并强制校验** checksum。
- AOSP 从 2017-12 起用 `A_VERSION = 0x01000001`；当对端版本 ≥ `A_VERSION_SKIP_CHECKSUM (0x01000001)` 时，adbd 允许 `data_check == 0`（跳过校验）。
- **建议照 `adb_shell` 做法：始终填真实字节和**——两端通吃（老设备也接受非零 checksum）。填 0 只对 ≥0x01000001 的设备合法，**对 Android 5/6 电视不合法**。

### 1.3 连接流程（TCP 直连 5555，不经 adb server）

**没有 `host:connect:` 序列**：`AdbDeviceTcp` 直接 `socket.create_connection((host, port))` 后自己实现协议。
来源：`adb_shell/transport/tcp_transport.py:88`；`adb_shell/adb_device.py:1474-1508`。

**握手时序**（`adb_shell/adb_device.py:165-271`）：

```
client                                   device
  |--- TCP connect (host:5555) ----------->|
  |--- CNXN(arg0=0x01000000,             ->|
  |      arg1=1048576,                   |
  |      payload=b'host::\0')            |
  |                                       |   (无认证的老设备直接回 CNXN)
  |<-- CNXN(arg0=dev_version,            --|
  |      arg1=dev_maxdata,               |
  |      payload=b'device::\0')          |
```

客户端 CNXN（`adb_shell/adb_device.py:224`）：

```python
msg = AdbMessage(constants.CNXN, constants.VERSION, constants.MAX_ADB_DATA, b'host::%s\0' % banner)
```

`constants.VERSION = 0x01000000`（`constants.py:40`），`constants.MAX_ADB_DATA = 1024*1024`（`constants.py:43`）。
读到 `AUTH` 或 `CNXN`（`adb_device.py:228`）；若直接收到 `CNXN` → 认证通过（`adb_device.py:231-232`）。

**AUTH 流程**（`adb_shell/adb_device.py:235-271`）：

| 步骤 | 方向 | 报文 | 语义 |
|---|---|---|---|
| 1 | device → client | `AUTH(arg0=1)` | `AUTH_TOKEN`，payload = 20 字节随机 token |
| 2 | client → device | `AUTH(arg0=2, payload=签名)` | `AUTH_SIGNATURE`，payload = RSA-SHA1(token) 签名 |
| 3 | device → client | `CNXN` | 签名被接受，握手完成 |
| 3' | device → client | `AUTH(arg0=3, payload=pubkey+'\0')` | `AUTH_RSAPUBLICKEY`，电视弹「允许 USB 调试？」，等用户点 |
| 4 | client → device | `AUTH(arg0=2, payload=用该私钥签的 token)` | 用户点「允许」后 adbd 换发新 token，客户端重新签名 |
| 5 | device → client | `CNXN` | 完成 |

- 常量：`adb_shell/constants.py:56/59/62`（`AUTH_TOKEN=1` / `AUTH_SIGNATURE=2` / `AUTH_RSAPUBLICKEY=3`）。
- 签名算法：`adb_shell/auth/sign_pythonrsa.py` 用 `rsa.sign(data, priv_key, 'SHA-1-PREHASHED')`。
- 公钥格式：`adb_shell/auth/keygen.py` 生成 Android 自定义 RSA 公钥二进制格式（小端 32-bit words，RSA-2048 / e=65537）。
- **无 `rsa_keys` 时**直接抛 `DeviceAuthError('Device authentication required, no keys available.')`（`adb_device.py:237-238`）——这是「认证被拒」的可编程信号。
- **超时**：`auth_timeout_s` 默认 10 秒（`constants.py:119`）；`tools/tv.py:54` 传 120 秒。

### 1.4 切 shell 模式：`OPEN` → `OKAY` → `WRTE`/`OKAY` → `CLSE`

```
client(local_id=L)                         device(remote_id=R)
  |--- OPEN(arg0=L, arg1=0,                ->|
  |      payload=b'shell:<cmd>\0')         |
  |<-- OKAY(arg0=R, arg1=L)               --|   ← 设备分配 remote_id
  |<-- WRTE(arg0=R, arg1=L, payload=out)  --|   设备发数据
  |--- OKAY(arg0=L, arg1=R)              ->|   ← 客户端 ACK（**必须回**）
  |    ... 重复 WRTE/OKAY ...
  |<-- CLSE(arg0=R, arg1=L)               --|   设备结束
  |--- CLSE(arg0=L, arg1=R)              ->|   客户端回 CLSE，结束
```

**逐字段语义**：

| 报文 | arg0 | arg1 | payload | 来源 |
|---|---|---|---|---|
| `OPEN` | **本地 id**（自增，从 1 起） | 0 | `service:command` + `\0` | `adb_device.py:1186-1188` |
| `OKAY`（设备→我们） | 设备分配的 **remote id** | **本地 id** | — | `adb_device.py:1189`（读 OKAY 取 remote_id） |
| `WRTE`（设备→我们） | remote id | local id | 实际数据 | `adb_device.py:1221-1254` |
| `OKAY`（我们→设备，ACK） | **local id** | **remote id** | — | `adb_device.py:1135-1145` |
| `CLSE` | local id | remote id | — | `adb_device.py:1117-1133` |

**arg0/arg1 通用规则**（`adb_shell/hidden_helpers.py:164-186`）：

```python
def args_match(self, arg0, arg1, allow_zeros=False):
    if allow_zeros:
        return arg1 in (0, self.local_id) and (self.remote_id is None or arg0 in (0, self.remote_id))
    return arg1 == self.local_id and (self.remote_id is None or arg0 == self.remote_id)
```

翻译成人话：
- **从客户端视角**：收到的包里 `arg1` 必须等于我的 `local_id`；`arg0` 必须等于对端的 `remote_id`（OPEN 之前 remote_id 还是 None，只校验 arg1）。
- `arg0` = **对端 id**，`arg1` = **本地 id**——两个方向**正好相反**，这是自研最容易搞错的地方。
- **配对**语义：`OPEN` 用 `arg0=local_id` 建立连接，后续所有报文靠这一对 id 串起来；`local_id` 每次 `_open` 自增 1，溢出（2^32）回绕到 1（`adb_device.py:1183-1185`）。

**`shell:` 命令格式**（`adb_shell/adb_device.py:1256-1291` `_streaming_command`）：

```python
adb_info = self._open(b'%s:%s' % (service, command), transport_timeout_s, read_timeout_s, timeout_s)
```

即 payload 精确为 `b'shell:' + command.encode('utf8') + b'\0'`。
- service 取 `b'shell'`（`adb_device.py:816-842` 的 `shell()` → `self._service(b'shell', command.encode('utf8'), ...)`）。
- **尾部 `\0` 不可省**（`adb_device.py:1188`：`destination + b'\0'`）。
- 本仓库只用到 `shell:` 和 `sync:`（push/pull）两个 service。

**shell 输出读到结束**（`adb_shell/adb_device.py:1221-1254` `_read_until_close`）：

```python
while True:
    cmd, data = self._read_until([constants.CLSE, constants.WRTE], adb_info)
    if cmd == constants.CLSE:
        msg = AdbMessage(constants.CLSE, adb_info.local_id, adb_info.remote_id)
        self._io_manager.send(msg, adb_info)
        break
    yield data
```

- **结束判定 = 收到 `CLSE`**（不靠输出内容猜，也不靠超时）。
- 收到 `WRTE` **必须回 `OKAY`**（`adb_device.py:1213-1215`，`_read_until` 里 “Acknowledge write packets”），否则对端流控窗口耗尽会卡死。**这是自研最容易漏的一步。**
- 客户端收到 `CLSE` 后也回一个 `CLSE`（`adb_device.py:1250-1251`）。
- **注意**：`adb_shell` 的 `shell()` **全量缓冲在内存**再返回（docstring： “All the data is held in memory”）。本项目拉日志/截图时要注意内存上限。

### 1.5 分块大小上限

| 常量 | 值 | 用途 | 来源 |
|---|---|---|---|
| `MAX_ADB_DATA` | 1 MB | 客户端声明自己能收的 payload 上限 | `constants.py:43` |
| `MAX_LEGACY_ADB_DATA` | 4 KB | 老 adbd 的 maxdata | `constants.py:44` |
| `MAX_PUSH_DATA` | 2 KB | filesync 单个 DATA 包默认上限（老设备） | `constants.py:47` |
| `MAX_CHUNK_SIZE` | 64 KB | filesync 分块上限 | `constants.py:50` |

filesync 实际分块（`adb_shell/adb_device.py:598-608`）：

```python
@property
def max_chunk_size(self):
    return min(constants.MAX_CHUNK_SIZE, self._maxdata // 2) or constants.MAX_PUSH_DATA
```

- `_maxdata` 是**设备**在 CNXN 里回报的值。
- 设备报 1 MB → `min(64KB, 512KB) = 64KB`。
- 设备报 4 KB（Android 5.x 电视）→ `min(64KB, 2KB) = 2KB`。
- 设备报 0 → 退化为 `MAX_PUSH_DATA = 2KB`。
- **答「单块最大多少」：不超过 64 KB，且不超过设备 maxdata 的一半。**

### 1.6 push/pull（`sync:` 协议）—— **已实现**

`adb_shell` 完整实现了 FileSync 协议。

**内部格式**（`adb_shell/constants.py:104-113`）：

| 常量 | struct | 含义 |
|---|---|---|
| `FILESYNC_LIST_FORMAT` | `<5I` | LIST 响应头 |
| `FILESYNC_PULL_FORMAT` | `<2I` | RECV 请求头 |
| `FILESYNC_PUSH_FORMAT` | `<2I` | SEND / DATA / DONE 头 |
| `FILESYNC_STAT_FORMAT` | `<4I` | STAT 响应头 |

**FileSync 命令 ID**（`constants.py:64-80`）：`SEND` `DATA` `DONE` `RECV` `STAT` `LIST` `DENT` `QUIT` `OKAY` `FAIL`。

**每个 FileSync 子包的头是 8 字节小端**（`adb_device.py:1470`）：

```python
buf = struct.pack(b'<2I', constants.FILESYNC_ID_TO_WIRE[command_id], size) + data
```

**push 时序**（`adb_device.py:1020-1074` `_push`）：

```
1. FileSync 子包: SEND  (payload = b"<remote_path>,<mode_int>")
2. 循环:         DATA  (payload = 最多 max_chunk_size 字节)
3. 结束:         DONE  (size 字段塞 mtime，payload 空)
4. 设备应答:     OKAY → 成功
                  FAIL → 抛 PushFailedError(payload)
```

- 整个 FileSync 流包在 **ADB `WRTE` 包**里传：`adb_device.py:1307-1315` `_filesync_flush` 把攒好的 FileSync 字节作为一个 `WRTE` 包发出，然后等 `OKAY`。
- `pull` 对称：`RECV`（`<2I`: id + size）→ 设备回 `DATA` 块 → `DONE` → 最后 `QUIT`。

**结论**：**不需要自己实现 sync 协议**，`adb_shell` 已经做完。我们只需在能力层包一层 `install` / `push` / `pull`。

### 1.7 错误处理：异常 → 可编程错误分类

`adb_shell/exceptions.py`：

| 异常 | 行 | 触发条件 | 映射到本项目 `DeviceResult` |
|---|---|---|---|
| `AdbCommandFailureException` | 26 | 收到 `FAIL` 包 | `error`（带 payload 原文） |
| `AdbConnectionError` | 32 | 传输层断连 / 未连接就发命令 | `unavailable`（**可重试**） |
| `AdbTimeoutError` | 38 | 命令超时 | `timeout`（**可重试**） |
| `DeviceAuthError` | 44 | 设备要求认证但没密钥 / 认证被拒 | `denied` |
| `InvalidChecksumError` | 53 | payload 校验和不符 | `error`（协议 bug 或数据损坏） |
| `InvalidCommandError` | 59 | 收到未知 command 字节 | `error` |
| `InvalidTransportError` | 65 | 传输层参数非法 | `error` |
| `InvalidResponseError` | 71 | 响应不符合预期 | `error` |
| `DevicePathInvalidError` | 77 | 设备端路径非法 | `error` |
| `PushFailedError` | 83 | sync push 收到 FAIL | `error`（带 payload） |
| `TcpTimeoutException` | 89 | socket 读写超时 | `timeout` |
| `UsbDeviceNotFoundError` | 95 | USB 设备不在（用不到） | `unavailable` |
| `UsbReadFailedError` / `UsbWriteFailedError` | 101 / 125 | USB 读写失败 | `error` |

**超时边界**：
- `DEFAULT_AUTH_TIMEOUT_S = 10.`（`constants.py:119`）
- `DEFAULT_READ_TIMEOUT_S = 10.`（`constants.py:122`）——**默认 10 秒非常短**，本项目必须显式传更长超时（`tools/tv.py:54,80,86` 都传 120s）。
- 传输层超时抛 `TcpTimeoutException`，消息形如 `Reading from {host}:{port} timed out ({n} seconds)`（`transport/tcp_transport.py:119`）。

**「连接失败 / 超时 / 认证拒绝」的可编程识别**（本项目「失败必须带原因」的落点）：

| 场景 | 表现 | 我们要给的 `err` |
|---|---|---|
| 电视没开无线调试 / IP 变了 | socket 层 `ConnectionRefusedError`（未被 adb_shell 包住） | `unavailable`: "目标 {ip}:{port} 拒绝连接（电视关机 / 无线调试未开 / IP 已变）" |
| 电视在但端口被过滤 | `TcpTimeoutException` | `timeout`: "连接 {ip}:{port} 超时（网络不通或被防火墙拦截）" |
| 设备要求认证但无密钥 | `DeviceAuthError` | `denied`: "设备要求 ADB 认证授权，请到电视上点『允许』" |
| 密钥不匹配 | `DeviceAuthError` / `InvalidResponseError` | `denied`: "ADB 认证被拒，电视上仍有『允许 USB 调试？』未确认" |
| 设备没响应 shell | `AdbTimeoutError` | `timeout`: "命令在 {n}s 内未完成" |

**`adb.exe` 路径的错误**：只能拿到退出码 + stderr 文本，需要**自己写正则映射表**把「device unauthorized」「device not found」「connection refused」「timeout」映射到同一套错误码。这是「优先 adb.exe」方案的真实成本，**不能假设 adb.exe 的错误信息稳定**。

### 1.8 Windows 兼容性

**结论：Windows 完全可用，无任何平台分支代码。**

- `adb_shell` 纯 Python 跨平台；`AdbDeviceTcp` 用 `socket.create_connection`（`transport/tcp_transport.py:88`），无平台特定逻辑。
- 仓库为 Windows 专门提供 PowerShell 版：`tools/disable.ps1`、`tools/restore.ps1`、`tools/theme.ps1`、`launcher/build-and-install.ps1`。
- **Windows 特有的三个坑（都已处理）**：

  1. **控制台代码页**（`tools/tv.py:21-25`）：
     ```python
     for _stream in (sys.stdout, sys.stderr):
         try:
             _stream.reconfigure(errors="replace")
         except (AttributeError, ValueError):
             pass
     ```
     原因见 `tools/tv.py:18-20` 注释：Windows 旧代码页（cp1251/cp1252）打印不了 `dumpsys` 输出的部分字符，Python 会在中途抛错。**这条对本项目同样适用**。

  2. **路径改写 / BOM**：
     - Git Bash 的 `pwd` 返回 `/c/Users/...`，Windows Python 打不开（尤其设了 `MSYS_NO_PATHCONV=1` 时）。`tools/disable.sh:11-13` 与 `tools/restore.sh:12-13` 用 `pwd -W` 取 Windows 路径。
     - PowerShell 5.1 管道输入带 BOM → `tools/tv.py:75` 用 `sys.stdin.reconfigure(encoding="utf-8-sig")` 去掉。
     - 清单文件 BOM/CRLF 归一化：`tools/disable.sh:22-23`（awk 去 BOM + 去 `\r`）；`tools/recovery.py:43`（`read_text(encoding="utf-8-sig", errors="replace")`）。

  3. **venv 路径**：`tools/disable.sh:14-15` 先试 `tools/venv/bin/python`（Unix），再试 `tools/venv/Scripts/python.exe`（Windows）。

- **macOS Local Network 隐私**：`docs/TROUBLESHOOTING.md:8-10` 原文——Google 签名的 `adb` 作为独立 app 受 Local Network 隐私管辖并被**静默拦截**。`PROMPT.md:24-25` 给的解法是「不要纠缠：改用 Python 的 `adb-shell` 包」。**这与 team-lead 的动机一致，但解决手段是「用 `adb_shell` 这个第三方包」，不是「用这个仓库自研的客户端」。**

### 1.9 `host:connect:` —— **未查到**

- 在 `tools/`、`launcher/`、`docs/`、`PROMPT.md`、`README.md` 中**均未出现** `host:connect`。
- 在 `adb_shell` 源码中也**未出现**。
- 原因：`adb_shell` 是**直连模式**（`AdbDeviceTcp`），根本没有 adb server 的 5037 端口概念。
- **对我们的含义**：若要支持「先连 adb server 再 `adb connect ip:port`」（adb.exe 的标准用法），那是 **adb.exe 传输层**的功能，`adb_shell` 不提供。但我们不需要——直连 5555 更简单，顺带绕过「adb.exe 需要先 `adb connect`」的两步流程。

---

## 2. 停用 / 恢复 / 记录（disable.sh / restore.sh / recovery.py）

### 2.1 `pm disable-user` 的确切命令形式

**答案：`pm disable-user --user 0 <pkg>`，带 `--user 0`，且 `--user 0` 在前、包名在后。**

来源（`tools/disable.sh:130-133`）：

```bash
{
  [ -n "$PKGS" ] && printf '%s\n' "$PKGS" | sed 's/^/pm disable-user --user 0 /'
  echo "pm list packages -d"
} | "$PY" -I tools/tv.py --batch
```

- 「`--user 0` 在前」由 `sed` 的前缀替换决定。
- `tools/disable.sh:3` 注释确认语义：**「Packages are disabled for user 0, never uninstalled.」**——**只停用、绝不用 `pm uninstall`**，这是可逆性的根本。
- 恢复用 `pm enable <pkg>`（`tools/recovery.py:238`）——恢复时**没有** `--user 0`（`pm enable` 默认作用于所有用户）。
- 停用 stock launcher：`pm disable-user --user 0 $STOCK_LAUNCHER`（`launcher/build-and-install.sh:62`）。
- 回滚时：`pm enable --user 0 $STOCK_LAUNCHER`（`launcher/build-and-install.sh:106`）——**这里带了 `--user 0`**，与 `pm enable <pkg>` 不一致，但实践中都可行。

### 2.2 「改了什么」怎么记录：JSON 记录文件

**文件路径**（`tools/recovery.py:58-63`）：

```python
def record_path(list_path, host):
    tag = re.sub(r"[^A-Za-z0-9.-]", "_", host or "no-host")
    digest = hashlib.sha1(str(Path(list_path).resolve()).encode()).hexdigest()[:8]
    return Path.home() / ".cleanyourtv" / f"recovery-{tag}-{digest}.json"
```

即：`~/.cleanyourtv/recovery-{host_tag}-{sha1(清单绝对路径)[:8]}.json`
- `host_tag`：主机名里非 `[A-Za-z0-9.-]` 字符替换成 `_`。
- `digest`：清单**绝对路径**的 sha1 前 8 位 → **同一份清单复制到别处会得到独立记录**（注释明说）。

**记录格式**（`tools/recovery.py:190-198`）：

```python
record = {
    "version": VERSION,                                    # = 1
    "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "host": host,
    "list": str(Path(list_path).resolve()),
    "home": home,
    "settings": scales,
    "packages": {name: package_state(name, installed, disabled) for name in targets},
}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `version` | int | 记录格式版本，固定 `1`（`tools/recovery.py:34`） |
| `created` | str | 记录时间 `%Y-%m-%dT%H:%M:%S%z` |
| `host` | str | 目标电视（IP/主机名） |
| `list` | str | 清单绝对路径（校验「是不是同一份清单」） |
| `home` | str / null | 清理**之前**谁拥有 HOME（形如 `pkg/activity`） |
| `settings` | dict | 3 个动画 scale 的旧值（`window_animation_scale` / `transition_animation_scale` / `animator_duration_scale`，`tools/recovery.py:30`） |
| `packages` | dict | `包名 → 状态`，取值 `absent` / `disabled` / `enabled` |

**`packages` 状态语义**（`tools/recovery.py:146-149`）：

```python
def package_state(name, installed, disabled):
    return ("absent" if name not in installed
            else "disabled" if name in disabled
            else "enabled")
```

| 状态 | 含义 | 恢复时动作 |
|---|---|---|
| `absent` | 该包**未安装** | **不动** |
| `disabled` | 装着，但**清理前就已停用** | **不动**（不擅自启用用户原本停用的应用） |
| `enabled` | 装着，清理前是启用的 | **`pm enable <pkg>`** |

**额外收录 Home owner**（`tools/recovery.py:188-189`）：

```python
home_package = home.split("/", 1)[0] if home else None
targets = packages + ([home_package] if home_package and home_package not in packages else [])
```

即：**即使 Home owner 不在清单里，也要记进 `packages`**，否则恢复时不知道该把 Home 还给谁。

**状态采集来源**（`tools/recovery.py:112-122`）：`pm list packages` 得已安装集合，`pm list packages -d` 得已停用集合；**若 `pm list packages` 返回空则直接中止**（`tools/recovery.py:116-118`）——“nothing was recorded and nothing will be disabled”，避免把「连接断了」误判成「电视上什么都没有」。

**Home 采集**（`tools/recovery.py:125-131`）：`cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME`，取最后一行，且**必须匹配 `HOME_RE`**（`tools/recovery.py:28`：`[A-Za-z_][\w.]*/[\w.$]+`）——Android 在没人接管 HOME 时会回 `android`（选择器），那不是组件，**绝不能写进 `set-home-activity`**。

**原子写**（`tools/recovery.py:95-105`）：先写 `*.json.tmp`（`:99`）+ `json.dump`（`:101`）+ `flush()` + `os.fsync()`，再 `os.replace(tmp, path)`（`:105`）——崩溃不会留下半个文件把后续重试堵死。

### 2.3 幂等：first-wins（重跑不覆盖第一次的记录）

`tools/recovery.py:152-176`：若记录已存在，

```python
added = [name for name in packages if name not in record["packages"]]
```

- **已记录项绝不覆盖**（注释：a retry must not record the broken state as the original）。
- **清单新增项会被追加**，状态取「当前」值——这些包本轮之前没被动过，现在的状态就是清理前的状态。
- 没有新增项时直接 `return 0`，不连电视。

`README.md:303-304` 复述同一行为：「a second run of the same list keeps the first record instead of saving the cleaned-up state over it」。

**恢复侧的对应处理**（`tools/recovery.py:269-271`）：

```python
edited = [name for name in packages if name not in states]
if edited:
    sys.stderr.write(f"not in the record, leaving alone: {', '.join(edited)}\n")
```

清单里新加但从未被停用过的包 → 恢复时**不动**。

### 2.4 `restore.sh` 怎么读回记录并恢复

`tools/restore.sh` 全文 23 行，核心只有最后一行（`tools/restore.sh:23`）：

```bash
"$PY" -I tools/recovery.py --commands "$LIST" "$STOCK" | "$PY" -I tools/tv.py --batch
```

即 **`recovery.py --commands` 生成恢复用的 batch 文本 → 管道喂给 `tv.py --batch`**。

**关键设计（`tools/restore.sh:19-22` 注释）**：

> “A refused plan never reaches the TV because recovery.py prints nothing until the whole plan is built, so tv.py reads an empty batch; pipefail only makes this script exits non-zero afterwards.”

→ **恢复计划必须整体构建成功才输出**；构建失败则 stdout 为空，电视上**一条命令都不会执行**。这是「先想清楚再动手」的具体实现，值得抄设计。

**恢复命令生成**（`tools/recovery.py:209-276`）：

| 情形 | 生成的命令 | 来源 |
|---|---|---|
| 有记录、`state == "enabled"` | `pm enable <pkg>` | `tools/recovery.py:236-238` |
| 有记录、`state` 为 `disabled`/`absent` | **不动**，stderr 打印 `leaving <pkg> alone (<state> ...)` | `tools/recovery.py:239-241` |
| 有记录、fallback stock launcher 未被停用 | `pm enable <fallback>` | `tools/recovery.py:245-247` |
| 有记录、`home` 存在且不是本项目 launcher | `cmd package set-home-activity --user 0 <home>` | `tools/recovery.py:249-251` |
| 有记录、`home` 为空或是本项目 launcher | `cmd package set-home-activity --user 0 $(cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME <fallback> \| tail -n 1)` | `tools/recovery.py:252-258` |
| 动画 scale 记录值是 `"null"` | `settings delete global <key>` | `tools/recovery.py:261-262` |
| 动画 scale 记录值是数字 | `settings put global <key> <value>` | `tools/recovery.py:263-264` |
| 动画 scale 记录值既不是 null 也不是数字 | **跳过**，stderr 说明 | `tools/recovery.py:265-266`（`SCALE_RE` = `-?\d+(\.\d+)?`，`tools/recovery.py:29`） |
| 结尾 | `pm list packages -d`（复核还剩哪些停用） | `tools/recovery.py:272` |

**无记录时的降级**（`tools/recovery.py:217-232`）：stderr 明确警告「Packages that were already disabled before cleanup will be enabled too: check the list before running this.」，然后退化为「启用清单里全部包 + 设 Home + 三个 scale 设 1.0 + `pm list packages -d`」。**这是有损降级，所以必须显式警告。**

**`stock` 参数语义**（`tools/recovery.py:212-213`）：`stock` 也要过 `PACKAGE_RE` 校验，否则 `SystemExit`。

### 2.5 停用清单的组织方式（debloat.txt 格式）

**格式**（`tools/disable.sh:18-28` 归一化 + `tools/recovery.py:37-55` 解析）：

- 一行一个包名。
- `#` 开头是注释；**行尾 `# 注释` 也被剥掉**（`tools/recovery.py:46`：`line.split("#", 1)[0].strip()`）。
- 空行忽略。
- BOM 去掉（`utf-8-sig` 或 awk 去 `\357\273\277`）。
- CRLF 去掉（去尾部 `\r`）。
- **只取每行第一个 token**（`tools/recovery.py:49`：`name = line.split()[0]`；`tools/disable.sh:23` 的 `awk '{print $1}'`）。

**包名校验（防 shell 注入）**：`tools/disable.sh:24-28` + `tools/recovery.py:27,50-51`

```python
PACKAGE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+")
```

- **一个不合格就整份清单拒绝**（`tools/disable.sh:25-28` 直接 `exit 1`；`tools/recovery.py:51` `raise SystemExit`）。
- 注释解释原因（`tools/disable.sh:18-19`）：「Lists come from contributors, and the package name is pasted into a shell command on the TV. Refuse the whole list unless every entry is a bare package name (#165).」
- **这条对我们极其重要**：包名会被拼进 `pm disable-user --user 0 <pkg>` 送进设备 shell。正则 + 全量拒绝 = 注入防护。我们的实现必须照做。

**相对/绝对路径解析规则**：
- `tools/disable.sh:11-12` 与 `tools/restore.sh:12-13`：**先在调用者的 cwd 下解析清单路径，再切到仓库根**。
  ```bash
  LIST_DIR="$(cd "$(dirname "$LIST")" && { pwd -W 2>/dev/null || pwd; })"
  LIST="$LIST_DIR/$(basename "$LIST")"
  cd "$(dirname "$0")/.."
  ```
  原因见注释（`tools/disable.sh:8-10`）：Git Bash 的 `pwd` 说 `/c/Users/...`，Windows Python 在 `MSYS_NO_PATHCONV=1` 下打不开；`pwd -W` 给 `C:/Users/...`，与 PowerShell 脚本算出的路径一致 → **与 PowerShell 版共享同一份记录文件**。
- 记录 key 用**解析后的绝对路径**（`tools/recovery.py:62`），所以**相对路径、绝对路径、仓库相对路径三种写法指向同一份清单时会命中同一条记录**（`tests/test_debloat_paths.py` 覆盖了这三种解析）。

**清单与 `config.json` 的分工**：`config.json` 不是停用清单，而是**launcher 行为配置**（dock 应用、隐藏应用、来源面板服务、列数、Live TV 包、按键 keycode），例如 `examples/byintek-u4/config.json`：

```json
{
  "dock_apps": ["com.google.android.youtube.tv", "com.netflix.mediaclient", "..."],
  "hidden_apps": ["com.sdt.app.skysettings"],
  "source_panel_service": "com.sdt.app.skysettings/.entry.activity.QuickSourceActivity",
  "columns": 4,
  "rows_below_dock": 0
}
```

### 2.6 批量执行怎么保证「一小批一小批」

**真相：代码层面并没有真正分小批。**

- `tools/disable.sh:130-133` 把**整份清单**一次性灌进**一个** `tv.py --batch`。
- `tv.py --batch`（`tools/tv.py:72-84`）确实是「一行一条命令、**同一连接**、逐条执行」：
  ```python
  for line in sys.stdin:
      line = line.strip()
      if not line or line.startswith("#"):
          continue
      out = dev.shell(line, read_timeout_s=120.0)
  ```
- 「小批」只存在于**提示词层**：`PROMPT.md:59-60`「Disable in small batches and screenshot between them: a batch can take something load-bearing with it and you cannot tell which.」以及 `README.md:341`「applied in small batches with a restore script ready」。

→ **对我们的含义**：若要「一小批一小批」，得由**上层**（我们的 `invoke` 编排层）分批，而不是抄他们的脚本。抄他们的脚本只能得到「一整批」。

**另一条关键纪律：一次 shell 调用只跑一条 `pm` 命令。**
- `PROMPT.md:58`「Run `pm` commands one per shell call, chained commands may silently stop after the first.」
- `docs/TROUBLESHOOTING.md:53-54`「**Chained `pm` commands** (`pm disable a; pm disable b`) may silently stop after the first. Run one per shell call; `--batch` does exactly that.」
- 这条**必须照做**：链式 `pm` 会在第一条后静默中断，而电视上看起来「成功了一半」。

### 2.7 三道护栏（会砖机的坑，全部可跳过）

| 护栏 | 触发判据 | 行为 | 覆盖开关 | 来源 |
|---|---|---|---|---|
| **setupwraith** | 清单含 `com.google.android.tungsten.setupwraith`，且 `dumpsys wifi \| grep -o 'cname=[^ ]*'` 输出里出现 setupwraith | **跳过该包**（不 disable），因为 Android 会删除被禁用 App 保存的 Wi-Fi，电视**永久掉线** | `FORCE_SETUPWRAITH=1` | `tools/disable.sh:29-50` |
| **Home owner** | 清单含当前 HOME owner，且 `cmd package query-activities --brief -a android.intent.action.MAIN -c android.intent.category.HOME` 查不到**其它**可用 HOME | **跳过该包**，否则电视下次按 Home 无反应 / Google TV 上 setupwraith 抢 Home 导致 system_server 崩溃循环 | `FORCE_HOME=1` | `tools/disable.sh:56-86` |
| **TV_INPUT_HARDWARE** | 包声明了 `android.permission.TV_INPUT_HARDWARE` | **跳过该包**：它软件层负责 HDMI/AV/tuner 的路由、HDCP 握手与缩放，停用后**端口认得线但永远黑屏**，分批重新启用也救不回来，**必须完全恢复 + 冷启动** | `TVINPUT_ALLOW=1` | `tools/disable.sh:88-123` |

**setupwraith 判定的两个细节**：

1. **探测失败也要跳过**（`tools/disable.sh:39-42`）：`dumpsys wifi` 失败/超时/看不到网络 → **不 disable**（fail-safe，电视绝不能掉线）。
2. **`grep` 不能用 `-q`**（`tools/disable.sh:43` 注释）：`set -o pipefail` 下 `grep -q` 提前退出会给上游 `printf` 发 SIGPIPE，导致**整个管道失败**（fail-open，危险）。所以一律 `grep` 不带 `-q`。**这是一个非常容易踩的 shell 坑，值得记住。**

**TV_INPUT 探测的巧妙做法**（`tools/disable.sh:96-106`）：**一轮往返**——把 for 循环放在**设备上**执行，只取 `dumpsys package` 里「requested permissions」块，避免每包一次往返：

```bash
NAMES="$(printf '%s ' $PKGS)"
TV_HW="$("$PY" -I tools/tv.py \
    "for p in $NAMES; do dumpsys package \$p | sed -n -E '/requested permissions:/,/(install|runtime) permissions:|User [0-9]+:/p' | grep -q TV_INPUT_HARDWARE && echo \$p; done; true" \
    </dev/null 2>/dev/null | tr -d '\r')"
```

- 注释解释：只看「requested permissions」块，否则**共享 UID 的厂商 App 会因为共享用户的权限被误判**（`tools/disable.sh:88-89`）。
- 结尾的 `; true`：循环的退出状态是最后一个包的 `grep -q`，最后一个包通常不是输入驱动 → 状态非零 → 会被误判为「检查失败」。`; true` 把它抹平，**只有 tv.py 真连不上才走警告分支**（`tools/disable.sh:101-103`）。
- 探测失败时**不跳过任何包**，但明确警告「Check every input after this run」（`tools/disable.sh:117-122`）。

**Home guard 的排除名单**（`tools/disable.sh:72-79`）：
- 排除 `*/FallbackHome`（固件占位符，不是可用 launcher）。
- 排除 `com.google.android.tungsten.setupwraith`、`com.android.tv.settings`、`com.android.settings`、`com.android.provision`（它们也 answer HOME，但**不是 launcher**；setupwraith 抢 Home 正是崩溃循环的根因）。
- 注释还举了 TCL Android TV 的例子：固件注册 `com.tcl.keycustomfunctionservice/.FallbackHome` 与 `com.android.tv.settings/.system.FallbackHome`。

**「先存记录再动手」**（`tools/disable.sh:125-128`）：

> “The state undo needs goes to disk before the first command reaches the TV, and a failed save stops here with the TV untouched: a record written afterwards would describe a TV that is already broken, and a retry must not overwrite the one taken before the first change (#239).”

```bash
"$PY" -I tools/recovery.py --save "$LIST"
```

→ **记录写失败 = 直接中止，电视一点没动。** 这是「可逆性」的硬保证。

**最后的安全提醒**（`tools/disable.sh:135-139`）：若 `ro.adb.secure == 0`，脚本在结束时警告「network ADB on port 5555 accepts any device on your network without the "Allow" prompt」，并指向 README 的关闭步骤。

---

## 3. `tools/b64push.py` —— base64 分块上传

**全文 45 行**（`tools/b64push.py`）。

### 3.1 为什么需要它（动机）

1. **`adb_shell` 的 push 在大文件上会挂**——`docs/TROUBLESHOOTING.md:55`：「**Large pushes hang** through the Python client. Use `tools/b64push.py` for APKs.」
2. **老 adbd 的 payload 上限**（`tools/b64push.py:15-16` 注释）：
   > “adbd before Android 7 aborts the connection on an ADB "open" payload over 4096 bytes, which Android 5.x TVs announce as their max data size. Newer adbd takes the full 48 KB.”

   → 即 Android 7 之前的 adbd，**OPEN 包的 payload 超过 4096 字节就断连**；而 Android 5.x 电视恰恰把 maxdata 报成 4096。
3. `PROMPT.md:26-27` 也把它写成给 AI 的标准动作：「If its file push hangs on larger files, transfer files as base64 chunks through the shell and verify the md5.」

**注意：这不是「adb push 的大小限制」**——真正的原因是 (1) 客户端 push 挂起 和 (2) 老设备 adbd 的 OPEN payload 上限。base64 是绕开 payload 上限的手段（把大块拆成小 shell 命令）。

### 3.2 分块大小公式

`tools/b64push.py:14` + `:24-25`：

```python
CHUNK = 48 * 1024  # raw bytes per shell command (base64 grows it by 4/3); capped per TV in main()
...
chunk = min(CHUNK, dev.max_chunk_size * 3 // 4 - 64)   # base64 of the chunk plus the echo must fit one payload
chunk -= chunk % 3   # a multiple of 3, or each chunk ends in '=' padding mid-stream and base64 -d corrupts it
```

三层：
1. 上限 `CHUNK = 48 * 1024` raw 字节（base64 变 4/3 倍，48KB → 64KB，正好卡在 `MAX_CHUNK_SIZE`）。
2. 再按设备能力收紧：`min(48KB, max_chunk_size * 3/4 - 64)`——`*3//4` 是 base64 膨胀的逆运算，`-64` 给 `echo '...'` 的引号和换行留余量。
3. **对齐到 3 的倍数**：`chunk -= chunk % 3`。原因写在注释里：不这样每块末尾会带 `=` padding，`base64 -d` 拼接时中间出现 padding 会被截断 → 文件损坏。

### 3.3 传输流程与校验

`tools/b64push.py:26-41`：

1. `rm -f {remote} {tmp}`（`tmp = remote + ".b64"`）——先清干净。
2. 循环：每块 `dev.shell(f"echo '{part}' >> {tmp} && echo ok{i}")`。
   - **每块回读确认**：输出里必须含 `ok{i}`，否则打印 `chunk {i} failed: {out}` 并 `exit 1`（`tools/b64push.py:31-34`）。**每块都有 ack，不靠最终 md5 才发现中途断了。**
3. 收尾：`base64 -d {tmp} > {remote} && rm {tmp} && md5sum {remote}`。
4. 比对本地 md5（`tools/b64push.py:22` 的 `hashlib.md5(data).hexdigest()`），打印 `MATCH` / `MISMATCH`，**md5 不匹配则 `exit 1`**（`tools/b64push.py:41`）。

→ **双重校验**：逐块 ack + 整体 md5。

---

## 4. `tools/adb-closed.py` 与 `docs/TROUBLESHOOTING.md`

### 4.1 端口探测怎么做

`tools/adb-closed.py` 全文 51 行。做法极简：**纯 TCP connect，不发任何 ADB 协议字节**。

`tools/adb-closed.py:31-47`：

```python
try:
    with socket.create_connection((host, port), timeout=TIMEOUT_S):
        pass
except ConnectionRefusedError:
    print(f"{host}:{port} is closed: the TV refused the connection.")
    return 0
except socket.timeout:
    print(f"{host}:{port} did not answer in {TIMEOUT_S:.0f} s: closed, unless the TV is off or not at this address.")
    return 0
except OSError as e:
    sys.stderr.write(f"could not check {host}:{port} ({e}). Is the TV on and on this network?\n")
    return 2
sys.stderr.write(f"{host}:{port} is still open: network ADB is on. ...")
return 1
```

**退出码语义**（`tools/adb-closed.py:6-7` 文档字符串）：

| 退出码 | 含义 | 判据 |
|---|---|---|
| 0 | 端口**已关闭** | `ConnectionRefusedError`（明确 RST）**或** `socket.timeout`（不响应） |
| 1 | 端口**仍开着** | connect 成功 |
| 2 | **检查本身没能进行** | 其它 `OSError`（DNS 不解析、无路由） |

**关键设计**：**超时也算「已关闭」**（`tools/adb-closed.py:38` 注释：「Some firmware drops instead of refusing, but so does a TV that is off or at another address.」）——因为用户的目标只是「确认 5555 没开」，超时足够；但消息里必须带上「除非电视是关的或换了地址」这个免责说明。**这是「失败也要说清楚」的好范例。**

参数：host 来自 `argv[0]` 或 `TV_HOST`；port 来自 `argv[1]` 或 `TV_PORT`（默认 5555）；`TIMEOUT_S = 3.0`（`tools/adb-closed.py:13`）。

README 的用法（`README.md:364`）：`python tools/adb-closed.py <tv ip>` 必须说端口已关。

### 4.2 TROUBLESHOOTING 全部坑（逐条摘出）

#### A. 连接类（`docs/TROUBLESHOOTING.md:6-49`）

| # | 坑 | 行号 | 要点 | 对我们 |
|---|---|---|---|---|
| A1 | macOS `adb connect` 报 “No route to host” 但端口是开的 | 8-10 | 新版 macOS 把 Google 签名的 `adb` 当独立 app 纳入 Local Network 隐私并**静默拦截**。`tools/tv.py` 完全绕开二进制 | **我们要 `adb_shell` fallback 的第一理由** |
| A2 | 某些电视（如小米）**没有 “wireless debugging” 开关** | 11-12 | 只要开 USB 调试就会在 5555 打开网络 ADB | connect 不能假设有无线调试开关 |
| A3 | USB 调试**灰掉**且没有无线调试入口 | 13-20 | 运营商盒子（Airtel Xstream, Android 11）固件删掉两个开关 → **任何 shell 都连不上**。超时是固件行为，不是网络问题，**别去重建网络**。灰掉（而非缺失）指向 device-owner 策略或配给锁；固件更新后可能恢复，也可能永不 | probe 必须能区分「设备拒绝」和「网络不通」 |
| A4 | 开发者选项隐藏 / 连点 7 次 Build 要密码 | 21-27 | ISP 盒子（FPT Play 650）。绕过：`com.android.tv.settings/.system.development.DevelopmentActivity` 或 `android.settings.APPLICATION_DEVELOPMENT_SETTINGS` action | 无关（我们不 root），但说明「用户自己能做的事有限」 |
| A5 | USB 调试**永远不开 5555** | 28-33 | 某些 Google TV 机型（Skyworth 代工的 Philips）。解法：用官方 adb 走一次 `adb pair <ip>:<port> <code>` → `adb connect` → `adb tcpip 5555`，之后 `tools/tv.py` 就能用了。`dns-sd -B _adb-tls-pairing._tcp` 或 `adb mdns services` 找配对端口。**Android 一掉 Wi-Fi 就关掉无线调试**；`adb tcpip` 开的 5555 **通常只到下次重启** | **重要**：5555 需定期重开 → probe 应能识别「端口关了但设备在线」并给出明确指引 |
| A6 | 开着 USB 调试时 **USB 设备失活** | 34-36 | 小米 Mi Box S 固件把 USB 口切到 ADB device mode，切断 USB 音箱/硬盘供电 | 无关，但提醒「ADB 开启有副作用」 |
| A7 | **用完关掉 5555** | 37 | 明文；`ro.adb.secure=0` 时局域网任意设备可拿 shell | 文档/能力必须带这句警告 |
| A8 | 电视在**独立 VLAN / IoT 网段** | 38-41 | 路由器拦了。加一条临时防火墙规则**只放 tcp/5555**，用完撤掉。放开整个网段或永久搬主网 = 撤销当初隔离的意义 | 错误消息应提示「网络不通 / 被防火墙拦截」 |
| A9 | **Windows PowerShell 5.1 会给管道输入加 BOM** | 42-43 | 第一条 batch 命令变成乱码。`tv.py` 剥 BOM，`.ps1` 输出干净 UTF-8 | 我们读 stdin 必须用 `utf-8-sig` |
| A10 | “Allow USB debugging?” **反复弹** | 44-47 | `tv.py` 曾用自己旁边的 key，而 platform-tools 注册了另一个。现在 `~/.android/adbkey` 存在就复用。**每次用未接受的 key 尝试都会多排一个弹窗**，测试前先全部 Back 掉 | **关键设计**：`tools/tv.py:36-38` 复用 `~/.android/adbkey` → 避免电视重复弹窗 |
| A11 | **Git Bash 改写路径** | 48-49 | `/data/local/tmp` 被改成 Windows 路径。脚本设 `MSYS_NO_PATHCONV=1`。Git Bash 里构建还要设 `JAVA_HOME` / `ANDROID_HOME` | 只影响他们脚本；我们用 Python 直连，不受影响 |

#### B. 操作类（`docs/TROUBLESHOOTING.md:51-153`）

| # | 坑 | 行号 | 要点 | 对我们 |
|---|---|---|---|---|
| B1 | **链式 `pm` 命令会静默中断** | 53-54 | `pm disable a; pm disable b` 第一条后可能就停。一次 shell 一条命令，`--batch` 正是这么做的 | **必须遵守** |
| B2 | **大文件 push 挂起** | 55 | 用 `tools/b64push.py` 传 APK | 见第 3 章 |
| B3 | `adb exec-out screencap -p` 出**坏 PNG** | 56-59 | 部分 MediaTek 机型（小米、Amazon Fire TV）：二进制会先往 stdout 打 `Init wrapper sys mutex successful`。解法：**在电视上存文件再 pull**（`screencap -p /data/local/tmp/s.png`），或砍掉 `\x89PNG` 头之前的一切 | **直接影响我们的 screenshot 能力**：应先试 `exec-out screencap -p`（校验 `\x89PNG` 头），失败则回退「存文件 + pull」 |
| B4 | **Play Protect 拦下 launcher** | 60-62 | “unsafe app blocked, built for an older version of Android”，`pm install` 挂住后失败于 `INSTALL_FAILED_VERIFICATION_FAILURE`（当时 targetSdk=29）。现在 target 34；用旧 checkout 要重编 | install 错误映射里要有 `INSTALL_FAILED_VERIFICATION_FAILURE` |
| B5 | **截图是空的** | 63-66 | Live TV / HDMI 输入 / Netflix 在前台时，TCL 与 Sony KDL-49W800G 上截图全空。**按 Home 再 screencap**。空截图**不能**说明 ADB 断了或播放有问题 | screenshot 应返回「空」这一独立状态并附原因 |
| B6 | **必须先装新 launcher 再停旧 launcher** | 67-75 | 否则 Home 键落到出厂测试应用。`set-home-activity` 可能报 `Success` 而 HOME 实际仍解析到 stock launcher（priority 2），直到它被停用。**停用前先查谁真的能接 HOME**：Sony 上 `com.google.android.leanbacklauncher` 有 boot receiver 但**没有 HOME activity** | 见 5.2 |
| B7 | **中国版小米拒绝第三方 Home** | 76-81 | MiTV-AETP0 (dofus), Android 9：框架按**包名白名单**过滤第三方 HOME 候选。停用 stock launcher 后会选到 `FallbackHome`。**带厂家签名的 application-id 构建**在 stock launcher 还启用时可行 | launcher 能力要考虑这种机型 |
| B8 | **停用 stock 后新 launcher 仍不拥有 Home** | 82-91 | Google TV（小米 MiTV-MOOR2）：另一个系统应用 `com.google.android.tungsten.setupwraith` 也 answer HOME（`.MainActivity` priority 4、`.RecoveryActivity` priority 1）。**无法超越**：非系统镜像 App 在 HOME filter 上的 `android:priority` 被忽略；`set-home-activity` 打印 `Success` 但毫无作用（重启也不行）；对 setupwraith 单个组件 `pm disable-user` 被 `SecurityException` 拒绝。**只有停用整个 setupwraith 包才腾得出位置** | Home guard 的由来 |
| B9 | **停 setupwraith 会永久掉 Wi-Fi** | 92-101 | Google TV 14（Sony BRAVIA 9 II、Philips R8G、Skyworth SWTV-24NA-4K NF、小米 TV Box S 3rd MiTV-AFMU0）、TCL G07 on Google TV 11。**首次开机向导保存的 Wi-Fi 属于 setupwraith，Android 在停用 App 时删掉该 App 保存的网络**。电视没坏但**离线**了，ADB 和手机遥控都失效，**重启也救不回来**。检查方法：`tools/tv.py "dumpsys wifi" \| grep -o 'cname=[^ ]*'`。若指向 setupwraith，去设置里**忘记并重连**（之后归 `android.uid.system`），或用网线 | **本报告最该抄的一条护栏** |
| B10 | **停 setupwraith 后投屏报 “Untrusted device”** | 102-118 | Chromecast with Google TV HD (boreal) on Google TV 14 UTTC.250917.004。`com.google.android.apps.mediashell` 从 setupwraith 内的 `...tv.setupwraith.libs.castinfo.CastInfoProvider` **读取出厂设备凭据**，停掉向导凭据就没了。重新启用后一分钟内 logcat 出现 `SetupWraith\|CastInfoProvider: querying cast infos` 与 `Created singleton instance of ClientAuthSigner`，投屏恢复。**不能既保留 provider 又只放弃 Home**：`pm disable-user` 对 `setupwraith/.RecoveryActivity` 被 `SecurityException: Shell cannot change component state` 拒绝。只能：① 停用时不含 setupwraith 和 launcherx；② 或每次开机后重新启用 setupwraith，等 `ClientAuthSigner` 就绪后再停（接收端把凭据留在内存，投屏能撑到下次重启） | 说明「停用系统包」的副作用可能扩散到别的功能 |
| B11 | **装完立刻每分钟掉一次 Wi-Fi** | 119-127 | Google TV 14（小米 MiTV-MZTU0）：同样是 setupwraith 的 `.RecoveryActivity` 抢了 Home，**system_server 崩溃循环**。判据：`logcat -b crash` 出现 `FATAL EXCEPTION IN SYSTEM PROCESS: android.display`，主日志里 `setupwraith/.RecoveryActivity` **每 200ms** 被启动一次。已在循环中的急救：`tools/tv.py 'pm enable --user 0 com.google.android.apps.tv.launcherx'` | 我们的错误消息要能区分「Home 归属错乱」与「其它失败」 |
| B12 | `theme.sh` 报 “couldn't create file: Permission denied” | 128-132 | TCL G07 (Android 11)：shell 不在可写 `Android/data` 的组里。`theme.sh` 先在 `/data/local/tmp` 暂存，复制被拒时改走 launcher 的 `ThemeProvider`。**launcher 早于该 provider 存在时没有它** → 先跑一次 `launcher/build-and-install.sh` | 说明「权限分层」在 Android 上很常见 |
| B13 | 换 launcher 后**没有软键盘** | 133-135 | 小米：默认输入法指向未安装的百度键盘。修：`tools/tv.py 'ime set com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME'` | 我们的 launch 能力要考虑输入法 |
| B14 | 第三方 launcher 常对基础功能收费 | 136-137 | 这是他们自研 launcher 的动机 | — |
| B15 | **时区错、时钟差 30/60 分钟** | 138-145 | 小米（伊朗）：选了邻近时区，老固件的 tzdata 早于伊朗 2022 年取消夏令时。查：`tools/tv.py 'cat /apex/com.android.tzdata/etc/tz/tz_version'`。设：`settings put global auto_time_zone 0; cmd alarm set-timezone Asia/Tehran`；若春季还跳一小时，用固定偏移 `cmd alarm set-timezone GMT+03:30` | 我们的 `state.read` 类能力应带时区 |
| B16 | `pm clear` 或重装后网络药丸只显示 “Wi-Fi” | 146-149 | 清数据会丢 Android 10 读 SSID 需要的定位权限。修：`pm grant com.gathin.tvhome android.permission.ACCESS_FINE_LOCATION`（安装脚本已做） | — |
| B17 | `restore.sh` 之后再套清单，Home 变成 **“Select a Home app”** | 150-153 | 停用一个**也声称 HOME** 的包（小米上的 PatchWall）会让 Android 丢掉默认值。安装脚本现在会自己在候选里挑出已知 stock launcher；否则在电视上选，或传 `STOCK_LAUNCHER=<package>` | 恢复后必须**校验 Home 归属**，不能假设 |

#### C. 无障碍服务段（`docs/TROUBLESHOOTING.md:155-194`）

三个状态词的严格定义（`docs/TROUBLESHOOTING.md:172-181`）：

| 词 | 含义 | 判据 |
|---|---|---|
| **Enabled** | 组件在该用户的冒号分隔列表里（类名可能写成另一种写法） | `settings get secure enabled_accessibility_services` |
| **Bound** | 出现在该用户的 `Bound services` 段（老版本是 `services`） | `dumpsys accessibility` |
| **Working** | 从另一个 app 里按物理按钮，行为正确 | 实测 |

**死键的 keycode 查法**（`docs/TROUBLESHOOTING.md:181`）：`dumpsys window policy` 会列出 Android 路由的「global keys」。

---

## 5. `PROMPT.md` 与 `launcher/build-and-install.sh` —— 安装顺序、验证、回滚

### 5.1 为什么必须「先装新 launcher → 确认它在跑 → 才停用旧 launcher」

**理由有三层，逐层加重：**

1. **顺序反了，Home 键会落到出厂测试应用**（`docs/TROUBLESHOOTING.md:67-75`）。旧 launcher 一停，新 launcher 还没接管 Home，电视就没有可用的 HOME。

2. **在 batch 里失败是「静默」的**（`launcher/build-and-install.sh:65-66` 注释）：
   > “Install on its own: in the batch below a failed install (e.g. "not enough space" on a full /data) would not stop the stock launcher from being disabled.”

   → 批量执行里 `pm install` 失败（比如 /data 满）**不会中断**后面的 `pm disable-user`，结果就是「旧 launcher 已停、新 launcher 没装上」= 电视变砖。

3. **启动失败在 batch 里只打印一行 `Error:`**（`launcher/build-and-install.sh:73-76` 注释）：
   > “Start the launcher on its own, before the stock Home is touched. In the batch below a start that fails only prints an "Error:" line, and the stock launcher would be disabled anyway, leaving the TV with no usable Home (#239).”

**结论：安装、启动、停用、设 Home —— 这四步必须分成「先单独验证前两步，再做后两步」**。

### 5.2 怎么验证「新 launcher 在跑」

**两层验证，都不可省：**

**第一层：`am start -W` 的输出**（`launcher/build-and-install.sh:77-84`）：

```bash
START_OUT=$("$PY" -I ../tools/tv.py 'am start -W -n com.gathin.tvhome/.MainActivity')
echo "$START_OUT"
if ! printf '%s' "$START_OUT" | grep -qF 'Status: ok' || printf '%s' "$START_OUT" | grep -qi '^Error'; then
  echo "ERROR: the new launcher did not start (see above); $STOCK_LAUNCHER was left enabled." >&2
  exit 1
fi
```

- **两个条件是 AND 的反面**：`必须含 'Status: ok'` **且** `不含行首 'Error'`。注释解释（`:79-80`）：
  > “Both halves matter: some builds answer "Status: ok" for a task that was merely brought to the front, and an activity that cannot start still prints "Error: Activity not started".”

  → **只看 `Status: ok` 会漏掉「只是被带到前台」的假阳性；只看 `Error` 会漏掉 `Status: ok` 后面还有错误的情形。**
- 注释还澄清（`:75-76`）：`am start -W` 的 `Status: ok` 表示 activity **真的起来了**；而后面的 HOME 解析**只是点名一个组件，并不证明它能启动**。两者不能互相替代。

**第二层：Home 归属校验（安装结束后）**（`launcher/build-and-install.sh:131-146`）：

```bash
HOME_NOW=$(query_home)
case "$HOME_NOW" in
  com.gathin.tvhome/*) echo "Home now resolves to $HOME_NOW" ;;
  *)
    ... rollback_home || true ; exit 1 ;;
esac
```

- 校验命令：`cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME`，取最后一行（`launcher/build-and-install.sh:60`、`:94-96`）。
- **不匹配就回滚并 exit 1**。
- 注释（`:126-130`）说明为什么必须 `set-home-activity` **放在最后**：
  > “Android drops the preference whenever the set of HOME apps changes, and on Google TV 14 the next candidate is the setup wizard's RecoveryActivity, which crash-loops system_server.”

  → **Android 会在 HOME 候选集合变化时丢弃当前偏好**，所以顺序必须是：先启新的 → 再停旧的 → **最后**设偏好。

**第三层（隐含）：`pm install` 成功判定**（`launcher/build-and-install.sh:67-72`）：

```bash
OUT=$("$PY" -I ../tools/tv.py 'pm install -r /data/local/tmp/tvhome.apk; rm -f /data/local/tmp/tvhome.apk')
case "$OUT" in
  *Success*) ;;
  *) echo "ERROR: pm install failed; the stock launcher was left untouched." >&2; exit 1 ;;
esac
```

→ 判定依据是输出里**含 `Success`**，否则**立即退出，stock launcher 完全没被动过**。

**完整的执行顺序**（`launcher/build-and-install.sh:63-90`）：

```
1. b64push APK → /data/local/tmp/tvhome.apk          （launcher/build-and-install.sh:64）
2. pm install -r（单独执行，判 Success）              （:67-72）
3. am start -W（单独执行，判 Status: ok 且无 Error）   （:77-84）
4. batch:
   a. pm grant ... ACCESS_FINE_LOCATION               （:86）
   b. cmd package compile -m speed -f com.gathin.tvhome（:87）
   c. pm disable-user --user 0 $STOCK_LAUNCHER        （:88）
   d. cmd package set-home-activity --user 0 com.gathin.tvhome/.MainActivity（:89）
5. 查 Home 归属，不对就 rollback_home 并 exit 1        （:131-146）
```

**注意第 4 步的顺序也重要**：`grant` / `compile` 在停用 stock **之前**（因为它们针对新包，停用 stock 之后不影响），而 `set-home-activity` 在**最后**。

### 5.3 回滚逻辑

`rollback_home`（`launcher/build-and-install.sh:101-124`）：

1. `STOCK_LAUNCHER=none` → 无法回滚（本来就没停任何东西），报「nothing to roll back」返回 1（`:102-105`）。
2. `pm enable --user 0 $STOCK_LAUNCHER`（`:106`）。
3. `cmd package set-home-activity --user 0 $(cmd package resolve-activity --brief -a ... HOME $STOCK_LAUNCHER | tail -n 1)`（`:108`）。
   - 注释（`:107`）：「set-home-activity needs pkg/activity on Android 9, so resolve the component on the TV first.」
4. 再查一次 Home（`:109`）；若确实回到 stock → 报「$STOCK_LAUNCHER owns Home again.」返回 0（`:110-112`）。
5. **仍未恢复 → 把恢复命令写进本地文件**（`:113-119`）：
   ```bash
   REC="${HOME:-$PWD}/cleanyourtv-handover-$(printf '%s' "$TV_HOST" | tr -c 'A-Za-z0-9._-' '_').txt"
   ```
   文件里写的是可直接复制粘贴的命令（`:115-118`）。
   - 原因（`:98-100` 注释）：**「一台启动不了新 Home 的电视只能靠 ADB 抢救」**，而如果是连接断了，跑脚本的终端可能再也回不来 → **必须留一份纸面/文件面的恢复指令**。
6. 报「recovery is incomplete; Home still resolves to '<X>'」并返回 1（`:120-123`）。

**这是「失败必须带原因 + 必须可逆」的最完整示范**：不只回滚，还留一份「万一回滚也失败」的兜底指令文件。

### 5.4 停用 setupwraith 的特殊处理

**在 install 脚本里**：`launcher/build-and-install.sh:141-144` —— 若 Home 归属不对且 `$HOME_NOW` 非空：

> “On Google TV this usually means com.google.android.tungsten.setupwraith also claims Home: add it to your debloat list (see README, Troubleshooting), apply the list, then run this script again.”

**在 disable 脚本里**：`tools/disable.sh:29-50` 的 setupwraith 护栏（见 2.7）。

**在文档里**：`docs/TROUBLESHOOTING.md:92-118` 详述了掉 Wi-Fi 和投屏失效两个副作用。

**探测命令**（`docs/TROUBLESHOOTING.md:97`）：

```bash
tools/tv.py "dumpsys wifi" | grep -o 'cname=[^ ]*'
```

- `cname=` 是 dumpsys wifi 输出里 Wi-Fi 配置的 **cname**（配置名）。`grep -o 'cname=[^ ]*'` 提取全部 cname 值。
- **判定逻辑**：若提取结果里**出现 setupwraith**（`tools/disable.sh:44`：`printf '%s\n' "$OWNERS" | grep setupwraith >/dev/null`）→ 说明当前 Wi-Fi 网络的 cname 属于 setupwraith → 停用它会删掉这个网络 → **跳过**。
- **若完全提取不到任何 cname**（`tools/disable.sh:39`：`[ -z "$OWNERS" ]`）→ **也跳过**（探测不可靠时保守处理）。错误消息明确告诉用户两条出路：重试，或 `FORCE_SETUPWRAITH=1`（有网线时）。
- **注意：判断是「包含 setupwraith」而不是「等于 setupwraith」**——因为可能有多条 cname，只要有一条属于它就不能停。

### 5.5 `PROMPT.md` 给 AI 的作业规范（对本项目最有参考价值）

`PROMPT.md` 全文 118 行，是给 AI 用的操作规程。关键条款：

| 主题 | 内容 | 行号 |
|---|---|---|
| 连接 | 端口 5555；macOS 被 Local Network 隐私拦截 → **改用 Python `adb-shell` 包**；push 挂起 → base64 分块 + md5 | 21-27 |
| 无线调试 | 只有 Wireless debugging 时用 `dns-sd -B` / `adb mdns services` 找配对端口 → `adb pair` → `adb connect` → `adb tcpip 5555` | 28-31 |
| 授权提示 | **要主动告诉用户「电视上什么时候会弹『允许 USB 调试？』」** | 32 |
| Android 14 install 挂住 | 是在等屏幕确认 → push APK 后用 shell 跑 `pm install -r` | 33-34 |
| **先备份** | 存全部包列表、停用列表、getprop 输出、所有 settings 到备份目录，**并写一个恢复脚本** | 36-38 |
| USB 副作用 | 问用户 USB 口接了什么；有些设备开调试时断电；提醒用完关调试 | 39-40 |
| **用完关 ADB** | 关 USB 调试与 Network/ADB 调试、撤销授权、重启、确认 `nc -vz` 失败 | 41-44 |
| 停用分类 | 把包分成：厂商遥测/分析、厂商内容与广告服务、后台服务、预装应用、流媒体应用 | 47-49 |
| **只停用不卸载** | `pm disable-user --user 0`，**绝不卸载系统包** | 50 |
| **不能停的** | 电视应用与输入服务（HDMI/调谐器）、输入/源菜单、遥控与蓝牙配对、软键盘、Google Play Store 与 Play Services | 51-53 |
| 查清再停 | 厂商包名常不说明功能 → 先 `dumpsys package` 看 activities/services | 53-55 |
| 问用户 | 动流媒体/语音助手/Chromecast/Miracast/更新服务前，**先列清单问用户用了哪些** | 56-57 |
| **一次一条** | `pm` 命令一次 shell 调用一条，链式可能静默中断 | 58 |
| **小批 + 截图** | 分小批停用，批间截图；一批可能带走承重的东西 | 59-60 |
| 卸载应用广告 | Android 9+ Private DNS 指向 AdGuard，`private_dns_mode hostname`；撤销是 `private_dns_mode opportunistic` | 61-64 |

**对本项目最有用的两条**：
- `PROMPT.md:50`「**只停用不卸载**」——与 `tools/disable.sh:3` 一致，是整个可逆性体系的地基。
- `PROMPT.md:36-38`「**先备份 + 写恢复脚本**」——与 `tools/disable.sh:125-128` 的「先落盘记录再动手」是同一条原则的两种表述。

### 5.6 stock launcher 怎么「探测」出来

`launcher/build-and-install.sh:22-57` 是一段**四分支**的 Home 探测：

| 情形 | 处理 | 行号 |
|---|---|---|
| 当前 HOME owner 在已知白名单里 | `STOCK_LAUNCHER=$HOME_PKG` | `:29-30` |
| HOME 已属于本项目 launcher | `STOCK_LAUNCHER=none`（不停任何东西） | `:32-33` |
| HOME 是 Android 选择器（`android`，两个 launcher 打平） | 查 `cmd package query-activities --brief ... HOME` 的全部 claimant，**若恰好只有一个已知 stock launcher** 就用它 | `:34-46` |
| 其它（未知 launcher） | **报错退出**，并明确列出 `android` / `setupwraith` / 其它三种含义 | `:47-55` |

已知 stock launcher 白名单（`launcher/build-and-install.sh:23`）：

```bash
KNOWN_STOCK_LAUNCHERS="com.google.android.tvlauncher com.google.android.apps.tv.launcherx com.google.android.leanbacklauncher com.mitv.tvhome"
```

**注释里的关键洞察**（`:35-37`）：Android 的选择器状态正是「`restore.sh` 之后又套一份清单」的结果——所以他们的 `restore` 与 `install` 脚本必须能处理这种状态，而不是假设 HOME 永远指向某个具体包。

**对我们的启示**：我们的 `adb.wifi.launch` / `adb.wifi.home_query` 能力应该**返回 Home 的完整 claimant 列表**，而不是单个 owner，因为「owner 是什么」取决于优先级，而「谁有资格」是集合问题。

---

## 6. 品牌去废清单的组织与「先探测再决定停什么」
### 6.1 清单放哪、命名规则

- **目录**：`examples/<brand>-<model>/debloat.txt`，例如 `examples/xiaomi-mi-tv-4a/debloat.txt`。
- **数量**：本仓库有 **123 个品牌/型号目录**（`examples/` 下 124 项，其中 1 个是 `README.md`）。
- **命名**：`<brand>-<model>`，全小写、连字符分隔、去掉空格与特殊符号。实例：`xiaomi-mitv-aesp0`、`tcl-g03-google-tv`、`amazon-fire-tv-stick-4k-2018`、`haier-h32s80efx`、`benq-e55-735`。
- **每个目录的内容**：
  - `debloat.txt` —— 要停的包名清单。
  - `config.json`（**可选**，只有 4 个型号有）—— launcher 行为配置（dock/隐藏应用/来源面板/列数/Live TV 包/按键 keycode）。
- **索引**：`examples/README.md`（138 行）是一张 **123 行的大表**，每行给出：目录名、机型全称与硬件/固件信息、地区、Android/Google TV 版本、以及**逐条说明停了什么、为什么、有什么副作用**。例如 `examples/README.md:103`（tcl-g03）就写明了「setupwraith owned the Wi-Fi: forget and re-join first」「Input key works without InputKeyService, gear is keycode 4077」。

**这套「一张表 + 每个型号一段人话笔记」的组织方式对本项目很有价值** —— 品牌/型号差异只能靠实测笔记沉淀，不能靠猜。

### 6.2 有没有「先探测再决定停什么」的机制

**有，但不在清单层，在执行层。** 三处：

1. **包名校验 + 整份拒绝**（`tools/disable.sh:24-28`）——先校验清单里每个名字是合法包名，任何一条不合法就整份拒绝，**不做部分执行**。
2. **三道运行时护栏**（2.7 节）——执行前**先探测设备状态**（当前 Wi-Fi 属主、当前 HOME owner、哪些包要 TV_INPUT_HARDWARE），探测到危险就**跳过该包并打印原因**；探测失败则**保守跳过**（setupwraith）或**保守不跳过但明确警告**（TV_INPUT）。
3. **探测用一轮往返**（`tools/disable.sh:96-106`）——把探测循环放到**设备上跑**，只回传结果，不做 N 次往返。

**但**：清单本身**不是**按设备自动生成的。`examples/README.md` 的表是**人工实测**沉淀的。所以「先探测再决定停什么」= **执行时的护栏**，不是**生成清单时的自适应**。

**对本项目的启示**：我们的 `adb.wifi.disable_pkg` 能力应当**自带这三道护栏**（或至少提供 `dry_run` 模式先报告「我会跳过 X，因为 Y」），而不是当 dumb executor。

---

## 7. 许可与安全

### 7.1 LICENSE —— 是什么，能不能抄

**`LICENSE`：MIT License, Copyright (c) 2026 Gathin**（另有 Outfit 字体的 OFL 说明）。

**结论：可以抄、可以改、可以闭源分发**，条件是：
- 在**所有副本/衍生作品的显著位置**保留版权声明与 MIT 许可全文；
- 不得声称作者为你的作品背书。

**依赖的 `adb_shell`：Apache License 2.0**（`tmp/adbshell_dl/adb_shell-0.4.4/LICENSE` 首行 “Apache License Version 2.0, January 2004”；`PKG-INFO:10` `Classifier: License :: OSI Approved :: Apache Software License`）。
- Apache-2.0 允许闭源分发，条件：保留许可与 NOTICE、声明修改、注明专利授权终止条款。
- `adb_shell` 自身包含从 Google AOSP 拷贝的代码（Apache-2.0）。

**给本项目的结论**：
- **MIT 的部分**（`disable.sh` / `recovery.py` / `b64push.py` / `adb-closed.py` / `build-and-install.sh` 的**设计思路**）：可以直接借鉴甚至改写移植，只需保留版权声明。
- **但我方的建议是「重写而非逐字抄」**，原因：他们的代码是 shell 脚本 + 面向贡献者的清单工作流，与我们「Python 能力层 + DeviceResult 六态信封 + shell 白名单」的架构不匹配；逐字抄会引入一个我们不想要的世界观（自由执行任意 shell、脚本化 CLI）。
- **`adb_shell` 是 pip 依赖，不是拷贝**：我们应在 `requirements` 里**依赖它**而不是把它的源码拷进仓库（Apache-2.0 允许，但依赖更干净、可升级、可审计）。

### 7.2 ADB 暴露的安全警告（原文）

**`SECURITY.md:25-26`**：

> “ADB itself is powerful by design. Turning on USB or network debugging lets any computer on your network ask to connect, and the TV asks you to allow it. Turn debugging off when you're done.”

**`SECURITY.md:3-4`**：

> “CleanYourTV talks to TVs over ADB and installs a launcher on them, so a security bug here can reach people's living rooms. Please report anything you find privately, not in a public issue.”

**`README.md:355-363`（"Isn't ADB access a security hole?"）**：

> “It is one explicit "allow this computer" tap on the TV, the same as developing an Android app. Network ADB is plaintext on port 5555, and on firmware with `ro.adb.secure=0` any device on the LAN gets a shell, so close it when you are done:”

关闭步骤（`README.md:357-364`）：
1. 电视上：开发者选项 → 关掉 **USB debugging** 和 **Network/ADB debugging**，并撤销 USB 调试授权。
2. 重启电视，然后从电脑确认端口没了：`nc -vz <tv ip> 5555` 必须失败。没有 `nc`（Windows）：`python tools/adb-closed.py <tv ip>` 必须说端口已关。
3. “The launcher keeps working without debugging.”

**`docs/TROUBLESHOOTING.md:37`**：

> “**Close port 5555 when you're done**: plaintext, and a shell for anyone on the LAN when `ro.adb.secure=0`.”

**`PROMPT.md:41-44`**：

> “When you finish, close network ADB instead of leaving it open: switch USB debugging and Network/ADB debugging off on the TV, revoke the USB debugging authorisations, reboot, and check that `nc -vz <tv ip> 5555` fails. Port 5555 is plaintext, and on firmware with `ro.adb.secure=0` any device on the LAN can get a shell.”

### 7.3 `ro.adb.secure=0` 的风险（具体机制）

- `ro.adb.secure` 是 Android 的属性：**为 1 时**，网络 ADB 连接需要用户在电视上确认「允许」；**为 0 时**，**局域网内任意设备连上 5555 就直接拿到 shell，无需授权**。
- 仓库里对它有专门处理：
  - `tools/disable.sh:52-54` 读取它：
    ```bash
    ADB_SECURE="$("$PY" -I tools/tv.py "getprop ro.adb.secure" 2>/dev/null | tr -d '[:space:]')" || ADB_SECURE=""
    ```
    注释：读失败只是跳过提示，不影响主流程。
  - `tools/disable.sh:135-139` 若为 `0`，在结束时警告用户去关掉。
  - `tools/tv.py:61-65` 的 `--probe` 会把 `ro.adb.secure` 一起打出来（8 个 getprop 之一）。
  - `tests/test_adb_exposure.py`（71 行）专门断言这个警告行为。
- **对本项目的含义**：`adb.wifi.*` 能力的每次输出（或至少 probe）都应该带上 `ro.adb.secure` 的值与「用完请关闭网络 ADB」的提醒。**这是我们能做、而大多数 ADB 封装不做的事。**

### 7.4 我们自己的额外风险面（本项目特有）

`SECURITY.md:18-23` 列的「什么算安全问题」是针对他们的场景。对我们（一个设备编排服务）还要额外考虑：

| 风险 | 说明 | 缓解 |
|---|---|---|
| **shell 白名单被绕过** | 我们的 `common.py` 用三层检查（`EXEC_ALLOWLIST` / `INTERPRETERS` 显式开关 / `SHELL_META` 元字符拒绝） | 保持白名单；`adb.wifi.shell` 必须**同样**走白名单，不因「是电视」而放宽 |
| **包名注入** | `pm disable-user --user 0 <pkg>` 里的 `<pkg>` 会进设备 shell | 用 `PACKAGE_RE` 全量校验（抄他们的做法） |
| **批量停用不可逆** | 停掉承重包（HDMI/ tuner）不可逆 | 抄三道护栏 + 停用前落盘记录 |
| **凭证泄露** | ADB 私钥 `~/.android/adbkey` | 不外传、不写日志；我们若生成自己的 key 需放用户目录并限制权限 |
| **明文 5555** | 局域网可嗅探 | 能力层文档写明；提供「关闭后确认端口已关」的能力（参考 `adb-closed.py`） |
| **静默失败** | 链式命令、空输出、探测失败 | 一次一条命令；空输出必须报「空」而不是「成功」；探测失败保守处理 |

---

## 8. 落地建议（给 `fleet/kinds/adb_wifi.py` 的具体方案）

### 8.1 纯 Python ADB 客户端 vs 调 adb.exe —— 明确建议

**建议：双传输层，`adb.exe` 优先 + `adb_shell` fallback；不自研完整 ADB 客户端。**

```
                 fleet/kinds/adb_wifi.py（能力层，不感知协议细节）
                                    │
                      ┌─────────────┴─────────────┐
                      │                           │
              AdbExecTransport            AdbShellTransport
              （调 adb.exe）            （Python 直连 5555）
                      │                           │
              需 platform-tools          依赖 adb-shell==0.4.4
              错误=退出码+stderr         错误=结构化异常
```

**为什么不自研完整 ADB 客户端**：

| 理由 | 详情 |
|---|---|
| **协议坑全是静默失败** | magic 是逐命令异值（不是常量）、checksum 是字节和（不是 CRC32）、`WRTE` 必须回 `OKAY` 否则卡死、AUTH 时序有 4 步、filesync 有 10 个子命令状态机、Android 7 前 OPEN payload 4096 上限。**每一处写错都不会立刻报错，而是表现为「连不上」「卡住」「文件损坏」** |
| **没有测试设备** | 我们手上没有 Android TV 实机（只有手机），**无法为自研协议写有意义的回归测试**。`adb_shell` 有大量用户验证过的边缘设备兼容 |
| **许可允许且成熟** | `adb_shell` = Apache-2.0，pip 依赖一行搞定，可审计可升级 |
| **AOSP 差异是持续漂移的** | `A_VERSION_SKIP_CHECKSUM`、wireless debugging 的 TLS 配对、adb 协议的新版本，都是官方在动的东西。自己追 = 长期负债 |

**为什么不纯用 adb.exe**：

| 理由 | 详情 |
|---|---|
| **Windows 上不一定有** | 我们的目标用户是 Windows + 电视盒子的组合，`platform-tools` 不是预装的 |
| **macOS Local Network 隐私会静默拦截** | `docs/TROUBLESHOOTING.md:8-10` 明确记载：端口是开的，`adb connect` 却报 “No route to host” |
| **打包分发** | 我们要「拷过去就能用」，不能要求用户先装 Android Studio / platform-tools 并配 PATH |
| **错误信息不可控** | adb.exe 只给退出码 + stderr 文本，**不满足团队「失败必须带原因」的硬要求**——我们得自己维护一张脆弱的字符串匹配表 |
| **`adb connect` 需要两步** | 先 `adb connect ip:5555` 再操作，状态散在 adb server 里（还可能是残留的旧连接） |

**那 adb.exe 优先的价值在哪？**
- 它**已经处理了所有 AOSP 边缘情况**，且**它报的错更全**（"device unauthorized"、"device offline" 这类是 adb server 层的概念，Python 直连拿不到）。
- 在装了 platform-tools 的开发机上，用它调试问题最省事。
- **但它必须被封装成可选后端，且永远不是唯一后端。**

**结论落地为三条规则**：
1. `adb.wifi.*` 的实现**只依赖一个内部 `AdbLink` 抽象**（`connect / shell / push / pull / close`），有两个实现。
2. 默认**优先 `adb_shell`**（依赖可控、错误可结构化、跨平台一致）；`adb.exe` 作为**可选增强**（用户显式开启或 `adb_shell` 不可用时）。
   > 注：这里与我上文「adb.exe 优先」的表述修正为 **`adb_shell` 优先**——因为本项目硬要求「失败必须带原因」，而 `adb_shell` 的异常是结构化的、可映射到 `DeviceResult` 六态；`adb.exe` 的错误需要脆弱的字符串匹配。**唯一应该优先 adb.exe 的场景是「用户机器上确实装了 platform-tools 且希望与 Android Studio 共用同一台 adb server」**，此时可作为可配置项。
3. **绝不自己实现 sync 协议**（`adb_shell` 已实现，见 1.6）。

### 8.2 哪些值得抄、哪些必须重写

| 组件 | 抄 or 重写 | 说明 |
|---|---|---|
| ADB 协议实现（header/CNXN/AUTH/shell/sync） | **不抄，自研亦不抄** → **用 `adb_shell`** | 见 8.1 |
| `tools/disable.sh` 的**三道护栏** | **抄设计，重写代码** | 护栏的**判据与 fail-safe 方向**是精华（探测失败 → 保守不动作），实现要落到我们的 Python 能力层 |
| `tools/recovery.py` 的**记录模型**（三分法 + first-wins + 原子写） | **抄设计，重写代码** | 与我们的 `DeviceResult`/可逆性要求完全同构 |
| `tools/restore.sh` 的「**计划先整体构建，构建失败则一条都不发**」 | **抄设计** | 极有价值的一条 |
| `tools/b64push.py` 的**分块公式**（`min(48KB, max_chunk*3//4-64)`，对齐 3 的倍数） | **抄公式，重写代码** | 公式可直接用（数学与实现无关）；逐块 ack + 整体 md5 的双重校验照抄 |
| `tools/adb-closed.py` 的**退出码语义与「超时也算关闭」** | **抄设计，重写代码** | 作为 `adb.wifi.port_check` 能力 |
| `tools/tv.py` 的**三个 Windows 兼容处理**（`errors="replace"`、`utf-8-sig`、复用 `~/.android/adbkey`） | **照抄** | 纯技术细节，与架构无关 |
| `launcher/build-and-install.sh` 的**安装顺序与验证分层** | **抄设计** | 「单独执行 + 双条件判定 + 归属校验 + 回滚 + 兜底指令文件」这一整套流程 |
| `examples/*/debloat.txt`（123 份清单 + `examples/README.md` 笔记表） | **可作为数据参考** | **不是代码**。若我们要做品牌适配，这是最有价值的现成素材；引用时保留 MIT 版权声明 |
| `PROMPT.md` 的作业规范 | **抄设计，写成我们自己的规范文档** | 尤其「一次一条 `pm`」「小批 + 截图」「用完关 ADB」 |

**逐字抄代码几乎不需要** —— 他们是 bash + Python CLI，我们是 Python 能力层，形态差异大。**值得抄的是「判断」与「顺序」，不是「语句」。**

### 8.3 本项目已有能力 → cleanyourtv 能力映射

本项目现有契约：`fleet/core/device.py:37-49`（`BaseDevice.probe/caps/invoke`）、`fleet/core/result.py:23-33`（六态 + `RETRYABLE`）、`common.py:69-105`（shell 白名单三层）、`fleet/transports/adb.py`（现有 `AdbTransport` 走 `android_device.adb_path()`，即依赖 platform-tools）。

**映射表**（我们已有 → 目标形态 → 对应参考）：

| 我们已有 | 目标能力名 | cleanyourtv 对应 | 关键差异 / 注意事项 |
|---|---|---|---|
| `adb.wifi.connect` | `adb.wifi.connect` | `tools/tv.py:52-55` `AdbDeviceTcp(...).connect(rsa_keys=[...])` | 我们要**封装认证失败 → `denied`**、超时 → `timeout`；并**复用 `~/.android/adbkey`**（`tools/tv.py:36-38`）避免电视重复弹窗 |
| `adb.wifi.shell` | `adb.wifi.shell` | `tools/tv.py:80,86` `dev.shell(line, read_timeout_s=120.0)` | **我们必须过白名单**（见 8.4）；超时必须显式传（`adb_shell` 默认 10s 太短） |
| `adb.wifi.install` | `adb.wifi.install` | `tools/tv.py:66-68` `dev.push(...)` + `launcher/build-and-install.sh:67` `pm install -r` | **大文件先 b64push**（`tools/b64push.py`）；`pm install` 判 `Success`（`launcher/build-and-install.sh:69-72`） |
| `adb.wifi.screenshot` | `adb.wifi.screenshot` | `docs/TROUBLESHOOTING.md:56-59` | **MediaTek 会先吐 `Init wrapper sys mutex successful`** → 必须校验 `\x89PNG` 头，失败则回退「`screencap -p` 存文件 + pull」；**空图要单独报告**（`:63-66`） |
| `adb.wifi.launch` | `adb.wifi.launch` | `launcher/build-and-install.sh:77-84` `am start -W` | **双条件判定**：`Status: ok` **且** 无 `^Error`；只判前者会误报成功 |
| `adb.wifi.disable_pkg` | `adb.wifi.disable_pkg` | `tools/disable.sh:130-133` `pm disable-user --user 0 <pkg>` | 必须带 `--user 0`；**只停用不卸载**；包名过 `PACKAGE_RE` |
| `adb.wifi.enable_pkg` | `adb.wifi.enable_pkg` | `tools/recovery.py:238` `pm enable <pkg>` | 恢复时**只 enable 原本 enabled 的** |
| `adb.wifi.list_disabled` | `adb.wifi.list_disabled` | `tools/recovery.py:112-123` `pm list packages -d` | `pm list packages` 返回空 → 报「设备没响应」而不是「什么都没有」 |
| （无）**需要新增** | `adb.wifi.debloat` | `tools/disable.sh` 整体 | **落盘恢复记录 → 三道护栏 → 一次一条执行 → 分批** |
| （无）**需要新增** | `adb.wifi.restore` | `tools/restore.sh` + `recovery.py --commands` | 「计划整体构建 → 失败则一条不发」 |
| （无）**需要新增** | `adb.wifi.home_query` | `cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME` | 用来做 Home 归属校验；结果必须过 `HOME_RE`（`tools/recovery.py:28`），`android` 这种选择器要拒 |
| （无）**需要新增** | `adb.wifi.port_check` | `tools/adb-closed.py` | 返回「已关/仍开/无法检查」三态，对应 `ok`/`unavailable` |
| （无）**需要新增** | `adb.wifi.exposure` | `getprop ro.adb.secure`（`tools/tv.py:64`） | 输出必须带「用完关闭网络 ADB」提醒 |

**capability 命名**：本项目 `fleet/catalog.py` 用 `domain.verb` 约定（现有如 `screen.read` / `pc.shell` / `shell.exec`）。建议统一为 **`adb.wifi.<verb>`**（`connect` / `shell` / `install` / `screenshot` / `launch` / `disable_pkg` / `enable_pkg` / `list_disabled` / `debloat` / `restore` / `home_query` / `port_check` / `exposure`），与 task #8「在 catalog.py 登记 adb.wifi.* 能力」一致。

### 8.4 shell 白名单 vs 他们自由执行 shell —— 差别与可逆性设计

**他们的做法**：**完全自由**。
- `tools/tv.py:85-87`：任意参数直接当 shell 命令发出去：
  ```python
  out = dev.shell(" ".join(args), read_timeout_s=120.0)
  ```
- `tools/tv.py:72-84` 的 `--batch` 也只是逐行执行任意文本。
- 唯一的「约束」在**清单层**（包名正则），不在命令层。
- 安全边界靠**人的纪律** + `PROMPT.md` 的规范 + `SECURITY.md` 的请求。

**我们的做法**：**三层白名单**（`common.py:69-105`）：
1. `EXEC_ALLOWLIST`（`common.py:69-73`）——命令 base 必须命中。
2. `INTERPRETERS`（`common.py:77`）——解释器（python/powershell/bash/cmd…）**默认不进**白名单，要跑得显式设 `FLEET_ALLOW_INTERPRETER=1`。
3. `SHELL_META`（`common.py:81`）——`& | ; < > ` $ \n \r` **一律拒**，防链式命令。

`common.py:89-91` 的诚实边界说明值得引用：

> 「白名单内的 type/dir/cat 本来就能读服务账号能读的一切——L1 的边界是安装闸（管理员启动），不是这份名单；名单防的是『借白名单命令跳出去干别的』，不是『白名单命令本身别干它该干的事』。」

**核心差别**：

| 维度 | 他们 | 我们 |
|---|---|---|
| 命令自由度 | 任意 shell 字符串 | base 白名单 + 禁元字符 |
| 注入面 | 包名有正则保护；**其它命令全靠调用方自觉** | 结构性拒绝元字符 |
| 副作用可逆性 | 靠**记录 + 恢复脚本**（`pm disable-user` 可逆） | 同上，但**要额外能拒绝不可逆操作** |
| 错误表达 | 脚本 exit code + stderr 文本 | `DeviceResult` 六态，**失败必须带原因**（`result.py:68-69`） |

**我们的设计建议（在保留可逆性前提下做安全）**：

1. **`adb.wifi.shell` 走独立的、**比 `pc.shell` **更窄**的白名单**。
   - 建议允许的 base：`getprop` / `pm`（仅 `list`/`enable`/`disable-user`/`install`/`uninstall`? 否） / `settings`（仅 `get`/`put`/`delete`）/ `am`（仅 `start`/`startsActivity`/`get-current-user`）/ `dumpsys` / `screencap` / `input` / `cmd package` / `wm` / `df` / `cat`（限 `/system` `/proc` 等只读路径）/ `ls` / `id` / `date` / `whoami`。
   - **不要**用「base 在白名单」这种粗粒度检查 `pm`——`pm uninstall` 和 `pm install` 都在 `pm` 之下。**必须做「base + 子命令」两级匹配**（他们只做了包名一级，我们至少两级）。
2. **元字符拒绝要分级**，不能一刀切：
   - `;` `&&` `||` `|` `>` `>>` `<` `` ` `` `$()` **一律拒**（防链式与注入）。
   - **但** `settings put global x 1.0` 这种**带空格的多参数**要允许 → 所以「元字符」检查与「参数个数/子命令」检查要分开。
   - `common.py` 现有一刀切对 `adb` 场景够用（我们不需要管道），**保持一致即可**。
3. **可逆性分级**，在能力层就标出来：
   | 操作 | 可逆性 | 我们的处置 |
   |---|---|---|
   | `pm disable-user --user 0 <pkg>` | **可逆**（`pm enable`） | 允许，但**必须先落盘记录**（抄 `tools/disable.sh:125-128`） |
   | `pm enable <pkg>` | 可逆 | 允许 |
   | `settings put global <k> <v>` | **可逆**（需记录旧值） | 允许，**但要先把旧值记进恢复记录**（抄 `tools/recovery.py:183`） |
   | `cmd package set-home-activity` | 可逆（记原 owner） | 允许，**必须记原 owner** |
   | `pm install` | 可逆（`pm uninstall`） | 允许，但 install 本身是**有副作用的写**，`DeviceResult.side_effect=True` |
   | `pm uninstall` | **不可逆** | **默认拒绝**；要放开必须显式开关 + 二次确认 |
   | `pm clear <pkg>` | **数据丢失**（不可逆） | **默认拒绝** |
   | `rm -rf` | **不可逆** | **拒绝**（且 `rm` 不进白名单） |
   | `reboot` | 状态中断 | 可允许，但 `side_effect=True` + 明确后果 |
4. **三道护栏落到能力层**（`adb.wifi.debloat`）：
   - setupwraith 护栏：`dumpsys wifi | grep -o 'cname=[^ ]*'`（**在我们这里是用 shell 能力执行探测，还是直接 `adb_shell` 拿原始输出后本地正则？→ 用后者，不依赖目标端有 grep**）。
     - **注意差异**：他们的 `grep -o` 跑在**设备上**；我们如果用 `adb_shell` 直接拿 `dumpsys wifi` 全文再在**本地**正则，更稳（不依赖 toybox grep 支持 `-o`）。
   - Home 护栏：查 HOME 候选，**排除 `FallbackHome` 与 setupwraith/settings/provision**（`tools/disable.sh:77-79`）。
   - TV_INPUT 护栏：查 `requested permissions` 块里有没有 `TV_INPUT_HARDWARE`（`tools/disable.sh:105`）。
   - **三道护栏的共同原则：探测失败 → 保守（跳过 + 报错），不猜。**
5. **`adb.wifi.debloat` 的执行形态**（综合他们 + 我们的要求）：
   ```
   ① 校验清单（包名正则；不合格整份拒绝）        ← tools/disable.sh:24-28
   ② 探测设备状态（HOME owner / cname / TV_INPUT）  ← tools/disable.sh:29-123
   ③ 计算「实际要执行的包」= 清单 − 被护栏跳过的
   ④ 落盘恢复记录（原子写；失败则中止，设备不动）  ← tools/disable.sh:125-128
   ⑤ 按 N 个一批执行，每条一次 shell（不链式）      ← PROMPT.md:58,59-60
   ⑥ 每批后回读状态并回报（可截图）
   ⑦ 任何一步失败 → 返回带原因的 DeviceResult，附上恢复命令
   ```
   **与他们的关键差异**：他们 ①②③④⑤ 是**在 bash 里顺序执行、一步失败就 exit**；我们要把它做成**一次能力调用**（`invoke("adb.wifi.debloat", {...})`），返回**结构化的 `DeviceResult`**（`data` 里放：跳过的包 + 原因、执行的包 + 各自结果、恢复记录路径），**符合本项目「上层只看到失败/成功，看不到为什么」的痛点**（`result.py:3-5` 原文）。
6. **错误表达**（照 `result.py:14-19` 的三态铁律）：
   - 连接被拒 → `unavailable` + 「目标 {ip}:{port} 拒绝连接（电视关机 / 无线调试未开 / IP 已变）」。
   - 超时 → `timeout` + 「连接 {ip}:{port} 超时（网络不通 / 被防火墙拦截 / VLAN 隔离）」。
   - 认证被拒 → `denied` + 「ADB 认证被拒，请在电视上点『允许 USB 调试？』」。
   - 设备上命令失败 → `error` + **原始 stderr 全文**。
   - 电视不支持某能力 → `unsupported`。
   - **空输出 ≠ 成功**：`pm list packages` 返回空要报 `unavailable`（抄 `tools/recovery.py:119-121`）。

### 8.5 一页纸行动清单

**立刻可做**：
1. 在 `requirements` 加 `adb-shell==0.4.4`（+ `rsa`、`cryptography` 由它带入）。
2. 实现 `fleet/transports/adb_shell_link.py`：封装 `AdbDeviceTcp` + `connect/shell/push/pull`，把 `adb_shell.exceptions` 映射到 `DeviceResult` 六态。
3. `fleet/kinds/adb_wifi.py` 的 `probe()`：三态分层（端口通不通 → 认证过没过 → 设备真活着），照 `fleet/kinds/android_phone.py:3-8` 的三层 probe 思路 + `tools/adb-closed.py` 的端口判定。
4. 复用 `~/.android/adbkey`（`tools/tv.py:36-38`）+ `sys.stdout.reconfigure(errors="replace")`（`tools/tv.py:21-25`）。
5. `adb.wifi.shell` 用**两级白名单**（base + 子命令），并沿用 `common.py` 的元字符拒绝。

**紧接着做**：
6. `adb.wifi.debloat` + `adb.wifi.restore`：落盘记录模型（三分法 + first-wins + 原子写）、三道护栏、一次一条命令、分批。
7. `adb.wifi.screenshot`：PNG 头校验 + 「存文件再 pull」回退 + 空图单独报告。
8. `adb.wifi.launch`：`am start -W` 双条件判定（`Status: ok` 且无 `^Error`）+ 随后 Home 归属校验。
9. `adb.wifi.port_check`：三态返回（已关 / 仍开 / 无法检查），超时算「已关」但附免责说明。
10. `adb.wifi.exposure`：probe 里读 `ro.adb.secure`，为 0 时在每次输出里带「用完关闭网络 ADB」提醒。

**明确不做**：
- 不自研 ADB 协议实现（不写 header/CNXN/AUTH/sync）。
- 不做 `pm uninstall` / `pm clear` / `rm`（不可逆）。
- 不逐字复制他们的 shell 脚本进我们仓库（只借鉴判断与顺序）。
- 不把 `adb.exe` 当唯一后端（可选增强，默认 `adb_shell`）。

---

## 附录 A：术语对照

| 术语 | 含义 |
|---|---|
| `maxdata` | 设备在 CNXN 响应里回报的最大 payload 字节数（`adb_shell` 里是 `_maxdata`） |
| `local_id` / `remote_id` | ADB 事务的本地/对端 id；`local_id` 每次 OPEN 自增 |
| `WRTE` / `OKAY` | 数据包 / 流控确认包（客户端收到 WRTE 必须回 OKAY） |
| `CLSE` | 关闭包，shell 输出的结束标志 |
| `service` | ADB 服务名，本项目只用 `shell:` 与 `sync:` |
| `cname` | dumpsys wifi 里 Wi-Fi 配置的名称；判断 Wi-Fi 属主靠它 |
| `FallbackHome` | 固件在无可用 launcher 时显示的占位组件，不是可用 Home |
| `setupwraith` | Google TV 首次开机向导 `com.google.android.tungsten.setupwraith` |

## 附录 B：本次调研的边界（诚实声明）

- **未改动 cleanyourtv 任何文件**：只读副本在 `tmp/cleanyourtv/`（`tmp/` 已在 `.gitignore`），**未提交到我们的 git**。
- **未把他们的代码拷贝进我们仓库**：本报告只是分析。
- `adb_shell` 源码是**从 PyPI 下载的 sdist**（`https://pypi.org/pypi/adb-shell/0.4.4/json` → `files.pythonhosted.org/.../adb_shell-0.4.4.tar.gz`），**只读阅读**，放在 `tmp/adbshell_dl/`。
- **本机没有可用的 python 可执行**（`pip: command not found`；WindowsApps 下的 python 是 stub），因此**没有运行任何字节布局验证脚本**。所有字节级事实均由 `constants.py`（`MESSAGE_FORMAT = b'<6I'`）、`adb_message.py`（`sum(data)`、`magic = command ^ 0xFFFFFFFF`）与 AOSP `adb.h` / `transport.cpp` **三方交叉确认**得出，未依赖运行时验证。
- **未在真实 Android TV 上实测**：`adb_shell` 的行为描述均来自其源码与文档，不是我们的实测。
- **`host:connect:` 未查到**：在 cleanyourtv 与 adb_shell 中均未出现。若我们后续需要 adb server 路径，只能走 `adb.exe` 后端。
- 报告中的行号基于 `git clone --depth 1` 当时的 commit；上游更新后行号可能漂移。





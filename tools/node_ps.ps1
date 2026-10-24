param(
    [string]$Srv  = "192.168.231.90",
    [int]   $Port = 9000,
    [string]$Pass = "sky-test-9f3a",
    [string]$Name = "PC2",
    [int]   $Lifetime = 0,     # 跑多少秒后自动退出（0 = 一直等指令；测试用）
    [switch]$SelfTest          # 只跑自检：认证→注册→本地执行一条→打印回执→退出
)
# 天网 · 受控端（被控电脑上跑这个）
# 协议：4 字节大端长度 + UTF-8 JSON（与 common.py 一致，不另造格式）
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

# 与 common.EXEC_ALLOWLIST 保持一致：执行方自己也要拦，不能只靠发令方自觉
$ALLOW = @('echo','ping','ver','systeminfo','dir','ls','whoami','hostname',
           'type','cat','ipconfig','tasklist','date','time','set','path')
$META = @('&','|',';','<','>',[string][char]96,[string][char]36,[string][char]10,[string][char]13)

$client = New-Object Net.Sockets.TcpClient
$client.Connect($Srv, $Port)
$client.NoDelay = $true
$st = $client.GetStream()
$st.ReadTimeout = 5000

function Send-Msg($o) {
    $b = [Text.Encoding]::UTF8.GetBytes(($o | ConvertTo-Json -Compress -Depth 8))
    $len = [BitConverter]::GetBytes([int]$b.Length)
    [Array]::Reverse($len)
    $st.Write($len, 0, 4)
    $st.Write($b, 0, $b.Length)
    $st.Flush()
}
function Read-Exact([int]$n) {
    $buf = New-Object byte[] $n
    $off = 0
    while ($off -lt $n) {
        $r = $st.Read($buf, $off, $n - $off)
        if ($r -le 0) { throw "连接被对端关闭" }
        $off += $r
    }
    return ,$buf
}
function Read-Msg {
    $h = Read-Exact 4
    [Array]::Reverse($h)
    $n = [BitConverter]::ToInt32($h, 0)
    $body = Read-Exact $n
    return ([Text.Encoding]::UTF8.GetString($body) | ConvertFrom-Json)
}

# ---- 1. 认证 ----
Send-Msg @{ t = "auth"; name = $Name; password = $Pass }
$resp = Read-Msg
if ($resp.t -ne "auth_ok") { Write-Host "[X] 认证失败: $($resp | ConvertTo-Json -Compress)"; exit 1 }
Write-Host "[ok] 认证通过 token=$($resp.token.Substring(0,8))..."

# ---- 2. 注册（报能力 + 这台机器的底细）----
$os = "unknown"
try { $os = (Get-CimInstance Win32_OperatingSystem -ErrorAction Stop).Caption } catch { $os = [Environment]::OSVersion.VersionString }
$info = @{
    hostname = $env:COMPUTERNAME
    user     = $env:USERNAME
    os       = $os
    cpus     = [Environment]::ProcessorCount
    sandbox  = $PWD.Path
    agent    = "powershell"
}
Send-Msg @{
    t = "register"; name = $Name; platform = "windows"
    capabilities = @("exec", "push", "pull")
    capability_specs = @{}
    info = $info
}
Write-Host "[ok] 已注册为节点 $Name（$($env:COMPUTERNAME) / $os）"

if ($SelfTest) {
    # 自检只证明四件事：连上了 / 认证过 / 注册发了 / 执行+回执帧拼对了。
    # 不走服务端下发（那条要有人在对面按），所以本地模拟一条 exec。
    $probe = "hostname"
    $o = (& cmd.exe /c $probe 2>&1 | Out-String).Trim()
    Send-Msg @{ t = "exec_result"; id = "selftest-1"; rc = 0; out = $o; err = "" }
    Write-Host "[ok] 自检执行 `"$probe`" => $o"
    Write-Host "[ok] 回执帧已发出（长度前缀 + JSON）"
    $client.Close()
    exit 0
}

Write-Host "     等指令中… （Ctrl+C 断开的不是你的电脑，只是这条连接）"

# ---- 3. 干活循环 ----
$lastHb = [DateTime]::UtcNow
$t0 = [DateTime]::UtcNow
while ($true) {
    if ($Lifetime -gt 0 -and ([DateTime]::UtcNow - $t0).TotalSeconds -ge $Lifetime) {
        Write-Host "[i] 到点退出（Lifetime=$Lifetime s）"
        break
    }
    try {
        $m = Read-Msg
    } catch [System.IO.IOException] {
        # 读超时：不是断线，趁空档发心跳
        if (([DateTime]::UtcNow - $lastHb).TotalSeconds -ge 15) {
            Send-Msg @{ t = "hb" }
            $lastHb = [DateTime]::UtcNow
        }
        continue
    } catch {
        Write-Host "[!] 连接结束: $($_.Exception.Message)"
        break
    }
    if ($null -eq $m) { break }

    switch ($m.t) {
        "exec" {
            $cmd = [string]$m.cmd
            $id  = $m.id
            $why = $null
            if ([string]::IsNullOrWhiteSpace($cmd)) { $why = "空命令" }
            elseif ($ALLOW -notcontains (($cmd.Trim() -split '\s+')[0]).ToLower()) {
                $why = "命令不在白名单: $($cmd.Trim().Split(' ')[0])"
            } else {
                foreach ($c in $META) { if ($cmd.Contains($c)) { $why = "含 shell 元字符 $c（禁止链式命令）"; break } }
            }
            if ($why) {
                Send-Msg @{ t = "exec_result"; id = $id; rc = -1; out = ""; err = $why }
                Write-Host "[拒绝] $cmd —— $why"
                continue
            }
            Write-Host "[执行] $cmd"
            $out = ""
            try {
                $out = (& cmd.exe /c $cmd 2>&1 | Out-String)
                $rc = $LASTEXITCODE
                if ($null -eq $rc) { $rc = 0 }
            } catch {
                $rc = -1; $out = "$($_.Exception.Message)"
            }
            Send-Msg @{ t = "exec_result"; id = $id; rc = $rc; out = $out; err = "" }
            Write-Host "[回执] rc=$rc 共 $($out.Length) 字"
        }
        "push" { Send-Msg @{ t = "push_result"; id = $m.id; ok = $false; err = "这个 PowerShell 版不接推文件" } }
        "pull" { Send-Msg @{ t = "pull_result"; id = $m.id; ok = $false; data = ""; err = "这个 PowerShell 版不接取文件" } }
        default { }
    }
}
$client.Close()

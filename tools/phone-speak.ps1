# 手机出声 / 云端合成的一键启动。
#
# 为什么需要它：手机自带的系统 TTS 在 HyperOS 上是废的
# （TextToSpeech 初始化直接回 -1，连系统设置页的『播放』都不产生音频轨道），
# 所以合成放到 PC 上，手机只负责播。这条链路有两个必须同时在的东西：
#
#   1. agent_server 在 9124 上出 /api/tts
#   2. adb reverse tcp:9124 tcp:9124 把手机本地的 9124 接到 PC
#
# 少任何一个，手机都会静默退回系统引擎 = 没声（不报错，最坑）。
# 这条脚本一次把两件都做完，并回读确认。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\phone-speak.ps1
#   powershell -ExecutionPolicy Bypass -File tools\phone-speak.ps1 -Stop

param(
    [switch]$Stop,
    [int]$Port = 9124,
    [string]$Token = "tianwang-local"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$py   = "D:\DevTools\Python312\pythonw.exe"
$adb  = "D:\DevTools\Android\platform-tools\adb.exe"

function Get-Listener([int]$p) {
    Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
}

if ($Stop) {
    $l = Get-Listener $Port
    if ($l) {
        foreach ($conn in $l) {
            try {
                [System.Diagnostics.Process]::GetProcessById($conn.OwningProcess).Kill()
                Write-Output ("已停 agent_server PID=" + $conn.OwningProcess)
            } catch { Write-Output ("停失败: " + $_.Exception.Message) }
        }
    } else { Write-Output ("$Port 本来就没在听") }
    if (Test-Path $adb) {
        & $adb reverse --remove tcp:$Port 2>&1 | Out-Null
        Write-Output "已撤 adb reverse"
    }
    exit 0
}

# 1) 网关
$l = Get-Listener $Port
if ($l) {
    Write-Output ("agent_server 已在跑 (PID=" + $l[0].OwningProcess + ")")
} else {
    if (-not (Test-Path $py)) { $py = "python" }
    # pythonw 不弹控制台；Start-Process 里不要用 -Wait，否则这脚本就卡住了
    Start-Process -FilePath $py `
        -ArgumentList @("-m", "fleet.agent_server", "--port", "$Port", "--token", "$Token") `
        -WorkingDirectory $root -WindowStyle Hidden
    Start-Sleep -Seconds 4
    $l = Get-Listener $Port
    if ($l) { Write-Output ("agent_server 已起 (PID=" + $l[0].OwningProcess + ")") }
    else { Write-Output "ERROR: agent_server 起不来，单独跑一次看报错"; exit 1 }
}

# 2) 自检 /api/tts —— 只看 HTTP 200 不够，还要确认真的是 wav
#    进度条必须关掉，否则 Invoke-WebRequest 会把逐块读取进度刷满屏
$ProgressPreference = "SilentlyContinue"
try {
    $u = "http://127.0.0.1:$Port/api/tts?text=" + [uri]::EscapeDataString("启动自检") + "&voice=xiaoxiao"
    $r = Invoke-WebRequest -Uri $u -Headers @{ "X-Fleet-Token" = $Token } -TimeoutSec 30 -UseBasicParsing
    $bytes = $r.RawContentStream.ToArray()
    $n = $bytes.Length
    # 只比前 4 字节 "RIFF"：后面 4 字节是长度（随内容变，不能写死 0），
    # 第 8..11 是 "WAVE"。写成 "RIFF"+四个 0 会永远判假。
    $hdr = [System.Text.Encoding]::ASCII.GetString($bytes[0..3])
    if ($hdr -eq "RIFF" -and $n -gt 1000) {
        Write-Output ("/api/tts OK   " + $n + " 字节，真 WAV")
    } else {
        Write-Output ("/api/tts 返回了但不是 WAV（头=" + $hdr + "，共 " + $n + " 字节）")
    }
} catch {
    Write-Output ("/api/tts 失败: " + $_.Exception.Message)
}

# 3) USB 反代
if (Test-Path $adb) {
    $dev = & $adb devices 2>&1 | Select-String -Pattern "\sdevice$"
    if (-not $dev) {
        Write-Output "没检测到手机（adb devices 为空），反代跳过"
    } else {
        & $adb reverse ("tcp:" + $Port) ("tcp:" + $Port) 2>&1 | Out-Null
        $list = (& $adb reverse --list 2>&1) -join " "
        if ($list -match ("tcp:" + $Port)) {
            Write-Output ("adb reverse OK  " + $list.Trim())
        } else {
            Write-Output "ERROR: adb reverse 没建成"
        }
    }
} else {
    Write-Output ("找不到 adb: " + $adb)
}

Write-Output ""
Write-Output "完成。手机端 App 打开喇叭开关即可（她会在每次回答结束时念出来）。"
Write-Output "注意：拔线 / 手机重启 / adb server 重启后反代会断，语音会静默失效，重跑本脚本。"

# Turn on Windows Mobile Hotspot (Wi-Fi Direct tethering).
# ASCII-only on purpose: PowerShell 5.1 reads UTF-8 without BOM as ANSI,
# so non-ASCII comments would be garbled. Keep it plain English.
#
# Why this exists: RTL8821CE's "software radio" can sit in the Off state,
# which leaves the Settings toggle dead and makes `netsh wlan show networks`
# fail with "wireless interface is powered down". Starting tethering through
# NetworkOperatorTetheringManager flips that software radio ON as a side
# effect, so this script also serves as the "fix the radio" button.
#
# Log: %LOCALAPPDATA%\fleet\hotspot.log

param(
    [int]$WaitSeconds = 15
)

$ErrorActionPreference = 'Stop'
$logDir = Join-Path $env:LOCALAPPDATA 'fleet'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$log = Join-Path $logDir 'hotspot.log'

function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $log -Value $line -Encoding UTF8
    Write-Output $line
}

try {
    [Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType = WindowsRuntime] | Out-Null
    [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager, Windows.Networking.NetworkOperators, ContentType = WindowsRuntime] | Out-Null

    # Pick the profile that actually has internet. Ethernet is the normal case
    # here, but an eventual USB-tether or Wi-Fi uplink should work as well.
    $conn = $null
    foreach ($p in [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles()) {
        if ($p.GetNetworkConnectivityLevel() -eq 'InternetAccess') { $conn = $p; break }
    }
    if (-not $conn) { throw 'no connection profile with InternetAccess (uplink down?)' }
    Write-Log ("uplink=" + $conn.ProfileName)

    $tm = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($conn)

    # NOTE: do not call ConfigureAccessPointAsync here. PowerShell surfaces the
    # WinRT async object as __ComObject without GetResults(), so awaiting it is
    # not possible without Add-Type (blocked by policy on this box). The AP
    # config is already persisted by the OS, so we simply reuse it.

    if ($tm.TetheringOperationalState -ne 'On') {
        $null = $tm.StartTetheringAsync()
    }

    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($tm.TetheringOperationalState -eq 'On') { break }
        Start-Sleep -Milliseconds 500
    }

    $ap = $tm.GetCurrentAccessPointConfiguration()
    Write-Log ("state=" + $tm.TetheringOperationalState + " ssid=" + $ap.Ssid + " clients=" + $tm.ClientCount)
    if ($tm.TetheringOperationalState -ne 'On') { exit 1 }
    exit 0
}
catch {
    Write-Log ("FAILED: " + $_.Exception.Message)
    exit 1
}

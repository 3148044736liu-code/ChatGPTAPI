$ErrorActionPreference = "Continue"
$logDir = "C:\ChatGPTAPI\logs"
$logFile = Join-Path $logDir ("restart_{0}.log" -f (Get-Date -Format "yyyy-MM-dd_HHmmss"))
[IO.Directory]::CreateDirectory($logDir) | Out-Null
function Log($msg) {
    $line = "{0} | {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -LiteralPath $logFile -Encoding UTF8 -Value $line
    Write-Output $line
}

Log "=== restart begin ==="
Log "Generated.py mtime: $((Get-Item 'C:\ChatGPTAPI\src\files\generated.py').LastWriteTime)"

# Snapshot pre-restart state.
try {
    $conn = Get-NetTCPConnection -LocalPort 3061 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        Log "Pre-restart: 3061 held by PID=$($conn.OwningProcess) Name=$($proc.ProcessName) StartTime=$($proc.StartTime)"
    } else {
        Log "Pre-restart: 3061 not in LISTEN"
    }
} catch { Log "Pre-restart probe failed: $_" }

# 1. Stop the supervisor (and its descendants, in a best-effort way).
Log "Step 1: Stop-ScheduledTask ChatGPTAPI-Interactive"
try {
    Stop-ScheduledTask -TaskName "ChatGPTAPI-Interactive" -ErrorAction Stop
} catch { Log "Stop-ScheduledTask error: $_" }
Start-Sleep -Seconds 3

# 2. Manually re-kill the API process tree if anything is still alive, so
#    the new supervisor's Free-ApiPort can win the port cleanly.
foreach ($name in @("pythonw.exe","python.exe","node.exe","chrome.exe")) {
    $procs = Get-CimInstance Win32_Process -Filter "Name='$name'" -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        try {
            $cmd = $p.CommandLine
            if ($cmd -match "src\.api\.server|browser_data|C:\\ChatGPTAPI") {
                Log "Killing $name PID=$($p.ProcessId) (descendant of API)"
                Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            }
        } catch { Log "Kill $name PID=$($p.ProcessId) failed: $_" }
    }
}
Start-Sleep -Seconds 2

# 3. Confirm port is free.
$conn2 = Get-NetTCPConnection -LocalPort 3061 -State Listen -ErrorAction SilentlyContinue
if ($conn2) {
    Log "WARN: 3061 still held after manual kill: PID=$($conn2.OwningProcess)"
} else {
    Log "Port 3061 free"
}

# 4. Start scheduled task again.
Log "Step 2: Start-ScheduledTask ChatGPTAPI-Interactive"
try {
    Start-ScheduledTask -TaskName "ChatGPTAPI-Interactive" -ErrorAction Stop
} catch { Log "Start-ScheduledTask error: $_" }

Log "=== restart command issued, waiting for supervisor ==="

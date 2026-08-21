$ErrorActionPreference = "Continue"
$target = "C:\ChatGPTAPI"
Set-Location -LiteralPath $target

if (-not ("BrowserWindowControl" -as [type])) {
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class BrowserWindowControl {
    [DllImport("user32.dll")]
    public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll", SetLastError=true)]
    public static extern bool SetWindowPos(
        IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint flags
    );
}
"@
}

$python = Join-Path $target ".venv\Scripts\python.exe"
$envFile = Join-Path $target ".env"
$stateFile = Join-Path $target "data\browser_foreground_state.json"
$runtimeStateFile = Join-Path $target "data\browser_runtime_state.json"
$stateDir = Split-Path -Parent $stateFile
[IO.Directory]::CreateDirectory($stateDir) | Out-Null
$logDir = Join-Path $target "logs"
[IO.Directory]::CreateDirectory($logDir) | Out-Null

$envText = if (Test-Path -LiteralPath $envFile) {
    [IO.File]::ReadAllText($envFile)
} else { "" }
$keepForeground = $envText -match '(?im)^BROWSER_KEEP_FOREGROUND\s*=\s*true\s*$'
$adminToken = ""
$adminTokenMatch = [regex]::Match(
    $envText,
    '(?im)^\s*ADMIN_TOKEN\s*=\s*(?:"([^"]*)"|''([^'']*)''|([^\r\n#]*))'
)
if ($adminTokenMatch.Success) {
    $adminToken = @(
        $adminTokenMatch.Groups[1].Value,
        $adminTokenMatch.Groups[2].Value,
        $adminTokenMatch.Groups[3].Value
    ) | Where-Object { $_ } | Select-Object -First 1
    $adminToken = $adminToken.Trim()
}
$dashboardHeaders = @{}
if ($adminToken) { $dashboardHeaders["X-Admin-Token"] = $adminToken }
$env:NO_PROXY = "127.0.0.1,localhost"
$env:no_proxy = $env:NO_PROXY

$HWND_TOPMOST = [IntPtr](-1)
$SW_RESTORE = 9
$SWP_NOMOVE = 0x0002
$SWP_NOSIZE = 0x0001
$SWP_SHOWWINDOW = 0x0040
$windowFlags = $SWP_NOMOVE -bor $SWP_NOSIZE -bor $SWP_SHOWWINDOW

function Write-ForegroundState {
    param(
        [bool]$Running,
        [int]$ChromeProcesses,
        [int]$VisibleWindows,
        [bool]$TopmostEnforced,
        [int]$ServerProcessId
    )
    $state = [ordered]@{
        running = $Running
        enabled = $keepForeground
        chrome_processes = $ChromeProcesses
        visible_windows = $VisibleWindows
        topmost_enforced = $TopmostEnforced
        server_pid = $ServerProcessId
        last_enforced_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    [IO.File]::WriteAllText(
        $stateFile,
        ($state | ConvertTo-Json -Compress),
        [Text.UTF8Encoding]::new($false)
    )
}

# Kill any orphan python.exe/pythonw.exe still holding the API port.
# ``Stop-ScheduledTask`` does not reap detached workers, so without this
# the supervisor's new child fails with exit code 3 and loops forever.
function Free-ApiPort {
    param([int]$Port)
    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $connections) {
        $holderPid = $connection.OwningProcess
        if ($holderPid -eq $PID) { continue }
        $holder = Get-CimInstance Win32_Process -Filter "ProcessId=$holderPid" -ErrorAction SilentlyContinue
        if (-not $holder) { continue }
        if ($holder.Name -notin @("python.exe", "pythonw.exe")) { continue }
        Stop-Process -Id $holderPid -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
}

# This PowerShell process is a supervisor. It keeps the Python service alive,
# restores it if the browser is closed, and continuously enforces a visible,
# topmost ChatGPT window when BROWSER_KEEP_FOREGROUND=true.
while ($true) {
    $supervisorLog = Join-Path $logDir ("supervisor_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
    Add-Content -LiteralPath $supervisorLog -Encoding UTF8 -Value (
        "{0} | INFO     | supervisor | Starting Python API service" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    )
    # Ensure no orphan from a previous Scheduled-Task run still holds 3061.
    Free-ApiPort -Port 3061
    $server = Start-Process -FilePath $python `
        -ArgumentList @("-m", "src.api.server") `
        -WorkingDirectory $target `
        -WindowStyle Hidden `
        -PassThru

    $browserObserved = $false
    $missingSince = $null
    $mainPageUnreadySince = $null
    $lastRuntimeCheck = [datetime]::MinValue
    while (-not $server.HasExited) {
        $desiredRunning = $true
        if (Test-Path -LiteralPath $runtimeStateFile) {
            try {
                $runtimeControl = [IO.File]::ReadAllText($runtimeStateFile) | ConvertFrom-Json
                $desiredRunning = $runtimeControl.desired_running -ne $false
            } catch {
                # A sub-millisecond atomic replacement may be in progress.
                # Retain the safe default and retry on the next two-second loop.
            }
        }
        $browserProcesses = @(
            Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -like "*C:\ChatGPTAPI\browser_data*" }
        )
        $visibleWindows = 0
        $topmostEnforced = $false

        foreach ($browserProcess in $browserProcesses) {
            $process = Get-Process -Id $browserProcess.ProcessId -ErrorAction SilentlyContinue
            if (-not $process -or $process.MainWindowHandle -eq 0) { continue }
            $visibleWindows++
            if ($keepForeground -and $desiredRunning) {
                [BrowserWindowControl]::ShowWindowAsync(
                    $process.MainWindowHandle, $SW_RESTORE
                ) | Out-Null
                $positioned = [BrowserWindowControl]::SetWindowPos(
                    $process.MainWindowHandle, $HWND_TOPMOST,
                    0, 0, 0, 0, $windowFlags
                )
                [BrowserWindowControl]::SetForegroundWindow(
                    $process.MainWindowHandle
                ) | Out-Null
                $topmostEnforced = $topmostEnforced -or $positioned
            }
        }

        if ($desiredRunning) {
            if ($browserProcesses.Count -gt 0) {
                $browserObserved = $true
                $missingSince = $null
            } elseif ($browserObserved) {
                if ($null -eq $missingSince) { $missingSince = Get-Date }
                if (((Get-Date) - $missingSince).TotalSeconds -ge 10) {
                    # Closing the persistent Chrome window closes the Patchright
                    # context. Restart Python to rebuild the browser cleanly.
                    Add-Content -LiteralPath $supervisorLog -Encoding UTF8 -Value (
                        "{0} | WARNING  | supervisor | Browser process missing; restarting API PID {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $server.Id
                    )
                    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
                    break
                }
            }
        } else {
            $browserObserved = $false
            $missingSince = $null
            $mainPageUnreadySince = $null
        }

        # The main Patchright page can be closed while worker tabs keep Chrome
        # alive. Poll the internal runtime state and restart the full context if
        # that condition persists for 10 seconds.
        if ($desiredRunning -and ((Get-Date) - $lastRuntimeCheck).TotalSeconds -ge 10) {
            $lastRuntimeCheck = Get-Date
            try {
                $runtime = Invoke-RestMethod `
                    -Uri "http://127.0.0.1:3061/api/dashboard/state" `
                    -Headers $dashboardHeaders `
                    -TimeoutSec 8
                if ($runtime.runtime.status -eq "starting") {
                    # A dashboard-triggered start may need time to restore the
                    # logged-in main page and all worker tabs.
                    $mainPageUnreadySince = $null
                } elseif ($runtime.browser.ready -eq $true) {
                    $mainPageUnreadySince = $null
                } else {
                    if ($null -eq $mainPageUnreadySince) {
                        $mainPageUnreadySince = Get-Date
                    }
                    if (((Get-Date) - $mainPageUnreadySince).TotalSeconds -ge 10) {
                        Add-Content -LiteralPath $supervisorLog -Encoding UTF8 -Value (
                            "{0} | WARNING  | supervisor | Browser page unready; restarting API PID {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $server.Id
                        )
                        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
                        break
                    }
                }
            } catch {
                # During startup the HTTP listener is intentionally absent.
                # Process/Chrome monitoring remains active while it initializes.
            }
        }

        Write-ForegroundState `
            -Running $true `
            -ChromeProcesses $browserProcesses.Count `
            -VisibleWindows $visibleWindows `
            -TopmostEnforced $topmostEnforced `
            -ServerProcessId $server.Id
        Start-Sleep -Seconds 2
        $server.Refresh()
    }

    Write-ForegroundState `
        -Running $false `
        -ChromeProcesses 0 `
        -VisibleWindows 0 `
        -TopmostEnforced $false `
        -ServerProcessId 0
    Add-Content -LiteralPath $supervisorLog -Encoding UTF8 -Value (
        "{0} | WARNING  | supervisor | Python API exited with code {1}; restart in 5 seconds" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $server.ExitCode
    )
    Start-Sleep -Seconds 5
}

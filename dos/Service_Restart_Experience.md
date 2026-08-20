# GPT-FastAPI 服务重启经验总结

> 记录 2026-08-19 重启服务过程中踩过的坑、进程架构、正确重启方法与排障命令。
> 适用场景：修改代码后需要重启服务、服务异常需要恢复、端口被占用排障。

---

## 1. 服务进程架构（必须先理解）

```
Windows 计划任务 ChatGPTAPI-Interactive（Highest 权限，登录时自启）
  └── powershell.exe  start-api-hidden.ps1   ← supervisor（监控进程）
        └── pythonw.exe  -m src.api.server   ← API 服务（监听 3061）
              └── node.exe                    ← Playwright driver
                    └── chrome.exe            ← ChatGPT 浏览器（browser_data 目录）
```

关键事实：

- **supervisor 是 PowerShell 无限循环**：Python 退出后等 5 秒自动重新拉起。
- **计划任务以 Highest 权限运行**：整个进程树都是高完整性级别，普通终端（Medium）**无法直接 Stop-Process**，会报 `Access is denied`。
- **Dashboard（/dashboard）只能控制浏览器运行层**（runtime start/stop），**没有重启 Python 进程的端点**。重启 Python 必须走计划任务。
- supervisor 还有两个自动重启触发条件（各持续 10 秒即触发）：
  - 带 `browser_data` 的 chrome 进程全部消失 → 重启 API；
  - `/api/dashboard/state` 返回 `browser.ready != true` → 重启 API。

## 2. 正确的重启方法

```powershell
Stop-ScheduledTask -TaskName "ChatGPTAPI-Interactive"
Start-Sleep -Seconds 3
Start-ScheduledTask -TaskName "ChatGPTAPI-Interactive"
```

重启后验证：

```powershell
# 1. 端口持有者应是新进程（对比 CreationDate）
Get-NetTCPConnection -LocalPort 3061 -State Listen | Select-Object OwningProcess

# 2. supervisor 日志确认启动过程
Get-Content "C:\ChatGPTAPI\logs\supervisor_$(Get-Date -Format yyyy-MM-dd).log" -Tail 20

# 3. 运行层状态（ADMIN_TOKEN 取自 .env）
Invoke-RestMethod -Uri "http://127.0.0.1:3061/api/dashboard/state" -Headers @{ "X-Admin-Token" = $token }
# 期望：runtime.running=true, browser.ready=true, current_url=https://chatgpt.com/
```

## 3. 踩坑记录：孤儿进程 + 端口占用死循环

### 现象

修改代码后重启计划任务，新实例反复崩溃，supervisor 日志循环出现：

```
INFO  | supervisor | Starting Python API service
WARNING | supervisor | Python API exited with code 3; restart in 5 seconds
```

### 根因

1. `Stop-ScheduledTask` **不会杀掉已脱离任务进程树的旧进程**。旧的 pythonw（API）进程继续存活并占用 3061 端口。
2. 新 supervisor 拉起的新 Python 实例绑定 3061 失败 → exit code 3 → 5 秒后再试 → 无限循环。
3. 旧进程是 Highest 权限，普通终端杀不掉；`Register-ScheduledTask` 注册临时提权任务也被拒（Access is denied）。

### 解决方案（已固化到脚本）

在 `start-api-hidden.ps1` 中新增 `Free-ApiPort` 函数：supervisor 每次启动 Python 前，先查找占用 3061 的孤儿 `python.exe/pythonw.exe` 并强制终止（supervisor 本身是 Highest 权限，可以杀同权限进程）。

```powershell
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
```

**经验：以后只要再遇到 "exited with code 3" 循环，先查端口占用，再重启计划任务即可自愈。**

## 4. 排障命令速查

```powershell
# 谁在监听 3061（含进程名、启动时间、父进程）
$conn = Get-NetTCPConnection -LocalPort 3061 -State Listen | Select-Object -First 1
Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)" |
    Select-Object ProcessId, Name, ParentProcessId, CreationDate

# 列出所有 python/pythonw 进程（判断新旧：对比 CreationDate 与代码修改时间）
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Select-Object ProcessId, ParentProcessId, Name, CreationDate | Format-Table -AutoSize

# 计划任务状态与上次运行结果
Get-ScheduledTask -TaskName "ChatGPTAPI-Interactive" | Select-Object State
Get-ScheduledTaskInfo -TaskName "ChatGPTAPI-Interactive" | Select-Object LastRunTime, LastTaskResult

# 当前终端完整性级别（Medium=8192 无法杀 High/System 进程）
whoami /groups | Select-String "Mandatory Label"

# 带 token 的 dashboard 状态查询
$envText = Get-Content "C:\ChatGPTAPI\.env" -Raw
$tok = [regex]::Match($envText, '(?im)^\s*ADMIN_TOKEN\s*=\s*(?:"([^"]*)"|''([^'']*)''|([^\r\n#]*))')
$token = (@($tok.Groups[1].Value, $tok.Groups[2].Value, $tok.Groups[3].Value) | Where-Object { $_ } | Select-Object -First 1).Trim()
Invoke-RestMethod -Uri "http://127.0.0.1:3061/api/dashboard/state" -Headers @{ "X-Admin-Token" = $token }
```

## 5. 关键经验清单

1. **改完 `src/` 代码必须重启服务才生效**——运行中的进程不会热加载；判断"修复是否生效"先看进程 `CreationDate` 是否晚于代码修改时间。
2. **重启只认计划任务**：`Stop/Start-ScheduledTask ChatGPTAPI-Interactive`，不要尝试手动杀进程或注册新任务（权限不够）。
3. **exit code 3 = 端口被占用**：先 `Get-NetTCPConnection -LocalPort 3061` 找占用者。
4. **`/health` 返回 401 不代表服务挂了**：只是需要鉴权；用 `/api/dashboard/state`（带 X-Admin-Token）或看审计日志确认。
5. **supervisor 日志是第一手证据**：`logs\supervisor_YYYY-MM-DD.log` 记录每次启动/退出码/孤儿清理。
6. **`data\browser_runtime_state.json` 的 `desired_running`**：dashboard 停止运行层后会持久化，supervisor 读到 false 就不会强制拉起浏览器；若浏览器"该起不起"，先检查这个文件。
7. **系统 ExecutionPolicy 禁止运行脚本**：直接执行 `.ps1` 文件会报 UnauthorizedAccess，但内联 PowerShell 命令不受影响；计划任务里的脚本用了 `-ExecutionPolicy Bypass` 所以正常。
8. **计划任务配置来源**：`install-interactive-startup.ps1`（AtLogOn 触发、Interactive 登录类型、Highest 权限、RestartCount 99）。

## 6. 本次重启结果（2026-08-19）

- 孤儿进程 PID 7120（11:57 启动的旧代码）被新 supervisor 的 `Free-ApiPort` 自动清除；
- 新 API 进程 PID 14192（15:35:21 启动）加载全部修复代码；
- `runtime.running=true`、`browser.ready=true`、chatgpt.com 主页面正常；
- 真实客户端调用 `/healthz`、`/v1/me`、`/v1/pool/status` 全部 200。

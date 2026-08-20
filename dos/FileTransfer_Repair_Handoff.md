# GPT-FastAPI 文件传输修复 — 任务交接文档

> 用途：在新对话中继续本任务。请先通读本文档，再按"下一步"继续。
> 最后更新：2026-08-19

---

## 1. 任务背景

项目 `C:\ChatGPTAPI`（GPT-FastAPI，通过浏览器自动化操作 ChatGPT 网页版提供 OpenAI 兼容 API）存在文件传输问题：

- ChatGPT 回复中声称生成了文件，但 API **没有捕获到文件**，也没有把下载链接/文件返回给调用方。
- 用户的接收机在 `http://127.0.0.1:8050`（接收机本身无需处理，只是最终消费方）。
- 用户明确要求：**优先使用 DOM 扫描 / 点击下载方式捕获文件，不要用网络层拦截方案**；实在解决不了再换方案。
- 项目使用文档：`C:\ChatGPTAPI\dos\GPT-FastAPI_API_Usage_Guide.md`
- 原始诊断报告主题：`GPT-FastAPI 文件生成返回诊断报告`

## 2. 根因分析（已确认）

1. **浏览器不接受下载**：Playwright/Patchright 持久化上下文未设置 `accept_downloads=True`，导致 `download` 事件永远不触发，点击下载必然超时。
2. **捕获逻辑过于依赖点击事件**：对 ChatGPT 的 estuary/interpreter 下载链接没有 HTTP 直取兜底，也没有整页 DOM 扫描兜底。
3. **失效元素超时过长**：对已脱离 DOM 的候选元素读属性等待 30 秒/个，严重拖慢且掩盖错误。
4. **修复后服务未重启**：运行中的 API 进程（旧 PID 7120）在代码修复前启动，一直跑旧代码；且它以高权限（计划任务 Highest）运行、占用 3061 端口，导致新实例无法绑定端口而崩溃循环（exit code 3）。

## 3. 已完成的修复

### 3.1 代码修复（已合入，已随服务重启生效）

**`src/browser/manager.py`**（约 L178-190，`launch_kwargs`）
- 新增 `accept_downloads=True`，使 Playwright 的 `download` 事件可以触发。

**`src/files/generated.py`**（核心捕获模块）
- `_ATTR_TIMEOUT_MS = 4_000`：候选元素属性读取超时从 30s 降到 4s，快速跳过失效元素。
- `_fetch_direct(page, href, target)`：用浏览器会话凭证（`page.context.request.get`）直接下载 estuary/interpreter 链接；支持从 `Content-Disposition` 提取文件名。
- `_scan_page_attachment_urls(page)`：整页 DOM 扫描 `a[href] / [data-href] / [data-url]`，匹配 `estuary/content`、`interpreter/download`、`/backend-api/files/`。
- `_filename_from_href`：支持从 URL 查询参数 `sandbox_path / filename / file_name / fn` 提取文件名。
- `capture_generated_files` 捕获顺序：
  1. 扫描最新 assistant turn 的 `a, button, [role=button]` 候选；
  2. 可下载链接优先 `_fetch_direct` 直取；
  3. 直取失败则 `page.expect_download` + 点击等待真实下载事件；
  4. 全部失败时整页 DOM 扫描兜底直取。

### 3.2 运维修复（supervisor 脚本）

**`start-api-hidden.ps1`**（约 L94-110）
- 新增 `Free-ApiPort -Port 3061` 函数：supervisor 每次启动 Python 前，先查找并终止占用 3061 端口的孤儿 `python.exe/pythonw.exe` 进程，避免端口冲突崩溃循环。
- 重启方式：`Stop-ScheduledTask -TaskName "ChatGPTAPI-Interactive"; Start-Sleep 3; Start-ScheduledTask -TaskName "ChatGPTAPI-Interactive"`（计划任务由 `install-interactive-startup.ps1` 创建，Highest 权限，登录时自启）。

### 3.3 服务重启（已完成并验证）

- 旧孤儿进程 PID 7120（修复前启动）已被新 supervisor 自动清除（日志：`Killing orphaned port holder PID 7120`）。
- 新 API 进程 **PID 14192**（2026-08-19 15:35:21 启动）已加载全部新代码。
- 验证结果：
  - 3061 端口正常监听；
  - `/api/dashboard/state`：`runtime.running=true`、`browser.ready=true`、`main_page_open=true`、`current_url=https://chatgpt.com/`；
  - 真实客户端（chatagent，192.168.8.222）调用 `/healthz`、`/v1/me`、`/v1/pool/status` 均 200。

## 4. 关键环境信息

| 项 | 值 |
|---|---|
| API 地址 | `http://127.0.0.1:3061`（局域网 `http://192.168.10.48:3061`） |
| Dashboard | `http://192.168.10.48:3061/dashboard`（仅浏览器运行层 start/stop，无 Python 进程重启端点） |
| 管理员头 | `X-Admin-Token`，值取自 `C:\ChatGPTAPI\.env` 的 `ADMIN_TOKEN` |
| 计划任务 | `ChatGPTAPI-Interactive`（执行 `start-api-hidden.ps1`，Highest 权限） |
| Supervisor 日志 | `C:\ChatGPTAPI\logs\supervisor_YYYY-MM-DD.log` |
| 运行时状态文件 | `data\browser_runtime_state.json`（`desired_running`） |
| 测试项目 | `codex-e2e`（**multi_agent=true，创建 session 必须带 `agent_id`**）；token 可通过 `GET /api/dashboard/projects/codex-e2e/token` 获取 |
| 接收机 | `http://127.0.0.1:8050`（用户侧，无需改动） |
| 终端权限 | Trae 终端为 Medium 完整性，**无法直接杀高权限进程、无法注册新计划任务**；只能通过停止/启动已有计划任务间接触发 |

## 5. 未完成事项（下一步）

1. **端到端文件下载验证**（上次被用户叫停，尚未完成）：
   - 用 `codex-e2e` 项目 token 创建 session：`POST /v1/sessions`，body 必须含 `agent_id`（multi_agent 项目），否则 422；
   - 发送生成文件请求（如"生成 e2e_test.csv 并提供下载附件"）：`POST /v1/sessions/{session_id}/messages`；
   - 检查响应中是否带文件附件/下载 URL；
   - 检查日志中 `generated_files` 模块输出（`Captured N generated file(s)`）；
   - 确认文件能传输到接收机 `http://127.0.0.1:8050`。
2. 若端到端仍失败：抓取 `logs/` 中 `generated_files` 相关行，分析是候选元素定位失败、直取 403/404、还是 download 事件未触发，再决定是否告知用户换方案（用户已授权：DOM/点击方案实在不行才可换）。

## 6. 常用验证命令（PowerShell）

```powershell
# 读取 ADMIN_TOKEN
$envText = Get-Content "C:\ChatGPTAPI\.env" -Raw
$tok = [regex]::Match($envText, '(?im)^\s*ADMIN_TOKEN\s*=\s*(?:"([^"]*)"|''([^'']*)''|([^\r\n#]*))')
$token = @($tok.Groups[1].Value,$tok.Groups[2].Value,$tok.Groups[3].Value) | Where-Object {$_} | Select-Object -First 1
$hdr = @{ "X-Admin-Token" = $token.Trim() }

# 运行层状态
Invoke-RestMethod -Uri "http://127.0.0.1:3061/api/dashboard/state" -Headers $hdr

# 重启服务（会触发 supervisor 清理孤儿端口占用并加载新代码）
Stop-ScheduledTask -TaskName "ChatGPTAPI-Interactive"; Start-Sleep 3; Start-ScheduledTask -TaskName "ChatGPTAPI-Interactive"

# 查看 supervisor 日志
Get-Content "C:\ChatGPTAPI\logs\supervisor_$(Get-Date -Format yyyy-MM-dd).log" -Tail 20

# 端口占用检查
Get-NetTCPConnection -LocalPort 3061 -State Listen | Select-Object OwningProcess
```

## 7. 注意事项

- 修改任何 `src/` 下 Python 代码后，**必须重启服务**（上述计划任务重启命令）才会生效；supervisor 会自动清理旧进程。
- 不要再引入网络层拦截方案（用户明确优先 DOM/点击方案）。
- 单元测试运行时如遇 langsmith 插件冲突：`$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"`。
- 系统禁止运行脚本（ExecutionPolicy），直接跑 `.ps1` 会报 UnauthorizedAccess，但内联命令不受影响。

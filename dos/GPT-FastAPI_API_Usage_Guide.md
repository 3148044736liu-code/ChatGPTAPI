# GPT-FastAPI 服务器接口使用说明

> 文件版本：1.4.4  
> 更新日期：2026-08-18  
> 适用服务器：`192.168.10.48:3061`  
> 当前提供方：ChatGPT 网页版（浏览器自动化，不调用 OpenAI 官方 API）

## 1. 服务概览

GPT-FastAPI 使用 FastAPI 对外提供 HTTP API，通过 Patchright 控制服务器上已登录 ChatGPT 的真实 Chromium 页面完成对话、图片生成和文件处理。

当前运行结构：

```text
调用任务机
  ↓ HTTP + Bearer Project Token
FastAPI :3061
  ↓ project_id / session_id 隔离
SQLite 会话映射
  ↓ provider_thread_id
1 个浏览器 Worker 页面
  ↓
ChatGPT 网页版
```

关键特性：

- 每个接入项目使用一个独立 Project Token。
- Project Token 映射到独立的 `project_id`，隔离会话、消息和文件。
- 一个项目可以创建多个 `session_id`，每个 session 对应一个 ChatGPT 网页对话。
- 所有项目和 session 共用 1 个浏览器 Worker，全局严格串行。
- 前一个浏览器任务结束后至少间隔 30 秒，下一个任务才会开始，以避免高频访问 ChatGPT 网页。
- Token 使用 Windows DPAPI 加密保存，可在可信内网 Dashboard 中反复复制。
- 浏览器运行层可以从 Dashboard 启动或停止；停止后 Dashboard 和数据接口继续可用。

## 2. 服务地址

| 项目 | 地址 |
|---|---|
| 服务根地址 | `http://192.168.10.48:3061` |
| OpenAI Base URL | `http://192.168.10.48:3061/v1` |
| 运维仪表盘 | `http://192.168.10.48:3061/dashboard` |
| 开发说明书下载 | `http://192.168.10.48:3061/api/dashboard/developer-guide` |
| Swagger 文档 | `http://192.168.10.48:3061/docs` |
| ReDoc 文档 | `http://192.168.10.48:3061/redoc` |
| OpenAPI JSON | `http://192.168.10.48:3061/openapi.json` |
| 健康检查 | `http://192.168.10.48:3061/healthz` |

局域网测试：

```powershell
Test-NetConnection 192.168.10.48 -Port 3061
Invoke-RestMethod http://192.168.10.48:3061/healthz
```

预期健康检查返回：

```json
{"status":"ok"}
```

## 3. 认证方式

### 3.1 获取 Project Token

浏览器打开：

```text
http://192.168.10.48:3061/dashboard
```

在“项目授权 Token 生成台”中填写：

- 项目名称
- 项目 ID（英文、数字、点、下划线或短横线）
- 用途说明
- 是否为多智能体项目

创建后得到格式类似以下内容的 Token：

```text
cgpt_<generated-random-value>
```

Token 可在 Dashboard 中反复复制、停用、启用、轮换或删除：

- 停用：Token 暂时不能调用，之后可以重新启用。
- 轮换：生成新 Token，旧 Token 立即失效。
- 删除：立即吊销并隐藏项目授权；历史会话和文件元数据保留，原 project_id 不可重新占用。

### 3.2 请求头

业务接口必须携带：

```http
Authorization: Bearer <PROJECT_TOKEN>
```

建议同时携带任务机标识，便于 Dashboard 和日志排查：

```http
X-Task-Machine: worker-01
X-Task-Name: daily-report
```

PowerShell 通用请求头：

```powershell
$token = "<PROJECT_TOKEN>"
$headers = @{
    Authorization    = "Bearer $token"
    "X-Task-Machine" = "worker-01"
    "X-Task-Name"    = "daily-report"
}
```

不要把真实 Token 写入代码仓库、日志、截图或公开文档。

## 4. 推荐调用流程

### 4.1 查看当前项目身份

```powershell
Invoke-RestMethod `
    -Uri "http://192.168.10.48:3061/v1/me" `
    -Headers $headers
```

该接口返回当前 Token 对应的 `project_id`、项目名称、多智能体模式、并发上限和文件配额。

### 4.2 创建一个内部会话

```powershell
$body = @{
    title = "2026-08-14 日报分析"
} | ConvertTo-Json

$session = Invoke-RestMethod `
    -Method Post `
    -Uri "http://192.168.10.48:3061/v1/sessions" `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $body

$sessionId = $session.id
```

创建 session 时尚未立即创建 ChatGPT 网页对话。第一次发送消息时，系统才会新建网页对话，并把真实对话 ID 保存到 `provider_thread_id`。

### 4.3 向会话发送消息

```powershell
$body = @{
    content  = "请生成今天的项目日报摘要"
    file_ids = @()
} | ConvertTo-Json

$result = Invoke-RestMethod `
    -Method Post `
    -Uri "http://192.168.10.48:3061/v1/sessions/$sessionId/messages" `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $body
```

返回中的重要字段：

| 字段 | 说明 |
|---|---|
| `session_id` | 本项目内部会话 ID，需要由任务机持久化保存 |
| `provider_thread_id` | ChatGPT 网页真实对话 ID |
| `message` | 模型回复文本 |
| `response_time_ms` | 本次响应耗时 |
| `attachments` | 检测到的生成文件和下载地址 |
| `pool` | 单浏览器 Worker 队列状态 |

### 4.4 第二天继续旧对话或创建新对话

继续同一对话：继续向原来的 `session_id` 发送消息。系统会从 SQLite 读取 `provider_thread_id`，让唯一的 Worker 页面跳转到对应 ChatGPT 对话。

创建独立的新对话：再次调用 `POST /v1/sessions`，保存新的 `session_id`。

建议调用方保存业务键与 session 的映射：

```json
{
  "daily-report:2026-08-13": "ses_old",
  "daily-report:2026-08-14": "ses_new"
}
```

## 5. 会话接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/v1/sessions` | 创建内部会话 |
| `GET` | `/v1/sessions?limit=50` | 按最近活跃时间倒序列出当前项目会话 |
| `GET` | `/v1/sessions/{session_id}` | 查询单个会话详情 |
| `DELETE` | `/v1/sessions/{session_id}` | 删除内部会话及本地消息；不会删除 ChatGPT 网页对话 |
| `GET` | `/v1/sessions/{session_id}/messages` | 查看本地保存的会话消息 |
| `POST` | `/v1/sessions/{session_id}/messages` | 通过单 Worker 串行队列向指定会话发送消息 |

查看最近会话：

```powershell
Invoke-RestMethod `
    -Uri "http://192.168.10.48:3061/v1/sessions?limit=50" `
    -Headers $headers
```

当前 `limit` 范围为 1～200。结果按 `COALESCE(last_active_at, created_at)` 倒序排列，只返回当前 Project Token 所属会话。

查看某个会话的本地消息：

```powershell
Invoke-RestMethod `
    -Uri "http://192.168.10.48:3061/v1/sessions/$sessionId/messages" `
    -Headers $headers
```

## 6. 多智能体项目

多智能体项目创建 session 时必须提供 `agent_id`：

```powershell
$body = @{
    title    = "审核智能体会话"
    agent_id = "reviewer"
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri "http://192.168.10.48:3061/v1/sessions" `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $body
```

推荐关系：

```text
一个 Project Token
├── agent_id: planner  → 独立 session_id → 独立 ChatGPT 对话
├── agent_id: writer   → 独立 session_id → 独立 ChatGPT 对话
└── agent_id: reviewer → 独立 session_id → 独立 ChatGPT 对话
```

同一智能体应持续复用自己的 `session_id`。不要让多个智能体共用一个 session，否则它们将共享相同网页上下文并被串行处理。

## 7. OpenAI 兼容接口

### 7.1 接口列表

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/v1/models` | 返回兼容模型列表 |
| `POST` | `/v1/chat/completions` | Chat Completions 兼容接口 |
| `POST` | `/v1/responses` | Responses API 兼容接口 |
| `POST` | `/v1/images/generations` | 图片生成兼容接口 |

Project Token 调用上述浏览器类 POST 接口时必须在 JSON 中携带已经创建的 `session_id`。

### 7.2 Chat Completions 示例

```powershell
$body = @{
    model      = "catgpt-browser"
    session_id = $sessionId
    messages   = @(
        @{ role = "system"; content = "你是一名项目分析助手" }
        @{ role = "user"; content = "总结今天的进度" }
    )
    stream = $false
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
    -Method Post `
    -Uri "http://192.168.10.48:3061/v1/chat/completions" `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $body
```

Python OpenAI SDK 示例：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://192.168.10.48:3061/v1",
    api_key="<PROJECT_TOKEN>",
)

response = client.chat.completions.create(
    model="catgpt-browser",
    messages=[{"role": "user", "content": "总结今天的进度"}],
    extra_body={"session_id": "ses_xxx"},
)

print(response.choices[0].message.content)
```

`temperature`、`max_tokens` 等字段可以接收，但网页端不保证与官方 API 完全相同的采样语义。

### 7.3 Responses API 示例

```powershell
$body = @{
    model        = "catgpt-browser"
    session_id   = $sessionId
    instructions = "回答要简洁"
    input         = "给出三条项目风险"
    stream        = $false
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
    -Method Post `
    -Uri "http://192.168.10.48:3061/v1/responses" `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $body
```

### 7.4 图片生成示例

```powershell
$body = @{
    model           = "dall-e-3"
    session_id      = $sessionId
    prompt          = "一张蓝色科技风的数据中心插画"
    n               = 1
    response_format = "b64_json"
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri "http://192.168.10.48:3061/v1/images/generations" `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $body
```

图片生成实际通过 ChatGPT 网页完成，参数兼容性不等同于 OpenAI 官方 DALL·E API。

## 8. 文件接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/v1/files` | 上传文件；Project Token 必须绑定 `session_id` |
| `GET` | `/v1/files` | 查看当前项目文件列表 |
| `GET` | `/v1/files/{file_id}` | 查看文件元数据 |
| `DELETE` | `/v1/files/{file_id}` | 删除文件记录及内容 |
| `GET` | `/v1/files/{file_id}/content` | 使用签名参数下载文件内容 |

当前服务器配置：

- 单文件最大：25 MB
- 单 session 最多：20 个文件
- 单项目文件总配额：500 MB
- 文件默认保留：72 小时
- 清理任务每 10 分钟执行一次

上传示例：

```powershell
curl.exe -X POST "http://192.168.10.48:3061/v1/files" `
    -H "Authorization: Bearer $token" `
    -H "X-Task-Machine: worker-01" `
    -H "X-Task-Name: daily-report" `
    -F "file=@C:\Data\report.pdf" `
    -F "session_id=$sessionId"
```

发送消息时引用文件：

```json
{
  "content": "分析这个文件",
  "file_ids": ["file_xxx"]
}
```

Project Token 只能引用属于同一项目、同一 session 的文件。上传响应和生成文件响应中的 `download_url` 带有临时签名，应直接使用返回的地址下载。

## 9. 单 Worker 串行队列

查看池状态：

```http
GET /v1/pool/status
Authorization: Bearer <PROJECT_TOKEN>
```

当前容量为 1：

```dotenv
MAX_CONCURRENT_SESSIONS=1
SESSION_REQUEST_GAP_MS=30000
```

```json
{
  "capacity": 1,
  "available": 1,
  "busy": 0
}
```

唯一的 Worker 对应一个浏览器页面，但不固定绑定项目或 session。处理请求时，系统临时借出该页面，根据本地保存的 `provider_thread_id` 跳转到目标 ChatGPT 对话；完成后归还页面。

同一个 `session_id` 在 HTTP 边界和 Worker 层都会串行化。不同项目、不同 session 的浏览器任务也进入同一条全局队列，不会并发操作网页；相邻任务之间至少等待 30 秒。

队列执行顺序如下：

```text
请求 A 开始操作浏览器
  → 切换对话、发送问题、等待完整回复
  → 捕获生成文件、写入数据库、发送 HTTP 响应
请求 A 完整结束
  → 至少等待 30 秒
请求 B 才开始操作浏览器
```

以下浏览器型接口进入同一条全局队列：

- `POST /v1/sessions/{session_id}/messages`
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/images/generations`
- 旧版 `/chat`、`/thread/*`、`/threads` 和 `/status`

创建、列举和查询 session，以及查询本地消息、Token、日志和池状态等纯数据库/状态接口不占用 Worker。排队状态保存在服务进程内存中，不是持久化作业队列；服务重启时，正在执行或等待的 HTTP 请求需要由调用方按业务幂等规则重试。

### 9.1 新任务与旧任务如何确定网页对话

`POST /v1/sessions` 只创建本地 `session_id`，初始 `provider_thread_id` 为空。第一次向该 session 发消息时，Worker 新建 ChatGPT 网页对话，发送完成后从 `/c/{thread_id}` 地址提取真实 thread id，并写入 SQLite：

```text
Project Token → project_id/owner_id
业务任务      → session_id
session_id    → provider_thread_id
provider_thread_id → https://chatgpt.com/c/{provider_thread_id}
```

旧任务继续追问时，调用方必须继续使用相同 Project Token 和原 `session_id`。服务从 SQLite 读取 `provider_thread_id`；如果 Worker 当前不在该对话，就先跳转到对应 `/c/{provider_thread_id}`，再发送新消息。跨天或服务重启后仍使用同一映射。

调用方应持久化以下关系，不要仅依赖“最近一个会话”猜测：

```text
project_id + business_task_id + agent_id → session_id
```

如果客户机丢失 `session_id`，可使用 `GET /v1/sessions?limit=50` 按最近活跃时间查找，再通过 `GET /v1/sessions/{session_id}/messages` 核对本地消息。删除 session 会删除本地映射和消息，但不会删除 ChatGPT 网页对话；删除后正常项目接口将无法再根据该 session 恢复原对话。

## 10. `/threads` 旧版最近对话接口

```http
GET /threads
```

该接口直接扫描 ChatGPT 主页面左侧栏中已经加载的链接：

```css
nav a[href^='/c/']
a[href^='/c/']
```

并从 `/c/{thread_id}` 中提取 ID，返回网页标题和地址。它具有以下限制：

- 只返回当前网页侧栏已经加载到 DOM 的近期对话。
- 不会滚动加载更早记录。
- 不查询本地 session 数据库。
- 不按项目隔离，会看到同一 ChatGPT 账号的混合对话。
- Project Token 被禁止调用该接口，会返回 HTTP 403。

项目系统必须使用 `GET /v1/sessions` 查看自己的最近会话，不应使用 `/threads`。

其他旧版共享页面接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/chat` | 在主页面当前对话发送消息 |
| `POST` | `/thread/new` | 在主页面新建对话并发送消息 |
| `POST` | `/thread/{thread_id}/chat` | 跳转指定网页对话并发送消息 |
| `GET` | `/threads` | 扫描网页侧栏近期对话 |
| `GET` | `/status` | 查看主页面登录和当前 thread |

这些接口仅用于旧版 `.env` Token 兼容，不适用于 Dashboard 生成的 Project Token。

## 11. Dashboard 与运维接口

Dashboard 无需输入 API Token，但管理操作仅允许服务器本机或可信私网访问，并检查同源请求。

### 11.1 状态与日志

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/dashboard/state` | 服务、浏览器、池、项目、存储和任务机状态 |
| `GET` | `/api/dashboard/logs` | 日志文件列表或指定日志末尾内容 |
| `GET` | `/api/dashboard/developer-guide` | 下载当前 Markdown 开发说明书，无需 Token |

日志目录：

```text
C:\ChatGPTAPI\logs
```

日志按天划分，默认保留 30 天。响应头 `X-Request-ID` 可用于关联任务机报错和服务器日志。

### 11.2 Project Token 管理

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/dashboard/projects` | 项目列表，不返回 Token 明文 |
| `POST` | `/api/dashboard/projects` | 创建项目及 Token |
| `GET` | `/api/dashboard/projects/{project_id}/token` | 从 DPAPI 加密保险箱读取 Token |
| `POST` | `/api/dashboard/projects/{project_id}/rotate` | 轮换 Token，旧 Token 立即失效 |
| `PATCH` | `/api/dashboard/projects/{project_id}` | 启用或停用 Token |
| `DELETE` | `/api/dashboard/projects/{project_id}` | 删除并吊销 Token |

### 11.3 启动或停止浏览器运行层

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/dashboard/runtime` | 查看人工期望状态和实际运行状态 |
| `POST` | `/api/dashboard/runtime/start` | 启动浏览器、恢复登录并创建 Worker 页面 |
| `POST` | `/api/dashboard/runtime/stop` | 停止浏览器业务并关闭全部项目页面 |

停止后的行为：

- Dashboard、日志、Token 管理、会话查询和文件元数据接口继续运行。
- 浏览器类业务请求返回 HTTP 503，错误类型为 `runtime_unavailable`。
- 主浏览器和 Worker 页面全部关闭。
- 人工停止状态持久化，Windows 守护脚本不会误判为故障并自动拉起。

## 12. 常见状态码

| 状态码 | 含义 | 排查方向 |
|---:|---|---|
| `200/201` | 请求成功 | 正常处理 |
| `400` | 请求参数缺失，例如 Project Token 未传 `session_id` | 检查 JSON 和 session_id |
| `401` | Token 缺失、错误、已轮换或已删除 | 重新复制有效 Token |
| `403` | Project Token 调用了共享浏览器接口，或管理请求不在可信私网/不同源 | 改用 `/v1/sessions` 或检查来源 |
| `404` | session、file 或 project 不存在，或不属于当前 Token | 检查资源 ID 与 Token |
| `409` | 资源冲突，例如文件属于其他 session、旧项目无加密 Token 副本 | 使用正确 session 或轮换 Token |
| `410` | 文件内容已经过期清理 | 重新上传文件 |
| `413` | 文件大小、数量或项目总配额超限 | 减少文件或清理存储 |
| `422` | 字段格式错误；多智能体项目缺少 agent_id | 按接口模型修正请求 |
| `500` | ChatGPT 网页、选择器或浏览器执行异常 | 使用 X-Request-ID 查询错误日志 |
| `503` | 浏览器运行层已停止、启动中或 Worker 尚未初始化 | 在 Dashboard 启动项目并等待运行中 |

## 13. 使用注意事项

1. 任务机必须持久化保存 `session_id`，不要依靠 ChatGPT 网页标题查找对话。
2. 一个项目可以有多个 session；继续上下文就复用旧 session，独立任务就创建新 session。
3. 所有项目目前共享同一个 ChatGPT 登录账号；项目隔离由 FastAPI、Token 和 SQLite 实现，不是独立 ChatGPT 账号隔离。
4. 如果有人在 ChatGPT 网页手动删除对话，本地 `provider_thread_id` 仍可能存在，但继续对话会失败。此时应新建 session，并把旧会话摘要作为新上下文。
5. 不要手动关闭前台 ChatGPT 窗口或 Worker 标签页。服务带有 Worker 页面健康检查，但人工维护应使用 Dashboard 的启动/停止按钮。
6. 不要把 3061 端口直接暴露到公网；Dashboard 管理接口设计用于可信局域网。
7. 对调用结果设置合理超时。网页自动化响应时间通常高于官方 API。

## 14. 最小接入清单

- 在 Dashboard 创建项目并保存 Project Token。
- 调用 `GET /v1/me` 验证 Token 对应项目。
- 调用 `POST /v1/sessions` 创建 session。
- 持久化保存返回的 `session_id`。
- 使用 `/v1/sessions/{session_id}/messages` 或 OpenAI 兼容接口发送消息。
- 每次请求携带 `Authorization`、`X-Task-Machine`、`X-Task-Name`。
- 使用 `GET /v1/sessions` 找回最近会话。
- 使用 Dashboard 和按日日志排查 401、403、500、503。

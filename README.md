<h1 align="center">GPT-FastAPI</h1>

<p align="center">
  基于真实浏览器会话，将 ChatGPT 或 Claude 网页能力封装为 OpenAI 兼容 API。
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/API-OpenAI%20Compatible-22A06B?style=flat-square" alt="OpenAI Compatible" />
  <img src="https://img.shields.io/github/license/3148044736liu-code/GPT-FastAPI?style=flat-square" alt="License" />
</p>

## 项目简介

GPT-FastAPI 使用 Patchright 驱动真实浏览器，通过已经登录的 ChatGPT 或 Claude 网页会话发送消息、读取回复，并通过 FastAPI 暴露标准 HTTP 接口。

项目适合以下场景：

- 使用 OpenAI Python SDK、LangChain 或其他兼容客户端接入网页模型。
- 在同一个服务中管理多个独立会话。
- 上传附件、保存模型生成文件并提供受控下载。
- 使用不同 Bearer Token 隔离用户、会话和文件。
- 在局域网内为其他应用提供统一的模型调用入口。

> 本项目不需要 ChatGPT 或 Claude 的官方 API Key，但需要可正常访问对应网页的账号和浏览器登录状态。

## 主要能力

| 能力 | ChatGPT | Claude |
|---|:---:|:---:|
| OpenAI Chat Completions 兼容接口 | 支持 | 支持 |
| OpenAI Responses 兼容接口 | 支持 | 支持 |
| 多轮对话 | 支持 | 支持 |
| 工具 / Function Calling | 支持 | 支持 |
| 图片输入与文件附件 | 支持 | 支持 |
| 图片生成 | 支持 | 不支持 |
| 多用户 Token 隔离 | 支持 | 支持 |
| 单浏览器 Worker 串行队列 | 支持 | 支持 |
| 会话、消息与文件持久化 | 支持 | 支持 |
| 终端交互客户端 | 支持 | 支持 |

## 工作流程

```text
业务应用 / OpenAI SDK / LangChain
                |
                v
       GPT-FastAPI（FastAPI）
                |
                v
      单 Worker 浏览器会话队列
                |
                v
       chatgpt.com 或 claude.ai
                |
                v
      解析网页回复并返回标准 JSON
```

## 环境要求

- Python 3.10 或更高版本，推荐 Python 3.11。
- Google Chrome 或 Patchright 可用的 Chromium。
- 可访问 `chatgpt.com` 或 `claude.ai` 的网络环境。
- 一个能够正常登录对应网页的账号。

## 快速开始

### 1. 获取代码

```bash
git clone https://github.com/3148044736liu-code/GPT-FastAPI.git
cd GPT-FastAPI
```

### 2. 创建虚拟环境

```bash
python -m venv .venv
```

macOS / Linux：

```bash
source .venv/bin/activate
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
```

### 3. 安装依赖

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
patchright install chromium
```

### 4. 创建配置文件

macOS / Linux：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

建议至少修改以下配置：

```dotenv
# 可选 chatgpt 或 claude
PROVIDER=chatgpt

# 不同服务商建议使用不同的浏览器配置目录
BROWSER_DATA_DIR=./browser_data

# 仅本机访问可使用 127.0.0.1
API_HOST=127.0.0.1
API_PORT=3061

# 请替换为随机强 Token
API_TOKEN=replace-with-a-strong-token
ADMIN_TOKEN=replace-with-a-separate-admin-token
DOWNLOAD_SECRET=replace-with-another-random-secret

# 浏览器 Worker 数量：当前部署固定使用 1 个 Worker
MAX_CONCURRENT_SESSIONS=1

# 相邻浏览器任务的随机最小开始间隔（秒）
BROWSER_TASK_GAP_MIN_SECONDS=30
BROWSER_TASK_GAP_MAX_SECONDS=50
MAX_BROWSER_QUEUE_DEPTH=100
```

可以使用下面的命令生成随机 Token：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

如果使用 Claude，建议同时修改：

```dotenv
PROVIDER=claude
BROWSER_DATA_DIR=./browser_data_claude
```

### 5. 完成首次登录

```bash
python scripts/first_login.py
```

脚本会打开浏览器窗口。完成 ChatGPT 或 Claude 登录后，登录状态会保存在 `BROWSER_DATA_DIR` 指定的目录中。

### 6. 启动服务

```bash
python -m src.api.server
```

服务启动后会自动：

1. 初始化本地数据库和文件目录。
2. 启动浏览器并恢复登录状态。
3. 创建单 Worker 多会话队列。
4. 启动过期文件清理任务。
5. 在 `API_HOST:API_PORT` 上提供 HTTP 服务。

### 7. 验证状态

以下示例假设服务地址为 `http://127.0.0.1:3061`：

```bash
curl http://127.0.0.1:3061/healthz
```

预期返回：

```json
{"status":"ok"}
```

检查浏览器登录状态：

```bash
curl http://127.0.0.1:3061/status \
  -H "Authorization: Bearer replace-with-a-strong-token"
```

运维与接口页面：

- 运行仪表盘：`http://127.0.0.1:3061/dashboard`
- Swagger UI：`http://127.0.0.1:3061/docs`
- ReDoc：`http://127.0.0.1:3061/redoc`
- OpenAPI JSON：`http://127.0.0.1:3061/openapi.json`

## 配置说明

| 配置项 | 说明 | 示例 |
|---|---|---|
| `PROVIDER` | 当前网页服务商 | `chatgpt` |
| `BROWSER_DATA_DIR` | 浏览器配置、Cookie 和登录状态目录 | `./browser_data` |
| `HEADLESS` | 是否隐藏浏览器窗口，排障时建议设为 `false` | `false` |
| `BROWSER_KEEP_FOREGROUND` | Windows 服务启动后持续显示并置顶 ChatGPT 浏览器窗口 | `true` |
| `API_HOST` | FastAPI 监听地址 | `127.0.0.1` |
| `API_PORT` | FastAPI 监听端口 | `3061` |
| `API_TOKEN` | 默认 Bearer Token | 随机强字符串 |
| `ADMIN_TOKEN` | Dashboard 与管理 API 的独立管理员 Token | 与项目 Token 不同的随机强字符串 |
| `API_USER_TOKENS` | 多用户 Token，格式为 `用户:Token`，逗号分隔 | `alice:token1,bob:token2` |
| `MAX_CONCURRENT_SESSIONS` | 浏览器 Worker 池容量（当前部署使用单 Worker） | `1` |
| `BROWSER_TASK_GAP_MIN_SECONDS` / `MAX` | 相邻浏览器任务的随机 start-to-start 间隔 | `30` / `50` |
| `MAX_BROWSER_QUEUE_DEPTH` | 并发提交队列上限 | `100` |
| `RESPONSE_TIMEOUT` | 等待模型完成回复的最长时间，单位毫秒 | `120000` |
| `MAX_FILE_SIZE_MB` | 单文件上传上限 | `25` |
| `USER_FILE_QUOTA_MB` | 单用户文件总配额 | `500` |
| `MAX_FILES_PER_SESSION` | 单会话最多保留文件数 | `20` |
| `FILE_TTL_HOURS` | 文件保留时长 | `72` |
| `FILE_CLEANUP_INTERVAL_MINUTES` | 过期文件清理周期 | `10` |
| `DOWNLOAD_SECRET` | 临时下载链接签名密钥 | 随机强字符串 |
| `OPENAI_USE_SESSION_POOL` | 无 `session_id` 的兼容请求是否自动进入会话池 | `false` |
| `LOG_LEVEL` | 日志级别 | `INFO` |
| `LOG_RETENTION_DAYS` | 按天日志的保留天数，过期文件自动清理 | `30` |
| `DASHBOARD_ONLINE_WINDOW_SECONDS` | 仪表盘判定任务机在线的最近调用窗口（秒） | `300` |
| `DASHBOARD_MAX_RECENT_REQUESTS` | 仪表盘保留的最近调用记录数 | `200` |

完整配置及默认值请查看 `.env.example`。

## 运行仪表盘与任务机标识

访问 `http://<服务器 IP>:3061/dashboard` 即可直接查看，无需输入 API Token：

- 服务、登录与浏览器前台状态。
- 单浏览器 Worker 队列的容量、空闲与占用数。
- 当前调用 API 的任务机 IP、调用量和最近活动。
- 接口成功率、高频路径和最近调用记录。
- 按天划分的服务日志与 API 访问日志，可直接在页面底部切换查看。
- 右上角可直接下载与当前部署版本一致的 Markdown 开发说明书。
- 浏览器运行层控制：可在仪表盘启动项目并打开 ChatGPT 页面，或停止项目并关闭全部浏览器和 Worker 页面；停止后仪表盘仍可访问。
- 项目授权 Token 生成台：创建、反复复制、轮换、停用和删除项目 Token。Token 使用 Windows DPAPI 加密保存，数据库不保存明文。

仪表盘只记录请求元数据，不记录提示词、返回正文或 Token。调用方可选择携带以下请求头，让任务机名称和任务名在仪表盘中更清晰：

```http
X-Task-Machine: render-node-01
X-Task-Name: daily-report
```

## 认证方式

除以下公开地址外，其他接口都需要 Bearer Token：

- `/healthz`
- `/docs`
- `/redoc`
- `/openapi.json`
- `/dashboard`
- `/dashboard/`
- `/api/dashboard/state`
- `/api/dashboard/logs`
- `/api/dashboard/developer-guide`
- `/api/dashboard/projects` 及其创建、复制、轮换、启停、删除子接口（仅允许本机或可信内网、同源页面访问）
- `/api/dashboard/runtime` 及其启动、停止子接口（仅允许本机或可信内网、同源页面访问）

仪表盘会显示调用任务机 IP、任务名称及接口调用元数据，建议仅向可信内网开放 3061 端口。业务接口仍需 Bearer Token。

## 项目 Token 与多智能体会话

在运维仪表盘的“项目授权 Token 生成台”中，为每个接入项目生成独立 Token。服务端保存用于鉴权的 SHA-256 摘要，并使用 Windows DPAPI（本机范围）加密保存可恢复副本，因此可在可信内网的仪表盘中反复复制；数据库不会保存明文。轮换会让旧 Token 立即失效，也可以随时停用或删除项目 Token。删除后 Token 立即吊销，历史会话和文件元数据保留，原 project_id 不可重新占用，避免误继承旧数据。

## 仪表盘启动与停止项目

访问 `http://<服务器 IP>:3061/dashboard`，右上角提供“启动项目”和“停止项目”：

- 停止项目：先记录人工停止状态，再关闭主 ChatGPT 页面和全部 Worker 页面；浏览器类业务接口返回 HTTP 503，Dashboard、日志、Token 管理和数据库管理仍保持可用。
- 启动项目：重新启动浏览器、恢复登录、打开主页面和 Worker 页面，并恢复浏览器类 API。
- 人工停止状态会持久化到 `data/browser_runtime_state.json`。Windows 守护脚本会识别该状态，不会将主动关闭浏览器误判为崩溃；服务器进程重启后仍保持停止，直到再次点击启动。

Token 行内的“复制”按钮可随时读取 DPAPI 加密副本并复制；“轮换”会生成新 Token 并立即吊销旧 Token；“删除”会永久吊销并隐藏该项目授权。

项目 Token 对应数据隔离关系：

```text
项目 Token → project_id（owner_id）→ session_id → provider_thread_id
```

项目 Token 调用浏览器类接口时必须携带 `session_id`。共享页面接口 `/chat`、`/thread*`、`/threads` 和 `/status` 对项目 Token 禁用。支持 `session_id` 的接口包括：

- `POST /v1/sessions/{session_id}/messages`
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/images/generations`

同一项目、同一 `session_id` 的请求会在 HTTP 请求开始到数据库写入完成的整个周期内严格串行。所有项目和所有 session 共用 1 个浏览器 Worker；一个浏览器任务结束后至少等待 30 秒，下一个任务才会开始，因此不同 session 也不会并发或高频操作 ChatGPT 网页。

多智能体项目以 Task 为隔离单元：同一 Agent 的不同任务也要创建独立 Task Conversation。先为 Agent 分配 Capability，再创建 Task：

```http
POST /v1/tasks
Authorization: Bearer <项目 Token>
X-Agent-ID: planner
Content-Type: application/json

{
  "agent_id": "planner",
  "name": "本次规划任务"
}
```

随后使用返回的随机 `task_id` 继续提交；并发请求会立即进入队列，网页侧始终只有一个标签页串行执行：

```json
POST /v1/tasks/tsk_7F3A91C2/messages
Authorization: Bearer <项目 Token>
X-Agent-ID: planner
Prefer: respond-async

{"content": "继续处理上一阶段结果"}
```

每个 Task 绑定独立 `session_id` 和 ChatGPT Conversation，网页标题为 `task_id`。执行前 Gateway 从 Recent 列表精确切换并校验 thread；找不到目标对话时不会发送 Prompt。

项目 Token 上传文件时必须绑定 `session_id`；消息发送只能引用属于同一项目、同一 session 的文件。

日志保存在 `C:\ChatGPTAPI\logs`，文件格式为 `<组件>_YYYY-MM-DD.log`。其中 `api_access_YYYY-MM-DD.log` 记录请求 ID、调用方 IP、任务机、方法、接口、状态码与耗时；`api_errors_YYYY-MM-DD.log` 保存未处理异常及完整堆栈。响应头中的 `X-Request-ID` 可用于把任务机报错与服务器日志对应起来。日志不会保存 API Token、请求正文或模型回复正文。

请求头格式：

```http
Authorization: Bearer <API_TOKEN>
```

设置 `API_USER_TOKENS` 后，每个 Token 会映射到独立的 `owner_id`，会话、消息和文件只能由所属用户访问。

## OpenAI SDK 调用

安装客户端：

```bash
python -m pip install openai
```

ChatGPT 示例：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:3061/v1",
    api_key="replace-with-a-strong-token",
)

response = client.chat.completions.create(
    model="catgpt-browser",
    messages=[
        {"role": "user", "content": "请用三句话介绍 FastAPI。"},
    ],
)

print(response.choices[0].message.content)
```

使用 Claude 时，将模型名改为：

```python
model="claude-browser"
```

## curl 调用

```bash
curl http://127.0.0.1:3061/v1/chat/completions \
  -H "Authorization: Bearer replace-with-a-strong-token" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "catgpt-browser",
    "messages": [
      {"role": "user", "content": "你好，请介绍一下你自己。"}
    ]
  }'
```

当前回复为非流式返回，请不要设置 `stream=true`。

## 多会话与文件接口

### 常用接口

| 方法 | 地址 | 说明 |
|---|---|---|
| `GET` | `/v1/me` | 查询当前用户和文件配额 |
| `GET` | `/v1/pool/status` | 查询浏览器 Worker 池状态 |
| `POST` | `/v1/sessions` | 创建会话 |
| `GET` | `/v1/sessions` | 查询会话列表 |
| `GET` | `/v1/sessions/{session_id}` | 查询会话详情 |
| `DELETE` | `/v1/sessions/{session_id}` | 删除会话记录 |
| `GET` | `/v1/sessions/{session_id}/messages` | 查询消息记录 |
| `POST` | `/v1/sessions/{session_id}/messages` | 向指定会话发送消息 |
| `POST` | `/v1/files` | 上传文件 |
| `GET` | `/v1/files` | 查询文件列表 |
| `GET` | `/v1/files/{file_id}` | 查询文件信息 |
| `DELETE` | `/v1/files/{file_id}` | 删除文件 |
| `GET` | `/v1/files/{file_id}/content` | 下载文件内容 |

### 创建会话

```bash
curl http://127.0.0.1:3061/v1/sessions \
  -X POST \
  -H "Authorization: Bearer replace-with-a-strong-token" \
  -H "Content-Type: application/json" \
  -d '{"title":"资料分析"}'
```

记录返回结果中的会话 ID，例如 `ses_xxx`。

### 上传文件

```bash
curl http://127.0.0.1:3061/v1/files \
  -X POST \
  -H "Authorization: Bearer replace-with-a-strong-token" \
  -F "file=@./example.pdf" \
  -F "session_id=ses_xxx"
```

记录返回结果中的文件 ID，例如 `file_xxx`。

### 发送带附件的消息

```bash
curl http://127.0.0.1:3061/v1/sessions/ses_xxx/messages \
  -X POST \
  -H "Authorization: Bearer replace-with-a-strong-token" \
  -H "Content-Type: application/json" \
  -d '{
    "content":"请分析附件并总结重点。",
    "file_ids":["file_xxx"]
  }'
```

不同会话可以并发执行，同一个会话内的消息会按顺序处理。

## OpenAI 兼容接口

| 方法 | 地址 | 说明 |
|---|---|---|
| `GET` | `/v1/models` | 查询当前可用模型 |
| `POST` | `/v1/chat/completions` | Chat Completions 兼容接口 |
| `POST` | `/v1/responses` | Responses 兼容接口 |
| `POST` | `/v1/images/generations` | 图片生成，仅 ChatGPT 可用 |

`/v1/chat/completions` 和 `/v1/responses` 支持可选的 `session_id`，可将兼容接口请求绑定到已有会话。

## 旧版兼容接口

项目仍保留以下早期接口，建议新项目优先使用 `/v1/*`：

| 方法 | 地址 | 说明 |
|---|---|---|
| `POST` | `/chat` | 单次聊天 |
| `POST` | `/thread/new` | 新建网页对话并发送消息 |
| `POST` | `/thread/{thread_id}/chat` | 在指定网页对话中继续聊天 |
| `GET` | `/threads` | 查询网页对话列表 |
| `GET` | `/status` | 查询服务商与登录状态 |

## 数据目录

| 路径 | 内容 |
|---|---|
| `browser_data/` | ChatGPT 浏览器配置、Cookie 和登录状态 |
| `browser_data_claude/` | Claude 浏览器配置、Cookie 和登录状态 |
| `data/catgpt_gateway.db` | 会话、消息和文件元数据 |
| `data/files/` | 上传文件和模型生成文件 |
| `logs/` | 服务运行日志 |
| `downloads/` | 调试和测试下载内容 |

这些目录以及 `.env` 已被 Git 忽略。不要手动上传浏览器配置、Cookie、数据库、日志或真实 Token。

## 项目结构

```text
GPT-FastAPI/
├─ src/
│  ├─ api/          # FastAPI 服务、兼容接口、会话与文件接口
│  ├─ browser/      # 浏览器启动、登录和反检测处理
│  ├─ chatgpt/      # ChatGPT 页面交互与回复解析
│  ├─ claude/       # Claude 页面交互与回复解析
│  ├─ cli/          # 终端交互客户端
│  ├─ files/        # 文件生成、识别和清理
│  └─ storage/      # SQLite 持久化
├─ scripts/         # 首次登录、诊断和冒烟测试脚本
├─ tests/           # 自动化测试
├─ docs/            # API、架构与扩展文档
├─ .env.example     # 配置模板
└─ requirements.txt # Python 依赖
```

## 运行测试

激活虚拟环境后执行：

```bash
python -m pytest -q
```

接口冒烟测试：

```bash
python scripts/smoke_test.py \
  --base http://127.0.0.1:3061 \
  --token replace-with-a-strong-token \
  --no-live
```

移除 `--no-live` 后，脚本会向当前服务商发送真实测试消息。

诊断上传控件：

执行前请先停止 API 服务，避免浏览器配置目录被同时占用。

```bash
python scripts/diagnose_upload.py
```

## 常见问题

### `/healthz` 正常，但 `/status` 显示未登录

浏览器进程仍在运行不代表网页会话仍然有效。重新执行：

```bash
python scripts/first_login.py
```

完成登录后重启 API 服务。

### 请求返回 `401 Unauthorized`

确认请求头中的 Token 与 `.env` 中的 `API_TOKEN` 或 `API_USER_TOKENS` 一致：

```http
Authorization: Bearer <正确的 Token>
```

### 日志提示页面或浏览器已经关闭

停止残留的服务和浏览器进程，确认端口已经释放，再重新执行：

```bash
python -m src.api.server
```

同时检查 `logs/` 中最新的服务日志。

### 需要在局域网访问

可以将 `API_HOST` 设置为当前机器的局域网 IP，或设置为 `0.0.0.0`。同时需要：

- 放行对应端口的系统防火墙规则。
- 客户端使用服务端真实局域网 IP。
- 使用随机强 Token，不要继续使用示例值。
- 不要把服务直接暴露到公网。

## 已知限制

- 依赖 ChatGPT 和 Claude 网页结构，页面更新后可能需要同步调整选择器。
- 模型回复需要经过真实浏览器交互，速度通常慢于官方 API。
- 当前不支持流式响应。
- 工具调用通过提示词和结构化解析实现，不是网页原生 API 能力。
- 浏览器登录会定期失效，需要重新登录。
- 图片生成仅适用于 ChatGPT。

## 安全建议

- 将 `.env`、浏览器配置目录、数据库和日志视为敏感数据。
- 局域网共享时必须设置强 `API_TOKEN` 和 `DOWNLOAD_SECRET`。
- 多用户场景优先使用 `API_USER_TOKENS` 隔离数据。
- 定期清理不再使用的会话、文件和过期 Token。
- 仅在你有权使用的账号和环境中运行本项目，并遵守对应服务条款。

## 许可证

本项目使用 MIT License，详见 [LICENSE](LICENSE)。

## 致谢

项目基于 CatGPT Gateway 持续扩展，核心能力由 FastAPI、Patchright、Pydantic、SQLite 与 OpenAI 兼容生态共同支持。

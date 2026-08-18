# CatGPT-Gateway 接口文档

> 版本：v1.1.0 ｜ 更新：2026-08-07
>
> 本文档覆盖 CatGPT-Gateway 的全部 HTTP 接口。在线交互式文档见
> `http://<host>:<port>/docs`（右上角 **Authorize** 填入 token 即可调试）。

---

## 目录

1. [概览](#1-概览)
2. [认证](#2-认证)
3. [多用户会话与文件 API（推荐入口）](#3-多用户会话与文件-api推荐入口)
4. [OpenAI 兼容 API](#4-openai-兼容-api)
5. [旧版兼容 API](#5-旧版兼容-api)
6. [系统端点](#6-系统端点)
7. [错误码一览](#7-错误码一览)
8. [相关配置速查](#8-相关配置速查)
9. [典型接入流程](#9-典型接入流程)
10. [行为说明与注意事项](#10-行为说明与注意事项)

---

## 1. 概览

CatGPT-Gateway 把 ChatGPT / Claude 网页能力包装成 HTTP API。接口分三组：

| 分组 | 说明 | 适用场景 |
|---|---|---|
| **多用户会话与文件** | `/v1/sessions`、`/v1/files`、`/v1/me`、`/v1/pool/status` | 多人调用、多会话并发、文件上传下载、消息持久化（**推荐**） |
| **OpenAI 兼容** | `/v1/chat/completions`、`/v1/images/generations`、`/v1/models`、`/v1/responses` | OpenAI SDK / LangChain / Codex CLI 直接接入 |
| **旧版兼容** | `/chat`、`/thread/*`、`/threads`、`/status` | 早期单会话接口，仅兼容保留 |

基本参数（默认值见 `.env`）：

```text
Base URL:  http://192.168.8.222:5061
Provider:  chatgpt（模型 id: catgpt-browser）或 claude（模型 id: claude-browser）
```

---

## 2. 认证

### 2.1 Bearer Token

除少数开放路径外，所有接口都要求请求头携带：

```text
Authorization: Bearer <token>
```

Token 在服务端 `.env` 中配置：

```text
API_TOKEN=dummy123                                  # 兼容 token，owner = "default"
API_USER_TOKENS=user1:secret1,user2:secret2,...     # 多用户 token，owner = 冒号前的名字
```

- 不同 token 映射到不同 owner；会话、消息、文件按 owner 完全隔离（互相不可见，访问返回 404/401）
- token 无效或缺失返回 `401`
- 注意：这是 **API 层隔离**，底层仍共用同一个浏览器登录态（同一个 ChatGPT 账号）

### 2.2 开放路径（无需 token）

```text
GET /healthz          健康检查
GET /docs             Swagger 交互文档
GET /redoc            ReDoc 文档
GET /openapi.json     OpenAPI 规范
```

### 2.3 文件下载的两种授权

`GET /v1/files/{file_id}/content` 支持两种方式任选其一：

1. 携带文件所有者的 Bearer Token；
2. 使用文件接口返回的 `download_url`（内含 `expires` 与 `signature` 查询参数，默认有效期 10 分钟，基于 `DOWNLOAD_SECRET` 签名）。

---

## 3. 多用户会话与文件 API（推荐入口）

### 3.1 GET `/v1/me` — 查看当前调用者

返回当前 token 对应的用户信息、并发上限与文件用量。

响应示例：

```json
{
  "owner_id": "default",
  "max_concurrent_sessions": 3,
  "file_usage": {
    "used_bytes": 12488,
    "file_count": 6,
    "quota_bytes": 524288000
  }
}
```

### 3.2 GET `/v1/pool/status` — 浏览器并发池状态

```json
{ "capacity": 3, "available": 3, "busy": 0 }
```

- `capacity`：worker 池总页面数（`MAX_CONCURRENT_SESSIONS`）
- `available`：当前空闲页面数；`busy`：正在处理消息的页面数

### 3.3 POST `/v1/sessions` — 新建会话

请求体：

```json
{ "title": "我的会话" }        // 可选，最长 200 字符，默认 "New session"
```

响应 `201`：

```json
{
  "id": "ses_4f3a...",
  "owner_id": "default",
  "provider": "chatgpt",
  "provider_thread_id": null,
  "title": "我的会话",
  "status": "new",
  "created_at": "2026-08-07T03:37:31+00:00",
  "updated_at": "2026-08-07T03:37:31+00:00",
  "last_active_at": null
}
```

第一次向该会话发消息后，会自动在 ChatGPT 网页侧新建对话，并把真实
thread id 写入 `provider_thread_id`，`status` 随之变化：

```text
new → busy（处理中）→ active（正常）/ error（provider 出错）
```

### 3.4 GET `/v1/sessions` — 会话列表

查询参数：`limit`（1–200，默认 50）。按最近活跃时间倒序。

```json
{ "data": [ { "id": "ses_...", "...": "..." } ] }
```

### 3.5 GET `/v1/sessions/{session_id}` — 查询单个会话

返回会话对象。会话不存在或不属于当前用户：`404`。

### 3.6 DELETE `/v1/sessions/{session_id}` — 删除会话

删除内部会话及其消息记录（ChatGPT 网页侧对话不会被删除）。

```json
{ "id": "ses_...", "deleted": true }
```

### 3.7 GET `/v1/sessions/{session_id}/messages` — 会话消息记录

按时间正序返回该会话保存的全部消息：

```json
{
  "data": [
    { "id": "msg_...", "session_id": "ses_...", "role": "user",
      "content": "...", "created_at": "..." },
    { "id": "msg_...", "session_id": "ses_...", "role": "assistant",
      "content": "...", "created_at": "..." }
  ]
}
```

### 3.8 POST `/v1/sessions/{session_id}/messages` — 发送消息（多会话并发入口）

请求体：

```json
{
  "content": "请读取附件并总结要点",   // 必填，1–100000 字符
  "file_ids": ["file_..."]             // 可选，最多 10 个，须先通过 /v1/files 上传
}
```

响应 `200`：

```json
{
  "id": "msg_...",
  "session_id": "ses_...",
  "provider_thread_id": "6a755902-...",
  "message": "（ChatGPT 回复文本；若有生成文件，末尾附下载链接）",
  "response_time_ms": 14796,
  "attachments": [
    {
      "id": "file_...",
      "object": "file",
      "session_id": "ses_...",
      "source": "generated",
      "filename": "report.xlsx",
      "bytes": 20480,
      "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "created_at": "...",
      "expires_at": "...",
      "download_url": "http://.../v1/files/file_.../content?expires=...&signature=..."
    }
  ],
  "pool": { "capacity": 3, "available": 2 }
}
```

错误：

| 状态码 | 场景 |
|---|---|
| `404` | 会话不存在/不属于当前用户；`file_ids` 中某文件不存在/不属于当前用户 |
| `410` | `file_ids` 中某文件内容已过期 |
| `500` | Provider 错误（浏览器操作失败等） |
| `503` | 服务未完成初始化 |

行为说明：

- `file_ids` 对应的本地文件会作为附件上传到 ChatGPT 输入框（见 [10.1](#101-附件上传行为)）
- 同一会话内消息串行执行；不同会话最多 3 路并发
- 回复中检测到 ChatGPT 生成的文件时会写入文件库（计入配额），并在
  `attachments` 与 `message` 末尾给出下载链接；超配额时静默丢弃生成文件、不影响消息本身

### 3.9 POST `/v1/files` — 上传文件

`multipart/form-data`：

| 字段 | 必填 | 说明 |
|---|---|---|
| `file` | 是 | 要上传的文件 |
| `session_id` | 否 | 绑定到的会话 id（之后发消息可用 `file_ids` 引用） |

响应 `201`（文件元数据对象，结构同 3.8 的 `attachments` 元素，`source` 为 `"upload"`）。

错误：

| 状态码 | 场景 |
|---|---|
| `404` | `session_id` 不存在/不属于当前用户 |
| `409` | 该会话文件数已达 `MAX_FILES_PER_SESSION` |
| `413` | 文件超过 `MAX_FILE_SIZE_MB` |
| `507` | 用户磁盘用量超过 `USER_FILE_QUOTA_MB` |

### 3.10 GET `/v1/files` — 文件列表

查询参数：`limit`（1–500，默认 100），按创建时间倒序。

```json
{ "data": [ { "id": "file_...", "...": "..." } ] }
```

### 3.11 GET `/v1/files/{file_id}` — 查询文件信息

返回文件元数据对象（含新的 `download_url`）。文件不存在/不属于当前用户：`404`。

### 3.12 DELETE `/v1/files/{file_id}` — 删除文件

删除文件记录及磁盘内容：

```json
{ "id": "file_...", "deleted": true }
```

### 3.13 GET `/v1/files/{file_id}/content` — 下载文件内容

查询参数（签名方式时需要）：`expires`（过期时间戳）、`signature`。

成功时直接返回文件字节流（`Content-Disposition` 带原始文件名）。

错误：

| 状态码 | 场景 |
|---|---|
| `401` | 无有效授权（token 非文件所有者，且签名缺失/错误/过期） |
| `404` | 文件记录不存在 |
| `410` | 文件已过期或磁盘内容已清理 |

---

## 4. OpenAI 兼容 API

兼容 OpenAI SDK / LangChain。默认模型 id：`catgpt-browser`（PROVIDER=claude 时为 `claude-browser`）。

### 4.1 POST `/v1/chat/completions` — 聊天

标准 OpenAI 请求字段均支持：`model`、`messages`、`tools`、`tool_choice`
（`auto`/`none`/`required`/指定函数）、`temperature`、`max_tokens`、`top_p`、
`stop`、`n`、`user`。

**网关扩展字段：**

| 字段 | 说明 |
|---|---|
| `session_id` | 传入一个 `/v1/sessions` 创建的会话 id，本请求改走多会话 worker 池：获得用户隔离、消息持久化、3 路并发；响应回显实际使用的 `session_id`。不传时默认走原单页逻辑（除非服务端开启 `OPENAI_USE_SESSION_POOL=true`，此时自动创建临时会话） |

**限制：**

- `stream=true` 返回 `400`（浏览器后端无法流式输出）
- `session_id` 不存在/不属于当前用户返回 `404`；会话池未初始化返回 `503`

**消息内容支持的形态：**

```jsonc
// 纯文本
{"role": "user", "content": "你好"}

// 图片输入（OpenAI vision 格式，URL / base64 data URL 均可）
{"role": "user", "content": [
  {"type": "text", "text": "这张图里是什么？"},
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,...."}}
]}

// 文件附件（网关扩展）
{"role": "user", "content": [
  {"type": "text", "text": "请总结这个文件"},
  {"type": "file", "file": {"filename": "doc.pdf",
                             "data": "<base64>",
                             "mime_type": "application/pdf"}}
]}
// 也支持 data-URL 写法：
// {"type": "file", "file": {"filename": "doc.pdf",
//                            "url": "data:application/pdf;base64,...."}}
```

**响应**（标准 OpenAI 结构 + `session_id` 回显，OpenAI SDK 会忽略该字段）：

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1786074000,
  "model": "catgpt-browser",
  "choices": [{
    "index": 0,
    "message": { "role": "assistant", "content": "POOL-OK", "tool_calls": null },
    "finish_reason": "stop"
  }],
  "usage": { "prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15 },
  "session_id": "ses_..."
}
```

**工具调用（function calling）：** 通过提示词注入实现。模型调用工具时
`finish_reason="tool_calls"`、`message.content=null`，`tool_calls` 结构标准。
把工具结果以 `role: "tool"` 消息传回即可继续。

### 4.2 POST `/v1/images/generations` — 图片生成（仅 ChatGPT）

请求：

| 字段 | 说明 |
|---|---|
| `prompt` | 必填 |
| `n` | 1–4，默认 1 |
| `size` | 如 `1024x1024` |
| `quality` | `standard` / `hd` |
| `style` | `vivid` / `natural` |
| `response_format` | `b64_json`（默认）或 `url`（返回服务器本地路径） |

响应为标准 `ImagesResponse`。PROVIDER=claude 时返回 `501`；
ChatGPT 未真正生成图片（例如拒绝或只给文字描述）时返回 `422`。

### 4.3 GET `/v1/models` — 模型列表

```json
{ "object": "list",
  "data": [ { "id": "catgpt-browser", "object": "model",
              "created": 1700000000, "owned_by": "catgpt" } ] }
```

### 4.4 POST `/v1/responses` — Responses API（Codex CLI 兼容）

接受 Responses API 格式：`input`（字符串或消息/`function_call`/
`function_call_output` 项数组）、`instructions`、扁平 `tools`、`tool_choice` 等。

- 支持 `stream=true`：以 SSE 返回 `response.created → ... → response.completed` 事件序列
- 非流式返回完整 ResponseObject（含 `output_text` 便捷字段）

---

## 5. 旧版兼容 API

单页串行逻辑，无用户隔离、无持久化。仅作兼容保留，新接入请使用第 3/4 组。

### 5.1 POST `/chat` — 在当前对话发送消息

```json
// 请求
{ "message": "你好" }

// 响应
{
  "message": "（markdown 回复）",
  "thread_id": "6a75...",
  "response_time_ms": 8421,
  "images": [ { "url": "...", "alt": "...", "local_path": "...", "prompt_title": "..." } ],
  "has_images": false
}
```

### 5.2 POST `/thread/{thread_id}/chat` — 在指定对话发送消息

请求体同 `/chat`；会先导航到该 thread 再发送。

### 5.3 POST `/thread/new` — 新建对话并发送第一条消息

请求体同 `/chat`。

### 5.4 GET `/threads` — 最近对话列表（网页侧边栏）

```json
{ "threads": [ { "id": "6a75...", "title": "...", "url": "https://chatgpt.com/c/..." } ] }
```

### 5.5 GET `/status` — 登录状态

```json
{ "status": "ok", "logged_in": true, "current_thread": "6a75..." }
```

---

## 6. 系统端点

| 端点 | 说明 |
|---|---|
| `GET /healthz` | 健康检查，返回 `{"status":"ok"}`，无需鉴权 |
| `GET /docs` | Swagger 交互文档（含 Authorize 在线调试） |
| `GET /redoc` | ReDoc 文档 |
| `GET /openapi.json` | OpenAPI 3.0 规范 |

---

## 7. 错误码一览

| 状态码 | 含义 |
|---|---|
| `400` | 请求不合法（如 stream=true、messages 为空） |
| `401` | token 无效/缺失；文件下载授权无效 |
| `404` | 资源不存在，或不属于当前用户 |
| `409` | 单会话文件数超限 |
| `410` | 文件内容已过期 |
| `413` | 单文件大小超限 |
| `422` | 期望生成图片但 ChatGPT 未生成 |
| `500` | Provider 错误（浏览器/网页操作失败） |
| `501` | 当前 provider 不支持（如 Claude 生成图片） |
| `503` | 服务组件未就绪（浏览器/数据库/会话池未初始化） |
| `507` | 用户磁盘配额不足 |

鉴权类错误的统一结构：

```json
{ "error": { "message": "Invalid or missing API token...", "type": "auth_error" } }
```

---

## 8. 相关配置速查

均通过 `.env` 配置（完整注释见 `.env.example`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `API_HOST` / `API_PORT` | `192.168.8.222` / `5061` | 监听地址 |
| `PROVIDER` | `chatgpt` | `chatgpt` 或 `claude` |
| `API_TOKEN` | — | 兼容 token（owner=default） |
| `API_USER_TOKENS` | — | `owner:token,...` 多用户 token |
| `DOWNLOAD_SECRET` | — | 签名下载链接的密钥 |
| `MAX_CONCURRENT_SESSIONS` | `3` | 会话 worker 池页面数 |
| `MAX_FILE_SIZE_MB` | `25` | 单文件上传上限（413） |
| `USER_FILE_QUOTA_MB` | `500` | 每用户磁盘配额（507） |
| `MAX_FILES_PER_SESSION` | `20` | 单会话文件数上限（409） |
| `FILE_TTL_HOURS` | `72` | 文件默认保留时长 |
| `FILE_CLEANUP_INTERVAL_MINUTES` | `10` | 后台过期清理周期 |
| `OPENAI_USE_SESSION_POOL` | `false` | 无 session_id 时 /v1/chat/completions 是否也走会话池 |

---

## 9. 典型接入流程

### 9.1 curl：会话 + 附件 + 下载

```bash
BASE=http://192.168.8.222:5061
TOKEN=dummy123

# 1) 新建会话
SES=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"demo"}' $BASE/v1/sessions | jq -r .id)

# 2) 上传文件（绑定到会话）
FID=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -F "file=@report.pdf" -F "session_id=$SES" $BASE/v1/files | jq -r .id)

# 3) 发送带附件的消息
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"content\":\"请读取附件并总结要点\",\"file_ids\":[\"$FID\"]}" \
  $BASE/v1/sessions/$SES/messages

# 4) 下载文件（Bearer 方式；或直接打开返回的 download_url）
curl -L -H "Authorization: Bearer $TOKEN" -o out.bin \
  $BASE/v1/files/$FID/content
```

### 9.2 Python（OpenAI SDK）

```python
from openai import OpenAI

client = OpenAI(base_url="http://192.168.8.222:5061/v1", api_key="dummy123")

# 普通调用（原单页逻辑）
resp = client.chat.completions.create(
    model="catgpt-browser",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)

# 接入会话池：先经 REST 创建 session，再通过 extra_body 传入
resp = client.chat.completions.create(
    model="catgpt-browser",
    messages=[{"role": "user", "content": "继续上面的话题"}],
    extra_body={"session_id": "ses_xxx"},
)
print(resp.session_id)  # 回显实际使用的会话
```

### 9.3 多人隔离

为每个调用方在 `API_USER_TOKENS` 里分配独立 token：

```text
API_USER_TOKENS=alice:<secret-a>,bob:<secret-b>
```

各自用自己的 token 调用即可；会话与文件互相不可见。

---

## 10. 行为说明与注意事项

### 10.1 附件上传行为

- 非图片文件走 composer 的通用文件 input（`#upload-files`）；图片可用
  `#upload-photos`。客户端会自动按文件类型选择，避免把文档塞进仅图片的 input
- 上传后会等待附件在输入框暂存确认（最长 8 秒），并等待 2 秒让网页完成处理；
  若确认失败会记录警告但继续发送（日志关键字：`upload verified` / `Could not verify`）
- ChatGPT 前端改版可能使上传失效，诊断脚本：`scripts/diagnose_upload.py`（需先停服）

### 10.2 并发模型

- 新 session API：同一会话内串行，不同会话由 3 个独立浏览器页面并发处理
- OpenAI 兼容接口（不带 session_id）与旧版接口：共用同一个页面，全局串行锁
- 消息之间没有配额联动：所有请求最终消耗的是同一个 ChatGPT 账号的网页额度

### 10.3 文件生命周期

```text
上传/生成 → 写入 data/files/<owner>/<key>/ → 记录入 files 表
  → expires_at = 创建时间 + FILE_TTL_HOURS
  → 后台任务每 FILE_CLEANUP_INTERVAL_MINUTES 分钟清理过期文件（记录+磁盘）
  → 也可随时 DELETE /v1/files/{id} 手动删除
```

### 10.4 已知边界

- API 用户隔离 ≠ ChatGPT 账号隔离（所有会话都在同一账号下）
- 生成文件捕获是 best-effort，依赖 ChatGPT 网页 DOM
- `/v1/chat/completions` 的 `stream`、以及 session 消息接口不支持流式
- token 估算（usage）为粗略值（约 4 字符/token）

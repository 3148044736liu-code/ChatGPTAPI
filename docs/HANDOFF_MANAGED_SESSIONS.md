# CatGPT-Gateway 多用户会话与文件接口交接文档

更新时间：2026-08-07（第二轮开发完成）

本文档用于把当前实现状态交接给后续继续完善本项目的编程 AI 或开发者。

## 1. 项目当前目标

当前项目 `CatGPT-Gateway` 原本主要是把 ChatGPT 网页能力包装成 API：

- OpenAI 兼容接口
- 简单聊天接口
- 图片生成接口
- 读取网页侧最近对话
- 检查登录状态

本轮新增目标是把它从“单人单会话工具”扩展为更接近可供局域网多人调用的后端服务：

- 支持用户上传文件
- 支持返回可点击下载链接
- 支持随机新建内部会话
- 支持数据库临时保存会话 id、消息、文件记录
- 支持最多 3 个用户或 3 个会话并行提问
- 尽量通过标准 API 访问，而不是只依赖前端

当前主访问地址：

```text
http://192.168.8.222:5061
```

当前前端测试地址另有一个项目 `agent-console`，运行在：

```text
http://192.168.8.222:5070
```

本文主要说明 `CatGPT-Gateway` 后端。

## 2. 当前已经实现的内容

### 2.1 新增数据库

新增 SQLite 存储层：

```text
src/storage/database.py
src/storage/__init__.py
```

数据库默认位置：

```text
data/catgpt_gateway.db
```

相关配置在：

```text
src/config.py
```

数据库表：

- `sessions`：内部会话，保存 owner、provider、网页侧真实 thread id、标题、状态、时间
- `messages`：内部消息记录，保存 session、role、content、时间
- `files`：文件记录，保存 owner、session、来源、文件名、本地路径、mime、大小、sha、过期时间

数据库启用了 SQLite WAL，适合当前这种轻量本地服务。

### 2.2 新增多用户 Token 配置

新增环境变量：

```text
API_USER_TOKENS=user1:replace-with-secret,user2:replace-with-secret,user3:replace-with-secret
```

逻辑在：

```text
src/config.py
src/api/server.py
```

鉴权规则：

- `Authorization: Bearer <token>`
- 老的 `API_TOKEN` 仍然可用，对应 owner 为 `default`
- `API_USER_TOKENS` 里的 token 会映射成不同 owner，例如 `user1`、`user2`、`user3`
- 新增的 session、message、file API 都按 owner 隔离

注意：真实 token 已写入本地 `.env`，交接时不要把 `.env` 提交到公开仓库。

### 2.3 新增 3 并发浏览器 worker 池

新增文件：

```text
src/api/session_service.py
```

核心类：

```text
SessionWorkerPool
```

相关配置：

```text
MAX_CONCURRENT_SESSIONS=3
```

实现方式：

- 服务启动时在同一个 Playwright persistent browser context 里创建 3 个独立 page
- 每个 page 绑定一个 `ChatGPTClient` 或 `ClaudeClient`
- 新的 `/v1/sessions/{session_id}/messages` 请求会进入 worker queue
- 同一个 session 内部有 session lock，保证同一个会话内消息顺序执行
- 不同 session 可以分配到不同 page 并发执行

当前已验证：

```text
GET /v1/pool/status
```

返回类似：

```json
{
  "capacity": 3,
  "available": 3,
  "busy": 0
}
```

### 2.4 修复并发串话问题

原来的响应提取逻辑会使用系统剪贴板。多个 Playwright page 并发时，共用系统剪贴板会造成串话。

已修改：

```text
src/chatgpt/detector.py
src/chatgpt/client.py
src/claude/detector.py
src/claude/client.py
```

关键变化：

- `extract_last_response_via_copy(..., use_clipboard=True)` 新增参数
- 旧接口默认仍使用剪贴板，保持兼容
- 新的 worker pool 创建 client 时使用 `use_clipboard=False`
- 新并发接口改为 page-local DOM 提取，避免 3 个 page 抢同一个剪贴板

### 2.5 新增文件上传、下载、文件记录 API

新增文件：

```text
src/api/managed_routes.py
```

新增接口：

```text
GET    /v1/me
GET    /v1/pool/status
POST   /v1/sessions
GET    /v1/sessions
GET    /v1/sessions/{session_id}
DELETE /v1/sessions/{session_id}
GET    /v1/sessions/{session_id}/messages
POST   /v1/sessions/{session_id}/messages
POST   /v1/files
GET    /v1/files
GET    /v1/files/{file_id}
DELETE /v1/files/{file_id}
GET    /v1/files/{file_id}/content
```

文件存储位置：

```text
data/files/
```

相关配置：

```text
MAX_FILE_SIZE_MB=25
FILE_TTL_HOURS=72
DOWNLOAD_SECRET=replace-with-secret
```

下载链接实现：

- 上传或生成文件后，API 会返回 `download_url`
- `download_url` 带 `expires` 和 `signature`
- 签名基于 `DOWNLOAD_SECRET`
- 默认签名链接有效期 10 分钟
- 也可以用 Bearer Token 访问 `/v1/files/{file_id}/content`

### 2.6 新增生成文件捕获逻辑

新增文件：

```text
src/files/generated.py
src/files/__init__.py
```

作用：

- 在 ChatGPT 网页回复完成后，尝试扫描最新 assistant 区域中的下载链接
- 对图片生成结果，会把本地下载或缓存到的文件复制到受管理目录
- 生成文件会写入 `files` 表，并在接口响应中返回可下载链接

注意：这部分是 best-effort。ChatGPT 网页 DOM 变化会影响捕获准确性，后续需要按实际 UI 继续加固。

### 2.7 修复 ChatGPT 网页真实附件上传（第二轮）

第一轮遗留的最重要问题「附件没有真正送达 ChatGPT」已定位并修复，且经过真实端到端验证（ChatGPT 能读取并回显附件内容）。

根因：ChatGPT composer 里实际有 **3 个** `<input type="file">`：

```text
input#upload-files    通用（无 accept 限制，位于 composer form 内）← 文档类必须用这个
input#upload-photos   accept="image/*"，仅图片
input#upload-camera   accept="image/*" capture，仅拍照
```

旧的选择器顺序把 `input#upload-photos` 排在最前，非图片文件被设置到仅图片 input 上，`set_input_files` 不报错但 ChatGPT 完全忽略，也没有附件 chip 出现。

修复内容（`src/selectors.py`、`src/chatgpt/client.py`）：

- `FILE_UPLOAD_INPUT` 顺序改为 `input#upload-files` 优先
- `_find_file_input(skip_image_only=...)`：上传非图片文件时自动跳过 accept 仅图片的 input
- `_wait_for_attachment(...)`：不再猜 chip class，改为全页扫描文件名（innerText/aria-label/title/alt）确认附件已暂存，并识别上传失败 toast
- 附件暂存后等待 2 秒让 composer 完成上传处理——立即输入文本会被静默丢弃（第一轮 12 秒的超时等待掩盖了这个问题）
- `send_message` 增加输入校验：输入后检查 composer 是否有文本，为空则重试插入；发送按钮 disabled 时重插文本并重试点击
- 新增 `scripts/diagnose_upload.py`：停服后可单独运行，dump composer 的文件 input / 附件按钮 / 菜单结构，方便下次 DOM 变化时快速定位

### 2.8 /docs 中文文档与 Bearer Authorize（第二轮）

- `server.py` 的 FastAPI 应用改为中文标题/描述，并通过 `openapi_tags` 把接口分为「多用户会话与文件」「OpenAI 兼容接口」「旧版兼容接口」三组
- 所有 managed 接口补充中文 `summary`/`description`，request model 字段加中文说明
- 通过 `HTTPBearer(auto_error=False)` 安全方案，`/docs` 右上角出现 **Authorize** 按钮，填 token 即可在线调试（实际鉴权仍由 BearerTokenMiddleware 完成）
- `GET /v1/me` 增加返回 `file_usage`（已用字节、文件数、配额）

### 2.9 /v1/chat/completions 接入会话池（第二轮）

`POST /v1/chat/completions` 可选走新的 session worker 池：

- 请求体传 `session_id`（网关扩展字段，OpenAI SDK 用 `extra_body` 传）→ 使用指定会话，404 校验归属
- 不传 `session_id` 但 `.env` 设 `OPENAI_USE_SESSION_POOL=true` → 自动创建临时会话
- 两种情况都会：持久化 user/assistant 消息、回显实际 `session_id`、捕获生成文件并把下载链接附在回复末尾（计入配额）
- 不满足以上条件时完全沿用原单页逻辑，保持兼容

### 2.10 配额、限制与后台清理（第二轮）

- `USER_FILE_QUOTA_MB`（默认 500）：每用户磁盘配额，上传超额返回 507；生成文件超配额时静默丢弃不阻断消息
- `MAX_FILES_PER_SESSION`（默认 20）：单会话文件数上限，超额返回 409
- `MAX_FILE_SIZE_MB`：单文件大小上限 413（原有）
- `FILE_CLEANUP_INTERVAL_MINUTES`（默认 10）：后台任务周期性清理过期文件（记录 + 磁盘字节 + 空目录），见 `src/files/cleanup.py`；启动时仍会先清理一次
- `Database` 新增 `owner_usage(owner)` 与 `session_file_count(session, owner)` 查询

### 2.11 自动化测试（第二轮）

新增 `tests/` 套件（pytest，36 个用例，全部通过，无需浏览器）：

```text
tests/conftest.py                 隔离配置/临时数据库/StubPool/TestClient fixtures
tests/test_database.py            会话/消息/文件 CRUD、隔离、用量、过期清理
tests/test_managed_api.py         鉴权、会话生命周期、多用户隔离、上传下载、
                                  签名链接、413/409/507 限制
tests/test_openai_session_pool.py session_id 路由、临时会话、归属校验
tests/test_cleanup_task.py        后台清理任务启停
```

运行：`.venv/Scripts/python.exe -m pytest`（pytest、pytest-asyncio 已加入 requirements.txt）。

### 2.12 运行时健壮性修复（第二轮，冒烟测试发现）

用 `scripts/smoke_test.py` 对真实服务做全接口冒烟时发现并修复两个问题：

1. **worker 池死页面不自愈**：某个池页面被关闭（标签崩溃/被手动关掉）后，
   分到它的请求会一直 500，直到重启服务。
   修复（`src/api/session_service.py`）：`SessionWorkerPool` 记录 BrowserManager，
   新增 `_spawn_worker` / `_ensure_alive` / `_looks_dead`；取到 worker 先检查页面，
   死了就重建；请求中途因页面死亡失败时重建页面并重试一次。

2. **ChatGPT 生成卡死时检测过慢**：后端流挂起（回复文本已出但 stop 按钮不消失、
   copy 按钮不出现）时，原检测器要等满 120s+120s 才落到文本稳定兜底，导致 HTTP 超时。
   修复（`src/chatgpt/detector.py`）：快照新增 `hasStopButton`；
   `_wait_for_copy_button_or_image` 增加 stall 检测（新回合文本稳定 20s 且 stop 按钮
   仍在 → 返回 `stall`）；`wait_for_response_complete` 对 `stall` 点击 stop 释放页面后
   视为完成；stop 按钮策略上限收紧到 45s。

冒烟脚本用法（对正在运行的服务，含真实消息验证）：

```bash
PYTHONPATH=. .venv/Scripts/python.exe scripts/smoke_test.py \
  [--base URL] [--token TOKEN] [--token2 TOKEN] [--no-live]
```

`--token2` 启用多用户隔离检查；`--no-live` 跳过会真实发消息的两项。

## 3. 新 API 使用流程

### 3.1 查看当前调用者

```bash
curl -H "Authorization: Bearer dummy123" \
  http://192.168.8.222:5061/v1/me
```

返回：

```json
{
  "owner_id": "default",
  "max_concurrent_sessions": 3
}
```

### 3.2 新建会话

```bash
curl -X POST \
  -H "Authorization: Bearer dummy123" \
  -H "Content-Type: application/json" \
  -d "{\"title\":\"测试会话\"}" \
  http://192.168.8.222:5061/v1/sessions
```

返回里会有内部会话 id，例如：

```json
{
  "id": "ses_xxx",
  "owner_id": "default",
  "provider": "chatgpt",
  "provider_thread_id": null,
  "title": "测试会话",
  "status": "new"
}
```

第一次向这个 session 发消息后，会自动新建 ChatGPT 网页侧会话，并把真实 thread id 写回 `provider_thread_id`。

### 3.3 上传文件

```bash
curl -X POST \
  -H "Authorization: Bearer dummy123" \
  -F "file=@README.md" \
  -F "session_id=ses_xxx" \
  http://192.168.8.222:5061/v1/files
```

返回：

```json
{
  "id": "file_xxx",
  "object": "file",
  "session_id": "ses_xxx",
  "source": "upload",
  "filename": "README.md",
  "download_url": "http://192.168.8.222:5061/v1/files/file_xxx/content?expires=...&signature=..."
}
```

### 3.4 发送带文件的消息

```bash
curl -X POST \
  -H "Authorization: Bearer dummy123" \
  -H "Content-Type: application/json" \
  -d "{\"content\":\"请读取这个文件并总结重点\",\"file_ids\":[\"file_xxx\"]}" \
  http://192.168.8.222:5061/v1/sessions/ses_xxx/messages
```

当前实现会把 `file_ids` 对应到本地文件路径，然后传给 `ChatGPTClient.send_message(..., file_paths=...)`。

如果网页端上传控件适配成功，ChatGPT 会真正收到附件；如果上传控件失效，则后续需要继续完善 Playwright 文件上传逻辑。

### 3.5 查看会话消息

```bash
curl -H "Authorization: Bearer dummy123" \
  http://192.168.8.222:5061/v1/sessions/ses_xxx/messages
```

### 3.6 下载文件

方式一：直接打开 `download_url`。

方式二：Bearer Token 下载：

```bash
curl -L \
  -H "Authorization: Bearer dummy123" \
  -o output.bin \
  http://192.168.8.222:5061/v1/files/file_xxx/content
```

## 4. 当前仍然保留的旧接口

旧接口仍然存在：

```text
GET  /v1/models
POST /v1/chat/completions
POST /v1/images/generations
POST /chat
POST /thread/new
GET  /threads
GET  /status
GET  /docs
```

其中：

- `/v1/models`：OpenAI 风格模型列表
- `/v1/chat/completions`：OpenAI 风格聊天
- `/v1/images/generations`：通过 ChatGPT 网页生成图片
- `/chat`：项目自定义简单聊天
- `/thread/new`：网页侧新建对话并发送消息
- `/threads`：读取网页侧最近对话
- `/status`：检查网页登录状态
- `/docs`：FastAPI 在线接口文档

注意：旧接口主要沿用原来的单页逻辑，不是新的多人会话主入口。

后续开发建议：

- 多用户、多会话、多并发场景统一使用新增的 `/v1/sessions` 和 `/v1/files`
- 旧接口保留做兼容
- 如果需要 OpenAI SDK 兼容又要多用户隔离，可以后续把 `/v1/chat/completions` 映射到新的 session worker pool

## 5. 当前已验证结果

已经做过的验证：

- Python 编译检查通过
- `python-multipart` 已安装，用于 FastAPI 文件上传
- 服务可启动
- `GET /healthz` 返回 `{"status":"ok"}`
- `GET /v1/pool/status` 返回容量 3
- 新建 session 成功
- 上传文件成功
- 下载文件成功，下载字节数与上传一致
- 发送带 file id 的消息成功
- 会话会写入真实 ChatGPT thread id
- 3 个 session 并发发送消息成功，实际能同时跑
- 多用户隔离验证通过：其他用户 token 访问不到不属于自己的 session
- 错误 token 返回 401

第二轮新增验证（2026-08-07）：

- pytest 套件 36 个用例全部通过（数据库、鉴权、文件上传下载、会话隔离、配额、会话池路由、清理任务）
- 真实附件上传端到端通过：上传含唯一 marker 的 txt → 发消息引用 → ChatGPT 原样回显 marker（修复前 ChatGPT 回复「看不到附件」）
- 附件暂存确认日志出现 `Attachment staged in composer — upload verified`
- `POST /v1/chat/completions` 带 `session_id` 实测走会话池：响应回显 session_id、消息持久化、正常返回回复
- `/openapi.json` 含 `HTTPBearer` 安全方案（/docs 出现 Authorize 按钮）、中文 tag 与 summary
- 服务启动日志确认后台清理任务启动（interval=10 min）

当前服务进程：

```text
5061 端口有 python 进程监听
```

注意：服务当前主要绑定在局域网地址 `192.168.8.222:5061`，`127.0.0.1:5061` 未必可访问。

## 6. 还没完全实现或需要继续加固的内容

> 第二轮状态：6.1 / 6.2 / 6.3 已完成（见 2.7–2.9），6.5 大部分完成（见 2.10）。
> 剩余主要是 6.4 的更多真实场景测试与 6.6 的账号级隔离。

### 6.1 真正上传文件到 ChatGPT 网页控件【已修复，见 2.7】

（历史描述，问题已定位并修复，保留供参考。）现在新增 API 已经能：

- 接收文件
- 保存文件
- 记录数据库
- 把文件路径传入 `ChatGPTClient.send_message`

但要确认 ChatGPT 网页端真的接收附件，需要检查：

```text
src/chatgpt/client.py
```

重点搜索：

```text
send_message
file_paths
set_input_files
input[type=file]
```

如果当前 ChatGPT 网页 DOM 变化，文件上传可能需要重新适配：

- 找到真实文件 input
- 或点击附件按钮后再设置文件
- 等待上传完成
- 处理上传失败提示
- 把上传结果与 message 绑定

第二轮已修复并真实验证通过，详见 2.7。

### 6.2 `/docs` 中文说明与 Bearer Authorize【已完成，见 2.8】

（历史描述，保留供参考。）FastAPI 自动文档能看到接口，但目前还不够适合外部调用者。

建议后续：

- 给新接口补 `summary`、`description`
- 给 request/response model 补字段说明
- 增加 Bearer Token security scheme，让 `/docs` 里可以点 Authorize
- 在 README 里加入完整 API 流程

### 6.3 旧 OpenAI 兼容接口接入新 session pool【已完成，见 2.9】

（历史描述，保留供参考。）当前：

```text
POST /v1/chat/completions
```

仍主要是原逻辑。

如果希望 OpenAI SDK 用户也自动获得：

- 用户隔离
- 数据库存储
- 3 并发
- 文件记录

需要把 `/v1/chat/completions` 改成使用新 session pool。可选方案：

- 如果请求里传了 `session_id`，使用指定 session
- 如果没传，自动新建临时 session
- 把 OpenAI messages 合并成一次 prompt
- 返回 OpenAI Chat Completions 格式

### 6.4 生成文件捕获需要更多真实场景测试

当前 `src/files/generated.py` 是通用 best-effort 实现。

需要继续测试：

- ChatGPT 图片生成
- ChatGPT 生成 CSV / DOCX / XLSX / ZIP
- 链接在 sandbox 域名时的下载
- 浏览器触发 `expect_download` 的场景
- 生成文件名重复时的处理

### 6.5 文件清理与配额【大部分完成，见 2.10】

第二轮已实现：后台定时清理任务、每用户磁盘 quota、每 session 文件数量限制。

当前支持：

- 文件过期时间
- 服务启动时清理过期文件
- 后台定时清理任务（FILE_CLEANUP_INTERVAL_MINUTES）
- 每用户磁盘 quota（USER_FILE_QUOTA_MB）
- 每 session 文件数量限制（MAX_FILES_PER_SESSION）
- 手动删除文件

仍可选加固：

- mime allowlist / blocklist
- 病毒扫描或安全检查

### 6.6 多用户仍共享同一个 ChatGPT 网页账号

当前所谓多用户是 API 层用户隔离，不是 ChatGPT 网页账号隔离。

也就是说：

- user1、user2、user3 在 API 和数据库层互相隔离
- 但底层仍共用同一个浏览器登录态
- ChatGPT 官网侧仍可能看到同一账号下的所有对话

如果要做到真正账号级隔离，需要：

- 每个 owner 一个独立 browser profile
- 每个 owner 独立登录 ChatGPT
- 每个 owner 独立 worker pool 或共享调度器

当前版本适合局域网内少量可信用户共享一个 ChatGPT 账号，不适合开放给不可信用户。

## 7. 核心文件清单

新增或重点修改文件：

```text
src/config.py
src/api/server.py
src/api/managed_routes.py
src/api/session_service.py
src/api/openai_routes.py        # 第二轮：session_id 会话池分支
src/api/openai_schemas.py       # 第二轮：session_id 扩展字段
src/storage/database.py
src/storage/__init__.py
src/files/generated.py
src/files/cleanup.py            # 第二轮：后台过期清理任务
src/files/__init__.py
src/chatgpt/client.py
src/chatgpt/detector.py
src/claude/client.py
src/claude/detector.py
src/selectors.py                # 第二轮：修正文件 input 选择器顺序
scripts/diagnose_upload.py      # 第二轮：composer 上传结构诊断脚本
tests/                          # 第二轮：pytest 自动化测试套件
requirements.txt
.env.example
.gitignore
```

运行时文件：

```text
.env
data/catgpt_gateway.db
data/files/
browser_data/
downloads/
logs/
```

注意：

- `.env`、`data/`、`browser_data/` 不应提交公开仓库
- `browser_data/` 保存浏览器登录态
- `data/files/` 保存上传和生成文件
- `data/catgpt_gateway.db` 保存临时业务数据

## 8. 环境变量说明

关键环境变量：

```text
API_TOKEN=dummy123
API_USER_TOKENS=user1:secret,user2:secret,user3:secret
DOWNLOAD_SECRET=secret-for-signed-download-url
MAX_CONCURRENT_SESSIONS=3
MAX_FILE_SIZE_MB=25
FILE_TTL_HOURS=72
USER_FILE_QUOTA_MB=500
MAX_FILES_PER_SESSION=20
FILE_CLEANUP_INTERVAL_MINUTES=10
OPENAI_USE_SESSION_POOL=false
API_HOST=192.168.8.222
API_PORT=5061
PROVIDER=chatgpt
```

说明：

- `API_TOKEN` 是兼容旧单 token 用法
- `API_USER_TOKENS` 用于多人隔离
- `DOWNLOAD_SECRET` 用于签名下载链接
- `MAX_CONCURRENT_SESSIONS` 控制新 session API 的并发 worker 数
- `MAX_FILE_SIZE_MB` 控制上传大小
- `FILE_TTL_HOURS` 控制文件默认过期时间
- `USER_FILE_QUOTA_MB` 每用户磁盘配额（第二轮）
- `MAX_FILES_PER_SESSION` 单会话文件数上限（第二轮）
- `FILE_CLEANUP_INTERVAL_MINUTES` 后台清理周期（第二轮）
- `OPENAI_USE_SESSION_POOL` 无 session_id 时是否也走会话池（第二轮）

## 9. 建议后续开发顺序

第一轮列的 1–5 项（真实附件上传、/docs、OpenAI 接入会话池、清理与配额、自动化测试）在第二轮均已完成。接下来建议：

1. 对生成文件捕获做更多真实场景测试（6.4）：图片、CSV/DOCX/XLSX/ZIP、sandbox 域名下载、`expect_download`、重名处理。
2. 如要开放给非可信用户，做账号级隔离（6.6）：每 owner 独立 browser profile + 独立登录 + 独立/共享 worker 调度。
3. 可选加固：mime allowlist/blocklist、病毒扫描（6.5 剩余）。

持续约定：

- 任何涉及 ChatGPT DOM 的改动后，先用 `scripts/diagnose_upload.py` 观察结构，再用真实浏览器跑一遍 `/v1/sessions/{id}/messages`。
- 改动存储/鉴权/文件逻辑后，跑 `.venv/Scripts/python.exe -m pytest` 回归。

## 10. 给后续 AI 的注意事项

继续开发时请先读这些文件：

```text
src/api/server.py
src/api/managed_routes.py
src/api/session_service.py
src/api/openai_routes.py
src/storage/database.py
src/files/cleanup.py
src/chatgpt/client.py
src/chatgpt/detector.py
src/selectors.py
src/config.py
```

不要直接重构旧接口。这个项目依赖 Playwright 操作真实 ChatGPT 网页，很多逻辑和 DOM 强相关，过度重构容易破坏已可用功能。

推荐策略：

- 先补测试
- 再局部修文件上传
- 再接入 OpenAI 兼容接口
- 每改一次都用真实浏览器跑一遍 `/v1/sessions/{id}/messages`

当前最关键的边界：

- 新 session API 是主要多人入口
- 旧接口是兼容入口
- API 用户隔离不等于 ChatGPT 账号隔离
- 3 并发只保证新 session worker pool 内部并发


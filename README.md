# xu-agent — 个人智能助手

基于 LangGraph 的全栈智能助手，支持工具调用、知识检索、定时任务、飞书 IM 等主流 Agent 能力。

## 功能

### 核心

- **流式对话** — SSE 实时推送，前端 throttle + React.memo 优化，流畅无卡顿
- **25+ 内置工具** — 文件读写/编辑、Shell 执行、SSH 远程命令、Glob 搜索、后台任务等
- **MCP 外部工具** — HTTP 协议动态加载外部 MCP Server 工具（如 GitHub Copilot MCP）
- **技能系统** — SKILL.md 动态加载，支持 PDF 处理、视频下载等扩展技能

### 上下文管理

- **Tool Result Pruning** — 自动缩短旧工具结果、重复输出和大型调用参数，不删除用户与助手对话
- **Summary Compact** — 接近 token 上限时自动调用 LLM 生成结构化摘要，压缩历史上下文
- **技术说明** — [Qingzhou Agent 上下文压缩技术说明](docs/qingzhou-context-compression.md)

### 知识与记忆

- **本地 RAG** — LlamaIndex 混合检索（规则匹配 + 向量相似 + BM25 + RRF 融合排序），支持 PDF/Markdown/Word 等文档
- **持久记忆** — 文件式记忆系统，关键词匹配注入，支持用户偏好/项目经验/反馈记录

### 任务与调度

- **持久任务图** — blockedBy 依赖关系，自动解锁，跨会话追踪任务进度
- **定时任务** — 会话级 Cron 调度，支持周期/一次性触发，到点自动注入 prompt 并执行
- **子 Agent** — 独立线程隔离执行子任务，不干扰主对话流

### IM 集成

- **飞书** — WebSocket 长连接接收消息，自动回复，消息去重，长文本分段发送

### 安全与可观测

- **权限 Guardrail** — 三级 allow/ask/deny + interrupt 人机审批，防止危险操作
- **LLM 恢复** — 529 重试 + fallback 模型降级，应对 API 服务端故障
- **结构化日志** — JSONL 格式记录全链路事件（模型调用、工具执行、上下文压缩等）

### 前端

- **文件上传/下载** — 拖拽上传文件，生成结果文件可点击下载（含中文文件名支持）
- **插件面板** — 可视化展示已配置的 MCP 插件
- **技能面板** — 展示可用技能卡片
- **配置面板** — UI 管理 LLM 和 SSH 配置

## 快速开始

### 1. 启动后端

```bash
cd backend

# 从模板创建 .env 并填入 API Key
cp .env.example .env

# 安装依赖
pip install -r requirements.txt

# 启动 LangGraph 开发服务器（端口 2024）
langgraph dev
```

### 2. 启动前端

```bash
cd web

# 从模板创建 .env
cp .env.example .env

# 安装依赖
pnpm install

# 启动开发服务器
pnpm dev
```

浏览器打开 `http://localhost:3000`。

## 配置

所有配置通过环境变量管理（完整列表见 `.env.example`）：

| 类别 | 关键变量 |
|------|----------|
| LLM | `LLM_ADAPTER_TYPE`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_AUTH_TOKEN`（Anthropic Bearer 认证）, `LLM_BASE_URL` |
| 上下文 | `AGENT_TOOL_RESULT_PRUNE_ENABLED`, `AGENT_CONTEXT_WINDOW` |
| RAG | `RAG_EMBEDDING_PROVIDER`, `RAG_EMBEDDING_MODEL`, `RAG_EMBEDDING_API_KEY` / `RAG_EMBEDDING_AUTH_TOKEN`, `RAG_EMBEDDING_BASE_URL`, `RAG_DOCS_DIR` |
| 定时 | `AGENT_CRON_ENABLED`, `AGENT_CRON_POLL_SECONDS` |
| 飞书 | `LARK_WS_ENABLED`, `LARK_APP_ID`, `LARK_APP_SECRET`, `LARK_MARKDOWN_ENABLED` |
| QQ 机器人 | `BOTPY_ENABLED`, `BOTPY_APPID`, `BOTPY_SECRET`, `BOTPY_SANDBOX` |
| MCP | `AGENT_MCP_CONFIG` |
| 记忆 | `AGENT_MEMORY_ENABLED` |

LLM 和 SSH 配置也可通过前端配置面板管理。

## 斜杠命令

- `/help`：查看当前支持的斜杠命令。
- `/clear`：复用当前 thread，清除消息、token 使用量和上下文压缩状态；支持自有 UI 与飞书，不会交给模型执行。
- `/compact [focus]`：手动触发当前 thread 的上下文压缩；可选 `focus` 用来提示摘要时优先保留哪些信息。手动压缩默认尽量把历史消息都压入摘要，不沿用自动压缩保留最近 20 条的策略；如需保留最近原文，可设置 `AGENT_MANUAL_COMPACT_KEEP_MESSAGES`。

## 扩展

- **新增工具**：在 `tools/registry.py` 中定义，在 `agent/graph.py` 中导入
- **新增技能**：创建 `skills/<名称>/SKILL.md`（含 YAML frontmatter）
- **新增 MCP 服务器**：在 `config/xu-agent.json` 的 `mcp` 字段中配置
- **切换模型**：设置 `LLM_ADAPTER_TYPE`（anthropic/openai）和 `LLM_MODEL`

# 可选 Playwright 浏览器工具

后端可以按需向模型注入 Playwright 浏览器工具。该能力默认关闭，因此不会占用日常
对话的工具上下文。

在项目根目录 `.env` 中启用：

```env
AGENT_PLAYWRIGHT_ENABLED=true
AGENT_PLAYWRIGHT_BROWSER=chromium
# 可选：使用本机浏览器，填 chrome 或 msedge；留空则使用 Playwright 下载的 Chromium
AGENT_PLAYWRIGHT_CHANNEL=
AGENT_PLAYWRIGHT_AUTO_DETECT_CHANNEL=true
AGENT_PLAYWRIGHT_HEADLESS=true
AGENT_PLAYWRIGHT_TIMEOUT_MS=30000
```

首次使用前安装 Python 包和浏览器：

```powershell
cd backend
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

Windows 默认会在 channel 留空时依次探测系统 Edge 和 Chrome；也可以显式设置
`AGENT_PLAYWRIGHT_CHANNEL=msedge` 或 `chrome`。使用系统浏览器时可以跳过浏览器下载命令。
修改环境变量后需要重启后端。启用后会注入 `playwright_open`、
`playwright_snapshot`、`playwright_click`、`playwright_type`、
`playwright_press`、`playwright_scroll`、`playwright_screenshot` 和
`playwright_close`。浏览器会话按 LangGraph thread 隔离，截图只能写入
项目工作目录内。导航和交互操作会经过权限审批。

# xu-agent — 个人智能助手

基于 LangGraph 的全栈智能助手，支持工具调用、知识检索、定时任务、飞书 IM 等主流 Agent 能力。

## 功能

### 核心

- **流式对话** — SSE 实时推送，前端 throttle + React.memo 优化，流畅无卡顿
- **25+ 内置工具** — 文件读写/编辑、Shell 执行、SSH 远程命令、Glob 搜索、后台任务等
- **MCP 外部工具** — HTTP 协议动态加载外部 MCP Server 工具（如 GitHub Copilot MCP）
- **技能系统** — SKILL.md 动态加载，支持 PDF 处理、视频下载等扩展技能

### 上下文管理

- **Snip Compact** — 消息数超阈值时轻裁中间旧对话，保留首尾关键消息，保护 tool_call/tool_result 配对完整性
- **Summary Compact** — 接近 token 上限时自动调用 LLM 生成结构化摘要，压缩历史上下文

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
cd frontend

# 从模板创建 .env
cp .env.example .env

# 安装依赖
pnpm install

# 启动开发服务器
pnpm dev
```

浏览器打开 `http://localhost:3000`。

## 配置

所有配置通过环境变量管理（完整列表见 `backend/.env.example`）：

| 类别 | 关键变量 |
|------|----------|
| LLM | `LLM_ADAPTER_TYPE`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL` |
| 上下文 | `AGENT_SNIP_TRIGGER_MESSAGES`, `AGENT_CONTEXT_WINDOW` |
| RAG | `RAG_EMBEDDING_PROVIDER`, `RAG_DOCS_DIR` |
| 定时 | `AGENT_CRON_ENABLED`, `AGENT_CRON_POLL_SECONDS` |
| 飞书 | `LARK_WS_ENABLED`, `LARK_APP_ID`, `LARK_APP_SECRET` |
| MCP | `AGENT_MCP_CONFIG` |
| 记忆 | `AGENT_MEMORY_ENABLED` |

LLM 和 SSH 配置也可通过前端配置面板管理。

## 扩展

- **新增工具**：在 `backend/src/tools.py` 中定义，在 `agent.py` 中导入
- **新增技能**：创建 `skills/<名称>/SKILL.md`（含 YAML frontmatter）
- **新增 MCP 服务器**：在 `backend/.mcp.json` 中配置
- **切换模型**：设置 `LLM_ADAPTER_TYPE`（anthropic/openai）和 `LLM_MODEL`

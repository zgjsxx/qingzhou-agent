# xu-agent v1.0.0 Release Note

## 概述

xu-agent 是一个基于 LangGraph 的个人 AI 助手，由 Python 后端（LangGraph Server）和 Next.js 前端组成。1.0.0 版本标志着项目达到可用状态，具备完整的对话、工具调用、权限审批、记忆持久化和上下文压缩能力。

---

## 核心功能

### 🤖 对话与工具调用

- **15 个工具**：文件读写（read_file / write_file / edit_file / glob）、Shell 执行（run_shell_command）、SSH 远程执行（run_ssh_command）、后台任务（list/get background tasks）、CPU 监控（get_system_cpu_usage）
- **多步骤规划**：todo_write 工具支持任务拆分与进度追踪
- **持久化任务**：create_task / list_tasks / get_task / claim_task / complete_task，支持 blockedBy 依赖链
- **Skill 按需加载**：启动时注入目录摘要，通过 load_skill 按需获取完整内容，节省上下文空间
- **多 Shell 支持**：auto / powershell / cmd / bash / sh，自动注入 UTF-8 preamble 解决 Windows 中文乱码

### 🔐 权限与审批（Human-in-the-loop）

- **三级权限**：allow / ask / deny
- **硬拒绝规则**：rm -rf /、sudo、shutdown、reboot 等危险命令直接拦截
- **需审批规则**：rm、pip install、写入 /etc 等操作需用户确认
- **前端审批 UI**：Agent Inbox 面板，支持逐条或一键审批，状态/描述切换面板

### 🧠 记忆系统

- **四种类型**：user / feedback / project / reference
- **Markdown + YAML frontmatter** 格式存储
- **按需注入**：关键词匹配选取相关记忆，注入到 human message 和 system prompt
- **目录索引**：MEMORY.md 列出所有记忆条目

### 📦 上下文压缩

- **自动压缩**：接近上下文窗口上限时自动触发
- **结构化摘要**：将旧消息压缩为 `<analysis>/<summary>` 格式
- **前端显示**：context_usage 实时展示 token 数、消息数、工具标记
- **可配置**：压缩阈值、保留消息数、失败上限等参数均可调

### 🔧 LLM 多供应商支持

- **Anthropic** 与 **OpenAI** 适配器，通过 `LLM_ADAPTER_TYPE` 切换
- **独立摘要模型**：AGENT_SUMMARY_LLM_MODEL 可单独配置压缩/摘要用的模型
- **故障恢复**：529 错误自动重试 + fallback 模型降级

### 📡 SSH 远程执行

- 系统 SSH 客户端调用，支持 host / user / port / key_file / timeout
- 缺省参数自动回退到 `.agent_config.json` 中保存的 SSH 配置
- `BatchMode=yes` + `StrictHostKeyChecking=accept-new` 确保非交互运行

---

## 前端功能

### 💬 对话界面

- 流式消息渲染，Markdown + GFM + KaTeX 数学公式 + 代码语法高亮
- 工具调用参数表格展示，大内容可折叠/展开
- 消息编辑、重新提交、分支切换
- 人机审批面板（Agent Inbox）

### 📎 文件上传

- 支持图片（JPEG/PNG/GIF/WebP）和 PDF
- 拖拽、粘贴、文件选择器三种上传方式
- 图片 base64 内联，PDF 上传后端后引用

### ⚙️ 设置面板

- LLM 配置（适配器、模型、Base URL、API Key）
- SSH 配置（Host、Port、User、Key）
- Skill 目录浏览
- 配置持久化到 `.agent_config.json`

### 🪄 侧面板（Artifact）

- 自定义 UI 组件可通过 React Portal 在侧面板渲染
- 上下文信息可传入模型提交，实现交互式操作

### ⚡ 性能优化

- Stream throttle（50ms / ~20fps），避免 SSE 高频更新阻塞主线程
- AssistantMessage React.memo + 自定义 comparator，已完成消息跳过 re-render
- ToolResult 折叠态纯字符串预览，展开态才做 JSON.parse/stringify
- framer-motion height:auto 动画移除，减少布局重排

---

## Skills

| Skill | 说明 |
|-------|------|
| youtube-downloader | yt-dlp 视频下载，支持多分辨率 / 格式 / 仅音频 |
| pdf | PDF 读取、生成、审阅，基于 reportlab + pdfplumber + pypdf |

---

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.11+ / LangGraph / LangChain |
| 前端 | React 19 / Next.js 15.5 / TypeScript / Tailwind CSS |
| 通信 | LangGraph SDK SSE 流式传输 |
| 包管理 | pnpm（前端）/ pip（后端） |

---

## 环境变量速览

- `LLM_ADAPTER_TYPE`：anthropic / openai
- `LLM_MODEL`：模型名称（默认 glm-5.1）
- `LLM_API_KEY` / `LLM_BASE_URL`：API 认证与地址
- `AGENT_CONTEXT_COMPACT_ENABLED`：上下文压缩开关
- `AGENT_MEMORY_ENABLED`：记忆系统开关
- `AGENT_PERMISSION_ALLOW_ASK_RULES`：权限 ask 规则开关
- `AGENT_LOG_ENABLED`：JSONL 日志开关
- `SHELL_TOOL_MAX_OUTPUT_CHARS`：Shell 输出截断阈值
- 完整变量列表见 `backend/.env.example`

---

## 已知限制

- `task`（子 Agent）工具已实现但当前禁用——同步子 Agent + interrupt 流程调试困难
- todo 和任务状态为内存存储，服务重启后丢失（持久化任务保留在 `.tasks/` 目录）
- 前端基于 langchain-ai/agent-chat-ui 改造，部分 UI 仍保留原项目样式

---

## 贡献者

- 项目架构、后端 Agent、前端改造、性能优化均由项目初始开发完成

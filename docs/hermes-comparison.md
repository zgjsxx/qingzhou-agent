# qingzhou-agent vs hermes-agent 功能对比与未来规划

> 生成日期: 2026-07-14
> qingzhou-agent 版本: v1.2.0
> hermes-agent 版本: v0.18.0

---

## 1. qingzhou-agent 当前功能概览

| 类别 | 功能 |
|------|------|
| **LLM** | 双 provider (Anthropic/OpenAI)，529 重试 + fallback |
| **工具** | 43 内置 + 8 Playwright + MCP 动态加载 |
| **文件操作** | read / write / edit / glob / search |
| **Shell** | 本地命令执行 + 后台任务管理 |
| **SSH** | paramiko 远程命令 + SFTP 上传/下载 |
| **Web** | DuckDuckGo 搜索 + Jina/HTTP 内容提取 |
| **Memory** | `.memory/` Markdown 持久记忆 |
| **RAG** | LlamaIndex 混合检索 (向量 + BM25 + RRF) |
| **Kanban** | SQLite 看板 (create / list / show / comment / claim / complete / block / retry / dispatch) |
| **Cron** | 定时任务调度 (持久化 JSON) |
| **Subagent** | run_subagent / delegate_task 隔离子代理 |
| **Skills** | 3 内置 (matplotlib-charts, pdf, youtube-downloader) |
| **浏览器** | 8 Playwright 工具 (可选开启) |
| **IM 网关** | 飞书 / Telegram / Discord / 微信 / QQ |
| **ACP** | 最小 stdio ACP 适配器 |
| **权限** | 三级 (allow / deny / ask) + 审批缓存 |
| **ASR** | SenseVoice 语音识别 |
| **Web UI** | Next.js 15 聊天 + 看板 + 监控 + 配置面板 |
| **CLI** | 轻量 REPL |

---

## 2. hermes-agent 功能概览

| 类别 | 功能 |
|------|------|
| **LLM** | 30+ provider 插件系统 (OpenRouter / Anthropic / Gemini / DeepSeek / xAI / Ollama 等) |
| **工具** | 40+ 核心 + toolsets 分组配置 |
| **IM 网关** | 22+ 平台 (含 Slack / WhatsApp / Signal / Teams / Email / Matrix / IRC / DingTalk 等) |
| **终端** | 6 后端 (local / Docker / Singularity / Modal / SSH / Daytona) |
| **浏览器** | 3 后端 + 14 工具 + Camofox 反检测 |
| **Web 搜索** | 9 后端 (Exa / Tavily / Brave / SearXNG / xAI / DDGS 等) |
| **图片生成** | 6 provider (FAL / DALL-E / xAI / ComfyUI 等) |
| **视频生成** | FAL / xAI |
| **TTS / STT** | 多 provider (Edge TTS / ElevenLabs / Groq / faster-whisper) + 语音对话模式 |
| **Computer Use** | 桌面操控 (截图 + 鼠标 + 键盘) |
| **Memory** | 多后端 (Honcho / Mem0 / RetainDB / Supermemory) + 自我改进学习图 |
| **Skills** | 100+ skills + Skill Hub + browse / search / install |
| **MCP** | 双向 (server + client) + OAuth + 安全审计 + 目录 |
| **i18n** | 18 语言 |
| **桌面 App** | Electron / Tauri |
| **TUI** | Ink 终端 UI |
| **API Server** | OpenAI 兼容 HTTP API |
| **可观测** | Observer hooks + Middleware + Langfuse / NeMo |
| **安全** | 多层 (审批 / 威胁模式 / URL 安全 / 路径安全 / 容器隔离) |
| **批量** | trajectory 生成 + SWE-bench runner |
| **Curator** | 自主创建和改进 skills |
| **LSP** | Language Server Protocol |
| **Bitwarden** | 密钥管理集成 |
| **凭证池** | 多凭证轮换避免限速 |
| **生活集成** | Spotify / Google Meet / Home Assistant |
| **Pet 系统** | 桌面宠物头像 + gamification |

---

## 3. 功能差距矩阵

| 功能维度 | qingzhou | hermes | 差距 |
|----------|----------|--------|------|
| LLM providers | 2 | 30+ | 大 |
| IM 平台 | 5 | 22+ | 大 |
| 终端后端 | 1 (local) | 6 | 大 |
| 浏览器后端 | 1 (Playwright) | 3 + 反检测 | 中 |
| 搜索后端 | 1 (DDGS) | 9 | 大 |
| 图片生成 | 0 | 6 provider | 全缺 |
| 视频生成 | 0 | 2 provider | 全缺 |
| TTS | 0 | 多 provider | 全缺 |
| Computer Use | 0 | cua-driver | 全缺 |
| Memory 后端 | 1 (文件) | 8+ | 大 |
| Skills 数量 | 3 | 100+ | 极大 |
| Skill 管理 | 手动 | Hub + 安装机制 | 大 |
| MCP | client only | 双向 + OAuth + 安全 | 大 |
| i18n | 无 | 18 语言 | 全缺 |
| 桌面 App | 无 | Electron / Tauri | 全缺 |
| TUI | 无 | Ink | 全缺 |
| API Server | 无 | OpenAI 兼容 | 全缺 |
| 会话搜索 | 无 | FTS5 + LLM 总结 | 全缺 |
| 可观测 | JSONL 日志 | hooks + 中间件 + 外接 | 中 |
| 安全体系 | 三级权限 | 多层 (7+ 子系统) | 大 |
| 批量/训练 | 无 | trajectory + SWE-bench | 全缺 |
| 自我改进 | 无 scaffold | Curator + 学习图 | 全缺 |
| 密钥管理 | env 文件 | Bitwarden + 凭证池 | 大 |
| 生活集成 | 无 | Spotify / Meet / HA | 全缺 |

---

## 4. 未来可新增功能 (按优先级排序)

### P0 — 高价值 + 低难度 (立即做)

#### 4.1 更多 LLM Provider 插件化

当前只有 Anthropic / OpenAI 两种。参考 hermes 的 `plugins/model-providers/` 插件注册机制，扩充：

- **Gemini** (Google AI Studio) — 免费额度大，多模态能力强
- **DeepSeek** — 国内成本低，代码能力强
- **xAI (Grok)** — 实时信息接入
- **Ollama** — 本地模型，零成本私有化
- **OpenRouter** — 聚合网关，一次接入数百模型
- **Azure / AWS Bedrock / Vertex AI** — 企业合规

实现方式：将 `agent/llm_config.py` 的硬编码映射改为动态 provider 注册表，每个 provider 是一个独立模块，提供 `list_models()` + `invoke()` 接口。

参考: `hermes-agent/plugins/model-providers/`

#### 4.2 更多 Web 搜索后端

当前只有 DuckDuckGo (DDGS)。扩充：

- **Tavily** — AI 搜索 API，返回结构化摘要
- **Exa** — 语义搜索，适合研究场景
- **Brave** — 免费层 + 付费层
- **SearXNG** — 自托管 meta-search，完全私有
- **xAI Search** — 实时 X/Twitter 信息

实现方式：参考 hermes 的 `plugins/web/` 注册机制，搜索工具接受 `backend` 参数切换。

参考: `hermes-agent/plugins/web/`

#### 4.3 TTS 语音合成

已有 ASR (SenseVoice) 输入，补上 TTS 输出形成完整语音闭环：

- **Edge TTS** — 免费，中文语音质量好，零成本方案
- **ElevenLabs** — 高质量付费
- **OpenAI TTS** — 付费但稳定
- **MiniMax TTS** — 国内语音

实现方式：新增 `tts` 工具 + Web UI 语音播放按钮 + IM 网关语音消息发送。

参考: `hermes-agent/tools/voice_mode.py`

#### 4.4 图片生成

- **DALL-E (OpenAI)** — 简单稳定
- **FAL.ai** — 聚合多种模型 (Flux / SDXL)
- **xAI** — Grok 图片生成
- **ComfyUI** — 本地 SD 工作流 (skill 模式)

实现方式：新增 `image_generate` 工具，支持 `provider` 参数切换；输出存到 `.agent_outputs/images/`，Web UI 展示。

参考: `hermes-agent/plugins/image_gen/`

#### 4.5 OpenAI 兼容 API Server

让 qingzhou 作为 HTTP API 后端被其他应用调用：

- `/v1/chat/completions` — 兼容 OpenAI SDK
- `/v1/models` — 模型列表
- Agent 工具能力通过 function calling 暴露
- Auth via `API_SERVER_KEY`

实现方式：在 gateway 中新增 `api_server.py` 平台适配器，FastAPI 路由。

参考: `hermes-agent/gateway/platforms/api_server.py`

#### 4.6 i18n 国际化

当前 Web UI 和系统提示仅中文。扩充：

- 中 / 英 / 日 / 韩 / 德 / 法 / 俄 等核心语言
- 系统提示 + Web UI + Skill 描述 三层翻译
- 语言包格式: `locales/{lang}/catalog.json`

参考: `hermes-agent/locales/`, `hermes-agent/agent/i18n.py`

#### 4.7 会话搜索 (Session Search)

搜索历史对话内容：

- **FTS5** 全文索引历史 thread
- **LLM 总结** 生成搜索结果摘要
- Web UI 搜索入口

实现方式：新增 `session_search` 工具，定期对 thread 建索引。

参考: `hermes-agent/tools/session_search.py`

---

### P1 — 高价值 + 中等难度

#### 4.8 Docker 终端后端

沙箱化命令执行，安全隔离：

- 本地 Docker 容器执行命令
- 预配置镜像 (python / node / ubuntu)
- 自动清理

参考: `hermes-agent/tools/environments/docker.py`

#### 4.9 Skill Hub + 更多 Skills

当前仅 3 个 skill，参考 hermes 的 100+ skills 和安装机制：

- **Skill Hub** — GitHub-backed 目录，browse / search / install
- **Skill 版本管理** — 更新 / 回滚
- **社区贡献** — publish 机制
- 扩充 skills: PDF 处理增强 / OCR / 数据分析 / 代码审查 / Git 工作流 / 笔记管理

参考: `hermes-agent/tools/skills_hub.py`, `hermes-agent/skills/`

#### 4.10 Curator — 自主 Skill 创建与改进

Agent 从使用经验中自动提炼 skill：

- 识别重复操作模式 → 生成 skill 草稿
- 使用中收集反馈 → 改进 skill 指令
- 学习图追踪 skill 改进轨迹

参考: `hermes-agent/agent/curator.py`, `hermes-agent/agent/learning_graph.py`

#### 4.11 更多 IM 平台

优先级排序 (按国内 + 国际用户需求):

- **Slack** — 国际企业标配
- **WhatsApp** — 全球最大 IM
- **Signal** — 加密通信
- **Microsoft Teams** — 企业场景
- **Email (IMAP/SMTP)** — 通用通信
- **Matrix** — 开源加密
- **DingTalk** — 国内企业

参考: `hermes-agent/plugins/platforms/`, `hermes-agent/gateway/platforms/`

#### 4.12 MCP Server 模式

当前 qingzhou 只有 MCP client。补充 server 端：

- 把对话暴露为 MCP 工具给外部 agent (Claude Code / Cursor) 用
- 10 工具: conversations_list / messages_read / messages_send / permissions 管理
- stdio + SSE 双传输

参考: `hermes-agent/mcp_serve.py`

#### 4.13 更多 Memory 后端

当前只有文件系统。扩充：

- **Honcho** — dialectic 用户建模 (理解用户是谁)
- **Mem0** — 云端记忆管理
- **RetainDB** — 数据库持久记忆
- **向量数据库** — Redis / Chroma / Qdrant 语义记忆

参考: `hermes-agent/plugins/memory/`

#### 4.14 Computer Use (桌面操控)

截图 + 鼠标 + 键盘自动化：

- cua-driver 后端
- 不抢占用户光标
- 支持 macOS / Windows / Linux

参考: `hermes-agent/tools/computer_use_tool.py`

#### 4.15 视频生成

- **FAL.ai** — Wan / AnimateDiff
- **xAI** — 视频生成 + 编辑 + 延长

参考: `hermes-agent/plugins/video_gen/`

#### 4.16 可观测 Hooks

标准化的 pre / post 事件钩子：

- LLM: pre_llm_call / post_llm_call
- Tool: pre_tool_call / post_tool_call / transform_tool_result
- Session: start / end / finalize
- 可外接 Langfuse / NeMo / Prometheus

参考: `hermes-agent/docs/observability/`

#### 4.17 Bitwarden 密钥管理

安全存储和注入 API key，替代 .env 文件：

- Bitwarden CLI 集成
- 运行时密钥注入
- 不落盘存储

参考: `hermes-agent/agent/secret_sources/bitwarden.py`

#### 4.18 凭证池 (Credential Pool)

多 API key 轮换避免限速：

- 按 provider 注册多个 key
- 自动轮换 + 限速感知
- 失效 key 自动标记

参考: `hermes-agent/agent/credential_pool.py`

---

### P2 — 高价值 + 高难度 (长期方向)

#### 4.19 桌面 App (Tauri)

独立 GUI 应用：

- 聊天 + 侧边文件浏览器 + 语音对话
- 内置更新
- macOS / Windows / Linux

参考: `hermes-agent/apps/desktop/`

#### 4.20 TUI (终端 UI)

Ink (React-Ink) 富终端界面：

- 多行编辑 / slash 命令补全
- 对话历史 / 流式工具输出
- 中断重定向

参考: `hermes-agent/ui-tui/`

#### 4.21 MOA 编排完善

当前有 scaffold (`agent/moa.py`)，完善为运行时：

- 多 agent 协作循环
- Trace 支持
- 投票 / 聚合机制

参考: `hermes-agent/agent/moa_loop.py`

#### 4.22 批量 Trajectory 生成

SWE-bench / 数据生成流水线：

- trajectory 压缩
- mini SWE-bench runner
- 训练数据导出

参考: `hermes-agent/batch_runner.py`

#### 4.23 完整安全体系

多层安全子系统：

- URL 安全检查
- 路径安全校验
- 威胁模式检测
- Schema 清洗
- 文件安全检查
- 网络出口隔离

参考: `hermes-agent/tools/tirith_security.py`, `tools/url_safety.py` 等

#### 4.24 LSP 集成

Language Server Protocol:

- 代码理解 + 补全 + 诊断
- 多语言 server (Python / TS / Go)

参考: `hermes-agent/agent/lsp/`

#### 4.25 生活场景集成

- **Home Assistant** — 智能家居控制
- **Spotify** — 音乐播放
- **Google Meet** — 会议机器人

参考: `hermes-agent/plugins/homeassistant/`, `plugins/spotify/`, `plugins/google_meet/`

#### 4.26 Pet 系统

桌面宠物头像 + gamification：

- 多 provider 头像生成
- 成就系统
- 桌面 overlay

参考: `hermes-agent/agent/pet/`

---

## 5. 最值得立即做的 5 件事

| # | 功能 | 原因 |
|---|------|------|
| 1 | **更多 LLM provider 插件化** | 当前仅 2 provider，扩充到 Gemini / DeepSeek / Ollama 成本最低收益最大 |
| 2 | **TTS 语音合成** | 已有 ASR 输入，补 TTS 输出形成语音闭环，Edge TTS 零成本 |
| 3 | **OpenAI 兼容 API Server** | 让 qingzhou 作为 API 被其他应用调用，解锁大量集成场景 |
| 4 | **Skill Hub + 更多 Skills** | 仅 3 skill 严重不足，参考 hermes 的安装机制和 100+ skills |
| 5 | **会话搜索** | 历史对话 FTS 搜索 + LLM 总结，实用且实现简单 |

---

## 6. 实现路径建议

### Phase 1: 基础扩充 (1-2 周)
- LLM provider 插件注册表
- 更多搜索后端 (Tavily / Brave)
- TTS (Edge TTS 优先)
- 会话搜索

### Phase 2: 能力增强 (2-4 周)
- 图片生成工具
- OpenAI 兼容 API Server
- Skill Hub 目录 + skill 安装机制
- Docker 终端后端
- 更多 IM 平台 (Slack / WhatsApp)

### Phase 3: 生态完善 (1-2 月)
- MCP Server 模式
- 更多 Memory 后端
- Curator 自我改进
- Computer Use
- 可观测 hooks
- 凭证池

### Phase 4: 长期演进 (2+ 月)
- 桌面 App (Tauri)
- TUI
- MOA 编排
- 完整安全体系
- 生活场景集成

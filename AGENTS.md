# xu-agent 智能体说明

## 概览

`xu-agent` 是一个基于 LangGraph 的个人 AI 助手。

- 后端图入口：`agent/graph.py`
- 工具定义：`tools/registry.py`
- 智能体交互日志：`agent/logging.py`
- LangGraph 开发配置：`langgraph.json`
- 前端聊天界面：`web/src`

后端通过 LangGraph Server 暴露名为 `agent` 的图，地址为
`config/xu-agent.json` 中 `server.backendPort` 指定的本地端口。前端 Next.js 应用从 `http://localhost:3000`
连接后端。

## 模型配置

模型通过后端环境变量配置：

- `LLM_ADAPTER_TYPE`：模型供应商前缀，例如 `anthropic` 或 `openai`
- `LLM_MODEL`：模型名称，例如 `glm-5.1`
- `LLM_API_KEY`：API 密钥
- `LLM_BASE_URL`：供应商基础 URL

`agent/graph.py` 会把项目级的 `LLM_*` 变量映射为 LangChain 期望的
供应商专用环境变量：

- `anthropic` -> `ANTHROPIC_API_KEY`、`ANTHROPIC_API_URL`
- `openai` -> `OPENAI_API_KEY`、`OPENAI_BASE_URL`

最终模型字符串按下面的格式生成：

```text
{LLM_ADAPTER_TYPE}:{LLM_MODEL}
```

## 工具

当前工具列表由 `tools/registry.py` 中的 `ALL_TOOLS` 定义。

### `todo_write`

维护当前 thread/session 的任务清单，用于多步骤任务的计划和进度同步。

- 输入是完整 todo 列表，而不是增量变更。
- 每个 todo 项包含：
  - `content`：任务内容
  - `status`：任务状态，只能是 `pending`、`in_progress` 或 `completed`
- 状态保存在当前后端进程内，按 thread/session 隔离。
- 后端重启后 todo 状态会清空。
- 该工具只记录计划和进度，不会执行实际文件、命令或网络操作。

使用原则：

- 多步骤任务、代码修改、问题排查、方案比较或长时间跟进任务，应先调用 `todo_write` 建立清单。
- 每完成一个阶段或切换当前重点时，应再次调用 `todo_write` 更新状态。
- 简单问答或一次性工具调用不需要使用 `todo_write`。

### `load_skill`

按名称动态加载项目技能的完整说明。

- 启动时后端只扫描 `skills/*/SKILL.md` 的元信息，并把技能目录注入 system prompt。
- system prompt 中只包含技能名称和简短描述，不包含完整 `SKILL.md` 内容。
- 当模型判断需要某个技能时，调用 `load_skill(name)` 获取完整内容。
- `load_skill` 只能按已注册的技能名称查找，不能传入任意文件路径。
- 技能内容作为工具结果进入当前对话上下文，后续模型可按其中说明继续工作。

当前内置项目技能示例：

- `code-review`：用于代码审查。
- `xu-agent-development`：用于开发和维护本项目 agent 能力。

### `run_shell_command`

在后端宿主机上运行 shell 命令。

支持的 shell：

- `auto`
- `powershell`
- `cmd`
- `bash`
- `sh`

重要行为：

- 默认超时时间为 30 秒。
- 超时时间限制在 `1..120` 秒。
- 命令超时后会终止对应进程树。
- 输出按 UTF-8 解码，遇到非法字节会用替代字符处理。
- 输出会按 `SHELL_TOOL_MAX_OUTPUT_CHARS` 截断。
- `cwd` 为空时，命令在 `.agent_outputs/shell/<thread_id>/` 下运行，避免污染项目根目录。
- 如需操作项目文件，必须显式传入项目内的 `cwd`。
- 禁止从磁盘根目录发起大范围递归扫描，例如
  `Get-ChildItem D:\ -Recurse` 或 `dir D:\ /s`。

### `read_file`

读取工作目录内的 UTF-8 文本文件。

- `path`：要读取的文件路径
- `cwd`：可选工作目录，留空表示后端进程工作目录
- `limit`：可选最大返回行数
- 路径会被限制在 `cwd` 内，不能通过 `..` 或绝对路径逃出工作目录

### `write_file`

向当前 thread 隔离的 agent 输出目录写入 UTF-8 文本文件。

- `path`：要写入的相对输出路径
- `content`：写入内容
- `cwd`：兼容旧调用的参数；会被忽略
- 会自动创建父目录
- 文件会写入 `.agent_outputs/files/<thread_id>/`
- 不接受绝对路径，不能通过 `..` 逃出该输出目录
- `write_file` 不用于修改项目代码；项目内已有文件修改应使用 `edit_file`

### `edit_file`

对工作目录内的 UTF-8 文本文件执行一次精确文本替换。

- `path`：要编辑的文件路径
- `old_text`：要替换的原文，不能为空
- `new_text`：替换后的文本
- `cwd`：可选工作目录，留空表示后端进程工作目录
- 只替换第一次匹配，避免误改多个位置
- 路径会被限制在 `cwd` 内

### `glob`

在工作目录内按 glob pattern 查找文件。

- `pattern`：glob 表达式，例如 `**/*.py`
- `cwd`：可选工作目录，留空表示后端进程工作目录
- `limit`：最大返回数量，默认 `200`
- 结果会去重、排序，并限制在 `cwd` 内

## 日志

`AgentLoggingMiddleware` 用来把智能体、模型和工具交互写入 JSONL 日志。
日志默认关闭；调试时可在后端 `.env` 中开启：

```env
AGENT_LOG_ENABLED=true
```

开启后默认写入：

```text
logs/agent.jsonl
```

记录的事件包括：

- `agent.start`
- `agent.end`
- `model.start`
- `model.end`
- `model.error`
- `tool.start`
- `tool.end`
- `tool.error`

相关环境变量：

- `AGENT_LOG_ENABLED`：设为 `true` 时启用 JSONL 日志
- `AGENT_LOG_DIR`
- `AGENT_LOG_MAX_BYTES`
- `AGENT_LOG_BACKUP_COUNT`

`logs/` 目录已被 git 忽略。

## 技能加载

技能目录位于项目根目录：

```text
skills/
  code-review/SKILL.md
  xu-agent-development/SKILL.md
```

每个 `SKILL.md` 可以包含简单 YAML frontmatter：

```markdown
---
name: code-review
description: 用于审查代码变更，优先发现 bug、回归风险、权限/安全问题和缺失测试。
---

# Code Review Skill
...
```

后端启动时，`agent/skills.py` 会扫描技能目录并建立 registry。registry 只保存：

- `name`
- `description`
- `SKILL.md` 路径

完整技能内容不会在启动时注入 system prompt。只有当 agent 调用 `load_skill(name)` 时，
后端才会读取对应 `SKILL.md` 并作为工具结果返回。

可选环境变量：

- `AGENT_SKILLS_DIR`：自定义技能目录。相对路径会按项目根目录解析。

## 权限

工具调用在真正执行前会先经过 `AgentPermissionMiddleware`。权限管线分三类结果：

- `allow`：允许执行。
- `deny`：直接拒绝，不执行工具。
- `ask`：需要用户审批。

`ask` 会通过 LangGraph `interrupt()` 暂停当前 run，并交给前端 Agent Inbox
审批界面处理。用户点击批准后，原工具调用会继续执行；用户拒绝后，工具不会执行，
并把 `Permission denied` 结果返回给模型。

同一个后端进程内，同一个 thread/session 已经批准过的完全相同工具调用会被缓存。
后续相同的 `tool name + args` 会直接放行，不再重复弹审批。后端重启后缓存清空。

硬拒绝规则主要用于永远不应该执行的 shell 操作，例如：

- `rm -rf /`
- `sudo`
- `shutdown`
- `reboot`
- `mkfs`
- `diskpart`
- `format`
- `dd if=`

需要审批的规则包括：

- `run_shell_command` 中的删除类命令，例如 `rm`、`del`、`Remove-Item`
- 危险权限变更，例如 `chmod 777`
- 写入系统配置路径，例如 `/etc/`
- `write_file` 或 `edit_file` 试图写出工作目录

可选环境变量：

- `AGENT_PERMISSION_ALLOW_ASK_RULES`：设为 `true` 时跳过交互审批并允许 ask 规则继续执行。
  默认是 `false`，更适合日常使用。

## 前端流式输出

前端使用 `@langchain/langgraph-sdk/react`。

当前提交请求时设置：

```ts
streamResumable: false
```

这是有意为之。之前启用可恢复流时，重启后端后未完成的旧 run 可能会继续恢复，
导致系统自动执行之前的任务。

前端仍会把当前 `threadId` 存在 URL 中。如果浏览器 URL 包含
`threadId=...`，UI 会重新连接到该线程并拉取状态历史。

## LangGraph 开发态持久化

LangGraph dev 会把本地状态存储在：

```text
.langgraph_api/
```

重要文件包括：

- `.langgraph_ops.pckl`
- `.langgraph_checkpoint.*.pckl`
- `.langgraph_retry_counter.pckl`

如果重启后端后旧任务又开始执行，可以检查这个目录。里面可能还保存着上一次会话中
处于 `running` 状态的 run。

重置本地 LangGraph dev 状态：

1. 停止后端进程。
2. 将 `.langgraph_api/` 移到备份位置，或删除它。
3. 从浏览器 URL 中移除 `threadId=...`，或开启一个新聊天。
4. 重启后端。

更推荐先移动目录做备份，例如：

```powershell
Move-Item .langgraph_api .langgraph_api.backup
```

## Git 提交规范

提交信息建议使用简洁的 Conventional Commits 风格：

```text
<type>(<scope>): <subject>
```

常用 `type`：

- `feat`：新增功能
- `fix`：修复问题
- `docs`：文档变更
- `refactor`：不改变行为的代码重构
- `test`：测试相关变更
- `chore`：构建、依赖、配置等杂项

规范建议：

- `subject` 使用一句简短描述，说明“做了什么”。
- scope 可选，用来标明影响范围，例如 `backend`、`frontend`、`agent`。
- 一次提交只包含一类清晰变更，避免把无关修改混在一起。
- 如果有破坏性变更，在提交正文中写明影响和迁移方式。

示例：

```text
feat(agent): add opt-in interaction logging
fix(tools): clamp shell command timeout
docs(agent): document git commit convention
```

## 运维提示

- `Ctrl+C` 可能只停止 LangGraph 包装进程，而留下 worker 或工具子进程。
- 如果后端看起来卡住了，可以检查：
  - `config/xu-agent.json` 中的 `server.backendPort`
  - `langgraph.exe`
  - `lcchat` 环境中的 `python.exe`
  - 高 CPU 占用的 `powershell.exe`、`cmd.exe` 或 `Robocopy.exe`
- 调试时最有用的文件是 `logs/agent.jsonl`。
- 如果 `agent.jsonl` 中出现 `tool.start` 但没有对应的 `tool.end`，
  通常表示当前 run 正在等待该工具返回。

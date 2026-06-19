# xu-agent 智能体说明

## 概览

`xu-agent` 是一个基于 LangGraph 的个人 AI 助手。

- 后端图入口：`backend/src/agent.py`
- 工具定义：`backend/src/tools.py`
- 智能体交互日志：`backend/src/agent_logging.py`
- LangGraph 开发配置：`backend/langgraph.json`
- 前端聊天界面：`frontend/src`

后端通过 LangGraph Server 暴露名为 `agent` 的图，地址为
`http://localhost:2024`。前端 Next.js 应用从 `http://localhost:3000`
连接后端。

## 模型配置

模型通过后端环境变量配置：

- `LLM_ADAPTER_TYPE`：模型供应商前缀，例如 `anthropic` 或 `openai`
- `LLM_MODEL`：模型名称，例如 `glm-5.1`
- `LLM_API_KEY`：API 密钥
- `LLM_BASE_URL`：供应商基础 URL

`backend/src/agent.py` 会把项目级的 `LLM_*` 变量映射为 LangChain 期望的
供应商专用环境变量：

- `anthropic` -> `ANTHROPIC_API_KEY`、`ANTHROPIC_API_URL`
- `openai` -> `OPENAI_API_KEY`、`OPENAI_BASE_URL`

最终模型字符串按下面的格式生成：

```text
{LLM_ADAPTER_TYPE}:{LLM_MODEL}
```

## 工具

当前工具列表由 `backend/src/tools.py` 中的 `ALL_TOOLS` 定义。

### `get_system_cpu_usage`

返回宿主机整体 CPU 使用率百分比。

- Windows：优先尝试 `typeperf`，然后回退到 PowerShell `Get-Counter`
- Linux 类系统：读取 `/proc/stat` 采样
- 采样间隔限制在 `1..10` 秒

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
- 禁止从磁盘根目录发起大范围递归扫描，例如
  `Get-ChildItem D:\ -Recurse` 或 `dir D:\ /s`。

### `read_file`

读取工作目录内的 UTF-8 文本文件。

- `path`：要读取的文件路径
- `cwd`：可选工作目录，留空表示后端进程工作目录
- `limit`：可选最大返回行数
- 路径会被限制在 `cwd` 内，不能通过 `..` 或绝对路径逃出工作目录

### `write_file`

向工作目录内写入 UTF-8 文本文件。

- `path`：要写入的文件路径
- `content`：写入内容
- `cwd`：可选工作目录，留空表示后端进程工作目录
- 会自动创建父目录
- 路径会被限制在 `cwd` 内

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
backend/logs/agent.jsonl
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

`backend/logs/` 目录已被 git 忽略。

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
backend/.langgraph_api/
```

重要文件包括：

- `.langgraph_ops.pckl`
- `.langgraph_checkpoint.*.pckl`
- `.langgraph_retry_counter.pckl`

如果重启后端后旧任务又开始执行，可以检查这个目录。里面可能还保存着上一次会话中
处于 `running` 状态的 run。

重置本地 LangGraph dev 状态：

1. 停止后端进程。
2. 将 `backend/.langgraph_api/` 移到备份位置，或删除它。
3. 从浏览器 URL 中移除 `threadId=...`，或开启一个新聊天。
4. 重启后端。

更推荐先移动目录做备份，例如：

```powershell
Move-Item backend\.langgraph_api backend\.langgraph_api.backup
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
  - 端口 `2024`
  - `langgraph.exe`
  - `lcchat` 环境中的 `python.exe`
  - 高 CPU 占用的 `powershell.exe`、`cmd.exe` 或 `Robocopy.exe`
- 调试时最有用的文件是 `backend/logs/agent.jsonl`。
- 如果 `agent.jsonl` 中出现 `tool.start` 但没有对应的 `tool.end`，
  通常表示当前 run 正在等待该工具返回。

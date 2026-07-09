# Qingzhou Agent 上下文压缩技术说明

本文记录 Qingzhou Agent 当前的上下文管理与压缩实现。核心代码位于：

- `agent/context.py`：token 统计、工具结果裁剪、自动摘要与状态更新。
- `agent/prompt.py`：上下文摘要提示词。
- `agent/commands.py`：`/compact`、`/clear` 等控制命令。
- `agent/graph.py`：上下文中间件注册。
- `web/src/providers/Stream.tsx`：前端状态类型与清理操作。

## 总体流程

Qingzhou 使用 `AgentContextCompactMiddleware` 在每次模型调用前后管理上下文：

```text
模型调用前
  1. 裁剪旧工具结果 prune_old_tool_results
  2. 使用裁剪后的消息检查自动摘要阈值
  3. 达到阈值时执行 Summary Compact
  4. 未达到阈值时仅写回工具裁剪结果

模型调用后
  1. 优先读取 provider 返回的 input_tokens
  2. 没有 usage 时使用模型 tokenizer 估算
  3. 将 context_usage 写回 LangGraph state
```

与早期版本不同，当前实现不再按消息数量直接删除中间对话。原 `snip`
机制已经移除，替换为只缩小旧工具载荷的 Tool Result Pruning。

## LangGraph State

上下文中间件使用扩展状态 `XuAgentState`：

```python
class XuAgentState(AgentState):
    context_usage: ContextUsage
    compact_metadata: CompactMetadata
    tool_prune_metadata: ToolPruneMetadata
    compact_failure_count: int
```

### context_usage

记录最近一次模型请求的上下文占用：

```json
{
  "input_tokens": 48231,
  "output_tokens": 1024,
  "total_tokens": 49255,
  "message_count": 86,
  "includes_tools": true,
  "counter": "response.usage_metadata"
}
```

`input_tokens` 用于下一次模型调用前判断是否需要自动摘要。

### compact_metadata

记录最近一次 LLM 摘要压缩：

```json
{
  "last_compacted_at": "2026-07-09T07:00:00+00:00",
  "before_tokens": 116000,
  "summarized_messages": 68,
  "kept_messages": 20,
  "failures": 0,
  "trigger": "auto",
  "focus": ""
}
```

### tool_prune_metadata

记录最近一次工具结果裁剪：

```json
{
  "last_pruned_at": "2026-07-09T07:07:50+00:00",
  "pruned_tool_results": 4,
  "deduplicated_tool_results": 0,
  "truncated_tool_calls": 0,
  "protected_messages": 22
}
```

## Tool Result Pruning

入口：

```python
prune_old_tool_results(
    messages: list[BaseMessage],
) -> tuple[list[BaseMessage], ToolPruneMetadata | None]
```

该阶段不调用 LLM，不删除 HumanMessage 或普通 AIMessage，只处理历史工具载荷。

### 默认参数

代码默认常量
```text
DEFAULT_PRUNE_PROTECT_LAST_MESSAGES = 20
DEFAULT_PRUNE_PROTECT_TAIL_TOKENS = 20_000
DEFAULT_PRUNE_MIN_RESULT_CHARS = 200
DEFAULT_PRUNE_ARGUMENT_CHARS = 500
DEFAULT_PRUNE_ARGUMENT_HEAD_CHARS = 200
```

可通过环境变量覆盖：

```env
AGENT_TOOL_RESULT_PRUNE_ENABLED=true
AGENT_TOOL_RESULT_PROTECT_LAST_MESSAGES=20
AGENT_TOOL_RESULT_PROTECT_TAIL_TOKENS=20000
AGENT_TOOL_RESULT_PRUNE_MIN_CHARS=200
AGENT_TOOL_CALL_PRUNE_ARGUMENT_CHARS=500
AGENT_TOOL_CALL_ARGUMENT_HEAD_CHARS=200
```

`DISABLE_COMPACT=true` 会同时关闭工具裁剪与自动摘要。

### 近期保护边界

中间件从消息末尾向前估算 token。单条消息的粗略估算为：

```text
(消息内容字符数 + tool_calls JSON 字符数) / 4 + 10
```

向前扫描直到：

```text
加入下一条消息会超过保护 token 预算
并且已经保护至少指定数量的近期消息
```

因此近期保护范围是：

```text
至少保留最近 20 条
并尽量保留最近约 20K token
```

如果最近 20 条本身已经超过 20K token，仍完整保护 20 条。如果全部历史不足
20K token，则不会对普通旧工具结果做一行压缩。

### 工具调用索引

LangChain 将工具调用标准化到 `AIMessage.tool_calls`：

```python
{
    "id": "call-123",
    "name": "run_shell_command",
    "args": {"command": "pytest"},
    "type": "tool_call",
}
```

裁剪器先建立：

```text
tool_call_id -> (tool_name, args)
```

随后通过 `ToolMessage.tool_call_id` 找到工具名称和原始参数。消息 ID 和
`tool_call_id` 不会改变，因此工具调用配对仍然有效。

### 重复结果去重

字符串形式、长度超过阈值的 ToolMessage 会计算 MD5。扫描顺序从新到旧：

- 最新结果保留全文。
- 较早的相同结果替换为：

```text
[Duplicate tool output - same content as a more recent call]
```

去重作用于整个消息列表，包括近期保护区。它只识别完全相同的内容，不做语义去重。

### 旧工具结果一行摘要

保护区以前、长度超过 200 字符的字符串 ToolMessage 会被确定性模板替换。

Shell 与 SSH：

```text
[run_shell_command] ran `pytest` -> exit 0, 80 lines output
[run_ssh_command] ran `systemctl status nginx` -> exit 1, 24 lines output
```

文件工具：

```text
[read_file] read agent/context.py (20,300 chars)
[write_file] wrote report.md (120 lines)
[edit_file] edited report.md (32 chars result)
```

其他已知工具：

```text
[glob] matched `**/*.py` (45 lines output)
[rag_search] query='上下文压缩' (8,200 chars result)
[web_search] query='LangGraph middleware' (12,000 chars result)
[playwright_open] https://example.com (1,100 chars result)
```

未知工具使用通用格式：

```text
[load_skill] name=pdf (2,829 chars result)
```

通用格式不会预览名为 `content`、`password`、`private_key`、`token` 或
`api_key` 的参数。

一行摘要只保存“调用了什么、主要参数、结果规模和退出码”，不会保存完整日志中的
业务结论。重要错误和决策应由助手回复或后续 LLM 摘要保留。

### 大型调用参数

ToolMessage 变短后，AIMessage 中的调用参数仍可能很大。例如 `write_file`
可能携带完整文件正文。

对于保护区以前、序列化后超过 500 字符的 `tool_calls[].args`：

1. 递归遍历 dict 和 list。
2. 超过 200 字符的字符串保留开头并添加 `...[truncated]`。
3. 数字、布尔值、短字符串和路径保持不变。
4. 如果递归处理后没有变化，则不写回消息。

LangChain 中的 args 已经是结构化字典，因此裁剪后仍能保持合法工具调用结构。

### 多模态工具结果

保护区以前的 ToolMessage 如果包含以下图片块：

```text
image
image_url
input_image
```

会替换为：

```json
{
  "type": "text",
  "text": "[screenshot removed to save context]"
}
```

同一列表中的非图片内容保持原样。

### 写回与日志

发生裁剪后，中间件使用：

```python
[
    RemoveMessage(id=REMOVE_ALL_MESSAGES),
    *pruned_messages,
]
```

替换 LangGraph 中的完整消息列表。普通对话内容保持不变，只是 ToolMessage
和旧 tool call 参数被换成更小的版本。

同时记录事件：

```text
context.tool_prune
```

事件位于 `logs/agent.jsonl`。当前前端会展示裁剪后的工具卡片内容，但尚未直接
展示 `tool_prune_metadata` 统计。

## 自动 Summary Compact

### token 统计

模型调用完成后，中间件按以下优先级统计：

1. 从响应消息的 `usage_metadata.input_tokens` 获取 provider 真实用量。
2. 如果 provider 没有返回 usage，调用模型的
   `get_num_tokens_from_messages(messages, tools=tools)`。
3. 如果工具参数不被当前 tokenizer 支持，则退回不带 tools 的消息计数。
4. 统计失败时保存错误信息，本轮不触发自动摘要。

只使用 `input_tokens` 判断上下文压力，避免把生成 token 误算成下一轮输入上下文。

### 触发阈值

默认配置：

```text
AGENT_CONTEXT_WINDOW = 128000
AGENT_COMPACT_MARGIN_TOKENS = 13000
```

阈值：

```text
threshold = context_window - margin
          = 128000 - 13000
          = 115000 input tokens
```

当上一次记录的 `input_tokens >= 115000` 时，下一次模型调用前执行摘要。

相关配置：

```env
AGENT_AUTO_COMPACT_ENABLED=true
AGENT_CONTEXT_WINDOW=128000
AGENT_COMPACT_MARGIN_TOKENS=13000
AGENT_COMPACT_KEEP_MESSAGES=20
AGENT_COMPACT_MAX_FAILURES=3
```

兼容关闭开关：

```env
DISABLE_COMPACT=true
DISABLE_AUTO_COMPACT=true
```

当前上下文窗口由配置指定，不会根据模型名称自动推断。

### 消息分割

自动摘要默认：

```text
较早消息 -> 交给摘要模型
最近 20 条 -> 保留原文
```

如果保留区域从 ToolMessage 开始，边界会向前移动，纳入对应的 AI tool call，
避免产生孤立工具结果。

当前自动摘要的尾部保护仍按消息数量计算，没有使用 Tool Result Pruning 的
20K token 保护算法。

### 摘要模型

默认使用当前主模型，也可单独配置：

```env
AGENT_SUMMARY_LLM_ADAPTER_TYPE=anthropic
AGENT_SUMMARY_LLM_MODEL=glm-5.1
```

摘要模型：

- 禁用 streaming。
- 不注册工具。
- callbacks 设为空，避免摘要调用污染主对话回调。
- 使用 `context-compaction-summary` tag。

摘要请求包含：

```text
SystemMessage：摘要规则与输出格式
HumanMessage：被压缩消息序列和可选 focus
```

图片、文件等多模态块在发送给摘要模型前替换为文字占位符。

### 摘要内容

`BASE_COMPACT_PROMPT` 要求保留：

1. 用户主要请求和意图。
2. 关键技术概念。
3. 检查、修改或创建的文件。
4. 遇到的错误和修复。
5. 已解决问题和当前排查状态。
6. 用户消息与重要反馈。
7. 未完成任务。
8. 压缩前正在进行的工作。
9. 与当前任务直接相关的下一步。

模型输出中的 `<analysis>...</analysis>` 会被移除，只保留 `<summary>` 内容。

### 状态替换

摘要成功后，消息列表替换为：

```text
SystemMessage：压缩触发方式、时间、压缩前 token 和消息数量
SystemMessage：结构化历史摘要和继续任务的提示
最近 20 条原始消息
```

写回使用 `RemoveMessage(id=REMOVE_ALL_MESSAGES)`，因此原始旧消息不再保留在当前
LangGraph 活跃 state 中。

摘要事件：

```text
context.compact
```

## 手动 `/compact`

支持：

```text
/compact
/compact 保留数据库迁移和失败测试
```

该命令由 `AgentCommandMiddleware` 拦截，不发送给主对话模型。

自动压缩和手动压缩复用同一套：

- 摘要模型。
- 摘要提示词。
- 消息替换逻辑。
- metadata。
- 日志事件。

区别是手动压缩：

- 不检查 token 阈值。
- `trigger` 记录为 `manual`。
- 可通过 focus 指示摘要模型优先保留特定主题。
- 默认 `AGENT_MANUAL_COMPACT_KEEP_MESSAGES=0`，即全部历史进入摘要。

如果希望手动压缩后保留近期原文：

```env
AGENT_MANUAL_COMPACT_KEEP_MESSAGES=10
```

## `/clear`

`/clear` 会清除：

- 当前 thread 的全部消息。
- `context_usage`。
- `compact_metadata`。
- `tool_prune_metadata`。
- `compact_failure_count`。
- 各消息平台维护的线程内短期历史。

## 失败处理

工具裁剪和摘要过程由中间件统一捕获异常：

```text
context.compact_error
```

失败时：

1. 当前消息保持不变。
2. `compact_failure_count` 加一。
3. 默认连续失败达到 3 次后停止自动摘要。
4. `/clear` 会重置失败计数。
5. 手动 `/compact` 失败会向用户返回错误信息。

当前没有实现：

- 摘要失败冷却时间。
- 鉴权与网络错误分类。
- 摘要失败时的本地 fallback。
- 连续低收益压缩的反抖动机制。
- 会话级并发压缩锁。

## 前端表现

关闭 `Hide Tool Calls` 时，前端会直接显示写回 state 的裁剪结果：

```text
[run_shell_command] ran `pip install pdfplumber` -> exit 0, 18 lines output
[load_skill] name=pdf (2,829 chars result)
[Duplicate tool output - same content as a more recent call]
[screenshot removed to save context]
```

大型调用参数会显示：

```text
前200字符...[truncated]
```

开启 `Hide Tool Calls` 后，工具调用和 ToolMessage 均不显示。

当前 `tool_prune_metadata` 已包含在前端 state 类型中，但没有独立的统计面板、
压缩徽标或提示消息。

## 当前限制

1. Tool Result Pruning 的 token 是字符数近似值，不是模型 tokenizer 的精确结果。
2. 自动 Summary Compact 的窗口固定为配置值，不会根据模型自动识别。
3. 自动摘要尾部按固定消息数量保护，不按 token 预算。
4. 滚动摘要是将旧摘要作为普通历史再次摘要，没有显式的摘要增量更新协议。
5. 工具摘要模板只保存调用事实，不保证保留完整错误原因。
6. 多模态列表中的大段文本暂时不会进一步裁剪。
7. 只处理 LangChain 标准化后的 `AIMessage.tool_calls`。
8. 尚未对所有原始工具参数执行完整敏感信息脱敏。
9. `tool_prune_metadata` 暂未在 UI 中可视化。

## 关键日志

Agent 日志默认位于：

```text
logs/agent.jsonl
```

启用方式：

```env
AGENT_LOG_ENABLED=true
AGENT_LOG_DIR=./logs
AGENT_LOG_MAX_BYTES=10485760
AGENT_LOG_BACKUP_COUNT=5
```

常用事件：

```text
context.tool_prune
context.compact
context.compact_error
model.start
model.end
tool.start
tool.end
```

快速判断是否触发：

```powershell
Select-String logs\agent.jsonl -Pattern '"event": "context\.(tool_prune|compact|compact_error)"'
```

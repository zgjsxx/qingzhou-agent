# Qingzhou 上下文压缩策略

本文只描述 Qingzhou Agent 当前生效的上下文压缩逻辑，不记录代码实现细节。

## 总体策略

Qingzhou 的上下文管理分两层：

1. Tool Result Pruning：在不改变对话结构的前提下，缩小历史工具结果和旧工具参数。
2. Summary Compact：当上下文接近阈值，使用摘要替换中间历史，保留头部和尾部原文。

`DISABLE_COMPACT=true` 会同时关闭自动摘要压缩和工具结果裁剪。

## Tool Result Pruning

工具结果裁剪会在模型调用前运行。它不调用 LLM，不删除普通用户消息或助手消息，只缩小历史工具负载。

默认保护范围：

```text
至少保留最近 20 条消息
并尽量保留最近约 20K token 的尾部上下文
```

可配置项：

```env
AGENT_TOOL_RESULT_PRUNE_ENABLED=true
AGENT_TOOL_RESULT_PROTECT_LAST_MESSAGES=20
AGENT_TOOL_RESULT_PROTECT_TAIL_TOKENS=20000
AGENT_TOOL_RESULT_PRUNE_MIN_CHARS=200
AGENT_TOOL_CALL_PRUNE_ARGUMENT_CHARS=500
AGENT_TOOL_CALL_ARGUMENT_HEAD_CHARS=200
```

裁剪规则：

- 重复的旧工具结果会替换为重复提示。
- 保护区以前、超过长度阈值的旧 `ToolMessage` 会替换成一行摘要。
- 旧 `AIMessage.tool_calls[].args` 过大时，会保留参数结构，但截断长字符串。
- 工具结果中的图片块会替换成文本占位。
- 工具调用 id、消息 id 和工具配对关系保持不变。

典型裁剪结果：

```text
[Duplicate tool output - same content as a more recent call]
[run_shell_command] ran `pytest` -> exit 0, 80 lines output
[read_file] read agent/context.py (20,300 chars)
[screenshot removed to save context]
```

## 自动摘要压缩

自动摘要压缩根据上一轮模型调用记录的 `input_tokens` 判断是否触发。

默认阈值：

```text
AGENT_CONTEXT_WINDOW = 128000
AGENT_COMPACT_MARGIN_TOKENS = 13000
触发阈值 = 115000 input tokens
```

可配置项：

```env
AGENT_AUTO_COMPACT_ENABLED=true
AGENT_CONTEXT_WINDOW=128000
AGENT_COMPACT_MARGIN_TOKENS=13000
AGENT_COMPACT_KEEP_MESSAGES=20
AGENT_COMPACT_PROTECT_FIRST_MESSAGES=3
AGENT_COMPACT_MAX_FAILURES=3
```

关闭自动摘要：

```env
DISABLE_AUTO_COMPACT=true
```

## 压缩后的消息结构

摘要压缩后的真实模型上下文采用：

```text
head + summary + tail
```

其中：

- `head`：头部原文消息。
- `summary`：中间历史的压缩摘要。
- `tail`：最近原文消息。

LangGraph 写回 state 时会额外使用 `RemoveMessage("__remove_all__")` 替换旧消息列表。它是状态更新指令，不是模型会看到的对话消息。

## Head 保留规则

首次压缩默认保留开头 3 条非 system 消息：

```env
AGENT_COMPACT_PROTECT_FIRST_MESSAGES=3
```

如果第一条是 system prompt，则 system prompt 总是保留在 head 中。

一旦当前历史中已经存在 previous summary，头部非 system 消息保护会衰减为 0，避免早期用户消息永久保留。system prompt 仍然保留。

head 边界不会切断工具调用组：

- 如果边界落在 `ToolMessage` 上，会向后移动。
- 如果边界前一条 `AIMessage` 带有 tool calls，会把对应工具结果一起纳入 head。

## Tail 保留规则

自动压缩默认保留最近 20 条消息：

```env
AGENT_COMPACT_KEEP_MESSAGES=20
```

手动 `/compact` 默认不保留 tail：

```env
AGENT_MANUAL_COMPACT_KEEP_MESSAGES=0
```

如果 tail 第一条是 `ToolMessage`，边界会向前移动，包含对应的 assistant tool call，避免产生孤立工具结果。

## 迭代摘要

如果压缩窗口里已经存在上一轮摘要：

```text
previous_summary + 新消息 -> 新 summary
```

上一轮摘要会显式作为 `previous_summary` 提供给摘要模型，新压缩只总结上一轮摘要之后的新消息。

如果除了旧摘要以外没有新消息可压缩，则本次压缩不会执行。

## Summary 角色选择

摘要不会作为 system message 插入。它会根据前后邻居动态选择：

- head 最后一条是 `assistant` 或 `tool`：优先用 `HumanMessage`。
- 其他情况：优先用 `AIMessage`。
- 如果这个角色和 tail 第一条冲突，则尝试切换到另一个角色。
- 如果 `HumanMessage` 和 `AIMessage` 两种角色都会造成相邻角色冲突，则把 summary prepend 到 tail 第一条消息内容里。

因此压缩结果可能是：

```text
head + AIMessage(summary) + tail
head + HumanMessage(summary) + tail
head + merged_summary_into_tail
```

`ToolMessage` 只参与边界和冲突判断，不会承载 summary。

## 摘要内容

摘要模型会保留：

- 用户主要目标和最新未完成请求。
- 已完成动作、关键决策和当前状态。
- 修改、检查或创建过的文件。
- 重要命令、输出、错误和修复结果。
- 已解决问题与仍待处理事项。
- 后续继续工作需要的上下文。

摘要模型输出中的 `<analysis>...</analysis>` 会被移除；如果输出包含 `<summary>...</summary>`，只保留其中内容。

图片、文件等多模态块在进入摘要模型前会替换为文本占位。

## 手动 `/compact`

支持：

```text
/compact
/compact 保留数据库迁移和失败测试
```

手动压缩不检查 token 阈值，直接执行摘要压缩。

带参数的 `/compact <focus>` 会把 focus 传给摘要模型，提示模型优先保留相关内容。

手动压缩和自动压缩共用同一套摘要、消息切分、迭代摘要和写回逻辑。

## 失败处理

压缩或裁剪异常时：

- 当前消息保持不变。
- `compact_failure_count` 加一。
- 连续失败达到 `AGENT_COMPACT_MAX_FAILURES` 后停止自动摘要。
- `/clear` 会重置失败计数。
- 手动 `/compact` 失败会向用户返回错误信息。

## 状态记录

最近一次上下文统计记录在 `context_usage`：

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

最近一次摘要压缩记录在 `compact_metadata`：

```json
{
  "last_compacted_at": "2026-07-09T07:00:00+00:00",
  "before_tokens": 116000,
  "summarized_messages": 68,
  "kept_messages": 23,
  "failures": 0,
  "trigger": "auto",
  "focus": ""
}
```

最近一次工具结果裁剪记录在 `tool_prune_metadata`：

```json
{
  "last_pruned_at": "2026-07-09T07:07:50+00:00",
  "pruned_tool_results": 4,
  "deduplicated_tool_results": 0,
  "truncated_tool_calls": 0,
  "protected_messages": 22
}
```

## 日志事件

常用事件：

```text
context.tool_prune
context.compact
context.compact_error
```

日志默认写入：

```text
logs/agent.jsonl
```

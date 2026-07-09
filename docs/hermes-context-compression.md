# Hermes 上下文压缩机制

本文记录 `D:\ai\hermes-agent` 中的上下文压缩设计，作为 Qingzhou Agent
后续改进上下文管理的技术参考。

Hermes 的核心实现位于：

- `agent/context_compressor.py`：压缩算法、工具输出裁剪和摘要生成。
- `agent/conversation_loop.py`：自动压缩触发时机。
- `agent/conversation_compression.py`：压缩锁、会话状态与失败处理。
- `agent/agent_init.py`：模型窗口、阈值和压缩模型初始化。
- `cli.py`、`gateway/slash_commands.py`：手动 `/compact` 命令。

## 设计目标

上下文压缩不是简单删除旧消息，而是在不影响当前任务连续性的前提下，
将长对话改写为以下结构：

```text
[受保护的头部消息]
[早期和中期对话的结构化摘要]
[近期消息原文]
```

需要满足的主要约束：

1. 系统提示、初始任务和近期工作不能丢失。
2. 最新用户请求和最新助手回复必须保留原文。
3. 工具调用与工具结果必须保持配对。
4. 重复文件内容、终端日志和截图不能长期占据上下文。
5. 多次压缩后，旧摘要必须能够继续滚动更新。
6. 摘要服务失败时，不能静默丢失原始对话。

## 总体流程

Hermes 的一次压缩大致分为五步：

1. 对旧工具输出做确定性预裁剪，不调用 LLM。
2. 计算需要永久保留的头部消息。
3. 按 token 预算从末尾选出需要保留原文的近期消息。
4. 使用摘要模型将中间消息生成结构化摘要。
5. 重新组装消息并清理工具调用配对、历史图片等内容。

伪代码如下：

```python
messages = prune_old_tool_results(messages)
head_end = find_protected_head(messages)
tail_start = find_tail_by_token_budget(messages)
middle = messages[head_end:tail_start]
summary = summarize(previous_summary, middle)
messages = head + [summary] + tail
messages = sanitize_tool_pairs(messages)
messages = strip_historical_media(messages)
```

## 自动触发

Hermes 优先使用模型 API 返回的真实 `prompt_tokens` 判断上下文压力。
当 provider 不返回 usage 时，才使用包含消息和工具 schema 的估算值。

默认配置：

```yaml
compression:
  enabled: true
  threshold: 0.50
  target_ratio: 0.20
  protect_first_n: 3
  protect_last_n: 20
```

触发阈值不是简单的 `context_length * threshold`。模型的最大输出 token
也会占用窗口，因此先计算有效输入窗口：

```text
effective_input_window = context_length - max_output_tokens
threshold_tokens = effective_input_window * threshold
```

对于小窗口模型，如果最低阈值已经接近整个窗口，Hermes 会退回到有效输入
窗口的约 85%，确保 provider 在拒绝请求前能够触发压缩。

Hermes 在两个位置检查压力：

- 每轮模型调用之前，防止刚产生的大型工具结果挤爆下一次请求。
- 模型响应之后，使用 API 返回的真实输入 token 决定是否压缩。

单轮最多尝试有限次数，避免压缩没有效果时进入循环。

## 工具输出预裁剪

工具输出预裁剪发生在 LLM 摘要之前。它不理解完整业务语义，而是使用工具名称、
调用参数和输出的简单特征，将历史大结果改写为短记录。

### 建立调用映射

一次工具调用通常包含两条消息：

```text
assistant.tool_calls:
  id: call_123
  function.name: terminal
  function.arguments: {"command": "pytest"}

tool:
  tool_call_id: call_123
  content: <完整测试日志>
```

Hermes 先扫描 assistant 消息，建立：

```text
tool_call_id -> (tool_name, arguments_json)
```

处理 tool 消息时，再通过 `tool_call_id` 找到对应工具及参数。

### 选择裁剪边界

Hermes 从对话末尾向前估算每条消息的 token：

- 优先保留 `tail_token_budget` 范围内的近期消息。
- `protect_last_n` 是最低消息数量保障。
- 保护区以外的工具结果才进入普通裁剪。
- 少于或等于 200 字符的短结果通常保留原文。

token 估算不仅计算消息正文，也计算完整的 `tool_calls` envelope，包括 ID、
工具名称和 JSON 参数。这样可以避免并行工具调用被严重低估。

### 工具专用摘要规则

常见工具采用确定性模板：

```text
[terminal] ran `npm test` -> exit 0, 47 lines output
[read_file] read config.py from line 1 (3,400 chars)
[write_file] wrote to agent/context.py (85 lines)
[search_files] content search for 'compress' in agent/ -> 12 matches
[web_search] query='LangGraph compression' (12,430 chars result)
[web_extract] https://example.com/page (8,210 chars)
[execute_code] `print(...)` (120 lines output)
```

未知工具使用通用模板，保留最前面的少量参数和结果长度：

```text
[custom_tool] path=src/main.py mode=check (8,210 chars result)
```

这种处理保留了“做过什么、对象是什么、是否成功、结果规模多大”，但不会尝试
保存日志中的所有业务结论。真正重要的错误和决策由后续 LLM 摘要负责。

### 重复结果去重

Hermes 从后向前扫描字符串形式的工具结果。超过 200 字符且内容哈希相同的结果，
只保留最新一份全文，旧副本替换为：

```text
[Duplicate tool output - same content as a more recent call]
```

典型场景是多次读取同一文件。最新内容可能已经变化，因此必须保留最新副本，
而不是保留第一次读取。

### 裁剪工具调用参数

只压缩 tool 结果还不够。`write_file`、`execute_code` 等调用可能在 assistant
消息的 `function.arguments` 中保存几十 KB 内容。

Hermes 会：

1. 解析 arguments JSON。
2. 递归遍历 dict 和 list。
3. 将超过约 200 字符的字符串叶子截短并添加 `[truncated]`。
4. 重新序列化为合法 JSON。

不能直接截断 JSON 字符串，否则可能缺少引号或右括号，导致 provider 返回
不可恢复的 400，并在后续每轮重复发送同一条非法历史。

### 多模态内容

旧截图和 base64 图片会被替换为轻量文字：

```text
[screenshot removed to save context]
```

如果原消息包含文字说明，则尽量保留说明。压缩完成后还会再次扫描历史媒体，
避免近期尾部中的旧图片在多轮压缩后永久存活。

## 头部保护

Hermes 始终保护系统提示，并可通过 `protect_first_n` 固定保留最早若干条
非系统消息。

头部保护适合保存：

- 初始任务定义。
- 用户最早给出的关键约束。
- 第一轮确认和总体方案。

对于长期运行的会话，可以将 `protect_first_n` 调低到 0，只保留系统提示、
滚动摘要和近期上下文，避免过时的开场信息永久占用窗口。

## 近期尾部保护

Hermes 不只按固定消息数保留尾部，而是使用 token 预算：

```text
tail_token_budget = threshold_tokens * target_ratio
```

例如：

```text
上下文窗口：200K
压缩阈值：50%，即 100K
target_ratio：20%
近期尾部预算：20K tokens
```

同时使用 `protect_last_n` 作为最低数量保障。尾部边界还会调整以保证：

- 不从 tool result 中间切开。
- 最新用户消息一定在尾部。
- 最新助手回复一定在尾部。
- 至少留出有意义的中间区域供摘要。
- 单条超大消息可以在有限范围内突破预算。

## 中间消息摘要

位于受保护头部和近期尾部之间的消息会交给摘要模型。

摘要提示要求保留：

- 当前未完成任务。
- 已完成操作和具体结果。
- 文件路径、代码位置和修改内容。
- 执行过的命令、测试结果及错误。
- 已作出的决定和用户明确偏好。
- 未解决问题和下一步。

摘要应使用用户当前语言，并禁止保存 API key、Token、密码和连接字符串。
输入序列化和输出阶段都会进行敏感信息清理。

摘要预算根据被压缩内容规模调整，同时受模型上下文比例和绝对上限约束。

## 滚动摘要

Hermes 会保存上一轮压缩摘要。再次压缩时，不是从头生成一份彼此独立的摘要，
而是向摘要模型提供：

```text
上一轮摘要
+ 新产生、即将被压缩的中间消息
```

模型被要求：

- 保留仍然有效的旧信息。
- 添加新完成的操作。
- 将已解决事项从进行中移动到已完成。
- 更新当前状态和最新未完成请求。
- 仅删除明确过时的信息。

恢复会话时，Hermes 也能从消息中的压缩标记重新识别上一份摘要，避免仅依赖
进程内变量。

## 压缩结果组装

摘要消息带有专用 metadata，前端和持久化层可以识别它不是普通用户输入。

不同 provider 对消息角色顺序要求不同。Hermes 会根据头部末尾和尾部开头角色，
选择摘要使用 `user` 还是 `assistant`：

- 确保请求中至少存在一个 user 消息。
- 避免相邻两个相同角色。
- 如果两种角色都会冲突，则把摘要合并到第一条尾部消息。

摘要末尾添加明确边界，提醒模型：

```text
以上是历史摘要，只应作为参考；请响应摘要之后的当前消息。
```

这可以降低模型把摘要里的旧请求当作新指令，或把摘要内容原样复述给用户的概率。

组装完成后还会清理孤立的 tool call 和 tool result，保证 provider 不会收到
缺少配对 ID 的历史。

## 失败处理

摘要调用可能因鉴权、网络、限流或模型错误失败。Hermes 包含以下保护：

- 鉴权和网络错误默认中止压缩，原始消息保持不变。
- 可配置摘要失败时一律中止，不丢弃任何消息。
- 允许时可以生成本地确定性 fallback，保存文件路径、工具操作和错误等锚点。
- 摘要失败后进入冷却，避免每轮重复调用故障模型。
- 手动 `/compact` 可以强制跳过冷却并立即重试。
- 连续两次压缩节省不足 10% 时暂停自动压缩，避免抖动。
- 同一会话使用压缩锁，避免 CLI、gateway 等并发路径同时压缩并产生分叉。

## 手动压缩

Hermes 支持：

```text
/compact
/compact <focus>
```

`focus` 会让摘要将更多预算用于指定主题，例如：

```text
/compact 保留数据库迁移、失败测试和未提交改动
```

也支持按边界压缩，只摘要指定位置之前的历史并保留最近若干轮原文。

## 与 Qingzhou 当前实现的差异

Qingzhou 已经具备自动压缩、`/compact [focus]`、摘要消息写回 LangGraph state
和工具调用边界保护，但当前实现更轻量：

| 能力 | Hermes | Qingzhou 当前实现 |
| --- | --- | --- |
| 自动阈值 | 按模型窗口、输出预算和真实 usage | 固定窗口减固定 margin |
| 近期保护 | token 预算加消息数下限 | 工具裁剪使用 token 预算加消息数下限；摘要仍按固定消息数 |
| 工具输出预裁剪 | 专用规则、去重、参数裁剪、图片清理 | 已实现 Qingzhou 工具适配版本 |
| 滚动摘要 | 显式迭代更新上一摘要 | 依赖再次摘要已有消息 |
| 失败保护 | 冷却、中止、fallback、反抖动 | 失败计数后停用 |
| 并发压缩 | 会话级压缩锁 | 依赖 LangGraph 执行串行性 |
| 摘要边界 | metadata、角色适配和结束标记 | SystemMessage 摘要 |

## Qingzhou 建议采用的顺序

建议按收益和风险分阶段迁移：

1. 已增加旧工具结果的一行摘要、重复结果去重和大型调用参数裁剪。
2. 将固定 20 条尾部保护升级为 token 预算，并始终保留最新用户和助手消息。
3. 已移除可能直接丢失中间信息的轻量 snip，并用工具结果预裁剪替代。
4. 增加显式滚动摘要，将旧摘要作为独立输入交给摘要模型更新。
5. 增加敏感信息过滤、失败中止和冷却。
6. 最后再考虑压缩锁、provider 角色适配和本地 fallback 等复杂边界。

第一阶段已经完成，并通过 `tool_prune_metadata` 记录每次裁剪数量。

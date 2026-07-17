# Hermes 飞书支持与 Qingzhou 差距分析

更新日期：2026-07-17

本文记录 `hermes-agent` 对飞书的支持范围，并对比 `qingzhou-agent`
当前在飞书 IM 能力上的状态。Qingzhou 近期已经补齐了一批第一阶段能力，
因此本文按“已支持能力 / 剩余差距 / 后续优先级”重新整理。

## 参考位置

- Hermes 飞书适配器：`D:\ai\hermes-agent\plugins\platforms\feishu\adapter.py`
- Hermes 飞书插件清单：`D:\ai\hermes-agent\plugins\platforms\feishu\plugin.yaml`
- Hermes 飞书使用文档：`D:\ai\hermes-agent\website\docs\user-guide\messaging\feishu.md`
- Qingzhou 飞书适配器：`gateway/platforms/lark.py`
- Qingzhou 飞书测试：`tests/test_agent_lark.py`

## Hermes 的飞书能力

Hermes 的飞书支持是完整的平台插件，不只是简单 IM 收发。

### 接入方式

- 支持 WebSocket 长连接。
- 支持 Webhook 回调。
- Webhook 模式支持 challenge、加密签名、verification token、请求体大小限制、速率限制和异常统计。
- 支持国内飞书和国际 Lark 域名切换。

### IM 消息

- 支持私聊。
- 支持群聊。
- 支持群聊中按 `@bot` 触发。
- 支持按群配置是否需要 mention。
- 支持 bot-to-bot 策略，避免机器人之间互相触发。
- 支持用户 allowlist。
- 支持群策略：open、allowlist、disabled。

### 消息类型

Hermes 会把飞书消息规范化为统一内部结构，覆盖范围较广：

- text
- post 富文本
- mention / at / at all
- image
- file
- audio
- video / media
- merge forward
- share chat
- interactive card
- code block / markdown-like rich content

### 媒体处理

- 支持图片、文件、音频、视频、文档下载和缓存。
- 支持发送文本、图片、音频、视频、文档。
- 支持按扩展名选择飞书 image/file/media 上传方式。
- 支持 OPUS、MP3、WAV、M4A、AAC、FLAC、WEBM 等音频类型。

### 状态与可靠性

- 处理时添加 `Typing` reaction。
- 成功后移除处理中 reaction。
- 失败后添加 `CrossMark` reaction。
- 支持消息去重，并持久化到 Hermes home 目录，重启后仍能去重。
- 支持 per-chat 串行处理，避免同一会话并发乱序。
- 支持文本批处理和媒体批处理，把用户连续发送的多条消息合并处理。
- 支持发送重试、分片、延迟和长度限制。

### 上下文与会话

- 支持共享群聊按用户隔离 session。
- 支持通过 union_id / open_id / user_id 做更稳定的用户识别。
- 支持 chat 元信息、sender 名称缓存。
- 支持话题、thread、reply 上下文路由。

### 互动事件

- 支持 reaction 事件转成内部 synthetic text event。
- 支持互动卡片按钮点击转成 command。
- 支持消息已读、消息获取、聊天信息获取等辅助能力。

### 飞书生态能力

Hermes 还支持 IM 之外的飞书能力：

- 飞书文档读取。
- 飞书 Drive 评论读取、回复和新增评论。
- 文档评论智能回复。
- 会议邀请处理。
- cron / home channel 投递。

## Qingzhou 当前飞书 IM 能力

Qingzhou 当前仍是轻量 IM bot 定位，但第一阶段“做广”的能力已经明显增加。

### 接入与会话

- 支持飞书长连接接收消息。
- 支持私聊和群聊。
- 群聊默认必须 `@bot` 才响应。
- 群聊默认按 `chat_id + sender_id` 隔离上下文，避免多人共享同一个 thread。
- 支持通过 `LARK_GROUP_THREAD_SCOPE=chat` 或 `FEISHU_GROUP_THREAD_SCOPE=chat` 恢复按群共享上下文。
- 支持无 bot 身份配置时用原始飞书 mention token 兜底识别。
- 私聊默认直接响应。
- 支持同一 chat 串行执行，避免同一会话并发乱序。
- 支持连续消息合并窗口。
- 支持内存态消息去重。
- 支持 slash command，例如 `/clear`。

### 权限与安全

- 支持用户 allowlist：
  - `LARK_ALLOWED_USERS`
  - `FEISHU_ALLOWED_USERS`
- 支持飞书互动审批卡片。
- 受保护工具操作可以通过飞书卡片批准或拒绝。
- 审批按钮默认仅允许发起人操作，也支持审批操作者白名单：
  - `LARK_APPROVAL_ALLOWED_USERS`
  - `FEISHU_APPROVAL_ALLOWED_USERS`

### 输入消息

- 支持 text。
- 支持 post 富文本中的文本抽取。
- 支持 mention 解析。
- 支持 image 消息下载到 `.agent_uploads/lark/`。
- 支持 file 消息下载到 `.agent_uploads/lark/`。
- 支持 audio/media 消息下载并走 ASR 转写。
- 支持 TXT / Word / Excel / CSV 等文件识别，并把本地路径交给 agent。
- 支持通过 skill 脚本读取上传文档和表格：
  - `skills/document-reader/scripts/read_document.py`
  - `skills/spreadsheet-reader/scripts/read_spreadsheet.py`

### 输出消息

- 支持发送文本。
- 支持发送 markdown/interactive card。
- 支持长消息拆分发送。
- 支持工具生成的本地图片转成飞书图片消息。
- 支持 TTS 生成语音并通过飞书发送音频。
- 支持 Web 语音 marker 转成飞书音频消息。
- 支持自动语音回复模式。

### 媒体与语音

- 支持图片输入和图片输出。
- 支持普通文件输入。
- 支持飞书语音输入：下载音频 -> ASR server -> 转写文本进入 agent。
- 支持 Edge TTS 优先、System.Speech fallback 的语音合成。
- 支持 OPUS 转换后发送飞书音频。

## 能力矩阵

| 能力 | Hermes | Qingzhou 当前状态 |
| --- | --- | --- |
| WebSocket 长连接 | 支持 | 支持 |
| Webhook 回调 | 支持 | 未支持 |
| 私聊 | 支持 | 支持 |
| 群聊 | 支持 | 支持 |
| 群聊 @bot 触发 | 支持 | 支持，默认启用 |
| 用户 allowlist | 支持 | 支持 |
| 按群策略 | 支持 | 未支持 |
| bot-to-bot 策略 | 支持 | 未支持 |
| text/post | 支持 | 支持 |
| mention 解析 | 支持 | 基础支持 |
| image 输入 | 支持 | 支持 |
| file 输入 | 支持 | 支持 |
| audio 输入 | 支持 | 支持，走 ASR server |
| video/media 输入 | 支持 | 部分支持 audio/media；视频未作为一等能力 |
| TXT/Word/Excel 文件读取 | 支持 | 支持，走 skill scripts |
| 文本输出 | 支持 | 支持 |
| 图片输出 | 支持 | 支持本地图片转飞书图片 |
| 音频输出 | 支持 | 支持 TTS/OPUS |
| 文档输出 | 支持 | 未作为一等能力 |
| 消息合并 | 支持 | 支持 |
| per-chat 串行 | 支持 | 支持 |
| 持久化 dedup | 支持 | 未支持，当前为内存态 |
| 发送重试/异常统计 | 支持 | 基础日志，未系统化 |
| 互动卡片按钮 | 支持 | 支持审批卡片 |
| reaction 事件输入 | 支持 | 未支持 |
| 群聊按用户隔离上下文 | 支持 | 支持，默认 `chat_id + sender_id` |
| 飞书文档/Drive 评论/会议邀请 | 支持 | 未支持 |

## 仍然存在的主要差距

### 1. Webhook 模式

Hermes 同时支持 WebSocket 和 Webhook；Qingzhou 当前主要使用长连接。

短期个人使用场景里，长连接已经够用。Webhook 更适合后续部署到服务器、
接企业统一回调、安全校验、审计和网关场景时再补。

### 2. 按群策略与 bot-to-bot 策略

Qingzhou 已经支持“群聊必须 mention”和用户 allowlist，但还没有：

- 单群 open / allowlist / disabled 策略。
- 单群是否要求 mention 的覆盖配置。
- bot-to-bot 忽略策略。

如果机器人开始进入多个真实群聊，这部分会变得更重要。

### 3. 群聊上下文隔离

Hermes 支持共享群聊按用户隔离 session，避免同一个群里多个人共享上下文。

Qingzhou 现在也支持群聊上下文隔离。默认情况下，群聊 thread key 按下面的形式生成：

```text
chat_id + sender_id
```

私聊仍然按 `chat_id` 绑定 thread。如果确实希望一个群共享同一个上下文，可以设置：

```env
LARK_GROUP_THREAD_SCOPE=chat
```

或：

```env
FEISHU_GROUP_THREAD_SCOPE=chat
```

### 4. 消息规范化深度

Qingzhou 已经覆盖 text、post、mention、image、file、audio/media 的核心路径，
但复杂飞书消息仍然不完整：

- merge forward
- share chat
- code block / markdown-like rich content
- 更完整的 at all / mention metadata
- reply/thread/topic 路由

### 5. 输出能力

Qingzhou 当前重点支持文本、图片、音频。还缺：

- 视频输出。
- 普通文档作为飞书文件发送。
- 按扩展名自动选择 image/file/media 上传方式的完整封装。

### 6. 可靠性

Hermes 有持久化 dedup、发送重试、异常统计。Qingzhou 当前主要是：

- 进程内 seen message 去重。
- 基础日志。
- 单 chat 串行。

后端重启后，消息去重状态会丢失。对于长期在线 bot，建议补：

```text
.runtime/lark_seen_message_ids.json
```

或类似运行态存储。

### 7. 飞书生态集成

Hermes 已经把飞书文档、Drive 评论、会议邀请接入到 agent 事件体系。

Qingzhou 当前聚焦 IM。按照第一阶段“做广”的目标，可以暂时不追全量，
后续平台化时再接：

- 飞书文档读取。
- Drive 评论读取/回复。
- 会议邀请处理。
- home channel / cron 投递。

## 后续优先级

### P0：稳定现有 IM 能力

1. 持久化消息去重。
   - 存到 `.runtime/lark_seen_message_ids.json` 或类似运行态目录。
   - 重启后仍然避免重复处理。

2. 发送失败重试和更清晰日志。
   - 上传媒体失败时可定位原因。
   - 区分 token、权限、格式、资源不存在、转换失败等错误。

3. 文档/表格 skill scripts 打磨。
   - 当前已支持轻量读取。
   - 后续可补更强的表格摘要、sheet 选择、列筛选和大文件分块。

### P1：多人群聊体验

1. 单群策略。
   - open / mention_required / allowlist / disabled。
   - 支持按 chat_id 配置。

2. bot-to-bot 策略。
   - 默认忽略机器人发送者。
   - 避免多机器人互相触发。

### P2：平台化能力

1. 支持 Webhook 模式。
   - challenge。
   - encrypt key。
   - verification token。
   - rate limit。

2. 扩展互动卡片。
   - 当前已支持权限审批。
   - 后续可支持看板任务确认、继续/停止、重新生成、查看工具摘要。

3. 支持 home channel / cron 投递。
   - 定时任务结果发到指定飞书 chat。
   - 失败告警发到飞书。

4. 支持飞书生态能力。
   - 文档评论。
   - Drive 评论。
   - 会议邀请。

## 阶段结论

Hermes 的飞书能力仍然更完整，定位是平台插件。Qingzhou 当前仍是轻量 IM bot，
但第一阶段“做广”的核心 IM 能力已经补上很多：

- 群聊 mention 策略已支持。
- 用户 allowlist 已支持。
- 飞书语音输入已支持。
- TTS 语音回复已支持。
- 图片/文件输入已支持。
- TXT/Word/Excel 文件链路已支持。
- 互动审批卡片已支持。

因此，Qingzhou 目前在个人和小团队飞书 IM 场景里已经比较实用。下一步最值得做的
不是继续堆消息类型，而是提升稳定性和多人群聊体验：持久化 dedup、单群策略、
bot-to-bot 策略和更清晰的失败重试日志。

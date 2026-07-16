# Hermes 飞书支持与 Qingzhou 差距分析

生成日期：2026-07-16

本文记录 `hermes-agent` 对飞书的支持范围，并对比 `qingzhou-agent` 当前在飞书 IM 能力上的差距，方便后续按阶段补齐。

## 参考位置

- Hermes 飞书适配器：`D:\ai\hermes-agent\plugins\platforms\feishu\adapter.py`
- Hermes 飞书插件清单：`D:\ai\hermes-agent\plugins\platforms\feishu\plugin.yaml`
- Hermes 飞书使用文档：`D:\ai\hermes-agent\website\docs\user-guide\messaging\feishu.md`
- Qingzhou 飞书适配器：`gateway/platforms/lark.py`

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

Qingzhou 当前飞书能力更偏精简版 IM bot：

- 支持飞书长连接接收消息。
- 支持文本、post、图片、文件。
- 支持发送文本和飞书卡片。
- 支持长消息拆分。
- 支持连续消息合并窗口。
- 支持同一 chat 串行执行。
- 支持内存态消息去重。
- 支持 slash command，例如 `/clear`。
- 支持图片上传回复。
- 支持 TTS 生成语音并通过飞书发送音频。
- 支持语音回复模式。
- 支持工具生成的本地图片和音频资源转成飞书消息。

## 主要差距

### 1. 接入方式

Hermes 支持 WebSocket 和 Webhook 两种方式；Qingzhou 目前主要是长连接。

短期看，长连接已经能覆盖个人使用场景。Webhook 可以放到后续阶段，尤其是需要部署到服务器、接企业回调、安全校验和统一网关时再补。

### 2. 权限和群策略

Hermes 有完整的飞书访问策略：

- 用户白名单。
- 群聊策略。
- 是否必须 mention。
- bot-to-bot 策略。
- 单群规则覆盖。

Qingzhou 目前这块较弱。后续如果进入真实群聊，优先补 `LARK_ALLOWED_USERS`、`LARK_REQUIRE_MENTION`、`LARK_GROUP_POLICY` 会比较有价值。

### 3. 群聊上下文隔离

Hermes 支持群聊按用户隔离 session，避免同一个群里多个人共享同一段上下文。

Qingzhou 当前更偏按 chat 维度绑定 thread。多人群里，如果用户 A 和用户 B 交替提问，可能会共享上下文，影响回答准确性。

### 4. 消息规范化

Hermes 的消息 normalize 覆盖范围更完整，包括 mention、富文本、媒体、转发、分享、互动卡片等。

Qingzhou 当前解析更轻量，足够覆盖基础文本、图片、文件和语音回复，但对复杂飞书消息类型的理解还不完整。

### 5. 媒体输入

Hermes 已覆盖图片、文件、音频、视频、文档等输入。

Qingzhou 已支持图片和文件输入，TTS 音频输出也已经可用。语音输入是下一步最值得补的能力：用户发飞书语音后，下载音频，走 ASR，再进入 agent。

### 6. 媒体输出

Hermes 支持文本、图片、音频、视频、文档输出。

Qingzhou 当前重点支持文本、图片、音频。视频、普通文档上传回复还不是一等能力。

### 7. 可靠性

Hermes 有持久化 dedup、批处理、重试、异常统计。

Qingzhou 当前主要是进程内状态。后端重启后，消息去重状态会丢失。对个人使用影响不大，但对长期在线 bot 来说，持久化 dedup 很有必要。

### 8. 互动卡片

Hermes 可以处理互动卡片按钮事件，并转成内部 command。

Qingzhou 目前主要是发送 markdown/card，不处理卡片按钮回调。后续可以用于审批、任务确认、看板操作等场景。

### 9. 飞书生态集成

Hermes 已经把飞书文档、Drive 评论、会议邀请接入到 agent 事件体系。

Qingzhou 当前主要聚焦 IM。按照“第一阶段做广”的目标，可以先保留为后续扩展方向，不必马上追全量。

## 建议优先级

### P0：低成本、体验收益明显

1. 支持飞书语音输入。
   - 下载飞书语音资源。
   - 复用现有 ASR server。
   - 把转写文本作为用户消息进入 agent。

2. 增加群聊 mention 策略。
   - 默认群聊必须 `@bot` 才响应。
   - 私聊仍然直接响应。

3. 增加用户 allowlist。
   - 避免 bot 被无关用户或群误用。

4. 持久化消息去重。
   - 存到 `.runtime/lark_seen_message_ids.json` 或类似运行态目录。
   - 重启后仍然避免重复处理。

### P1：多人群聊体验

1. 群聊按用户隔离 thread。
   - thread key 从 `chat_id` 调整为 `chat_id + user_id`。
   - 可通过配置开关控制。

2. 完善 post / mention 解析。
   - 正确识别 `@bot`。
   - 保留 mention 目标。
   - 更好地渲染飞书富文本。

3. 增加发送失败重试和更清晰日志。
   - 上传媒体失败时可定位原因。
   - 区分 token、权限、格式、资源不存在等错误。

### P2：平台化能力

1. 支持 Webhook 模式。
   - challenge。
   - encrypt key。
   - verification token。
   - rate limit。

2. 支持互动卡片按钮事件。
   - 看板任务确认。
   - 权限审批。
   - 工具执行确认。

3. 支持 home channel / cron 投递。
   - 定时任务结果发到指定飞书 chat。
   - 失败告警发到飞书。

4. 支持文档评论、Drive 评论、会议邀请。
   - 这部分属于飞书生态集成，不是 IM 核心路径。

## 阶段结论

Hermes 的飞书能力更完整，定位是平台插件；Qingzhou 当前是轻量 IM bot。

如果目标是第一阶段“做广”，Qingzhou 不需要一次追齐 Hermes。最值得先补的是：

1. 飞书语音输入。
2. 群聊 mention 策略。
3. 用户 allowlist。
4. 持久化 dedup。
5. 群聊按用户隔离上下文。

这几项完成后，Qingzhou 在个人和小团队飞书 IM 场景里就会比较实用。Webhook、互动卡片、文档评论、会议邀请可以放到第二阶段平台化时再做。

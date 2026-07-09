# Telegram 接入

`xu-agent` 通过 Telegram 官方 Bot API 提供私聊和群聊机器人能力，默认使用
长轮询，不需要公网地址。

## 创建机器人

1. 在 Telegram 中打开 `@BotFather`。
2. 发送 `/newbot`。
3. 按提示设置名称和以 `bot` 结尾的用户名。
4. 保存 BotFather 返回的 Bot Token。

可以通过 `@userinfobot` 查询自己的数字用户 ID。

## 配置

在配置页面的 `Telegram` 项中填写：

- `Bot Token`：BotFather 生成的 Token。
- `Allowed User IDs`：允许访问的用户 ID，多个 ID 用逗号分隔；留空表示不限制。
- `Require group mention`：群聊中是否必须回复机器人或 `@机器人`。
- `Merge Wait`：合并同一聊天连续消息的静默等待时间。

也可以使用项目根目录 `.env`：

```env
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456789:replace-with-token
TELEGRAM_ALLOWED_USERS=123456789
TELEGRAM_REQUIRE_MENTION=true
TELEGRAM_MERGE_WAIT_SECONDS=3
```

保存配置后重启后端。

## 消息能力

- 私聊和群聊文本。
- `/help`、`/clear`、`/compact`。
- 连续消息合并。
- 图片与文档下载到 `.agent_uploads/telegram/`。
- 将 Agent 各轮可见 AI 文本按顺序发送，不发送工具调用和工具结果。
- 按 Telegram chat ID 隔离 LangGraph thread 和历史消息。

Telegram 群组默认启用 Privacy Mode。若机器人需要接收普通群消息，需要通过
BotFather 关闭 Group Privacy，或把机器人设为群管理员。

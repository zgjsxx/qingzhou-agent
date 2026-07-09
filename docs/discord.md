# Discord 接入

`xu-agent` 通过 Discord 官方 Gateway 接入机器人消息，使用 `discord.py`
长连接运行，不需要公网 webhook。

## 创建机器人

1. 打开 Discord Developer Portal。
2. 创建 Application，并在 `Bot` 页面创建机器人。
3. 复制 Bot Token。
4. 在 `Privileged Gateway Intents` 中打开 `Message Content Intent`。
5. 在 `OAuth2 -> URL Generator` 中选择 `bot` scope，并至少勾选：
   - `Send Messages`
   - `Read Message History`
   - `View Channels`
6. 用生成的邀请链接把机器人加入服务器。

## 配置

可以在配置页面的 `Discord` 项中填写：

- `Bot Token`：Discord Developer Portal 生成的 token。
- `Allowed User IDs`：允许访问的 Discord 用户 ID，多个 ID 用逗号分隔；留空表示不限制。
- `Proxy`：可选代理地址，例如 `http://127.0.0.1:7890`，用于网络环境中 Discord REST 请求不稳定时发送回复。
- `Require server mention`：服务器频道里是否必须 `@机器人` 或回复机器人。
- `Merge Wait`：合并同一频道连续消息的静默等待时间。

也可以使用项目根目录 `.env`：

```env
DISCORD_ENABLED=true
DISCORD_BOT_TOKEN=replace-with-discord-bot-token
DISCORD_ALLOWED_USERS=
DISCORD_REQUIRE_MENTION=true
DISCORD_MERGE_WAIT_SECONDS=3
DISCORD_PROXY=http://127.0.0.1:7890
```

保存配置后重启后端。

## 消息能力

- 私信和服务器频道文本消息。
- `/help`、`/clear`、`/compact`。
- 连续消息合并。
- 附件下载到 `.agent_uploads/discord/`。
- 将 Agent 各轮可见 AI 文本按顺序发送，不发送工具调用和工具结果。
- 按 Discord channel ID 隔离 LangGraph thread 和历史消息。

服务器频道默认只处理 `@机器人`、回复机器人或 `/` 开头的消息，避免普通频道聊天全部进入 Agent。

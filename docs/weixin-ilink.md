# 微信 iLink 接入

`xu-agent` 可以通过腾讯 iLink Bot API 接收个人微信中的机器人私聊消息。
当前版本支持文本单聊，不支持普通微信群、图片、语音和文件。

## 安装

进入后端目录并安装依赖：

```powershell
cd backend
python -m pip install -r requirements.txt
```

## 扫码登录

运行登录脚本：

```powershell
python scripts/weixin_login.py
```

使用个人微信扫描终端二维码并在手机中确认。登录成功后，机器人账号和
Token 会保存到：

```text
config/weixin/account.json
```

该目录已加入 `.gitignore`，不能提交或分享其中的 Token。

## 启用

可以在配置页面的 `Weixin` 项中打开开关，也可以在项目根目录 `.env` 中设置：

```env
WEIXIN_ENABLED=true
```

重启后端后，`agent.py` 会启动 iLink 长轮询线程。每个微信用户使用独立的
`weixin_<user_id>` LangGraph thread，并复用 `/help`、`/clear` 和 `/compact`
命令。

## 运行数据

以下文件均保存在 `config/weixin/`：

- `account.json`：机器人账号和 Token。
- `sync.json`：`getupdates` 长轮询游标。
- `context-tokens.json`：按用户保存的回复上下文 Token。

同一 Token 同一时间只应由一个后端实例轮询。登录失效后，需要重新运行扫码
登录脚本。

旧版本的 `.weixin/` 数据会在首次启动或登录时自动迁移到新目录。

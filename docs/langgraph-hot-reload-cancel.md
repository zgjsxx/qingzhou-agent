# LangGraph hot-reload cancel 问题

**日期**：2026-06-23

## Root cause

Agent 的 `write_file` 工具写入 `.py` 文件到 backend 目录下时，LangGraph dev server 的 WatchFiles 误判为"源码变更"，触发 reload 并强制 cancel 正在运行的 agent task（CancelledError），导致前端对话中断且日志停在 `model.start` 没有 `model.end`。

## 事件链路

1. Agent 调用 `write_file` 工具，写入了 `output\pdf\gaote_extract.py`（一个 `.py` 文件）
2. LangGraph dev server 的 `WatchFiles` 监控到 backend 目录下 `.py` 文件变化，判定为"源码修改"
3. 触发 server reload → 调用 `Shutting down background workers`
4. 正在执行的 agent run 被强制 cancel，抛出 `CancelledError`
5. LangGraph worker 认为这是"失败"，尝试 retry

## 关键日志证据

- `run_wait_time_ms=414564`（等了 ~7 分钟才开始执行）— 多次 reload 导致排队
- `run_exec_ms=100461`（执行了 ~100 秒就被 cancel）— 不是模型超时，是被 reload 杀掉
- agent.jsonl 停在 `model.start` 没有 `model.end` — 不是 API 挂住，是请求被 cancel

## 附带问题

模型 API 调用没有 timeout 配置，挂住时不抛异常，中间件无法记录 `model.error`。`CancelledError` 不经过中间件的 try/except，所以也不会记录。这两个问题叠加才会出现"日志停在 model.start 无后续"的现象。

## 修复方向

让 WatchFiles 忽略 agent 工具写入的目录（`output/`、`tmp/`），或在 langgraph dev 的 `--exclude` 配置中排除这些路径。

# xu-agent Agent Notes

## Overview

`xu-agent` is a LangGraph-based personal assistant.

- Backend graph entry: `backend/src/agent.py`
- Tool definitions: `backend/src/tools.py`
- Agent interaction logging: `backend/src/agent_logging.py`
- LangGraph dev config: `backend/langgraph.json`
- Frontend chat UI: `frontend/src`

The backend exposes the graph named `agent` through LangGraph Server on
`http://localhost:2024`. The frontend connects to it from the Next.js app on
`http://localhost:3000`.

## Model Configuration

The model is configured from backend environment variables:

- `LLM_ADAPTER_TYPE`: provider prefix, for example `anthropic` or `openai`
- `LLM_MODEL`: model name, for example `glm-5.1`
- `LLM_API_KEY`: API key
- `LLM_BASE_URL`: provider base URL

`backend/src/agent.py` maps the project-level `LLM_*` variables to the
provider-specific variables that LangChain expects:

- `anthropic` -> `ANTHROPIC_API_KEY`, `ANTHROPIC_API_URL`
- `openai` -> `OPENAI_API_KEY`, `OPENAI_BASE_URL`

The final model string is built as:

```text
{LLM_ADAPTER_TYPE}:{LLM_MODEL}
```

## Tools

The current tool list is defined by `ALL_TOOLS` in `backend/src/tools.py`.

### `get_system_cpu_usage`

Returns the host's total CPU usage percentage.

- Windows: tries `typeperf` first, then PowerShell `Get-Counter`
- Linux-like systems: samples `/proc/stat`
- Sampling interval is clamped to `1..10` seconds

### `run_shell_command`

Runs a shell command on the backend host.

Supported shells:

- `auto`
- `powershell`
- `cmd`
- `bash`
- `sh`

Important behavior:

- Default timeout is 30 seconds.
- Timeout is clamped to `1..120` seconds.
- Timed-out commands kill the process tree.
- Output is decoded as UTF-8 with replacement for invalid bytes.
- Output is truncated by `SHELL_TOOL_MAX_OUTPUT_CHARS`.
- Broad recursive scans from drive roots are blocked, for example
  `Get-ChildItem D:\ -Recurse` or `dir D:\ /s`.

## Logging

`AgentLoggingMiddleware` writes JSONL logs to:

```text
backend/logs/agent.jsonl
```

Logged events include:

- `agent.start`
- `agent.end`
- `model.start`
- `model.end`
- `model.error`
- `tool.start`
- `tool.end`
- `tool.error`

Environment variables:

- `AGENT_LOG_DIR`
- `AGENT_LOG_MAX_BYTES`
- `AGENT_LOG_BACKUP_COUNT`

The `backend/logs/` directory is ignored by git.

## Frontend Streaming

The frontend uses `@langchain/langgraph-sdk/react`.

Current submissions set:

```ts
streamResumable: false
```

This is intentional. Earlier, resumable streams caused unfinished old runs to
resume after restarting the backend, which made the system execute previous
tasks automatically.

The frontend still stores the active `threadId` in the URL. If the browser URL
contains `threadId=...`, the UI will reconnect to that thread and fetch its
state history.

## LangGraph Dev Persistence

LangGraph dev stores local state in:

```text
backend/.langgraph_api/
```

Important files include:

- `.langgraph_ops.pckl`
- `.langgraph_checkpoint.*.pckl`
- `.langgraph_retry_counter.pckl`

If old tasks run again after restarting the backend, inspect this directory.
It may contain `running` runs from previous sessions.

To reset local LangGraph dev state:

1. Stop the backend process.
2. Move `backend/.langgraph_api/` to a backup location or delete it.
3. Remove `threadId=...` from the browser URL or start a new chat.
4. Restart the backend.

Prefer moving the directory first, for example:

```powershell
Move-Item backend\.langgraph_api backend\.langgraph_api.backup
```

## Operational Notes

- `Ctrl+C` may stop the LangGraph wrapper but leave worker/tool child processes.
- If the backend appears stuck, check:
  - port `2024`
  - `langgraph.exe`
  - `python.exe` from the `lcchat` environment
  - high-CPU `powershell.exe`, `cmd.exe`, or `Robocopy.exe`
- The most useful debug file is `backend/logs/agent.jsonl`.
- If `agent.jsonl` shows a `tool.start` without a matching `tool.end`, the
  current run is waiting on that tool.


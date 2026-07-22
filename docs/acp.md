# ACP Adapter

qingzhou-agent includes a minimal Agent Client Protocol v1 adapter. It exposes
the existing LangGraph backend through ACP stdio so editor clients can launch it
as an agent subprocess.

## Start

Start qingzhou-agent normally first:

```powershell
.\start.ps1
```

Then configure the ACP client to launch:

```powershell
D:\ai\qingzhou-agent\.venv\Scripts\python.exe D:\ai\qingzhou-agent\agent\acp.py
```

By default the adapter connects to the backend address derived from
`config/qingzhou-agent.json` field `server.backendPort`.

Override this when needed:

```powershell
D:\ai\qingzhou-agent\.venv\Scripts\python.exe D:\ai\qingzhou-agent\agent\acp.py --api-url http://127.0.0.1:<backendPort> --assistant-id agent
```

Do not `cd` into `D:\ai\qingzhou-agent` when launching from an editor. The
adapter uses the ACP `session/new.cwd` value when the client provides it, and
otherwise falls back to the process current directory. Launching the script by
absolute path lets VSCode keep the current project as the working directory.

If a client cannot launch with the workspace as process cwd and does not send
`session/new.cwd`, pass the workspace explicitly as a fallback:

```powershell
D:\ai\qingzhou-agent\.venv\Scripts\python.exe D:\ai\qingzhou-agent\agent\acp.py --workspace-dir D:\ems\code-new\ems-server
```

Environment variables are also supported:

```text
QINGZHOU_ACP_API_URL
LANGGRAPH_API_URL
QINGZHOU_ACP_ASSISTANT_ID
QINGZHOU_ACP_WORKSPACE_DIR
```

## Supported MVP Surface

- `initialize`
- `session/new`
- `session/prompt`
- `session/cancel` as a no-op notification
- `session/update` notifications for assistant text chunks

The adapter currently supports text prompts and resource links. Tool calls,
client-side filesystem operations, terminal integration, permission mapping,
session loading, and rich artifacts are intentionally left for later versions.

## VSCode

No VSCode extension is required for the first integration pass. Any ACP client
that can launch a stdio agent process can run the adapter command above. A
dedicated VSCode extension only becomes necessary when qingzhou-agent needs
workspace-aware UI features such as current file context, inline edits, diff
views, terminal approval, or sidebar session management.

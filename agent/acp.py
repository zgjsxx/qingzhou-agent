"""Minimal Agent Client Protocol adapter for qingzhou-agent.

This module exposes qingzhou-agent as an ACP v1 stdio subprocess. It is a thin
adapter over the existing LangGraph HTTP backend; start the normal backend
first, then launch this process from an ACP client.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_AGENT_DIR = Path(__file__).resolve().parent
sys.path = [entry for entry in sys.path if Path(entry or os.getcwd()).resolve() != _AGENT_DIR]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import argparse
import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, TextIO

from langgraph_sdk import get_client


PROTOCOL_VERSION = 1
DEFAULT_API_URL = "http://127.0.0.1:2024"
DEFAULT_ASSISTANT_ID = "agent"


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass
class AcpSession:
    session_id: str
    thread_id: str
    cwd: str


class LangGraphBridge:
    def __init__(self, api_url: str, assistant_id: str):
        self.api_url = api_url
        self.assistant_id = assistant_id
        self.client = get_client(url=api_url)

    async def create_thread(self, cwd: str) -> str:
        thread = await self.client.threads.create(
            metadata={"source": "acp", "cwd": cwd},
        )
        return str(thread["thread_id"])

    async def aclose(self) -> None:
        close = getattr(self.client, "aclose", None)
        if close is not None:
            await close()

    async def stream_prompt(self, thread_id: str, prompt: str, cwd: str) -> AsyncIterator[str]:
        baseline_id, baseline_text = await self._latest_assistant_before_run(thread_id)
        last_text = ""
        async for part in self.client.runs.stream(
            thread_id,
            self.assistant_id,
            input={"messages": [{"role": "user", "content": prompt}]},
            stream_mode=["values"],
            stream_subgraphs=True,
            stream_resumable=False,
            metadata={"source": "acp", "cwd": cwd},
            multitask_strategy="reject",
        ):
            event, data = _stream_part_event_data(part)
            if event and event not in {"values"}:
                continue
            message_id, text = _latest_assistant_message(data)
            if message_id and message_id == baseline_id:
                continue
            if not message_id and text == baseline_text:
                continue
            if not text or text == last_text:
                continue
            if text.startswith(last_text):
                delta = text[len(last_text) :]
            else:
                delta = text
            last_text = text
            if delta:
                yield delta

    async def _latest_assistant_before_run(self, thread_id: str) -> tuple[str | None, str]:
        try:
            state = await self.client.threads.get_state(thread_id)
        except Exception:  # noqa: BLE001 - best-effort baseline only.
            return None, ""
        values = state.get("values") if isinstance(state, dict) else getattr(state, "values", None)
        return _latest_assistant_message(values)


class AcpServer:
    def __init__(self, bridge: LangGraphBridge, workspace_dir: str | None = None):
        self.bridge = bridge
        self.workspace_dir = workspace_dir
        self.sessions: dict[str, AcpSession] = {}

    async def handle(self, message: dict[str, Any], writer: "JsonRpcWriter") -> None:
        msg_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}

        if not method:
            if msg_id is not None:
                writer.error(msg_id, -32600, "Invalid Request")
            return

        try:
            result = await self._dispatch(method, params, writer)
        except JsonRpcError as exc:
            if msg_id is not None:
                writer.error(msg_id, exc.code, exc.message, exc.data)
            return
        except Exception as exc:  # noqa: BLE001 - JSON-RPC boundary.
            if msg_id is not None:
                writer.error(msg_id, -32603, str(exc))
            return

        if msg_id is not None:
            writer.response(msg_id, result)

    async def _dispatch(self, method: str, params: dict[str, Any], writer: "JsonRpcWriter") -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "session/new":
            return await self._new_session(params)
        if method == "session/prompt":
            return await self._prompt(params, writer)
        if method == "session/cancel":
            return None
        raise JsonRpcError(-32601, f"Method not found: {method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": False,
                "promptCapabilities": {
                    "image": False,
                    "audio": False,
                    "embeddedContext": False,
                },
                "mcpCapabilities": {
                    "http": False,
                    "sse": False,
                },
                "sessionCapabilities": {},
                "auth": {},
            },
            "agentInfo": {
                "name": "qingzhou-agent",
                "title": "Qingzhou Agent",
                "version": _package_version(),
            },
            "authMethods": [],
        }

    async def _new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = str(params.get("cwd") or self.workspace_dir or os.getcwd())
        cwd_path = Path(cwd).expanduser()
        if not cwd_path.is_absolute():
            raise JsonRpcError(-32602, "session/new cwd must be an absolute path")
        thread_id = await self.bridge.create_thread(str(cwd_path))
        session_id = f"acp_{thread_id}"
        self.sessions[session_id] = AcpSession(
            session_id=session_id,
            thread_id=thread_id,
            cwd=str(cwd_path),
        )
        return {"sessionId": session_id}

    async def _prompt(self, params: dict[str, Any], writer: "JsonRpcWriter") -> dict[str, Any]:
        session_id = str(params.get("sessionId") or "")
        session = self.sessions.get(session_id)
        if session is None:
            raise JsonRpcError(-32602, f"unknown ACP session: {session_id}")

        prompt = _content_blocks_to_text(params.get("prompt") or [])
        if not prompt.strip():
            raise JsonRpcError(-32602, "session/prompt requires non-empty text content")

        message_id = f"msg_{uuid.uuid4().hex}"
        if prompt.strip() in {"/clear", "/reset"}:
            session.thread_id = await self.bridge.create_thread(session.cwd)
            writer.notification(
                "session/update",
                {
                    "sessionId": session.session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "messageId": message_id,
                        "content": {"type": "text", "text": "Context cleared."},
                    },
                },
            )
            return {"stopReason": "end_turn"}

        try:
            bridged_prompt = _with_workspace_context(prompt, session.cwd)
            async for chunk in self.bridge.stream_prompt(session.thread_id, bridged_prompt, session.cwd):
                writer.notification(
                    "session/update",
                    {
                        "sessionId": session.session_id,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "messageId": message_id,
                            "content": {"type": "text", "text": chunk},
                        },
                    },
                )
        except asyncio.CancelledError:
            return {"stopReason": "cancelled"}

        return {"stopReason": "end_turn"}


class JsonRpcWriter:
    def __init__(self, output: TextIO):
        self.output = output

    def response(self, msg_id: Any, result: Any) -> None:
        self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def error(self, msg_id: Any, code: int, message: str, data: Any | None = None) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._write({"jsonrpc": "2.0", "id": msg_id, "error": error})

    def notification(self, method: str, params: Any) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, message: dict[str, Any]) -> None:
        self.output.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.output.flush()


def _content_blocks_to_text(blocks: Any) -> str:
    if not isinstance(blocks, list):
        raise JsonRpcError(-32602, "prompt must be a list of content blocks")

    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append(str(block.get("text") or ""))
        elif block_type == "resource_link":
            uri = block.get("uri")
            name = block.get("name") or block.get("title") or uri
            if uri:
                parts.append(f"[Resource: {name}]\n{uri}")
        elif block_type == "resource":
            resource = block.get("resource")
            if isinstance(resource, dict) and resource.get("text"):
                uri = resource.get("uri")
                header = f"[Resource: {uri}]\n" if uri else "[Resource]\n"
                parts.append(header + str(resource["text"]))
    return "\n\n".join(part for part in parts if part).strip()


def _with_workspace_context(prompt: str, cwd: str) -> str:
    return (
        "<acp_context>\n"
        "This context is for tool routing only. Do not summarize it, quote it, or answer about it unless the user asks "
        "which directory or workspace is active.\n"
        f"VSCode workspace cwd: {cwd}\n"
        "When using file or shell tools, pass this cwd explicitly unless the user asks for another directory.\n"
        "The qingzhou-agent installation directory is not the user's project workspace.\n"
        "</acp_context>\n\n"
        f"User request:\n{prompt}"
    )


def _stream_part_event_data(part: Any) -> tuple[str | None, Any]:
    if isinstance(part, tuple) and len(part) >= 2:
        return str(part[0]), part[1]
    if isinstance(part, dict):
        return part.get("event"), part.get("data", part)
    event = getattr(part, "event", None)
    data = getattr(part, "data", None)
    return event, data


def _latest_assistant_message(data: Any) -> tuple[str | None, str]:
    if not isinstance(data, dict):
        return None, ""
    messages = data.get("messages")
    if not isinstance(messages, list):
        return None, ""
    for message in reversed(messages):
        role = _message_role(message)
        if role not in {"assistant", "ai"}:
            continue
        return _message_id(message), _message_content_text(message)
    return None, ""


def _latest_assistant_text(data: Any) -> str:
    return _latest_assistant_message(data)[1]


def _message_id(message: Any) -> str | None:
    if isinstance(message, dict):
        value = message.get("id")
    else:
        value = getattr(message, "id", None)
    return str(value) if value else None


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or message.get("type") or "")
    return str(getattr(message, "role", None) or getattr(message, "type", None) or "")


def _message_content_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and item.get("text"):
                    parts.append(str(item["text"]))
                elif item.get("content"):
                    parts.append(str(item["content"]))
        return "".join(parts)
    return str(content or "")


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("xu-agent")
    except Exception:  # noqa: BLE001 - version metadata may be absent in dev.
        return "0.0.0"


def _configure_standard_streams() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


async def serve(
    input_stream: TextIO,
    output_stream: TextIO,
    bridge: LangGraphBridge,
    workspace_dir: str | None = None,
) -> None:
    server = AcpServer(bridge, workspace_dir=workspace_dir or os.getenv("QINGZHOU_ACP_WORKSPACE_DIR"))
    writer = JsonRpcWriter(output_stream)

    try:
        for line in input_stream:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("message must be an object")
            except Exception as exc:  # noqa: BLE001 - JSON-RPC parse boundary.
                writer.error(None, -32700, f"Parse error: {exc}")
                continue
            await server.handle(message, writer)
    finally:
        await bridge.aclose()


def main(argv: list[str] | None = None) -> int:
    _configure_standard_streams()

    parser = argparse.ArgumentParser(description="Run qingzhou-agent as an ACP stdio agent.")
    parser.add_argument("--api-url", default=os.getenv("QINGZHOU_ACP_API_URL") or os.getenv("LANGGRAPH_API_URL") or DEFAULT_API_URL)
    parser.add_argument("--assistant-id", default=os.getenv("QINGZHOU_ACP_ASSISTANT_ID") or DEFAULT_ASSISTANT_ID)
    parser.add_argument("--workspace-dir", default=os.getenv("QINGZHOU_ACP_WORKSPACE_DIR"))
    args = parser.parse_args(argv)

    bridge = LangGraphBridge(args.api_url, args.assistant_id)
    asyncio.run(serve(sys.stdin, sys.stdout, bridge, workspace_dir=args.workspace_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

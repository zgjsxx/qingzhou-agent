"""Slash commands and their interception middleware.

Provides:
- Deterministic command parsing (/help, /clear, /compact)
- AgentCommandMiddleware that intercepts commands before the model runs
- Thread-level command handling for non-LLM entry points (Lark, etc.)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command

from agent_context import manual_compact_state, manual_acompact_state, _compact_failure_update
from agent_logging import log_event

# ---------------------------------------------------------------------------
# Command constants & responses
# ---------------------------------------------------------------------------

CLEAR_COMMAND = "/clear"
CLEAR_RESPONSE = "当前会话上下文已清除。"
HELP_COMMAND = "/help"
COMPACT_COMMAND = "/compact"
HELP_RESPONSE = """可用斜杠命令：

- `/help`：查看当前支持的命令。
- `/clear`：清除当前 thread 的消息、token 使用量和上下文压缩状态。
- `/compact [focus]`：手动压缩当前 thread 的历史上下文；可选 focus 用来提示摘要时优先保留哪些信息。"""

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SlashCommand:
    """Parsed slash command from the latest user message."""

    name: str
    raw: str
    args: str = ""
    message_id: str | None = None


@dataclass(frozen=True)
class SlashCommandResult:
    """Result of handling a slash command before an agent run."""

    command: SlashCommand
    response: str
    clear_history: bool = False

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def message_text(message: Any) -> str:
    """Extract comparable text from LangChain or plain message-like objects."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
            else:
                text = getattr(block, "text", None)
                parts.append(str(text if text is not None else block))
        return "\n".join(parts)
    return str(content)


def parse_slash_command_text(text: str) -> SlashCommand | None:
    """Parse a supported slash command from plain text.

    Only exact command names are accepted for no-argument commands. `/compact`
    accepts optional trailing focus text.
    """
    candidate = text.strip()
    lowered = candidate.lower()
    if lowered == HELP_COMMAND:
        return SlashCommand(name=HELP_COMMAND, raw=candidate)
    if lowered == CLEAR_COMMAND:
        return SlashCommand(name=CLEAR_COMMAND, raw=candidate)
    if lowered == COMPACT_COMMAND or lowered.startswith(f"{COMPACT_COMMAND} "):
        return SlashCommand(name=COMPACT_COMMAND, raw=candidate, args=candidate[len(COMPACT_COMMAND) :].strip())
    return None


def parse_slash_command_messages(messages: list[Any]) -> SlashCommand | None:
    """Parse a command from the last human message.

    AgentMemoryMiddleware may prepend memory text to the final request message.
    Therefore we try both the full message content and its last non-empty line,
    while still requiring the command to be in the latest human message only.
    """
    if not messages:
        return None
    last = messages[-1]
    if not (getattr(last, "type", None) == "human" or last.__class__.__name__ == "HumanMessage"):
        return None

    text = message_text(last).strip()
    candidates = [text]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        candidates.append(lines[-1])

    for candidate in candidates:
        command = parse_slash_command_text(candidate)
        if command:
            return SlashCommand(
                name=command.name,
                raw=command.raw,
                args=command.args,
                message_id=getattr(last, "id", None),
            )
    return None

# ---------------------------------------------------------------------------
# Utility predicates
# ---------------------------------------------------------------------------

def is_clear_command(text: str) -> bool:
    """Return whether text is exactly the clear-context command."""
    command = parse_slash_command_text(text)
    return bool(command and command.name == CLEAR_COMMAND)


def is_help_command(text: str) -> bool:
    """Return whether text is exactly the help command."""
    command = parse_slash_command_text(text)
    return bool(command and command.name == HELP_COMMAND)

# ---------------------------------------------------------------------------
# Thread-level command helpers (used by Lark bridge etc.)
# ---------------------------------------------------------------------------

def clear_context_update() -> dict[str, Any]:
    """Build a state update that clears conversation and compaction state."""
    return {
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES, content="")],
        "context_usage": {},
        "compact_metadata": {},
        "snip_compact_metadata": {},
        "compact_failure_count": 0,
    }


def clear_thread_context(graph: Any, thread_id: str, *, source: str) -> bool:
    """Clear the latest state for a thread when the graph has a checkpointer."""
    config = {"configurable": {"thread_id": thread_id}}
    try:
        graph.update_state(config, clear_context_update())
    except ValueError as exc:
        if "checkpointer" not in str(exc).lower():
            raise
        log_event("command.clear", source=source, thread_id=thread_id, checkpoint_updated=False)
        return False

    log_event("command.clear", source=source, thread_id=thread_id, checkpoint_updated=True)
    return True


def handle_thread_slash_command(text: str, graph: Any, thread_id: str, *, source: str) -> SlashCommandResult | None:
    """Handle deterministic commands that do not need to enter the agent graph.

    `/compact` intentionally returns None here because it needs the current graph
    state and is dispatched inside AgentCommandMiddleware by the same
    parsed command model.
    """
    command = parse_slash_command_text(text)
    if not command:
        return None

    if command.name == HELP_COMMAND:
        log_event("command.help", source=source, thread_id=thread_id)
        return SlashCommandResult(command=command, response=HELP_RESPONSE)

    if command.name == CLEAR_COMMAND:
        try:
            clear_thread_context(graph, thread_id, source=source)
            return SlashCommandResult(command=command, response=CLEAR_RESPONSE, clear_history=True)
        except Exception as exc:
            log_event(
                "command.error",
                command=CLEAR_COMMAND,
                source=source,
                thread_id=thread_id,
                error=repr(exc),
            )
            return SlashCommandResult(command=command, response=f"清除会话上下文失败：{exc}")

    return None

# ---------------------------------------------------------------------------
# Agent middleware helpers (private)
# ---------------------------------------------------------------------------

def _state_value(state: Any, key: str, default: Any = None) -> Any:
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def _state_has_slash_command(state: Any) -> bool:
    return parse_slash_command_messages(list(_state_value(state, "messages", []) or [])) is not None


def _agent_slash_command_request(request: ModelRequest) -> tuple[SlashCommand, list[Any]] | None:
    state_messages = list(_state_value(request.state, "messages", []) or [])
    request_messages = list(request.messages or [])
    state_command = parse_slash_command_messages(state_messages)
    request_command = parse_slash_command_messages(request_messages)
    command = state_command or request_command
    if not command:
        return None

    # 优先以 state 中真实落库的最后一条用户消息为准；request.messages 可能被其它 middleware 注入记忆文本，
    # 适合作为识别兜底，但不适合直接决定要删除哪条 checkpoint 消息。
    messages_without_command = state_messages[:-1] if state_command and state_messages else state_messages
    return command, messages_without_command


def _remove_manual_command_update(command_id: str | None) -> dict[str, Any]:
    if not command_id:
        return {}
    return {"messages": [RemoveMessage(id=command_id, content="")]}


def _manual_compact_model_response(update: dict[str, Any], content: str) -> ExtendedModelResponse:
    return ExtendedModelResponse(
        model_response=ModelResponse(result=[AIMessage(content=content)]),
        command=Command(update=update),
    )


def _manual_compact_success_message(update: dict[str, Any]) -> str:
    metadata = update.get("compact_metadata", {}) if isinstance(update, dict) else {}
    summarized = metadata.get("summarized_messages", 0)
    kept = metadata.get("kept_messages", 0)
    return f"已压缩上下文：摘要 {summarized} 条历史消息，保留 {kept} 条最近消息。"


def _handle_agent_slash_command(request: ModelRequest, *, is_async: bool = False) -> Any | None:
    parsed = _agent_slash_command_request(request)
    if not parsed:
        return None

    command, messages_without_command = parsed
    command_id = command.message_id

    if command.name == HELP_COMMAND:
        update = _remove_manual_command_update(command_id)
        log_event("command.help", source="agent_commands")
        return _manual_compact_model_response(update, HELP_RESPONSE)

    if command.name == CLEAR_COMMAND:
        log_event("command.clear", source="agent_commands", checkpoint_updated=True)
        return _manual_compact_model_response(clear_context_update(), CLEAR_RESPONSE)

    if command.name != COMPACT_COMMAND:
        return None

    # /compact 是控制命令，不交给主模型；它和自动压缩复用同一套 compact state update。
    async def _run_async_compact() -> ExtendedModelResponse:
        try:
            update = await manual_acompact_state(request.state, focus=command.args, messages=messages_without_command)
        except Exception as exc:
            update = _compact_failure_update(request.state, exc) | _remove_manual_command_update(command_id)
            return _manual_compact_model_response(update, f"上下文压缩失败：{exc}")
        if not update:
            update = _remove_manual_command_update(command_id)
            return _manual_compact_model_response(update, "当前会话消息数量较少，暂无可压缩的历史上下文。")
        return _manual_compact_model_response(update, _manual_compact_success_message(update))

    if is_async:
        return _run_async_compact()

    try:
        update = manual_compact_state(request.state, focus=command.args, messages=messages_without_command)
    except Exception as exc:
        update = _compact_failure_update(request.state, exc) | _remove_manual_command_update(command_id)
        return _manual_compact_model_response(update, f"上下文压缩失败：{exc}")
    if not update:
        update = _remove_manual_command_update(command_id)
        return _manual_compact_model_response(update, "当前会话消息数量较少，暂无可压缩的历史上下文。")
    return _manual_compact_model_response(update, _manual_compact_success_message(update))

# ---------------------------------------------------------------------------
# Middleware class
# ---------------------------------------------------------------------------

class AgentCommandMiddleware(AgentMiddleware):
    """Intercept slash commands (/help, /clear, /compact) before the model runs."""

    def before_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        if _state_has_slash_command(state):
            return None
        return None

    async def abefore_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        if _state_has_slash_command(state):
            return None
        return None

    def wrap_model_call(self, request: ModelRequest, handler: Any) -> Any:
        command_response = _handle_agent_slash_command(request)
        if command_response is not None:
            return command_response
        return handler(request)

    async def awrap_model_call(self, request: ModelRequest, handler: Any) -> Any:
        command_response = _handle_agent_slash_command(request, is_async=True)
        if command_response is not None:
            if asyncio.iscoroutine(command_response):
                return await command_response
            return command_response
        return await handler(request)

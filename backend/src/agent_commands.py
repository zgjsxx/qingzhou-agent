"""Deterministic slash commands shared by non-LLM agent entry points."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from agent_logging import log_event

CLEAR_COMMAND = "/clear"
CLEAR_RESPONSE = "当前会话上下文已清除。"
HELP_COMMAND = "/help"
COMPACT_COMMAND = "/compact"
HELP_RESPONSE = """可用斜杠命令：

- `/help`：查看当前支持的命令。
- `/clear`：清除当前 thread 的消息、token 使用量和上下文压缩状态。
- `/compact [focus]`：手动压缩当前 thread 的历史上下文；可选 focus 用来提示摘要时优先保留哪些信息。"""


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


def is_clear_command(text: str) -> bool:
    """Return whether text is exactly the clear-context command."""
    command = parse_slash_command_text(text)
    return bool(command and command.name == CLEAR_COMMAND)


def is_help_command(text: str) -> bool:
    """Return whether text is exactly the help command."""
    command = parse_slash_command_text(text)
    return bool(command and command.name == HELP_COMMAND)


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
    state and is dispatched inside AgentContextCompactMiddleware by the same
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

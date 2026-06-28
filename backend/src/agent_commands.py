"""Deterministic slash commands shared by non-LLM agent entry points."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from agent_logging import log_event

CLEAR_COMMAND = "/clear"
CLEAR_RESPONSE = "当前会话上下文已清除。"


def is_clear_command(text: str) -> bool:
    """Return whether text is exactly the clear-context command."""
    return text.strip().lower() == CLEAR_COMMAND


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

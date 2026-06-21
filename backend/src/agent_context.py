"""Placeholder context middleware.

Context compaction is intentionally disabled for now. The previous
implementation could call the LLM for summaries, drop tool results, and
replace history with weak or empty summaries. Keeping this module as a small
no-op preserves existing imports while making the runtime behavior explicit.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware

MANUAL_COMPACT_MARKER = "[compact requested]"


def is_context_compaction_enabled() -> bool:
    """Context compaction is currently disabled."""
    return False


class AgentContextCompactMiddleware(AgentMiddleware):
    """No-op placeholder for future context compaction work."""

    def before_model(self, state: dict[str, Any], runtime: Any) -> None:
        return None

    async def abefore_model(self, state: dict[str, Any], runtime: Any) -> None:
        return None

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(request)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(request)

"""Context usage tracking and placeholder compaction middleware.

Context compaction is intentionally disabled for now. The middleware still
tracks the exact input token count for each model call using the active chat
model's tokenizer/counting implementation.
"""

from __future__ import annotations

import asyncio
from typing import Any, NotRequired, TypedDict

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    AgentState,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import BaseMessage
from langgraph.types import Command

MANUAL_COMPACT_MARKER = "[compact requested]"


class ContextUsage(TypedDict):
    input_tokens: int | None
    output_tokens: NotRequired[int | None]
    total_tokens: NotRequired[int | None]
    message_count: int
    includes_tools: bool
    counter: str
    error: NotRequired[str]


class XuAgentState(AgentState):
    # 扩展 LangChain 默认 AgentState，保存最近一次模型调用的上下文 token 统计。
    # 前端从 graph state 读取该字段，用于在对话框中显示当前上下文占用量。
    context_usage: NotRequired[ContextUsage]


def is_context_compaction_enabled() -> bool:
    """Context compaction is currently disabled."""
    return False


def _request_messages(request: ModelRequest) -> list[BaseMessage]:
    messages = list(getattr(request, "messages", []) or [])
    system_message = getattr(request, "system_message", None)
    if system_message is not None:
        return [system_message, *messages]
    return messages


def _count_model_tokens(
    request: ModelRequest,
    messages: list[BaseMessage],
    *,
    include_tools: bool,
) -> int:
    model = getattr(request, "model", None)
    counter = getattr(model, "get_num_tokens_from_messages", None)
    if counter is None:
        raise RuntimeError("active model does not expose get_num_tokens_from_messages")

    if include_tools:
        try:
            return int(counter(messages, tools=getattr(request, "tools", None)))
        except TypeError:
            return int(counter(messages))

    return int(counter(messages))


def _context_usage(request: ModelRequest) -> ContextUsage:
    system_message = getattr(request, "system_message", None)
    all_messages = _request_messages(request)
    tools = list(getattr(request, "tools", []) or [])

    return {
        "input_tokens": _count_model_tokens(request, all_messages, include_tools=bool(tools)),
        "message_count": len(all_messages),
        "includes_tools": bool(tools),
        "counter": f"{type(getattr(request, 'model', None)).__name__}.get_num_tokens_from_messages",
    }


def _context_usage_or_error(request: ModelRequest) -> ContextUsage:
    try:
        return _context_usage(request)
    except Exception as exc:
        all_messages = _request_messages(request)
        return {
            "input_tokens": None,
            "message_count": len(all_messages),
            "includes_tools": bool(list(getattr(request, "tools", []) or [])),
            "counter": f"{type(getattr(request, 'model', None)).__name__}.get_num_tokens_from_messages",
            "error": str(exc),
        }


def _response_usage(response: ModelResponse, request: ModelRequest) -> ContextUsage | None:
    result = list(getattr(response, "result", []) or [])
    for message in result:
        usage = getattr(message, "usage_metadata", None)
        if not isinstance(usage, dict):
            continue
        input_tokens = usage.get("input_tokens")
        if input_tokens is None:
            continue
        all_messages = _request_messages(request)
        return {
            "input_tokens": int(input_tokens),
            "output_tokens": _optional_int(usage.get("output_tokens")),
            "total_tokens": _optional_int(usage.get("total_tokens")),
            "message_count": len(all_messages),
            "includes_tools": bool(list(getattr(request, "tools", []) or [])),
            "counter": "response.usage_metadata",
        }
    return None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class AgentContextCompactMiddleware(AgentMiddleware):
    """Track context usage while leaving compaction disabled."""

    def before_model(self, state: dict[str, Any], runtime: Any) -> None:
        return None

    async def abefore_model(self, state: dict[str, Any], runtime: Any) -> None:
        return None

    def wrap_model_call(self, request: ModelRequest, handler: Any) -> Any:
        response = handler(request)
        if isinstance(response, ModelResponse):
            usage = _response_usage(response, request) or _context_usage_or_error(request)
            return ExtendedModelResponse(
                model_response=response,
                command=Command(update={"context_usage": usage}),
            )
        return response

    async def awrap_model_call(self, request: ModelRequest, handler: Any) -> Any:
        response = await handler(request)
        if isinstance(response, ModelResponse):
            usage = _response_usage(response, request) or await asyncio.to_thread(
                _context_usage_or_error,
                request,
            )
            return ExtendedModelResponse(
                model_response=response,
                command=Command(update={"context_usage": usage}),
            )
        return response

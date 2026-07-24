"""Guard model responses that contain no user-visible assistant text."""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage

from agent.logging import log_event

VISIBLE_TEXT_FALLBACK = (
    "模型本轮没有返回可见文本，只返回了内部思考内容。"
    "这通常是上游 LLM 响应格式异常，请重试；如果连续出现，请检查模型服务。"
)


def _content_has_visible_text(content: Any) -> bool:
    if isinstance(content, str):
        return bool(content.strip())

    if not isinstance(content, list):
        return bool(str(content).strip())

    for block in content:
        if isinstance(block, str) and block.strip():
            return True
        if not isinstance(block, dict):
            text = getattr(block, "text", None) or getattr(block, "content", None)
            if text and str(text).strip():
                return True
            continue

        block_type = block.get("type")
        if block_type == "text" and str(block.get("text", "")).strip():
            return True
        if block_type is None and str(block.get("content", "")).strip():
            return True

    return False


def _response_result(response: Any) -> list[Any]:
    result = getattr(response, "result", None)
    if isinstance(result, list):
        return result
    if result is not None:
        return [result]
    return [response]


def guard_empty_visible_ai_response(response: Any) -> Any:
    """Replace final thinking-only AI responses with a visible fallback."""
    result = _response_result(response)
    if not result:
        return response

    message = result[-1]
    if not isinstance(message, AIMessage):
        return response
    if getattr(message, "tool_calls", None):
        return response
    if _content_has_visible_text(message.content):
        return response

    guarded = AIMessage(
        content=VISIBLE_TEXT_FALLBACK,
        additional_kwargs=getattr(message, "additional_kwargs", {}) or {},
        response_metadata=getattr(message, "response_metadata", {}) or {},
        id=getattr(message, "id", None),
        name=getattr(message, "name", None),
    )
    log_event(
        "model.empty_visible_response",
        message_id=getattr(message, "id", None),
        content=message.content,
        response_metadata=getattr(message, "response_metadata", None),
    )

    if hasattr(response, "result"):
        response.result = [*result[:-1], guarded]
        return response
    return guarded


class AgentResponseGuardMiddleware(AgentMiddleware):
    """Ensure a completed model turn always has visible assistant output."""

    def wrap_model_call(self, request: ModelRequest, handler: Any) -> Any:
        return guard_empty_visible_ai_response(handler(request))

    async def awrap_model_call(self, request: ModelRequest, handler: Any) -> Any:
        return guard_empty_visible_ai_response(await handler(request))

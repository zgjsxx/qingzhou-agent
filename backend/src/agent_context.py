"""Context usage tracking and automatic compaction middleware.

The middleware tracks model input token usage and can replace older messages
with a structured summary when the conversation approaches the configured
context window.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from typing import Any, NotRequired, TypedDict

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    AgentState,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain.chat_models import init_chat_model
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command

from agent_config import config_str
from agent_logging import log_event
from agent_prompt import BASE_COMPACT_PROMPT, NO_TOOLS_PREAMBLE, NO_TOOLS_TRAILER

MANUAL_COMPACT_MARKER = "[compact requested]"
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
DEFAULT_COMPACT_MARGIN_TOKENS = 13_000
DEFAULT_COMPACT_KEEP_MESSAGES = 20
DEFAULT_COMPACT_MAX_FAILURES = 3


class ContextUsage(TypedDict):
    input_tokens: int | None
    output_tokens: NotRequired[int | None]
    total_tokens: NotRequired[int | None]
    message_count: int
    includes_tools: bool
    counter: str
    error: NotRequired[str]


class CompactMetadata(TypedDict):
    last_compacted_at: str
    before_tokens: int | None
    summarized_messages: int
    kept_messages: int
    failures: int


class XuAgentState(AgentState):
    # 扩展 LangChain 默认 AgentState，保存最近一次模型调用的上下文 token 统计。
    # 前端从 graph state 读取该字段，用于在对话框中显示当前上下文占用量。
    context_usage: NotRequired[ContextUsage]
    compact_metadata: NotRequired[CompactMetadata]
    compact_failure_count: NotRequired[int]


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(os.getenv(name, default))
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, minimum)


def is_context_compaction_enabled() -> bool:
    """Return whether automatic context compaction is enabled."""
    if _bool_env("DISABLE_COMPACT", False) or _bool_env("DISABLE_AUTO_COMPACT", False):
        return False
    return _bool_env("AGENT_AUTO_COMPACT_ENABLED", True)


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


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


def _state_value(state: Any, key: str, default: Any = None) -> Any:
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def _should_auto_compact_state(state: Any) -> tuple[bool, int | None]:
    if not is_context_compaction_enabled():
        return False, None

    failure_count = int(_state_value(state, "compact_failure_count", 0) or 0)
    max_failures = _int_env("AGENT_COMPACT_MAX_FAILURES", DEFAULT_COMPACT_MAX_FAILURES)
    if failure_count >= max_failures:
        return False, None

    context_window = _int_env("AGENT_CONTEXT_WINDOW", DEFAULT_CONTEXT_WINDOW_TOKENS)
    margin = _int_env("AGENT_COMPACT_MARGIN_TOKENS", DEFAULT_COMPACT_MARGIN_TOKENS)
    threshold = max(context_window - margin, 1)

    usage = _state_value(state, "context_usage", {}) or {}
    input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    if input_tokens is None:
        return False, None
    return int(input_tokens) >= threshold, int(input_tokens)


def _message_has_tool_calls(message: BaseMessage) -> bool:
    return isinstance(message, AIMessage) and bool(getattr(message, "tool_calls", None))


def _split_messages_for_compaction(messages: list[BaseMessage]) -> tuple[list[BaseMessage], list[BaseMessage]]:
    keep_count = _int_env("AGENT_COMPACT_KEEP_MESSAGES", DEFAULT_COMPACT_KEEP_MESSAGES)
    if len(messages) <= keep_count:
        return [], messages

    boundary = max(len(messages) - keep_count, 0)

    # Do not start the kept tail with ToolMessage objects; include their
    # preceding AIMessage so tool_call/tool_result pairs remain valid.
    while boundary > 0 and isinstance(messages[boundary], ToolMessage):
        boundary -= 1
    if boundary > 0 and _message_has_tool_calls(messages[boundary - 1]):
        boundary -= 1

    return messages[:boundary], messages[boundary:]


def _safe_content_for_summary(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        sanitized: list[Any] = []
        for block in content:
            if isinstance(block, dict):
                block_type = str(block.get("type", "")).lower()
                if block_type in {"image", "image_url", "document", "file"}:
                    sanitized.append(f"[{block_type}]")
                else:
                    sanitized.append(block)
            else:
                sanitized.append(str(block))
        return sanitized
    return str(content)


def _message_for_summary(message: BaseMessage, index: int) -> str:
    role = getattr(message, "type", message.__class__.__name__)
    content = _safe_content_for_summary(getattr(message, "content", ""))
    lines = [f"## Message {index}: {role}", f"content: {content}"]
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        lines.append(f"tool_calls: {tool_calls}")
    name = getattr(message, "name", None)
    if name:
        lines.append(f"name: {name}")
    return "\n".join(lines)


def _serialize_messages_for_summary(messages: list[BaseMessage]) -> str:
    return "\n\n".join(_message_for_summary(message, index) for index, message in enumerate(messages, start=1))


def _compact_prompt() -> str:
    # 压缩提示词参考 Claude Code 的结构：
    # 1. NO_TOOLS_PREAMBLE 强制 summary 模型只输出文本，避免调用工具。
    # 2. BASE_COMPACT_PROMPT 描述如何把历史对话整理成可恢复上下文的摘要。
    # 3. NO_TOOLS_TRAILER 再次强调不要调用工具，降低 summary 调用污染主流程的风险。
    return f"{NO_TOOLS_PREAMBLE}\n\n{BASE_COMPACT_PROMPT}{NO_TOOLS_TRAILER}"


def _extract_text(message: BaseMessage) -> str:
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
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def _format_compact_summary(text: str) -> str:
    without_analysis = re.sub(r"<analysis>.*?</analysis>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    summary_match = re.search(r"<summary>(.*?)</summary>", without_analysis, flags=re.DOTALL | re.IGNORECASE)
    if summary_match:
        return f"Summary:\n{summary_match.group(1).strip()}"
    return without_analysis


def _summary_request_messages(messages_to_summarize: list[BaseMessage]) -> list[BaseMessage]:
    transcript = _serialize_messages_for_summary(messages_to_summarize)
    # summary 模型只需要两类输入：
    # - SystemMessage：压缩规则和输出格式要求。
    # - HumanMessage：被压缩的历史消息，统一包在 <messages> 中，避免和规则混在一起。
    return [
        SystemMessage(content=_compact_prompt()),
        HumanMessage(content=f"<messages>\n{transcript}\n</messages>"),
    ]


def _summary_model_spec() -> str:
    adapter = os.getenv(
        "AGENT_SUMMARY_LLM_ADAPTER_TYPE",
        os.getenv("LLM_ADAPTER_TYPE", config_str("llm", "adapterType", "anthropic")),
    ).strip()
    model = os.getenv(
        "AGENT_SUMMARY_LLM_MODEL",
        os.getenv("LLM_MODEL", config_str("llm", "model", "glm-5.1")),
    ).strip()
    return f"{adapter}:{model}" if adapter else model


def _configure_summary_provider_env() -> None:
    adapter = os.getenv("LLM_ADAPTER_TYPE", config_str("llm", "adapterType", "anthropic")).strip().lower()
    api_key = os.getenv("LLM_API_KEY", config_str("llm", "apiKey", "")).strip()
    base_url = os.getenv("LLM_BASE_URL", config_str("llm", "baseUrl", "")).strip()

    if adapter == "anthropic":
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
        if base_url:
            os.environ["ANTHROPIC_API_URL"] = base_url
    elif adapter == "openai":
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key
        if base_url:
            os.environ["OPENAI_BASE_URL"] = base_url


def _clean_summary_model() -> Any:
    _configure_summary_provider_env()
    return init_chat_model(_summary_model_spec(), disable_streaming=True)


def _summarize_messages(messages_to_summarize: list[BaseMessage]) -> str:
    response = _clean_summary_model().invoke(
        _summary_request_messages(messages_to_summarize),
        config={"callbacks": [], "tags": ["context-compaction-summary"]},
    )
    return _format_compact_summary(_extract_text(response))


async def _asummarize_messages(messages_to_summarize: list[BaseMessage]) -> str:
    response = await _clean_summary_model().ainvoke(
        _summary_request_messages(messages_to_summarize),
        config={"callbacks": [], "tags": ["context-compaction-summary"]},
    )
    return _format_compact_summary(_extract_text(response))


def _compact_boundary_message(before_tokens: int | None, summarized_count: int, kept_count: int) -> SystemMessage:
    return SystemMessage(
        content=(
            "[Context compacted automatically]\n"
            f"Compacted at: {datetime.now(timezone.utc).isoformat()}\n"
            f"Before compact input tokens: {before_tokens if before_tokens is not None else 'unknown'}\n"
            f"Messages summarized: {summarized_count}\n"
            f"Messages kept: {kept_count}"
        )
    )


def _summary_message(summary: str) -> SystemMessage:
    return SystemMessage(
        content=(
            "This session is being continued from an earlier conversation that was compacted.\n"
            "The summary below covers the earlier portion of the conversation.\n\n"
            f"{summary}\n\n"
            "Continue the conversation from where it left off without asking the user to repeat context."
        )
    )


def _build_compacted_messages(
    messages_to_summarize: list[BaseMessage],
    messages_to_keep: list[BaseMessage],
    summary: str,
    before_tokens: int | None,
) -> list[BaseMessage]:
    return [
        _compact_boundary_message(before_tokens, len(messages_to_summarize), len(messages_to_keep)),
        _summary_message(summary),
        *messages_to_keep,
    ]


def _replace_messages_update(messages: list[BaseMessage]) -> list[BaseMessage]:
    return [RemoveMessage(id=REMOVE_ALL_MESSAGES, content=""), *messages]


def _compact_state(state: Any) -> dict[str, Any]:
    should_compact, before_tokens = _should_auto_compact_state(state)
    if not should_compact:
        return {}

    messages = list(_state_value(state, "messages", []) or [])
    messages_to_summarize, messages_to_keep = _split_messages_for_compaction(messages)
    if not messages_to_summarize:
        return {}

    summary = _summarize_messages(messages_to_summarize)
    compacted_messages = _build_compacted_messages(messages_to_summarize, messages_to_keep, summary, before_tokens)
    metadata: CompactMetadata = {
        "last_compacted_at": datetime.now(timezone.utc).isoformat(),
        "before_tokens": before_tokens,
        "summarized_messages": len(messages_to_summarize),
        "kept_messages": len(messages_to_keep),
        "failures": 0,
    }
    log_event(
        "context.compact",
        before_tokens=before_tokens,
        summarized_messages=len(messages_to_summarize),
        kept_messages=len(messages_to_keep),
    )
    return {
        "messages": _replace_messages_update(compacted_messages),
        "compact_metadata": metadata,
        "compact_failure_count": 0,
    }


async def _acompact_state(state: Any) -> dict[str, Any]:
    should_compact, before_tokens = _should_auto_compact_state(state)
    if not should_compact:
        return {}

    messages = list(_state_value(state, "messages", []) or [])
    messages_to_summarize, messages_to_keep = _split_messages_for_compaction(messages)
    if not messages_to_summarize:
        return {}

    summary = await _asummarize_messages(messages_to_summarize)
    compacted_messages = _build_compacted_messages(messages_to_summarize, messages_to_keep, summary, before_tokens)
    metadata: CompactMetadata = {
        "last_compacted_at": datetime.now(timezone.utc).isoformat(),
        "before_tokens": before_tokens,
        "summarized_messages": len(messages_to_summarize),
        "kept_messages": len(messages_to_keep),
        "failures": 0,
    }
    log_event(
        "context.compact",
        before_tokens=before_tokens,
        summarized_messages=len(messages_to_summarize),
        kept_messages=len(messages_to_keep),
    )
    return {
        "messages": _replace_messages_update(compacted_messages),
        "compact_metadata": metadata,
        "compact_failure_count": 0,
    }


def _compact_failure_update(state: Any, exc: Exception) -> dict[str, Any]:
    failure_count = int(_state_value(state, "compact_failure_count", 0) or 0) + 1
    log_event("context.compact_error", failures=failure_count, error=repr(exc))
    return {"compact_failure_count": failure_count}


class AgentContextCompactMiddleware(AgentMiddleware):
    """Track context usage and compact old messages near the context limit."""

    def before_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        try:
            update = _compact_state(state)
        except Exception as exc:
            update = _compact_failure_update(state, exc)
        return update or None

    async def abefore_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        try:
            update = await _acompact_state(state)
        except Exception as exc:
            update = _compact_failure_update(state, exc)
        return update or None

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

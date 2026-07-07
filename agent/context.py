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
from typing import Any
from typing_extensions import NotRequired, TypedDict

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

from agent.config import config_str
from agent.logging import log_event
from agent.prompt import BASE_COMPACT_PROMPT, NO_TOOLS_PREAMBLE, NO_TOOLS_TRAILER
from agent.llm_config import configure_provider_env, provider_model_kwargs

MANUAL_COMPACT_MARKER = "[compact requested]"
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
DEFAULT_COMPACT_MARGIN_TOKENS = 13_000
DEFAULT_COMPACT_KEEP_MESSAGES = 20
DEFAULT_MANUAL_COMPACT_KEEP_MESSAGES = 0
DEFAULT_COMPACT_MAX_FAILURES = 3
DEFAULT_SNIP_TRIGGER_MESSAGES = 50
DEFAULT_SNIP_KEEP_HEAD_MESSAGES = 3
DEFAULT_SNIP_KEEP_TAIL_MESSAGES = 47


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
    trigger: NotRequired[str]
    focus: NotRequired[str]


class SnipCompactMetadata(TypedDict):
    last_snipped_at: str
    before_message_count: int
    after_message_count: int
    removed_message_count: int
    keep_head_messages: int
    keep_tail_messages: int
    actual_tail_messages: int
    tail_expanded_for_tool_pair: bool


class XuAgentState(AgentState):
    # 扩展 LangChain 默认 AgentState，保存最近一次模型调用的上下文 token 统计。
    # 前端从 graph state 读取该字段，用于在对话框中显示当前上下文占用量。
    context_usage: NotRequired[ContextUsage]
    compact_metadata: NotRequired[CompactMetadata]
    snip_compact_metadata: NotRequired[SnipCompactMetadata]
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


def is_snip_compaction_enabled() -> bool:
    """Return whether lightweight message-count based compaction is enabled."""
    if _bool_env("DISABLE_COMPACT", False) or _bool_env("DISABLE_AUTO_COMPACT", False):
        return False
    return _bool_env("AGENT_SNIP_COMPACT_ENABLED", True)


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


def _manual_compact_before_tokens(state: Any) -> int | None:
    usage = _state_value(state, "context_usage", {}) or {}
    if not isinstance(usage, dict):
        return None
    return _optional_int(usage.get("input_tokens"))


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


def _message_tool_result_ids(message: BaseMessage) -> set[str]:
    ids: set[str] = set()
    if isinstance(message, ToolMessage):
        tool_call_id = getattr(message, "tool_call_id", None)
        if tool_call_id:
            ids.add(str(tool_call_id))

    content = getattr(message, "content", None)
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type", "")).lower() != "tool_result":
                continue
            for key in ("tool_call_id", "tool_use_id", "id"):
                value = block.get(key)
                if value:
                    ids.add(str(value))
                    break
    return ids


def _message_tool_call_ids(message: BaseMessage) -> set[str]:
    ids: set[str] = set()
    if isinstance(message, AIMessage):
        for tool_call in getattr(message, "tool_calls", None) or []:
            if isinstance(tool_call, dict) and tool_call.get("id"):
                ids.add(str(tool_call["id"]))

    content = getattr(message, "content", None)
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type", "")).lower() != "tool_use":
                continue
            value = block.get("id")
            if value:
                ids.add(str(value))
    return ids


def _find_previous_tool_call_message(
    messages: list[BaseMessage],
    before_index: int,
    tool_result_ids: set[str],
) -> int | None:
    for index in range(before_index - 1, -1, -1):
        if _message_tool_call_ids(messages[index]) & tool_result_ids:
            return index
    return None


def _snip_tail_start(messages: list[BaseMessage], tail_start: int, min_start: int) -> tuple[int, bool]:
    expanded = False
    while tail_start > min_start:
        tool_result_ids = _message_tool_result_ids(messages[tail_start])
        if not tool_result_ids:
            break

        tool_call_index = _find_previous_tool_call_message(messages, tail_start, tool_result_ids)
        if tool_call_index is None or tool_call_index < min_start or tool_call_index >= tail_start:
            break

        tail_start = tool_call_index
        expanded = True
    return tail_start, expanded


def _snip_compact_state(state: Any) -> dict[str, Any]:
    if not is_snip_compaction_enabled():
        return {}

    messages = list(_state_value(state, "messages", []) or [])
    trigger_count = _int_env("AGENT_SNIP_TRIGGER_MESSAGES", DEFAULT_SNIP_TRIGGER_MESSAGES)
    if len(messages) <= trigger_count:
        return {}

    keep_head = _int_env("AGENT_SNIP_KEEP_HEAD_MESSAGES", DEFAULT_SNIP_KEEP_HEAD_MESSAGES, minimum=0)
    keep_tail = _int_env("AGENT_SNIP_KEEP_TAIL_MESSAGES", DEFAULT_SNIP_KEEP_TAIL_MESSAGES, minimum=0)
    if keep_head + keep_tail >= len(messages):
        return {}

    tail_start = max(len(messages) - keep_tail, keep_head)
    tail_start, tail_expanded = _snip_tail_start(messages, tail_start, keep_head)
    removed_count = max(tail_start - keep_head, 0)
    if removed_count <= 0:
        return {}

    snipped_messages = [*messages[:keep_head], *messages[tail_start:]]
    metadata: SnipCompactMetadata = {
        "last_snipped_at": datetime.now(timezone.utc).isoformat(),
        "before_message_count": len(messages),
        "after_message_count": len(snipped_messages),
        "removed_message_count": removed_count,
        "keep_head_messages": keep_head,
        "keep_tail_messages": keep_tail,
        "actual_tail_messages": len(messages) - tail_start,
        "tail_expanded_for_tool_pair": tail_expanded,
    }
    log_event(
        "context.snip_compact",
        before_message_count=len(messages),
        after_message_count=len(snipped_messages),
        removed_message_count=removed_count,
        keep_head_messages=keep_head,
        keep_tail_messages=keep_tail,
        actual_tail_messages=len(messages) - tail_start,
        tail_expanded_for_tool_pair=tail_expanded,
    )
    return {
        "messages": _replace_messages_update(snipped_messages),
        "snip_compact_metadata": metadata,
    }


def _split_messages_for_compaction(
    messages: list[BaseMessage],
    keep_count: int | None = None,
) -> tuple[list[BaseMessage], list[BaseMessage]]:
    keep_count = _int_env("AGENT_COMPACT_KEEP_MESSAGES", DEFAULT_COMPACT_KEEP_MESSAGES) if keep_count is None else keep_count
    if keep_count <= 0:
        return messages, []
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


def _summary_request_messages(messages_to_summarize: list[BaseMessage], focus: str = "") -> list[BaseMessage]:
    transcript = _serialize_messages_for_summary(messages_to_summarize)
    focus_block = f"\n\n<focus>\n{focus}\n</focus>" if focus else ""
    # summary 模型只需要两类输入：
    # - SystemMessage：压缩规则和输出格式要求。
    # - HumanMessage：被压缩的历史消息，统一包在 <messages> 中，避免和规则混在一起。
    return [
        SystemMessage(content=_compact_prompt()),
        HumanMessage(content=f"<messages>\n{transcript}\n</messages>{focus_block}"),
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
    auth_token = os.getenv("LLM_AUTH_TOKEN", os.getenv("ANTHROPIC_AUTH_TOKEN", "")).strip()
    base_url = os.getenv("LLM_BASE_URL", config_str("llm", "baseUrl", "")).strip()

    configure_provider_env(
        adapter=adapter,
        api_key=api_key,
        auth_token=auth_token,
        base_url=base_url,
    )


def _clean_summary_model() -> Any:
    _configure_summary_provider_env()
    adapter = os.getenv("LLM_ADAPTER_TYPE", config_str("llm", "adapterType", "anthropic")).strip()
    auth_token = os.getenv("LLM_AUTH_TOKEN", os.getenv("ANTHROPIC_AUTH_TOKEN", "")).strip()
    return init_chat_model(
        _summary_model_spec(),
        disable_streaming=True,
        **provider_model_kwargs(adapter=adapter, auth_token=auth_token),
    )


def _summarize_messages(messages_to_summarize: list[BaseMessage], focus: str = "") -> str:
    response = _clean_summary_model().invoke(
        _summary_request_messages(messages_to_summarize, focus=focus),
        config={"callbacks": [], "tags": ["context-compaction-summary"]},
    )
    return _format_compact_summary(_extract_text(response))


async def _asummarize_messages(messages_to_summarize: list[BaseMessage], focus: str = "") -> str:
    response = await _clean_summary_model().ainvoke(
        _summary_request_messages(messages_to_summarize, focus=focus),
        config={"callbacks": [], "tags": ["context-compaction-summary"]},
    )
    return _format_compact_summary(_extract_text(response))


def _compact_boundary_message(
    before_tokens: int | None,
    summarized_count: int,
    kept_count: int,
    trigger: str,
) -> SystemMessage:
    return SystemMessage(
        content=(
            f"[Context compacted by {trigger}]\n"
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
    trigger: str,
) -> list[BaseMessage]:
    return [
        _compact_boundary_message(before_tokens, len(messages_to_summarize), len(messages_to_keep), trigger),
        _summary_message(summary),
        *messages_to_keep,
    ]


def _replace_messages_update(messages: list[BaseMessage]) -> list[BaseMessage]:
    return [RemoveMessage(id=REMOVE_ALL_MESSAGES, content=""), *messages]


def _compact_metadata(
    *,
    before_tokens: int | None,
    summarized_messages: int,
    kept_messages: int,
    trigger: str,
    focus: str,
) -> CompactMetadata:
    metadata: CompactMetadata = {
        "last_compacted_at": datetime.now(timezone.utc).isoformat(),
        "before_tokens": before_tokens,
        "summarized_messages": summarized_messages,
        "kept_messages": kept_messages,
        "failures": 0,
        "trigger": trigger,
    }
    if focus:
        metadata["focus"] = focus
    return metadata


def _compact_state_update(
    *,
    messages_to_summarize: list[BaseMessage],
    messages_to_keep: list[BaseMessage],
    summary: str,
    before_tokens: int | None,
    trigger: str,
    focus: str,
) -> dict[str, Any]:
    # 自动压缩和手动 /compact 最终都从这里生成 LangGraph state update。
    # 这样消息替换、metadata 字段、日志结构保持一套语义，后续改压缩格式时不会出现两边不一致。
    compacted_messages = _build_compacted_messages(
        messages_to_summarize,
        messages_to_keep,
        summary,
        before_tokens,
        trigger,
    )
    metadata = _compact_metadata(
        before_tokens=before_tokens,
        summarized_messages=len(messages_to_summarize),
        kept_messages=len(messages_to_keep),
        trigger=trigger,
        focus=focus,
    )
    log_event(
        "context.compact",
        before_tokens=before_tokens,
        summarized_messages=len(messages_to_summarize),
        kept_messages=len(messages_to_keep),
        trigger=trigger,
        focus_present=bool(focus),
    )
    return {
        "messages": _replace_messages_update(compacted_messages),
        "compact_metadata": metadata,
        "compact_failure_count": 0,
    }


def _compact_state_now(
    state: Any,
    *,
    before_tokens: int | None,
    trigger: str,
    focus: str = "",
    keep_messages: int | None = None,
    messages: list[BaseMessage] | None = None,
) -> dict[str, Any]:
    # 这里不再判断是否“应该压缩”，只负责执行压缩动作。
    # 自动触发先用 _should_auto_compact_state 判断，手动 /compact 则直接调用同一个动作入口。
    source_messages = list(messages if messages is not None else (_state_value(state, "messages", []) or []))
    messages_to_summarize, messages_to_keep = _split_messages_for_compaction(source_messages, keep_count=keep_messages)
    if not messages_to_summarize:
        return {}

    summary = _summarize_messages(messages_to_summarize, focus=focus)
    return _compact_state_update(
        messages_to_summarize=messages_to_summarize,
        messages_to_keep=messages_to_keep,
        summary=summary,
        before_tokens=before_tokens,
        trigger=trigger,
        focus=focus,
    )


async def _acompact_state_now(
    state: Any,
    *,
    before_tokens: int | None,
    trigger: str,
    focus: str = "",
    keep_messages: int | None = None,
    messages: list[BaseMessage] | None = None,
) -> dict[str, Any]:
    source_messages = list(messages if messages is not None else (_state_value(state, "messages", []) or []))
    messages_to_summarize, messages_to_keep = _split_messages_for_compaction(source_messages, keep_count=keep_messages)
    if not messages_to_summarize:
        return {}

    summary = await _asummarize_messages(messages_to_summarize, focus=focus)
    return _compact_state_update(
        messages_to_summarize=messages_to_summarize,
        messages_to_keep=messages_to_keep,
        summary=summary,
        before_tokens=before_tokens,
        trigger=trigger,
        focus=focus,
    )


def _compact_state(state: Any) -> dict[str, Any]:
    should_compact, before_tokens = _should_auto_compact_state(state)
    if not should_compact:
        return {}
    return _compact_state_now(state, before_tokens=before_tokens, trigger="auto")


async def _acompact_state(state: Any) -> dict[str, Any]:
    should_compact, before_tokens = _should_auto_compact_state(state)
    if not should_compact:
        return {}
    return await _acompact_state_now(state, before_tokens=before_tokens, trigger="auto")


def manual_compact_state(state: Any, *, focus: str = "", messages: list[BaseMessage] | None = None) -> dict[str, Any]:
    # 对外保留一个明确的手动压缩入口，便于命令、工具或后续飞书入口复用。
    # 它不另写压缩逻辑，只把 trigger/focus 传入共享的 _compact_state_now。
    # 手动 /compact 表达的是“现在尽量收缩上下文”，因此默认不再沿用自动压缩保留 20 条的策略。
    # 如果某些部署希望手动压缩后仍保留最近几条原文，可通过 AGENT_MANUAL_COMPACT_KEEP_MESSAGES 调整。
    keep_messages = _int_env("AGENT_MANUAL_COMPACT_KEEP_MESSAGES", DEFAULT_MANUAL_COMPACT_KEEP_MESSAGES, minimum=0)
    return _compact_state_now(
        state,
        before_tokens=_manual_compact_before_tokens(state),
        trigger="manual",
        focus=focus,
        keep_messages=keep_messages,
        messages=messages,
    )


async def manual_acompact_state(
    state: Any,
    *,
    focus: str = "",
    messages: list[BaseMessage] | None = None,
) -> dict[str, Any]:
    keep_messages = _int_env("AGENT_MANUAL_COMPACT_KEEP_MESSAGES", DEFAULT_MANUAL_COMPACT_KEEP_MESSAGES, minimum=0)
    return await _acompact_state_now(
        state,
        before_tokens=_manual_compact_before_tokens(state),
        trigger="manual",
        focus=focus,
        keep_messages=keep_messages,
        messages=messages,
    )


def _compact_failure_update(state: Any, exc: Exception) -> dict[str, Any]:
    failure_count = int(_state_value(state, "compact_failure_count", 0) or 0) + 1
    log_event("context.compact_error", failures=failure_count, error=repr(exc))
    return {"compact_failure_count": failure_count}




class AgentContextCompactMiddleware(AgentMiddleware):
    """Track context usage and compact old messages near the context limit."""

    def before_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        try:
            update = _snip_compact_state(state) or _compact_state(state)
        except Exception as exc:
            update = _compact_failure_update(state, exc)
        return update or None

    async def abefore_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        try:
            update = _snip_compact_state(state)
            if not update:
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
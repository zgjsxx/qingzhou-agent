"""Context compaction middleware for long-running agent threads."""

from __future__ import annotations

import os
import json
import time
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
    messages_to_dict,
)
from langgraph.graph.message import add_messages

from agent_logging import log_event

BACKEND_DIR = Path(__file__).resolve().parents[1]
TRANSCRIPT_DIR = BACKEND_DIR / ".transcripts"
TOOL_RESULTS_DIR = BACKEND_DIR / ".agent_outputs" / "tool-results"
MANUAL_COMPACT_MARKER = "[compact requested]"
COMPACT_PLACEHOLDER = (
    "[Earlier conversation messages were compacted to save context. "
    "Ask the user or re-run tools if details are needed.]"
)
TOOL_RESULT_PLACEHOLDER = (
    "[Earlier tool result compacted to save context. Re-run the tool if the full output is needed.]"
)
SUMMARY_PREFIX = "[Compacted conversation summary]"
REACTIVE_SUMMARY_PREFIX = "[Reactive compact conversation summary]"
MAX_REACTIVE_RETRIES = 1
PENDING_STATE_UPDATES: dict[str, list[BaseMessage]] = {}


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
    """Return whether local context compaction is enabled."""
    return _bool_env("AGENT_CONTEXT_COMPACT_ENABLED", True)


def _message_id(message: Any) -> str | None:
    value = getattr(message, "id", None)
    return str(value) if value else None


def _message_content_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return str(content)


def _has_tool_calls(message: Any) -> bool:
    if not isinstance(message, AIMessage):
        return False
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        return True
    raw_tool_calls = getattr(message, "additional_kwargs", {}).get("tool_calls")
    return bool(raw_tool_calls)


def _is_tool_message(message: Any) -> bool:
    return isinstance(message, ToolMessage) or getattr(message, "type", None) == "tool"


def _state_messages(state: Any) -> list[Any]:
    if isinstance(state, dict):
        value = state.get("messages", [])
    else:
        value = getattr(state, "messages", [])
    return list(value or [])


def _thread_key(runtime: Any, state: Any) -> str:
    config = getattr(runtime, "config", None)
    configurable = _mapping_get(config, "configurable") or {}
    for key in ("thread_id", "threadId", "session_id", "sessionId"):
        value = _mapping_get(configurable, key) or _mapping_get(config, key)
        if value:
            return str(value)
    return f"state:{id(state)}"


def _mapping_get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _replacement_tool_message(message: ToolMessage) -> ToolMessage | None:
    message_id = _message_id(message)
    tool_call_id = getattr(message, "tool_call_id", None)
    if not message_id or not tool_call_id:
        return None

    return ToolMessage(
        content=TOOL_RESULT_PLACEHOLDER,
        id=message_id,
        tool_call_id=str(tool_call_id),
        name=getattr(message, "name", None),
        status=getattr(message, "status", "success"),
    )


def _persist_large_output(message: ToolMessage, output: str) -> str:
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = re_safe_id(_message_id(message) or getattr(message, "tool_call_id", None) or str(time.time_ns()))
    path = TOOL_RESULTS_DIR / f"{safe_id}.txt"
    if not path.exists():
        path.write_text(output, encoding="utf-8")
    preview_chars = _int_env("AGENT_CONTEXT_PERSIST_PREVIEW_CHARS", 2000)
    preview = output[:preview_chars]
    return (
        "<persisted-output>\n"
        f"Full output saved at: {path}\n"
        "Re-run the tool or read this file if the full output is needed.\n"
        f"Preview:\n{preview}\n"
        "</persisted-output>"
    )


def re_safe_id(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)[:120]


def _tool_result_budget_updates(messages: list[Any], max_chars: int, persist_threshold: int) -> list[BaseMessage]:
    tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
    total = sum(len(_message_content_text(message)) for message in tool_messages)
    if total <= max_chars:
        return []

    updates: list[BaseMessage] = []
    ranked = sorted(tool_messages, key=lambda message: len(_message_content_text(message)), reverse=True)
    for message in ranked:
        if total <= max_chars:
            break
        content = _message_content_text(message)
        if len(content) <= persist_threshold or content.startswith("<persisted-output>"):
            continue
        replacement = _replacement_tool_message(message)
        if replacement is None:
            continue
        replacement.content = _persist_large_output(message, content)
        updates.append(replacement)
        total -= len(content)
        total += len(_message_content_text(replacement))

    return updates


def _micro_compact_updates(
    messages: list[Any],
    keep_recent_tool_results: int,
    min_chars: int,
) -> list[BaseMessage]:
    tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
    if len(tool_messages) <= keep_recent_tool_results:
        return []

    updates: list[BaseMessage] = []
    for message in tool_messages[:-keep_recent_tool_results]:
        content = _message_content_text(message)
        if len(content) <= min_chars or content == TOOL_RESULT_PLACEHOLDER:
            continue
        replacement = _replacement_tool_message(message)
        if replacement is not None:
            updates.append(replacement)

    return updates


def _snip_tail_start(messages: list[Any], tail_start: int) -> int:
    while tail_start > 0 and tail_start < len(messages) and _is_tool_message(messages[tail_start]):
        tail_start -= 1
    return tail_start


def _snip_head_end(messages: list[Any], head_end: int) -> int:
    if head_end > 0 and _has_tool_calls(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_message(messages[head_end]):
            head_end += 1
    return head_end


def _snip_compact_updates(
    messages: list[Any],
    max_messages: int,
    keep_head: int,
    keep_tail: int,
) -> list[BaseMessage]:
    if len(messages) <= max_messages:
        return []

    keep_head = min(keep_head, max_messages)
    keep_tail = min(keep_tail, max_messages - keep_head)
    head_end = _snip_head_end(messages, keep_head)
    tail_start = _snip_tail_start(messages, len(messages) - keep_tail)
    if head_end >= tail_start:
        return []

    removed = messages[head_end:tail_start]
    removed_ids = [_message_id(message) for message in removed]
    removed_ids = [message_id for message_id in removed_ids if message_id]
    if not removed_ids:
        return []

    placeholder = HumanMessage(
        content=f"{COMPACT_PLACEHOLDER} Snipped {len(removed)} messages.",
        id=removed_ids[0],
    )
    removals = [RemoveMessage(id=message_id) for message_id in removed_ids[1:]]
    return [placeholder, *removals]


def compact_message_updates(messages: list[Any]) -> list[BaseMessage]:
    """Build reducer updates that compact old messages without calling an LLM."""
    if not is_context_compaction_enabled():
        return []

    keep_recent_tool_results = _int_env("AGENT_CONTEXT_KEEP_RECENT_TOOL_RESULTS", 4)
    tool_result_min_chars = _int_env("AGENT_CONTEXT_TOOL_RESULT_MIN_CHARS", 1200)
    tool_result_budget_chars = _int_env("AGENT_CONTEXT_TOOL_RESULT_BUDGET_CHARS", 200_000)
    persist_threshold_chars = _int_env("AGENT_CONTEXT_PERSIST_THRESHOLD_CHARS", 30_000)
    max_messages = _int_env("AGENT_CONTEXT_MAX_MESSAGES", 80)
    keep_head = _int_env("AGENT_CONTEXT_KEEP_HEAD_MESSAGES", 4)
    keep_tail = _int_env("AGENT_CONTEXT_KEEP_TAIL_MESSAGES", 60)

    updates: list[BaseMessage] = []
    updates.extend(_tool_result_budget_updates(messages, tool_result_budget_chars, persist_threshold_chars))
    updates.extend(_micro_compact_updates(messages, keep_recent_tool_results, tool_result_min_chars))
    updates.extend(_snip_compact_updates(messages, max_messages, keep_head, keep_tail))
    return updates


def _estimate_chars(messages: list[Any]) -> int:
    return len(json.dumps(messages_to_dict(messages), ensure_ascii=False, default=str))


def _write_transcript(messages: list[Any], reason: str) -> Path:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"{reason}_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for message in messages:
            handle.write(json.dumps(messages_to_dict([message])[0], ensure_ascii=False, default=str))
            handle.write("\n")
    return path


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
            else:
                text = getattr(block, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content)


def _is_manual_compact_request(messages: list[Any]) -> bool:
    if not messages:
        return False
    last = messages[-1]
    if not isinstance(last, ToolMessage):
        return False
    return getattr(last, "name", None) == "compact" or MANUAL_COMPACT_MARKER in _message_content_text(last)


def _summary_prompt(messages: list[Any], transcript_path: Path, reason: str) -> str:
    conversation = json.dumps(messages_to_dict(messages), ensure_ascii=False, default=str)
    max_input_chars = _int_env("AGENT_CONTEXT_SUMMARY_INPUT_CHARS", 100_000)
    if len(conversation) > max_input_chars:
        conversation = conversation[-max_input_chars:]
    return (
        "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
        "Summarize this coding-agent conversation so work can continue after context compaction.\n"
        "Preserve these details, using concise Chinese unless source text requires otherwise:\n"
        "1. current user goal and status\n"
        "2. important user constraints and preferences\n"
        "3. key findings, decisions, and failed attempts\n"
        "4. files read, files changed, and commands run when relevant\n"
        "5. remaining work and next best actions\n"
        "6. any approvals, denials, or safety constraints still in effect\n\n"
        f"Compaction reason: {reason}\n"
        f"Full transcript saved at: {transcript_path}\n\n"
        "Conversation JSON:\n"
        f"{conversation}\n\n"
        "REMINDER: TEXT ONLY. Do NOT call tools."
    )


def _summarize_messages_sync(model: Any, messages: list[Any], reason: str) -> str:
    transcript_path = _write_transcript(messages, reason)
    response = model.invoke([HumanMessage(content=_summary_prompt(messages, transcript_path, reason))])
    summary = _message_text(response).strip()
    return summary or "(empty compact summary)"


async def _summarize_messages_async(model: Any, messages: list[Any], reason: str) -> str:
    transcript_path = _write_transcript(messages, reason)
    response = await model.ainvoke([HumanMessage(content=_summary_prompt(messages, transcript_path, reason))])
    summary = _message_text(response).strip()
    return summary or "(empty compact summary)"


def _summary_updates(messages: list[Any], summary: str, prefix: str, keep_tail: int) -> list[BaseMessage]:
    if not messages:
        return [HumanMessage(content=f"{prefix}\n\n{summary}")]

    tail_start = _snip_tail_start(messages, max(0, len(messages) - keep_tail))
    removed = messages[:tail_start]
    if not removed:
        return []

    removed_ids = [_message_id(message) for message in removed]
    removed_ids = [message_id for message_id in removed_ids if message_id]
    if not removed_ids:
        return []

    summary_message = HumanMessage(content=f"{prefix}\n\n{summary}", id=removed_ids[0])
    removals = [RemoveMessage(id=message_id) for message_id in removed_ids[1:]]
    return [summary_message, *removals]


def _has_compact_summary(messages: list[Any]) -> bool:
    return any(
        isinstance(message, HumanMessage)
        and isinstance(getattr(message, "content", None), str)
        and getattr(message, "content").startswith((SUMMARY_PREFIX, REACTIVE_SUMMARY_PREFIX))
        for message in messages[:3]
    )


def _should_auto_summarize(messages: list[Any]) -> bool:
    if not _bool_env("AGENT_CONTEXT_AUTO_SUMMARY_ENABLED", True):
        return False
    if _has_compact_summary(messages):
        return False
    max_chars = _int_env("AGENT_CONTEXT_SUMMARY_TRIGGER_CHARS", 120_000)
    max_count = _int_env("AGENT_CONTEXT_SUMMARY_TRIGGER_MESSAGES", 140)
    return len(messages) > max_count or _estimate_chars(messages) > max_chars


def _is_prompt_too_long_error(exc: Exception) -> bool:
    text = repr(exc).lower()
    return any(marker in text for marker in ("prompt_too_long", "too many tokens", "context length", "413"))


class AgentContextCompactMiddleware(AgentMiddleware):
    """Compact old message history before model calls using local transforms only."""

    def before_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        return self._compact_state(state, runtime)

    async def abefore_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        return self._compact_state(state, runtime)

    def _compact_state(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        thread_key = _thread_key(runtime, state)
        pending = PENDING_STATE_UPDATES.pop(thread_key, [])
        messages = _state_messages(state)
        updates = compact_message_updates(messages)
        combined = [*pending, *updates]
        if not combined:
            return None

        log_event(
            "context.compact",
            message_count=len(messages),
            update_count=len(combined),
            pending_count=len(pending),
            replacement_count=sum(1 for update in combined if not isinstance(update, RemoveMessage)),
            removal_count=sum(1 for update in combined if isinstance(update, RemoveMessage)),
        )
        return {"messages": combined}

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        request = self._maybe_compact_request_sync(request)
        try:
            return handler(request)
        except Exception as exc:
            if not _is_prompt_too_long_error(exc):
                raise
            return self._reactive_retry_sync(request, handler, exc)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        request = await self._maybe_compact_request_async(request)
        try:
            return await handler(request)
        except Exception as exc:
            if not _is_prompt_too_long_error(exc):
                raise
            return await self._reactive_retry_async(request, handler, exc)

    def _maybe_compact_request_sync(self, request: Any) -> Any:
        messages = list(getattr(request, "messages", []) or [])
        reason = ""
        if _is_manual_compact_request(messages):
            reason = "manual"
        elif _should_auto_summarize(messages):
            reason = "auto"
        if not reason:
            return request

        summary = _summarize_messages_sync(request.model, messages, reason)
        prefix = SUMMARY_PREFIX
        keep_tail = _int_env("AGENT_CONTEXT_POST_SUMMARY_KEEP_TAIL_MESSAGES", 8)
        return self._apply_summary_to_request(request, messages, summary, prefix, keep_tail, reason)

    async def _maybe_compact_request_async(self, request: Any) -> Any:
        messages = list(getattr(request, "messages", []) or [])
        reason = ""
        if _is_manual_compact_request(messages):
            reason = "manual"
        elif _should_auto_summarize(messages):
            reason = "auto"
        if not reason:
            return request

        summary = await _summarize_messages_async(request.model, messages, reason)
        prefix = SUMMARY_PREFIX
        keep_tail = _int_env("AGENT_CONTEXT_POST_SUMMARY_KEEP_TAIL_MESSAGES", 8)
        return self._apply_summary_to_request(request, messages, summary, prefix, keep_tail, reason)

    def _reactive_retry_sync(self, request: Any, handler: Any, original_exc: Exception) -> Any:
        retries = int(getattr(request, "model_settings", {}).get("_context_reactive_retries", 0))
        if retries >= MAX_REACTIVE_RETRIES:
            raise original_exc

        messages = list(getattr(request, "messages", []) or [])
        summary = _summarize_messages_sync(request.model, messages, "reactive")
        keep_tail = _int_env("AGENT_CONTEXT_REACTIVE_KEEP_TAIL_MESSAGES", 5)
        retry_request = self._apply_summary_to_request(
            request,
            messages,
            summary,
            REACTIVE_SUMMARY_PREFIX,
            keep_tail,
            "reactive",
        )
        model_settings = dict(getattr(retry_request, "model_settings", {}) or {})
        model_settings["_context_reactive_retries"] = retries + 1
        return handler(retry_request.override(model_settings=model_settings))

    async def _reactive_retry_async(self, request: Any, handler: Any, original_exc: Exception) -> Any:
        retries = int(getattr(request, "model_settings", {}).get("_context_reactive_retries", 0))
        if retries >= MAX_REACTIVE_RETRIES:
            raise original_exc

        messages = list(getattr(request, "messages", []) or [])
        summary = await _summarize_messages_async(request.model, messages, "reactive")
        keep_tail = _int_env("AGENT_CONTEXT_REACTIVE_KEEP_TAIL_MESSAGES", 5)
        retry_request = self._apply_summary_to_request(
            request,
            messages,
            summary,
            REACTIVE_SUMMARY_PREFIX,
            keep_tail,
            "reactive",
        )
        model_settings = dict(getattr(retry_request, "model_settings", {}) or {})
        model_settings["_context_reactive_retries"] = retries + 1
        return await handler(retry_request.override(model_settings=model_settings))

    def _apply_summary_to_request(
        self,
        request: Any,
        messages: list[Any],
        summary: str,
        prefix: str,
        keep_tail: int,
        reason: str,
    ) -> Any:
        updates = _summary_updates(messages, summary, prefix, keep_tail)
        if not updates:
            return request

        compacted_messages = add_messages(messages, updates)
        thread_key = _thread_key(getattr(request, "runtime", None), getattr(request, "state", None))
        PENDING_STATE_UPDATES[thread_key] = updates
        log_event(
            "context.summary",
            reason=reason,
            before_count=len(messages),
            after_count=len(compacted_messages),
            update_count=len(updates),
        )
        return request.override(messages=compacted_messages)

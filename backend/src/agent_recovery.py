"""Model-call recovery middleware for transient LLM failures."""

from __future__ import annotations

import asyncio
import os
import random
import time
from email.utils import parsedate_to_datetime
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain.chat_models import init_chat_model

from agent_logging import log_event

MAX_TOKEN_STOP_REASONS = {"max_tokens", "length", "max_output_tokens"}


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        parsed = int(os.getenv(name, default))
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, minimum)


def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        parsed = float(os.getenv(name, default))
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, minimum)


def is_recovery_enabled() -> bool:
    """Return whether model-call recovery is enabled."""
    return _bool_env("AGENT_RECOVERY_ENABLED", True)


def classify_recovery_error(exc: Exception) -> str | None:
    """Classify retryable provider errors using stable status/message markers."""
    text = repr(exc).lower()
    status_code = _exception_status_code(exc)

    if status_code == 429 or "rate limit" in text or "rate_limit" in text or "too many requests" in text:
        return "rate_limit"
    if status_code == 529 or "overloaded" in text or "temporarily overloaded" in text:
        return "overloaded"
    if status_code in {408, 500, 502, 503, 504}:
        return "transient"
    if any(marker in text for marker in ("timeout", "timed out", "connection", "temporarily unavailable")):
        return "transient"
    return None


def _exception_status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass

    response = getattr(exc, "response", None)
    for attr in ("status_code", "status"):
        value = getattr(response, attr, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return None


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
    if not headers:
        return None

    retry_after = None
    if hasattr(headers, "get"):
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if not retry_after:
        return None

    try:
        return max(float(retry_after), 0.0)
    except (TypeError, ValueError):
        pass

    try:
        parsed = parsedate_to_datetime(str(retry_after))
        return max(parsed.timestamp() - time.time(), 0.0)
    except (TypeError, ValueError, OSError):
        return None


def _retry_delay_seconds(attempt_index: int, exc: Exception) -> float:
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        return min(retry_after, _int_env("AGENT_RECOVERY_MAX_DELAY_MS", 32_000) / 1000)

    base_ms = _int_env("AGENT_RECOVERY_BASE_DELAY_MS", 500)
    max_ms = _int_env("AGENT_RECOVERY_MAX_DELAY_MS", 32_000)
    jitter_ratio = _float_env("AGENT_RECOVERY_JITTER_RATIO", 0.25)
    delay_ms = min(base_ms * (2**attempt_index), max_ms)
    jitter = delay_ms * jitter_ratio * random.random()
    return (delay_ms + jitter) / 1000


def _fallback_model() -> Any | None:
    model = os.getenv("LLM_FALLBACK_MODEL", "").strip()
    if not model:
        return None

    adapter = os.getenv("LLM_FALLBACK_ADAPTER_TYPE", os.getenv("LLM_ADAPTER_TYPE", "anthropic")).strip()
    _configure_fallback_provider_env(adapter)
    model_spec = f"{adapter}:{model}" if adapter and ":" not in model else model
    return init_chat_model(model_spec)


def _configure_fallback_provider_env(adapter: str) -> None:
    api_key = os.getenv("LLM_FALLBACK_API_KEY", "").strip()
    base_url = os.getenv("LLM_FALLBACK_BASE_URL", "").strip()
    provider = adapter.lower()

    if provider == "anthropic":
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
        if base_url:
            os.environ["ANTHROPIC_API_URL"] = base_url
    elif provider == "openai":
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key
        if base_url:
            os.environ["OPENAI_BASE_URL"] = base_url


def _maybe_switch_to_fallback(request: ModelRequest) -> ModelRequest:
    fallback = _fallback_model()
    if fallback is None:
        return request
    log_event("recovery.fallback_model", fallback_model=os.getenv("LLM_FALLBACK_MODEL", "").strip())
    return request.override(model=fallback)


def _response_stopped_by_max_tokens(response: ModelResponse) -> bool:
    result = getattr(response, "result", None) or []
    for message in result:
        metadata = dict(getattr(message, "response_metadata", {}) or {})
        additional = dict(getattr(message, "additional_kwargs", {}) or {})
        candidates = [
            metadata.get("stop_reason"),
            metadata.get("finish_reason"),
            metadata.get("stop"),
            additional.get("stop_reason"),
            additional.get("finish_reason"),
        ]
        if any(str(value).lower() in MAX_TOKEN_STOP_REASONS for value in candidates if value is not None):
            return True
    return False


def _escalated_token_request(request: ModelRequest) -> ModelRequest:
    model_settings = dict(getattr(request, "model_settings", {}) or {})
    max_tokens = _int_env("AGENT_RECOVERY_ESCALATED_MAX_TOKENS", 64_000, minimum=1)
    token_param = os.getenv("AGENT_RECOVERY_MAX_TOKENS_PARAM", "max_tokens").strip() or "max_tokens"
    model_settings[token_param] = max_tokens
    model_settings["_recovery_escalated_max_tokens"] = True
    log_event("recovery.max_tokens_escalate", max_tokens=max_tokens, token_param=token_param)
    return request.override(model_settings=model_settings)


class AgentRecoveryMiddleware(AgentMiddleware):
    """Retry transient model errors and recover once from output truncation."""

    def wrap_model_call(self, request: ModelRequest, handler: Any) -> ModelResponse:
        if not is_recovery_enabled():
            return handler(request)

        response = self._call_with_retries_sync(request, handler)
        if self._should_retry_for_max_tokens(request, response):
            return self._call_with_retries_sync(_escalated_token_request(request), handler)
        return response

    async def awrap_model_call(self, request: ModelRequest, handler: Any) -> ModelResponse:
        if not is_recovery_enabled():
            return await handler(request)

        response = await self._call_with_retries_async(request, handler)
        if self._should_retry_for_max_tokens(request, response):
            return await self._call_with_retries_async(_escalated_token_request(request), handler)
        return response

    def _should_retry_for_max_tokens(self, request: ModelRequest, response: ModelResponse) -> bool:
        if not _bool_env("AGENT_RECOVERY_ESCALATE_MAX_TOKENS", True):
            return False
        model_settings = dict(getattr(request, "model_settings", {}) or {})
        if model_settings.get("_recovery_escalated_max_tokens"):
            return False
        return _response_stopped_by_max_tokens(response)

    def _call_with_retries_sync(self, request: ModelRequest, handler: Any) -> ModelResponse:
        max_retries = _int_env("AGENT_RECOVERY_MAX_RETRIES", 5)
        max_529 = _int_env("AGENT_RECOVERY_MAX_CONSECUTIVE_529", 3)
        current_request = request
        overloaded_count = 0

        for attempt in range(max_retries + 1):
            try:
                return handler(current_request)
            except Exception as exc:
                reason = classify_recovery_error(exc)
                if reason is None or attempt >= max_retries:
                    raise

                overloaded_count = overloaded_count + 1 if reason == "overloaded" else 0
                if reason == "overloaded" and overloaded_count >= max_529:
                    current_request = _maybe_switch_to_fallback(current_request)
                    overloaded_count = 0

                delay = _retry_delay_seconds(attempt, exc)
                log_event("recovery.retry", reason=reason, attempt=attempt + 1, delay_seconds=delay)
                time.sleep(delay)

        raise RuntimeError("unreachable recovery retry loop")

    async def _call_with_retries_async(self, request: ModelRequest, handler: Any) -> ModelResponse:
        max_retries = _int_env("AGENT_RECOVERY_MAX_RETRIES", 5)
        max_529 = _int_env("AGENT_RECOVERY_MAX_CONSECUTIVE_529", 3)
        current_request = request
        overloaded_count = 0

        for attempt in range(max_retries + 1):
            try:
                return await handler(current_request)
            except Exception as exc:
                reason = classify_recovery_error(exc)
                if reason is None or attempt >= max_retries:
                    raise

                overloaded_count = overloaded_count + 1 if reason == "overloaded" else 0
                if reason == "overloaded" and overloaded_count >= max_529:
                    current_request = _maybe_switch_to_fallback(current_request)
                    overloaded_count = 0

                delay = _retry_delay_seconds(attempt, exc)
                log_event("recovery.retry", reason=reason, attempt=attempt + 1, delay_seconds=delay)
                await asyncio.sleep(delay)

        raise RuntimeError("unreachable recovery retry loop")

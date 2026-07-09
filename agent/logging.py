"""JSONL logging for agent, model, and tool interactions."""

from __future__ import annotations

import json
import logging
import os
import queue
import time
import traceback
from datetime import datetime, timezone
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse

ROOT_DIR = Path(__file__).resolve().parents[1]


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    """Parse common .env boolean values while keeping missing values explicit."""
    value = os.getenv(name)
    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def is_agent_logging_enabled() -> bool:
    """Return whether verbose agent interaction logging is enabled."""
    return _bool_env("AGENT_LOG_ENABLED", False)


def _log_dir() -> Path:
    configured = Path(os.getenv("AGENT_LOG_DIR", "logs"))
    if configured.is_absolute():
        return configured
    return ROOT_DIR / configured


LOG_DIR = _log_dir()
LOG_FILE = LOG_DIR / "agent.jsonl"
MAX_BYTES = _int_env("AGENT_LOG_MAX_BYTES", 10 * 1024 * 1024)
BACKUP_COUNT = _int_env("AGENT_LOG_BACKUP_COUNT", 5)


def _build_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("xu_agent")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        # Use QueueHandler+QueueListener to avoid blocking I/O in the async event loop.
        # The RotatingFileHandler runs in a background thread via the listener.
        log_queue: queue.Queue[logging.LogRecord] = queue.Queue(-1)
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter("%(message)s"))

        listener = QueueListener(log_queue, file_handler, respect_handler_level=True)
        listener.start()

        queue_handler = QueueHandler(log_queue)
        logger.addHandler(queue_handler)

    return logger


# Do not create the logs directory or file handler unless logging is explicitly enabled.
LOGGER = _build_logger() if is_agent_logging_enabled() else logging.getLogger("xu_agent.disabled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(v) for v in value]
    if hasattr(value, "model_dump"):
        return _safe_json(value.model_dump())
    if hasattr(value, "dict"):
        return _safe_json(value.dict())
    return repr(value)


def _message_to_json(message: Any) -> dict[str, Any]:
    data = _safe_json(message)
    if isinstance(data, dict):
        return data

    return {
        "type": getattr(message, "type", message.__class__.__name__),
        "content": getattr(message, "content", repr(message)),
        "tool_calls": _safe_json(getattr(message, "tool_calls", None)),
        "id": getattr(message, "id", None),
    }


def _state_messages(state: Any) -> list[Any]:
    if isinstance(state, dict):
        return state.get("messages", [])
    return getattr(state, "messages", [])


def log_event(event: str, **payload: Any) -> None:
    # Guard direct calls too, in case this module is used without the middleware.
    if not is_agent_logging_enabled():
        return

    LOGGER.info(
        json.dumps(
            {
                "ts": _now(),
                "event": event,
                **_safe_json(payload),
            },
            ensure_ascii=False,
        )
    )


class AgentLoggingMiddleware(AgentMiddleware):
    """Log every agent run, model call, tool call, and exception to JSONL."""

    def __init__(self, agent_name: str = "agent") -> None:
        super().__init__()
        self.agent_name = agent_name

    def before_agent(self, state: dict[str, Any], runtime: Any) -> None:
        self._log_agent_start(state)

    async def abefore_agent(self, state: dict[str, Any], runtime: Any) -> None:
        self._log_agent_start(state)

    def after_agent(self, state: dict[str, Any], runtime: Any) -> None:
        self._log_agent_end(state)

    async def aafter_agent(self, state: dict[str, Any], runtime: Any) -> None:
        self._log_agent_end(state)

    def _log_agent_start(self, state: Any) -> None:
        log_event(
            "agent.start",
            agent_name=self.agent_name,
            messages=[_message_to_json(message) for message in _state_messages(state)],
        )

    def _log_agent_end(self, state: Any) -> None:
        log_event(
            "agent.end",
            agent_name=self.agent_name,
            messages=[_message_to_json(message) for message in _state_messages(state)],
        )

    def wrap_model_call(self, request: ModelRequest, handler: Any) -> ModelResponse:
        start = self._log_model_start(request)
        try:
            response = handler(request)
        except Exception as exc:
            self._log_model_error(start, exc)
            raise

        self._log_model_end(start, response)
        return response

    async def awrap_model_call(self, request: ModelRequest, handler: Any) -> ModelResponse:
        start = self._log_model_start(request)
        try:
            response = await handler(request)
        except Exception as exc:
            self._log_model_error(start, exc)
            raise

        self._log_model_end(start, response)
        return response

    def _log_model_start(self, request: ModelRequest) -> float:
        start = time.perf_counter()
        model_name = getattr(request.model, "model", None) or getattr(request.model, "model_name", None)
        log_event(
            "model.start",
            agent_name=self.agent_name,
            model=repr(request.model),
            model_name=model_name,
            tool_names=[getattr(tool, "name", repr(tool)) for tool in request.tools],
            messages=[_message_to_json(message) for message in request.messages],
            system_message=_message_to_json(request.system_message) if request.system_message else None,
            model_settings=request.model_settings,
        )
        return start

    def _log_model_error(self, start: float, exc: Exception) -> None:
        log_event(
            "model.error",
            agent_name=self.agent_name,
            elapsed_ms=round((time.perf_counter() - start) * 1000, 2),
            error=repr(exc),
            traceback=traceback.format_exc(),
        )

    def _log_model_end(self, start: float, response: Any) -> None:
        result = getattr(response, "result", [response])
        log_event(
            "model.end",
            agent_name=self.agent_name,
            elapsed_ms=round((time.perf_counter() - start) * 1000, 2),
            result=[_message_to_json(message) for message in result],
            structured_response=_safe_json(getattr(response, "structured_response", None)),
        )

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        start, tool_name = self._log_tool_start(request)
        try:
            result = handler(request)
        except Exception as exc:
            self._log_tool_error(start, tool_name, exc)
            raise

        self._log_tool_end(start, tool_name, result)
        return result

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        start, tool_name = self._log_tool_start(request)
        try:
            result = await handler(request)
        except Exception as exc:
            self._log_tool_error(start, tool_name, exc)
            raise

        self._log_tool_end(start, tool_name, result)
        return result

    def _log_tool_start(self, request: Any) -> tuple[float, str | None]:
        start = time.perf_counter()
        tool_call = _safe_json(request.tool_call)
        tool_name = tool_call.get("name") if isinstance(tool_call, dict) else None
        log_event("tool.start", agent_name=self.agent_name, tool=tool_name, tool_call=tool_call)
        return start, tool_name

    def _log_tool_error(self, start: float, tool_name: str | None, exc: Exception) -> None:
        log_event(
            "tool.error",
            agent_name=self.agent_name,
            tool=tool_name,
            elapsed_ms=round((time.perf_counter() - start) * 1000, 2),
            error=repr(exc),
            traceback=traceback.format_exc(),
        )

    def _log_tool_end(self, start: float, tool_name: str | None, result: Any) -> None:
        log_event(
            "tool.end",
            agent_name=self.agent_name,
            tool=tool_name,
            elapsed_ms=round((time.perf_counter() - start) * 1000, 2),
            result=_safe_json(result),
        )

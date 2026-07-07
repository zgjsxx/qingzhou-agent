"""Tencent QQ bot bridge powered by qq-botpy."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import re
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.commands import handle_thread_slash_command
from agent.logging import log_event

ROOT_DIR = Path(__file__).resolve().parents[2]
BOTPY_LOG_DIR = ROOT_DIR / ".runtime" / "logs"

DEFAULT_HISTORY_MAX_MESSAGES = 20
DEFAULT_REPLY_MAX_CHARS = 1500
DEFAULT_TOOL_RESULT_PREVIEW_CHARS = 400

_start_lock = threading.Lock()
_started = False
_chat_histories: dict[str, list[Any]] = {}
_chat_history_lock = threading.Lock()
_chat_run_locks: dict[str, asyncio.Lock] = {}


@dataclass(frozen=True)
class BotpyMessageEvent:
    event_type: str
    message_id: str
    chat_id: str
    text: str
    sender_id: str = ""
    raw_message: Any = None


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    try:
        return int(value)
    except ValueError:
        return default


def _thread_id_for_chat(chat_id: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", chat_id).strip("_")
    return f"botpy_{safe_id or 'unknown'}"


def _history_for_thread(thread_id: str) -> list[Any]:
    with _chat_history_lock:
        return list(_chat_histories.get(thread_id, []))


def _store_thread_history(thread_id: str, messages: list[Any]) -> None:
    max_messages = max(2, _int_env("BOTPY_HISTORY_MAX_MESSAGES", DEFAULT_HISTORY_MAX_MESSAGES))
    with _chat_history_lock:
        _chat_histories[thread_id] = list(messages[-max_messages:])


def _clear_thread_history(thread_id: str) -> None:
    with _chat_history_lock:
        _chat_histories.pop(thread_id, None)


def _run_lock_for_chat(chat_id: str) -> asyncio.Lock:
    lock = _chat_run_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_run_locks[chat_id] = lock
    return lock


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content or "")


def extract_final_ai_text(result: Any) -> str:
    messages = result.get("messages", []) if isinstance(result, dict) else []
    for message in reversed(messages):
        message_type = getattr(message, "type", "")
        role = getattr(message, "role", "")
        if message_type == "ai" or role == "assistant" or message.__class__.__name__ == "AIMessage":
            text = _content_to_text(getattr(message, "content", ""))
            if text.strip():
                return text.strip()
    return ""


def _trim_preview(text: str, limit: int = DEFAULT_TOOL_RESULT_PREVIEW_CHARS) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...[truncated {len(text) - limit} chars]"


def _message_outbound_texts(message: Any) -> list[str]:
    """Convert streamed LangChain messages into QQ-friendly outbound texts.

    QQ 通道只逐条转发 AI 的自然语言文本，不转发工具调用和工具结果。
    """
    message_type = getattr(message, "type", "")
    role = getattr(message, "role", "")
    class_name = message.__class__.__name__
    outputs: list[str] = []

    if message_type == "ai" or role == "assistant" or class_name == "AIMessage":
        text = _content_to_text(getattr(message, "content", ""))
        if text.strip():
            outputs.append(text.strip())
        return outputs

    return outputs


def _message_key(message: Any, index: int) -> str:
    message_id = getattr(message, "id", None)
    if message_id:
        return f"id:{message_id}"
    message_type = getattr(message, "type", message.__class__.__name__)
    content_preview = _trim_preview(_content_to_text(getattr(message, "content", "")), 120)
    return f"{index}:{message_type}:{content_preview}"


def _stream_channel_messages_enabled() -> bool:
    return _bool_env("BOTPY_STREAM_ALL_MESSAGES", True)


def _invoke_and_collect_outbound_messages(
    graph: Any,
    *,
    input_payload: dict[str, Any],
    config: dict[str, Any],
    previous_messages: list[Any],
) -> tuple[Any, list[str]]:
    """Invoke the graph in stream mode and collect new outbound texts in order."""
    sent_keys = {_message_key(message, index) for index, message in enumerate(previous_messages)}
    outbound_texts: list[str] = []
    final_state: Any = None

    for state in graph.stream(input_payload, config=config, stream_mode="values"):
        final_state = state
        messages = state.get("messages", []) if isinstance(state, dict) else []
        for index, message in enumerate(messages):
            key = _message_key(message, index)
            if key in sent_keys:
                continue
            sent_keys.add(key)
            for text in _message_outbound_texts(message):
                if text.strip():
                    outbound_texts.append(text.strip())

    return final_state, outbound_texts


def _clean_botpy_text(text: str) -> str:
    cleaned = re.sub(r"<@!?\w+>", "", str(text or ""))
    return cleaned.strip()


def parse_botpy_message_event(event_type: str, message: Any) -> BotpyMessageEvent | None:
    message_id = str(getattr(message, "id", "") or "").strip()
    text = _clean_botpy_text(getattr(message, "content", ""))
    if not message_id:
        return None

    author = getattr(message, "author", None)
    sender_id = (
        str(getattr(author, "id", "") or "").strip()
        or str(getattr(author, "user_openid", "") or "").strip()
    )
    channel_id = str(getattr(message, "channel_id", "") or "").strip()
    guild_id = str(getattr(message, "guild_id", "") or "").strip()
    group_openid = str(getattr(message, "group_openid", "") or "").strip()
    author_openid = str(getattr(author, "user_openid", "") or "").strip()

    if event_type == "at_message_create":
        chat_id = f"guild:{guild_id}:channel:{channel_id}"
    elif event_type == "direct_message_create":
        chat_id = f"dm:{guild_id or channel_id or sender_id}"
    elif event_type == "group_at_message_create":
        chat_id = f"group:{group_openid or channel_id or sender_id}"
    elif event_type == "c2c_message_create":
        chat_id = f"c2c:{author_openid or sender_id}"
    else:
        chat_id = f"unknown:{channel_id or guild_id or sender_id or message_id}"

    return BotpyMessageEvent(
        event_type=event_type,
        message_id=message_id,
        chat_id=chat_id,
        text=text,
        sender_id=sender_id,
        raw_message=message,
    )


def _reply_chunks(text: str) -> list[str]:
    limit = max(200, _int_env("BOTPY_REPLY_MAX_CHARS", DEFAULT_REPLY_MAX_CHARS))
    if len(text) <= limit:
        return [text]
    return [text[index : index + limit] for index in range(0, len(text), limit)]


async def reply_botpy_text(message: Any, text: str) -> None:
    for chunk in _reply_chunks(text):
        await message.reply(content=chunk)


class BotpyBridgeClient:
    """Wrapper that provides botpy event handlers while reusing the local graph."""

    def __init__(self, graph: Any, appid: str, secret: str, is_sandbox: bool):
        self.graph = graph
        self.appid = appid
        self.secret = secret
        self.is_sandbox = is_sandbox
        self._client: Any | None = None

    async def _handle_message(self, event_type: str, message: Any) -> None:
        event = parse_botpy_message_event(event_type, message)
        if event is None:
            log_event("botpy.event_ignored", reason="missing_message_id", event_type=event_type)
            return

        async with _run_lock_for_chat(event.chat_id):
            await self._handle_message_locked(event)

    async def _handle_message_locked(self, event: BotpyMessageEvent) -> None:
        if not event.text:
            log_event(
                "botpy.message_unsupported",
                chat_id=event.chat_id,
                message_id=event.message_id,
                event_type=event.event_type,
            )
            await reply_botpy_text(event.raw_message, "目前只支持处理 QQ 文本消息。")
            return

        thread_id = _thread_id_for_chat(event.chat_id)
        command_result = handle_thread_slash_command(event.text, self.graph, thread_id, source="botpy")
        if command_result:
            if command_result.clear_history:
                _clear_thread_history(thread_id)
            await reply_botpy_text(event.raw_message, command_result.response)
            return

        user_content = (
            "[QQ bot message]\n"
            f"event_type: {event.event_type}\n"
            f"chat_id: {event.chat_id}\n"
            f"sender_id: {event.sender_id}\n"
            f"message_id: {event.message_id}\n\n"
            f"{event.text}"
        )
        log_event("botpy.run_start", chat_id=event.chat_id, message_id=event.message_id, thread_id=thread_id)
        try:
            if _stream_channel_messages_enabled():
                previous_messages = _history_for_thread(thread_id)
                input_payload = {"messages": [*previous_messages, {"role": "user", "content": user_content}]}
                run_config = {
                    "configurable": {"thread_id": thread_id},
                    "metadata": {"source": "botpy", "botpy_chat_id": event.chat_id},
                    "tags": ["botpy"],
                    "callbacks": [],
                }
                result, outbound_texts = await asyncio.to_thread(
                    _invoke_and_collect_outbound_messages,
                    self.graph,
                    input_payload=input_payload,
                    config=run_config,
                    previous_messages=previous_messages,
                )
                messages = result.get("messages", []) if isinstance(result, dict) else []
                if messages:
                    _store_thread_history(thread_id, list(messages))
                for text in outbound_texts:
                    await reply_botpy_text(event.raw_message, text)
                log_event("botpy.run_end", chat_id=event.chat_id, message_id=event.message_id, thread_id=thread_id)
                return

            previous_messages = _history_for_thread(thread_id)
            result = await asyncio.to_thread(
                self.graph.invoke,
                {"messages": [*previous_messages, {"role": "user", "content": user_content}]},
                {
                    "configurable": {"thread_id": thread_id},
                    "metadata": {"source": "botpy", "botpy_chat_id": event.chat_id},
                    "tags": ["botpy"],
                    "callbacks": [],
                },
            )
            messages = result.get("messages", []) if isinstance(result, dict) else []
            if messages:
                _store_thread_history(thread_id, list(messages))
            answer = extract_final_ai_text(result) or "我处理完了，但没有生成可发送的文本回复。"
            await reply_botpy_text(event.raw_message, answer)
            log_event("botpy.run_end", chat_id=event.chat_id, message_id=event.message_id, thread_id=thread_id)
        except Exception as exc:
            log_event(
                "botpy.run_error",
                chat_id=event.chat_id,
                message_id=event.message_id,
                thread_id=thread_id,
                error=repr(exc),
                traceback=traceback.format_exc(),
            )
            await reply_botpy_text(event.raw_message, f"处理 QQ 消息时出错：{exc}")

    def run_forever(self) -> None:
        try:
            import botpy
        except ImportError as exc:
            raise RuntimeError("QQ bot mode requires: pip install qq-botpy") from exc

        BOTPY_LOG_DIR.mkdir(parents=True, exist_ok=True)
        botpy.configure_logging(ext_handlers=[{
            "handler": logging.handlers.TimedRotatingFileHandler,
            "filename": str(BOTPY_LOG_DIR / "%(name)s.log"),
            "when": "D",
            "backupCount": 7,
            "encoding": "utf-8",
        }])

        intents = botpy.Intents(
            public_guild_messages=_bool_env("BOTPY_PUBLIC_GUILD_MESSAGES", True),
            direct_message=_bool_env("BOTPY_DIRECT_MESSAGE", True),
            public_messages=_bool_env("BOTPY_PUBLIC_MESSAGES", False),
        )

        bridge = self

        class _Client(botpy.Client):
            async def on_at_message_create(self, message):
                await bridge._handle_message("at_message_create", message)

            async def on_direct_message_create(self, message):
                await bridge._handle_message("direct_message_create", message)

            async def on_group_at_message_create(self, message):
                await bridge._handle_message("group_at_message_create", message)

            async def on_c2c_message_create(self, message):
                await bridge._handle_message("c2c_message_create", message)

            async def on_ready(self):
                log_event("botpy.ready")

        self._client = _Client(intents=intents, is_sandbox=self.is_sandbox)
        log_event(
            "botpy.start",
            appid=self.appid,
            sandbox=self.is_sandbox,
            public_guild_messages=_bool_env("BOTPY_PUBLIC_GUILD_MESSAGES", True),
            direct_message=_bool_env("BOTPY_DIRECT_MESSAGE", True),
            public_messages=_bool_env("BOTPY_PUBLIC_MESSAGES", False),
        )
        print("[xu-agent botpy] bridge starting", file=sys.stderr, flush=True)
        self._client.run(appid=self.appid, secret=self.secret)


def _run_with_blockbuster_skip(func: Any) -> None:
    try:
        from blockbuster.blockbuster import blockbuster_skip
    except ImportError:
        func()
        return

    token = blockbuster_skip.set(True)
    try:
        func()
    finally:
        blockbuster_skip.reset(token)


def _run_botpy_bridge_forever(bridge: BotpyBridgeClient) -> None:
    """Run botpy inside a dedicated thread-local event loop.

    qq-botpy 1.2.x constructs its Client with ``asyncio.get_event_loop()`` in
    ``Client.__init__``. A plain daemon thread has no default loop on Python
    3.11+, so we must create and bind one before instantiating/running the SDK.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        _run_with_blockbuster_skip(bridge.run_forever)
    finally:
        try:
            loop.close()
        finally:
            asyncio.set_event_loop(None)


def start_botpy_bridge(graph: Any) -> None:
    """Start the optional QQ bot bridge in a daemon thread."""
    global _started

    if not _bool_env("BOTPY_ENABLED", False):
        return
    appid = os.getenv("BOTPY_APPID", "").strip()
    secret = os.getenv("BOTPY_SECRET", "").strip()
    if not appid or not secret:
        log_event("botpy.disabled", reason="missing_appid_or_secret")
        return

    with _start_lock:
        if _started:
            return
        _started = True

    bridge = BotpyBridgeClient(
        graph=graph,
        appid=appid,
        secret=secret,
        is_sandbox=_bool_env("BOTPY_SANDBOX", False),
    )

    def run_bridge() -> None:
        try:
            _run_botpy_bridge_forever(bridge)
        except Exception as exc:
            log_event("botpy.error", error=repr(exc))
            print(f"[xu-agent botpy] bridge stopped: {exc}", file=sys.stderr, flush=True)

    thread = threading.Thread(
        target=run_bridge,
        name="botpy-bridge",
        daemon=True,
    )
    thread.start()

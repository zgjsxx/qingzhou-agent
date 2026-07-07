"""Telegram Bot API bridge for the local LangGraph agent."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_commands import handle_thread_slash_command
from agent_config import config_section
from agent_logging import log_event

DEFAULT_HISTORY_MAX_MESSAGES = 20
DEFAULT_REPLY_MAX_CHARS = 4000
TELEGRAM_UPLOAD_DIR = (
    Path(__file__).resolve().parents[2] / "backend" / ".agent_uploads" / "telegram"
)

_start_lock = threading.Lock()
_started = False
_chat_histories: dict[str, list[Any]] = {}
_chat_history_lock = threading.Lock()


@dataclass
class TelegramMessageEvent:
    message_id: str
    chat_id: str
    chat_type: str
    sender_id: str
    text: str
    message: Any
    attachment_path: str = ""
    attachment_type: str = ""


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _config_value(env_name: str, key: str, default: Any = "") -> Any:
    env_value = os.getenv(env_name)
    if env_value is not None and env_value.strip():
        return env_value
    return config_section("telegram").get(key, default)


def _int_value(env_name: str, key: str, default: int) -> int:
    try:
        return int(_config_value(env_name, key, default))
    except (TypeError, ValueError):
        return default


def _float_value(env_name: str, key: str, default: float) -> float:
    try:
        return float(_config_value(env_name, key, default))
    except (TypeError, ValueError):
        return default


def _enabled() -> bool:
    return _bool_value(_config_value("TELEGRAM_ENABLED", "enabled", False))


def _allowed_users() -> set[str]:
    raw = str(_config_value("TELEGRAM_ALLOWED_USERS", "allowedUsers", ""))
    return {item.strip() for item in raw.split(",") if item.strip()}


def _thread_id_for_chat(chat_id: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_.:@-]+", "_", chat_id).strip("_")
    return f"telegram_{safe_id or 'unknown'}"


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return str(content or "")


def _message_key(message: Any, index: int) -> str:
    message_id = getattr(message, "id", None)
    if message_id:
        return f"id:{message_id}"
    return (
        f"{index}:{getattr(message, 'type', message.__class__.__name__)}:"
        f"{_content_to_text(getattr(message, 'content', ''))[:120]}"
    )


def _collect_ai_texts(
    graph: Any,
    input_payload: dict[str, Any],
    config: dict[str, Any],
    previous_messages: list[Any],
) -> tuple[Any, list[str]]:
    seen = {
        _message_key(message, index)
        for index, message in enumerate(previous_messages)
    }
    texts: list[str] = []
    final_state: Any = None
    for state in graph.stream(input_payload, config=config, stream_mode="values"):
        final_state = state
        messages = state.get("messages", []) if isinstance(state, dict) else []
        for index, message in enumerate(messages):
            key = _message_key(message, index)
            if key in seen:
                continue
            seen.add(key)
            if (
                getattr(message, "type", "") == "ai"
                or getattr(message, "role", "") == "assistant"
                or message.__class__.__name__ == "AIMessage"
            ):
                text = _content_to_text(getattr(message, "content", "")).strip()
                if text:
                    texts.append(text)
    return final_state, texts


def _safe_filename(name: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned[:180] or fallback


class TelegramBridge:
    def __init__(
        self,
        graph: Any,
        token: str,
        allowed_users: set[str],
    ):
        self.graph = graph
        self.token = token
        self.allowed_users = allowed_users
        self.pending_events: dict[str, list[TelegramMessageEvent]] = {}
        self.pending_tasks: dict[str, asyncio.Task] = {}
        self.chat_locks: dict[str, asyncio.Lock] = {}

    def _is_allowed(self, sender_id: str) -> bool:
        return not self.allowed_users or sender_id in self.allowed_users

    async def _download_attachment(
        self,
        message: Any,
        context: Any,
    ) -> tuple[str, str]:
        file_id = ""
        filename = ""
        attachment_type = ""
        if getattr(message, "document", None):
            document = message.document
            file_id = str(document.file_id)
            filename = _safe_filename(
                str(document.file_name or ""),
                f"document-{message.message_id}",
            )
            attachment_type = "document"
        elif getattr(message, "photo", None):
            photo = message.photo[-1]
            file_id = str(photo.file_id)
            filename = f"photo-{message.message_id}.jpg"
            attachment_type = "image"
        if not file_id:
            return "", ""

        await asyncio.to_thread(
            TELEGRAM_UPLOAD_DIR.mkdir,
            parents=True,
            exist_ok=True,
        )
        target = TELEGRAM_UPLOAD_DIR / filename
        telegram_file = await context.bot.get_file(file_id)
        await telegram_file.download_to_drive(custom_path=target)
        return str(target.resolve()), attachment_type

    async def handle_update(self, update: Any, context: Any) -> None:
        message = getattr(update, "effective_message", None)
        chat = getattr(update, "effective_chat", None)
        sender = getattr(update, "effective_user", None)
        if message is None or chat is None or sender is None:
            return

        sender_id = str(sender.id)
        if getattr(sender, "is_bot", False) or not self._is_allowed(sender_id):
            log_event(
                "telegram.event_ignored",
                reason="sender_not_allowed",
                sender_id=sender_id,
            )
            return

        text = str(
            getattr(message, "text", None)
            or getattr(message, "caption", None)
            or ""
        ).strip()
        chat_type = str(getattr(chat, "type", "private") or "private")
        if chat_type != "private":
            bot_username = str(getattr(context.bot, "username", "") or "")
            mention = bool(
                bot_username
                and re.search(
                    rf"@{re.escape(bot_username)}\b",
                    text,
                    flags=re.IGNORECASE,
                )
            )
            reply_user = getattr(
                getattr(message, "reply_to_message", None),
                "from_user",
                None,
            )
            reply_to_bot = bool(
                reply_user
                and str(getattr(reply_user, "id", ""))
                == str(getattr(context.bot, "id", ""))
            )
            require_mention = _bool_value(
                _config_value(
                    "TELEGRAM_REQUIRE_MENTION",
                    "requireMention",
                    True,
                ),
                True,
            )
            if require_mention and not (
                text.startswith("/") or mention or reply_to_bot
            ):
                return
            if bot_username:
                text = re.sub(
                    rf"@{re.escape(bot_username)}\b",
                    "",
                    text,
                    flags=re.IGNORECASE,
                ).strip()

        attachment_path, attachment_type = await self._download_attachment(
            message,
            context,
        )
        if not text and not attachment_path:
            return

        event = TelegramMessageEvent(
            message_id=str(message.message_id),
            chat_id=str(chat.id),
            chat_type=chat_type,
            sender_id=sender_id,
            text=text,
            message=message,
            attachment_path=attachment_path,
            attachment_type=attachment_type,
        )
        log_event(
            "telegram.message_received",
            chat_id=event.chat_id,
            sender_id=event.sender_id,
            message_id=event.message_id,
            attachment_type=event.attachment_type,
            text_preview=event.text[:200],
        )
        await self._enqueue(event)

    async def _enqueue(self, event: TelegramMessageEvent) -> None:
        self.pending_events.setdefault(event.chat_id, []).append(event)
        old_task = self.pending_tasks.pop(event.chat_id, None)
        if old_task and not old_task.done():
            old_task.cancel()
        task = asyncio.create_task(self._flush_after_delay(event.chat_id))
        self.pending_tasks[event.chat_id] = task

    async def _flush_after_delay(self, chat_id: str) -> None:
        wait = max(
            0.0,
            min(
                _float_value(
                    "TELEGRAM_MERGE_WAIT_SECONDS",
                    "mergeWaitSeconds",
                    3.0,
                ),
                10.0,
            ),
        )
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return
        events = self.pending_events.pop(chat_id, [])
        self.pending_tasks.pop(chat_id, None)
        if not events:
            return
        lock = self.chat_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            await self._process_events(events)

    async def _reply_text(self, message: Any, text: str) -> None:
        limit = max(
            200,
            _int_value(
                "TELEGRAM_REPLY_MAX_CHARS",
                "replyMaxChars",
                DEFAULT_REPLY_MAX_CHARS,
            ),
        )
        for index in range(0, len(text), limit):
            await message.reply_text(text[index : index + limit])

    async def _process_events(
        self,
        events: list[TelegramMessageEvent],
    ) -> None:
        latest = events[-1]
        thread_id = _thread_id_for_chat(latest.chat_id)
        text_only = "\n".join(event.text for event in events if event.text)
        if len(events) == 1 and text_only:
            command_result = handle_thread_slash_command(
                text_only,
                self.graph,
                thread_id,
                source="telegram",
            )
            if command_result:
                if command_result.clear_history:
                    with _chat_history_lock:
                        _chat_histories.pop(thread_id, None)
                await self._reply_text(latest.message, command_result.response)
                return

        fragments: list[str] = []
        for index, event in enumerate(events, start=1):
            parts = [event.text] if event.text else []
            if event.attachment_path:
                parts.append(
                    f"{event.attachment_type} attachment:\n"
                    f"  path: {event.attachment_path}\n"
                    "  (the attachment is available to local file tools)"
                )
            fragments.append(f"[{index}] " + "\n".join(parts))
        user_content = (
            "[Telegram message]\n"
            f"chat_id: {latest.chat_id}\n"
            f"chat_type: {latest.chat_type}\n"
            f"sender_id: {latest.sender_id}\n\n"
            + "\n".join(fragments)
        )
        with _chat_history_lock:
            previous_messages = list(_chat_histories.get(thread_id, []))
        run_config = {
            "configurable": {"thread_id": thread_id},
            "metadata": {
                "source": "telegram",
                "telegram_chat_id": latest.chat_id,
            },
            "tags": ["telegram"],
            "callbacks": [],
        }
        log_event(
            "telegram.run_start",
            chat_id=latest.chat_id,
            message_ids=[event.message_id for event in events],
            thread_id=thread_id,
        )
        try:
            result, outbound_texts = await asyncio.to_thread(
                _collect_ai_texts,
                self.graph,
                {
                    "messages": [
                        *previous_messages,
                        {"role": "user", "content": user_content},
                    ]
                },
                run_config,
                previous_messages,
            )
            messages = (
                result.get("messages", []) if isinstance(result, dict) else []
            )
            if messages:
                max_messages = max(
                    2,
                    _int_value(
                        "TELEGRAM_HISTORY_MAX_MESSAGES",
                        "historyMaxMessages",
                        DEFAULT_HISTORY_MAX_MESSAGES,
                    ),
                )
                with _chat_history_lock:
                    _chat_histories[thread_id] = list(
                        messages[-max_messages:]
                    )
            if not outbound_texts:
                outbound_texts = [
                    "I finished processing the request but did not generate "
                    "a text response."
                ]
            stream_all = _bool_value(
                _config_value(
                    "TELEGRAM_STREAM_ALL_MESSAGES",
                    "streamAllMessages",
                    True,
                ),
                True,
            )
            if not stream_all:
                outbound_texts = outbound_texts[-1:]
            for outbound_text in outbound_texts:
                await self._reply_text(latest.message, outbound_text)
            log_event(
                "telegram.run_end",
                chat_id=latest.chat_id,
                message_ids=[event.message_id for event in events],
                thread_id=thread_id,
            )
        except Exception as exc:
            log_event(
                "telegram.run_error",
                chat_id=latest.chat_id,
                thread_id=thread_id,
                error=repr(exc),
                traceback=traceback.format_exc(),
            )
            await self._reply_text(
                latest.message,
                f"Error while processing Telegram message: {exc}",
            )

    def run_forever(self) -> None:
        try:
            from telegram.ext import Application, MessageHandler, filters
        except ImportError as exc:
            raise RuntimeError(
                "Telegram mode requires: pip install python-telegram-bot"
            ) from exc

        application = Application.builder().token(self.token).build()
        application.add_handler(
            MessageHandler(
                filters.TEXT | filters.PHOTO | filters.Document.ALL,
                self.handle_update,
            )
        )
        log_event(
            "telegram.start",
            allowed_users=sorted(self.allowed_users),
        )
        print(
            "[xu-agent telegram] bridge starting",
            file=sys.stderr,
            flush=True,
        )
        application.run_polling(
            allowed_updates=["message"],
            close_loop=False,
            stop_signals=None,
        )


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


def _run_bridge(bridge: TelegramBridge) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        _run_with_blockbuster_skip(bridge.run_forever)
    finally:
        try:
            loop.close()
        finally:
            asyncio.set_event_loop(None)


def start_telegram_bridge(graph: Any) -> None:
    """Start the optional Telegram Bot API bridge in a daemon thread."""
    global _started
    if not _enabled():
        return
    token = str(_config_value("TELEGRAM_BOT_TOKEN", "botToken", "")).strip()
    if not token:
        log_event("telegram.disabled", reason="missing_bot_token")
        return
    with _start_lock:
        if _started:
            return
        _started = True
    bridge = TelegramBridge(graph, token, _allowed_users())
    thread = threading.Thread(
        target=lambda: _run_bridge(bridge),
        name="telegram-bot-bridge",
        daemon=True,
    )
    thread.start()

"""Discord Gateway bridge for the local LangGraph agent."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_commands import handle_thread_slash_command
from agent_config import config_section
from agent_logging import log_event

DEFAULT_HISTORY_MAX_MESSAGES = 20
DEFAULT_REPLY_MAX_CHARS = 1900
DEFAULT_SEND_RETRIES = 3
DEFAULT_CONNECT_RETRY_SECONDS = 15.0
DISCORD_UPLOAD_DIR = (
    Path(__file__).resolve().parents[3] / ".agent_uploads" / "discord"
)

_start_lock = threading.Lock()
_started = False
_chat_histories: dict[str, list[Any]] = {}
_chat_history_lock = threading.Lock()


@dataclass
class DiscordMessageEvent:
    message_id: str
    channel_id: str
    guild_id: str
    sender_id: str
    text: str
    message: Any
    attachment_paths: list[str]


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _config_value(env_name: str, key: str, default: Any = "") -> Any:
    env_value = os.getenv(env_name)
    if env_value is not None and env_value.strip():
        return env_value
    return config_section("discord").get(key, default)


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
    return _bool_value(_config_value("DISCORD_ENABLED", "enabled", False))


def _proxy_value() -> str:
    configured = str(_config_value("DISCORD_PROXY", "proxy", "")).strip()
    if configured:
        return configured
    return (
        os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("http_proxy")
        or ""
    ).strip()


def _allowed_users() -> set[str]:
    raw = str(_config_value("DISCORD_ALLOWED_USERS", "allowedUsers", ""))
    return {item.strip() for item in raw.split(",") if item.strip()}


def _thread_id_for_channel(channel_id: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_.:@-]+", "_", channel_id).strip("_")
    return f"discord_{safe_id or 'unknown'}"


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


class DiscordBridge:
    def __init__(
        self,
        graph: Any,
        token: str,
        allowed_users: set[str],
    ):
        self.graph = graph
        self.token = token
        self.allowed_users = allowed_users
        self.pending_events: dict[str, list[DiscordMessageEvent]] = {}
        self.pending_tasks: dict[str, asyncio.Task] = {}
        self.channel_locks: dict[str, asyncio.Lock] = {}

    def _is_allowed(self, sender_id: str) -> bool:
        return not self.allowed_users or sender_id in self.allowed_users

    async def _download_attachments(self, message: Any) -> list[str]:
        attachments = list(getattr(message, "attachments", []) or [])
        if not attachments:
            return []
        await asyncio.to_thread(
            DISCORD_UPLOAD_DIR.mkdir,
            parents=True,
            exist_ok=True,
        )
        paths: list[str] = []
        for index, attachment in enumerate(attachments, start=1):
            filename = _safe_filename(
                str(getattr(attachment, "filename", "") or ""),
                f"attachment-{getattr(message, 'id', 'unknown')}-{index}",
            )
            target = DISCORD_UPLOAD_DIR / filename
            await attachment.save(target)
            paths.append(str(target.resolve()))
        return paths

    def _is_dm(self, message: Any) -> bool:
        return getattr(message, "guild", None) is None

    def _mentioned_bot(self, message: Any, bot_user: Any) -> bool:
        bot_id = str(getattr(bot_user, "id", "") or "")
        for user in getattr(message, "mentions", []) or []:
            if str(getattr(user, "id", "") or "") == bot_id:
                return True
        content = str(getattr(message, "content", "") or "")
        return bool(bot_id and re.search(rf"<@!?{re.escape(bot_id)}>", content))

    def _reply_to_bot(self, message: Any, bot_user: Any) -> bool:
        reference = getattr(message, "reference", None)
        resolved = getattr(reference, "resolved", None)
        author = getattr(resolved, "author", None)
        return bool(
            author
            and str(getattr(author, "id", "") or "")
            == str(getattr(bot_user, "id", "") or "")
        )

    def _strip_bot_mention(self, text: str, bot_user: Any) -> str:
        bot_id = str(getattr(bot_user, "id", "") or "")
        if not bot_id:
            return text.strip()
        return re.sub(rf"<@!?{re.escape(bot_id)}>", "", text).strip()

    async def handle_message(self, message: Any, bot_user: Any) -> None:
        author = getattr(message, "author", None)
        channel = getattr(message, "channel", None)
        if author is None or channel is None:
            return
        if getattr(author, "bot", False):
            return

        sender_id = str(getattr(author, "id", "") or "")
        if not self._is_allowed(sender_id):
            log_event(
                "discord.event_ignored",
                reason="sender_not_allowed",
                sender_id=sender_id,
            )
            return

        text = str(getattr(message, "content", "") or "").strip()
        if not self._is_dm(message):
            require_mention = _bool_value(
                _config_value(
                    "DISCORD_REQUIRE_MENTION",
                    "requireMention",
                    True,
                ),
                True,
            )
            if require_mention and not (
                text.startswith("/")
                or self._mentioned_bot(message, bot_user)
                or self._reply_to_bot(message, bot_user)
            ):
                return
            text = self._strip_bot_mention(text, bot_user)

        attachment_paths = await self._download_attachments(message)
        if not text and not attachment_paths:
            return

        guild = getattr(message, "guild", None)
        event = DiscordMessageEvent(
            message_id=str(getattr(message, "id", "")),
            channel_id=str(getattr(channel, "id", "")),
            guild_id=str(getattr(guild, "id", "") or ""),
            sender_id=sender_id,
            text=text,
            message=message,
            attachment_paths=attachment_paths,
        )
        log_event(
            "discord.message_received",
            channel_id=event.channel_id,
            guild_id=event.guild_id,
            sender_id=event.sender_id,
            message_id=event.message_id,
            attachment_count=len(event.attachment_paths),
            text_preview=event.text[:200],
        )
        await self._enqueue(event)

    async def _enqueue(self, event: DiscordMessageEvent) -> None:
        self.pending_events.setdefault(event.channel_id, []).append(event)
        old_task = self.pending_tasks.pop(event.channel_id, None)
        if old_task and not old_task.done():
            old_task.cancel()
        task = asyncio.create_task(self._flush_after_delay(event.channel_id))
        self.pending_tasks[event.channel_id] = task

    async def _flush_after_delay(self, channel_id: str) -> None:
        wait = max(
            0.0,
            min(
                _float_value(
                    "DISCORD_MERGE_WAIT_SECONDS",
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
        events = self.pending_events.pop(channel_id, [])
        self.pending_tasks.pop(channel_id, None)
        if not events:
            return
        lock = self.channel_locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            await self._process_events(events)

    async def _reply_text(self, message: Any, text: str) -> None:
        limit = max(
            200,
            _int_value(
                "DISCORD_REPLY_MAX_CHARS",
                "replyMaxChars",
                DEFAULT_REPLY_MAX_CHARS,
            ),
        )
        for index in range(0, len(text), limit):
            chunk = text[index : index + limit]
            await self._send_chunk_with_retry(message, chunk)

    async def _send_chunk_with_retry(self, message: Any, chunk: str) -> None:
        retries = max(
            1,
            min(
                _int_value(
                    "DISCORD_SEND_RETRIES",
                    "sendRetries",
                    DEFAULT_SEND_RETRIES,
                ),
                5,
            ),
        )
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                await message.reply(chunk, mention_author=False)
                return
            except Exception as reply_exc:
                last_error = reply_exc
            try:
                await message.channel.send(chunk)
                return
            except Exception:
                send_exc = sys.exc_info()[1]
                if isinstance(send_exc, Exception):
                    last_error = send_exc
                log_event(
                    "discord.send_retry",
                    channel_id=str(getattr(message.channel, "id", "")),
                    message_id=str(getattr(message, "id", "")),
                    attempt=attempt,
                    retries=retries,
                    error=repr(last_error),
                )
                if attempt < retries:
                    await asyncio.sleep(min(2 ** (attempt - 1), 5))
        raise RuntimeError(f"Discord send failed after {retries} attempts: {last_error}")

    async def _process_events(
        self,
        events: list[DiscordMessageEvent],
    ) -> None:
        latest = events[-1]
        thread_id = _thread_id_for_channel(latest.channel_id)
        text_only = "\n".join(event.text for event in events if event.text)
        if len(events) == 1 and text_only:
            command_result = handle_thread_slash_command(
                text_only,
                self.graph,
                thread_id,
                source="discord",
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
            for attachment_path in event.attachment_paths:
                parts.append(
                    "attachment:\n"
                    f"  path: {attachment_path}\n"
                    "  (the attachment is available to local file tools)"
                )
            fragments.append(f"[{index}] " + "\n".join(parts))
        user_content = (
            "[Discord message]\n"
            f"channel_id: {latest.channel_id}\n"
            f"guild_id: {latest.guild_id or '(dm)'}\n"
            f"sender_id: {latest.sender_id}\n\n"
            + "\n".join(fragments)
        )
        with _chat_history_lock:
            previous_messages = list(_chat_histories.get(thread_id, []))
        run_config = {
            "configurable": {"thread_id": thread_id},
            "metadata": {
                "source": "discord",
                "discord_channel_id": latest.channel_id,
                "discord_guild_id": latest.guild_id,
            },
            "tags": ["discord"],
            "callbacks": [],
        }
        log_event(
            "discord.run_start",
            channel_id=latest.channel_id,
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
                        "DISCORD_HISTORY_MAX_MESSAGES",
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
                    "DISCORD_STREAM_ALL_MESSAGES",
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
                "discord.run_end",
                channel_id=latest.channel_id,
                message_ids=[event.message_id for event in events],
                thread_id=thread_id,
            )
        except Exception as exc:
            log_event(
                "discord.run_error",
                channel_id=latest.channel_id,
                thread_id=thread_id,
                error=repr(exc),
                traceback=traceback.format_exc(),
            )
            try:
                await self._reply_text(
                    latest.message,
                    f"Error while processing Discord message: {exc}",
                )
            except Exception as reply_exc:
                log_event(
                    "discord.error_reply_failed",
                    channel_id=latest.channel_id,
                    thread_id=thread_id,
                    error=repr(reply_exc),
                    traceback=traceback.format_exc(),
                )

    async def _run_client(self) -> None:
        try:
            import discord
        except ImportError as exc:
            raise RuntimeError(
                "Discord mode requires: pip install discord.py"
            ) from exc

        bridge = self
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.dm_messages = True
        intents.guild_messages = True

        class _Client(discord.Client):
            async def on_ready(self) -> None:
                log_event(
                    "discord.start",
                    user_id=str(getattr(self.user, "id", "")),
                    user_name=str(getattr(self.user, "name", "")),
                    allowed_users=sorted(bridge.allowed_users),
                )
                print(
                    "[xu-agent discord] bridge connected",
                    file=sys.stderr,
                    flush=True,
                )

            async def on_message(self, message: Any) -> None:
                await bridge.handle_message(message, self.user)

        proxy = _proxy_value()
        client_options: dict[str, Any] = {"intents": intents}
        if proxy:
            client_options["proxy"] = proxy
        client = _Client(**client_options)
        log_event(
            "discord.connecting",
            allowed_users=sorted(self.allowed_users),
            proxy_configured=bool(proxy),
        )
        print(
            "[xu-agent discord] bridge starting",
            file=sys.stderr,
            flush=True,
        )
        await client.start(self.token)

    def run_forever(self) -> None:
        asyncio.run(self._run_client())


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


def _run_bridge(bridge: DiscordBridge) -> None:
    retry_seconds = max(
        1.0,
        min(
            _float_value(
                "DISCORD_CONNECT_RETRY_SECONDS",
                "connectRetrySeconds",
                DEFAULT_CONNECT_RETRY_SECONDS,
            ),
            300.0,
        ),
    )
    while True:
        try:
            _run_with_blockbuster_skip(bridge.run_forever)
            return
        except Exception as exc:
            log_event(
                "discord.bridge_error",
                error=repr(exc),
                traceback=traceback.format_exc(),
                retry_seconds=retry_seconds,
            )
            print(
                f"[xu-agent discord] bridge stopped: {exc}; "
                f"retrying in {retry_seconds:g}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(retry_seconds)


def start_discord_bridge(graph: Any) -> None:
    """Start the optional Discord Gateway bridge in a daemon thread."""
    global _started
    if not _enabled():
        return
    token = str(_config_value("DISCORD_BOT_TOKEN", "botToken", "")).strip()
    if not token:
        log_event("discord.disabled", reason="missing_bot_token")
        return
    with _start_lock:
        if _started:
            return
        _started = True
    bridge = DiscordBridge(graph, token, _allowed_users())
    thread = threading.Thread(
        target=lambda: _run_bridge(bridge),
        name="discord-bot-bridge",
        daemon=True,
    )
    thread.start()

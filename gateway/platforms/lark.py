"""Feishu/Lark long-connection bridge for the local agent."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.types import Command

from agent.commands import handle_thread_slash_command
from agent.logging import log_event
from agent.tts import FALLBACK_PROVIDER, synthesize_speech, tts_enabled

LARK_API_BASE_URL = "https://open.feishu.cn/open-apis"
DEFAULT_HISTORY_MAX_MESSAGES = 20
DEFAULT_REPLY_MAX_CHARS = 12000
DEFAULT_TOOL_RESULT_PREVIEW_CHARS = 400
LARK_ACK_EMOJI = os.getenv("LARK_ACK_EMOJI_TYPE", "OK")
LARK_DONE_EMOJI = os.getenv("LARK_DONE_EMOJI_TYPE", "DONE")
LARK_ERROR_EMOJI = os.getenv("LARK_ERROR_EMOJI_TYPE", "CROSS_MARK")
ROOT_DIR = Path(__file__).resolve().parents[2]
LARK_UPLOAD_DIR = ROOT_DIR / ".agent_uploads" / "lark"
LARK_TTS_DIR = ROOT_DIR / ".agent_outputs" / "lark_tts"
MERGE_WAIT_SECONDS = max(0.0, min(float(os.getenv("LARK_MERGE_WAIT_SECONDS", "10.0")), 10.0))
QINGZHOU_AUDIO_MARKER_REGEX = re.compile(r"\[\[qingzhou-audio:(\{.*?\})\]\]", re.DOTALL)
LOCAL_DOWNLOAD_URL_REGEX = re.compile(r'(?:https?://[^/\s<>)"\']+)?/api/local/downloads/[^\s<>)"\']+', re.IGNORECASE)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
TEXT_FILE_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".yaml", ".yml"}
WORD_FILE_SUFFIXES = {".doc", ".docx"}
EXCEL_FILE_SUFFIXES = {".xls", ".xlsx"}

_start_lock = threading.Lock()
_started = False
_chat_histories: dict[str, list[Any]] = {}
_chat_history_lock = threading.Lock()
_chat_run_locks: dict[str, threading.Lock] = {}
_chat_run_locks_guard = threading.Lock()
_seen_message_ids: dict[str, float] = {}
_seen_lock = threading.Lock()
_token_lock = threading.Lock()
_tenant_access_token_value = ""
_tenant_access_token_expires_at = 0.0


@dataclass
class LarkPendingApproval:
    approval_id: str
    thread_id: str
    chat_id: str
    requester_id: str
    interrupt_value: Any
    message_ids: tuple[str, ...]
    reaction_ids: tuple[str, ...]
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class LarkMessageEvent:
    message_id: str
    chat_id: str
    message_type: str
    text: str
    chat_type: str = ""
    sender_id: str = ""
    mention_ids: tuple[str, ...] = field(default_factory=tuple)
    mention_names: tuple[str, ...] = field(default_factory=tuple)
    file_key: str = ""
    image_key: str = ""
    filename: str = ""
    duration_ms: int = 0


def _safe_lark_id(value: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", str(value or "")).strip("_")
    return safe_id or "unknown"


def _lark_group_thread_scope() -> str:
    scope = (
        os.getenv("LARK_GROUP_THREAD_SCOPE", "")
        or os.getenv("FEISHU_GROUP_THREAD_SCOPE", "")
        or "sender"
    ).strip().lower()
    if scope in {"chat", "shared"}:
        return "chat"
    return "sender"


def _lark_context_key_for_event(event: LarkMessageEvent) -> str:
    if _is_lark_group_chat(event) and _lark_group_thread_scope() == "sender" and event.sender_id:
        return f"{event.chat_id}:{event.sender_id}"
    return event.chat_id


class _PendingBuffer:
    """Buffer for merging rapid-fire Lark messages from the same context.

    When a user sends file+text or multiple quick messages, Lark delivers
    them as separate events. This buffer collects messages from the same
    conversation context within a configurable time window before submitting
    a single merged prompt to the LLM.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, list[LarkMessageEvent]] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._reaction_ids: dict[str, list[str]] = {}

    def add(self, event: LarkMessageEvent, reaction_id: str, on_flush: Any) -> None:
        """Add event to its context buffer and reset the merge timer."""
        context_key = _lark_context_key_for_event(event)
        with self._lock:
            self._events.setdefault(context_key, []).append(event)
            self._reaction_ids.setdefault(context_key, []).append(reaction_id)
            # Cancel existing timer
            old_timer = self._timers.pop(context_key, None)
            if old_timer is not None:
                old_timer.cancel()
            # Start new timer
            wait = MERGE_WAIT_SECONDS if MERGE_WAIT_SECONDS > 0 else 0.05
            timer = threading.Timer(wait, self._flush, args=[context_key, on_flush])
            timer.daemon = True
            self._timers[context_key] = timer
            timer.start()

    def set_reaction(self, context_key: str, message_id: str, reaction_id: str) -> bool:
        """Attach an asynchronously-created reaction to a buffered message."""
        with self._lock:
            events = self._events.get(context_key, [])
            reaction_ids = self._reaction_ids.get(context_key, [])
            for index, event in enumerate(events):
                if event.message_id == message_id:
                    reaction_ids[index] = reaction_id
                    return True
        return False

    def _flush(self, context_key: str, on_flush: Any) -> None:
        """Timer expired — merge all buffered events and invoke callback."""
        with self._lock:
            events = self._events.pop(context_key, [])
            reaction_ids = self._reaction_ids.pop(context_key, [])
            self._timers.pop(context_key, None)
        if events:
            on_flush(events, reaction_ids)


_pending_buffer = _PendingBuffer()


class DaemonWorkerPool:
    """Small daemon-thread worker pool that cannot block interpreter exit."""

    def __init__(self, max_workers: int, thread_name_prefix: str):
        self._queue: queue.Queue[tuple[Any, tuple[Any, ...]] | None] = queue.Queue()
        self._threads = [
            threading.Thread(
                target=self._worker,
                name=f"{thread_name_prefix}_{index}",
                daemon=True,
            )
            for index in range(max_workers)
        ]
        for thread in self._threads:
            thread.start()

    def _worker(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is None:
                    return
                func, args = task
                func(*args)
            except Exception as exc:
                log_event("lark.worker_error", error=repr(exc), traceback=traceback.format_exc())
            finally:
                self._queue.task_done()

    def submit(self, func: Any, *args: Any) -> None:
        self._queue.put((func, args))

    def shutdown(self, wait: bool = True) -> None:
        for _ in self._threads:
            self._queue.put(None)
        if wait:
            for thread in self._threads:
                thread.join()


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


def _get_value(source: Any, *path: str) -> Any:
    value = source
    for key in path:
        if value is None:
            return None
        if isinstance(value, dict):
            value = value.get(key)
        else:
            value = getattr(value, key, None)
    return value


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_text_content(message_type: str, content: Any) -> str:
    data = _parse_json_object(content)
    if message_type == "text":
        return str(data.get("text") or "").strip()
    if message_type == "post":
        fragments: list[str] = []
        for blocks in data.get("content", []):
            if not isinstance(blocks, list):
                continue
            for item in blocks:
                if isinstance(item, dict) and item.get("tag") == "text":
                    fragments.append(str(item.get("text") or ""))
        return "".join(fragments).strip()
    return ""


def _collect_lark_mentions(*sources: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    ids: list[str] = []
    names: list[str] = []

    def add_id(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in ids:
            ids.append(text)

    def add_name(value: Any) -> None:
        text = str(value or "").strip().lstrip("@")
        if text and text not in names:
            names.append(text)

    def visit(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            parsed = _parse_json_object(value)
            if parsed:
                visit(parsed)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if isinstance(value, dict):
            text = str(value.get("text") or "")
            for token in re.findall(r"@_[A-Za-z0-9_]+", text):
                add_name(token)
            tag = str(value.get("tag") or "").lower()
            if tag in {"at", "mention"}:
                add_name(value.get("user_name") or value.get("name") or value.get("text"))
                add_id(value.get("user_id") or value.get("open_id") or value.get("union_id"))
            for key in ("key", "name", "display_name", "user_name", "tenant_key"):
                add_name(value.get(key))
            id_data = value.get("id")
            if isinstance(id_data, dict):
                for key in ("open_id", "user_id", "union_id"):
                    add_id(id_data.get(key))
            for key in ("open_id", "user_id", "union_id"):
                add_id(value.get(key))
            for key in ("mentions", "content"):
                visit(value.get(key))
            return
        for key in ("key", "name", "display_name", "user_name", "tenant_key"):
            add_name(getattr(value, key, None))
        id_data = getattr(value, "id", None)
        if id_data is not None:
            visit(id_data)
        for key in ("open_id", "user_id", "union_id"):
            add_id(getattr(value, key, None))
        mentions = getattr(value, "mentions", None)
        if mentions is not None:
            visit(mentions)

    for source in sources:
        visit(source)
    return tuple(ids), tuple(names)


def parse_lark_message_event(data: Any) -> LarkMessageEvent | None:
    """Extract the fields we need from the SDK event object."""
    event = _get_value(data, "event") or data
    message = _get_value(event, "message")
    if message is None:
        return None

    message_id = str(_get_value(message, "message_id") or "").strip()
    chat_id = str(_get_value(message, "chat_id") or "").strip()
    chat_type = str(_get_value(message, "chat_type") or "").strip()
    message_type = str(_get_value(message, "message_type") or "").strip()
    content = _get_value(message, "content")
    content_data = _parse_json_object(content)
    text = _extract_text_content(message_type, content)
    mention_ids, mention_names = _collect_lark_mentions(_get_value(message, "mentions"), content_data)
    sender_id = (
        str(_get_value(event, "sender", "sender_id", "open_id") or "").strip()
        or str(_get_value(event, "sender", "sender_id", "user_id") or "").strip()
    )

    file_key = ""
    image_key = ""
    filename = ""
    duration_ms = 0
    if message_type == "file":
        file_key = str(content_data.get("file_key") or "").strip()
        filename = str(content_data.get("file_name") or "").strip()
    elif message_type == "image":
        image_key = str(content_data.get("image_key") or "").strip()
    elif message_type in {"audio", "media"}:
        file_key = str(content_data.get("file_key") or "").strip()
        filename = str(content_data.get("file_name") or content_data.get("filename") or "").strip()
        try:
            duration_ms = int(content_data.get("duration") or content_data.get("duration_ms") or 0)
        except (TypeError, ValueError):
            duration_ms = 0

    if not message_id or not chat_id:
        return None
    return LarkMessageEvent(
        message_id=message_id,
        chat_id=chat_id,
        chat_type=chat_type,
        message_type=message_type,
        text=text,
        sender_id=sender_id,
        mention_ids=mention_ids,
        mention_names=mention_names,
        file_key=file_key,
        image_key=image_key,
        filename=filename,
        duration_ms=duration_ms,
    )


def _safe_event_repr(data: Any, limit: int = 1000) -> str:
    try:
        raw = repr(data)
    except Exception as exc:
        raw = f"<repr failed: {exc!r}>"
    return raw if len(raw) <= limit else f"{raw[:limit]}...[truncated {len(raw) - limit} chars]"


def _thread_id_for_chat(chat_id: str) -> str:
    return f"lark_{_safe_lark_id(chat_id)}"


def _thread_id_for_event(event: LarkMessageEvent) -> str:
    context_key = _lark_context_key_for_event(event)
    if context_key == event.chat_id:
        return _thread_id_for_chat(event.chat_id)
    return f"lark_{_safe_lark_id(event.chat_id)}__{_safe_lark_id(event.sender_id)}"


def _csv_env_values(*names: str) -> set[str]:
    values: set[str] = set()
    for name in names:
        raw = os.getenv(name, "")
        for item in raw.split(","):
            value = item.strip()
            if value:
                values.add(value)
    return values


def _lark_group_require_mention() -> bool:
    if "LARK_GROUP_REQUIRE_MENTION" in os.environ:
        return _bool_env("LARK_GROUP_REQUIRE_MENTION", True)
    return _bool_env("LARK_REQUIRE_MENTION", True)


def _is_lark_group_chat(event: LarkMessageEvent) -> bool:
    chat_type = event.chat_type.strip().lower()
    return chat_type in {"group", "chat", "group_chat"}


def _is_bot_mentioned(event: LarkMessageEvent) -> bool:
    bot_ids = _csv_env_values(
        "LARK_BOT_OPEN_ID",
        "FEISHU_BOT_OPEN_ID",
        "LARK_BOT_USER_ID",
        "FEISHU_BOT_USER_ID",
        "LARK_BOT_UNION_ID",
        "FEISHU_BOT_UNION_ID",
    )
    mentioned_ids = {value for value in event.mention_ids if value}
    if bot_ids:
        return bool(bot_ids & mentioned_ids)

    bot_names = _csv_env_values("LARK_BOT_NAME", "FEISHU_BOT_NAME")
    mentioned_names = {value.lstrip("@") for value in event.mention_names if value}
    if bot_names:
        return bool(bot_names & mentioned_names)

    # Without configured bot identity, any explicit mention is treated as a
    # trigger. This keeps group chats quiet while avoiding a brittle setup step.
    if mentioned_ids or mentioned_names:
        return True
    return bool(re.search(r"(^|\s)@_[A-Za-z0-9_]+(\s|$)", event.text or ""))


def _is_lark_sender_allowed(event: LarkMessageEvent) -> bool:
    allowed_users = _csv_env_values("LARK_ALLOWED_USERS", "FEISHU_ALLOWED_USERS")
    if not allowed_users:
        return True
    return bool(event.sender_id and event.sender_id in allowed_users)


def _is_lark_approval_operator_allowed(operator_id: str, pending: LarkPendingApproval) -> bool:
    if operator_id and operator_id == pending.requester_id:
        return True
    approval_users = _csv_env_values("LARK_APPROVAL_ALLOWED_USERS", "FEISHU_APPROVAL_ALLOWED_USERS")
    return bool(operator_id and operator_id in approval_users)


def should_process_lark_event(event: LarkMessageEvent) -> bool:
    if not _is_lark_sender_allowed(event):
        return False
    if not _is_lark_group_chat(event):
        return True
    if not _lark_group_require_mention():
        return True
    return _is_bot_mentioned(event)


def _remember_seen_message(message_id: str) -> bool:
    now = time.time()
    ttl_seconds = _int_env("LARK_DEDUP_TTL_SECONDS", 3600)
    with _seen_lock:
        stale_ids = [key for key, seen_at in _seen_message_ids.items() if now - seen_at > ttl_seconds]
        for key in stale_ids:
            _seen_message_ids.pop(key, None)
        if message_id in _seen_message_ids:
            return False
        _seen_message_ids[message_id] = now
        return True


def _history_for_thread(thread_id: str) -> list[Any]:
    with _chat_history_lock:
        return list(_chat_histories.get(thread_id, []))


def _store_thread_history(thread_id: str, messages: list[Any]) -> None:
    max_messages = max(2, _int_env("LARK_HISTORY_MAX_MESSAGES", DEFAULT_HISTORY_MAX_MESSAGES))
    with _chat_history_lock:
        _chat_histories[thread_id] = list(messages[-max_messages:])


def _clear_thread_history(thread_id: str) -> None:
    with _chat_history_lock:
        _chat_histories.pop(thread_id, None)


def _run_lock_for_context(context_key: str) -> threading.Lock:
    with _chat_run_locks_guard:
        return _chat_run_locks.setdefault(context_key, threading.Lock())


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


def _strip_qingzhou_audio_markers(text: str) -> str:
    return QINGZHOU_AUDIO_MARKER_REGEX.sub("", str(text or "")).strip()


def _local_download_url_to_path(url: str) -> Path | None:
    parsed = urllib.parse.urlparse(str(url or ""))
    path = urllib.parse.unquote(parsed.path or "")
    prefix = "/api/local/downloads/"
    if not path.startswith(prefix):
        return None
    relative_path = path[len(prefix) :].lstrip("/\\")
    candidate = (ROOT_DIR / relative_path).resolve()
    try:
        candidate.relative_to(ROOT_DIR)
    except ValueError:
        return None
    return candidate


def _extract_local_download_paths(text: str, allowed_suffixes: set[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for match in LOCAL_DOWNLOAD_URL_REGEX.finditer(str(text or "")):
        candidate = _local_download_url_to_path(match.group(0).rstrip(".,;:"))
        if candidate is None or candidate.suffix.lower() not in allowed_suffixes:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        paths.append(candidate)
    return paths


def _extract_qingzhou_audio_marker_paths(text: str) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for match in QINGZHOU_AUDIO_MARKER_REGEX.finditer(str(text or "")):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        candidate: Path | None = None
        raw_path = str(payload.get("path") or "").strip()
        if raw_path:
            candidate = Path(raw_path).expanduser().resolve()
            try:
                candidate.relative_to(ROOT_DIR)
            except ValueError:
                candidate = None
        if candidate is None:
            candidate = _local_download_url_to_path(str(payload.get("url") or ""))
        if candidate is None or candidate in seen:
            continue
        seen.add(candidate)
        paths.append(candidate)
    return paths


def _lark_file_kind(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in TEXT_FILE_SUFFIXES:
        return "text"
    if suffix in WORD_FILE_SUFFIXES:
        return "word"
    if suffix in EXCEL_FILE_SUFFIXES:
        return "excel"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    return "file"


def _lark_file_usage_hint(kind: str) -> str:
    if kind == "text":
        return "Read it as a local text file when the user asks about its contents."
    if kind == "word":
        return "Use Word/docx parsing tools or shell utilities to inspect the document."
    if kind == "excel":
        return "Use spreadsheet/xlsx parsing tools or shell utilities to inspect workbook sheets and cells."
    if kind == "pdf":
        return "Use PDF tools or shell utilities to inspect the document."
    if kind == "image":
        return "Use image-aware tools or file utilities to inspect the image."
    return "Use file or shell utilities to inspect the local file when needed."


def _event_to_text_fragment(event: LarkMessageEvent, app_id: str, app_secret: str) -> str:
    """Convert a single LarkMessageEvent into a text fragment for the merged prompt."""
    if event.text:
        return f"文本消息: {event.text}"

    if event.message_type in {"audio", "media"} and event.file_key:
        info = _download_lark_resource(
            event.message_id,
            event.file_key,
            "audio",
            preferred_filename=event.filename,
            app_id=app_id,
            app_secret=app_secret,
        )
        if info.get("error"):
            return f"语音消息: (下载失败: {info['error']})"
        transcript = _transcribe_lark_audio(info["path"])
        if transcript.get("error"):
            return (
                f"语音消息:\n"
                f"  filename: {info['filename']}\n"
                f"  size: {info['size']}\n"
                f"  path: {info['path']}\n"
                f"  transcription_error: {transcript['error']}"
            )
        text = str(transcript.get("text") or "").strip()
        if not text:
            text = "(未识别到文字)"
        duration = f"\n  duration_ms: {event.duration_ms}" if event.duration_ms else ""
        return (
            f"语音消息:{duration}\n"
            f"  filename: {info['filename']}\n"
            f"  size: {info['size']}\n"
            f"  path: {info['path']}\n"
            f"  transcription: {text}"
        )

    if event.message_type == "file" and event.file_key:
        info = _download_lark_resource(
            event.message_id,
            event.file_key,
            "files",
            preferred_filename=event.filename,
            app_id=app_id,
            app_secret=app_secret,
        )
        if not info.get("error"):
            file_kind = _lark_file_kind(info.get("filename") or event.filename)
            return (
                f"File message:\n"
                f"  filename: {info['filename']}\n"
                f"  kind: {file_kind}\n"
                f"  size: {info['size']}\n"
                f"  path: {info['path']}\n"
                f"  note: The file has been downloaded locally. {_lark_file_usage_hint(file_kind)}"
            )
        if info.get("error"):
            return f"文件消息: (下载失败: {info['error']})"
        return (
            f"文件消息:\n"
            f"  filename: {info['filename']}\n"
            f"  size: {info['size']}\n"
            f"  path: {info['path']}\n"
            f"  (文件已下载到本地，可以用 shell 工具或文件工具查看内容)"
        )

    if event.message_type == "image" and event.image_key:
        info = _download_lark_resource(event.message_id, event.image_key, "images", app_id=app_id, app_secret=app_secret)
        if info.get("error"):
            return f"图片消息: (下载失败: {info['error']})"
        return (
            f"图片消息:\n"
            f"  filename: {info['filename']}\n"
            f"  size: {info['size']}\n"
            f"  path: {info['path']}\n"
            f"  (图片已下载到本地，可以用文件工具查看)"
        )

    return f"[{event.message_type}消息]"


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
    """Convert streamed LangChain messages into channel-friendly texts.

    飞书通道只逐条转发 AI 的可见文本，不转发工具调用和工具结果。
    这样用户能看到思考过程中的自然语言阶段性输出，但不会被底层工具噪音刷屏。
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
    return _bool_env("LARK_STREAM_ALL_MESSAGES", True)


def _invoke_and_forward_messages(
    graph: Any,
    *,
    input_payload: dict[str, Any],
    config: dict[str, Any],
    previous_messages: list[Any],
    on_text: Any,
) -> Any:
    """Invoke the graph and forward each newly produced AI/tool message in order."""
    sent_keys = {_message_key(message, index) for index, message in enumerate(previous_messages)}
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
                    on_text(text.strip())

    return final_state


def _tenant_token_request(app_id: str, app_secret: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{LARK_API_BASE_URL}/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_tenant_access_token(app_id: str, app_secret: str) -> str:
    global _tenant_access_token_value
    global _tenant_access_token_expires_at

    now = time.time()
    with _token_lock:
        if _tenant_access_token_value and now < _tenant_access_token_expires_at - 120:
            return _tenant_access_token_value
        data = _tenant_token_request(app_id, app_secret)
        token = str(data.get("tenant_access_token") or "")
        if not token:
            raise RuntimeError(f"Failed to get Lark tenant_access_token: {data}")
        _tenant_access_token_value = token
        _tenant_access_token_expires_at = now + int(data.get("expire") or 7200)
        return _tenant_access_token_value


def _request_lark_json(path: str, token: str, payload: dict[str, Any] | None = None, *, method: str = "POST", query: dict[str, str] | None = None) -> dict[str, Any]:
    query_string = f"?{urllib.parse.urlencode(query)}" if query else ""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(
        f"{LARK_API_BASE_URL}{path}{query_string}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Lark API HTTP {exc.code}: {detail}") from exc


def _post_lark_json(path: str, token: str, payload: dict[str, Any], query: dict[str, str] | None = None) -> dict[str, Any]:
    return _request_lark_json(path, token, payload, query=query)


def _multipart_form_data(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"qingzhou-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    for name, (filename, data, content_type) in files.items():
        safe_filename = filename.replace('"', "_")
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{safe_filename}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                data,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary


def _request_lark_multipart(
    path: str,
    token: str,
    *,
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
    timeout: int = 30,
) -> dict[str, Any]:
    data, boundary = _multipart_form_data(fields, files)
    request = urllib.request.Request(
        f"{LARK_API_BASE_URL}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Lark API HTTP {exc.code}: {detail}") from exc


def _request_local_multipart(
    url: str,
    *,
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
    timeout: int = 120,
) -> dict[str, Any]:
    data, boundary = _multipart_form_data(fields, files)
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _download_lark_resource(
    message_id: str,
    resource_key: str,
    resource_type: str,
    *,
    preferred_filename: str = "",
    app_id: str,
    app_secret: str,
) -> dict[str, str]:
    """Download an image or file from Lark via the message resources API.

    Uses /im/v1/messages/{message_id}/resources/{file_key}?type=...
    which allows the bot to download resources sent by users (not just by the bot).

    Returns dict with keys: path, filename, size (human-readable).
    """
    LARK_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    token = _get_tenant_access_token(app_id, app_secret)

    # type 参数：file/audio 消息用 "file"，image 消息用 "image"。
    # 如果飞书后续要求 audio 专用 type，可通过 LARK_AUDIO_RESOURCE_TYPE 覆盖。
    if resource_type == "images":
        resource_type_param = "image"
    elif resource_type == "audio":
        resource_type_param = os.getenv("LARK_AUDIO_RESOURCE_TYPE", "file").strip() or "file"
    else:
        resource_type_param = "file"
    api_path = f"/im/v1/messages/{message_id}/resources/{resource_key}"
    query_string = f"?type={resource_type_param}"
    url = f"{LARK_API_BASE_URL}{api_path}{query_string}"
    request = urllib.request.Request(
        url,
        data=None,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content_disposition = response.headers.get("Content-Disposition", "")
            # Extract filename from Content-Disposition.
            # 飞书可能返回两种格式：
            #   filename="report.pdf"              (ASCII)
            #   filename*=UTF-8''%E6%8A%A5%E5%91%8A.pdf  (RFC 5987 中文)
            # 消息事件中的 file_name 已由 SDK 按 JSON/UTF-8 正确解码，应优先使用。
            # HTTP Content-Disposition 的普通 filename 可能被 urllib 按 Latin-1
            # 解释，直接使用会把“设备参数表”保存成“è®¾å¤...”一类乱码。
            remote_filename = preferred_filename.strip()
            if not remote_filename and content_disposition:
                # 优先尝试 RFC 5987 filename* (支持中文)
                match_star = re.search(r'filename\*\s*=\s*UTF-8\'\'([^\s;]+)', content_disposition, re.IGNORECASE)
                if match_star:
                    remote_filename = urllib.parse.unquote(match_star.group(1))
                else:
                    # fallback: filename="xxx"
                    match = re.search(r'filename\s*=\s*"([^"]+)"', content_disposition)
                    if match:
                        remote_filename = urllib.parse.unquote(match.group(1))

            data = response.read()
            size_bytes = len(data)

            # Determine local filename — only strip chars unsafe for Windows filesystem
            if remote_filename:
                safe_name = re.sub(r'[<>:"/\\|?*]', '_', remote_filename)
            else:
                if resource_type == "images":
                    ext = ".png"
                elif resource_type == "audio":
                    ext = ".opus"
                else:
                    ext = ".bin"
                safe_name = f"{resource_key}{ext}"

            local_path = LARK_UPLOAD_DIR / safe_name
            local_path.write_bytes(data)

            # Human-readable size
            if size_bytes >= 1024 * 1024:
                size_str = f"{size_bytes / 1024 / 1024:.1f}MB"
            elif size_bytes >= 1024:
                size_str = f"{size_bytes / 1024:.0f}KB"
            else:
                size_str = f"{size_bytes}B"

            return {
                "path": str(local_path),
                "filename": safe_name,
                "size": size_str,
            }
    except urllib.error.HTTPError as exc:
        # HTTPError 包含飞书返回的详细 JSON 错误信息
        error_body = ""
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        log_event("lark.download_error", resource_type=resource_type, resource_key=resource_key, http_status=exc.code, error_body=error_body, error=repr(exc))
        return {
            "path": "",
            "filename": "",
            "size": "",
            "error": f"HTTP {exc.code}: {error_body or repr(exc)}",
        }
    except Exception as exc:
        log_event("lark.download_error", resource_type=resource_type, resource_key=resource_key, error=repr(exc))
        return {
            "path": "",
            "filename": "",
            "size": "",
            "error": str(exc),
        }


def _asr_server_url() -> str:
    configured = os.getenv("QINGZHOU_ASR_URL", "").strip().rstrip("/")
    return configured or "http://127.0.0.1:8765"


def _audio_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix in {".wav", ".wave"}:
        return "audio/wav"
    if suffix == ".ogg":
        return "audio/ogg"
    if suffix == ".webm":
        return "audio/webm"
    if suffix in {".m4a", ".mp4"}:
        return "audio/mp4"
    if suffix == ".opus":
        return "audio/ogg"
    return "application/octet-stream"


def _transcribe_with_asr_server(audio_path: Path, language: str) -> dict[str, Any]:
    response = _request_local_multipart(
        f"{_asr_server_url()}/transcribe",
        fields={"language": language},
        files={"file": (audio_path.name, audio_path.read_bytes(), _audio_content_type(audio_path))},
        timeout=max(10, _int_env("LARK_ASR_TIMEOUT_SECONDS", 180)),
    )
    if not response.get("ok"):
        raise RuntimeError(f"ASR server returned failure: {response}")
    return response


def _transcribe_lark_audio(audio_path: str | Path) -> dict[str, Any]:
    path = Path(audio_path).expanduser().resolve()
    language = os.getenv("LARK_ASR_LANGUAGE", os.getenv("SENSEVOICE_LANGUAGE", "auto")).strip() or "auto"
    try:
        result = _transcribe_with_asr_server(path, language)
        log_event("lark.audio_transcribed", path=str(path), provider="asr_server")
        return result
    except Exception as exc:
        log_event("lark.audio_asr_server_error", path=str(path), error=repr(exc))
        if not _bool_env("LARK_ASR_LOCAL_FALLBACK", True):
            return {"text": "", "error": str(exc), "error_type": type(exc).__name__}

    try:
        from agent.asr import transcribe_audio

        result = transcribe_audio(path, language=language)
        result.pop("raw", None)
        log_event("lark.audio_transcribed", path=str(path), provider="local")
        return result
    except Exception as exc:
        log_event(
            "lark.audio_transcribe_error",
            path=str(path),
            error=repr(exc),
            traceback=traceback.format_exc(),
        )
        return {"text": "", "error": str(exc), "error_type": type(exc).__name__}


def _reply_chunks(text: str) -> list[str]:
    limit = max(1000, _int_env("LARK_REPLY_MAX_CHARS", DEFAULT_REPLY_MAX_CHARS))
    if len(text) <= limit:
        return [text]
    return [text[index : index + limit] for index in range(0, len(text), limit)]


def _lark_markdown_card(content: str) -> dict[str, Any]:
    """Build a Feishu Card JSON 2.0 payload for an Agent Markdown reply."""
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "style": {
                "text_size": {
                    "normal_v2": {
                        "default": "normal",
                        "pc": "normal",
                        "mobile": "normal",
                    }
                }
            },
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": [
                {
                    "tag": "markdown",
                    "content": content,
                    "text_align": "left",
                    "text_size": "normal_v2",
                    "margin": "0px 0px 0px 0px",
                }
            ],
        },
    }


def _interrupts_from_result(result: Any) -> list[Any]:
    if not isinstance(result, dict):
        return []
    interrupts = result.get("__interrupt__") or []
    if isinstance(interrupts, list):
        return interrupts
    return [interrupts]


def _interrupt_value(interrupt_obj: Any) -> Any:
    return getattr(interrupt_obj, "value", interrupt_obj)


def _approval_summary(interrupt_value: Any) -> tuple[str, str, dict[str, Any]]:
    action_requests = _get_value(interrupt_value, "action_requests") or []
    action = action_requests[0] if isinstance(action_requests, list) and action_requests else {}
    name = str(_get_value(action, "name") or "tool_call")
    description = str(_get_value(action, "description") or "This action requires approval.")
    args = _get_value(action, "args") or {}
    return name, description, args if isinstance(args, dict) else {}


def _json_preview(value: Any, limit: int = 1200) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        text = repr(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...[truncated {len(text) - limit} chars]"


def _lark_approval_card(approval_id: str, interrupt_value: Any) -> dict[str, Any]:
    tool_name, description, args = _approval_summary(interrupt_value)
    args_preview = _json_preview(args)
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**需要审批工具调用**\n\n"
                        f"- 工具：`{tool_name}`\n"
                        f"- 原因：{description}\n\n"
                        f"参数：\n```json\n{args_preview}\n```"
                    ),
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "批准"},
                    "type": "primary",
                    "value": {"approval_id": approval_id, "decision": "approve"},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "拒绝"},
                    "type": "danger",
                    "value": {"approval_id": approval_id, "decision": "reject"},
                },
            ],
        },
    }


def send_lark_approval_card(chat_id: str, approval_id: str, interrupt_value: Any, *, app_id: str, app_secret: str) -> None:
    token = _get_tenant_access_token(app_id, app_secret)
    response = _send_lark_message(
        chat_id,
        token,
        msg_type="interactive",
        content=_lark_approval_card(approval_id, interrupt_value),
    )
    if int(response.get("code") or 0) != 0:
        raise RuntimeError(f"Lark send approval card failed: {response}")


def _send_lark_message(
    chat_id: str,
    token: str,
    *,
    msg_type: str,
    content: dict[str, Any],
) -> dict[str, Any]:
    return _post_lark_json(
        "/im/v1/messages",
        token,
        {
            "receive_id": chat_id,
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False),
        },
        query={"receive_id_type": "chat_id"},
    )


def _ffmpeg_executable() -> str:
    configured = os.getenv("LARK_FFMPEG", "").strip()
    if configured:
        return configured
    return shutil.which("ffmpeg") or ""


def _convert_audio_to_opus(input_path: Path) -> Path:
    if input_path.suffix.lower() in {".opus", ".webm"} and _bool_env("LARK_DIRECT_OPUS_UPLOAD", True):
        return input_path

    ffmpeg = _ffmpeg_executable()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to convert TTS WAV output to Feishu audio opus")

    LARK_TTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = LARK_TTS_DIR / f"{input_path.stem}.opus"
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libopus",
        "-b:a",
        os.getenv("LARK_AUDIO_BITRATE", "24k"),
        str(output_path),
    ]
    process = subprocess.run(
        command,
        cwd=str(Path(__file__).resolve().parents[2]),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=max(5, _int_env("LARK_AUDIO_CONVERT_TIMEOUT_SECONDS", 60)),
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "").strip()
        raise RuntimeError(f"ffmpeg audio conversion failed: {detail or process.returncode}")
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError(f"ffmpeg did not create opus audio: {output_path}")
    return output_path


def _upload_lark_file(
    file_path: str | Path,
    *,
    file_type: str,
    app_id: str,
    app_secret: str,
    token: str = "",
) -> str:
    path = Path(file_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Lark upload file does not exist: {path}")

    upload_token = token or _get_tenant_access_token(app_id, app_secret)
    suffix = path.suffix.lower()
    if suffix == ".webm":
        content_type = "audio/webm"
    elif file_type == "opus":
        content_type = "audio/ogg"
    else:
        content_type = "application/octet-stream"
    response = _request_lark_multipart(
        "/im/v1/files",
        upload_token,
        fields={"file_type": file_type, "file_name": path.name},
        files={"file": (path.name, path.read_bytes(), content_type)},
        timeout=max(10, _int_env("LARK_UPLOAD_TIMEOUT_SECONDS", 60)),
    )
    if int(response.get("code") or 0) != 0:
        raise RuntimeError(f"Lark upload file failed: {response}")
    file_key = str(_get_value(response, "data", "file_key") or "").strip()
    if not file_key:
        raise RuntimeError(f"Lark upload file returned no file_key: {response}")
    return file_key


def _image_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".gif":
        return "image/gif"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".bmp":
        return "image/bmp"
    return "image/png"


def _upload_lark_image(
    file_path: str | Path,
    *,
    app_id: str,
    app_secret: str,
    token: str = "",
) -> str:
    path = Path(file_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Lark upload image does not exist: {path}")

    upload_token = token or _get_tenant_access_token(app_id, app_secret)
    response = _request_lark_multipart(
        "/im/v1/images",
        upload_token,
        fields={"image_type": "message"},
        files={"image": (path.name, path.read_bytes(), _image_content_type(path))},
        timeout=max(10, _int_env("LARK_UPLOAD_TIMEOUT_SECONDS", 60)),
    )
    if int(response.get("code") or 0) != 0:
        raise RuntimeError(f"Lark upload image failed: {response}")
    image_key = str(_get_value(response, "data", "image_key") or "").strip()
    if not image_key:
        raise RuntimeError(f"Lark upload image returned no image_key: {response}")
    return image_key


def send_lark_image(chat_id: str, image_path: str | Path, *, app_id: str, app_secret: str) -> None:
    token = _get_tenant_access_token(app_id, app_secret)
    image_key = _upload_lark_image(image_path, app_id=app_id, app_secret=app_secret, token=token)
    response = _send_lark_message(
        chat_id,
        token,
        msg_type="image",
        content={"image_key": image_key},
    )
    if int(response.get("code") or 0) != 0:
        raise RuntimeError(f"Lark send image failed: {response}")


def send_lark_images_from_text(chat_id: str, text: str, *, app_id: str, app_secret: str) -> bool:
    sent_any = False
    for image_path in _extract_local_download_paths(text, IMAGE_SUFFIXES):
        if not image_path.exists() or not image_path.is_file():
            log_event("lark.image_missing", chat_id=chat_id, path=str(image_path))
            continue
        try:
            send_lark_image(chat_id, image_path, app_id=app_id, app_secret=app_secret)
        except Exception as exc:
            log_event(
                "lark.image_error",
                chat_id=chat_id,
                path=str(image_path),
                error=repr(exc),
                traceback=traceback.format_exc(),
            )
            continue
        log_event("lark.image_sent", chat_id=chat_id, path=str(image_path))
        sent_any = True
    return sent_any


def send_lark_audio(chat_id: str, audio_path: str | Path, *, app_id: str, app_secret: str) -> None:
    token = _get_tenant_access_token(app_id, app_secret)
    opus_path = _convert_audio_to_opus(Path(audio_path))
    file_key = _upload_lark_file(opus_path, file_type="opus", app_id=app_id, app_secret=app_secret, token=token)
    response = _send_lark_message(
        chat_id,
        token,
        msg_type="audio",
        content={"file_key": file_key},
    )
    if int(response.get("code") or 0) != 0:
        raise RuntimeError(f"Lark send audio failed: {response}")


def send_lark_audio_markers(chat_id: str, text: str, *, app_id: str, app_secret: str) -> bool:
    sent_any = False
    for audio_path in _extract_qingzhou_audio_marker_paths(text):
        if not audio_path.exists() or not audio_path.is_file():
            log_event("lark.marker_audio_missing", chat_id=chat_id, path=str(audio_path))
            continue
        try:
            send_lark_audio(chat_id, audio_path, app_id=app_id, app_secret=app_secret)
        except Exception as exc:
            log_event(
                "lark.marker_audio_error",
                chat_id=chat_id,
                path=str(audio_path),
                error=repr(exc),
                traceback=traceback.format_exc(),
            )
            continue
        log_event("lark.marker_audio_sent", chat_id=chat_id, path=str(audio_path))
        sent_any = True
    return sent_any


def _lark_voice_reply_enabled() -> bool:
    return _bool_env("LARK_VOICE_REPLY_ENABLED", False)


def send_lark_voice_reply(chat_id: str, text: str, *, app_id: str, app_secret: str) -> bool:
    if not _lark_voice_reply_enabled():
        return False
    if not tts_enabled():
        log_event("lark.voice_skipped", chat_id=chat_id, reason="tts_disabled")
        return False

    content = _strip_qingzhou_audio_markers(text)
    if not content:
        return False

    result = synthesize_speech(
        content,
        voice=os.getenv("LARK_TTS_VOICE", ""),
        audio_format=os.getenv("LARK_TTS_FORMAT", "opus"),
    )
    try:
        send_lark_audio(chat_id, result["path"], app_id=app_id, app_secret=app_secret)
    except Exception:
        if str(result.get("provider") or "") != "edge_tts":
            raise
        fallback = synthesize_speech(
            content,
            provider=FALLBACK_PROVIDER,
            voice=os.getenv("LARK_TTS_VOICE", ""),
            audio_format="wav",
        )
        send_lark_audio(chat_id, fallback["path"], app_id=app_id, app_secret=app_secret)
        result = {**fallback, "fallback_from": "edge_tts"}
    log_event(
        "lark.voice_sent",
        chat_id=chat_id,
        filename=result.get("filename", ""),
        voice=result.get("voice", ""),
    )
    return True


def send_lark_text(chat_id: str, text: str, *, app_id: str, app_secret: str) -> None:
    token = _get_tenant_access_token(app_id, app_secret)
    clean_text = _strip_qingzhou_audio_markers(text)
    if not clean_text:
        return
    for chunk in _reply_chunks(clean_text):
        response: dict[str, Any] | None = None
        card_error: Exception | None = None
        if _bool_env("LARK_MARKDOWN_ENABLED", True):
            try:
                response = _send_lark_message(
                    chat_id,
                    token,
                    msg_type="interactive",
                    content=_lark_markdown_card(chunk),
                )
                if int(response.get("code") or 0) != 0:
                    raise RuntimeError(f"Lark send card failed: {response}")
            except Exception as exc:
                card_error = exc
                log_event("lark.markdown_fallback", chat_id=chat_id, error=repr(exc))

        if response is None or card_error is not None:
            response = _send_lark_message(
                chat_id,
                token,
                msg_type="text",
                content={"text": chunk},
            )
        if int(response.get("code") or 0) != 0:
            raise RuntimeError(f"Lark send message failed: {response}")


def add_lark_reaction(message_id: str, emoji_type: str = LARK_ACK_EMOJI, *, app_id: str, app_secret: str) -> str:
    """Add an emoji reaction to a message, return the reaction_id."""
    token = _get_tenant_access_token(app_id, app_secret)
    path = f"/im/v1/messages/{message_id}/reactions"
    response = _request_lark_json(path, token, {"reaction_type": {"emoji_type": emoji_type}})
    if int(response.get("code") or 0) != 0:
        raise RuntimeError(f"Lark add reaction failed: {response}")
    return str(_get_value(response, "data", "reaction_id") or "")


def delete_lark_reaction(message_id: str, reaction_id: str, *, app_id: str, app_secret: str) -> dict[str, Any]:
    """Delete an emoji reaction from a message."""
    token = _get_tenant_access_token(app_id, app_secret)
    path = f"/im/v1/messages/{message_id}/reactions/{reaction_id}"
    request = urllib.request.Request(
        f"{LARK_API_BASE_URL}{path}",
        data=None,
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        log_event("lark.reaction_delete_error", message_id=message_id, reaction_id=reaction_id, error=f"HTTP {exc.code}: {detail}")
        return {"code": exc.code}


class LarkWsBridge:
    def __init__(self, graph: Any, app_id: str, app_secret: str):
        self.graph = graph
        self.app_id = app_id
        self.app_secret = app_secret
        self.executor = DaemonWorkerPool(max_workers=2, thread_name_prefix="lark-agent")
        self.reaction_executor = DaemonWorkerPool(max_workers=2, thread_name_prefix="lark-reaction")
        self._reaction_lock = threading.Lock()
        self._late_reactions: dict[str, str] = {}
        self._completed_messages: dict[str, tuple[float, str]] = {}
        self._approval_lock = threading.Lock()
        self._pending_approvals: dict[str, LarkPendingApproval] = {}

    def handle_event(self, data: Any) -> None:
        log_event("lark.event_received", raw=_safe_event_repr(data))
        print("[xu-agent lark] event received", file=sys.stderr, flush=True)
        event = parse_lark_message_event(data)
        if event is None:
            log_event("lark.event_ignored", reason="missing_message", raw=_safe_event_repr(data))
            return
        log_event(
            "lark.message_received",
            chat_id=event.chat_id,
            message_id=event.message_id,
            message_type=event.message_type,
            text_preview=event.text[:200] if event.text else "",
        )
        if not _remember_seen_message(event.message_id):
            log_event("lark.event_ignored", reason="duplicate", message_id=event.message_id)
            return
        if not _is_lark_sender_allowed(event):
            log_event(
                "lark.event_ignored",
                reason="user_not_allowed",
                chat_id=event.chat_id,
                message_id=event.message_id,
                sender_id=event.sender_id,
            )
            return
        if not should_process_lark_event(event):
            log_event(
                "lark.event_ignored",
                reason="group_mention_required",
                chat_id=event.chat_id,
                message_id=event.message_id,
                chat_type=event.chat_type,
            )
            return
        # 先进入合并缓冲区，再异步添加确认表情。飞书 reaction API 偶尔耗时数秒，
        # 如果同步等待它返回，后续消息无法及时进入缓冲区，会错过合并窗口。
        _pending_buffer.add(event, "", on_flush=self._flush_merged_events)
        self.reaction_executor.submit(self._add_reaction, event)

    def handle_card_action(self, data: Any) -> dict[str, Any]:
        event = _get_value(data, "event") or data
        action_value = _get_value(event, "action", "value") or {}
        approval_id = str(_get_value(action_value, "approval_id") or "").strip()
        decision = str(_get_value(action_value, "decision") or "").strip().lower()
        operator_id = (
            str(_get_value(event, "operator", "open_id") or "").strip()
            or str(_get_value(event, "operator", "user_id") or "").strip()
        )
        if not approval_id or decision not in {"approve", "reject"}:
            return {"toast": {"type": "warning", "content": "无法识别这个审批操作。"}}
        with self._approval_lock:
            pending = self._pending_approvals.get(approval_id)
        if pending is None:
            return {"toast": {"type": "warning", "content": "这个审批已经处理或已过期。"}}
        if not _is_lark_approval_operator_allowed(operator_id, pending):
            log_event(
                "lark.approval_operator_denied",
                approval_id=approval_id,
                operator_id=operator_id,
                requester_id=pending.requester_id,
            )
            return {"toast": {"type": "warning", "content": "只有请求人或审批白名单用户可以处理。"}}
        with self._approval_lock:
            self._pending_approvals.pop(approval_id, None)
        log_event(
            "lark.approval_decision",
            approval_id=approval_id,
            decision=decision,
            operator_id=operator_id,
            thread_id=pending.thread_id,
        )
        self.executor.submit(self._resume_approval, pending, decision, operator_id)
        return {"toast": {"type": "success", "content": "已提交审批决定。"}}

    def _add_reaction(self, event: LarkMessageEvent) -> None:
        try:
            reaction_id = add_lark_reaction(event.message_id, app_id=self.app_id, app_secret=self.app_secret)
            log_event("lark.reaction_added", message_id=event.message_id, reaction_id=reaction_id)
            context_key = _lark_context_key_for_event(event)
            if not reaction_id or _pending_buffer.set_reaction(context_key, event.message_id, reaction_id):
                return

            # reaction 返回时消息可能已经离开缓冲区，但 Agent 仍在处理中。
            # 这种情况下先保存 reaction，等最终回复流程结束后再清理。
            final_emoji = ""
            with self._reaction_lock:
                self._prune_completed_messages()
                completed = self._completed_messages.get(event.message_id)
                if completed is not None:
                    final_emoji = completed[1]
                else:
                    self._late_reactions[event.message_id] = reaction_id
            if final_emoji:
                self._replace_reaction(event.message_id, reaction_id, final_emoji)
        except Exception as exc:
            log_event("lark.reaction_add_error", message_id=event.message_id, error=repr(exc))

    def _flush_merged_events(self, events: list[LarkMessageEvent], reaction_ids: list[str]) -> None:
        """Buffer timer expired — merge all events and submit to worker pool."""
        self.executor.submit(self._process_merged_events, events, reaction_ids)

    def _process_merged_events(self, events: list[LarkMessageEvent], reaction_ids: list[str]) -> None:
        context_key = _lark_context_key_for_event(events[0])
        with _run_lock_for_context(context_key):
            self._process_merged_events_locked(events, reaction_ids)

    def _process_merged_events_locked(self, events: list[LarkMessageEvent], reaction_ids: list[str]) -> None:
        chat_id = events[0].chat_id
        sender_id = events[0].sender_id
        context_key = _lark_context_key_for_event(events[0])
        thread_scope = "sender" if context_key != chat_id else "chat"

        # Build merged text content from all events
        fragments: list[str] = []
        for idx, event in enumerate(events):
            fragment = _event_to_text_fragment(event, self.app_id, self.app_secret)
            fragments.append(f"[{idx + 1}] {fragment}")

        merged_text = "\n".join(fragments)

        # Check for slash commands in the text portion
        text_only = " ".join(e.text for e in events if e.text)
        thread_id = _thread_id_for_event(events[0])
        if text_only:
            command_result = handle_thread_slash_command(text_only, self.graph, thread_id, source="lark")
            if command_result:
                if command_result.clear_history:
                    _clear_thread_history(thread_id)
                send_lark_text(
                    chat_id,
                    command_result.response,
                    app_id=self.app_id,
                    app_secret=self.app_secret,
                )
                self._finish_reactions(events, reaction_ids, LARK_DONE_EMOJI)
                return

        user_content = (
            "[Feishu message]\n"
            f"chat_id: {chat_id}\n"
            f"sender_id: {sender_id}\n\n"
            f"{merged_text}"
        )
        log_event("lark.run_start", chat_id=chat_id, message_ids=[e.message_id for e in events], thread_id=thread_id)
        try:
            if _stream_channel_messages_enabled():
                previous_messages = _history_for_thread(thread_id)
                input_payload = {"messages": [*previous_messages, {"role": "user", "content": user_content}]}
                run_config = {
                    "configurable": {"thread_id": thread_id},
                    "metadata": {
                        "source": "lark",
                        "lark_chat_id": chat_id,
                        "lark_sender_id": sender_id,
                        "lark_context_key": context_key,
                        "lark_thread_scope": thread_scope,
                    },
                    "tags": ["lark"],
                    "callbacks": [],
                }
                result = _invoke_and_forward_messages(
                    self.graph,
                    input_payload=input_payload,
                    config=run_config,
                    previous_messages=previous_messages,
                    on_text=lambda text: send_lark_text(
                        chat_id,
                        text,
                        app_id=self.app_id,
                        app_secret=self.app_secret,
                    ),
                )
                if self._handle_interrupt_result(result, events, reaction_ids, thread_id, chat_id):
                    return
                messages = result.get("messages", []) if isinstance(result, dict) else []
                if messages:
                    _store_thread_history(thread_id, list(messages))
                answer = extract_final_ai_text(result)
                if answer:
                    self._send_voice_reply_if_enabled(chat_id, answer)
                log_event("lark.run_end", chat_id=chat_id, message_ids=[e.message_id for e in events], thread_id=thread_id)
                self._finish_reactions(events, reaction_ids, LARK_DONE_EMOJI)
                return

            previous_messages = _history_for_thread(thread_id)
            result = self.graph.invoke(
                {"messages": [*previous_messages, {"role": "user", "content": user_content}]},
                config={
                    "configurable": {"thread_id": thread_id},
                    "metadata": {
                        "source": "lark",
                        "lark_chat_id": chat_id,
                        "lark_sender_id": sender_id,
                        "lark_context_key": context_key,
                        "lark_thread_scope": thread_scope,
                    },
                    "tags": ["lark"],
                    "callbacks": [],
                },
            )
            if self._handle_interrupt_result(result, events, reaction_ids, thread_id, chat_id):
                return
            messages = result.get("messages", []) if isinstance(result, dict) else []
            if messages:
                _store_thread_history(thread_id, list(messages))
            answer = extract_final_ai_text(result) or "我处理完了，但没有生成可发送的文本回复。"
            send_lark_text(chat_id, answer, app_id=self.app_id, app_secret=self.app_secret)
            self._send_voice_reply_if_enabled(chat_id, answer)
            log_event("lark.run_end", chat_id=chat_id, message_ids=[e.message_id for e in events], thread_id=thread_id)
            self._finish_reactions(events, reaction_ids, LARK_DONE_EMOJI)
        except Exception as exc:
            log_event(
                "lark.run_error",
                chat_id=chat_id,
                message_ids=[e.message_id for e in events],
                thread_id=thread_id,
                error=repr(exc),
                traceback=traceback.format_exc(),
            )
            try:
                send_lark_text(
                    chat_id,
                    f"处理飞书消息时出错：{exc}",
                    app_id=self.app_id,
                    app_secret=self.app_secret,
                )
            except Exception as send_exc:
                log_event(
                    "lark.reply_error",
                    chat_id=chat_id,
                    error=repr(send_exc),
                    traceback=traceback.format_exc(),
                )
            self._finish_reactions(events, reaction_ids, LARK_ERROR_EMOJI)

    def _handle_interrupt_result(
        self,
        result: Any,
        events: list[LarkMessageEvent],
        reaction_ids: list[str],
        thread_id: str,
        chat_id: str,
    ) -> bool:
        interrupts = _interrupts_from_result(result)
        if not interrupts:
            return False
        approval_id = f"lark_appr_{uuid.uuid4().hex[:12]}"
        interrupt_value = _interrupt_value(interrupts[0])
        pending = LarkPendingApproval(
            approval_id=approval_id,
            thread_id=thread_id,
            chat_id=chat_id,
            requester_id=events[0].sender_id if events else "",
            interrupt_value=interrupt_value,
            message_ids=tuple(event.message_id for event in events),
            reaction_ids=tuple(reaction_ids),
        )
        with self._approval_lock:
            self._pending_approvals[approval_id] = pending
        send_lark_approval_card(
            chat_id,
            approval_id,
            interrupt_value,
            app_id=self.app_id,
            app_secret=self.app_secret,
        )
        log_event(
            "lark.approval_requested",
            approval_id=approval_id,
            chat_id=chat_id,
            thread_id=thread_id,
            message_ids=list(pending.message_ids),
        )
        return True

    def _resume_approval(self, pending: LarkPendingApproval, decision: str, operator_id: str) -> None:
        resume_decision: dict[str, Any]
        if decision == "approve":
            resume_decision = {"type": "approve"}
        else:
            resume_decision = {
                "type": "reject",
                "message": f"Rejected from Feishu by {operator_id or 'user'}.",
            }
        dummy_events = [
            LarkMessageEvent(
                message_id=message_id,
                chat_id=pending.chat_id,
                message_type="approval",
                text="",
            )
            for message_id in pending.message_ids
        ]
        try:
            result = self.graph.invoke(
                Command(resume={"decisions": [resume_decision]}),
                config={
                    "configurable": {"thread_id": pending.thread_id},
                    "metadata": {"source": "lark", "lark_chat_id": pending.chat_id},
                    "tags": ["lark", "approval"],
                    "callbacks": [],
                },
            )
            if self._handle_interrupt_result(
                result,
                dummy_events,
                list(pending.reaction_ids),
                pending.thread_id,
                pending.chat_id,
            ):
                return
            messages = result.get("messages", []) if isinstance(result, dict) else []
            if messages:
                _store_thread_history(pending.thread_id, list(messages))
            answer = extract_final_ai_text(result) or "审批已处理。"
            send_lark_text(pending.chat_id, answer, app_id=self.app_id, app_secret=self.app_secret)
            self._send_voice_reply_if_enabled(pending.chat_id, answer)
            self._finish_reactions(dummy_events, list(pending.reaction_ids), LARK_DONE_EMOJI)
            log_event("lark.approval_resumed", approval_id=pending.approval_id, decision=decision)
        except Exception as exc:
            log_event(
                "lark.approval_resume_error",
                approval_id=pending.approval_id,
                error=repr(exc),
                traceback=traceback.format_exc(),
            )
            try:
                send_lark_text(
                    pending.chat_id,
                    f"处理飞书审批时出错：{exc}",
                    app_id=self.app_id,
                    app_secret=self.app_secret,
                )
            except Exception:
                pass
            self._finish_reactions(dummy_events, list(pending.reaction_ids), LARK_ERROR_EMOJI)

    def _send_voice_reply_if_enabled(self, chat_id: str, text: str) -> None:
        try:
            send_lark_images_from_text(
                chat_id,
                text,
                app_id=self.app_id,
                app_secret=self.app_secret,
            )
            if send_lark_audio_markers(
                chat_id,
                text,
                app_id=self.app_id,
                app_secret=self.app_secret,
            ):
                return
            send_lark_voice_reply(
                chat_id,
                text,
                app_id=self.app_id,
                app_secret=self.app_secret,
            )
        except Exception as exc:
            log_event(
                "lark.voice_error",
                chat_id=chat_id,
                error=repr(exc),
                traceback=traceback.format_exc(),
            )

    def _prune_completed_messages(self) -> None:
        cutoff = time.monotonic() - 300
        expired = [
            message_id
            for message_id, (completed_at, _emoji) in self._completed_messages.items()
            if completed_at < cutoff
        ]
        for message_id in expired:
            self._completed_messages.pop(message_id, None)

    def _finish_reactions(self, events: list[LarkMessageEvent], reaction_ids: list[str], final_emoji: str) -> None:
        reactions_to_replace: list[tuple[str, str, str]] = []
        now = time.monotonic()
        with self._reaction_lock:
            self._prune_completed_messages()
            for index, event in enumerate(events):
                self._completed_messages[event.message_id] = (now, final_emoji)
                buffered_reaction = reaction_ids[index] if index < len(reaction_ids) else ""
                late_reaction = self._late_reactions.pop(event.message_id, "")
                reaction_id = late_reaction or buffered_reaction
                if reaction_id:
                    reactions_to_replace.append((event.message_id, reaction_id, final_emoji))

        for message_id, reaction_id, emoji_type in reactions_to_replace:
            self._replace_reaction(message_id, reaction_id, emoji_type)

    def _remove_reaction(self, message_id: str, reaction_id: str) -> None:
        if not reaction_id:
            return
        try:
            delete_lark_reaction(message_id, reaction_id, app_id=self.app_id, app_secret=self.app_secret)
            log_event("lark.reaction_removed", message_id=message_id, reaction_id=reaction_id)
        except Exception as exc:
            log_event("lark.reaction_remove_error", message_id=message_id, reaction_id=reaction_id, error=repr(exc))

    def _replace_reaction(self, message_id: str, reaction_id: str, emoji_type: str) -> None:
        self._remove_reaction(message_id, reaction_id)
        if not emoji_type:
            return
        try:
            final_reaction_id = add_lark_reaction(
                message_id,
                emoji_type=emoji_type,
                app_id=self.app_id,
                app_secret=self.app_secret,
            )
            log_event(
                "lark.reaction_final_added",
                message_id=message_id,
                emoji_type=emoji_type,
                reaction_id=final_reaction_id,
            )
        except Exception as exc:
            log_event(
                "lark.reaction_final_add_error",
                message_id=message_id,
                emoji_type=emoji_type,
                error=repr(exc),
            )

    def run_forever(self) -> None:
        try:
            import lark_oapi as lark
        except ImportError as exc:
            raise RuntimeError("Feishu/Lark WS mode requires: pip install lark-oapi") from exc

        log_event("lark.ws_handler_register")
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(lambda data: self.handle_event(data))
            .register_p2_card_action_trigger(lambda data: self.handle_card_action(data))
            .build()
        )
        log_level = lark.LogLevel.DEBUG if _bool_env("LARK_DEBUG", False) else lark.LogLevel.INFO
        client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=event_handler,
            log_level=log_level,
        )
        client.on_reconnecting = lambda: log_event("lark.ws_reconnecting")
        client.on_reconnected = lambda: log_event("lark.ws_reconnected")
        log_event("lark.ws_start")
        print("[xu-agent lark] WS bridge starting", file=sys.stderr, flush=True)
        client.start()


def start_lark_ws_bridge(graph: Any) -> None:
    """Start the optional Feishu/Lark long-connection bridge in a daemon thread."""
    global _started

    if not _bool_env("LARK_WS_ENABLED", False):
        return
    app_id = os.getenv("LARK_APP_ID", "").strip()
    app_secret = os.getenv("LARK_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        log_event("lark.ws_disabled", reason="missing_app_id_or_secret")
        return

    with _start_lock:
        if _started:
            return
        _started = True

    bridge = LarkWsBridge(graph=graph, app_id=app_id, app_secret=app_secret)

    def run_bridge() -> None:
        try:
            _run_with_blockbuster_skip(bridge.run_forever)
        except Exception as exc:
            log_event("lark.ws_error", error=repr(exc))
            print(f"[xu-agent lark] WS bridge stopped: {exc}", file=sys.stderr, flush=True)

    thread = threading.Thread(
        target=run_bridge,
        name="lark-ws-bridge",
        daemon=True,
    )
    thread.start()


def _run_with_blockbuster_skip(func: Any) -> None:
    """Run a dedicated integration thread outside LangGraph's blocking detector.

    LangGraph dev enables blockbuster globally. The Feishu SDK runs its own
    websocket event loop in this daemon thread and performs network connects
    inside that loop, which blockbuster otherwise treats as blocking ASGI work.
    Setting the skip ContextVar here is intentionally scoped to the Lark bridge
    thread, so normal agent/model/tool execution remains protected.
    """
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

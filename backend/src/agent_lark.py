"""Feishu/Lark long-connection bridge for the local agent."""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from agent_commands import handle_thread_slash_command
from agent_logging import log_event

LARK_API_BASE_URL = "https://open.feishu.cn/open-apis"
DEFAULT_HISTORY_MAX_MESSAGES = 20
DEFAULT_REPLY_MAX_CHARS = 12000
DEFAULT_TOOL_RESULT_PREVIEW_CHARS = 400
LARK_ACK_EMOJI = os.getenv("LARK_ACK_EMOJI_TYPE", "OK")

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


@dataclass(frozen=True)
class LarkMessageEvent:
    message_id: str
    chat_id: str
    message_type: str
    text: str
    sender_id: str = ""


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


def parse_lark_message_event(data: Any) -> LarkMessageEvent | None:
    """Extract the fields we need from the SDK event object."""
    event = _get_value(data, "event") or data
    message = _get_value(event, "message")
    if message is None:
        return None

    message_id = str(_get_value(message, "message_id") or "").strip()
    chat_id = str(_get_value(message, "chat_id") or "").strip()
    message_type = str(_get_value(message, "message_type") or "").strip()
    content = _get_value(message, "content")
    text = _extract_text_content(message_type, content)
    sender_id = (
        str(_get_value(event, "sender", "sender_id", "open_id") or "").strip()
        or str(_get_value(event, "sender", "sender_id", "user_id") or "").strip()
    )

    if not message_id or not chat_id:
        return None
    return LarkMessageEvent(
        message_id=message_id,
        chat_id=chat_id,
        message_type=message_type,
        text=text,
        sender_id=sender_id,
    )


def _safe_event_repr(data: Any, limit: int = 1000) -> str:
    try:
        raw = repr(data)
    except Exception as exc:
        raw = f"<repr failed: {exc!r}>"
    return raw if len(raw) <= limit else f"{raw[:limit]}...[truncated {len(raw) - limit} chars]"


def _thread_id_for_chat(chat_id: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", chat_id).strip("_")
    return f"lark_{safe_id or 'unknown'}"


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


def _run_lock_for_chat(chat_id: str) -> threading.Lock:
    with _chat_run_locks_guard:
        return _chat_run_locks.setdefault(chat_id, threading.Lock())


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


def _reply_chunks(text: str) -> list[str]:
    limit = max(1000, _int_env("LARK_REPLY_MAX_CHARS", DEFAULT_REPLY_MAX_CHARS))
    if len(text) <= limit:
        return [text]
    return [text[index : index + limit] for index in range(0, len(text), limit)]


def send_lark_text(chat_id: str, text: str, *, app_id: str, app_secret: str) -> None:
    token = _get_tenant_access_token(app_id, app_secret)
    for chunk in _reply_chunks(text):
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": chunk}, ensure_ascii=False),
        }
        response = _post_lark_json(
            "/im/v1/messages",
            token,
            payload,
            query={"receive_id_type": "chat_id"},
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
            text_preview=event.text[:200],
        )
        if not _remember_seen_message(event.message_id):
            log_event("lark.event_ignored", reason="duplicate", message_id=event.message_id)
            return
        # Add ack reaction immediately (before worker pool processing)
        reaction_id = ""
        try:
            reaction_id = add_lark_reaction(event.message_id, app_id=self.app_id, app_secret=self.app_secret)
            log_event("lark.reaction_added", message_id=event.message_id, reaction_id=reaction_id)
        except Exception as exc:
            log_event("lark.reaction_add_error", message_id=event.message_id, error=repr(exc))
        self.executor.submit(self._process_event, event, reaction_id)

    def _process_event(self, event: LarkMessageEvent, reaction_id: str = "") -> None:
        with _run_lock_for_chat(event.chat_id):
            self._process_event_locked(event, reaction_id)

    def _remove_reaction(self, event: LarkMessageEvent, reaction_id: str) -> None:
        if not reaction_id:
            return
        try:
            delete_lark_reaction(event.message_id, reaction_id, app_id=self.app_id, app_secret=self.app_secret)
            log_event("lark.reaction_removed", message_id=event.message_id, reaction_id=reaction_id)
        except Exception as exc:
            log_event("lark.reaction_remove_error", message_id=event.message_id, reaction_id=reaction_id, error=repr(exc))

    def _process_event_locked(self, event: LarkMessageEvent, reaction_id: str = "") -> None:
        if not event.text:
            log_event(
                "lark.message_unsupported",
                chat_id=event.chat_id,
                message_id=event.message_id,
                message_type=event.message_type,
            )
            send_lark_text(
                event.chat_id,
                "我目前只支持处理飞书文本消息。",
                app_id=self.app_id,
                app_secret=self.app_secret,
            )
            self._remove_reaction(event, reaction_id)
            return

        thread_id = _thread_id_for_chat(event.chat_id)
        command_result = handle_thread_slash_command(event.text, self.graph, thread_id, source="lark")
        if command_result:
            if command_result.clear_history:
                _clear_thread_history(thread_id)
            send_lark_text(
                event.chat_id,
                command_result.response,
                app_id=self.app_id,
                app_secret=self.app_secret,
            )
            self._remove_reaction(event, reaction_id)
            return

        user_content = (
            "[Feishu message]\n"
            f"chat_id: {event.chat_id}\n"
            f"sender_id: {event.sender_id}\n"
            f"message_id: {event.message_id}\n\n"
            f"{event.text}"
        )
        log_event("lark.run_start", chat_id=event.chat_id, message_id=event.message_id, thread_id=thread_id)
        try:
            if _stream_channel_messages_enabled():
                previous_messages = _history_for_thread(thread_id)
                input_payload = {"messages": [*previous_messages, {"role": "user", "content": user_content}]}
                run_config = {
                    "configurable": {"thread_id": thread_id},
                    "metadata": {"source": "lark", "lark_chat_id": event.chat_id},
                    "tags": ["lark"],
                    "callbacks": [],
                }
                result = _invoke_and_forward_messages(
                    self.graph,
                    input_payload=input_payload,
                    config=run_config,
                    previous_messages=previous_messages,
                    on_text=lambda text: send_lark_text(
                        event.chat_id,
                        text,
                        app_id=self.app_id,
                        app_secret=self.app_secret,
                    ),
                )
                messages = result.get("messages", []) if isinstance(result, dict) else []
                if messages:
                    _store_thread_history(thread_id, list(messages))
                log_event("lark.run_end", chat_id=event.chat_id, message_id=event.message_id, thread_id=thread_id)
                self._remove_reaction(event, reaction_id)
                return

            previous_messages = _history_for_thread(thread_id)
            result = self.graph.invoke(
                {"messages": [*previous_messages, {"role": "user", "content": user_content}]},
                config={
                    "configurable": {"thread_id": thread_id},
                    "metadata": {"source": "lark", "lark_chat_id": event.chat_id},
                    "tags": ["lark"],
                    "callbacks": [],
                },
            )
            messages = result.get("messages", []) if isinstance(result, dict) else []
            if messages:
                _store_thread_history(thread_id, list(messages))
            answer = extract_final_ai_text(result) or "我处理完了，但没有生成可发送的文本回复。"
            send_lark_text(event.chat_id, answer, app_id=self.app_id, app_secret=self.app_secret)
            log_event("lark.run_end", chat_id=event.chat_id, message_id=event.message_id, thread_id=thread_id)
            self._remove_reaction(event, reaction_id)
        except Exception as exc:
            log_event(
                "lark.run_error",
                chat_id=event.chat_id,
                message_id=event.message_id,
                thread_id=thread_id,
                error=repr(exc),
                traceback=traceback.format_exc(),
            )
            try:
                send_lark_text(
                    event.chat_id,
                    f"处理飞书消息时出错：{exc}",
                    app_id=self.app_id,
                    app_secret=self.app_secret,
                )
            except Exception as send_exc:
                log_event(
                    "lark.reply_error",
                    chat_id=event.chat_id,
                    error=repr(send_exc),
                    traceback=traceback.format_exc(),
                )
            self._remove_reaction(event, reaction_id)

    def run_forever(self) -> None:
        try:
            import lark_oapi as lark
        except ImportError as exc:
            raise RuntimeError("Feishu/Lark WS mode requires: pip install lark-oapi") from exc

        log_event("lark.ws_handler_register")
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(lambda data: self.handle_event(data))
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

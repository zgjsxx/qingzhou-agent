"""Weixin iLink bridge for personal WeChat bot direct messages.

The wire protocol is compatible with Tencent's iLink Bot API behavior used by
Hermes Agent. This first version intentionally supports text DMs only; media
requires the separate encrypted CDN protocol.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import struct
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from agent_commands import handle_thread_slash_command
from agent_config import config_section
from agent_logging import log_event

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8)
CHANNEL_VERSION = "2.2.0"
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
LONG_POLL_TIMEOUT_MS = 35_000
DEFAULT_HISTORY_MAX_MESSAGES = 20
DEFAULT_REPLY_MAX_CHARS = 2000
SESSION_EXPIRED_ERRCODE = -14

BACKEND_DIR = Path(__file__).resolve().parents[1]
WEIXIN_DATA_DIR = BACKEND_DIR / ".weixin"
ACCOUNT_FILE = WEIXIN_DATA_DIR / "account.json"
SYNC_FILE = WEIXIN_DATA_DIR / "sync.json"
CONTEXT_TOKENS_FILE = WEIXIN_DATA_DIR / "context-tokens.json"

_start_lock = threading.Lock()
_started = False
_chat_histories: dict[str, list[Any]] = {}
_chat_history_lock = threading.Lock()


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _bool_env_or_config(env_name: str, key: str, default: bool = False) -> bool:
    env_value = os.getenv(env_name)
    if env_value is not None and env_value.strip():
        return _bool_value(env_value, default)
    return _bool_value(config_section("weixin").get(key), default)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def _json_read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_account(credentials: dict[str, str]) -> None:
    """Persist QR-login credentials outside source control."""
    _json_write(
        ACCOUNT_FILE,
        {
            "account_id": credentials["account_id"],
            "token": credentials["token"],
            "base_url": credentials.get("base_url") or ILINK_BASE_URL,
            "user_id": credentials.get("user_id", ""),
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


def load_account() -> dict[str, str]:
    saved = _json_read(ACCOUNT_FILE)
    config = config_section("weixin")
    return {
        "account_id": str(
            os.getenv("WEIXIN_ACCOUNT_ID")
            or config.get("accountId")
            or saved.get("account_id")
            or ""
        ).strip(),
        "token": str(
            os.getenv("WEIXIN_TOKEN")
            or saved.get("token")
            or ""
        ).strip(),
        "base_url": str(
            os.getenv("WEIXIN_BASE_URL")
            or config.get("baseUrl")
            or saved.get("base_url")
            or ILINK_BASE_URL
        ).strip(),
        "user_id": str(saved.get("user_id") or "").strip(),
    }


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _headers(token: str | None, body: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _print_qr_ascii(content: str) -> None:
    """Render a QR code using only ASCII characters for Windows consoles."""
    import qrcode

    qr = qrcode.QRCode(border=2)
    qr.add_data(content)
    qr.make(fit=True)
    for row in qr.get_matrix():
        print("".join("##" if cell else "  " for cell in row))


def _request_json(
    *,
    base_url: str,
    endpoint: str,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout_seconds: float = 15,
) -> dict[str, Any]:
    if payload is None:
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/{endpoint}",
            headers={
                "iLink-App-Id": ILINK_APP_ID,
                "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
            },
        )
    else:
        body = json.dumps(
            {**payload, "base_info": {"channel_version": CHANNEL_VERSION}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/{endpoint}",
            data=body.encode("utf-8"),
            headers=_headers(token, body),
            method="POST",
        )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"iLink HTTP {exc.code}: {detail[:300]}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("iLink returned a non-object JSON response")
    return result


async def _api_get(base_url: str, endpoint: str, timeout_seconds: float = 35) -> dict[str, Any]:
    return await asyncio.to_thread(
        _request_json,
        base_url=base_url,
        endpoint=endpoint,
        timeout_seconds=timeout_seconds,
    )


async def _api_post(
    base_url: str,
    endpoint: str,
    payload: dict[str, Any],
    token: str,
    timeout_seconds: float = 40,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _request_json,
        base_url=base_url,
        endpoint=endpoint,
        payload=payload,
        token=token,
        timeout_seconds=timeout_seconds,
    )


async def qr_login(timeout_seconds: int = 480) -> dict[str, str] | None:
    """Run iLink QR login and save the returned bot credentials."""
    qr_response = await _api_get(
        ILINK_BASE_URL,
        f"{EP_GET_BOT_QR}?bot_type=3",
    )
    qrcode_value = str(qr_response.get("qrcode") or "").strip()
    qrcode_content = str(qr_response.get("qrcode_img_content") or "").strip()
    if not qrcode_value:
        raise RuntimeError("iLink QR response did not contain qrcode")

    scan_content = qrcode_content or qrcode_value
    print("\n请使用微信扫描以下二维码：")
    if qrcode_content:
        print(qrcode_content)
    try:
        _print_qr_ascii(scan_content)
    except ImportError:
        print("未安装 qrcode，无法在终端绘制二维码，请打开上面的链接。")

    deadline = time.monotonic() + timeout_seconds
    current_base_url = ILINK_BASE_URL
    while time.monotonic() < deadline:
        status_response = await _api_get(
            current_base_url,
            f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
        )
        status = str(status_response.get("status") or "wait")
        if status == "scaned":
            print("已扫码，请在微信中确认登录。")
        elif status == "scaned_but_redirect":
            redirect_host = str(status_response.get("redirect_host") or "").strip()
            if redirect_host:
                current_base_url = f"https://{redirect_host}"
        elif status == "expired":
            raise RuntimeError("微信登录二维码已过期，请重新运行登录脚本")
        elif status == "confirmed":
            credentials = {
                "account_id": str(status_response.get("ilink_bot_id") or "").strip(),
                "token": str(status_response.get("bot_token") or "").strip(),
                "base_url": str(status_response.get("baseurl") or current_base_url).strip(),
                "user_id": str(status_response.get("ilink_user_id") or "").strip(),
            }
            if not credentials["account_id"] or not credentials["token"]:
                raise RuntimeError("iLink confirmed login without complete credentials")
            save_account(credentials)
            return credentials
        await asyncio.sleep(1)
    return None


def _extract_text(message: dict[str, Any]) -> str:
    for item in message.get("item_list") or []:
        if item.get("type") == 1:
            return str((item.get("text_item") or {}).get("text") or "").strip()
        voice_text = str((item.get("voice_item") or {}).get("text") or "").strip()
        if voice_text:
            return voice_text
    return ""


def _thread_id_for_user(user_id: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_.:@-]+", "_", user_id).strip("_")
    return f"weixin_{safe_id or 'unknown'}"


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
    seen = {_message_key(message, index) for index, message in enumerate(previous_messages)}
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


class WeixinBridge:
    def __init__(self, graph: Any, credentials: dict[str, str]):
        self.graph = graph
        self.account_id = credentials["account_id"]
        self.token = credentials["token"]
        self.base_url = credentials["base_url"].rstrip("/")
        self.context_tokens = {
            str(key): str(value)
            for key, value in _json_read(CONTEXT_TOKENS_FILE).items()
            if value
        }
        self.seen_messages: dict[str, float] = {}
        self.chat_locks: dict[str, asyncio.Lock] = {}

    def _remember_message(self, message_id: str) -> bool:
        now = time.monotonic()
        self.seen_messages = {
            key: timestamp
            for key, timestamp in self.seen_messages.items()
            if now - timestamp < 300
        }
        if message_id and message_id in self.seen_messages:
            return False
        if message_id:
            self.seen_messages[message_id] = now
        return True

    async def send_text(self, user_id: str, text: str) -> None:
        limit = max(200, _int_env("WEIXIN_REPLY_MAX_CHARS", DEFAULT_REPLY_MAX_CHARS))
        chunks = [text[index : index + limit] for index in range(0, len(text), limit)]
        for chunk in chunks:
            response = await _api_post(
                self.base_url,
                EP_SEND_MESSAGE,
                {
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": user_id,
                        "client_id": f"xu-agent-weixin-{uuid.uuid4().hex}",
                        "message_type": 2,
                        "message_state": 2,
                        "item_list": [{"type": 1, "text_item": {"text": chunk}}],
                        **(
                            {"context_token": self.context_tokens[user_id]}
                            if self.context_tokens.get(user_id)
                            else {}
                        ),
                    }
                },
                self.token,
            )
            ret = response.get("ret", 0)
            errcode = response.get("errcode", 0)
            if ret not in {0, None} or errcode not in {0, None}:
                raise RuntimeError(
                    f"iLink send failed: ret={ret}, errcode={errcode}, "
                    f"errmsg={response.get('errmsg', '')}"
                )
            if len(chunks) > 1:
                await asyncio.sleep(0.3)

    async def _process_message(self, message: dict[str, Any]) -> None:
        sender_id = str(message.get("from_user_id") or "").strip()
        message_id = str(message.get("message_id") or "").strip()
        if not sender_id or sender_id == self.account_id or not self._remember_message(message_id):
            return
        text = _extract_text(message)
        if not text:
            log_event(
                "weixin.message_unsupported",
                sender_id=sender_id,
                message_id=message_id,
            )
            await self.send_text(sender_id, "目前仅支持处理微信文本消息。")
            return

        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self.context_tokens[sender_id] = context_token
            _json_write(CONTEXT_TOKENS_FILE, self.context_tokens)

        lock = self.chat_locks.setdefault(sender_id, asyncio.Lock())
        async with lock:
            await self._process_text(sender_id, message_id, text)

    async def _process_text(self, sender_id: str, message_id: str, text: str) -> None:
        thread_id = _thread_id_for_user(sender_id)
        command_result = handle_thread_slash_command(
            text,
            self.graph,
            thread_id,
            source="weixin",
        )
        if command_result:
            if command_result.clear_history:
                with _chat_history_lock:
                    _chat_histories.pop(thread_id, None)
            await self.send_text(sender_id, command_result.response)
            return

        with _chat_history_lock:
            previous_messages = list(_chat_histories.get(thread_id, []))
        user_content = (
            "[Weixin iLink message]\n"
            f"sender_id: {sender_id}\n"
            f"message_id: {message_id}\n\n{text}"
        )
        config = {
            "configurable": {"thread_id": thread_id},
            "metadata": {"source": "weixin", "weixin_user_id": sender_id},
            "tags": ["weixin"],
            "callbacks": [],
        }
        log_event(
            "weixin.run_start",
            sender_id=sender_id,
            message_id=message_id,
            thread_id=thread_id,
        )
        try:
            result, outbound_texts = await asyncio.to_thread(
                _collect_ai_texts,
                self.graph,
                {"messages": [*previous_messages, {"role": "user", "content": user_content}]},
                config,
                previous_messages,
            )
            messages = result.get("messages", []) if isinstance(result, dict) else []
            if messages:
                max_messages = max(
                    2,
                    _int_env("WEIXIN_HISTORY_MAX_MESSAGES", DEFAULT_HISTORY_MAX_MESSAGES),
                )
                with _chat_history_lock:
                    _chat_histories[thread_id] = list(messages[-max_messages:])
            if not outbound_texts:
                outbound_texts = ["我处理完了，但没有生成可发送的文本回复。"]
            if not _bool_env_or_config(
                "WEIXIN_STREAM_ALL_MESSAGES",
                "streamAllMessages",
                True,
            ):
                outbound_texts = outbound_texts[-1:]
            for outbound_text in outbound_texts:
                await self.send_text(sender_id, outbound_text)
            log_event(
                "weixin.run_end",
                sender_id=sender_id,
                message_id=message_id,
                thread_id=thread_id,
            )
        except Exception as exc:
            log_event(
                "weixin.run_error",
                sender_id=sender_id,
                message_id=message_id,
                thread_id=thread_id,
                error=repr(exc),
                traceback=traceback.format_exc(),
            )
            await self.send_text(sender_id, f"处理微信消息时出错：{exc}")

    async def run_forever(self) -> None:
        sync_buf = str(_json_read(SYNC_FILE).get("get_updates_buf") or "")
        timeout_ms = LONG_POLL_TIMEOUT_MS
        failures = 0
        log_event("weixin.poll_start", account_id=self.account_id)
        print("[xu-agent weixin] iLink bridge starting", file=sys.stderr, flush=True)
        while True:
            try:
                response = await _api_post(
                    self.base_url,
                    EP_GET_UPDATES,
                    {"get_updates_buf": sync_buf},
                    self.token,
                    timeout_seconds=(timeout_ms / 1000) + 5,
                )
                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)
                if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE:
                    log_event("weixin.session_expired", account_id=self.account_id)
                    await asyncio.sleep(60)
                    continue
                if ret not in {0, None} or errcode not in {0, None}:
                    raise RuntimeError(
                        f"getupdates failed: ret={ret}, errcode={errcode}, "
                        f"errmsg={response.get('errmsg', '')}"
                    )
                failures = 0
                suggested_timeout = response.get("longpolling_timeout_ms")
                if isinstance(suggested_timeout, int) and suggested_timeout > 0:
                    timeout_ms = suggested_timeout
                next_sync_buf = str(response.get("get_updates_buf") or "")
                for message in response.get("msgs") or []:
                    if isinstance(message, dict):
                        asyncio.create_task(self._process_message(message))
                # 先把当前批次消息提交给处理任务，再持久化服务端游标。
                # 如果游标先落盘，而本地文件操作或消息分发随后失败，重启后会从新游标
                # 继续拉取，导致这一批已经被服务端确认、却未进入 Agent 的消息永久丢失。
                if next_sync_buf:
                    sync_buf = next_sync_buf
                    _json_write(SYNC_FILE, {"get_updates_buf": sync_buf})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                log_event(
                    "weixin.poll_error",
                    failures=failures,
                    error=repr(exc),
                    traceback=traceback.format_exc(),
                )
                await asyncio.sleep(30 if failures >= 3 else 2)
                if failures >= 3:
                    failures = 0


def _run_with_blockbuster_skip(func: Any) -> None:
    """Run the dedicated iLink loop outside LangGraph's ASGI blocker guard.

    微信桥接拥有独立的 daemon 线程和事件循环，不会阻塞 LangGraph 的 ASGI
    事件循环。这里仅对该线程设置 skip，避免保存同步游标和 context token 时，
    pathlib 的本地文件操作被 blockbuster 误判成 ASGI 主线程阻塞。
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


def _run_bridge(graph: Any, credentials: dict[str, str]) -> None:
    _run_with_blockbuster_skip(
        lambda: asyncio.run(WeixinBridge(graph, credentials).run_forever())
    )


def start_weixin_bridge(graph: Any) -> None:
    """Start the optional Weixin bridge in a daemon thread."""
    global _started
    if not _bool_env_or_config("WEIXIN_ENABLED", "enabled", False):
        return
    credentials = load_account()
    if not credentials["account_id"] or not credentials["token"]:
        log_event("weixin.disabled", reason="missing_credentials")
        print(
            "[xu-agent weixin] credentials missing; run scripts/weixin_login.py",
            file=sys.stderr,
            flush=True,
        )
        return
    with _start_lock:
        if _started:
            return
        _started = True
    thread = threading.Thread(
        target=_run_bridge,
        args=(graph, credentials),
        name="weixin-ilink-bridge",
        daemon=True,
    )
    thread.start()

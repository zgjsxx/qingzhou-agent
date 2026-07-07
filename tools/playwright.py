"""Optional Playwright browser tools.

The tools in this module are imported only when ``AGENT_PLAYWRIGHT_ENABLED`` is
enabled. Playwright itself is started lazily on the first browser tool call.
"""

from __future__ import annotations

import atexit
import asyncio
import os
import queue
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from langchain.tools import tool


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if not BACKEND_DIR.is_absolute():
    raise RuntimeError("tools/playwright.py must be loaded from an absolute module path.")
DEFAULT_THREAD_ID = "__default__"
DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_SNAPSHOT_CHARS = 12_000

_thread_id_getter: Callable[[], str] = lambda: DEFAULT_THREAD_ID


def set_thread_id_getter(getter: Callable[[], str]) -> None:
    """Provide the current LangGraph thread id without creating an import cycle."""
    global _thread_id_getter
    _thread_id_getter = getter


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _timeout_ms() -> int:
    try:
        value = int(os.getenv("AGENT_PLAYWRIGHT_TIMEOUT_MS", DEFAULT_TIMEOUT_MS))
    except ValueError:
        value = DEFAULT_TIMEOUT_MS
    return min(max(value, 1_000), 120_000)


def _browser_channel_available(channel: str) -> bool:
    executable = "msedge" if channel == "msedge" else "chrome"
    if shutil.which(executable):
        return True

    local_app_data = Path(os.getenv("LOCALAPPDATA", ""))
    program_files = Path(os.getenv("PROGRAMFILES", ""))
    program_files_x86 = Path(os.getenv("PROGRAMFILES(X86)", ""))
    candidates = (
        [
            program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
            program_files / "Microsoft/Edge/Application/msedge.exe",
            local_app_data / "Microsoft/Edge/Application/msedge.exe",
        ]
        if channel == "msedge"
        else [
            program_files / "Google/Chrome/Application/chrome.exe",
            program_files_x86 / "Google/Chrome/Application/chrome.exe",
            local_app_data / "Google/Chrome/Application/chrome.exe",
        ]
    )
    return any(str(candidate) and candidate.is_file() for candidate in candidates)


def _detect_installed_chromium_channel() -> str:
    if os.name != "nt" or not _bool_env(
        "AGENT_PLAYWRIGHT_AUTO_DETECT_CHANNEL", True
    ):
        return ""
    for channel in ("msedge", "chrome"):
        if _browser_channel_available(channel):
            return channel
    return ""


def _validate_url(url: str) -> str:
    normalized = str(url or "").strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be an absolute http:// or https:// URL.")
    return normalized


def _output_path(path: str) -> Path:
    root = BACKEND_DIR
    requested = Path(str(path or "output/playwright.png").strip()).expanduser()
    resolved = requested if requested.is_absolute() else root / requested
    resolved = Path(os.path.normpath(str(resolved)))
    if not resolved.is_relative_to(root):
        raise ValueError("screenshot path must stay inside the backend working directory.")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


class _BrowserWorker:
    """Own all sync Playwright objects on one dedicated thread."""

    def __init__(self) -> None:
        self._requests: queue.Queue[
            tuple[str, str, dict[str, Any], queue.Queue[tuple[bool, Any]]] | None
        ] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name="xu-agent-playwright",
            daemon=True,
        )
        self._thread.start()

    def call(self, thread_id: str, action: str, **kwargs: Any) -> Any:
        if not self._thread.is_alive():
            raise RuntimeError(
                "Playwright worker is not running. Check the backend log and restart "
                "the backend after fixing the Playwright installation."
            )
        response: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        self._requests.put((thread_id or DEFAULT_THREAD_ID, action, kwargs, response))
        ok, result = response.get()
        if ok:
            return result
        raise RuntimeError(str(result))

    def close(self) -> None:
        if self._thread.is_alive():
            self._requests.put(None)
            self._thread.join(timeout=5)

    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._fail_pending(
                "Playwright is not installed. Run: pip install playwright && "
                "playwright install chromium"
            )
            return

        sessions: dict[str, tuple[Any, Any]] = {}
        try:
            with sync_playwright() as playwright:
                browser_name = os.getenv("AGENT_PLAYWRIGHT_BROWSER", "chromium").strip().lower()
                browser_type = getattr(playwright, browser_name, None)
                if browser_type is None or browser_name not in {"chromium", "firefox", "webkit"}:
                    raise RuntimeError(
                        "AGENT_PLAYWRIGHT_BROWSER must be chromium, firefox, or webkit."
                    )
                launch_options: dict[str, Any] = {
                    "headless": _bool_env("AGENT_PLAYWRIGHT_HEADLESS", True)
                }
                browser_channel = os.getenv("AGENT_PLAYWRIGHT_CHANNEL", "").strip()
                if not browser_channel and browser_name == "chromium":
                    browser_channel = _detect_installed_chromium_channel()
                if browser_channel:
                    if browser_name != "chromium":
                        raise RuntimeError(
                            "AGENT_PLAYWRIGHT_CHANNEL is supported only with chromium."
                        )
                    launch_options["channel"] = browser_channel
                browser = browser_type.launch(**launch_options)
                while True:
                    request = self._requests.get()
                    if request is None:
                        break
                    thread_id, action, kwargs, response = request
                    try:
                        result = self._execute(browser, sessions, thread_id, action, kwargs)
                        response.put((True, result))
                    except Exception as exc:
                        response.put((False, f"{type(exc).__name__}: {exc}"))
                for context, _page in sessions.values():
                    context.close()
                browser.close()
        except Exception as exc:
            self._fail_pending(f"{type(exc).__name__}: {exc}")

    def _fail_pending(self, message: str) -> None:
        while True:
            request = self._requests.get()
            if request is None:
                return
            _thread_id, _action, _kwargs, response = request
            response.put((False, message))

    @staticmethod
    def _session(browser: Any, sessions: dict[str, tuple[Any, Any]], thread_id: str):
        session = sessions.get(thread_id)
        if session is None:
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(_timeout_ms())
            session = (context, page)
            sessions[thread_id] = session
        return session

    def _execute(
        self,
        browser: Any,
        sessions: dict[str, tuple[Any, Any]],
        thread_id: str,
        action: str,
        kwargs: dict[str, Any],
    ) -> Any:
        if action == "close":
            session = sessions.pop(thread_id, None)
            if session is None:
                return "No browser session was open for this thread."
            session[0].close()
            return "Closed the browser session for this thread."

        _context, page = self._session(browser, sessions, thread_id)
        if action == "open":
            response = page.goto(
                _validate_url(kwargs["url"]),
                wait_until="domcontentloaded",
                timeout=_timeout_ms(),
            )
            status = response.status if response is not None else "unknown"
            return f"Opened {page.url}\nstatus: {status}\ntitle: {page.title()}"
        if action == "snapshot":
            max_chars = min(max(int(kwargs["max_chars"]), 1_000), 50_000)
            body_text = page.locator("body").inner_text(timeout=_timeout_ms())
            if len(body_text) > max_chars:
                omitted = len(body_text) - max_chars
                body_text = f"{body_text[:max_chars]}\n\n[truncated, {omitted} characters omitted]"
            return f"url: {page.url}\ntitle: {page.title()}\n\nvisible text:\n{body_text}"
        if action == "click":
            page.locator(kwargs["selector"]).click(timeout=_timeout_ms())
            return f"Clicked {kwargs['selector']}\nurl: {page.url}"
        if action == "type":
            locator = page.locator(kwargs["selector"])
            locator.fill(kwargs["text"], timeout=_timeout_ms())
            if kwargs["submit"]:
                locator.press("Enter", timeout=_timeout_ms())
            return f"Entered text into {kwargs['selector']}\nsubmitted: {kwargs['submit']}"
        if action == "press":
            selector = kwargs["selector"].strip()
            if selector:
                page.locator(selector).press(kwargs["key"], timeout=_timeout_ms())
            else:
                page.keyboard.press(kwargs["key"])
            return f"Pressed {kwargs['key']}" + (f" on {selector}" if selector else "")
        if action == "scroll":
            page.mouse.wheel(int(kwargs["delta_x"]), int(kwargs["delta_y"]))
            return f"Scrolled by x={kwargs['delta_x']}, y={kwargs['delta_y']}"
        if action == "screenshot":
            # Do not pass ``path`` here. Playwright's path handling performs
            # synchronous mkdir/write operations on its internal asyncio-loop
            # thread, which LangGraph BlockBuster correctly rejects.
            return page.screenshot(full_page=bool(kwargs["full_page"]))
        raise ValueError(f"Unknown browser action: {action}")


_WORKER: _BrowserWorker | None = None
_WORKER_LOCK = threading.Lock()


def _worker() -> _BrowserWorker:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None:
            _WORKER = _BrowserWorker()
    return _WORKER


def _dispatch_sync(action: str, **kwargs: Any) -> str:
    try:
        if action == "screenshot":
            output = _output_path(str(kwargs["path"]))
            image_bytes = _worker().call(
                _thread_id_getter(),
                action,
                full_page=bool(kwargs["full_page"]),
            )
            output.write_bytes(image_bytes)
            return f"Saved browser screenshot to: {output}"
        return str(_worker().call(_thread_id_getter(), action, **kwargs))
    except (RuntimeError, ValueError, OSError) as exc:
        return f"Error: {exc}"


async def _dispatch(action: str, **kwargs: Any) -> str:
    """Run every browser operation outside LangGraph's ASGI event-loop thread."""
    return await asyncio.to_thread(_dispatch_sync, action, **kwargs)


def _shutdown() -> None:
    global _WORKER
    with _WORKER_LOCK:
        worker, _WORKER = _WORKER, None
    if worker is not None:
        worker.close()


atexit.register(_shutdown)


@tool
async def playwright_open(url: str) -> str:
    """Open an absolute HTTP(S) URL in this thread's isolated browser session."""
    try:
        normalized = _validate_url(url)
    except ValueError as exc:
        return f"Error: {exc}"
    return await _dispatch("open", url=normalized)


@tool
async def playwright_snapshot(max_chars: int = DEFAULT_SNAPSHOT_CHARS) -> str:
    """Read the current page URL, title, and visible text for page understanding."""
    return await _dispatch("snapshot", max_chars=max_chars)


@tool
async def playwright_click(selector: str) -> str:
    """Click an element using a Playwright selector such as text=Sign in or #submit."""
    if not str(selector or "").strip():
        return "Error: selector must not be empty."
    return await _dispatch("click", selector=selector)


@tool
async def playwright_type(selector: str, text: str, submit: bool = False) -> str:
    """Fill an input selected by a Playwright selector and optionally press Enter."""
    if not str(selector or "").strip():
        return "Error: selector must not be empty."
    return await _dispatch("type", selector=selector, text=text, submit=submit)


@tool
async def playwright_press(key: str, selector: str = "") -> str:
    """Press a key globally or on an optional Playwright selector."""
    if not str(key or "").strip():
        return "Error: key must not be empty."
    return await _dispatch("press", key=key, selector=selector)


@tool
async def playwright_scroll(delta_y: int = 600, delta_x: int = 0) -> str:
    """Scroll the current page by pixel deltas; positive delta_y scrolls down."""
    return await _dispatch("scroll", delta_x=delta_x, delta_y=delta_y)


@tool
async def playwright_screenshot(
    path: str = "output/playwright.png",
    full_page: bool = True,
) -> str:
    """Save a screenshot under the backend working directory and return its path."""
    # Path resolution and directory creation perform synchronous filesystem
    # calls. Keep them in the dedicated Playwright worker rather than the ASGI
    # event-loop thread used by LangGraph dev.
    return await _dispatch("screenshot", path=path, full_page=full_page)


@tool
async def playwright_close() -> str:
    """Close this thread's isolated Playwright browser context."""
    return await _dispatch("close")


PLAYWRIGHT_TOOLS = [
    playwright_open,
    playwright_snapshot,
    playwright_click,
    playwright_type,
    playwright_press,
    playwright_scroll,
    playwright_screenshot,
    playwright_close,
]

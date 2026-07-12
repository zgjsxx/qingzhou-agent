"""Built-in web search and extraction tools."""

from __future__ import annotations

import concurrent.futures as cf
import contextlib
import html
import ipaddress
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from langchain.tools import tool


DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 10
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_EXTRACT_MAX_CHARS = 20_000
MAX_EXTRACT_MAX_CHARS = 80_000
USER_AGENT = "qingzhou-agent/1.0 (+https://github.com/zgjsxx/qingzhou-agent)"


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(os.getenv(name, default))
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, minimum)


def _clamp_int(value: int | None, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


@contextlib.contextmanager
def _without_ssl_keylogfile():
    """Avoid local SSLKEYLOGFILE permission problems breaking web tools."""
    previous = os.environ.pop("SSLKEYLOGFILE", None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ["SSLKEYLOGFILE"] = previous


def _tool_timeout() -> int:
    return _int_env("WEB_TOOL_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)


def _run_ddgs_search(query: str, limit: int) -> list[dict[str, Any]]:
    from ddgs import DDGS  # type: ignore

    results: list[dict[str, Any]] = []
    with _without_ssl_keylogfile():
        with DDGS(timeout=10) as client:
            for index, hit in enumerate(client.text(query, max_results=limit)):
                if index >= limit:
                    break
                results.append(
                    {
                        "title": str(hit.get("title") or "").strip(),
                        "url": str(hit.get("href") or hit.get("url") or "").strip(),
                        "snippet": str(hit.get("body") or "").strip(),
                        "position": index + 1,
                    }
                )
    return results


def _search_ddgs(query: str, limit: int, timeout_seconds: int) -> tuple[list[dict[str, Any]] | None, str | None]:
    try:
        import ddgs  # type: ignore  # noqa: F401
    except ImportError:
        return None, "ddgs package is not installed. Install project dependencies or run: pip install ddgs"

    pool = cf.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_run_ddgs_search, query, limit)
        return future.result(timeout=timeout_seconds), None
    except cf.TimeoutError:
        return None, f"DuckDuckGo search timed out after {timeout_seconds}s."
    except Exception as exc:  # noqa: BLE001 - ddgs raises provider-specific exceptions.
        return None, f"DuckDuckGo search failed: {exc}"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _format_search_results(query: str, results: list[dict[str, Any]]) -> str:
    if not results:
        return f"No web search results for: {query}"

    lines = [f"Web search results for: {query}"]
    for item in results:
        title = item.get("title") or "(untitled)"
        url = item.get("url") or ""
        snippet = item.get("snippet") or item.get("description") or ""
        position = item.get("position") or len(lines)
        lines.append(f"\n{position}. {title}")
        if url:
            lines.append(f"   URL: {url}")
        if snippet:
            lines.append(f"   Snippet: {snippet}")
    return "\n".join(lines)


def web_search_impl(query: str, limit: int = DEFAULT_SEARCH_LIMIT, backend: str = "ddgs") -> str:
    """Run a web search and return a compact text result list."""
    query = str(query or "").strip()
    if not query:
        return "Error: query must not be empty."

    safe_limit = _clamp_int(limit, DEFAULT_SEARCH_LIMIT, 1, MAX_SEARCH_LIMIT)
    selected_backend = (backend or os.getenv("WEB_SEARCH_BACKEND", "ddgs")).strip().lower()
    if selected_backend in {"auto", ""}:
        selected_backend = "ddgs"
    if selected_backend != "ddgs":
        return "Error: unsupported web_search backend. Supported backends: ddgs."

    results, error = _search_ddgs(query, safe_limit, _tool_timeout())
    if error:
        return f"Error: {error}"
    return _format_search_results(query, results or [])


class _HtmlTextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }
    SKIP_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in self.SKIP_TAGS:
            self._skip_depth += 1
        if lowered in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if lowered in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text)
            self.parts.append(" ")

    def text(self) -> str:
        joined = "".join(self.parts)
        joined = html.unescape(joined)
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        joined = re.sub(r"\n\s*\n\s*\n+", "\n\n", joined)
        return joined.strip()


def _normalize_urls(urls: str | list[str]) -> list[str]:
    if isinstance(urls, str):
        raw_items = re.split(r"[\r\n,]+", urls)
    elif isinstance(urls, list):
        raw_items = [str(item) for item in urls]
    else:
        raw_items = [str(urls)]
    return [item.strip() for item in raw_items if item and item.strip()]


def _is_private_hostname(hostname: str) -> bool:
    lowered = hostname.strip().lower().rstrip(".")
    if lowered in {"localhost", "0.0.0.0"} or lowered.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(lowered)
        return address.is_private or address.is_loopback or address.is_link_local or address.is_multicast
    except ValueError:
        return False


def _validate_public_http_url(url: str) -> tuple[urllib.parse.ParseResult | None, str | None]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None, "only http and https URLs are supported"
    if not parsed.hostname:
        return None, "URL hostname is missing"
    if _is_private_hostname(parsed.hostname):
        return None, "private, local, and link-local hosts are not allowed"
    return parsed, None


def _request_text(url: str, timeout_seconds: int) -> tuple[str | None, str | None]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/*, */*;q=0.8"})
    try:
        with _without_ssl_keylogfile():
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                content_type = response.headers.get("content-type", "")
                body = response.read()
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"request failed: {exc.reason}"
    except socket.timeout:
        return None, "request timed out"
    except OSError as exc:
        return None, f"request failed: {exc}"

    charset = "utf-8"
    match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
    if match:
        charset = match.group(1).strip("\"'")
    return body.decode(charset, errors="replace"), None


def _jina_reader_url(url: str) -> str:
    return "https://r.jina.ai/http://" + url


def _extract_with_jina(url: str, timeout_seconds: int) -> tuple[str | None, str | None]:
    return _request_text(_jina_reader_url(url), timeout_seconds)


def _extract_with_raw_http(url: str, timeout_seconds: int) -> tuple[str | None, str | None]:
    text, error = _request_text(url, timeout_seconds)
    if error:
        return None, error
    if not text:
        return "", None
    parser = _HtmlTextExtractor()
    try:
        parser.feed(text)
    except Exception:  # noqa: BLE001 - HTMLParser can raise on malformed markup.
        return re.sub(r"\s+", " ", text).strip(), None
    extracted = parser.text()
    return extracted or re.sub(r"\s+", " ", text).strip(), None


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}\n\n[truncated, {omitted} characters omitted]"


def _extract_one(url: str, backend: str, timeout_seconds: int, max_chars: int) -> str:
    parsed, validation_error = _validate_public_http_url(url)
    if validation_error or parsed is None:
        return f"## {url}\n\nError: {validation_error}."

    selected_backend = backend
    text: str | None = None
    error: str | None = None
    if selected_backend in {"auto", "jina"}:
        text, error = _extract_with_jina(url, timeout_seconds)
        if selected_backend == "jina" and error:
            return f"## {url}\n\nError: Jina Reader extraction failed: {error}."
    if text is None and selected_backend in {"auto", "raw"}:
        text, error = _extract_with_raw_http(url, timeout_seconds)
    if text is None:
        return f"## {url}\n\nError: extraction failed: {error or 'unknown error'}."

    return f"## {url}\n\n{_truncate(text.strip(), max_chars)}"


def web_extract_impl(
    urls: str | list[str],
    max_chars: int = DEFAULT_EXTRACT_MAX_CHARS,
    backend: str = "auto",
) -> str:
    """Extract readable text/markdown from one or more public URLs."""
    normalized = _normalize_urls(urls)
    if not normalized:
        return "Error: urls must not be empty."

    selected_backend = (backend or os.getenv("WEB_EXTRACT_BACKEND", "auto")).strip().lower()
    if selected_backend not in {"auto", "jina", "raw"}:
        return "Error: unsupported web_extract backend. Supported backends: auto, jina, raw."

    safe_max_chars = _clamp_int(max_chars, DEFAULT_EXTRACT_MAX_CHARS, 1000, MAX_EXTRACT_MAX_CHARS)
    timeout_seconds = _tool_timeout()
    sections = [_extract_one(url, selected_backend, timeout_seconds, safe_max_chars) for url in normalized]
    return "\n\n---\n\n".join(sections)


@tool
def web_search(query: str, limit: int = DEFAULT_SEARCH_LIMIT, backend: str = "ddgs") -> str:
    """Search the web with the built-in free DDGS backend.

    Args:
        query: Search query.
        limit: Number of results, clamped to 1..10.
        backend: Search backend. Only ddgs is supported in the built-in MVP.
    """
    return web_search_impl(query=query, limit=limit, backend=backend)


@tool
def web_extract(urls: str | list[str], max_chars: int = DEFAULT_EXTRACT_MAX_CHARS, backend: str = "auto") -> str:
    """Extract readable text from public URLs.

    Args:
        urls: A URL string, comma/newline-separated URLs, or a list of URLs.
        max_chars: Maximum extracted characters per URL, clamped to 1000..80000.
        backend: auto, jina, or raw. auto tries Jina Reader first, then raw HTTP.
    """
    return web_extract_impl(urls=urls, max_chars=max_chars, backend=backend)

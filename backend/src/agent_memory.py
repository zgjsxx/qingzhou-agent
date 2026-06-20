"""Persistent file-based memory for cross-session agent knowledge."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from agent_logging import log_event

BACKEND_DIR = Path(__file__).resolve().parents[1]
MEMORY_DIR = BACKEND_DIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
MEMORY_TYPES = {"user", "feedback", "project", "reference"}


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(os.getenv(name, default))
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, minimum)


def is_memory_enabled() -> bool:
    """Return whether persistent memory is enabled."""
    return _bool_env("AGENT_MEMORY_ENABLED", True)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]+", "-", value.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:80] or f"memory-{int(time.time())}"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    meta: dict[str, str] = {}
    for line in parts[1].strip().splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip("\"'")
    return meta, parts[2].strip()


def _memory_path(filename: str) -> Path:
    path = (MEMORY_DIR / filename).resolve()
    if not path.is_relative_to(MEMORY_DIR.resolve()):
        raise ValueError(f"Memory path escapes memory directory: {filename}")
    return path


def _normalize_memory_type(mem_type: str) -> str:
    normalized = str(mem_type or "").strip().lower()
    return normalized if normalized in MEMORY_TYPES else "user"


def write_memory_file(name: str, mem_type: str, description: str, body: str) -> str:
    """Write or replace a memory markdown file and rebuild the index."""
    if not is_memory_enabled():
        return "Memory is disabled."

    clean_name = str(name or "").strip()
    clean_description = str(description or "").strip()
    clean_body = str(body or "").strip()
    if not clean_name:
        return "Error: memory name must not be empty."
    if not clean_description:
        return "Error: memory description must not be empty."
    if not clean_body:
        return "Error: memory body must not be empty."

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{_slugify(clean_name)}.md"
    path = _memory_path(filename)
    normalized_type = _normalize_memory_type(mem_type)
    path.write_text(
        (
            "---\n"
            f"name: {clean_name}\n"
            f"description: {clean_description}\n"
            f"type: {normalized_type}\n"
            "---\n\n"
            f"{clean_body}\n"
        ),
        encoding="utf-8",
    )
    rebuild_memory_index()
    return f"Memory saved: {path}"


def list_memory_files() -> list[dict[str, str]]:
    """List memory markdown files with frontmatter metadata."""
    if not MEMORY_DIR.exists():
        return []

    items: list[dict[str, str]] = []
    for path in sorted(MEMORY_DIR.glob("*.md")):
        if path.name == MEMORY_INDEX.name:
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        meta, body = _parse_frontmatter(raw)
        items.append(
            {
                "filename": path.name,
                "name": meta.get("name", path.stem),
                "description": meta.get("description", body.splitlines()[0][:120] if body else ""),
                "type": _normalize_memory_type(meta.get("type", "user")),
                "body": body,
            }
        )
    return items


def rebuild_memory_index() -> str:
    """Rebuild .memory/MEMORY.md from individual memory files."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"- [{item['name']}]({item['filename']}) — {item['description']} ({item['type']})"
        for item in list_memory_files()
    ]
    max_lines = _int_env("AGENT_MEMORY_INDEX_MAX_LINES", 200)
    text = "\n".join(lines[:max_lines])
    MEMORY_INDEX.write_text(f"{text}\n" if text else "", encoding="utf-8")
    return str(MEMORY_INDEX)


def read_memory_index() -> str:
    """Read the memory index for system prompt injection."""
    if not is_memory_enabled() or not MEMORY_INDEX.exists():
        return ""
    max_chars = _int_env("AGENT_MEMORY_INDEX_MAX_CHARS", 25_000)
    return MEMORY_INDEX.read_text(encoding="utf-8", errors="replace")[:max_chars].strip()


def read_memory_file(filename: str) -> str:
    """Read one memory file with an injection budget."""
    path = _memory_path(filename)
    if not path.exists() or not path.is_file():
        return ""
    max_chars = _int_env("AGENT_MEMORY_FILE_MAX_CHARS", 4096)
    return path.read_text(encoding="utf-8", errors="replace")[:max_chars].strip()


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
            else:
                text = getattr(block, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content)


def _recent_human_text(messages: list[Any]) -> str:
    texts: list[str] = []
    for message in reversed(messages):
        if isinstance(message, HumanMessage) or getattr(message, "type", None) == "human":
            text = _message_text(message).strip()
            if text:
                texts.append(text)
            if len(texts) >= 3:
                break
    return "\n".join(reversed(texts))[:4000]


def _keywords(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9_-]{3,}|[\u4e00-\u9fff]{2,}", text)
        if len(token.strip()) >= 2
    }


def select_relevant_memories(messages: list[Any], max_items: int | None = None) -> list[str]:
    """Select relevant memory filenames using deterministic keyword matching."""
    files = list_memory_files()
    if not files:
        return []

    recent = _recent_human_text(messages)
    if not recent.strip():
        return []

    max_selected = max_items if max_items is not None else _int_env("AGENT_MEMORY_MAX_RELEVANT", 5)
    recent_keywords = _keywords(recent)
    scored: list[tuple[int, str]] = []
    for item in files:
        haystack = f"{item['name']} {item['description']} {item['type']}".lower()
        haystack_keywords = _keywords(haystack)
        score = len(recent_keywords & haystack_keywords)
        if item["name"].lower() in recent.lower() or item["description"].lower() in recent.lower():
            score += 3
        if score > 0:
            scored.append((score, item["filename"]))

    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [filename for _, filename in scored[:max_selected]]


def load_relevant_memories(messages: list[Any]) -> str:
    """Load selected memory contents for request-level injection."""
    selected = select_relevant_memories(messages)
    if not selected:
        return ""

    max_total = _int_env("AGENT_MEMORY_TOTAL_MAX_CHARS", 60_000)
    parts = ["<relevant_memories>"]
    total = len(parts[0])
    for filename in selected:
        content = read_memory_file(filename)
        if not content:
            continue
        block = f"\n\n<!-- {filename} -->\n{content}"
        if total + len(block) > max_total:
            break
        parts.append(block)
        total += len(block)
    parts.append("\n</relevant_memories>")
    return "".join(parts) if len(parts) > 2 else ""


def _copy_message_with_content(message: BaseMessage, content: str) -> BaseMessage:
    if hasattr(message, "model_copy"):
        return message.model_copy(update={"content": content})
    return HumanMessage(content=content, id=getattr(message, "id", None))


def _inject_memories_into_messages(messages: list[Any], memory_text: str) -> list[Any]:
    if not memory_text:
        return messages

    updated = list(messages)
    for index in range(len(updated) - 1, -1, -1):
        message = updated[index]
        if isinstance(message, HumanMessage) or getattr(message, "type", None) == "human":
            original = _message_text(message)
            content = f"{memory_text}\n\n{original}"
            updated[index] = _copy_message_with_content(message, content)
            return updated
    return messages


def _system_with_memory_index(system_message: Any) -> SystemMessage | None:
    index = read_memory_index()
    if not index:
        return system_message

    memory_section = (
        "\n\nPersistent memory index:\n"
        f"{index}\n"
        "Use relevant memories when they are injected into the current request. "
        "Call remember when the user explicitly asks you to remember a durable preference, "
        "constraint, project fact, or reference."
    )
    if system_message is None:
        return SystemMessage(content=memory_section.strip())
    content = getattr(system_message, "content", "")
    if memory_section in str(content):
        return system_message
    return SystemMessage(content=f"{content}{memory_section}")


class AgentMemoryMiddleware(AgentMiddleware):
    """Inject persistent memory index and relevant memory content per model call."""

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        request = self._prepare_request(request)
        return handler(request)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        request = self._prepare_request(request)
        return await handler(request)

    def _prepare_request(self, request: Any) -> Any:
        if not is_memory_enabled():
            return request

        messages = list(getattr(request, "messages", []) or [])
        memory_text = load_relevant_memories(messages)
        updated_messages = _inject_memories_into_messages(messages, memory_text)
        updated_system = _system_with_memory_index(getattr(request, "system_message", None))
        if memory_text:
            log_event("memory.inject", selected_chars=len(memory_text))
        return request.override(messages=updated_messages, system_message=updated_system)

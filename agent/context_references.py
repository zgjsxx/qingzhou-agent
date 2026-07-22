"""Inline context references such as @file:path and @folder:path."""

from __future__ import annotations

import os
import re
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES


REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_RE = re.compile(
    r"(?<![\w/])@(?P<kind>file|folder):"
    r"(?P<target>`[^`]+`(?::\d+(?:-\d+)?)?|\"[^\"]+\"(?::\d+(?:-\d+)?)?|'[^']+'(?::\d+(?:-\d+)?)?|\S+)",
    re.IGNORECASE,
)
LINE_RANGE_RE = re.compile(r"^(?P<path>.+):(?P<start>\d+)(?:-(?P<end>\d+))?$")
TRAILING_PUNCTUATION = ".,;!?。，；！？、"
CLOSING_TO_OPENING = {")": "(", "]": "[", "}": "{", "）": "（", "】": "【"}
INLINE_REFERENCE_SUFFIXES = ("\u7684\u5185\u5bb9", "\u5185\u5bb9")
INLINE_REFERENCE_DELIMITERS = ",;!?，。；！？、"
FENCE_RE = re.compile(r"```")

DEFAULT_MAX_FILE_CHARS = 80_000
DEFAULT_MAX_INJECT_CHARS = 120_000
DEFAULT_FOLDER_LIMIT = 200

BLOCKED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    ".next",
    ".runtime",
    ".langgraph_api",
    "__pycache__",
    "logs",
}
BLOCKED_FILE_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
}
BLOCKED_RELATIVE_FILES = {
    Path("config") / "qingzhou-agent.json",
}
TEXT_EXTENSIONS = {
    ".bat",
    ".c",
    ".cfg",
    ".conf",
    ".cpp",
    ".cs",
    ".css",
    ".csv",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".log",
    ".md",
    ".ps1",
    ".py",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
LANGUAGE_BY_EXTENSION = {
    ".bat": "bat",
    ".c": "c",
    ".conf": "text",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".csv": "csv",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "jsx",
    ".md": "markdown",
    ".ps1": "powershell",
    ".py": "python",
    ".rs": "rust",
    ".sh": "bash",
    ".sql": "sql",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}


@dataclass(frozen=True)
class ContextReference:
    raw: str
    kind: str
    target: str
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True)
class ContextReferenceResult:
    message: str
    references: tuple[ContextReference, ...]
    warnings: tuple[str, ...]
    injected_chars: int
    blocked: bool = False


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(os.getenv(name, default))
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, minimum)


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
            else:
                text = getattr(block, "text", None)
                parts.append(str(text if text is not None else block))
        return "\n".join(parts)
    return str(content)


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"`", '"', "'"}:
        return value[1:-1]
    return value


def _split_inline_suffix(value: str) -> tuple[str, str]:
    for suffix in INLINE_REFERENCE_SUFFIXES:
        if value.endswith(suffix):
            return value[: -len(suffix)], suffix
    return value, ""


def _trim_target(raw: str) -> tuple[str, str]:
    target = raw
    suffix = ""
    for index, char in enumerate(target):
        if char in INLINE_REFERENCE_DELIMITERS:
            suffix = target[index:]
            target = target[:index]
            break
    while target and target[-1] in TRAILING_PUNCTUATION:
        suffix = target[-1] + suffix
        target = target[:-1]
    while target and target[-1] in CLOSING_TO_OPENING and target.count(target[-1]) > target.count(CLOSING_TO_OPENING[target[-1]]):
        suffix = target[-1] + suffix
        target = target[:-1]
    return target, suffix


def _parse_file_target(target: str) -> tuple[str, int | None, int | None]:
    target, _suffix = _split_inline_suffix(target)
    match = LINE_RANGE_RE.match(target)
    if not match:
        return _strip_wrapping_quotes(target), None, None
    start = int(match.group("start"))
    end_text = match.group("end")
    end = int(end_text) if end_text else start
    return _strip_wrapping_quotes(match.group("path")), start, end


def parse_context_references(text: str) -> list[ContextReference]:
    """Parse supported context references from message text."""
    references: list[ContextReference] = []
    for match in REFERENCE_RE.finditer(text):
        kind = match.group("kind").lower()
        raw_target, _suffix = _trim_target(match.group("target"))
        start = end = None
        if kind == "file":
            target, start, end = _parse_file_target(raw_target)
        else:
            target = _strip_wrapping_quotes(raw_target)
        references.append(ContextReference(raw=match.group(0), kind=kind, target=target, start=start, end=end))
    return references


def _normalize_plain_segment(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]{2,}", " ", line)
        line = re.sub(r"\s+([,，.;；:：!?！？、])", r"\1", line)
        lines.append(line.rstrip())
    if text.endswith(("\n", "\r")):
        lines.append("")
    return "\n".join(lines)


def _inline_reference_blocks(text: str, blocks: list[str | None]) -> str:
    parts: list[str] = []
    cursor = 0
    for index, match in enumerate(REFERENCE_RE.finditer(text)):
        parts.append(_normalize_plain_segment(text[cursor : match.start()]))
        raw_target, suffix = _trim_target(match.group("target"))
        _target, inline_suffix = _split_inline_suffix(raw_target)
        block = blocks[index] if index < len(blocks) else None
        if block:
            parts.append(f"\n\n--- Attached Context ---\n\n{block}\n\n")
        parts.append(_normalize_plain_segment(f"{inline_suffix}{suffix}"))
        cursor = match.end()
    parts.append(_normalize_plain_segment(text[cursor:]))
    return "".join(parts).strip()


def _resolve_workspace_path(target: str, cwd: Path, allowed_root: Path) -> Path:
    target_path = Path(target).expanduser()
    if not target_path.is_absolute():
        target_path = cwd / target_path
    resolved = target_path.resolve()
    resolved.relative_to(allowed_root)
    return resolved


def _allowed_roots(explicit_root: Path | None = None) -> list[Path]:
    roots = [Path(explicit_root or REPO_ROOT).resolve()]
    if explicit_root is None:
        home = Path.home().resolve()
        if home not in roots:
            roots.append(home)
        extra = os.getenv("AGENT_CONTEXT_REF_ALLOWED_ROOTS", "")
        for item in extra.split(os.pathsep):
            if not item.strip():
                continue
            try:
                path = Path(item).expanduser().resolve()
            except OSError:
                continue
            if path not in roots:
                roots.append(path)
    return roots


def _resolve_allowed_path(target: str, cwd: Path, allowed_roots: list[Path]) -> tuple[Path, Path]:
    target_path = Path(target).expanduser()
    if not target_path.is_absolute():
        target_path = cwd / target_path
    resolved = target_path.resolve()
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return resolved, root
        except ValueError:
            continue
    raise ValueError(f"Path escapes allowed roots: {target}")


def _relative_label(path: Path, allowed_root: Path) -> str:
    try:
        return path.relative_to(allowed_root).as_posix()
    except ValueError:
        return path.as_posix()


def _is_blocked_path(path: Path, allowed_root: Path) -> bool:
    try:
        relative = path.relative_to(allowed_root)
    except ValueError:
        return True
    if path.name in BLOCKED_FILE_NAMES:
        return True
    if relative in BLOCKED_RELATIVE_FILES:
        return True
    return any(part in BLOCKED_DIRS for part in relative.parts)


def _looks_binary(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return False
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return True
    return b"\x00" in chunk


def _safe_fence(content: str) -> str:
    return FENCE_RE.sub("`` `", content)


def _read_file_reference(
    reference: ContextReference,
    cwd: Path,
    allowed_roots: list[Path],
    max_file_chars: int,
) -> tuple[str | None, str | None]:
    try:
        path, allowed_root = _resolve_allowed_path(reference.target, cwd, allowed_roots)
    except (OSError, ValueError):
        return None, f"Skipped {reference.raw}: path is outside the allowed roots."
    if _is_blocked_path(path, allowed_root):
        return None, f"Skipped {reference.raw}: path is blocked."
    if not path.exists():
        return None, f"Skipped {reference.raw}: file does not exist."
    if not path.is_file():
        return None, f"Skipped {reference.raw}: path is not a file."
    if _looks_binary(path):
        return None, f"Skipped {reference.raw}: file appears to be binary."

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return None, f"Skipped {reference.raw}: file is not valid UTF-8 text."
    except OSError as exc:
        return None, f"Skipped {reference.raw}: could not read file ({exc})."

    start = reference.start
    end = reference.end
    if start is not None:
        if start < 1 or (end is not None and end < start):
            return None, f"Skipped {reference.raw}: invalid line range."
        if start > len(lines):
            return None, f"Skipped {reference.raw}: line range starts after end of file."
        selected = lines[start - 1 : end]
        line_label = f":{start}-{end}" if end and end != start else f":{start}"
    else:
        selected = lines
        line_label = ""

    content = "\n".join(selected)
    truncated = ""
    if len(content) > max_file_chars:
        content = content[:max_file_chars]
        truncated = f"\n\n[Truncated after {max_file_chars} characters.]"

    language = LANGUAGE_BY_EXTENSION.get(path.suffix.lower(), "")
    label = f"{_relative_label(path, allowed_root)}{line_label}"
    block = f"[file: {label}]\n```{language}\n{_safe_fence(content)}{truncated}\n```"
    return block, None


def _format_size(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _list_folder_reference(
    reference: ContextReference,
    cwd: Path,
    allowed_roots: list[Path],
    folder_limit: int,
) -> tuple[str | None, str | None]:
    try:
        path, allowed_root = _resolve_allowed_path(reference.target, cwd, allowed_roots)
    except (OSError, ValueError):
        return None, f"Skipped {reference.raw}: path is outside the allowed roots."
    if _is_blocked_path(path, allowed_root):
        return None, f"Skipped {reference.raw}: path is blocked."
    if not path.exists():
        return None, f"Skipped {reference.raw}: folder does not exist."
    if not path.is_dir():
        return None, f"Skipped {reference.raw}: path is not a folder."

    entries: list[Path] = []
    for current, dirnames, filenames in os.walk(path):
        current_path = Path(current)
        try:
            current_relative = current_path.relative_to(allowed_root)
        except ValueError:
            dirnames[:] = []
            continue
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if name not in BLOCKED_DIRS and not name.startswith(".") and not _is_blocked_path(current_path / name, allowed_root)
        ]
        for filename in sorted(filenames):
            child = current_path / filename
            if filename.startswith(".") or _is_blocked_path(child, allowed_root):
                continue
            entries.append(child)
            if len(entries) >= folder_limit:
                break
        if len(entries) >= folder_limit:
            break

    label = _relative_label(path, allowed_root)
    root_line = f"{label}/" if label != "." else "./"
    lines = [root_line]
    for child in entries:
        relative = child.relative_to(path).as_posix()
        lines.append(f"- {relative} ({_format_size(child)})")
    if len(entries) >= folder_limit:
        lines.append(f"- ... truncated after {folder_limit} files")
    if len(entries) == 0:
        lines.append("- (empty)")
    return f"[folder: {label}]\n```text\n{chr(10).join(lines)}\n```", None


def preprocess_context_references(
    text: str,
    *,
    cwd: Path | str | None = None,
    allowed_root: Path | str | None = None,
    max_file_chars: int | None = None,
    max_inject_chars: int | None = None,
    folder_limit: int | None = None,
) -> ContextReferenceResult:
    """Expand supported context references in a user message."""
    references = parse_context_references(text)
    if not references:
        return ContextReferenceResult(message=text, references=(), warnings=(), injected_chars=0)

    root = Path(allowed_root or REPO_ROOT).resolve()
    roots = _allowed_roots(root if allowed_root is not None else None)
    base_cwd = Path(cwd or root).resolve()
    try:
        base_cwd.relative_to(root)
    except ValueError:
        base_cwd = root

    file_limit = max_file_chars if max_file_chars is not None else _int_env("AGENT_CONTEXT_REF_MAX_FILE_CHARS", DEFAULT_MAX_FILE_CHARS)
    inject_limit = (
        max_inject_chars
        if max_inject_chars is not None
        else _int_env("AGENT_CONTEXT_REF_MAX_INJECT_CHARS", DEFAULT_MAX_INJECT_CHARS)
    )
    list_limit = folder_limit if folder_limit is not None else _int_env("AGENT_CONTEXT_REF_FOLDER_LIMIT", DEFAULT_FOLDER_LIMIT)

    blocks: list[str | None] = []
    warnings: list[str] = []
    for reference in references:
        if reference.kind == "file":
            block, warning = _read_file_reference(reference, base_cwd, roots, file_limit)
        else:
            block, warning = _list_folder_reference(reference, base_cwd, roots, list_limit)
        if warning:
            warnings.append(warning)
        blocks.append(block)

    injected = "\n\n".join(block for block in blocks if block)
    if len(injected) > inject_limit:
        warnings.append(f"Skipped context attachment: expanded context exceeded {inject_limit} characters.")
        blocks = [None for _ in blocks]
        injected = ""
        blocked = True
    else:
        blocked = False

    message = _inline_reference_blocks(text, blocks)
    parts = [message] if message else []
    if warnings:
        parts.append("--- Context Reference Warnings ---\n" + "\n".join(f"- {warning}" for warning in warnings))
    message = "\n\n".join(parts).strip()
    return ContextReferenceResult(
        message=message or text,
        references=tuple(references),
        warnings=tuple(warnings),
        injected_chars=len(injected),
        blocked=blocked,
    )


def context_reference_update(state: dict[str, Any]) -> dict[str, Any] | None:
    messages = state.get("messages") or []
    if not messages:
        return None
    last = messages[-1]
    if not (isinstance(last, HumanMessage) or getattr(last, "type", None) == "human"):
        return None
    content = getattr(last, "content", None)
    if not isinstance(content, (str, list)):
        return None

    original_text = _message_text(last)
    try:
        result = preprocess_context_references(original_text, cwd=Path.cwd())
    except Exception as exc:  # noqa: BLE001 - keep before_model failures visible to the model/user.
        warning = (
            f"{original_text}\n\n"
            "--- Context Reference Warnings ---\n"
            f"- Failed to expand context references before model call: {exc}"
        )
        updated = last.model_copy(update={"content": warning}) if isinstance(last, BaseMessage) else last
        if getattr(last, "id", None):
            return {"messages": [updated]}
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES, content=""), *messages[:-1], updated]}
    if not result.references or result.message == _message_text(last):
        return None

    updated = last.model_copy(update={"content": result.message}) if isinstance(last, BaseMessage) else last
    if getattr(last, "id", None):
        return {"messages": [updated]}
    return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES, content=""), *messages[:-1], updated]}


class AgentContextReferenceMiddleware(AgentMiddleware):
    """Expand @file and @folder references before the model runs."""

    def before_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        return context_reference_update(state)

    async def abefore_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        return await asyncio.to_thread(context_reference_update, state)

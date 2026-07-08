"""Lightweight console entry point for qingzhou-agent.

The CLI intentionally stays thin: it loads the same LangGraph agent used by the
web and IM entry points, keeps an in-process message history, and leaves agent
behavior in ``agent.graph``.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import uuid
from pathlib import Path
from typing import Any

DEFAULT_HISTORY_MAX_MESSAGES = 40
EXIT_COMMANDS = {"/exit", "/quit", "exit", "quit"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE pairs from .env without overriding the shell."""
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if value and value[0] in {'"', "'"}:
            try:
                value = shlex.split(value, posix=False)[0]
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]
            except ValueError:
                value = value.strip("\"'")
        os.environ[key] = value


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(getattr(item, "text", item)))
        return "".join(parts)
    return str(content or "")


def _message_key(message: Any, index: int) -> str:
    message_id = getattr(message, "id", None)
    if message_id:
        return f"id:{message_id}"
    return (
        f"{index}:{getattr(message, 'type', message.__class__.__name__)}:"
        f"{_content_to_text(getattr(message, 'content', ''))[:120]}"
    )


def _is_ai_message(message: Any) -> bool:
    return (
        getattr(message, "type", "") == "ai"
        or getattr(message, "role", "") == "assistant"
        or message.__class__.__name__ == "AIMessage"
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
            if _is_ai_message(message):
                text = _content_to_text(getattr(message, "content", "")).strip()
                if text:
                    texts.append(text)

    return final_state, texts


def _load_graph() -> Any:
    _load_dotenv(_repo_root() / ".env")
    os.environ["QINGZHOU_CLI"] = "1"
    from agent.graph import graph

    return graph


def _print_banner(thread_id: str) -> None:
    print("Qingzhou Agent CLI")
    print(f"thread: {thread_id}")
    print("Type /exit or /quit to leave.")
    print()


def run_chat(args: argparse.Namespace) -> int:
    graph = _load_graph()
    thread_id = args.thread_id or f"cli_{uuid.uuid4().hex[:12]}"
    history: list[Any] = []
    max_messages = max(2, int(args.history_max))

    if args.prompt:
        prompt = " ".join(args.prompt).strip()
        if not prompt:
            return 0
        return _run_once(graph, prompt, thread_id, history, max_messages, args)

    _print_banner(thread_id)
    while True:
        try:
            prompt = input("qingzhou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not prompt:
            continue
        if prompt.lower() in EXIT_COMMANDS:
            return 0
        status = _run_once(graph, prompt, thread_id, history, max_messages, args)
        if status != 0:
            return status


def _run_once(
    graph: Any,
    prompt: str,
    thread_id: str,
    history: list[Any],
    max_messages: int,
    args: argparse.Namespace,
) -> int:
    config = {
        "configurable": {"thread_id": thread_id},
        "metadata": {"source": "cli"},
        "tags": ["cli"],
        "callbacks": [],
    }
    input_payload = {"messages": [*history, {"role": "user", "content": prompt}]}

    try:
        result, texts = _collect_ai_texts(graph, input_payload, config, history)
    except KeyboardInterrupt:
        print()
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    messages = result.get("messages", []) if isinstance(result, dict) else []
    if messages:
        history[:] = list(messages[-max_messages:])

    if not texts:
        print("(no text response)")
        return 0

    output_texts = texts if args.all_messages else texts[-1:]
    for index, text in enumerate(output_texts):
        if index:
            print()
        print(text)
    return 0


def _add_chat_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--thread-id",
        help="Thread id for this CLI session. Defaults to a fresh cli_* id.",
    )
    parser.add_argument(
        "--history-max",
        type=int,
        default=DEFAULT_HISTORY_MAX_MESSAGES,
        help=f"Maximum messages kept in local CLI history. Default: {DEFAULT_HISTORY_MAX_MESSAGES}.",
    )
    parser.add_argument(
        "--all-messages",
        action="store_true",
        help="Print every new assistant message instead of only the final one.",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qingzhou",
        description="Lightweight command-line interface for qingzhou-agent.",
    )
    _add_chat_arguments(parser)
    parser.set_defaults(func=run_chat, command=None)
    return parser


def build_chat_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qingzhou chat",
        description="Start a qingzhou-agent chat session.",
    )
    _add_chat_arguments(parser)
    parser.set_defaults(func=run_chat, command="chat")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "chat":
        parser = build_chat_parser()
        args = parser.parse_args(argv[1:])
    else:
        parser = build_parser()
        args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())

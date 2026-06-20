"""Tools for the agent."""

from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import time
import glob as glob_module
import threading
from contextvars import ContextVar
from pathlib import Path

from langchain.tools import tool

from agent_context import MANUAL_COMPACT_MARKER
from skills import load_skill_content

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 120
DEFAULT_MAX_OUTPUT_CHARS = 12000
TODO_STATUSES = {"pending", "in_progress", "completed"}
DEFAULT_TODO_THREAD_ID = "__default__"
CURRENT_TOOL_THREAD_ID: ContextVar[str] = ContextVar(
    "CURRENT_TOOL_THREAD_ID",
    default=DEFAULT_TODO_THREAD_ID,
)
THREAD_TODOS: dict[str, list[dict[str, str]]] = {}
THREAD_TODOS_LOCK = threading.Lock()


def set_current_tool_thread_id(thread_id: str | None):
    """Set the current tool thread id for tools that keep per-thread state."""
    return CURRENT_TOOL_THREAD_ID.set(thread_id or DEFAULT_TODO_THREAD_ID)


def reset_current_tool_thread_id(token) -> None:
    """Restore the previous tool thread id after a tool call completes."""
    CURRENT_TOOL_THREAD_ID.reset(token)


def _resolve_cwd(cwd: str) -> str:
    if not cwd:
        return str(Path.cwd())

    resolved = Path(cwd).expanduser().resolve()
    if not resolved.exists():
        raise ValueError(f"Working directory does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Working directory is not a directory: {resolved}")
    return str(resolved)


def _resolve_safe_path(path: str, cwd: str = "") -> tuple[Path, Path]:
    if not path or not path.strip():
        raise ValueError("Path must not be empty.")

    root = Path(_resolve_cwd(cwd)).resolve()
    requested = Path(path).expanduser()
    resolved = (root / requested).resolve() if not requested.is_absolute() else requested.resolve()

    if not resolved.is_relative_to(root):
        raise ValueError(f"Path escapes working directory: {path}")

    return root, resolved


def _relative_to_root(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def _select_shell(shell: str) -> tuple[str, list[str]]:
    requested = (shell or "auto").strip().lower()

    if requested == "auto":
        requested = "powershell" if os.name == "nt" else "bash"

    if requested == "powershell":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            raise ValueError("PowerShell is not available on this machine.")
        return "powershell", [executable, "-NoProfile", "-NonInteractive", "-Command"]

    if requested == "cmd":
        executable = shutil.which("cmd")
        if not executable:
            raise ValueError("cmd.exe is not available on this machine.")
        return "cmd", [executable, "/c"]

    if requested == "bash":
        executable = shutil.which("bash")
        if not executable:
            raise ValueError("bash is not available on this machine.")
        return "bash", [executable, "-lc"]

    if requested == "sh":
        executable = shutil.which("sh")
        if not executable:
            raise ValueError("sh is not available on this machine.")
        return "sh", [executable, "-lc"]

    raise ValueError("Unsupported shell. Use one of: auto, powershell, cmd, bash, sh.")


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}\n\n[output truncated, {omitted} characters omitted]"


def _is_broad_recursive_scan(command: str) -> bool:
    normalized = re.sub(r"\s+", " ", command.strip().lower())
    drive_root = r"[a-z]:\\(?:\s|$|['\"]|/)"

    if re.search(rf"get-childitem\s+['\"]?{drive_root}", normalized) and " -recurse" in normalized:
        return True
    if re.search(rf"\bdir\s+['\"]?{drive_root}", normalized) and re.search(r"(^|\s)/(s|a|b)\b", normalized):
        return True
    if re.search(rf"for\s+/d\s+%[a-z]\s+in\s+\(['\"]?[a-z]:\\\*", normalized) and "dir /s" in normalized:
        return True
    return False


def _parse_optional_positive_int(value: int | None, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, minimum)


def _format_todos(todos: list[dict[str, str]]) -> str:
    if not todos:
        return "(empty todo list)"

    labels = {
        "pending": "[ ]",
        "in_progress": "[~]",
        "completed": "[x]",
    }
    return "\n".join(
        f"{index}. {labels[item['status']]} {item['content']} ({item['status']})"
        for index, item in enumerate(todos, start=1)
    )


def _normalize_todos(todos: object) -> list[dict[str, str]]:
    if not isinstance(todos, list):
        raise ValueError("todos must be a list.")

    normalized: list[dict[str, str]] = []
    for index, item in enumerate(todos, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"todo #{index} must be an object.")

        content = str(item.get("content", "")).strip()
        status = str(item.get("status", "")).strip()
        if not content:
            raise ValueError(f"todo #{index} content must not be empty.")
        if status not in TODO_STATUSES:
            raise ValueError(
                f"todo #{index} status must be one of: pending, in_progress, completed."
            )

        normalized.append({"content": content, "status": status})

    return normalized


def _run_process(argv: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _popen_kwargs() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
        return

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_shell_process(
    argv: list[str],
    cwd: str,
    timeout: int,
) -> tuple[int | None, str, str, bool]:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_popen_kwargs(),
    )

    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return process.returncode, stdout, stderr, False
    except subprocess.TimeoutExpired as exc:
        _kill_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.kill()
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            return None, stdout, stderr, True
        stdout = stdout or exc.stdout or ""
        stderr = stderr or exc.stderr or ""
        return None, stdout, stderr, True


def _parse_typeperf_cpu(stdout: str) -> float | None:
    for line in stdout.splitlines():
        if not line.startswith('"') or '","' not in line:
            continue
        value = line.rsplit(",", 1)[-1].strip().strip('"')
        try:
            return float(value)
        except ValueError:
            continue
    return None


def _read_proc_cpu_totals() -> tuple[int, int] | None:
    try:
        line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None

    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None

    values = [int(value) for value in parts[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return idle, total


@tool
def get_system_cpu_usage(sample_seconds: int = 1) -> str:
    """Get the current total CPU usage percentage for the backend host.

    Args:
        sample_seconds: Sampling interval in seconds, clamped to 1..10.
    """
    try:
        sample_seconds = int(sample_seconds)
    except (TypeError, ValueError):
        sample_seconds = 1
    sample_seconds = min(max(sample_seconds, 1), 10)

    if os.name == "nt":
        typeperf = shutil.which("typeperf")
        if typeperf:
            completed = _run_process(
                [typeperf, r"\Processor(_Total)\% Processor Time", "-sc", "1"],
                timeout=sample_seconds + 10,
            )
            cpu = _parse_typeperf_cpu(completed.stdout)
            if cpu is not None:
                return f"当前系统 CPU 占用约为 {cpu:.1f}%。"

        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell:
            completed = _run_process(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    (
                        "Get-Counter '\\Processor(_Total)\\% Processor Time' "
                        f"-SampleInterval {sample_seconds} -MaxSamples 1 | "
                        "Select-Object -ExpandProperty CounterSamples | "
                        "Select-Object -ExpandProperty CookedValue"
                    ),
                ],
                timeout=sample_seconds + 10,
            )
            try:
                cpu = float(completed.stdout.strip().splitlines()[-1])
                return f"当前系统 CPU 占用约为 {cpu:.1f}%。"
            except (IndexError, ValueError):
                pass

        return "Error: unable to read CPU usage with typeperf or PowerShell Get-Counter."

    first = _read_proc_cpu_totals()
    if first:
        time.sleep(sample_seconds)
        second = _read_proc_cpu_totals()
        if second:
            idle_delta = second[0] - first[0]
            total_delta = second[1] - first[1]
            if total_delta > 0:
                cpu = 100 * (1 - idle_delta / total_delta)
                return f"当前系统 CPU 占用约为 {cpu:.1f}%。"

    return "Error: unable to read CPU usage from /proc/stat."


@tool
def todo_write(todos: list[dict[str, str]]) -> str:
    """Replace the current thread's todo list for multi-step work.

    Args:
        todos: Full todo list. Each item needs content and status, where status is
            pending, in_progress, or completed.
    """
    try:
        normalized = _normalize_todos(todos)
    except ValueError as exc:
        return f"Error: {exc}"

    thread_id = CURRENT_TOOL_THREAD_ID.get()
    with THREAD_TODOS_LOCK:
        THREAD_TODOS[thread_id] = normalized

    total = len(normalized)
    completed = sum(1 for item in normalized if item["status"] == "completed")
    in_progress = sum(1 for item in normalized if item["status"] == "in_progress")
    pending = sum(1 for item in normalized if item["status"] == "pending")

    return (
        "Todo list updated.\n"
        f"thread: {thread_id}\n"
        f"summary: {completed} completed, {in_progress} in_progress, {pending} pending, {total} total\n\n"
        f"{_format_todos(normalized)}"
    )


@tool
def load_skill(name: str) -> str:
    """Load the full content of a registered project skill on demand.

    Args:
        name: Skill registry name from the skill catalog.
    """
    return load_skill_content(name)


@tool
def compact(focus: str = "") -> str:
    """Summarize and compact earlier conversation context before continuing.

    Args:
        focus: Optional note about what details the summary should preserve.
    """
    note = f"\nFocus: {focus.strip()}" if focus and focus.strip() else ""
    return f"{MANUAL_COMPACT_MARKER} Conversation compaction will run before the next model step.{note}"


@tool
def task(description: str, cwd: str = "", max_steps: int | None = None) -> str:
    """Launch a synchronous subagent for an isolated complex subtask.

    The subagent uses a fresh conversation context and returns only its final
    conclusion. It cannot launch another subagent.

    Args:
        description: Clear subtask for the subagent to complete.
        cwd: Optional working directory hint for file and shell tools.
        max_steps: Optional maximum subagent reasoning/tool steps.
    """
    from agent_subagent import spawn_subagent

    return spawn_subagent(description=description, cwd=cwd, max_steps=max_steps)


@tool
def read_file(path: str, cwd: str = "", limit: int | None = None) -> str:
    """Read a UTF-8 text file from the working directory.

    Args:
        path: File path to read. Relative paths are resolved under cwd.
        cwd: Optional working directory. Empty means the backend process working directory.
        limit: Optional maximum number of lines to return.
    """
    try:
        root, file_path = _resolve_safe_path(path, cwd)
        if not file_path.exists():
            return f"Error: file does not exist: {_relative_to_root(file_path, root)}"
        if not file_path.is_file():
            return f"Error: path is not a file: {_relative_to_root(file_path, root)}"

        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if limit is not None:
            max_lines = _parse_optional_positive_int(limit, len(lines))
            if len(lines) > max_lines:
                omitted = len(lines) - max_lines
                lines = [*lines[:max_lines], f"... ({omitted} more lines)"]

        return "\n".join(lines)
    except OSError as exc:
        return f"Error: failed to read file: {exc}"
    except ValueError as exc:
        return f"Error: {exc}"


@tool
def write_file(path: str, content: str, cwd: str = "") -> str:
    """Write UTF-8 text to a file inside the working directory.

    Args:
        path: File path to write. Relative paths are resolved under cwd.
        content: Text content to write.
        cwd: Optional working directory. Empty means the backend process working directory.
    """
    try:
        root, file_path = _resolve_safe_path(path, cwd)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content.encode('utf-8'))} bytes to {_relative_to_root(file_path, root)}"
    except OSError as exc:
        return f"Error: failed to write file: {exc}"
    except ValueError as exc:
        return f"Error: {exc}"


@tool
def edit_file(path: str, old_text: str, new_text: str, cwd: str = "") -> str:
    """Replace the first exact text match in a UTF-8 file.

    Args:
        path: File path to edit. Relative paths are resolved under cwd.
        old_text: Exact text to replace once.
        new_text: Replacement text.
        cwd: Optional working directory. Empty means the backend process working directory.
    """
    if old_text == "":
        return "Error: old_text must not be empty."

    try:
        root, file_path = _resolve_safe_path(path, cwd)
        if not file_path.exists():
            return f"Error: file does not exist: {_relative_to_root(file_path, root)}"
        if not file_path.is_file():
            return f"Error: path is not a file: {_relative_to_root(file_path, root)}"

        text = file_path.read_text(encoding="utf-8", errors="replace")
        if old_text not in text:
            return f"Error: text not found in {_relative_to_root(file_path, root)}"

        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {_relative_to_root(file_path, root)}"
    except OSError as exc:
        return f"Error: failed to edit file: {exc}"
    except ValueError as exc:
        return f"Error: {exc}"


@tool("glob")
def glob_files(pattern: str, cwd: str = "", limit: int = 200) -> str:
    """Find files by glob pattern inside the working directory.

    Args:
        pattern: Glob pattern, for example **/*.py or backend/src/*.py.
        cwd: Optional working directory. Empty means the backend process working directory.
        limit: Maximum number of matches to return.
    """
    if not pattern or not pattern.strip():
        return "Error: pattern must not be empty."

    try:
        root = Path(_resolve_cwd(cwd)).resolve()
        max_matches = _parse_optional_positive_int(limit, 200)
        matches: list[str] = []
        for match in glob_module.glob(pattern, root_dir=root, recursive=True):
            resolved = (root / match).resolve()
            if resolved.is_relative_to(root):
                matches.append(_relative_to_root(resolved, root))

        matches = sorted(dict.fromkeys(matches))
        if not matches:
            return "(no matches)"
        if len(matches) > max_matches:
            omitted = len(matches) - max_matches
            matches = [*matches[:max_matches], f"... ({omitted} more matches)"]

        return "\n".join(matches)
    except OSError as exc:
        return f"Error: failed to glob files: {exc}"
    except ValueError as exc:
        return f"Error: {exc}"


@tool
def run_shell_command(
    command: str,
    cwd: str = "",
    shell: str = "auto",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Run a shell command on the backend host and return stdout/stderr.

    Args:
        command: Command text to execute.
        cwd: Optional working directory. Use an empty string for the backend process working directory.
        shell: Shell to use: auto, powershell, cmd, bash, or sh.
        timeout_seconds: Command timeout in seconds, clamped to 1..120.
    """
    if not command or not command.strip():
        return "Error: command must not be empty."

    if _is_broad_recursive_scan(command):
        return (
            "Error: refusing to run a broad recursive scan from a drive root. "
            "Use a narrower path, a dedicated inventory tool, or ask for confirmation first."
        )

    try:
        resolved_cwd = _resolve_cwd(cwd)
        selected_shell, argv_prefix = _select_shell(shell)
    except ValueError as exc:
        return f"Error: {exc}"

    try:
        timeout = int(timeout_seconds)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    timeout = min(max(timeout, 1), MAX_TIMEOUT_SECONDS)

    try:
        max_output_chars = int(os.getenv("SHELL_TOOL_MAX_OUTPUT_CHARS", DEFAULT_MAX_OUTPUT_CHARS))
    except ValueError:
        max_output_chars = DEFAULT_MAX_OUTPUT_CHARS
    max_output_chars = max(max_output_chars, 1000)

    try:
        returncode, stdout, stderr, timed_out = _run_shell_process(
            [*argv_prefix, command],
            cwd=resolved_cwd,
            timeout=timeout,
        )
    except OSError as exc:
        return f"Error: failed to run command: {exc}"

    if timed_out:
        output = (
            f"shell: {selected_shell}\n"
            f"cwd: {resolved_cwd}\n"
            f"timeout_seconds: {timeout}\n"
            "status: timed out; process tree killed\n\n"
            f"stdout:\n{stdout}\n\n"
            f"stderr:\n{stderr}"
        )
        return _truncate(output, max_output_chars)

    output = (
        f"shell: {selected_shell}\n"
        f"cwd: {resolved_cwd}\n"
        f"exit_code: {returncode}\n\n"
        f"stdout:\n{stdout}\n\n"
        f"stderr:\n{stderr}"
    )
    return _truncate(output, max_output_chars)


ALL_TOOLS = [
    get_system_cpu_usage,
    todo_write,
    load_skill,
    compact,
    task,
    read_file,
    write_file,
    edit_file,
    glob_files,
    run_shell_command,
]

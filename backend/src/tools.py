"""Tools for the agent."""

from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import time
import glob as glob_module
import json
import threading
from contextvars import ContextVar
from pathlib import Path

from langchain.tools import tool

from agent_config import config_int, config_str
from agent_context import MANUAL_COMPACT_MARKER
from agent_memory import write_memory_file
from agent_tasks import (
    BACKEND_DIR,
    claim_persistent_task,
    complete_persistent_task,
    create_persistent_task,
    list_persistent_tasks,
    load_persistent_task,
    task_detail_json,
    task_summary_line,
)
from skills import load_skill_content

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 120
DEFAULT_MAX_OUTPUT_CHARS = 12000
BACKGROUND_DIR = BACKEND_DIR / ".agent_outputs" / "background"
SSH_KEY_DIR = BACKEND_DIR / ".agent_outputs" / "ssh_keys"
BACKGROUND_DEFAULT_TIMEOUT_SECONDS = 1800
BACKGROUND_TASKS_LOCK = threading.Lock()
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
        return str(BACKEND_DIR)

    requested = Path(cwd).expanduser()
    resolved = requested if requested.is_absolute() else BACKEND_DIR / requested
    if not resolved.exists():
        raise ValueError(f"Working directory does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Working directory is not a directory: {resolved}")
    return str(resolved.absolute())


def _resolve_safe_path(path: str, cwd: str = "") -> tuple[Path, Path]:
    if not path or not path.strip():
        raise ValueError("Path must not be empty.")

    root = Path(_resolve_cwd(cwd))
    requested = Path(path).expanduser()
    resolved = requested if requested.is_absolute() else root / requested
    resolved = resolved.absolute()

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


def _shell_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


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
        env=_shell_env(),
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


def _run_shell_process_streaming(
    argv: list[str],
    cwd: str,
    timeout: int,
    log_path: Path,
) -> tuple[int | None, bool]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=_shell_env(),
        **_popen_kwargs(),
    )
    started_at = time.time()
    timed_out = False

    with log_path.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(f"[agent background] pid={process.pid}\n")
        handle.flush()
        while True:
            if process.stdout is not None:
                line = process.stdout.readline()
                if line:
                    handle.write(line)
                    handle.flush()
                    continue

            returncode = process.poll()
            if returncode is not None:
                if process.stdout is not None:
                    rest = process.stdout.read()
                    if rest:
                        handle.write(rest)
                handle.flush()
                return returncode, timed_out

            if time.time() - started_at > timeout:
                timed_out = True
                handle.write(f"\n[agent background] timeout after {timeout}s; killing process tree\n")
                handle.flush()
                _kill_process_tree(process)
                return None, timed_out

            time.sleep(0.2)


def _background_id() -> str:
    return f"bg_{int(time.time())}_{os.getpid()}_{threading.get_ident()}_{time.time_ns() % 100000:05d}"


def _background_meta_path(task_id: str) -> Path:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(task_id or "").strip())
    if not safe_id:
        raise ValueError("background task id must not be empty.")
    return BACKGROUND_DIR / f"{safe_id}.json"


def _background_log_path(task_id: str) -> Path:
    return _background_meta_path(task_id).with_suffix(".log")


def _write_background_meta(task_id: str, data: dict[str, object]) -> None:
    BACKGROUND_DIR.mkdir(parents=True, exist_ok=True)
    path = _background_meta_path(task_id)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_background_meta(task_id: str) -> dict[str, object]:
    path = _background_meta_path(task_id)
    if not path.exists():
        raise FileNotFoundError(f"Background task not found: {task_id}")
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def _background_timeout(timeout_seconds: int | None) -> int:
    try:
        timeout = int(timeout_seconds) if timeout_seconds is not None else BACKGROUND_DEFAULT_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        timeout = BACKGROUND_DEFAULT_TIMEOUT_SECONDS
    try:
        configured_max = int(os.getenv("SHELL_BACKGROUND_MAX_TIMEOUT_SECONDS", BACKGROUND_DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        configured_max = BACKGROUND_DEFAULT_TIMEOUT_SECONDS
    max_timeout = _parse_optional_positive_int(
        configured_max,
        BACKGROUND_DEFAULT_TIMEOUT_SECONDS,
    )
    return min(max(timeout, 1), max_timeout)


def _start_background_shell_command(
    *,
    command: str,
    cwd: str,
    shell: str,
    timeout_seconds: int,
    max_output_chars: int,
) -> str:
    task_id = _background_id()
    log_path = _background_log_path(task_id)
    started_at = time.time()
    meta: dict[str, object] = {
        "id": task_id,
        "type": "shell",
        "status": "running",
        "command": command,
        "cwd": cwd,
        "shell": shell,
        "timeout_seconds": timeout_seconds,
        "started_at": started_at,
        "finished_at": None,
        "exit_code": None,
        "timed_out": False,
        "log_path": str(log_path),
        "error": "",
    }
    _write_background_meta(task_id, meta)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "Background shell task started.\n"
        f"id: {task_id}\n"
        f"cwd: {cwd}\n"
        f"command: {command}\n"
        f"timeout_seconds: {timeout_seconds}\n"
        f"started_at: {started_at}\n\n"
        "output:\n",
        encoding="utf-8",
        errors="replace",
    )

    def worker() -> None:
        try:
            selected_shell, argv_prefix = _select_shell(shell)
            meta["shell"] = selected_shell
            with BACKGROUND_TASKS_LOCK:
                _write_background_meta(task_id, meta)
            returncode, timed_out = _run_shell_process_streaming(
                [*argv_prefix, command],
                cwd=cwd,
                timeout=timeout_seconds,
                log_path=log_path,
            )
            with log_path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(
                    "\n[agent background] finished\n"
                    f"exit_code: {returncode}\n"
                    f"timed_out: {timed_out}\n"
                    f"finished_at: {time.time()}\n"
                )
            meta.update(
                {
                    "status": "timed_out" if timed_out else "completed",
                    "finished_at": time.time(),
                    "exit_code": returncode,
                    "timed_out": timed_out,
                    "shell": selected_shell,
                }
            )
        except Exception as exc:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(f"\n[agent background] Error: {exc}\n")
            meta.update({"status": "failed", "finished_at": time.time(), "error": str(exc)})
        with BACKGROUND_TASKS_LOCK:
            _write_background_meta(task_id, meta)

    thread = threading.Thread(target=worker, name=f"shell-bg-{task_id}", daemon=True)
    thread.start()
    return (
        f"Background shell task started.\n"
        f"id: {task_id}\n"
        f"status: running\n"
        f"cwd: {cwd}\n"
        f"timeout_seconds: {timeout_seconds}\n"
        f"log_path: {log_path}\n"
        f"Use list_background_tasks or get_background_task('{task_id}') to check progress."
    )


def _format_background_meta(meta: dict[str, object], include_output: bool = False, max_output_chars: int = 8000) -> str:
    lines = [
        f"id: {meta.get('id')}",
        f"status: {meta.get('status')}",
        f"command: {meta.get('command')}",
        f"cwd: {meta.get('cwd')}",
        f"exit_code: {meta.get('exit_code')}",
        f"timed_out: {meta.get('timed_out')}",
        f"log_path: {meta.get('log_path')}",
    ]
    error = str(meta.get("error") or "")
    if error:
        lines.append(f"error: {error}")

    if include_output:
        log_path = Path(str(meta.get("log_path") or ""))
        if log_path.exists():
            output = log_path.read_text(encoding="utf-8", errors="replace")
            lines.extend(["", "output:", _truncate(output, max_output_chars)])
        else:
            lines.extend(["", "output: (not available yet)"])
    return "\n".join(lines)


def _ssh_config_value(explicit: str, key: str, default: str = "") -> str:
    value = str(explicit or "").strip()
    return value if value else config_str("ssh", key, default)


def _ssh_port(explicit: int | None) -> int:
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    return config_int("ssh", "port", 22)


def _materialized_ssh_key(explicit_key_file: str = "") -> str:
    key_file = _ssh_config_value(explicit_key_file, "keyFile")
    if key_file:
        return key_file

    key_text = config_str("ssh", "privateKey")
    if not key_text:
        return ""

    SSH_KEY_DIR.mkdir(parents=True, exist_ok=True)
    path = SSH_KEY_DIR / "default_ssh_key"
    normalized = key_text.replace("\r\n", "\n").strip()
    path.write_text(f"{normalized}\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return str(path)


def _ssh_target(user: str, host: str) -> str:
    return f"{user}@{host}" if user else host


def _ssh_extra_args() -> list[str]:
    raw = config_str("ssh", "extraArgs")
    if not raw:
        return []
    return [part for part in raw.split() if part.strip()]


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
    """Placeholder for future conversation context compaction.

    Args:
        focus: Optional note about what details a future compaction should preserve.
    """
    note = f"\nFocus: {focus.strip()}" if focus and focus.strip() else ""
    return (
        f"{MANUAL_COMPACT_MARKER} Context compaction is currently disabled; "
        f"no messages were summarized or removed.{note}"
    )


@tool
def remember(name: str, type: str, description: str, body: str) -> str:
    """Persist a durable memory for future conversations.

    Args:
        name: Short memory name, for example user-preference-tabs.
        type: One of user, feedback, project, or reference.
        description: One-line summary used for memory lookup.
        body: Full markdown detail explaining what to remember and how to apply it.
    """
    return write_memory_file(name=name, mem_type=type, description=description, body=body)


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
def create_task(subject: str, description: str = "", blockedBy: list[str] | None = None) -> str:
    """Create a persistent task with optional blockedBy dependencies.

    Args:
        subject: Short task title.
        description: Detailed task description for later recovery.
        blockedBy: Optional list of task IDs that must be completed first.
    """
    try:
        task_item = create_persistent_task(subject=subject, description=description, blocked_by=blockedBy)
        return f"Created task.\n{task_summary_line(task_item)}"
    except ValueError as exc:
        return f"Error: {exc}"


@tool
def list_tasks() -> str:
    """List persistent tasks with status, owner, and dependencies."""
    tasks = list_persistent_tasks()
    if not tasks:
        return "No persistent tasks. Use create_task to add one."
    return "\n".join(task_summary_line(task_item) for task_item in tasks)


@tool
def get_task(task_id: str) -> str:
    """Get full JSON details for a persistent task.

    Args:
        task_id: Task ID returned by create_task or list_tasks.
    """
    try:
        return task_detail_json(load_persistent_task(task_id))
    except (FileNotFoundError, ValueError) as exc:
        return f"Error: {exc}"


@tool
def claim_task(task_id: str, owner: str = "agent") -> str:
    """Claim an unblocked pending persistent task and mark it in_progress.

    Args:
        task_id: Task ID to claim.
        owner: Agent or worker name claiming the task.
    """
    try:
        task_item = claim_persistent_task(task_id, owner=owner)
        return f"Claimed task.\n{task_summary_line(task_item)}"
    except (FileNotFoundError, ValueError) as exc:
        return f"Error: {exc}"


@tool
def complete_task(task_id: str) -> str:
    """Mark an in_progress persistent task completed and report newly unblocked tasks.

    Args:
        task_id: Task ID to complete.
    """
    try:
        task_item, unblocked = complete_persistent_task(task_id)
    except (FileNotFoundError, ValueError) as exc:
        return f"Error: {exc}"

    output = [f"Completed task.\n{task_summary_line(task_item)}"]
    if unblocked:
        output.append("Unblocked tasks:")
        output.extend(task_summary_line(item) for item in unblocked)
    return "\n".join(output)


def _read_file_impl(path: str, cwd: str = "", limit: int | None = None) -> str:
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
def read_file(path: str, cwd: str = "", limit: int | None = None) -> str:
    """Read a UTF-8 text file from the working directory.

    Args:
        path: File path to read. Relative paths are resolved under cwd.
        cwd: Optional working directory. Empty means the backend process working directory.
        limit: Optional maximum number of lines to return.
    """
    return _read_file_impl(path, cwd, limit)


def _write_file_impl(path: str, content: str, cwd: str = "") -> str:
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
def write_file(path: str, content: str, cwd: str = "") -> str:
    """Write UTF-8 text to a file inside the working directory.

    Args:
        path: File path to write. Relative paths are resolved under cwd.
        content: Text content to write.
        cwd: Optional working directory. Empty means the backend process working directory.
    """
    return _write_file_impl(path, content, cwd)


def _edit_file_impl(path: str, old_text: str, new_text: str, cwd: str = "") -> str:
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


@tool
def edit_file(path: str, old_text: str, new_text: str, cwd: str = "") -> str:
    """Replace the first exact text match in a UTF-8 file.

    Args:
        path: File path to edit. Relative paths are resolved under cwd.
        old_text: Exact text to replace once.
        new_text: Replacement text.
        cwd: Optional working directory. Empty means the backend process working directory.
    """
    return _edit_file_impl(path, old_text, new_text, cwd)


def _glob_files_impl(pattern: str, cwd: str = "", limit: int = 200) -> str:
    if not pattern or not pattern.strip():
        return "Error: pattern must not be empty."

    try:
        root = Path(_resolve_cwd(cwd))
        max_matches = _parse_optional_positive_int(limit, 200)
        matches: list[str] = []
        for match in glob_module.glob(pattern, root_dir=root, recursive=True):
            resolved = (root / match).absolute()
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


@tool("glob")
def glob_files(pattern: str, cwd: str = "", limit: int = 200) -> str:
    """Find files by glob pattern inside the working directory.

    Args:
        pattern: Glob pattern, for example **/*.py or backend/src/*.py.
        cwd: Optional working directory. Empty means the backend process working directory.
        limit: Maximum number of matches to return.
    """
    return _glob_files_impl(pattern, cwd, limit)


@tool
def run_shell_command(
    command: str,
    cwd: str = "",
    shell: str = "auto",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    run_in_background: bool = False,
) -> str:
    """Run a shell command on the backend host and return stdout/stderr.

    Args:
        command: Command text to execute.
        cwd: Optional working directory. Use an empty string for the backend process working directory.
        shell: Shell to use: auto, powershell, cmd, bash, or sh.
        timeout_seconds: Command timeout in seconds, clamped to 1..120.
        run_in_background: If true, start the command in a background worker and return a background task ID.
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

    if run_in_background:
        return _start_background_shell_command(
            command=command,
            cwd=resolved_cwd,
            shell=shell,
            timeout_seconds=_background_timeout(timeout_seconds),
            max_output_chars=max_output_chars,
        )

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


@tool
def run_ssh_command(
    command: str,
    host: str = "",
    user: str = "",
    port: int | None = None,
    key_file: str = "",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Run a command on a remote host through the system ssh client.

    Args:
        command: Remote command text to execute.
        host: SSH host. Empty uses the saved SSH host from configuration.
        user: SSH username. Empty uses the saved SSH username from configuration.
        port: SSH port. Empty uses the saved SSH port from configuration.
        key_file: Private key file path. Empty uses configured keyFile or privateKey.
        timeout_seconds: Command timeout in seconds, clamped to 1..120.
    """
    if not command or not command.strip():
        return "Error: command must not be empty."

    resolved_host = _ssh_config_value(host, "host")
    resolved_user = _ssh_config_value(user, "user")
    resolved_port = _ssh_port(port)
    resolved_key = _materialized_ssh_key(key_file)
    if not resolved_host:
        return "Error: SSH host is required. Set it in configuration or pass host."

    ssh = shutil.which("ssh")
    if not ssh:
        return "Error: ssh client is not available on this machine."

    try:
        timeout = int(timeout_seconds)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    timeout = min(max(timeout, 1), MAX_TIMEOUT_SECONDS)

    argv = [
        ssh,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-p",
        str(resolved_port),
    ]
    if resolved_key:
        argv.extend(["-i", resolved_key])
    argv.extend(_ssh_extra_args())
    argv.extend([_ssh_target(resolved_user, resolved_host), command])

    try:
        completed = _run_process(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        return (
            f"ssh_host: {resolved_host}\n"
            f"ssh_user: {resolved_user or '(default)'}\n"
            f"timeout_seconds: {timeout}\n"
            "status: timed out"
        )
    except OSError as exc:
        return f"Error: failed to run ssh: {exc}"

    try:
        max_output_chars = int(os.getenv("SHELL_TOOL_MAX_OUTPUT_CHARS", DEFAULT_MAX_OUTPUT_CHARS))
    except ValueError:
        max_output_chars = DEFAULT_MAX_OUTPUT_CHARS
    output = (
        f"ssh_host: {resolved_host}\n"
        f"ssh_user: {resolved_user or '(default)'}\n"
        f"ssh_port: {resolved_port}\n"
        f"exit_code: {completed.returncode}\n\n"
        f"stdout:\n{completed.stdout}\n\n"
        f"stderr:\n{completed.stderr}"
    )
    return _truncate(output, max(max_output_chars, 1000))


@tool
def list_background_tasks(limit: int = 20) -> str:
    """List recent background shell tasks.

    Args:
        limit: Maximum number of background tasks to show.
    """
    try:
        max_items = _parse_optional_positive_int(limit, 20)
        if not BACKGROUND_DIR.exists():
            return "No background tasks."
        metas: list[dict[str, object]] = []
        for path in sorted(BACKGROUND_DIR.glob("bg_*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                metas.append(json.loads(path.read_text(encoding="utf-8", errors="replace")))
            except (OSError, json.JSONDecodeError):
                continue
            if len(metas) >= max_items:
                break
        if not metas:
            return "No background tasks."
        return "\n\n".join(_format_background_meta(meta, include_output=False) for meta in metas)
    except OSError as exc:
        return f"Error: failed to list background tasks: {exc}"


@tool
def get_background_task(task_id: str, include_output: bool = True, max_output_chars: int = 12000) -> str:
    """Get status and optional output for one background shell task.

    Args:
        task_id: Background task ID returned by run_shell_command.
        include_output: Whether to include captured stdout/stderr from the log file.
        max_output_chars: Maximum output characters to return.
    """
    try:
        max_chars = _parse_optional_positive_int(max_output_chars, 12000, minimum=1000)
        meta = _read_background_meta(task_id)
        return _format_background_meta(meta, include_output=include_output, max_output_chars=max_chars)
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as exc:
        return f"Error: {exc}"


ALL_TOOLS = [
    get_system_cpu_usage,
    todo_write,
    load_skill,
    compact,
    remember,
    # Temporarily disabled: synchronous subagents are hard to debug when their
    # internal tools trigger interrupts/approval flows. Keep the implementation
    # for a later, explicit subagent lifecycle.
    # task,
    create_task,
    list_tasks,
    get_task,
    claim_task,
    complete_task,
    read_file,
    write_file,
    edit_file,
    glob_files,
    run_shell_command,
    run_ssh_command,
    list_background_tasks,
    get_background_task,
]

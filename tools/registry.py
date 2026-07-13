"""Tools for the agent."""

from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import time
import glob as glob_module
import fnmatch
import json
import io
import posixpath
import socket
import threading
from contextvars import ContextVar
from pathlib import Path

from langchain.tools import tool

from agent.cron import cancel_job as cancel_cron_job
from agent.cron import is_cron_enabled
from agent.cron import list_jobs as list_cron_jobs
from agent.cron import schedule_job as schedule_cron_job
from agent.config import config_int, config_str, ssh_host_entry
from agent.context import MANUAL_COMPACT_MARKER
from agent.memory import write_memory_file
from agent.tasks import (
    ROOT_DIR,
    claim_persistent_task,
    complete_persistent_task,
    create_persistent_task,
    list_persistent_tasks,
    load_persistent_task,
    task_detail_json,
    task_summary_line,
)
from agent.skills import load_skill_content
from tools.web import web_extract, web_search

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 120
DEFAULT_MAX_OUTPUT_CHARS = 12000
BACKGROUND_DIR = ROOT_DIR / ".agent_outputs" / "background"
WRITE_FILES_DIR = ROOT_DIR / ".agent_outputs" / "files"
SHELL_CWD_DIR = ROOT_DIR / ".agent_outputs" / "shell"
BACKGROUND_DEFAULT_TIMEOUT_SECONDS = 1800
WRITE_FILE_OUTPUT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
BACKGROUND_TASKS_LOCK = threading.Lock()
BACKGROUND_PROCESSES: dict[str, subprocess.Popen[str]] = {}
BACKGROUND_CANCEL_REQUESTS: set[str] = set()
WRITE_FILE_CLEANUP_LOCK = threading.Lock()
WRITE_FILE_LAST_CLEANUP = 0.0
TODO_STATUSES = {"pending", "in_progress", "completed"}
DEFAULT_TODO_THREAD_ID = "__default__"
CURRENT_TOOL_THREAD_ID: ContextVar[str] = ContextVar(
    "CURRENT_TOOL_THREAD_ID",
    default=DEFAULT_TODO_THREAD_ID,
)
THREAD_TODOS: dict[str, list[dict[str, str]]] = {}
THREAD_TODOS_LOCK = threading.Lock()


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def set_current_tool_thread_id(thread_id: str | None):
    """Set the current tool thread id for tools that keep per-thread state."""
    return CURRENT_TOOL_THREAD_ID.set(thread_id or DEFAULT_TODO_THREAD_ID)


def reset_current_tool_thread_id(token) -> None:
    """Restore the previous tool thread id after a tool call completes."""
    CURRENT_TOOL_THREAD_ID.reset(token)


def _resolve_cwd(cwd: str) -> str:
    if not cwd:
        return str(ROOT_DIR)

    requested = Path(cwd).expanduser()
    resolved = requested if requested.is_absolute() else ROOT_DIR / requested
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


def _safe_storage_id(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "").strip())
    return safe.strip("._-") or DEFAULT_TODO_THREAD_ID


def _write_file_thread_dir() -> Path:
    return WRITE_FILES_DIR / _safe_storage_id(CURRENT_TOOL_THREAD_ID.get())


def _shell_thread_dir() -> Path:
    return SHELL_CWD_DIR / _safe_storage_id(CURRENT_TOOL_THREAD_ID.get())


def _write_file_max_age_seconds() -> int:
    try:
        configured = int(os.getenv("AGENT_WRITE_FILE_OUTPUT_MAX_AGE_SECONDS", WRITE_FILE_OUTPUT_MAX_AGE_SECONDS))
    except (TypeError, ValueError):
        configured = WRITE_FILE_OUTPUT_MAX_AGE_SECONDS
    return max(configured, 0)


def _cleanup_old_write_file_outputs() -> None:
    global WRITE_FILE_LAST_CLEANUP

    max_age = _write_file_max_age_seconds()
    if max_age <= 0:
        return

    now = time.time()
    if now - WRITE_FILE_LAST_CLEANUP < 60 * 60:
        return

    with WRITE_FILE_CLEANUP_LOCK:
        if now - WRITE_FILE_LAST_CLEANUP < 60 * 60:
            return
        WRITE_FILE_LAST_CLEANUP = now

        if not WRITE_FILES_DIR.exists():
            return

        cutoff = now - max_age
        for file_path in WRITE_FILES_DIR.rglob("*"):
            try:
                if file_path.is_file() and file_path.stat().st_mtime < cutoff:
                    file_path.unlink()
            except OSError:
                continue

        for dir_path in sorted(
            (item for item in WRITE_FILES_DIR.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            try:
                dir_path.rmdir()
            except OSError:
                continue


def _resolve_write_file_output_path(path: str) -> tuple[Path, Path]:
    if not path or not path.strip():
        raise ValueError("Path must not be empty.")

    requested = Path(path).expanduser()
    if requested.is_absolute():
        raise ValueError("write_file only accepts relative paths inside the agent output directory.")

    root = _write_file_thread_dir().resolve(strict=False)
    resolved = (root / requested).resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("Path escapes the agent output directory.")

    return root, resolved


def _resolve_shell_cwd(cwd: str) -> str:
    if cwd:
        return _resolve_cwd(cwd)

    shell_cwd = _shell_thread_dir().resolve(strict=False)
    shell_cwd.mkdir(parents=True, exist_ok=True)
    return str(shell_cwd)


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
        # 修复 PowerShell 子进程输出乱码问题：Windows 默认使用 GBK 编码，导致中文输出
        # 在 subprocess 中显示为乱码。通过在每条命令前注入 UTF-8 编码设置，确保输出
        # 以 UTF-8 编码返回给 Python（subprocess 已配置 encoding="utf-8"）。
        # chcp 65001 切换控制台代码页为 UTF-8，$OutputEncoding 和
        # [Console]::OutputEncoding 确保 PowerShell 管道输出也使用 UTF-8。
        utf8_preamble = (
            "chcp 65001 | Out-Null; "
            "$OutputEncoding=[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        )
        return "powershell", [executable, "-NoProfile", "-NonInteractive", "-Command", utf8_preamble]

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


def _kill_process_tree_by_pid(pid: int) -> bool:
    if pid <= 0:
        return False

    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            return completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    try:
        os.killpg(pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except OSError:
            return False


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
    task_id: str,
    meta: dict[str, object],
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
    with BACKGROUND_TASKS_LOCK:
        BACKGROUND_PROCESSES[task_id] = process
        meta["pid"] = process.pid
        _write_background_meta(task_id, meta)
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
        "pid": None,
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
                task_id=task_id,
                meta=meta,
            )
            with BACKGROUND_TASKS_LOCK:
                cancelled = task_id in BACKGROUND_CANCEL_REQUESTS
                if not cancelled:
                    try:
                        current_meta = _read_background_meta(task_id)
                    except (FileNotFoundError, OSError, json.JSONDecodeError):
                        current_meta = {}
                    cancelled = current_meta.get("status") == "cancel_requested"
            with log_path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(
                    "\n[agent background] finished\n"
                    f"exit_code: {returncode}\n"
                    f"timed_out: {timed_out}\n"
                    f"cancelled: {cancelled}\n"
                    f"finished_at: {time.time()}\n"
                )
            meta.update(
                {
                    "status": "cancelled" if cancelled else "timed_out" if timed_out else "completed",
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
            BACKGROUND_PROCESSES.pop(task_id, None)
            BACKGROUND_CANCEL_REQUESTS.discard(task_id)
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


def _ssh_config_value(explicit: str, key: str, target_host: str = "", default: str = "") -> str:
    value = str(explicit or "").strip()
    if value:
        return value
    entry = ssh_host_entry(target_host)
    entry_val = entry.get(key)
    if entry_val is not None:
        return str(entry_val).strip()
    return default


def _ssh_port(explicit: int | None, target_host: str = "") -> int:
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    entry = ssh_host_entry(target_host)
    try:
        return int(entry.get("port", 22))
    except (TypeError, ValueError):
        return 22


def _ssh_password(explicit_password: str = "", target_host: str = "") -> str:
    value = str(explicit_password or "").strip()
    if value:
        return value
    entry = ssh_host_entry(target_host)
    return str(entry.get("password", "") or "").strip()


def _ssh_private_key_text(target_host: str = "") -> str:
    """Return the raw private key text from configuration (not a file path)."""
    entry = ssh_host_entry(target_host)
    return str(entry.get("privateKey", "") or "").strip()


def _resolve_ssh_connection_options(
    *,
    host: str = "",
    user: str = "",
    port: int | None = None,
    key_file: str = "",
    password: str = "",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[str, str, int, str, str, str, int]:
    """统一解析 SSH 连接参数。

    这里把 host/user/port/key/password/timeout 的解析收敛成一个入口，
    这样命令执行、文件上传、后续可能的文件下载都能复用同一套规则，
    避免不同 SSH 工具对同一份配置解释不一致。
    """

    resolved_host = _ssh_config_value(host, "host")
    if not resolved_host:
        raise ValueError("SSH host is required. Set it in configuration or pass host.")

    resolved_user = _ssh_config_value(user, "user", target_host=resolved_host) or "root"
    resolved_port = _ssh_port(port, target_host=resolved_host)

    # keyFile 路径优先；如果没有，再尝试读取配置里的 privateKey 文本。
    resolved_key_file = _ssh_config_value(key_file, "keyFile", resolved_host)
    resolved_key_text = "" if resolved_key_file else _ssh_private_key_text(target_host=resolved_host)
    resolved_password = _ssh_password(password, target_host=resolved_host)

    try:
        timeout = int(timeout_seconds)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    timeout = min(max(timeout, 1), MAX_TIMEOUT_SECONDS)

    return (
        resolved_host,
        resolved_user,
        resolved_port,
        resolved_key_file,
        resolved_key_text,
        resolved_password,
        timeout,
    )


def _paramiko_connect(
    host: str,
    port: int,
    user: str,
    password: str = "",
    key_file: str = "",
    key_text: str = "",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> "paramiko.SSHClient":
    """Create and return a connected paramiko SSHClient."""
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    pkey = None
    if key_text:
        normalized = key_text.replace("\r\n", "\n").strip()
        for key_class in (paramiko.RSAKey, paramiko.Ed25519Key):
            try:
                pkey = key_class.from_private_key(io.StringIO(normalized))
                break
            except (paramiko.SSHException, ValueError):
                continue

    connect_kwargs: dict = {
        "hostname": host,
        "port": port,
        "username": user,
        "timeout": timeout,
    }
    if password:
        connect_kwargs["password"] = password
    if key_file:
        connect_kwargs["key_filename"] = key_file
    if pkey:
        connect_kwargs["pkey"] = pkey

    client.connect(**connect_kwargs)
    return client


def _ensure_remote_directory(
    sftp: "paramiko.SFTPClient",
    remote_dir: str,
) -> None:
    """递归创建远端目录。

    SFTP 没有像 `mkdir -p` 那样的现成语义，所以这里按路径层级逐段检查。
    远端统一按 POSIX 路径处理，避免在 Windows 后端上误用本地分隔符。
    """

    normalized = posixpath.normpath(str(remote_dir or "").strip())
    if not normalized or normalized == ".":
        return

    parts = [part for part in normalized.split("/") if part and part != "."]
    current = "/" if normalized.startswith("/") else ""
    for part in parts:
        current = posixpath.join(current, part) if current else part
        try:
            sftp.stat(current)
        except OSError:
            sftp.mkdir(current)


def _local_output_path(local_path: str, cwd: str = "") -> Path:
    """解析下载目标本地路径，并复用现有工作目录边界约束。"""

    _, resolved_local_path = _resolve_safe_path(local_path, cwd)
    return resolved_local_path


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
    """Tell the user how to trigger manual conversation context compaction.

    Args:
        focus: Optional note about what details a future compaction should preserve.
    """
    note = f"\nFocus: {focus.strip()}" if focus and focus.strip() else ""
    return (
        f"{MANUAL_COMPACT_MARKER} Manual context compaction is available as a slash command. "
        f"Ask the user to send `/compact` directly; optional focus text can follow the command.{note}"
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
def rag_rebuild_index(data_dir: str = "") -> str:
    """Rebuild the local llama-index RAG index from documents.

    Args:
        data_dir: Optional document directory. Defaults to RAG_DOCS_DIR or data/rag_docs.
    """
    from agent.rag import rag_rebuild_index as rebuild_index

    return rebuild_index(data_dir=data_dir)


@tool
def rag_search(query: str, top_k: int = 5) -> str:
    """Search the local llama-index RAG knowledge base and return ranked passages.

    Args:
        query: Search question or keywords.
        top_k: Number of retrieved chunks to return.
    """
    from agent.rag import rag_search as search_index

    return search_index(query=query, top_k=top_k)


@tool
def run_subagent(description: str, cwd: str = "", max_steps: int | None = None) -> str:
    """Launch a synchronous subagent for an isolated complex subtask.

    The subagent uses a fresh conversation context and returns only its final
    conclusion. It cannot launch another subagent.

    Args:
        description: Clear subtask for the subagent to complete.
        cwd: Optional working directory hint for file and shell tools.
        max_steps: Optional maximum subagent reasoning/tool steps.
    """
    from agent.subagent import spawn_subagent

    return spawn_subagent(description=description, cwd=cwd, max_steps=max_steps)


@tool
def delegate_task(
    goal: str = "",
    context: str = "",
    tasks: list[dict] | None = None,
    cwd: str = "",
    mode: str = "readonly",
    max_steps: int | None = None,
) -> str:
    """Delegate one or more focused subtasks to isolated leaf subagents.

    Use this for reasoning-heavy subtasks, code review, file investigation,
    research synthesis, or parallel independent workstreams. Subagents do not
    inherit the parent conversation; pass all relevant file paths, constraints,
    errors, and output language requirements in goal/context.

    Args:
        goal: Single subtask goal. Ignored when tasks is provided.
        context: Background information shared with the subagent(s).
        tasks: Optional list of task objects with goal, context, and mode.
        cwd: Optional working directory hint for file and shell tools.
        mode: readonly or workspace_write. workspace_write enables edit_file.
        max_steps: Optional maximum subagent reasoning/tool steps.
    """
    from agent.subagent import delegate_task as run_delegate_task

    return run_delegate_task(goal=goal, context=context, tasks=tasks, cwd=cwd, mode=mode, max_steps=max_steps)


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


@tool
def schedule_cron(cron: str, prompt: str, recurring: bool = True, durable: bool = True) -> str:
    """Schedule a prompt to run automatically in the current thread.

    Args:
        cron: Five-field cron expression: minute hour day-of-month month day-of-week.
        prompt: Prompt to inject into this same thread when the cron fires.
        recurring: True for recurring jobs, false for one-shot jobs.
        durable: True to persist the job across backend restarts.
    """
    if not is_cron_enabled():
        return "Error: cron scheduler is disabled. Set AGENT_CRON_ENABLED=true in .env and restart the agent."

    thread_id = CURRENT_TOOL_THREAD_ID.get()
    result = schedule_cron_job(
        thread_id=thread_id,
        cron=cron,
        prompt=prompt,
        recurring=recurring,
        durable=durable,
    )
    if isinstance(result, str):
        return f"Error: {result}"
    return (
        "Scheduled cron job.\n"
        f"id: {result.id}\n"
        f"thread: {result.thread_id}\n"
        f"cron: {result.cron}\n"
        f"recurring: {result.recurring}\n"
        f"durable: {result.durable}\n"
        f"prompt: {result.prompt}"
    )


@tool
def list_crons(current_thread_only: bool = True) -> str:
    """List scheduled cron jobs.

    Args:
        current_thread_only: If true, only list jobs bound to the current thread.
    """
    scheduler_note = ""
    if not is_cron_enabled():
        scheduler_note = (
            "Warning: cron scheduler is disabled. Set AGENT_CRON_ENABLED=true "
            "in .env and restart the agent.\n\n"
        )

    thread_id = CURRENT_TOOL_THREAD_ID.get() if current_thread_only else None
    jobs = list_cron_jobs(thread_id=thread_id)
    if not jobs:
        return f"{scheduler_note}No scheduled cron jobs."
    lines = []
    for job in jobs:
        status = "enabled" if job.enabled else "disabled"
        recurrence = "recurring" if job.recurring else "one-shot"
        durability = "durable" if job.durable else "session"
        lines.append(
            f"{job.id}: '{job.cron}' [{status}, {recurrence}, {durability}]\n"
            f"  thread: {job.thread_id}\n"
            f"  last_fired_at: {job.last_fired_at or '(never)'}\n"
            f"  prompt: {job.prompt}"
        )
    body = "\n\n".join(lines)
    return f"{scheduler_note}{body}"


@tool
def cancel_cron(job_id: str) -> str:
    """Cancel a scheduled cron job.

    Args:
        job_id: Cron job ID returned by schedule_cron or list_crons.
    """
    result = cancel_cron_job(job_id)
    if result.startswith("job not found") or result.endswith("is required"):
        return f"Error: {result}"
    return result


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
        _cleanup_old_write_file_outputs()
        root, file_path = _resolve_write_file_output_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return (
            f"Wrote {len(content.encode('utf-8'))} bytes to {_relative_to_root(file_path, root)}\n"
            f"root: {root}\n"
            f"path: {file_path}"
        )
    except OSError as exc:
        return f"Error: failed to write file: {exc}"
    except ValueError as exc:
        return f"Error: {exc}"


@tool
def write_file(path: str, content: str, cwd: str = "") -> str:
    """Write UTF-8 text to the agent output directory for the current thread.

    Args:
        path: Relative output path. Files are stored under .agent_outputs/files/{thread_id}.
        content: Text content to write.
        cwd: Deprecated compatibility parameter. It is ignored; write_file never writes project files.
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
        pattern: Glob pattern, for example **/*.py or agent/*.py.
        cwd: Optional working directory. Empty means the backend process working directory.
        limit: Maximum number of matches to return.
    """
    return _glob_files_impl(pattern, cwd, limit)


SEARCH_SKIP_DIRS = {
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
    ".pytest_cache",
}
SEARCH_TEXT_EXTENSIONS = {
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
SEARCH_MODES = {"content", "files_with_matches", "count"}


def _matches_any_glob(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    for pattern in patterns:
        normalized_pattern = pattern.replace("\\", "/")
        if fnmatch.fnmatch(normalized, normalized_pattern):
            return True
        if normalized_pattern.startswith("**/") and fnmatch.fnmatch(normalized, normalized_pattern[3:]):
            return True
    return False


def _split_globs(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _looks_like_text_file(path: Path) -> bool:
    if path.suffix.lower() in SEARCH_TEXT_EXTENSIONS:
        return True
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"\x00" not in chunk


def _compile_search_pattern(query: str, regex: bool, case_sensitive: bool) -> re.Pattern[str]:
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = query if regex else re.escape(query)
    return re.compile(pattern, flags)


def _search_files_impl(
    query: str,
    cwd: str = "",
    include: str = "**/*",
    exclude: str = "",
    regex: bool = False,
    case_sensitive: bool = False,
    context_lines: int = 2,
    max_results: int = 100,
    mode: str = "content",
) -> str:
    if not query:
        return "Error: query must not be empty."

    requested_mode = (mode or "content").strip().lower()
    if requested_mode not in SEARCH_MODES:
        return "Error: mode must be one of: content, files_with_matches, count."

    include_patterns = _split_globs(include) or ["**/*"]
    exclude_patterns = _split_globs(exclude)
    before_after = min(_parse_optional_positive_int(context_lines, 2, minimum=0), 20)
    limit = min(_parse_optional_positive_int(max_results, 100), 1000)

    try:
        root = Path(_resolve_cwd(cwd))
        matcher = _compile_search_pattern(query, regex, case_sensitive)
    except re.error as exc:
        return f"Error: invalid regex: {exc}"
    except ValueError as exc:
        return f"Error: {exc}"

    files_with_matches: list[str] = []
    content_blocks: list[str] = []
    total_matches = 0
    searched_files = 0
    omitted_matches = 0

    try:
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if name not in SEARCH_SKIP_DIRS)
            current_path = Path(current)
            for filename in sorted(filenames):
                path = current_path / filename
                relative = _relative_to_root(path, root)
                if not _matches_any_glob(relative, include_patterns):
                    continue
                if exclude_patterns and _matches_any_glob(relative, exclude_patterns):
                    continue
                if not _looks_like_text_file(path):
                    continue

                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue

                searched_files += 1
                matching_indexes = [index for index, line in enumerate(lines) if matcher.search(line)]
                if not matching_indexes:
                    continue

                files_with_matches.append(relative)
                total_matches += len(matching_indexes)

                if requested_mode != "content":
                    continue

                for match_index in matching_indexes:
                    if len(content_blocks) >= limit:
                        omitted_matches += 1
                        continue
                    start = max(0, match_index - before_after)
                    end = min(len(lines), match_index + before_after + 1)
                    excerpt = [f"{line_no + 1}: {lines[line_no]}" for line_no in range(start, end)]
                    content_blocks.append(f"{relative}:{match_index + 1}\n" + "\n".join(excerpt))
    except OSError as exc:
        return f"Error: failed to search files: {exc}"

    if requested_mode == "count":
        return f"Search results for: {query}\nFiles searched: {searched_files}\nFiles with matches: {len(files_with_matches)}\nTotal matches: {total_matches}"

    if not files_with_matches:
        return "(no matches)"

    if requested_mode == "files_with_matches":
        unique_files = sorted(dict.fromkeys(files_with_matches))
        omitted_files = max(0, len(unique_files) - limit)
        shown = unique_files[:limit]
        if omitted_files:
            shown.append(f"... ({omitted_files} more files)")
        return "\n".join(shown)

    output = "\n\n".join(content_blocks)
    if omitted_matches:
        output = f"{output}\n\n... ({omitted_matches} more matches)"
    return output


@tool
def search_files(
    query: str,
    cwd: str = "",
    include: str = "**/*",
    exclude: str = "",
    regex: bool = False,
    case_sensitive: bool = False,
    context_lines: int = 2,
    max_results: int = 100,
    mode: str = "content",
) -> str:
    """Search file contents inside the working directory.

    Args:
        query: Text or regex pattern to search for.
        cwd: Optional working directory. Empty means the backend process working directory.
        include: Comma-separated glob patterns to include, for example **/*.py,docs/**/*.md.
        exclude: Comma-separated glob patterns to exclude.
        regex: Treat query as a regular expression when true; otherwise search literal text.
        case_sensitive: Use case-sensitive matching when true.
        context_lines: Number of lines before and after each match in content mode.
        max_results: Maximum matches or files returned.
        mode: Output mode: content, files_with_matches, or count.
    """
    return _search_files_impl(query, cwd, include, exclude, regex, case_sensitive, context_lines, max_results, mode)


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
        cwd: Optional working directory. Empty uses a thread-scoped .agent_outputs/shell directory.
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
        resolved_cwd = _resolve_shell_cwd(cwd)
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
    password: str = "",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Run a command on a remote host through paramiko (pure-Python SSH).

    Args:
        command: Remote command text to execute.
        host: SSH host. Empty uses the saved SSH host from configuration.
        user: SSH username. Empty uses the saved SSH username from configuration.
        port: SSH port. Empty uses the saved SSH port from configuration.
        key_file: Private key file path. Empty uses configured keyFile or privateKey.
        password: SSH password. Empty uses the saved SSH password from configuration.
        timeout_seconds: Command timeout in seconds, clamped to 1..120.
    """
    if not command or not command.strip():
        return "Error: command must not be empty."

    try:
        (
            resolved_host,
            resolved_user,
            resolved_port,
            resolved_key_file,
            resolved_key_text,
            resolved_password,
            timeout,
        ) = _resolve_ssh_connection_options(
            host=host,
            user=user,
            port=port,
            key_file=key_file,
            password=password,
            timeout_seconds=timeout_seconds,
        )
    except ValueError as exc:
        return f"Error: {exc}"


    try:
        max_output_chars = int(os.getenv("SHELL_TOOL_MAX_OUTPUT_CHARS", DEFAULT_MAX_OUTPUT_CHARS))
    except ValueError:
        max_output_chars = DEFAULT_MAX_OUTPUT_CHARS

    try:
        client = _paramiko_connect(
            host=resolved_host,
            port=resolved_port,
            user=resolved_user,
            password=resolved_password,
            key_file=resolved_key_file,
            key_text=resolved_key_text,
            timeout=timeout,
        )
    except (Exception, socket.error) as exc:
        import paramiko
        if isinstance(exc, paramiko.SSHException):
            return f"Error: SSH connection failed: {exc}"
        return f"Error: SSH connection failed: {exc}"

    try:
        stdin_chan, stdout_chan, stderr_chan = client.exec_command(command, timeout=timeout)
        stdout_chan.channel.settimeout(timeout)
        try:
            stdout = stdout_chan.read().decode("utf-8", errors="replace")
            stderr = stderr_chan.read().decode("utf-8", errors="replace")
        except socket.timeout:
            output = (
                f"ssh_host: {resolved_host}\n"
                f"ssh_user: {resolved_user or '(default)'}\n"
                f"ssh_port: {resolved_port}\n"
                f"timeout_seconds: {timeout}\n"
                "status: timed out\n\n"
                f"stdout:\n{stdout_chan.read().decode('utf-8', errors='replace') if stdout_chan.channel.recv_ready() else '(partial)'}\n\n"
                f"stderr:\n{stderr_chan.read().decode('utf-8', errors='replace') if stderr_chan.channel.recv_stderr_ready() else '(partial)'}"
            )
            return _truncate(output, max(max_output_chars, 1000))

        exit_status = stdout_chan.channel.recv_exit_status()

        output = (
            f"ssh_host: {resolved_host}\n"
            f"ssh_user: {resolved_user or '(default)'}\n"
            f"ssh_port: {resolved_port}\n"
            f"exit_code: {exit_status}\n\n"
            f"stdout:\n{stdout}\n\n"
            f"stderr:\n{stderr}"
        )
        return _truncate(output, max(max_output_chars, 1000))
    finally:
        client.close()


@tool
def ssh_upload_file(
    local_path: str,
    remote_path: str,
    cwd: str = "",
    host: str = "",
    user: str = "",
    port: int | None = None,
    key_file: str = "",
    password: str = "",
    create_dirs: bool = True,
    overwrite: bool = True,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Upload one local file to a remote host over the existing SSH/SFTP channel.

    Args:
        local_path: Local file path to upload. Relative paths are resolved under cwd.
        remote_path: Remote destination path, for example /tmp/demo.txt.
        cwd: Optional local working directory. Empty means the backend process working directory.
        host: SSH host. Empty uses the saved SSH host from configuration.
        user: SSH username. Empty uses the saved SSH username from configuration.
        port: SSH port. Empty uses the saved SSH port from configuration.
        key_file: Private key file path. Empty uses configured keyFile or privateKey.
        password: SSH password. Empty uses the saved SSH password from configuration.
        create_dirs: Whether to create remote parent directories automatically.
        overwrite: Whether to overwrite the remote file if it already exists.
        timeout_seconds: Connection and transfer timeout in seconds, clamped to 1..120.
    """
    if not local_path or not local_path.strip():
        return "Error: local_path must not be empty."
    if not remote_path or not remote_path.strip():
        return "Error: remote_path must not be empty."

    try:
        _, resolved_local_path = _resolve_safe_path(local_path, cwd)
    except ValueError as exc:
        return f"Error: {exc}"

    if not resolved_local_path.exists():
        return f"Error: local file does not exist: {resolved_local_path}"
    if not resolved_local_path.is_file():
        return f"Error: local path is not a file: {resolved_local_path}"

    normalized_remote_path = posixpath.normpath(str(remote_path).strip())
    remote_dir = posixpath.dirname(normalized_remote_path)

    try:
        (
            resolved_host,
            resolved_user,
            resolved_port,
            resolved_key_file,
            resolved_key_text,
            resolved_password,
            timeout,
        ) = _resolve_ssh_connection_options(
            host=host,
            user=user,
            port=port,
            key_file=key_file,
            password=password,
            timeout_seconds=timeout_seconds,
        )
    except ValueError as exc:
        return f"Error: {exc}"

    try:
        local_size = resolved_local_path.stat().st_size
    except OSError as exc:
        return f"Error: failed to stat local file: {exc}"

    try:
        client = _paramiko_connect(
            host=resolved_host,
            port=resolved_port,
            user=resolved_user,
            password=resolved_password,
            key_file=resolved_key_file,
            key_text=resolved_key_text,
            timeout=timeout,
        )
    except (Exception, socket.error) as exc:
        return f"Error: SSH connection failed: {exc}"

    try:
        sftp = client.open_sftp()
        try:
            # 先处理远端父目录，再决定是否允许覆盖；这样返回信息更稳定，也更接近日常使用预期。
            if create_dirs and remote_dir not in {"", "."}:
                _ensure_remote_directory(sftp, remote_dir)

            if not overwrite:
                try:
                    sftp.stat(normalized_remote_path)
                    return f"Error: remote file already exists: {normalized_remote_path}"
                except OSError:
                    pass

            sftp.put(str(resolved_local_path), normalized_remote_path)
            try:
                remote_size = int(sftp.stat(normalized_remote_path).st_size)
            except OSError:
                remote_size = local_size

            return (
                "Uploaded file over SSH/SFTP.\n"
                f"ssh_host: {resolved_host}\n"
                f"ssh_user: {resolved_user}\n"
                f"ssh_port: {resolved_port}\n"
                f"local_path: {resolved_local_path}\n"
                f"remote_path: {normalized_remote_path}\n"
                f"local_size: {local_size}\n"
                f"remote_size: {remote_size}\n"
                f"create_dirs: {create_dirs}\n"
                f"overwrite: {overwrite}"
            )
        finally:
            sftp.close()
    except OSError as exc:
        return f"Error: SSH upload failed: {exc}"
    finally:
        client.close()


@tool
def ssh_download_file(
    remote_path: str,
    local_path: str,
    cwd: str = "",
    host: str = "",
    user: str = "",
    port: int | None = None,
    key_file: str = "",
    password: str = "",
    create_dirs: bool = True,
    overwrite: bool = True,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Download one remote file to the local working directory over SSH/SFTP.

    Args:
        remote_path: Remote source path, for example /tmp/demo.txt.
        local_path: Local destination path. Relative paths are resolved under cwd.
        cwd: Optional local working directory. Empty means the backend process working directory.
        host: SSH host. Empty uses the saved SSH host from configuration.
        user: SSH username. Empty uses the saved SSH username from configuration.
        port: SSH port. Empty uses the saved SSH port from configuration.
        key_file: Private key file path. Empty uses configured keyFile or privateKey.
        password: SSH password. Empty uses the saved SSH password from configuration.
        create_dirs: Whether to create local parent directories automatically.
        overwrite: Whether to overwrite the local file if it already exists.
        timeout_seconds: Connection and transfer timeout in seconds, clamped to 1..120.
    """
    if not remote_path or not remote_path.strip():
        return "Error: remote_path must not be empty."
    if not local_path or not local_path.strip():
        return "Error: local_path must not be empty."

    normalized_remote_path = posixpath.normpath(str(remote_path).strip())

    try:
        resolved_local_path = _local_output_path(local_path, cwd)
    except ValueError as exc:
        return f"Error: {exc}"

    if resolved_local_path.exists() and not resolved_local_path.is_file():
        return f"Error: local path is not a file: {resolved_local_path}"
    if resolved_local_path.exists() and not overwrite:
        return f"Error: local file already exists: {resolved_local_path}"

    if create_dirs:
        try:
            resolved_local_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"Error: failed to create local parent directories: {exc}"
    elif not resolved_local_path.parent.exists():
        return f"Error: local parent directory does not exist: {resolved_local_path.parent}"

    try:
        (
            resolved_host,
            resolved_user,
            resolved_port,
            resolved_key_file,
            resolved_key_text,
            resolved_password,
            timeout,
        ) = _resolve_ssh_connection_options(
            host=host,
            user=user,
            port=port,
            key_file=key_file,
            password=password,
            timeout_seconds=timeout_seconds,
        )
    except ValueError as exc:
        return f"Error: {exc}"

    try:
        client = _paramiko_connect(
            host=resolved_host,
            port=resolved_port,
            user=resolved_user,
            password=resolved_password,
            key_file=resolved_key_file,
            key_text=resolved_key_text,
            timeout=timeout,
        )
    except (Exception, socket.error) as exc:
        return f"Error: SSH connection failed: {exc}"

    try:
        sftp = client.open_sftp()
        try:
            try:
                remote_size = int(sftp.stat(normalized_remote_path).st_size)
            except OSError as exc:
                return f"Error: remote file does not exist or is inaccessible: {normalized_remote_path} ({exc})"

            sftp.get(normalized_remote_path, str(resolved_local_path))
            try:
                local_size = resolved_local_path.stat().st_size
            except OSError:
                local_size = remote_size

            return (
                "Downloaded file over SSH/SFTP.\n"
                f"ssh_host: {resolved_host}\n"
                f"ssh_user: {resolved_user}\n"
                f"ssh_port: {resolved_port}\n"
                f"remote_path: {normalized_remote_path}\n"
                f"local_path: {resolved_local_path}\n"
                f"remote_size: {remote_size}\n"
                f"local_size: {local_size}\n"
                f"create_dirs: {create_dirs}\n"
                f"overwrite: {overwrite}"
            )
        finally:
            sftp.close()
    except OSError as exc:
        return f"Error: SSH download failed: {exc}"
    finally:
        client.close()


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


@tool
def cancel_background_task(task_id: str) -> str:
    """Cancel a running background shell task.

    Args:
        task_id: Background task ID returned by run_shell_command.
    """
    try:
        meta = _read_background_meta(task_id)
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as exc:
        return f"Error: {exc}"

    background_task_id = str(meta.get("id") or task_id)
    status = str(meta.get("status") or "")
    if status != "running":
        return f"Background task {background_task_id} is {status or 'unknown'}, cannot cancel."

    killed = False
    has_live_worker = False
    with BACKGROUND_TASKS_LOCK:
        BACKGROUND_CANCEL_REQUESTS.add(background_task_id)
        meta["status"] = "cancel_requested"
        meta["cancel_requested_at"] = time.time()
        process = BACKGROUND_PROCESSES.get(background_task_id)
        if process is not None:
            has_live_worker = True
            _kill_process_tree(process)
            killed = True
        _write_background_meta(background_task_id, meta)

    if not killed:
        try:
            pid = int(meta.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        killed = _kill_process_tree_by_pid(pid)

    if not has_live_worker:
        meta.update(
            {
                "status": "cancelled",
                "finished_at": time.time(),
                "exit_code": None,
                "timed_out": False,
            }
        )
        with BACKGROUND_TASKS_LOCK:
            BACKGROUND_CANCEL_REQUESTS.discard(background_task_id)
            _write_background_meta(background_task_id, meta)

    log_path = Path(str(meta.get("log_path") or ""))
    if log_path:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(
                    "\n[agent background] cancel requested\n"
                    f"killed: {killed}\n"
                    f"cancel_requested_at: {meta['cancel_requested_at']}\n"
                )
        except OSError:
            pass

    return (
        f"Cancel requested for background task {background_task_id}.\n"
        f"killed: {killed}\n"
        "Use get_background_task to confirm final status."
    )


ALL_TOOLS = [
    todo_write,
    load_skill,
    compact,
    remember,
    rag_rebuild_index,
    rag_search,
    delegate_task,
    # MOA scaffold exists in agent/moa.py but is intentionally not registered.
    # It is too token-expensive for the current personal-agent workflow.
    web_search,
    web_extract,
    # Legacy compatibility wrapper; prefer delegate_task for new multi-agent work.
    run_subagent,
    create_task,
    list_tasks,
    get_task,
    claim_task,
    complete_task,
    schedule_cron,
    list_crons,
    cancel_cron,
    read_file,
    write_file,
    edit_file,
    glob_files,
    search_files,
    run_shell_command,
    run_ssh_command,
    ssh_upload_file,
    ssh_download_file,
    list_background_tasks,
    get_background_task,
    cancel_background_task,
]

if _bool_env("AGENT_PLAYWRIGHT_ENABLED"):
    from tools.playwright import PLAYWRIGHT_TOOLS, set_thread_id_getter

    set_thread_id_getter(lambda: CURRENT_TOOL_THREAD_ID.get())
    ALL_TOOLS.extend(PLAYWRIGHT_TOOLS)

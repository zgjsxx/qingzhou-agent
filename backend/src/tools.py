"""Tools for the agent."""

from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import time
from pathlib import Path

from langchain.tools import tool

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 120
DEFAULT_MAX_OUTPUT_CHARS = 12000


def _resolve_cwd(cwd: str) -> str:
    if not cwd:
        return str(Path.cwd())

    resolved = Path(cwd).expanduser().resolve()
    if not resolved.exists():
        raise ValueError(f"Working directory does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Working directory is not a directory: {resolved}")
    return str(resolved)


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


ALL_TOOLS = [get_system_cpu_usage, run_shell_command]

"""Local runtime configuration loaded from config/qingzhou-agent.json."""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "qingzhou-agent.json"

_CONFIG_LOCK = threading.RLock()
_CONFIG_DATA: dict[str, Any] = {}
_CONFIG_SIGNATURE: tuple[int, int] | None = None
_CONFIG_SOURCE_PATH: Path | None = None
_CONFIG_CALLBACKS: list[Callable[[], None]] = []
_WATCHER_STARTED = False
_WATCHER_STOP = threading.Event()


def _config_signature() -> tuple[int, int] | None:
    try:
        stat = CONFIG_FILE.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _read_config_file() -> dict[str, Any] | None:
    if not CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[qingzhou-agent] config reload skipped: {exc}", file=sys.stderr, flush=True)
        return None
    if not isinstance(data, dict):
        return {}
    ssh = data.get("ssh")
    if isinstance(ssh, dict):
        data["ssh"] = [ssh]
    return data


def reload_agent_config(*, force: bool = False) -> bool:
    """Reload the config snapshot from disk.

    Returns True when the in-memory snapshot changed. Invalid JSON is ignored so
    a partially-written config file does not wipe the active runtime config.
    """
    global _CONFIG_DATA, _CONFIG_SIGNATURE, _CONFIG_SOURCE_PATH

    signature = _config_signature()
    source_path = CONFIG_FILE
    with _CONFIG_LOCK:
        if (
            not force
            and _CONFIG_SOURCE_PATH == source_path
            and signature == _CONFIG_SIGNATURE
        ):
            return False
        data = _read_config_file()
        if data is None:
            return False
        _CONFIG_DATA = data
        _CONFIG_SIGNATURE = signature
        _CONFIG_SOURCE_PATH = source_path
        return True


def register_config_reload_callback(callback: Callable[[], None]) -> None:
    with _CONFIG_LOCK:
        if callback not in _CONFIG_CALLBACKS:
            _CONFIG_CALLBACKS.append(callback)


def _notify_config_reload_callbacks() -> None:
    with _CONFIG_LOCK:
        callbacks = list(_CONFIG_CALLBACKS)
    for callback in callbacks:
        try:
            callback()
        except Exception as exc:  # noqa: BLE001 - config watchers must keep running.
            print(f"[qingzhou-agent] config reload callback failed: {exc}", file=sys.stderr, flush=True)


def _config_watch_interval_seconds() -> float:
    try:
        value = float(os.getenv("AGENT_CONFIG_WATCH_INTERVAL_SECONDS", "1.0"))
    except (TypeError, ValueError):
        value = 1.0
    return max(0.2, min(value, 30.0))


def start_config_watcher(callback: Callable[[], None] | None = None) -> None:
    """Start a daemon watcher that refreshes the config snapshot on file changes."""
    global _WATCHER_STARTED

    if callback is not None:
        register_config_reload_callback(callback)

    with _CONFIG_LOCK:
        if _WATCHER_STARTED:
            return
        _WATCHER_STARTED = True

    def run() -> None:
        while not _WATCHER_STOP.wait(_config_watch_interval_seconds()):
            if reload_agent_config():
                _notify_config_reload_callbacks()

    threading.Thread(target=run, name="qingzhou-config-watcher", daemon=True).start()


def load_agent_config() -> dict[str, Any]:
    """Return the current in-memory config snapshot."""
    if _CONFIG_SOURCE_PATH != CONFIG_FILE:
        reload_agent_config(force=True)
    with _CONFIG_LOCK:
        return copy.deepcopy(_CONFIG_DATA)


def config_section(name: str) -> dict[str, Any]:
    value = load_agent_config().get(name, {})
    return value if isinstance(value, dict) else {}


def config_str(section: str, key: str, default: str = "") -> str:
    value = config_section(section).get(key)
    if value is None:
        return default
    return str(value).strip()


def config_int(section: str, key: str, default: int) -> int:
    value = config_section(section).get(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def ssh_hosts() -> list[dict[str, Any]]:
    """Return the ssh hosts array. Always a list (empty if missing)."""
    cfg = load_agent_config()
    ssh = cfg.get("ssh", [])
    return ssh if isinstance(ssh, list) else []


def ssh_host_entry(target_host: str = "") -> dict[str, Any]:
    """Find the SSH entry matching *target_host*.

    - Exact match on ``host`` field returns that entry.
    - No match returns the first entry as default.
    - Empty array returns ``{}``.
    """
    hosts = ssh_hosts()
    if not hosts:
        return {}
    for entry in hosts:
        if entry.get("host") == target_host:
            return entry
    return hosts[0]


reload_agent_config(force=True)

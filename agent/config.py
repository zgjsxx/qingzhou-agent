"""Local runtime configuration loaded from config/qingzhou-agent.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "qingzhou-agent.json"


def load_agent_config() -> dict[str, Any]:
    """Load local UI-managed config. Missing or invalid config returns an empty dict.

    Backward-compat: if ``ssh`` is a dict (old single-host format), it is
    automatically wrapped into a list so callers always see an array.
    """
    if not CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Migrate old single-host ssh dict → array
    ssh = data.get("ssh")
    if isinstance(ssh, dict):
        data["ssh"] = [ssh]
    return data


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

    - Exact match on ``host`` field → return that entry.
    - No match → return the first entry as default.
    - Empty array → return ``{}``.
    """
    hosts = ssh_hosts()
    if not hosts:
        return {}
    for entry in hosts:
        if entry.get("host") == target_host:
            return entry
    return hosts[0]

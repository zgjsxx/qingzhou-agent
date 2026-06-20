"""Local runtime configuration loaded from backend/.agent_config.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
CONFIG_FILE = BACKEND_DIR / ".agent_config.json"


def load_agent_config() -> dict[str, Any]:
    """Load local UI-managed config. Missing or invalid config returns an empty dict."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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

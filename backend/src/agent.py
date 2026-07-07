"""Compatibility entrypoint for backend/langgraph.json."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_SRC_DIR.parents[1]
sys.path.insert(0, str(BACKEND_SRC_DIR))
sys.path.insert(0, str(REPO_ROOT))

from agent.graph import graph  # noqa: E402,F401

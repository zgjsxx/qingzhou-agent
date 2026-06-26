#!/usr/bin/env python
r"""Manually rebuild the local RAG index.

Examples:
    python scripts/build_rag_index.py
    python scripts/build_rag_index.py --data-dir data/rag_docs
    python scripts/build_rag_index.py --env-file .env --data-dir D:\docs\rag
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = BACKEND_DIR / "src"


def _load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE lines without adding a runtime dependency."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild the llama-index RAG index.")
    parser.add_argument(
        "--data-dir",
        default="",
        help="Document directory. Defaults to RAG_DOCS_DIR or backend/data/rag_docs.",
    )
    parser.add_argument(
        "--env-file",
        default=str(BACKEND_DIR / ".env"),
        help="Env file to load before building. Defaults to backend/.env.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    _load_env_file(Path(args.env_file).expanduser())
    sys.path.insert(0, str(SRC_DIR))

    from agent_rag import rag_rebuild_index

    print("Rebuilding RAG index...", flush=True)
    result = rag_rebuild_index(data_dir=args.data_dir)
    print(result)
    return 0 if "successfully" in result else 1


if __name__ == "__main__":
    raise SystemExit(main())

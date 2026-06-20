"""Persistent task graph tools for recoverable multi-step work."""

from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
TASKS_DIR = BACKEND_DIR / ".tasks"


@dataclass
class PersistentTask:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]


def _safe_task_id(task_id: str) -> str:
    value = str(task_id or "").strip()
    if not value:
        raise ValueError("task_id must not be empty.")
    if not all(char.isalnum() or char in {"-", "_"} for char in value):
        raise ValueError("task_id may only contain letters, numbers, hyphen, and underscore.")
    return value


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{_safe_task_id(task_id)}.json"


def _new_task_id() -> str:
    return f"task_{int(time.time())}_{random.randint(0, 9999):04d}"


def _normalize_dependencies(blocked_by: list[str] | None) -> list[str]:
    if blocked_by is None:
        return []
    if not isinstance(blocked_by, list):
        raise ValueError("blockedBy must be a list of task IDs.")
    return [_safe_task_id(item) for item in blocked_by]


def _task_to_json(task: PersistentTask) -> str:
    return json.dumps(asdict(task), ensure_ascii=False, indent=2)


def _task_from_json(raw: str) -> PersistentTask:
    data = json.loads(raw)
    blocked_by = data.get("blockedBy", [])
    if not isinstance(blocked_by, list):
        blocked_by = []
    return PersistentTask(
        id=str(data.get("id", "")).strip(),
        subject=str(data.get("subject", "")).strip(),
        description=str(data.get("description", "")).strip(),
        status=str(data.get("status", "pending")).strip(),
        owner=data.get("owner") if data.get("owner") else None,
        blockedBy=[str(item).strip() for item in blocked_by if str(item).strip()],
    )


def save_persistent_task(task: PersistentTask) -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    _task_path(task.id).write_text(f"{_task_to_json(task)}\n", encoding="utf-8")


def load_persistent_task(task_id: str) -> PersistentTask:
    path = _task_path(task_id)
    if not path.exists():
        raise FileNotFoundError(f"Task not found: {task_id}")
    return _task_from_json(path.read_text(encoding="utf-8", errors="replace"))


def list_persistent_tasks() -> list[PersistentTask]:
    if not TASKS_DIR.exists():
        return []
    tasks: list[PersistentTask] = []
    for path in sorted(TASKS_DIR.glob("task_*.json")):
        try:
            tasks.append(_task_from_json(path.read_text(encoding="utf-8", errors="replace")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
    return tasks


def create_persistent_task(subject: str, description: str = "", blocked_by: list[str] | None = None) -> PersistentTask:
    clean_subject = str(subject or "").strip()
    if not clean_subject:
        raise ValueError("subject must not be empty.")

    task = PersistentTask(
        id=_new_task_id(),
        subject=clean_subject,
        description=str(description or "").strip(),
        status="pending",
        owner=None,
        blockedBy=_normalize_dependencies(blocked_by),
    )
    save_persistent_task(task)
    return task


def can_start_persistent_task(task_id: str) -> tuple[bool, list[str]]:
    task = load_persistent_task(task_id)
    blockers: list[str] = []
    for dep_id in task.blockedBy:
        try:
            dep = load_persistent_task(dep_id)
        except FileNotFoundError:
            blockers.append(dep_id)
            continue
        if dep.status != "completed":
            blockers.append(dep_id)
    return not blockers, blockers


def claim_persistent_task(task_id: str, owner: str = "agent") -> PersistentTask:
    task = load_persistent_task(task_id)
    if task.status != "pending":
        raise ValueError(f"Task {task.id} is {task.status}, cannot claim.")

    can_start, blockers = can_start_persistent_task(task.id)
    if not can_start:
        raise ValueError(f"Task {task.id} is blocked by: {', '.join(blockers)}")

    task.owner = str(owner or "agent").strip() or "agent"
    task.status = "in_progress"
    save_persistent_task(task)
    return task


def complete_persistent_task(task_id: str) -> tuple[PersistentTask, list[PersistentTask]]:
    task = load_persistent_task(task_id)
    if task.status != "in_progress":
        raise ValueError(f"Task {task.id} is {task.status}, cannot complete.")

    task.status = "completed"
    save_persistent_task(task)
    unblocked = [
        candidate
        for candidate in list_persistent_tasks()
        if candidate.status == "pending" and candidate.blockedBy and can_start_persistent_task(candidate.id)[0]
    ]
    return task, unblocked


def task_summary_line(task: PersistentTask) -> str:
    deps = f" blockedBy={task.blockedBy}" if task.blockedBy else ""
    owner = f" owner={task.owner}" if task.owner else ""
    return f"{task.id}: {task.subject} [{task.status}]{owner}{deps}"


def task_detail_json(task: PersistentTask) -> str:
    return _task_to_json(task)

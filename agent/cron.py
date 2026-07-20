"""Session-bound cron scheduling for automatic agent prompts."""

from __future__ import annotations

import asyncio
import json
import os
import random
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph_sdk import get_client

from agent.config import config_int
from agent.logging import log_event
from agent.tasks import ROOT_DIR

CRON_DIR = ROOT_DIR / ".agent_cron"
DEFAULT_STORAGE_PATH = CRON_DIR / "scheduled_tasks.json"
DEFAULT_POLL_SECONDS = 30
DEFAULT_MAX_JOBS = 50
DEFAULT_API_URL = f"http://localhost:{config_int('server', 'backendPort', 2024)}"
DEFAULT_ASSISTANT_ID = "agent"


@dataclass
class CronJob:
    id: str
    thread_id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool
    enabled: bool
    created_at: str
    last_fired_at: str | None = None


@dataclass
class PendingCron:
    job_id: str
    thread_id: str
    prompt: str
    fired_at: str


_jobs: dict[str, CronJob] = {}
_pending: dict[str, PendingCron] = {}
_lock = threading.RLock()
_started = False


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(os.getenv(name, default))
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, minimum)


def _storage_path() -> Path:
    value = os.getenv("AGENT_CRON_STORAGE_PATH", "").strip()
    if not value:
        return DEFAULT_STORAGE_PATH
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT_DIR / path


def _api_url() -> str:
    return (
        os.getenv("AGENT_CRON_API_URL")
        or os.getenv("LANGGRAPH_API_URL")
        or DEFAULT_API_URL
    ).strip()


def _assistant_id() -> str:
    return os.getenv("AGENT_CRON_ASSISTANT_ID", DEFAULT_ASSISTANT_ID).strip() or DEFAULT_ASSISTANT_ID


def is_cron_enabled() -> bool:
    return _bool_env("AGENT_CRON_ENABLED", False)


def _new_job_id() -> str:
    return f"cron_{int(time.time())}_{random.randint(0, 999999):06d}"


def _job_from_dict(data: dict[str, Any]) -> CronJob:
    return CronJob(
        id=str(data.get("id", "")).strip(),
        thread_id=str(data.get("thread_id", "")).strip(),
        cron=str(data.get("cron", "")).strip(),
        prompt=str(data.get("prompt", "")).strip(),
        recurring=bool(data.get("recurring", True)),
        durable=bool(data.get("durable", True)),
        enabled=bool(data.get("enabled", True)),
        created_at=str(data.get("created_at", "")).strip() or datetime.now(timezone.utc).isoformat(),
        last_fired_at=data.get("last_fired_at") if data.get("last_fired_at") else None,
    )


def _save_jobs_locked() -> None:
    path = _storage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    durable_jobs = [asdict(job) for job in _jobs.values() if job.durable]
    path.write_text(json.dumps({"tasks": durable_jobs}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_durable_jobs() -> None:
    path = _storage_path()
    if not path.exists():
        return

    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        items = raw.get("tasks", raw if isinstance(raw, list) else [])
        if not isinstance(items, list):
            return
    except (OSError, json.JSONDecodeError):
        log_event("cron.load_error", path=str(path))
        return

    loaded = 0
    with _lock:
        for item in items:
            if not isinstance(item, dict):
                continue
            job = _job_from_dict(item)
            if not job.id or not job.thread_id or not job.prompt or validate_cron(job.cron):
                continue
            _jobs[job.id] = job
            loaded += 1
    if loaded:
        log_event("cron.load", count=loaded, path=str(path))


def _cron_field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    if "," in field:
        return any(_cron_field_matches(part.strip(), value) for part in field.split(","))
    if "-" in field:
        start, end = field.split("-", 1)
        return int(start) <= value <= int(end)
    return value == int(field)


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False

    minute, hour, day_of_month, month, day_of_week = fields
    cron_weekday = (dt.weekday() + 1) % 7
    if not (
        _cron_field_matches(minute, dt.minute)
        and _cron_field_matches(hour, dt.hour)
        and _cron_field_matches(month, dt.month)
    ):
        return False

    dom_ok = _cron_field_matches(day_of_month, dt.day)
    dow_ok = _cron_field_matches(day_of_week, cron_weekday)
    dom_unconstrained = day_of_month == "*"
    dow_unconstrained = day_of_week == "*"
    if dom_unconstrained and dow_unconstrained:
        return True
    if dom_unconstrained:
        return dow_ok
    if dow_unconstrained:
        return dom_ok
    return dom_ok or dow_ok


def _validate_cron_field(field: str, low: int, high: int) -> str | None:
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            return f"invalid step: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            error = _validate_cron_field(part.strip(), low, high)
            if error:
                return error
        return None
    if "-" in field:
        start, end = field.split("-", 1)
        if not start.isdigit() or not end.isdigit():
            return f"invalid range: {field}"
        start_value = int(start)
        end_value = int(end)
        if start_value > end_value:
            return f"range start greater than end: {field}"
        if start_value < low or end_value > high:
            return f"range out of bounds [{low}-{high}]: {field}"
        return None
    if not field.isdigit():
        return f"invalid field: {field}"
    value = int(field)
    if value < low or value > high:
        return f"value out of bounds [{low}-{high}]: {field}"
    return None


def validate_cron(cron_expr: str) -> str | None:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"expected 5 fields, got {len(fields)}"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for field, (low, high), name in zip(fields, bounds, names):
        error = _validate_cron_field(field, low, high)
        if error:
            return f"{name}: {error}"
    return None


def schedule_job(
    *,
    thread_id: str,
    cron: str,
    prompt: str,
    recurring: bool = True,
    durable: bool = True,
) -> CronJob | str:
    thread_id = str(thread_id or "").strip()
    cron = str(cron or "").strip()
    prompt = str(prompt or "").strip()
    if not thread_id:
        return "thread_id is required"
    if not prompt:
        return "prompt is required"
    error = validate_cron(cron)
    if error:
        return error

    with _lock:
        max_jobs = _int_env("AGENT_CRON_MAX_JOBS", DEFAULT_MAX_JOBS)
        if len(_jobs) >= max_jobs:
            return f"too many scheduled jobs; max {max_jobs}"
        job = CronJob(
            id=_new_job_id(),
            thread_id=thread_id,
            cron=cron,
            prompt=prompt,
            recurring=bool(recurring),
            durable=bool(durable),
            enabled=True,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        _jobs[job.id] = job
        if job.durable:
            _save_jobs_locked()

    log_event("cron.register", job_id=job.id, thread_id=thread_id, cron=cron, recurring=job.recurring, durable=job.durable)
    return job


def list_jobs(thread_id: str | None = None) -> list[CronJob]:
    with _lock:
        jobs = list(_jobs.values())
    if thread_id:
        jobs = [job for job in jobs if job.thread_id == thread_id]
    return sorted(jobs, key=lambda job: job.created_at)


def cancel_job(job_id: str) -> str:
    job_id = str(job_id or "").strip()
    if not job_id:
        return "job_id is required"
    with _lock:
        job = _jobs.pop(job_id, None)
        _pending.pop(job_id, None)
        if job and job.durable:
            _save_jobs_locked()
    if not job:
        return f"job not found: {job_id}"
    log_event("cron.cancel", job_id=job_id, thread_id=job.thread_id)
    return f"Cancelled {job_id}"


def _enqueue_due_jobs(now: datetime) -> None:
    marker = now.strftime("%Y-%m-%d %H:%M")
    fired_at = now.isoformat()
    changed = False
    with _lock:
        for job in list(_jobs.values()):
            if not job.enabled:
                continue
            try:
                if not cron_matches(job.cron, now):
                    continue
            except Exception as exc:
                log_event("cron.match_error", job_id=job.id, error=repr(exc))
                continue
            if job.last_fired_at == marker:
                continue
            if job.id not in _pending:
                _pending[job.id] = PendingCron(
                    job_id=job.id,
                    thread_id=job.thread_id,
                    prompt=job.prompt,
                    fired_at=fired_at,
                )
                log_event("cron.enqueue", job_id=job.id, thread_id=job.thread_id, fired_at=fired_at)
            job.last_fired_at = marker
            changed = True
            if not job.recurring:
                _jobs.pop(job.id, None)
                changed = True
        if changed:
            _save_jobs_locked()


async def _thread_has_active_run(client: Any, thread_id: str) -> bool:
    for status in ("pending", "running"):
        try:
            runs = await client.runs.list(thread_id, status=status, limit=1)
        except Exception as exc:
            log_event("cron.busy_check_error", thread_id=thread_id, status=status, error=repr(exc))
            return True
        if runs:
            return True
    return False


async def _deliver_pending_once() -> None:
    with _lock:
        pending = list(_pending.values())
    if not pending:
        return

    client = get_client(url=_api_url())
    assistant_id = _assistant_id()
    for item in pending:
        if await _thread_has_active_run(client, item.thread_id):
            continue

        content = (
            "[Scheduled task triggered]\n"
            f"Job ID: {item.job_id}\n"
            f"Triggered at: {item.fired_at}\n\n"
            f"{item.prompt}"
        )
        try:
            await client.runs.create(
                item.thread_id,
                assistant_id,
                input={"messages": [{"role": "user", "content": content}]},
                stream_mode=["values"],
                stream_subgraphs=True,
                stream_resumable=False,
                metadata={"source": "cron", "cron_job_id": item.job_id},
                multitask_strategy="reject",
            )
        except Exception as exc:
            log_event("cron.deliver_error", job_id=item.job_id, thread_id=item.thread_id, error=repr(exc))
            continue

        with _lock:
            _pending.pop(item.job_id, None)
        log_event("cron.deliver", job_id=item.job_id, thread_id=item.thread_id)


def _scheduler_loop() -> None:
    poll_seconds = _int_env("AGENT_CRON_POLL_SECONDS", DEFAULT_POLL_SECONDS)
    while True:
        try:
            _enqueue_due_jobs(datetime.now())
            asyncio.run(_deliver_pending_once())
        except Exception as exc:
            log_event("cron.loop_error", error=repr(exc))
        time.sleep(poll_seconds)


def start_cron_scheduler() -> None:
    global _started
    if not is_cron_enabled():
        return
    with _lock:
        if _started:
            return
        load_durable_jobs()
        _started = True
    thread = threading.Thread(target=_scheduler_loop, name="agent-cron-scheduler", daemon=True)
    thread.start()
    log_event("cron.start", poll_seconds=_int_env("AGENT_CRON_POLL_SECONDS", DEFAULT_POLL_SECONDS), api_url=_api_url())

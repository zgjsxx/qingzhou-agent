"""SQLite-backed Kanban Lite for durable multi-agent task routing."""

from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT_DIR = Path(__file__).resolve().parents[1]
KANBAN_DIR = ROOT_DIR / ".kanban"
DEFAULT_CLAIM_TTL_SECONDS = 15 * 60
VALID_STATUSES = {"todo", "ready", "running", "blocked", "done", "archived"}


@dataclass
class KanbanTask:
    id: str
    title: str
    body: str
    assignee: str | None
    status: str
    priority: int
    created_by: str | None
    created_at: int
    started_at: int | None
    completed_at: int | None
    claim_lock: str | None
    claim_expires: int | None
    current_run_id: int | None
    result: str | None


@dataclass
class KanbanRun:
    id: int
    task_id: str
    assignee: str | None
    status: str
    claim_lock: str | None
    claim_expires: int | None
    started_at: int
    ended_at: int | None
    outcome: str | None
    summary: str | None
    error: str | None


def _db_path() -> Path:
    configured = os.getenv("AGENT_KANBAN_DB", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return KANBAN_DIR / "kanban.db"


def _new_task_id() -> str:
    return f"kbn_{int(time.time())}_{random.randint(0, 9999):04d}"


def _now() -> int:
    return int(time.time())


def _row_task(row: sqlite3.Row) -> KanbanTask:
    return KanbanTask(
        id=str(row["id"]),
        title=str(row["title"]),
        body=str(row["body"] or ""),
        assignee=row["assignee"],
        status=str(row["status"]),
        priority=int(row["priority"] or 0),
        created_by=row["created_by"],
        created_at=int(row["created_at"]),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        claim_lock=row["claim_lock"],
        claim_expires=row["claim_expires"],
        current_run_id=row["current_run_id"],
        result=row["result"],
    )


def _row_run(row: sqlite3.Row) -> KanbanRun:
    return KanbanRun(
        id=int(row["id"]),
        task_id=str(row["task_id"]),
        assignee=row["assignee"],
        status=str(row["status"]),
        claim_lock=row["claim_lock"],
        claim_expires=row["claim_expires"],
        started_at=int(row["started_at"]),
        ended_at=row["ended_at"],
        outcome=row["outcome"],
        summary=row["summary"],
        error=row["error"],
    )


def _metadata_json(metadata: dict[str, Any] | None) -> str | None:
    if not metadata:
        return None
    return json.dumps(metadata, ensure_ascii=False, sort_keys=True)


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id             TEXT PRIMARY KEY,
            title          TEXT NOT NULL,
            body           TEXT NOT NULL DEFAULT '',
            assignee       TEXT,
            status         TEXT NOT NULL,
            priority       INTEGER NOT NULL DEFAULT 0,
            created_by     TEXT,
            created_at     INTEGER NOT NULL,
            started_at     INTEGER,
            completed_at   INTEGER,
            claim_lock     TEXT,
            claim_expires  INTEGER,
            current_run_id INTEGER,
            result         TEXT
        );

        CREATE TABLE IF NOT EXISTS task_links (
            parent_id TEXT NOT NULL,
            child_id  TEXT NOT NULL,
            PRIMARY KEY (parent_id, child_id)
        );

        CREATE TABLE IF NOT EXISTS task_comments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL,
            author     TEXT NOT NULL,
            body       TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS task_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL,
            run_id     INTEGER,
            kind       TEXT NOT NULL,
            payload    TEXT,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS task_runs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id       TEXT NOT NULL,
            assignee      TEXT,
            status        TEXT NOT NULL,
            claim_lock    TEXT,
            claim_expires INTEGER,
            started_at    INTEGER NOT NULL,
            ended_at      INTEGER,
            outcome       TEXT,
            summary       TEXT,
            metadata      TEXT,
            error         TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee, status);
        CREATE INDEX IF NOT EXISTS idx_runs_task ON task_runs(task_id);
        """
    )
    conn.commit()


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    init_db(conn)
    return conn


@contextmanager
def connect_closing(path: Path | None = None):
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_txn(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _append_event(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    run_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            task_id,
            run_id,
            kind,
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
            _now(),
        ),
    )


def _parents_done(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
          FROM task_links l
          JOIN tasks p ON p.id = l.parent_id
         WHERE l.child_id = ?
           AND p.status NOT IN ('done', 'archived')
         LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    return row is None


def get_task(conn: sqlite3.Connection, task_id: str) -> KanbanTask | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_task(row) if row else None


def list_tasks(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    assignee: str | None = None,
    include_archived: bool = False,
) -> list[KanbanTask]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    elif not include_archived:
        clauses.append("status != 'archived'")
    if assignee:
        clauses.append("assignee = ?")
        params.append(assignee)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM tasks {where} ORDER BY priority DESC, created_at ASC",
        params,
    ).fetchall()
    return [_row_task(row) for row in rows]


def create_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: str = "",
    assignee: str | None = "agent",
    parents: Iterable[str] | None = None,
    priority: int = 0,
    created_by: str = "agent",
) -> KanbanTask:
    clean_title = str(title or "").strip()
    if not clean_title:
        raise ValueError("title must not be empty.")
    parent_ids = [str(item).strip() for item in (parents or []) if str(item).strip()]
    now = _now()
    task_id = _new_task_id()
    with write_txn(conn):
        missing = [
            parent_id
            for parent_id in parent_ids
            if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (parent_id,)).fetchone() is None
        ]
        if missing:
            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
        status = "ready"
        if parent_ids:
            undone = conn.execute(
                "SELECT 1 FROM tasks WHERE id IN (%s) AND status NOT IN ('done', 'archived') LIMIT 1"
                % ",".join("?" for _ in parent_ids),
                parent_ids,
            ).fetchone()
            status = "todo" if undone else "ready"
        conn.execute(
            """
            INSERT INTO tasks(id, title, body, assignee, status, priority, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                clean_title,
                str(body or "").strip(),
                str(assignee).strip() if assignee else None,
                status,
                int(priority or 0),
                str(created_by or "agent").strip() or "agent",
                now,
            ),
        )
        for parent_id in parent_ids:
            conn.execute(
                "INSERT OR IGNORE INTO task_links(parent_id, child_id) VALUES (?, ?)",
                (parent_id, task_id),
            )
        _append_event(
            conn,
            task_id,
            "created",
            {"status": status, "assignee": assignee, "parents": parent_ids},
        )
    task = get_task(conn, task_id)
    assert task is not None
    return task


def recompute_ready(conn: sqlite3.Connection) -> list[str]:
    promoted: list[str] = []
    with write_txn(conn):
        rows = conn.execute("SELECT id FROM tasks WHERE status = 'todo' ORDER BY created_at ASC").fetchall()
        for row in rows:
            task_id = str(row["id"])
            if not _parents_done(conn, task_id):
                continue
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'todo'",
                (task_id,),
            )
            if cur.rowcount == 1:
                promoted.append(task_id)
                _append_event(conn, task_id, "promoted", {"status": "ready"})
    return promoted


def claim_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    owner: str = "agent",
    ttl_seconds: int = DEFAULT_CLAIM_TTL_SECONDS,
) -> KanbanTask | None:
    now = _now()
    lock = f"{str(owner or 'agent').strip() or 'agent'}:{os.getpid()}"
    expires = now + max(int(ttl_seconds or DEFAULT_CLAIM_TTL_SECONDS), 1)
    with write_txn(conn):
        if not _parents_done(conn, task_id):
            conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'", (task_id,))
            _append_event(conn, task_id, "claim_rejected", {"reason": "parents_not_done"})
            return None
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = 'running',
                   claim_lock = ?,
                   claim_expires = ?,
                   started_at = COALESCE(started_at, ?)
             WHERE id = ?
               AND status = 'ready'
               AND claim_lock IS NULL
            """,
            (lock, expires, now, task_id),
        )
        if cur.rowcount != 1:
            return None
        assignee_row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
        run_cur = conn.execute(
            """
            INSERT INTO task_runs(task_id, assignee, status, claim_lock, claim_expires, started_at)
            VALUES (?, ?, 'running', ?, ?, ?)
            """,
            (task_id, assignee_row["assignee"] if assignee_row else None, lock, expires, now),
        )
        run_id = int(run_cur.lastrowid)
        conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))
        _append_event(conn, task_id, "claimed", {"owner": owner, "lock": lock}, run_id=run_id)
    return get_task(conn, task_id)


def _end_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str,
    outcome: str,
    summary: str | None = None,
    metadata: dict[str, Any] | None = None,
    error: str | None = None,
) -> int | None:
    row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    run_id = row["current_run_id"] if row else None
    if run_id is None:
        return None
    conn.execute(
        """
        UPDATE task_runs
           SET status = ?, outcome = ?, ended_at = ?, summary = ?, metadata = ?, error = ?,
               claim_lock = NULL, claim_expires = NULL
         WHERE id = ?
        """,
        (status, outcome, _now(), summary, _metadata_json(metadata), error, run_id),
    )
    return int(run_id)


def reap_expired_running(conn: sqlite3.Connection) -> list[str]:
    now = _now()
    expired: list[str] = []
    with write_txn(conn):
        rows = conn.execute(
            """
            SELECT id, current_run_id, claim_lock, claim_expires
              FROM tasks
             WHERE status = 'running'
               AND claim_expires IS NOT NULL
               AND claim_expires < ?
             ORDER BY claim_expires ASC
            """,
            (now,),
        ).fetchall()
        for row in rows:
            task_id = str(row["id"])
            run_id = row["current_run_id"]
            reason = (
                f"Kanban claim expired before completion. "
                f"lock={row['claim_lock'] or ''} expired_at={row['claim_expires']}"
            )
            conn.execute(
                """
                UPDATE tasks
                   SET status = 'blocked',
                       claim_lock = NULL,
                       claim_expires = NULL,
                       current_run_id = NULL
                 WHERE id = ?
                   AND status = 'running'
                """,
                (task_id,),
            )
            if run_id is not None:
                conn.execute(
                    """
                    UPDATE task_runs
                       SET status = 'blocked',
                           outcome = 'expired',
                           ended_at = ?,
                           summary = ?,
                           error = ?,
                           claim_lock = NULL,
                           claim_expires = NULL
                     WHERE id = ?
                    """,
                    (now, reason, reason, run_id),
                )
            _append_event(conn, task_id, "expired", {"reason": reason}, run_id=run_id)
            expired.append(task_id)
    return expired


def complete_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    summary: str = "",
    result: str = "",
    metadata: dict[str, Any] | None = None,
) -> bool:
    now = _now()
    with write_txn(conn):
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = 'done',
                   result = ?,
                   completed_at = ?,
                   claim_lock = NULL,
                   claim_expires = NULL
             WHERE id = ?
               AND status IN ('running', 'ready', 'blocked')
            """,
            (result or summary, now, task_id),
        )
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn,
            task_id,
            status="done",
            outcome="completed",
            summary=summary or result,
            metadata=metadata,
        )
        conn.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (task_id,))
        _append_event(
            conn,
            task_id,
            "completed",
            {"summary": (summary or result or "").strip().splitlines()[0][:300] or None},
            run_id=run_id,
        )
    recompute_ready(conn)
    return True


def block_task(conn: sqlite3.Connection, task_id: str, *, reason: str = "") -> bool:
    with write_txn(conn):
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = 'blocked',
                   claim_lock = NULL,
                   claim_expires = NULL
             WHERE id = ?
               AND status IN ('running', 'ready', 'todo')
            """,
            (task_id,),
        )
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn,
            task_id,
            status="blocked",
            outcome="blocked",
            summary=reason,
            error=reason,
        )
        conn.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (task_id,))
        _append_event(conn, task_id, "blocked", {"reason": reason}, run_id=run_id)
    return True


def retry_task(conn: sqlite3.Connection, task_id: str) -> KanbanTask | None:
    next_status = "ready" if _parents_done(conn, task_id) else "todo"
    with write_txn(conn):
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = ?,
                   claim_lock = NULL,
                   claim_expires = NULL,
                   current_run_id = NULL
             WHERE id = ?
               AND status = 'blocked'
            """,
            (next_status, task_id),
        )
        if cur.rowcount != 1:
            return None
        _append_event(conn, task_id, "retried", {"status": next_status})
    return get_task(conn, task_id)


def add_comment(conn: sqlite3.Connection, task_id: str, *, body: str, author: str = "agent") -> int:
    if not (body or "").strip():
        raise ValueError("comment body must not be empty.")
    if get_task(conn, task_id) is None:
        raise FileNotFoundError(f"Task not found: {task_id}")
    with write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_comments(task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (task_id, str(author or "agent").strip() or "agent", str(body).strip(), _now()),
        )
        comment_id = int(cur.lastrowid)
        _append_event(conn, task_id, "commented", {"comment_id": comment_id, "author": author})
    return comment_id


def list_comments(conn: sqlite3.Connection, task_id: str, *, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, task_id, author, body, created_at
          FROM task_comments
         WHERE task_id = ?
         ORDER BY created_at DESC, id DESC
         LIMIT ?
        """,
        (task_id, max(int(limit or 20), 1)),
    ).fetchall()


def list_runs(conn: sqlite3.Connection, task_id: str) -> list[KanbanRun]:
    rows = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY started_at ASC, id ASC",
        (task_id,),
    ).fetchall()
    return [_row_run(row) for row in rows]


def parent_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        (task_id,),
    ).fetchall()
    return [str(row["parent_id"]) for row in rows]


def build_worker_context(conn: sqlite3.Connection, task_id: str) -> str:
    task = get_task(conn, task_id)
    if task is None:
        raise FileNotFoundError(f"Task not found: {task_id}")

    lines = [
        f"# Kanban task {task.id}: {task.title}",
        "",
        f"Assignee: {task.assignee or '(unassigned)'}",
        f"Status: {task.status}",
        "",
    ]
    if task.body.strip():
        lines.extend(["## Body", task.body.strip(), ""])

    runs = [run for run in list_runs(conn, task_id) if run.ended_at is not None]
    if runs:
        lines.append("## Prior attempts")
        for run in runs[-5:]:
            outcome = run.outcome or run.status
            lines.append(f"- run #{run.id}: {outcome}")
            if run.summary:
                lines.append(f"  summary: {run.summary[:1000]}")
            if run.error:
                lines.append(f"  error: {run.error[:500]}")
        lines.append("")

    parents = parent_ids(conn, task_id)
    if parents:
        lines.append("## Parent task results")
        for parent_id in parents:
            parent = get_task(conn, parent_id)
            if parent is None:
                lines.append(f"- {parent_id}: missing")
                continue
            lines.append(f"### {parent.id}: {parent.title} [{parent.status}]")
            latest = [run for run in list_runs(conn, parent_id) if run.outcome == "completed"]
            if latest and latest[-1].summary:
                lines.append(latest[-1].summary[:2000])
            elif parent.result:
                lines.append(parent.result[:2000])
            else:
                lines.append("(no handoff result recorded)")
            lines.append("")

    comments = list(reversed(list_comments(conn, task_id, limit=10)))
    if comments:
        lines.append("## Comment thread")
        for row in comments:
            lines.append(f"comment from {row['author']} at {row['created_at']}:")
            lines.append(str(row["body"])[:1000])
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def task_to_dict(task: KanbanTask) -> dict[str, Any]:
    return asdict(task)


def task_summary_line(task: KanbanTask) -> str:
    assignee = f" @{task.assignee}" if task.assignee else ""
    return f"{task.id}: [{task.status}] prio={task.priority}{assignee} {task.title}"


def task_detail_json(conn: sqlite3.Connection, task: KanbanTask) -> str:
    payload = task_to_dict(task)
    payload["parents"] = parent_ids(conn, task.id)
    payload["runs"] = [asdict(run) for run in list_runs(conn, task.id)]
    payload["comments"] = [
        {
            "id": int(row["id"]),
            "author": row["author"],
            "body": row["body"],
            "created_at": int(row["created_at"]),
        }
        for row in reversed(list_comments(conn, task.id, limit=20))
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def dispatch_once(
    conn: sqlite3.Connection,
    *,
    max_tasks: int = 1,
    cwd: str = "",
    mode: str = "readonly",
    max_steps: int | None = None,
) -> dict[str, Any]:
    expired = reap_expired_running(conn)
    promoted = recompute_ready(conn)
    dispatched: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    ready = list_tasks(conn, status="ready")
    for task in ready[: max(int(max_tasks or 1), 1)]:
        claimed = claim_task(conn, task.id, owner=f"kanban:{task.assignee or 'agent'}")
        if claimed is None:
            continue
        try:
            context = build_worker_context(conn, claimed.id)
            from agent.subagent import delegate_task

            raw_result = delegate_task(
                goal=f"Complete kanban task {claimed.id}: {claimed.title}",
                context=context,
                cwd=cwd,
                mode=mode,
                max_steps=max_steps,
            )
            status = "ok"
            summary = raw_result
            try:
                payload = json.loads(raw_result)
                results = payload.get("results") if isinstance(payload, dict) else None
                if isinstance(results, list) and results:
                    status = str(results[0].get("status") or "ok")
                    summary = str(results[0].get("summary") or raw_result)
            except json.JSONDecodeError:
                pass
            if status == "error":
                block_task(conn, claimed.id, reason=summary)
                errors.append({"task_id": claimed.id, "error": summary})
            else:
                complete_task(
                    conn,
                    claimed.id,
                    summary=summary,
                    result=raw_result,
                    metadata={"dispatcher": "kanban_lite", "mode": mode},
                )
                dispatched.append({"task_id": claimed.id, "status": "done", "summary": summary[:300]})
        except Exception as exc:
            reason = str(exc)
            block_task(conn, claimed.id, reason=reason)
            errors.append({"task_id": claimed.id, "error": reason})
    return {"expired": expired, "promoted": promoted, "dispatched": dispatched, "errors": errors}

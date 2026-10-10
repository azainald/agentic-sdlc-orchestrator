"""Run persistence.

Both stores keep a run as its own JSON (``Run.model_dump_json``). That gives
two properties for free: a stored run is always a *copy* (callers can never
mutate stored state by accident), and the schema lives in one place (the
pydantic models) instead of being mirrored in SQL columns.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Optional

from .models import Run, now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id         TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    scenario   TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    body       TEXT NOT NULL
)
"""

_UPSERT = """
INSERT INTO runs (id, state, scenario, created_at, updated_at, body)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    state = excluded.state,
    scenario = excluded.scenario,
    updated_at = excluded.updated_at,
    body = excluded.body
"""


class SqliteRunStore:
    """SQLite-backed RunStore. ``path`` is a file path or ``":memory:"``.

    One connection is shared across threads (``check_same_thread=False``) and
    every statement runs under a lock, so concurrent callers are serialised
    instead of tripping over sqlite's per-connection thread rules.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        if path != ":memory:":
            # WAL: readers don't block the writer and a crash can't corrupt the file.
            self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            self._conn.execute(_SCHEMA)

    def save_run(self, run: Run) -> None:
        row = (run.id, run.state.value, run.scenario, run.created_at, now(), run.model_dump_json())
        with self._lock, self._conn:  # `with conn` = one transaction
            self._conn.execute(_UPSERT, row)

    def load_run(self, run_id: str) -> Optional[Run]:
        with self._lock:
            row = self._conn.execute("SELECT body FROM runs WHERE id = ?", (run_id,)).fetchone()
        return Run.model_validate_json(row[0]) if row else None

    def list_runs(self) -> list[Run]:
        with self._lock:
            rows = self._conn.execute("SELECT body FROM runs ORDER BY created_at, id").fetchall()
        return [Run.model_validate_json(body) for (body,) in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class InMemoryRunStore:
    """Dict-backed RunStore for tests and the CLI. Stores JSON, not objects."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, str] = {}

    def save_run(self, run: Run) -> None:
        body = run.model_dump_json()
        with self._lock:
            self._runs[run.id] = body

    def load_run(self, run_id: str) -> Optional[Run]:
        with self._lock:
            body = self._runs.get(run_id)
        return Run.model_validate_json(body) if body is not None else None

    def list_runs(self) -> list[Run]:
        with self._lock:
            bodies = list(self._runs.values())
        return sorted((Run.model_validate_json(b) for b in bodies), key=lambda r: (r.created_at, r.id))

    def close(self) -> None:
        pass

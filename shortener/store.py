"""Storage layer.

UrlRepository is the interface the rest of the app depends on. UrlStore is the
default implementation: a dict in memory, optionally saved to a JSON file.
To use a real database, write another class with the same methods.
"""
import dataclasses
import json
import os
import threading
from pathlib import Path
from typing import Protocol

from shortener.clock import Clock, system_clock
from shortener.records import Click, IdempotencyEntry, UrlRecord


class UrlRepository(Protocol):
    def add(self, record: UrlRecord) -> bool:
        """Insert a new record. Return False (and do nothing) if the code exists."""

    def get(self, code: str) -> UrlRecord | None: ...

    def update(self, code: str, **changes) -> UrlRecord | None:
        """Replace fields of a record (e.g. url, expires_at). None if unknown."""

    def soft_delete(self, code: str) -> UrlRecord | None:
        """Mark the record as deleted (it is kept). None if unknown."""

    def add_click(self, code: str, click: Click) -> None: ...

    def get_clicks(self, code: str) -> list[Click]: ...

    def get_idempotency(self, key: str) -> IdempotencyEntry | None: ...

    def put_idempotency(self, key: str, entry: IdempotencyEntry) -> None: ...


class UrlStore:
    """In-memory store with optional JSON-file persistence. Thread-safe.

    When `path` is given, existing data is loaded at start-up and the whole
    file is rewritten after every change (simple and crash-safe, but O(size)
    per write: fine for a demo, not for heavy traffic).
    """

    def __init__(self, path: str | os.PathLike | None = None, clock: Clock = system_clock):
        self._path = Path(path) if path else None
        self._clock = clock
        self._lock = threading.RLock()
        self._urls: dict[str, UrlRecord] = {}
        self._clicks: dict[str, list[Click]] = {}
        self._idempotency: dict[str, IdempotencyEntry] = {}
        if self._path and self._path.exists():
            self._load()

    # --- repository methods ---------------------------------------------------
    def add(self, record: UrlRecord) -> bool:
        with self._lock:
            if record.code in self._urls:
                return False
            self._urls[record.code] = record
            self._save()
            return True

    def get(self, code: str) -> UrlRecord | None:
        with self._lock:
            return self._urls.get(code)

    def update(self, code: str, **changes) -> UrlRecord | None:
        with self._lock:
            record = self._urls.get(code)
            if record is None:
                return None
            record = dataclasses.replace(record, **changes)
            self._urls[code] = record
            self._save()
            return record

    def soft_delete(self, code: str) -> UrlRecord | None:
        return self.update(code, deleted_at=self._clock())

    def add_click(self, code: str, click: Click) -> None:
        with self._lock:
            self._clicks.setdefault(code, []).append(click)
            self._save()

    def get_clicks(self, code: str) -> list[Click]:
        with self._lock:
            return list(self._clicks.get(code, []))

    def get_idempotency(self, key: str) -> IdempotencyEntry | None:
        with self._lock:
            return self._idempotency.get(key)

    def put_idempotency(self, key: str, entry: IdempotencyEntry) -> None:
        with self._lock:
            self._idempotency[key] = entry
            self._save()

    # --- persistence ------------------------------------------------------------
    def _save(self) -> None:
        if self._path is None:
            return
        data = {
            "urls": {code: dataclasses.asdict(r) for code, r in self._urls.items()},
            "clicks": {
                code: [dataclasses.asdict(c) for c in clicks]
                for code, clicks in self._clicks.items()
            },
            "idempotency": {
                key: dataclasses.asdict(entry) for key, entry in self._idempotency.items()
            },
        }
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        tmp_path.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp_path, self._path)  # atomic: readers never see a half-written file

    def _load(self) -> None:
        data = json.loads(self._path.read_text(encoding="utf-8"))
        self._urls = {code: UrlRecord(**r) for code, r in data.get("urls", {}).items()}
        self._clicks = {
            code: [Click(**c) for c in clicks] for code, clicks in data.get("clicks", {}).items()
        }
        self._idempotency = {
            key: IdempotencyEntry(entry["fingerprint"], UrlRecord(**entry["record"]))
            for key, entry in data.get("idempotency", {}).items()
        }

"""Egress JSONL writer and daily token counter (impl 10 U10-57; design 10 §4.5).

One line per decision in ``<logs>/egress-<YYYY-MM-DD>.jsonl`` (UTC date of ``ts``). No line
ever carries payload text (TH10-22); locking and appending reuse the audit log's U10-61
helpers, so several processes share one cross-process lock file (TH10-19, TH10-20).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from herness.core import time as clock
from herness.core.audit import append_jsonl_locked, log_lock
from herness.core.errors import SchemaViolation
from herness.core.ids import canonical_json

__all__ = ["EgressLog"]

# Design 10 §4.5 line keys (``reason`` included); ``ts``, ``profile``, ``config_hash`` are added.
LINE_KEYS: Final = frozenset({
    "egress_id", "decision", "reason", "purpose", "payload_class", "destination", "method",
    "path", "run_id", "task_id", "bytes_out", "bytes_in", "tokens_in", "tokens_out",
    "payload_sha256", "scan_hits", "status_code", "latency_ms",
})  # fmt: skip
_DECISIONS: Final = frozenset({"allowed", "blocked", "completed"})
_LOCK_NAME: Final = ".egress.lock"
MAX_REMEMBERED: Final = 100_000  # allowed-line map entries per day (U10-57 limit)


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class EgressLog:
    """Append egress lines and keep today's token total incrementally (U10-57)."""

    def __init__(self, logs_dir: Path, config_hash: str | None, profile: str) -> None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        self._dir = logs_dir
        self._config_hash = config_hash
        self._profile = profile
        self._lock_path = logs_dir / _LOCK_NAME
        self._held = threading.local()  # set while this thread is inside ``locked()``
        self._mutex = threading.Lock()  # guards the in-memory counter below
        self._day: str | None = None
        self._offset = 0
        self._total = 0
        self._allowed: dict[str, int] = {}

    def _holding(self) -> bool:
        return bool(getattr(self._held, "on", False))

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the cross-process egress lock (``StoreBusy`` after the U10-61 timeout)."""
        with log_lock(self._lock_path):
            self._held.on = True
            try:
                yield
            finally:
                self._held.on = False

    def _path(self, day: str) -> Path:
        return self._dir / f"egress-{day}.jsonl"

    def write(self, line: dict[str, Any]) -> None:
        """Append ``line`` with ``ts``, ``profile`` and ``config_hash``; one shape per line.

        Every line carries exactly the §4.5 keys (null where not yet known) and a known
        ``decision``; anything else is a ``SchemaViolation``.
        """
        if line.keys() != LINE_KEYS or line["decision"] not in _DECISIONS:
            msg = "egress line keys invalid"  # key names are never echoed (ENG §3.4)
            raise SchemaViolation(msg)
        now = clock.now()
        record = dict(line)
        record["ts"] = clock.format_utc(now)[:23] + "Z"  # milliseconds (§4.5)
        record["profile"] = self._profile
        record["config_hash"] = self._config_hash
        data = (canonical_json(record) + "\n").encode("utf-8")
        append_jsonl_locked(
            self._path(clock.utc_day(now)),
            lambda _prev: data,
            lock_path=self._lock_path,
            lock_held=self._holding(),
        )

    def tokens_today(self, now: datetime) -> int:
        """Tokens of today's ``allowed`` and ``completed`` lines, read incrementally."""
        day = clock.utc_day(now)
        with self._mutex:
            if day != self._day:
                self._day, self._offset, self._total = day, 0, 0
                self._allowed.clear()
            try:
                with self._path(day).open("rb") as fh:
                    fh.seek(self._offset)
                    chunk = fh.read()
            except FileNotFoundError:
                return self._total
            end = chunk.rfind(b"\n") + 1  # only whole lines; a partial tail is read later
            for raw in chunk[:end].splitlines():
                self._account(raw)
            self._offset += end
            return self._total

    def _account(self, raw: bytes) -> None:
        try:
            record = json.loads(raw)
        except ValueError:
            return  # a torn or foreign line adds nothing
        if not isinstance(record, dict):
            return
        egress_id, decision = record.get("egress_id"), record.get("decision")
        tokens_in = _as_int(record.get("tokens_in"))
        if decision == "allowed":
            self._total += tokens_in
            if isinstance(egress_id, str) and len(self._allowed) < MAX_REMEMBERED:
                self._allowed[egress_id] = tokens_in
        elif decision == "completed":
            earlier = self._allowed.pop(egress_id, 0) if isinstance(egress_id, str) else 0
            self._total += tokens_in + _as_int(record.get("tokens_out")) - earlier

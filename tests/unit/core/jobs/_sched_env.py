"""Shared fixture and helpers of the scheduler tests (impl 08 §11 `ops_db`, T08-14).

`sched_db`: the `jobs_db` store and backends, reloaded with a `sources.yaml` that has `jira`
enabled (sync and reconcile schedules) and `servicenow` disabled, a settable fake clock and an
empty missed-report set. Local times are in the shipped business timezone
(`America/New_York`).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from tests.unit.core.jobs import _queue_env

from herness.core import config as c
from herness.core import redact as r
from herness.core import time as clock
from herness.core.jobs import scheduler
from herness.core.jobs.ports import JobRow
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState
from herness.core.settings import RedactionConfig
from herness.store.ops.core import read_all, run_write

jobs_db = _queue_env.jobs_db  # fixture, re-exported for the test modules
TZ = ZoneInfo("America/New_York")
SOURCES_YAML = """\
version: 1
sources:
  jira:
    enabled: true
    flavor: cloud
    base_url: https://acme.atlassian.net
    auth: {method: api_token, credentials: "secret:jira"}
    schedule: "*/30 * * * *"
    reconcile: {schedule: "0 3 * * SUN"}
  servicenow:
    enabled: false
    base_url: https://corp.service-now.com
    auth: {method: basic, credentials: "secret:sn"}
    schedule: "*/15 * * * *"
    entities:
      incident: {fields: [number]}
"""


class Clock:
    """Settable `clock.now` for `submit`, `claim` and events."""

    def __init__(self) -> None:
        self.current = datetime(2026, 9, 1, tzinfo=UTC)

    def set(self, value: datetime) -> datetime:
        self.current = value
        return value


def local(day: tuple[int, int, int], hhmm: str) -> datetime:
    """The aware UTC instant of local ``hhmm`` on ``day`` in the business timezone."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(*day, hour, minute, tzinfo=TZ).astimezone(UTC)


def reload_config(config_dir: Path, sources: str = SOURCES_YAML, **extra: str) -> None:
    """Rewrite ``sources.yaml`` (and any other owner file in ``extra``) and reload."""
    (config_dir / "sources.yaml").write_text(sources, encoding="utf-8")
    for stem, text in extra.items():
        (config_dir / f"{stem}.yaml").write_text(text, encoding="utf-8")
    c.reset_config()  # also drops the cached redactor: set the fixed-key one again
    c.init_config("local", config_dir=config_dir, env={})
    directory = NameDirectory.from_files(None, (), None)
    r._State.redactor = r.Redactor(
        RedactionConfig(directory_file=None), bytes(range(32)), directory
    )


@pytest.fixture
def sched_db(
    jobs_db: ProcessState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Clock]:
    """`jobs_db` plus the jira/servicenow sources, a fake clock and no missed reports."""
    del jobs_db
    reload_config(tmp_path / "config")
    fake = Clock()
    monkeypatch.setattr(clock, "now", lambda: fake.current)
    scheduler._missed_reported.clear()
    yield fake
    scheduler._missed_reported.clear()


def jobs(schedule: str | None = None) -> list[JobRow]:
    """Every job row, oldest first, optionally only those of ``schedule``."""
    rows = [
        JobRow.model_validate(dict(row)) for row in read_all("SELECT * FROM job ORDER BY rowid")
    ]
    if schedule is None:
        return rows
    return [row for row in rows if row.payload.get("schedule") == schedule]


def events(kind: str) -> list[dict[str, Any]]:
    """`resilience_event` rows of ``kind`` (target, job_id and detail text)."""
    sql = "SELECT target, job_id, detail FROM resilience_event WHERE kind = ? ORDER BY rowid"
    return [dict(row) for row in read_all(sql, (kind,))]


def finish(job_id: str, status: str, at: datetime) -> None:
    """Force a job to a final ``status`` finished at ``at`` (a handler outcome stand-in)."""

    def write(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE job SET status = ?, finished_at = ?, lease_owner = NULL,"
            " lease_expires_at = NULL WHERE job_id = ?",
            (status, clock.format_utc(at), job_id),
        )

    run_write(write, op="test_finish")

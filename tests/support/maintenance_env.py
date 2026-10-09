"""`maint_env` fixture and data-tree planters for the backup and purge tests (impl 10 T10-20).

Spec 11 ``tiny_build`` is not in the tree yet, so each test plants its own files under the
``ops_store`` data root (U11-78). ``paths.logs`` and ``paths.backup_target`` are pointed at
``<data>/logs`` and ``<tmp>/backup`` through the environment, as ``ops_store`` does for
``paths.data``.
"""

from __future__ import annotations

import datetime
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.support.ops_store import OpsStoreHandle

from herness.core.config import HernessConfig, get_config, reset_config
from herness.core.jobs.ports import JobContext


@dataclass
class FakeCtx:
    """The ``JobContext`` parts the maintenance units use: ``job.payload`` and ``heartbeat``."""

    payload: dict[str, Any] = field(default_factory=dict)
    beats: int = 0

    @property
    def job(self) -> Any:
        return SimpleNamespace(payload=self.payload)

    def heartbeat(self, note: str | None = None) -> None:
        del note
        self.beats += 1

    def as_ctx(self) -> JobContext:
        return cast("JobContext", self)


@dataclass(frozen=True)
class MaintEnv:
    """Config and paths of one backup/purge test."""

    data: Path
    logs: Path
    target: Path

    @property
    def cfg(self) -> HernessConfig:
        return get_config()


@pytest.fixture
def maint_env(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[MaintEnv]:
    """A migrated ops store plus ``paths.logs`` and an existing ``paths.backup_target``."""
    logs, target = ops_store.data_root / "logs", tmp_path / "backup"
    logs.mkdir()
    target.mkdir()
    monkeypatch.setenv("HERNESS_PATHS__LOGS", str(logs))
    monkeypatch.setenv("HERNESS_PATHS__BACKUP_TARGET", str(target))
    reset_config()
    yield MaintEnv(ops_store.data_root, logs, target)
    reset_config()


def plant(path: Path, *, mtime: datetime.datetime | None = None, body: bytes = b"x") -> Path:
    """Write ``path`` (parents created) and set its mtime when given."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    if mtime is not None:
        stamp = mtime.timestamp()
        os.utime(path, (stamp, stamp))
    return path


def set_mtime(path: Path, mtime: datetime.datetime) -> None:
    stamp = mtime.timestamp()
    os.utime(path, (stamp, stamp))


def admin_actions(logs: Path, action: str) -> list[dict[str, Any]]:
    """The ``fields`` of every ``admin_action`` audit line with ``action``, oldest first."""
    found: list[dict[str, Any]] = []
    for file in sorted(logs.glob("audit-*.jsonl")):
        for line in file.read_text(encoding="utf-8").splitlines():
            if not line.startswith('{"'):
                continue  # a planted fixture line, not an audit record
            record = json.loads(line)
            fields = record.get("fields") or {}
            if record.get("event") == "admin_action" and fields.get("action") == action:
                found.append(record["fields"])
    return found


def parse_counts(text: str) -> dict[str, int]:
    """``k=v;k=v`` audit counts as a dict."""
    return {k: int(v) for k, v in (item.split("=") for item in text.split(";"))}

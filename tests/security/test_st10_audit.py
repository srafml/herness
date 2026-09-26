"""Security tests for the audit log (impl 10 ST10-17, ST10-18; TH10-07, TH10-08, TH10-09).

ST10-17 names the doctor ``audit_chain`` check, which is wired by a later ``herness.admin``
card; here the check's engine, ``verify_chain``, must fail and name the broken line.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config

from herness.core import audit as a
from herness.core import config as c
from herness.core.errors import SchemaViolation

pytestmark = pytest.mark.unit

USER = "fedcba9876543210fedcba9876543210"
SECRET = "Zq8-known-secret-value-41"  # noqa: S105 - test sentinel
OLD = (1_700_000_000, 1_700_000_000)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    c.reset_config()


@pytest.fixture
def logs(tmp_path: Path) -> Path:
    cfg = c.init_config(config_dir=write_full_config(tmp_path), env={})
    return cfg.paths.logs


def _five_lines(logs: Path) -> Path:
    for n in range(5):
        a.audit("admin_action", USER, action="purge", target=f"t{n}", counts=f"rows={n}")
    (path,) = logs.glob("audit-*.jsonl")
    return path


def _rewrite(path: Path, lines: list[bytes], tail: bytes = b"\n") -> None:
    path.write_bytes(b"\n".join(lines) + tail)
    os.utime(path, OLD)


def test_st10_17_tampering_breaks_chain_naming_the_line(logs: Path) -> None:
    """ST10-17 edit, delete, reorder and truncate each fail verify_chain at file:line."""
    path = _five_lines(logs)
    name = path.name
    orig = path.read_bytes().splitlines()
    assert a.verify_chain(logs).ok
    edited = orig[1].replace(b'"t1"', b'"tX"')
    attacks = [
        ([orig[0], edited, *orig[2:]], b"\n", f"{name}:3"),
        ([orig[0], *orig[2:]], b"\n", f"{name}:2"),
        ([orig[0], orig[2], orig[1], *orig[3:]], b"\n", f"{name}:2"),
        ([*orig[:4], orig[4][:20]], b"", f"{name}:5"),
    ]
    for lines, tail, where in attacks:
        _rewrite(path, lines, tail)
        report = a.verify_chain(logs)
        assert not report.ok
        assert report.first_break == where


def test_st10_17_truncated_earlier_day_breaks_next_day(
    logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST10-17 dropping the tail of an older file breaks the next file's first line."""
    day = [datetime.datetime(2026, 9, 20, 8, 0, tzinfo=datetime.UTC)]
    monkeypatch.setattr(a.clock, "now", lambda: day[0])
    first = _five_lines(logs)
    day[0] += datetime.timedelta(days=1)
    a.audit("egress", "system", egress_id="egr_1", reason="blocked")
    _rewrite(first, first.read_bytes().splitlines()[:3])
    assert a.verify_chain(logs).first_break == "audit-2026-09-21.jsonl:1"


def test_st10_18_known_secret_in_detail_refused(
    logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST10-18 audit(..., detail=<known secret>) raises SchemaViolation; nothing written."""
    monkeypatch.setattr(a, "_known_values", lambda: frozenset({SECRET}))
    with pytest.raises(SchemaViolation) as exc:
        a.audit("admin_action", USER, action="secret_set", target="snow.token", detail=SECRET)
    assert SECRET not in str(exc.value)
    assert SECRET not in repr(dict(exc.value.context)) + repr(dict(exc.value.details))
    assert not list(logs.glob("audit-*.jsonl"))

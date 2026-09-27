"""Tests for herness.connectors.jobs.build_sync_payload: `herness sync` options to a job
(impl 01 U01-54; T01-11, TH01-07). Pure: no config, store or registry."""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from herness.connectors.jobs import ReconcilePayload, SyncPayload, build_sync_payload
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

TODAY = datetime.date(2026, 3, 1)
JAN1, FEB1 = datetime.date(2026, 1, 1), datetime.date(2026, 2, 1)


def _build(source: str | None, **options: Any) -> tuple[str, dict[str, object], str]:
    return build_sync_payload(source, today=TODAY, **options)


@pytest.mark.parametrize(
    ("source", "options", "expected"),
    [
        (
            None,
            {},
            ("sync", {"source": None, "entities": None, "mode": "incremental"}, "sync:all"),
        ),
        (
            "jira",
            {},
            ("sync", {"source": "jira", "entities": None, "mode": "incremental"}, "sync:jira"),
        ),
        (
            "servicenow",
            {"entities": ["incident", "problem"]},
            (
                "sync",
                {
                    "source": "servicenow",
                    "entities": ["incident", "problem"],
                    "mode": "incremental",
                },
                "sync:servicenow",
            ),
        ),
        (
            "servicenow",
            {"backfill": True, "from_": JAN1, "to": FEB1},
            (
                "sync",
                {
                    "source": "servicenow",
                    "entities": None,
                    "mode": "backfill",
                    "start": "2026-01-01",
                    "end": "2026-02-01",
                },
                "sync:servicenow:backfill:2026-01-01:2026-02-01",
            ),
        ),
        (
            None,
            {"backfill": True, "from_": JAN1},
            (
                "sync",
                {
                    "source": None,
                    "entities": None,
                    "mode": "backfill",
                    "start": "2026-01-01",
                    "end": "2026-03-01",
                },
                "sync:all:backfill:2026-01-01:2026-03-01",
            ),
        ),
        (
            "jira",
            {"full": True},
            (
                "sync",
                {
                    "source": "jira",
                    "entities": None,
                    "mode": "backfill",
                    "start": None,
                    "end": None,
                },
                "sync:jira:backfill:None:None",
            ),
        ),
        (
            "files",
            {"reconcile": True, "entities": ["roster"]},
            ("reconcile", {"source": "files", "entities": ["roster"]}, "reconcile:files"),
        ),
        (
            "jira",
            {"reconcile": True},
            ("reconcile", {"source": "jira", "entities": None}, "reconcile:jira"),
        ),
    ],
)
def test_ut01_57_option_matrix(
    source: str | None, options: dict[str, Any], expected: tuple[str, dict[str, object], str]
) -> None:
    """UT01-57 each option combination maps to its job kind, payload and idem key."""
    kind, payload, key = _build(source, **options)
    assert (kind, payload, key) == expected
    model = ReconcilePayload if kind == "reconcile" else SyncPayload
    model.model_validate(payload)  # postcondition: the handler accepts it


@pytest.mark.parametrize(
    ("source", "options", "names"),
    [
        (None, {"entities": ["incident"]}, "--entities"),
        ("jira", {"reconcile": True, "full": True}, "--reconcile"),
        ("jira", {"reconcile": True, "backfill": True, "from_": JAN1}, "--reconcile"),
        (None, {"reconcile": True}, "--reconcile"),
        ("jira", {"reconcile": True, "from_": JAN1}, "--reconcile"),
        ("jira", {"full": True, "backfill": True, "from_": JAN1}, "--full"),
        ("jira", {"backfill": True}, "--from"),
        ("jira", {"backfill": True, "from_": FEB1, "to": JAN1}, "--from"),
        ("jira", {"backfill": True, "from_": JAN1, "to": JAN1}, "--from"),
        (
            "jira",
            {"backfill": True, "from_": JAN1, "to": TODAY + datetime.timedelta(days=1)},
            "--to",
        ),
        ("jira", {"backfill": True, "from_": TODAY}, "--from"),
        ("jira", {"from_": JAN1}, "--from"),
        ("jira", {"to": FEB1}, "--to"),
        ("jira", {"full": True, "to": FEB1}, "--to"),
        ("Jira!", {}, "source"),
        ("jira", {"entities": ["BAD NAME"]}, "--entities"),
    ],
)
def test_ut01_57_conflicts_are_config_errors(
    source: str | None, options: dict[str, Any], names: str
) -> None:
    """UT01-57 conflicting or invalid options → ConfigError naming the options."""
    with pytest.raises(ConfigError) as info:
        _build(source, **options)
    assert names in info.value.message

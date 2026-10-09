"""Tests for ``cmd_maintenance``, ``register_handlers`` and ``handle_maintenance``.

Impl 10 U10-73 to U10-75 and U10-104 (UT10-68, R-42, R-44, R-45). Enqueue shape is checked
against recording stand-ins for ``enqueue``/``worker_alive``; registration and the inline path
run on the real ops store (``worker_env``) and the real handler registry.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.support.worker_env import WorkerEnv, worker_env  # noqa: F401 - fixture import

from herness import admin
from herness.admin import commands_data
from herness.admin.commands_data import cmd_maintenance, cmd_privacy_delete
from herness.admin.maintenance import handle_maintenance
from herness.core import time as clock
from herness.core.errors import ConfigError, FatalError
from herness.core.jobs import resolve_handler
from herness.core.jobs.ports import JobContext, require_jobs_backend
from herness.core.resilience._state import process_state
from herness.core.types import JobOutcome

pytestmark = pytest.mark.unit

NO_WORKER = "no worker is running; start it with `herness worker` or rerun with `--inline`"
RID = "servicenow:incident:INC0001"


def _purge(_record_id: str) -> int:
    return 0


def _ctx(payload: dict[str, object]) -> JobContext:
    return cast("JobContext", SimpleNamespace(job=SimpleNamespace(payload=payload)))


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def enqueue(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append((args, kwargs))
        return f"job_{len(self.calls)}"


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(commands_data, "enqueue", recorder.enqueue)
    monkeypatch.setattr(commands_data, "worker_alive", lambda: True)
    return recorder


# --- handle_maintenance -------------------------------------------------------------------


def test_ut10_68_unknown_action_is_config_error() -> None:
    """UT10-68 an action outside the table raises ConfigError("unknown maintenance action")."""
    payloads: list[dict[str, object]] = [{"action": "nope"}, {}, {"action": 7}]
    for payload in payloads:
        with pytest.raises(ConfigError) as caught:
            handle_maintenance(_ctx(payload), memory_purge=_purge)
        assert str(caught.value) == "unknown maintenance action"


@pytest.mark.parametrize(
    ("action", "owner"),
    [("privacy_delete", "T10-29"), ("rekey", "T10-30")],
)
def test_ut10_68_table_routes_unbuilt_actions_to_typed_fatal_error(action: str, owner: str) -> None:
    """UT10-68 each action reaches its table entry; unbuilt owners raise FatalError (not
    ConfigError) naming the action and the owner card."""
    with pytest.raises(FatalError) as caught:
        handle_maintenance(_ctx({"action": action}), memory_purge=_purge)
    assert type(caught.value) is FatalError
    assert action in str(caught.value)
    assert owner in str(caught.value)


def test_ut10_68_unit_result_is_wrapped_or_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-68 counts become JobOutcome(done, counts); a unit's own JobOutcome is returned as is;
    memory_purge reaches the unit."""
    seen: list[object] = []

    def counts(_ctx: JobContext, purge: Callable[[str], int]) -> dict[str, Any]:
        seen.append(purge)
        return {"n": 2}

    own = JobOutcome(status="yield")
    table = {"backup": counts, "purge": lambda _c, _p: own}
    monkeypatch.setattr("herness.admin.maintenance._ACTIONS", table)
    assert handle_maintenance(_ctx({"action": "backup"}), memory_purge=_purge) == JobOutcome(
        status="done", result={"n": 2}
    )
    assert seen == [_purge]
    assert handle_maintenance(_ctx({"action": "purge"}), memory_purge=_purge) is own


# --- cmd_maintenance / cmd_privacy_delete -------------------------------------------------


def test_ut10_68_cmd_maintenance_enqueue_shape(rec: Recorder) -> None:
    """UT10-68 payload, gpu class, priority 80 and the dated idem_key are exact."""
    day = clock.now().strftime("%Y-%m-%d")
    result = cmd_maintenance("backup", dry_run=True, actor="ops")
    assert rec.calls == [
        (
            ("maintenance", {"action": "backup", "dry_run": True, "requested_by": "ops"}, "none"),
            {"priority": 80, "idem_key": f"maintenance:backup:manual:{day}"},
        )
    ]
    assert (result.ok, result.data, result.warnings, result.exit_code) == (
        True,
        {"job_id": "job_1"},
        [],
        0,
    )


def test_ut10_68_no_worker_warning_verbatim(rec: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-68 / R-44: worker_alive false adds the warning text verbatim, for both commands."""
    monkeypatch.setattr(commands_data, "worker_alive", lambda: False)
    assert cmd_maintenance("purge", dry_run=False, actor="ops").warnings == [NO_WORKER]
    assert cmd_privacy_delete([RID], reason_ref="DSR-1", actor="ops").warnings == [NO_WORKER]
    assert len(rec.calls) == 2


def test_ut10_68_cmd_privacy_delete_enqueue_shape(rec: Recorder) -> None:
    """UT10-68 one job per record with the exact payload, priority and idem_key."""
    ids = [RID, "jira:issue:ABC-1"]
    result = cmd_privacy_delete(ids, reason_ref="DSR-7", actor="ops")
    assert rec.calls == [
        (
            (
                "maintenance",
                {
                    "action": "privacy_delete",
                    "record_id": rid,
                    "reason_ref": "DSR-7",
                    "requested_by": "ops",
                },
                "none",
            ),
            {"priority": 80, "idem_key": "privacy_delete:" + rid},
        )
        for rid in ids
    ]
    assert result.data == {
        "jobs": [{"record_id": ids[0], "job_id": "job_1"}, {"record_id": ids[1], "job_id": "job_2"}]
    }


# --- register_handlers (R-42) -------------------------------------------------------------


def test_ut10_68_registered_handler_takes_ctx_only(
    worker_env: WorkerEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT10-68 / R-42: the resolved handler is called with ctx alone and dispatches; calling
    register_handlers again with the same memory_purge is a no-op, a different one raises."""
    del worker_env
    monkeypatch.delitem(process_state().handlers, "maintenance")
    admin.register_handlers(memory_purge=_purge)
    handler = resolve_handler("maintenance")
    assert isinstance(handler, functools.partial)
    assert handler.func is handle_maintenance
    with pytest.raises(ConfigError, match="unknown maintenance action"):
        handler(_ctx({"action": "bogus"}))
    admin.register_handlers(memory_purge=_purge)
    assert resolve_handler("maintenance") is handler
    with pytest.raises(ConfigError, match="already registered"):
        admin.register_handlers(memory_purge=lambda _rid: 1)


def test_ut10_68_inline_runs_through_registered_handler(
    worker_env: WorkerEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT10-68 / R-45: inline=True on the real ops store runs the job in-process; the dry-run
    purge unit (T10-20) returns its counts as the job result."""
    del worker_env
    monkeypatch.delitem(process_state().handlers, "maintenance")
    admin.register_handlers(memory_purge=_purge)
    result = cmd_maintenance("purge", dry_run=True, actor="ops", inline=True)
    outcome = cast("dict[str, Any]", result.data["outcome"])
    assert outcome["status"] == "done"
    assert outcome["result"]["lake_partitions"] == 0
    assert "chat_sessions" not in outcome["result"]  # dry run: chat is not counted
    rows = require_jobs_backend().list_jobs(status=None, kind="maintenance", limit=10)
    assert [r.payload["action"] for r in rows] == ["purge"]
    assert rows[0].priority == 80
    assert rows[0].status == "done"

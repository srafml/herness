"""Source-side helpers of the impl 08 fault suite (T08-24: FT08-04, FT08-05).

FT08-04 runs the production Jira connector over the recorded `cloud_sync_it` cassette (the
FT01-04 set-up, `httpx2.MockTransport`, no socket); FT08-05 runs the `sync` job handler
(`handle_sync`) over the ServiceNow cassette host (the FT01-06 set-up). The faults come only
from the caller's `fault_env` plan (R-40); `rule_calls` reads how often a plan rule matched,
which is how often the fault point (one per HTTP attempt) was reached.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest
from tests.support import jira_pages
from tests.support.build_harness import FakeJobContext
from tests.support.jira_pages import CLOUD_SEARCH
from tests.support.sn_cassettes import FakeServiceNow
from tests.unit.connectors import _servicenow_env as sn
from tests.unit.connectors._http_data import bind_resilience, mock_client
from tests.unit.connectors._jira_data import NOW, Replay, cassette, settings

import herness.connectors._write_loop as write_loop
from herness.connectors.http import SourceHttp
from herness.connectors.jira import JiraConnector, build_jql
from herness.connectors.jobs import handle_sync
from herness.connectors.runner import SyncRunner
from herness.core.resilience import process_state
from herness.core.resilience.faults import FaultPlan
from herness.core.types import JobOutcome
from herness.store.ops import read_all, set_watermark

__all__ = ["NOW", "bind_resilience"]

FIELD: Final = "updated"
JIRA_WM: Final = datetime.datetime(2026, 9, 1, 8, 0, tzinfo=datetime.UTC)
_SETTLED: Final = NOW - datetime.timedelta(seconds=60)  # until = NOW - settle_seconds


def events(kind: str) -> list[dict[str, Any]]:
    """Stored `resilience_event` rows of `kind`: ts, target and the decoded detail."""
    rows = read_all(
        "SELECT ts, target, detail FROM resilience_event WHERE kind = ? ORDER BY ts, event_id",
        (kind,),
    )
    return [{"ts": r["ts"], "target": r["target"], **json.loads(str(r["detail"]))} for r in rows]


def rule_calls(index: int = 0) -> int:
    """How many fault-point calls matched rule `index` of the loaded plan."""
    plan = process_state().fault_plan
    assert isinstance(plan, FaultPlan)
    return plan.counters[index]


def jira_runner(data_root: Path) -> tuple[SyncRunner, Replay]:
    """A Jira runner (fixed clock `NOW`) replaying `cloud_sync_it` for the window it asks."""
    interactions = cassette("cloud_sync_it.json")
    since = JIRA_WM - datetime.timedelta(minutes=60)
    jql = build_jql(since=since, until=_SETTLED, scope=jira_pages.SCOPE)
    for step in interactions:
        if step["request"]["path"] == CLOUD_SEARCH:
            step["request"]["json"]["jql"] = jql
    replay = Replay(interactions)
    cfg = settings(fetch_remote_links=True)
    http = SourceHttp(mock_client(replay, jira_pages.BASE_URL), breaker_key="jira", auth=None)
    conn = JiraConnector(
        cfg, custom_field_ids=jira_pages.SYNTH_FIELD_IDS, http=http, clock=lambda: NOW
    )
    set_watermark("jira", "issue", FIELD, JIRA_WM, now=NOW)
    return SyncRunner(conn, cfg, clock=lambda: NOW, data_root=data_root), replay


def count_watermark_writes(monkeypatch: pytest.MonkeyPatch) -> list[datetime.datetime]:
    """Record every value the write loop commits as a watermark."""
    sets: list[datetime.datetime] = []
    real = write_loop.set_watermark

    def counting(source: str, entity: str, field: str, value: datetime.datetime, **kw: Any) -> Any:
        sets.append(value)
        return real(source, entity, field, value, **kw)

    monkeypatch.setattr(write_loop, "set_watermark", counting)
    return sets


def servicenow_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: datetime.datetime
) -> tuple[Callable[[], JobOutcome], FakeServiceNow]:
    """The `sync` job of source `servicenow` over the cassette host, and that host."""
    sn.store_secret()
    sn.load(tmp_path)
    bind_resilience()
    fake = FakeServiceNow.from_cassette("incident_table")
    sn.install(monkeypatch, sn.SnHost(fake))
    since = now - datetime.timedelta(days=1)
    set_watermark("servicenow", "incident", "sys_updated_on", since, now=now)

    def run_job() -> JobOutcome:
        return handle_sync(FakeJobContext({"source": "servicenow"}, kind="sync"))

    return run_job, fake

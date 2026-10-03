"""ST07-20 (TH07-20): bad SQL is never promoted into procedural memory (impl 07 U07-90, T07-19).

Only SQL behind verified findings creates a template; failures only lower scores. Beyond the
spec row, a template whose rebuilt query fails the spec 05 guard (an identifier outside the
allowlist, a non-SELECT statement) or that still holds a literal or an unbound placeholder is
not created, and the skip is logged without SQL text or values.
"""

# ruff: noqa: S608  # the attack SQL built from literals is the test data

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._procedural_env import incidents_sql, make_deps, rows, run_with
from tests.unit.harness.memory._write_env import NOW

from herness.harness.memory import procedural
from herness.harness.memory.procedural import ParameterizedSql, ParamSpec, promote_procedural

pytestmark = pytest.mark.unit

_PLANTED_TABLE = "core.api_keys"
_PLANTED_VALUE = "u-4711-planted"


def _skips(logs: list[dict[str, object]]) -> list[object]:
    return [e["reason"] for e in logs if e["event"] == "memory.procedural.skipped"]


def test_st07_20_rejected_findings_only_create_no_template(
    ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """ST07-20 SQL from rejected findings only → no template and no qa_pair, on every rerun."""
    pe = make_deps(tmp_path)
    for _ in range(3):
        run_id = run_with([(incidents_sql(svc=f"s{i}"), False) for i in range(5)])
        report = promote_procedural(run_id, deps=pe.deps, now=NOW)
        assert report.queries_seen == 5
        assert (report.templates_created, report.qa_pairs_created) == (0, 0)
        assert report.promoted == []
        assert promote_procedural(run_id, deps=pe.deps, now=NOW).templates_created == 0
    assert rows("sql_template") == []
    assert rows("qa_pair") == []


def test_st07_20_failures_only_lower_an_existing_template(
    ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """ST07-20 rejected findings on an existing template add fails and no qa_pair."""
    pe = make_deps(tmp_path)
    promote_procedural(run_with([(incidents_sql(), True)]), deps=pe.deps, now=NOW)
    before = rows("sql_template")[0]["confidence"]
    report = promote_procedural(run_with([(incidents_sql(svc="x"), False)]), deps=pe.deps, now=NOW)
    (tpl,) = rows("sql_template")
    assert report.templates_updated == 1
    assert report.qa_pairs_created == 0
    assert (tpl["data"]["passes"], tpl["data"]["fails"]) == (1, 1)
    assert tpl["confidence"] < before
    assert len(rows("qa_pair")) == 1


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        (f"SELECT token FROM {_PLANTED_TABLE} WHERE user_id = '{_PLANTED_VALUE}'",
         "guard_rejected"),
        (
            "SELECT service_id, secret_col FROM metrics.daily_incidents"
            f" WHERE service_id = '{_PLANTED_VALUE}'",
            "guard_rejected",
        ),
        ("SELECT name FROM stg.raw_tickets WHERE name = 'x'", "guard_rejected"),
        (f"DELETE FROM core.service WHERE service_id = '{_PLANTED_VALUE}'", "guard_rejected"),
        ("SELECT name FROM core.service WHERE name = :who AND team_id = 't'",
         "unbound_placeholder"),
    ],
    ids=["table_outside_allowlist", "column_outside_schema", "denied_schema", "non_select",
         "unbound_placeholder"],
)  # fmt: skip
def test_st07_20_guard_rejected_template_is_not_created(
    ops_store: OpsStoreHandle, tmp_path: Path, sql: str, reason: str
) -> None:
    """ST07-20 a verified query whose rebuilt template fails the guard creates nothing."""
    pe = make_deps(tmp_path)
    run_id = run_with([(sql, True), (sql, True)])
    with capture_logs() as logs:
        report = promote_procedural(run_id, deps=pe.deps, now=NOW)
    assert report.templates_created == 0
    assert report.skipped_unparsable == 2
    assert rows("sql_template") == []
    assert rows("qa_pair") == []
    assert _skips(logs) == [reason]
    text = repr(logs)
    assert _PLANTED_VALUE not in text
    assert "api_keys" not in text
    assert "SELECT" not in text
    assert "DELETE" not in text


def test_st07_20_template_with_unbound_literal_is_not_created(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST07-20 a template still carrying a literal (a value left in the SQL) is rejected."""
    pe = make_deps(tmp_path)
    leaky = ParameterizedSql(
        template=f"SELECT name FROM core.service WHERE name = '{_PLANTED_VALUE}' AND team_id = $p1",
        fingerprint="0123456789abcdef",
        params=[ParamSpec("p1", "string", "t")],
    )
    monkeypatch.setattr(procedural, "parameterize_sql", lambda _sql: leaky)
    with capture_logs() as logs:
        report = promote_procedural(run_with([(incidents_sql(), True)]), deps=pe.deps, now=NOW)
    assert report.templates_created == 0
    assert rows("sql_template") == []
    assert _skips(logs) == ["template_literal"]
    assert _PLANTED_VALUE not in repr(logs)


def test_st07_20_guard_is_consulted_before_create(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST07-20 the guard sees the rebuilt query (literals from the examples), not the template."""
    pe = make_deps(tmp_path)
    seen: list[str] = []
    real = pe.deps.guard.check

    def spy(sql: str, **kw: bool) -> object:
        seen.append(sql)
        return real(sql, **kw)

    monkeypatch.setattr(pe.deps.guard, "check", spy)
    promote_procedural(run_with([(incidents_sql(), True)]), deps=pe.deps, now=NOW)
    assert seen == [
        "SELECT service_id, SUM(incidents) AS n FROM metrics.daily_incidents WHERE day >="
        " '2026-01-01' AND day < '2026-02-01' AND service_id = 'svc_a' GROUP BY service_id"
    ]
    template = rows("sql_template")[0]["data"]["sql_template"]
    assert "$start_date" in template
    assert "2026" not in template
    assert "svc_a" not in template

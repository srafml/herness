"""Unit tests for the outcome job (impl 07 U07-86, U07-87; T07-18): UT07-74, UT07-87.

The warehouse is planted by `_outcome_env.plant` (spec 11 `tiny_build` is not in the tree);
`compute_metric` and `peer_group` are the real spec 04 functions on it, the writer is the real
MemoryWriter and every ops row goes through the real closed-loop functions.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import JsonValue
from structlog.testing import capture_logs
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._outcome_env import (
    EFFECTIVE,
    METRIC,
    NOW,
    OutcomeEnv,
    accepted_rec,
    all_closed,
    flat,
    improves,
    make_env,
    outcomes,
    plant,
    run,
    spy_compute,
    summaries,
)

from herness.core.errors import ConfigError, ToolInputError
from herness.harness.memory import outcome
from herness.harness.memory.outcome_stats import due_weeks, metric_weeks
from herness.harness.memory.settings import MemoryConfig, OutcomeConfig
from herness.store import ops
from herness.store.ops import core

pytestmark = pytest.mark.unit

PEERS = ("svc_p1", "svc_p2", "svc_p3")
FOUR = (*PEERS, "svc_p4")
REC_OK = "rec_" + "0" * 26


@pytest.fixture
def env(ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OutcomeEnv:
    """svc_t and svc_u improve 20 %; peers p1..p4 (same criticality) stay flat."""
    del ops_store
    con = plant({"svc_t": improves(), "svc_u": improves()} | dict.fromkeys(FOUR, flat))
    return make_env(tmp_path, monkeypatch, con)


def _due(rec_id: str) -> ops.DueMeasurement:
    per, default = due_weeks(MemoryConfig().outcome)
    [pair] = [p for p in ops.due_measurements(now="2026-04-01T12:00:00.000000Z", due_weeks=per,
              default_due_weeks=default) if p["rec_id"] == rec_id]  # fmt: skip
    return pair


def _measure(env: OutcomeEnv, rec_id: str) -> ops.OutcomeRow | None:
    return outcome.measure_recommendation(
        _due(rec_id), con=env.con, catalog=env.catalog, deps=env.deps, now=NOW
    )


# UT07-74: measure_recommendation (U07-87)


def test_ut07_74_treated_peer_excluded_leaves_two_peers_so_prior_year(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-74 three peers, one with its own accepted rec on the metric: two peers remain
    (< min_peers 3), so the method is prior_year and the series covers the target only."""
    del ops_store
    env = make_env(tmp_path, monkeypatch, plant({"svc_t": improves()} | dict.fromkeys(PEERS, flat)))
    calls = spy_compute(monkeypatch)
    accepted_rec("svc_p1")
    rec_id = accepted_rec("svc_t")
    row = _measure(env, rec_id)
    assert row is not None
    details = row["details"]
    assert details["method"] == "prior_year"
    assert details["peer_ids"] == []
    assert calls == [["svc_t"], ["svc_t"]]  # this year and the prior year
    assert details["peer_query_id"] not in (None, row["query_id"])
    assert row["verdict"] == "paid_off"  # 20 % better than the same weeks a year earlier
    assert set(env.evidence) >= {row["query_id"], details["peer_query_id"]}


def test_ut07_74_untreated_peers_give_did_peer_median(
    env: OutcomeEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-74 without a treated peer the three peers are the control (did_peer_median)."""
    calls = spy_compute(monkeypatch)
    rec_id = accepted_rec("svc_t")
    row = _measure(env, rec_id)
    assert row is not None
    assert row["details"]["method"] == "did_peer_median"
    assert row["details"]["peer_ids"] == [*FOUR, "svc_u"]
    assert calls == [["svc_t", *FOUR, "svc_u"]]
    assert row["verdict"] == "paid_off"
    assert row["baseline"] == pytest.approx(10.0, abs=0.05)
    assert row["actual"] == pytest.approx(8.0, abs=0.05)


def _step(pre: float, post: float) -> Any:
    return lambda _sid, week: post if week >= EFFECTIVE.date() else pre


def test_ut07_74_control_is_the_weekly_peer_median(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-74 heterogeneous peers: the control is the weekly peer MEDIAN (step 7). Pre
    (9, 10, 11) and post (10, 14, 9) both have median 10, so did = -2 and rel = 0.2; a single
    peer (+1, +4 or -1 change) or the mean (+1) would give another did."""
    del ops_store
    hours = {"svc_t": improves(), "svc_p1": _step(9.0, 10.0), "svc_p2": _step(10.0, 14.0),
             "svc_p3": _step(11.0, 9.0)}  # fmt: skip
    env = make_env(tmp_path, monkeypatch, plant(hours))
    row = _measure(env, accepted_rec("svc_t"))
    assert row is not None
    assert row["details"]["method"] == "did_peer_median"
    assert row["delta"] == pytest.approx(-2.0, abs=0.01)
    assert row["details"]["rel"] == pytest.approx(0.2, abs=0.01)
    assert row["verdict"] == "paid_off"


def test_ut07_74_ops_tool_input_error_propagates(
    env: OutcomeEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-74 only spec 04's ToolInputError (unknown target, refused request) skips a pair;
    one from the ops layer (treated_targets argument check) propagates and writes nothing."""

    def bad(**_kw: Any) -> set[tuple[str, str]]:
        msg = "treated_targets: invalid argument"
        raise ToolInputError(msg)

    rec_id = accepted_rec("svc_t")
    monkeypatch.setattr(ops, "treated_targets", bad)
    with pytest.raises(ToolInputError, match="treated_targets"):
        _measure(env, rec_id)
    assert outcomes() == []


def test_ut07_74_min_peers_above_group_falls_back_to_prior_year(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-74 three untreated peers but `min_peers = 4`: prior_year (TH07-18 min_peers)."""
    del ops_store
    con = plant({"svc_t": improves()} | dict.fromkeys(PEERS, flat))
    env = make_env(tmp_path, monkeypatch, con, OutcomeConfig(min_peers=4))
    row = _measure(env, accepted_rec("svc_t"))
    assert row is not None
    assert row["details"]["method"] == "prior_year"


def test_ut07_74_peer_group_prior_year_fallback_is_honoured(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-74 spec 04 returns fallback prior_year (min_service_peers 5 > 3 peers)."""
    del ops_store
    con = plant({"svc_t": improves()} | dict.fromkeys(PEERS, flat))
    env = make_env(tmp_path, monkeypatch, con, OutcomeConfig(min_peers=1), min_service_peers=5)
    row = _measure(env, accepted_rec("svc_t"))
    assert row is not None
    assert row["details"]["method"] == "prior_year"


def test_ut07_74_no_prior_year_data_is_inconclusive(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-74 prior_year without year-earlier rows: no control, so `inconclusive`."""
    del ops_store
    con = plant({"svc_t": improves(start=date(2025, 6, 2))})
    env = make_env(tmp_path, monkeypatch, con)
    row = _measure(env, accepted_rec("svc_t"))
    assert row is not None
    assert row["details"]["method"] == "prior_year"
    assert row["verdict"] == "inconclusive"


def test_ut07_74_outcome_row_details_and_summary(env: OutcomeEnv) -> None:
    """UT07-74 the outcome row holds the U07-87 details; one active outcome_summary cites
    the series query and carries the numbers in data only."""
    rec_id = accepted_rec("svc_t", expected_delta=-1.0)
    row = _measure(env, rec_id)
    assert row is not None
    [stored] = outcomes()
    assert stored["outcome_id"] == row["outcome_id"]
    assert stored["measured_at"] == "2026-04-01T12:00:00.000000Z"
    details = stored["details"]
    assert set(details) >= {
        "method", "pre", "post", "peer_group_key", "peer_ids", "peer_query_id", "n_pre",
        "n_post", "coverage", "did", "se", "t", "rel", "expected_rel", "build_id", "config_hash",
    }  # fmt: skip
    assert details["pre"] == ["2025-10-27", "2026-01-05"]
    assert details["post"] == ["2026-01-19", "2026-03-30"]
    assert details["expected_rel"] == pytest.approx(0.1, abs=0.01)
    assert details["config_hash"] == "cfg_" + "0" * 16
    assert details["peer_group_key"] == "service:crit_1"
    [summary] = summaries()
    assert summary["status"] == "active"
    assert summary["content"] == (
        f"Accepted fund {rec_id} for svc_t on {METRIC} showed a measurable improvement"
        " (first measurement)."
    )
    assert summary["provenance"]["via"] == "outcome_job"
    assert summary["provenance"]["query_ids"] == [row["query_id"]]
    assert summary["data"]["outcome_id"] == row["outcome_id"]
    assert summary["data"]["embedding_pending"] is False  # embedded after commit


def test_ut07_74_existing_outcome_returns_none_without_queries(
    env: OutcomeEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-74 an existing (rec_id, measurement) returns None before any warehouse query."""
    rec_id = accepted_rec("svc_t")
    due = _due(rec_id)
    assert _measure(env, rec_id) is not None
    calls = spy_compute(monkeypatch)
    before = list(env.evidence)
    again = outcome.measure_recommendation(due, con=env.con, catalog=env.catalog,
                                           deps=env.deps, now=NOW)  # fmt: skip
    assert again is None
    assert calls == []
    assert env.evidence == before
    assert len(outcomes()) == 1


def test_ut07_74_lost_insert_race_writes_no_second_summary(
    env: OutcomeEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-74 when the row appears between the existence check and the write (a concurrent
    job), INSERT OR IGNORE keeps one row and no second summary is written."""
    rec_id = accepted_rec("svc_t")
    due = _due(rec_id)
    assert _measure(env, rec_id) is not None
    monkeypatch.setattr(ops, "outcome_exists", lambda *_a, **_k: False)
    again = outcome.measure_recommendation(due, con=env.con, catalog=env.catalog,
                                           deps=env.deps, now=NOW)  # fmt: skip
    assert again is None
    assert len(outcomes()) == 1
    assert len(summaries()) == 1


def test_ut07_74_unknown_metric_is_skipped(env: OutcomeEnv) -> None:
    """UT07-74 a metric the catalog does not know logs `memory.outcome.skipped` and writes
    nothing."""
    rec_id = accepted_rec("svc_t", metric="no_such_metric")
    with capture_logs() as logs:
        assert _measure(env, rec_id) is None
    assert {"event": "memory.outcome.skipped", "reason": "unknown_metric"}.items() <= next(
        e for e in logs if e["event"] == "memory.outcome.skipped").items()  # fmt: skip
    assert outcomes() == []
    assert summaries() == []


def test_ut07_74_unknown_target_is_skipped(env: OutcomeEnv) -> None:
    """UT07-74 a target unknown to the warehouse is not measurable: skipped, nothing written."""
    rec_id = accepted_rec("svc_missing")
    with capture_logs() as logs:
        assert _measure(env, rec_id) is None
    assert any(e.get("reason") == "invalid_request" for e in logs)
    assert outcomes() == []


def test_ut07_74_work_item_measures_its_owning_service(
    env: OutcomeEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-74 a work_item target is measured on the owning service of core.work_item."""
    env.con.execute("INSERT INTO core.work_item (record_id, key, service_id) VALUES "
                    "('wi_owned', 'K-1', 'svc_t'), ('wi_orphan', 'K-2', NULL)")  # fmt: skip
    calls = spy_compute(monkeypatch)
    owned = _measure(env, accepted_rec("wi_owned", target_type="work_item"))
    assert owned is not None
    assert calls[0][0] == "svc_t"
    assert owned["details"]["method"] == "did_peer_median"
    assert "svc_t" not in cast("list[str]", owned["details"]["peer_ids"])


def test_ut07_74_work_item_without_owner_is_inconclusive(
    env: OutcomeEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-74 a work item whose service_id is NULL has no series: `inconclusive`, method
    `none`, cited by the recorded peer group query (persisted as evidence)."""
    env.con.execute("INSERT INTO core.work_item (record_id, key) VALUES ('wi_orphan', 'K-2')")
    calls = spy_compute(monkeypatch)
    row = _measure(env, accepted_rec("wi_orphan", target_type="work_item"))
    assert row is not None
    assert calls == []
    assert row["verdict"] == "inconclusive"
    assert row["details"]["method"] == "none"
    assert row["query_id"] == row["details"]["peer_query_id"]
    assert row["query_id"] in env.evidence
    assert row["baseline"] is None
    assert summaries()[0]["provenance"]["query_ids"] == [row["query_id"]]


def test_ut07_74_target_id_with_numeral_is_named_generically(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-74 a target id holding an uncited numeral is left out of the summary text so the
    system numeral rule (numerals.system) cannot reject it."""
    del ops_store
    con = plant({"svc 7": improves()} | dict.fromkeys(PEERS, flat))
    env = make_env(tmp_path, monkeypatch, con)
    rec_id = accepted_rec("svc 7")
    assert _measure(env, rec_id) is not None
    [summary] = summaries()
    assert "svc 7" not in summary["content"]
    assert f"{rec_id} for a service target on" in summary["content"]


def test_ut07_74_week_resolver_applies_per_metric_override() -> None:
    """UT07-74 `metric_weeks` / `due_weeks` (T07-17 carry-over): the shipped
    change_failure_rate override measures after 8 weeks, capped below by lag + L = 12."""
    cfg = MemoryConfig().outcome
    assert metric_weeks(cfg, "change_failure_rate").measure_after_weeks == 8
    assert metric_weeks(cfg, METRIC).measure_after_weeks == 12
    per, default = due_weeks(cfg)
    assert default == (12, 26)
    assert per == {"change_failure_rate": (12, 26)}
    longer = OutcomeConfig(per_metric={METRIC: {"measure_after_weeks": 14}})
    assert due_weeks(longer)[0] == {METRIC: (14, 26)}


def test_ut07_74_default_config_hash_reads_the_process_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT07-74 the default `config_hash` collaborator hashes the process config (T10-03)."""
    sentinel = object()
    monkeypatch.setattr(outcome, "get_config", lambda: sentinel)
    monkeypatch.setattr(outcome, "config_hash", lambda cfg: "cfg_x" if cfg is sentinel else "")
    default = outcome.OutcomeDeps.__dataclass_fields__["config_hash"].default
    assert callable(default)
    assert default() == "cfg_x"


# UT07-87: outcome_measure_handler (U07-86)


def test_ut07_87_sweep_measures_every_due_pair(env: OutcomeEnv) -> None:
    """UT07-87 `{"sweep": true}` measures each due pair once, heartbeats per pair and tallies
    verdicts; the warehouse is opened once and closed."""
    first, second = accepted_rec("svc_t"), accepted_rec("svc_u")
    result, ctx = run()
    assert result.status == "done"
    assert result.result == {"measured": 2, "skipped": 0, "verdicts": {"paid_off": 2}}
    assert sorted(n or "" for n in ctx.notes) == sorted(
        [f"measured {first} m1", f"measured {second} m1"]
    )
    assert env.opened == 1
    assert all_closed(env)
    assert len(outcomes()) == 2


def test_ut07_87_single_pair_and_not_due(env: OutcomeEnv) -> None:
    """UT07-87 `{"rec_id", "measurement"}` measures that pair when due; measurement 2 is not
    due yet and returns `skipped: not_due` without opening the warehouse."""
    rec_id = accepted_rec("svc_t")
    later, _ = run({"rec_id": rec_id, "measurement": 2})
    assert later.result == {"skipped": "not_due"}
    assert env.opened == 0
    result, _ = run({"rec_id": rec_id, "measurement": 1})
    assert result.result == {"measured": 1, "skipped": 0, "verdicts": {"paid_off": 1}}
    again, _ = run({"rec_id": rec_id, "measurement": 1})
    assert again.result == {"skipped": "not_due"}  # measured: no longer due


def test_ut07_87_empty_sweep_does_not_open_the_warehouse(env: OutcomeEnv) -> None:
    """UT07-87 nothing due: done with zero counts, no warehouse connection."""
    accepted_rec("svc_t", decision="rejected")
    accepted_rec("svc_u", metric=None)
    result, _ = run()
    assert result.result == {"measured": 0, "skipped": 0, "verdicts": {}}
    assert env.opened == 0
    assert outcomes() == []


def test_ut07_87_skipped_pairs_are_counted(env: OutcomeEnv) -> None:
    """UT07-87 an unknown metric counts as skipped; the other pair is measured."""
    accepted_rec("svc_t", metric="no_such_metric")
    accepted_rec("svc_u")
    result, _ = run()
    assert result.result == {"measured": 1, "skipped": 1, "verdicts": {"paid_off": 1}}


def test_ut07_87_yield_then_resume(env: OutcomeEnv) -> None:
    """UT07-87 `should_yield` after the first pair returns `yield`; the next run measures
    only the remaining pair."""
    accepted_rec("svc_t")
    accepted_rec("svc_u")
    result, _ = run(yield_after=1)
    assert result.status == "yield"
    assert result.result == {"measured": 1}
    rest, _ = run()
    assert rest.result == {"measured": 1, "skipped": 0, "verdicts": {"paid_off": 1}}
    assert len(outcomes()) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {}, {"sweep": False}, {"sweep": 1}, {"sweep": "true"}, {"sweep": True, "rec_id": REC_OK},
        {"rec_id": "rec_bad", "measurement": 1}, {"rec_id": REC_OK, "measurement": 3},
        {"rec_id": REC_OK, "measurement": True}, {"rec_id": REC_OK}, {"measurement": 1},
        {"rec_id": REC_OK, "measurement": 1, "extra": 1}, {"rec_id": 5, "measurement": 1},
    ],
)  # fmt: skip
def test_ut07_87_bad_payload_raises_config_error(
    env: OutcomeEnv, payload: dict[str, JsonValue]
) -> None:
    """UT07-87 any other payload is `ConfigError("outcome_measure payload invalid")`."""
    with pytest.raises(ConfigError, match="outcome_measure payload invalid"):
        run(payload)
    assert env.opened == 0


def test_ut07_87_unconfigured_handler_raises_config_error(env: OutcomeEnv) -> None:
    """UT07-87 without `configure_outcome` the handler raises ConfigError (R-42 seam)."""
    outcome.configure_outcome(None)
    with pytest.raises(ConfigError, match="configure_outcome"):
        run()
    outcome.configure_outcome(env.deps)
    assert outcome.outcome_deps() is env.deps


def test_ut07_87_measured_log_and_counter(env: OutcomeEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-87 each measurement logs `memory.outcome.measured` with ids only and adds one to
    `herness_memory_outcomes_total{verdict}`."""
    counted: list[tuple[str, dict[str, str]]] = []

    def record(name: str, **kw: Any) -> None:
        counted.append((name, dict(kw["labels"])))

    monkeypatch.setattr("herness.harness.memory._write_steps.record_counter", record)
    rec_id = accepted_rec("svc_t")
    with capture_logs() as logs:
        run()
    [event] = [e for e in logs if e["event"] == "memory.outcome.measured"]
    assert event["rec_id"] == rec_id
    assert event["verdict"] == "paid_off"
    assert event["method"] == "did_peer_median"
    assert ("herness_memory_outcomes_total", {"verdict": "paid_off"}) in counted


def test_ut07_87_query_ids_resolve_in_evidence(env: OutcomeEnv) -> None:
    """UT07-87 the outcome and peer group query ids are ops `evidence` rows (step 8)."""
    accepted_rec("svc_t")
    run()
    [row] = outcomes()
    for qid in (row["query_id"], row["details"]["peer_query_id"]):
        assert core.read_one("SELECT 1 FROM evidence WHERE query_id = ?", (qid,)) is not None

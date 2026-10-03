"""Tests for herness.metrics.peers (impl 04 U04-61 … U04-63; design 04 §3.1, §5.8, §5.8.1).

`metrics_tiny` with materialized facts: teams T1 (owns S1, criticality 1 -> bucket hi) and T2
(no service -> none), services S1 (crit 1) and S2 (crit 3), orgs O1 > O2 > O3, incidents
I1-I3 of S1 in cluster C1 (I4 canceled). Each test adds its own rows. The build started
2026-04-01 06:00 UTC, so `as_of` is 2026-04-01 in the business timezone.
"""

from collections.abc import Callable, Iterator, Sequence
from types import SimpleNamespace
from typing import Any, Final

import duckdb
import pytest
from freezegun import freeze_time
from pydantic import ValidationError
from structlog.testing import capture_logs
from tests.support.metrics_tiny import (
    BUILD_ID,
    build_metrics_tiny,
    patch_facts_config,
    shipped_catalog,
    tiny_weights,
)

from herness.core.errors import SchemaViolation, ToolInputError
from herness.metrics import compute, peers
from herness.metrics.catalog import MetricCatalog
from herness.metrics.context import StepContext
from herness.metrics.evidence import RecordedQuery, run_recorded
from herness.metrics.facts import materialize_facts
from herness.metrics.org import run_org_step
from herness.metrics.peers import PeerGroupInfo, peer_group
from herness.metrics.windows import default_window, resolve_as_of

pytestmark = pytest.mark.unit

MTTR: Final = "mttr_hours"
SQL_CAP: Final = 20_000  # Evidence.sql limit (to_evidence)
INJECTIONS: Final = (
    "S1' OR '1'='1",
    "x'); DROP TABLE metrics.incident_fact; --",
    "$1",
    "{{ p('tz') }}",
)
_MV_DDL: Final = (
    "CREATE TABLE metrics.metric_value (metric VARCHAR NOT NULL, entity_type VARCHAR NOT NULL,"
    " entity_id VARCHAR NOT NULL, period VARCHAR NOT NULL, period_start DATE NOT NULL,"
    " value DOUBLE, numerator DOUBLE, denominator DOUBLE, sample_size BIGINT NOT NULL,"
    " unit VARCHAR NOT NULL, flags VARCHAR[] NOT NULL, query_id VARCHAR NOT NULL)"
)
_WEIGHTS: Final = tiny_weights()
_TZ: Final = _WEIGHTS.business_timezone


def _catalog(*, min_peer_group: int = 5, min_service_peers: int = 3) -> MetricCatalog:
    """The shipped catalog with these peer thresholds (model_copy: no range validation)."""
    cfg = shipped_catalog().config
    org = cfg.scoring.org.model_copy(update={"min_peer_group": min_peer_group})
    pg = cfg.scoring.peer_group.model_copy(update={"min_service_peers": min_service_peers})
    scoring = cfg.scoring.model_copy(update={"org": org, "peer_group": pg})
    return MetricCatalog(cfg.model_copy(update={"scoring": scoring}))


def _use(monkeypatch: pytest.MonkeyPatch, catalog: MetricCatalog) -> None:
    monkeypatch.setattr(peers, "catalog_from_config", lambda: catalog)


@pytest.fixture
def tiny(monkeypatch: pytest.MonkeyPatch) -> Iterator[duckdb.DuckDBPyConnection]:
    """`metrics_tiny` with facts, an empty `metrics.metric_value` and the shipped catalog."""
    patch_facts_config(monkeypatch)
    con = build_metrics_tiny()
    with freeze_time("2026-04-01 06:30:00"):
        materialize_facts(con, BUILD_ID)
    con.execute(_MV_DDL)
    monkeypatch.setattr(peers, "get_config", lambda: SimpleNamespace(weights=_WEIGHTS))
    _use(monkeypatch, _catalog())
    yield con
    con.close()


def _as_of(con: duckdb.DuckDBPyConnection) -> Any:
    row = con.execute("SELECT started_at FROM meta.build").fetchone()
    assert row is not None
    return resolve_as_of(row[0], _TZ, None)


def _window_start(con: duckdb.DuckDBPyConnection) -> Any:
    return default_window("t12w", _as_of(con), _TZ, {"week": 26, "month": 24, "quarter": 8}).start


def _teams(con: duckdb.DuckDBPyConnection, rows: Sequence[tuple[str, int | None, bool]]) -> None:
    """Active or inactive teams; a team with a criticality owns one service of it."""
    for team, crit, active in rows:
        con.execute(
            "INSERT INTO core.team (team_id, org_id, active) VALUES (?, 'O2', ?)", [team, active]
        )
        if crit is not None:
            con.execute(
                "INSERT INTO core.service (service_id, criticality) VALUES (?, ?)",
                [f"S_{team}", crit],
            )
            con.execute(
                "INSERT INTO core.service_map (service_id, team_id, role) VALUES (?, ?, 'owner')",
                [f"S_{team}", team],
            )


def _services(con: duckdb.DuckDBPyConnection, crit: int | None, *ids: str) -> None:
    for sid in ids:
        con.execute("INSERT INTO core.service (service_id, criticality) VALUES (?, ?)", [sid, crit])


def _mv(
    con: duckdb.DuckDBPyConnection,
    entity_id: str,
    value: float | None,
    *,
    entity_type: str = "team",
    flags: Sequence[str] = (),
) -> None:
    con.execute(
        "INSERT INTO metrics.metric_value VALUES (?, ?, ?, 't12w', ?, ?, NULL, NULL, 20, 'hours',"
        " ?, 'q_0000000000000000')",
        [MTTR, entity_type, entity_id, _window_start(con), value, list(flags)],
    )


def _shape(info: PeerGroupInfo) -> tuple[str, list[str], int, str | None]:
    return info.key, info.member_ids, info.size, info.fallback


# --- UT04-71 team and org --------------------------------------------------------------------


def test_ut04_71_peer_group_info_invariants() -> None:
    """UT04-71 PeerGroupInfo is frozen, strict and keeps size == len(member_ids)."""
    info = PeerGroupInfo(key="team:all", member_ids=["A"], size=1, fallback="all", query_id="q")
    with pytest.raises(ValidationError):
        PeerGroupInfo(key="team:all", member_ids=["A"], size=2, fallback=None, query_id="q")
    with pytest.raises(ValidationError):
        PeerGroupInfo(key="k", member_ids=[], size=0, fallback="other", query_id="q")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        info.size = 3  # type: ignore[misc]


def test_ut04_71_team_buckets_and_all_fallback(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-71 keys spelled team:crit_hi (R-61); few metric values fall back to team:all."""
    _teams(tiny, [("T3", 2, True), ("T4", 1, True), ("T5", 3, True), ("T6", 1, False)])
    assert _shape(peer_group("team", "T1", con=tiny)) == ("team:crit_hi", ["T3", "T4"], 2, None)
    assert _shape(peer_group("team", "T5", con=tiny)) == ("team:crit_lo", [], 0, None)
    assert peer_group("team", "T2", con=tiny).key == "team:crit_none"
    assert peer_group("team", "T6", con=tiny).key == "team:crit_hi"  # inactive, still found
    _mv(tiny, "T1", 2.0)
    _mv(tiny, "T3", 4.0)
    _mv(tiny, "T4", 6.0, flags=["insufficient_sample"])  # does not count, as in score.org
    few = peer_group("team", "T1", metric=MTTR, con=tiny)
    assert _shape(few) == ("team:all", ["T2", "T3", "T4", "T5"], 4, "all")
    _use(monkeypatch, _catalog(min_peer_group=2))
    assert _shape(peer_group("team", "T1", metric=MTTR, con=tiny)) == (
        "team:crit_hi",
        ["T3", "T4"],
        2,
        None,
    )


def test_ut04_71_org_levels_and_all_fallback(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-71 orgs group by closure depth ('org:level<d>'), else 'org:all'."""
    assert _shape(peer_group("org", "O2", con=tiny)) == ("org:level1", [], 0, None)
    tiny.execute("INSERT INTO core.org (org_id, parent_org_id) VALUES ('O4', 'O1')")
    tiny.execute(
        "INSERT INTO metrics.org_closure (org_id, ancestor_org_id, depth)"
        " VALUES ('O4', 'O4', 0), ('O4', 'O1', 1)"
    )
    assert _shape(peer_group("org", "O2", con=tiny)) == ("org:level1", ["O4"], 1, None)
    assert peer_group("org", "O1", con=tiny).key == "org:level0"
    _mv(tiny, "O2", 1.0, entity_type="org")
    info = peer_group("org", "O2", metric=MTTR, con=tiny)
    assert _shape(info) == ("org:all", ["O1", "O3", "O4"], 3, "all")


@pytest.mark.parametrize("min_peer_group", [1, 2, 3, 5])
def test_ut04_71_same_key_as_score_org(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch, min_peer_group: int
) -> None:
    """UT04-71 the key equals score.org.peer_group for every scored team/org and the metric."""
    _teams(tiny, [("T3", 2, True), ("T4", 1, True), ("T5", 3, True), ("T6", 4, True)])
    for team, value in (("T1", 1.0), ("T3", 2.0), ("T4", None), ("T5", 5.0), ("T6", 3.0)):
        _mv(tiny, team, value)
    for org, value in (("O1", 1.0), ("O2", 2.0)):
        _mv(tiny, org, value, entity_type="org")
    catalog = _catalog(min_peer_group=min_peer_group)
    org_cfg = catalog.config.scoring.org.model_copy(update={"metrics": {MTTR: 1.0}})
    scoring = catalog.config.scoring.model_copy(update={"org": org_cfg})
    catalog = MetricCatalog(catalog.config.model_copy(update={"scoring": scoring}))
    _use(monkeypatch, catalog)
    run_org_step(tiny, StepContext(BUILD_ID, catalog, _WEIGHTS, _as_of(tiny), _TZ, frozenset()))
    rows = tiny.execute("SELECT entity_type, entity_id, peer_group FROM score.org").fetchall()
    assert len(rows) == 9
    for entity_type, entity_id, key in rows:
        assert peer_group(entity_type, entity_id, metric=MTTR, con=tiny).key == key


def test_ut04_71_unknown_entity(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-71 an entity that does not exist is a ToolInputError naming its type and id."""
    for entity_type in ("team", "org", "service", "work_item"):
        with pytest.raises(ToolInputError, match=f"unknown {entity_type} NOPE"):
            peer_group(entity_type, "NOPE", con=tiny)  # type: ignore[arg-type]


def test_ut04_71_input_validation(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-71 entity type, id and metric are checked before any SQL runs."""
    bad: list[Callable[[], object]] = [
        lambda: peer_group("cluster", "C1", con=tiny),  # type: ignore[arg-type]
        lambda: peer_group("team", "", con=tiny),
        lambda: peer_group("team", "x" * 257, con=tiny),
        lambda: peer_group("team", "T1\n", con=tiny),
        lambda: peer_group("team", 5, con=tiny),  # type: ignore[arg-type]
        lambda: peer_group("team", "T1", metric="nope", con=tiny),
        lambda: peer_group("team", "T1", metric="availability_pct", con=tiny),
        lambda: peer_group("org", "O1", metric="availability_pct", con=tiny),
    ]
    for call in bad:
        with pytest.raises(ToolInputError):
            call()
    with pytest.raises(ToolInputError, match="unknown team"):  # 256 characters pass the checks
        peer_group("team", "x" * 256, con=tiny)
    assert peer_group("service", "S1", metric="availability_pct", con=tiny).key == "service:crit_1"
    cfg = shipped_catalog().config
    off = [m.model_copy(update={"enabled": False}) if m.name == MTTR else m for m in cfg.metrics]
    _use(monkeypatch, MetricCatalog(cfg.model_copy(update={"metrics": off})))
    with pytest.raises(ToolInputError, match="disabled"):
        peer_group("team", "T1", metric=MTTR, con=tiny)


# --- UT04-72 services --------------------------------------------------------------------------


def test_ut04_72_service_criticality_group(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-72 services crit 2: key service:crit_2, the entity itself excluded."""
    _services(tiny, 2, "S5", "S6", "S7", "S8")
    assert _shape(peer_group("service", "S5", con=tiny)) == (
        "service:crit_2",
        ["S6", "S7", "S8"],
        3,
        None,
    )
    assert _shape(peer_group("service", "S2", con=tiny)) == ("service:crit_3", [], 0, "prior_year")


def test_ut04_72_null_criticality_matches_null(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-72 a NULL criticality gives service:crit_none and groups with other NULLs."""
    _services(tiny, None, "S9", "S10")
    info = peer_group("service", "S9", con=tiny)
    assert _shape(info) == ("service:crit_none", ["S10"], 1, "prior_year")


# --- UT04-73 work items ------------------------------------------------------------------------


def test_ut04_73_item_with_service(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-73 a work item with service_id uses its service's group without that service."""
    _services(tiny, 1, "S11", "S12", "S13")
    info = peer_group("work_item", "W1", con=tiny)
    assert _shape(info) == ("service:crit_1", ["S11", "S12", "S13"], 3, None)


def _item(
    con: duckdb.DuckDBPyConnection, record_id: str, project: str, components: list[str]
) -> None:
    con.execute(
        "INSERT INTO core.work_item (record_id, key, type, project, components)"
        " VALUES (?, ?, 'epic', ?, ?)",
        [record_id, record_id, project, components],
    )


def test_ut04_73_item_via_service_map(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-73 no service_id: highest-confidence owner map row of the project/component."""
    _services(tiny, 2, "S5", "S6", "S7")
    _item(tiny, "WM9", "MAP", ["api"])
    tiny.execute(
        "INSERT INTO core.service_map (service_id, jira_project, jira_component, role, confidence)"
        " VALUES ('S6', 'MAP', NULL, 'owner', 0.9), ('S5', 'MAP', 'api', 'owner', 0.9),"
        " ('S2', 'MAP', 'web', 'owner', 1.0), ('S7', 'MAP', NULL, 'support', 1.0),"
        " ('S1', 'OTHER', NULL, 'owner', 1.0)"
    )
    assert _shape(peer_group("work_item", "WM9", con=tiny)) == (
        "service:crit_2",
        ["S6", "S7"],
        2,
        "prior_year",
    )


def _clone_incident(
    con: duckdb.DuckDBPyConnection, record_id: str, cluster: str, sid: str, ts: str
) -> None:
    con.execute(
        "INSERT INTO metrics.incident_fact SELECT * REPLACE (? AS record_id, ? AS cluster_id,"
        " ? AS service_id, CAST(? AS TIMESTAMPTZ) AS opened_at, false AS excluded)"
        " FROM metrics.incident_fact WHERE record_id = 'I1'",
        [record_id, cluster, sid, ts],
    )


def test_ut04_73_cluster_fix_most_frequent_service(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-73 cluster_fix:<id> resolves to the cluster's most frequent service in the window."""
    _services(tiny, 1, "S11")
    for n in range(4):  # older than s_window_days: ignored
        _clone_incident(tiny, f"OLD{n}", "C1", "S2", "2024-01-01 00:00:00+00")
    _clone_incident(tiny, "FUT", "C1", "S2", "2026-04-02 00:00:00+00")  # after as_of
    assert _shape(peer_group("work_item", "cluster_fix:C1", con=tiny)) == (
        "service:crit_1",
        ["S11"],
        1,
        "prior_year",
    )
    _clone_incident(tiny, "TA", "C2", "S2", "2026-03-01 00:00:00+00")
    _clone_incident(tiny, "TB", "C2", "S1", "2026-03-02 00:00:00+00")
    assert peer_group("work_item", "cluster_fix:C2", con=tiny).member_ids == ["S11"]  # tie: S1


def test_ut04_73_unresolved_items(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-73 no owning service -> work_item:unresolved, size 0, prior_year."""
    _item(tiny, "WU10", "NOMAP", [])
    tiny.execute("INSERT INTO core.work_item (record_id, service_id) VALUES ('WU11', 'GHOST')")
    expected = ("work_item:unresolved", [], 0, "prior_year")
    for entity_id in ("WU10", "WU11", "cluster_fix:NOPE"):
        assert _shape(peer_group("work_item", entity_id, con=tiny)) == expected


# --- UT04-74 prior-year fallback ---------------------------------------------------------------


def test_ut04_74_two_peers_prior_year_members_kept(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-74 two peers (< min_service_peers 3): fallback prior_year, members kept."""
    _services(tiny, 2, "S5", "S6", "S7")
    assert _shape(peer_group("service", "S5", con=tiny)) == (
        "service:crit_2",
        ["S6", "S7"],
        2,
        "prior_year",
    )
    _use(monkeypatch, _catalog(min_service_peers=2))
    assert peer_group("service", "S5", con=tiny).fallback is None


# --- U04-62 evidence, connection, logging ------------------------------------------------------


def test_ut04_71_one_recorded_query_and_callback(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-71 one recorded query; on_evidence gets it; DEBUG log carries ids and counts."""
    seen: list[RecordedQuery] = []
    with capture_logs() as logs:
        info = peer_group("team", "T1", metric=MTTR, con=tiny, on_evidence=seen.append)
    assert [rq.query_id for rq in seen] == [info.query_id]
    rq = seen[0]
    assert rq.params["template"] == {
        "name": "peer_group",
        "entity_type": "team",
        "has_metric": True,
    }
    assert rq.build_id == BUILD_ID
    assert len(rq.sql) < SQL_CAP
    events = [e for e in logs if e["event"] == "metrics.peer_group.resolved"]
    assert events == [
        {
            "event": "metrics.peer_group.resolved",
            "log_level": "debug",
            "component": "metrics",
            "entity_type": "team",
            "key": "team:all",
            "size": 1,
            "fallback": "all",
            "query_id": info.query_id,
        }
    ]


@pytest.mark.parametrize(
    ("entity_type", "has_metric"),
    [
        ("team", True),
        ("team", False),
        ("org", True),
        ("org", False),
        ("service", False),
        ("work_item", False),
    ],
)
def test_ut04_71_rendered_sql_within_cap(
    tiny: duckdb.DuckDBPyConnection, entity_type: str, has_metric: bool
) -> None:
    """UT04-71 every rendered variant stays under the 20,000-character evidence cap."""
    seen: list[RecordedQuery] = []
    ids = {"team": "T1", "org": "O1", "service": "S1", "work_item": "W1"}
    peer_group(
        entity_type,  # type: ignore[arg-type]
        ids[entity_type],
        metric=MTTR if has_metric else None,
        con=tiny,
        on_evidence=seen.append,
    )
    assert len(seen[0].sql) < SQL_CAP


def test_ut04_71_con_none_opens_and_closes(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-71 con=None opens the read-only CURRENT warehouse and closes it afterwards."""
    opened: list[object] = []

    class _Con:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, *args: Any) -> Any:
            return tiny.execute(*args)

        def close(self) -> None:
            self.closed = True

    def fake_open(path: object) -> _Con:
        opened.append(path)
        return _Con()

    monkeypatch.setattr(peers, "open_readonly", fake_open)
    monkeypatch.setattr(peers, "run_recorded", _via(tiny))
    assert peer_group("team", "T1").key == "team:crit_hi"
    assert opened == [None]


def _via(con: duckdb.DuckDBPyConnection) -> Callable[..., RecordedQuery]:
    def call(_con: object, *args: Any, **kwargs: Any) -> RecordedQuery:
        return run_recorded(con, *args, **kwargs)

    return call


def test_ut04_71_build_row_required(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-71 meta.build must hold exactly one row (U04-52)."""
    tiny.execute("DELETE FROM meta.build")
    with pytest.raises(SchemaViolation):
        peer_group("team", "T1", con=tiny)
    tiny.execute("DROP TABLE meta.build")
    with pytest.raises(SchemaViolation):
        peer_group("team", "T1", con=tiny)


def test_ut04_71_compute_reexports_peer_group() -> None:
    """UT04-71 compute re-exports peer_group and PeerGroupInfo (§2 row)."""
    assert compute.peer_group is peer_group
    assert compute.PeerGroupInfo is PeerGroupInfo
    assert {"peer_group", "PeerGroupInfo"} <= set(compute.__all__)


# --- TH04-01 bound, never rendered -------------------------------------------------------------


@pytest.mark.parametrize("bad_id", INJECTIONS)
def test_th04_01_entity_id_is_bound_not_rendered(
    tiny: duckdb.DuckDBPyConnection, bad_id: str
) -> None:
    """TH04-01 injection-shaped ids reach SQL only as typed binds; tables stay intact."""
    _services(tiny, 2, bad_id, "S5")
    seen: list[RecordedQuery] = []
    info = peer_group("service", bad_id, con=tiny, on_evidence=seen.append)
    assert _shape(info) == ("service:crit_2", ["S5"], 1, "prior_year")
    assert bad_id not in seen[0].sql
    bind = seen[0].params["bind"]
    assert isinstance(bind, dict)
    assert bind["pg_entity_id"] == bad_id
    assert "CAST($pg_entity_id AS VARCHAR)" in seen[0].sql
    for entity_type in ("team", "org", "work_item"):
        with pytest.raises(ToolInputError, match="unknown"):
            peer_group(entity_type, bad_id, con=tiny)  # type: ignore[arg-type]
    count = tiny.execute("SELECT count(*) FROM metrics.incident_fact").fetchone()
    assert count is not None
    assert count[0] == 4


def test_th04_01_metric_is_allowlisted_and_bound(tiny: duckdb.DuckDBPyConnection) -> None:
    """TH04-01 an injection-shaped metric is rejected; a valid one is a bind, not SQL text."""
    for bad in INJECTIONS:
        with pytest.raises(ToolInputError, match="unknown metric"):
            peer_group("team", "T1", metric=bad, con=tiny)
    seen: list[RecordedQuery] = []
    peer_group("team", "T1", metric=MTTR, con=tiny, on_evidence=seen.append)
    assert MTTR not in seen[0].sql
    bind = seen[0].params["bind"]
    assert isinstance(bind, dict)
    assert bind["pg_metric"] == MTTR

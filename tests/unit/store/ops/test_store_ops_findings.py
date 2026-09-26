"""Unit tests for the ops area `findings` (impl 06 U06-41 … U06-44, U06-144, T06-06).

UT06-27 (inserts and every design 06 §6.5 transition), UT06-28 (reads) and UT06-95 (privacy
scrub) run on the migrated tmp ops store of the `ops_store` fixture.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.types import (
    SKEPTIC_CHECKS,
    Challenge,
    Finding,
    FindingStatus,
    VerificationRecord,
)
from herness.store import ops
from herness.store.ops import core, findings

pytestmark = pytest.mark.unit

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # pragma: allowlist secret - ULID alphabet
_T0 = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
_Q1 = "q_" + "a" * 16
_Q2 = "q_" + "b" * 16
_RECORD = "jira:issue:ABC-123"
_STATUSES: tuple[FindingStatus, ...] = (
    "proposed",
    "challenged",
    "verified",
    "rejected",
    "revised",
    "merged",
)


def _ulid(n: int) -> str:
    digits = []
    for _ in range(26):
        n, r = divmod(n, 32)
        digits.append(_CROCKFORD[r])
    return "".join(reversed(digits))


def _fid(n: int) -> str:
    return f"fnd_{_ulid(n)}"


def _rid(n: int) -> str:
    return f"run_{_ulid(n)}"


def _tid(n: int) -> str:
    return f"task_{_ulid(n)}"


def _number(n: int, query_id: str = _Q1, **extra: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": f"n{n}",
        "value": n,
        "unit": "count",
        "query_id": query_id,
        "column": "incidents",
        "row_key": {"team": "t1"},
    }
    values.update(extra)
    return values


def _finding(n: int, *, run: int = 1, task: int = 1, **overrides: object) -> Finding:
    values: dict[str, object] = {
        "finding_id": _fid(n),
        "run_id": _rid(run),
        "task_id": _tid(task),
        "author_role": "analyst",
        "claim": "Team t1 had [[n1]] incidents",
        "entity_type": "team",
        "entity_id": "t1",
        "numbers": [_number(1)],
        "query_ids": [_Q1],
        "confidence": 0.5,
        "created_at": _T0 + timedelta(seconds=n),
    }
    if overrides.get("status") == "merged":
        values["merged_into"] = _fid(900)
    values.update(overrides)
    return Finding.model_validate(values)


def _challenge(n: int, verdict: str = "uphold") -> Challenge:
    return Challenge.model_validate(
        {
            "finding_id": _fid(n),
            "round": 1,
            "checks": [{"check": c, "result": "pass", "note": ""} for c in SKEPTIC_CHECKS],
            "verdict": verdict,
            "required_actions": ["recheck"] if verdict == "revise" else [],
        }
    )


def _verification(reason: str | None = None) -> VerificationRecord:
    result = {
        "build_id": "b_1",
        "passed": reason is None,
        "items": [],
        "n_numbers": 0,
        "n_failed": 0,
        "verified_at": _T0,
        "duration_ms": 3,
    }
    return VerificationRecord.model_validate({"gate": 1, "result": result, "reason": reason})


def _write[T](fn: Callable[[sqlite3.Connection], T]) -> T:
    return core.run_write(fn, op="test_findings")


def _sql(sql: str, *params: object) -> None:
    _write(lambda c: c.execute(sql, params))


def _insert(*rows: Finding) -> list[bool]:
    return _write(lambda conn: [findings.insert_finding(conn, f) for f in rows])


def _transition(finding_id: str, to: FindingStatus, allowed: set[str], **kw: object) -> bool:
    return _write(
        lambda conn: findings.transition_finding(conn, finding_id, to, allowed, **kw)  # type: ignore[arg-type]
    )


def _get(n: int) -> Finding:
    return findings.get_findings([_fid(n)])[_fid(n)]


def _raw(n: int) -> dict[str, object]:
    row = core.read_one("SELECT * FROM finding WHERE finding_id = ?", (_fid(n),))
    assert row is not None
    return dict(row)


def _run_row(n: int, kind: str, status: str, finished: datetime | None) -> None:
    fin = None if finished is None else clock.format_utc(finished)
    _sql(
        "INSERT INTO run (run_id, kind, depth, profile, config_hash, status, started_at,"
        " finished_at) VALUES (?, ?, 'standard', 'default', 'h', ?, ?, ?)",
        _rid(n),
        kind,
        status,
        clock.format_utc(_T0),
        fin,
    )


# UT06-27 ---------------------------------------------------------------------------------


def test_ut06_27_insert_idempotent_and_round_trips(ops_store: Path) -> None:
    """UT06-27 insert returns True then False; the row reads back as the same Finding."""
    f = _finding(1, challenge=[_challenge(1)], verification=_verification())
    assert _insert(f) == [True]
    assert _insert(f) == [False]
    assert _get(1) == f
    raw = _raw(1)
    assert raw["status"] == "proposed"
    assert raw["numbers"] == core.dump_json(f.model_dump(mode="json")["numbers"], field="t")


def test_ut06_27_insert_check_violation_raises(ops_store: Path) -> None:
    """UT06-27 a CHECK violation raises SchemaViolation instead of returning False."""
    bad = _finding(1).model_copy(update={"confidence": 1.5})
    with pytest.raises(SchemaViolation, match="ops constraint failed"):
        _insert(bad)
    assert findings.get_findings([_fid(1)]) == {}


# Every row of the design 06 §6.5 table: (from, to, allowed_from, extra kwargs).
_LEGAL: tuple[tuple[FindingStatus, FindingStatus, set[str], str], ...] = (
    ("proposed", "proposed", {"proposed"}, "challenge"),
    ("proposed", "challenged", {"proposed"}, "challenge"),
    ("proposed", "rejected", {"proposed", "challenged"}, "verification"),
    ("challenged", "rejected", {"proposed", "challenged"}, "challenge"),
    ("challenged", "revised", {"challenged"}, ""),
    ("proposed", "verified", {"proposed"}, "verification"),
    ("proposed", "merged", {"proposed"}, "merged"),
)


def _kwargs(extra: str) -> dict[str, object]:
    if extra == "challenge":
        return {"append_challenge": _challenge(1, "revise")}
    if extra == "verification":
        return {"verification": _verification()}
    if extra == "merged":
        return {"merged_into": _fid(2)}
    return {}


@pytest.mark.parametrize(("start", "to", "allowed", "extra"), _LEGAL)
def test_ut06_27_every_legal_transition(
    ops_store: Path, start: FindingStatus, to: FindingStatus, allowed: set[str], extra: str
) -> None:
    """UT06-27 each design 06 §6.5 transition returns True and changes only its columns."""
    f = _finding(1, status=start, challenge=[_challenge(1)])
    _insert(f)
    before = _raw(1)
    kwargs = _kwargs(extra)
    assert _transition(_fid(1), to, allowed, **kwargs) is True
    after = _raw(1)
    changed = {k for k in before if before[k] != after[k]}
    expected = {"status"} if start != to else set()
    if extra == "challenge":
        expected.add("challenge")
    elif extra:
        expected.add(extra if extra == "verification" else "merged_into")
    assert changed == expected
    got = _get(1)
    assert got.status == to
    if extra == "challenge":
        assert got.challenge == [_challenge(1), kwargs["append_challenge"]]
    if extra == "verification":
        assert got.verification == kwargs["verification"]
    if extra == "merged":
        assert got.merged_into == _fid(2)


# One illegal transition per status: the target and allowed_from of a real §6.5 row whose
# from-set excludes the current status.
_ILLEGAL: tuple[tuple[FindingStatus, FindingStatus, set[str], str], ...] = (
    ("proposed", "revised", {"challenged"}, ""),
    ("challenged", "verified", {"proposed"}, "verification"),
    ("verified", "rejected", {"proposed", "challenged"}, "challenge"),
    ("rejected", "proposed", {"proposed"}, "challenge"),
    ("revised", "merged", {"proposed"}, "merged"),
    ("merged", "challenged", {"proposed"}, "challenge"),
)


@pytest.mark.parametrize(("start", "to", "allowed", "extra"), _ILLEGAL)
def test_ut06_27_illegal_transition_per_status(
    ops_store: Path, start: FindingStatus, to: FindingStatus, allowed: set[str], extra: str
) -> None:
    """UT06-27 an illegal transition from each status returns False; the row is unchanged."""
    _insert(_finding(1, status=start))
    before = _raw(1)
    assert _transition(_fid(1), to, allowed, **_kwargs(extra)) is False
    assert _raw(1) == before


def test_ut06_27_every_status_has_an_illegal_case() -> None:
    """UT06-27 the illegal table covers every FindingStatus and the legal table every row."""
    assert {start for start, *_ in _ILLEGAL} == set(_STATUSES)
    assert len(_LEGAL) == 7


def test_ut06_27_unknown_finding_and_null_challenge(ops_store: Path) -> None:
    """UT06-27 an unknown id returns False; appending to a NULL challenge column works."""
    assert _transition(_fid(9), "verified", {"proposed"}) is False
    _insert(_finding(1))
    _sql("UPDATE finding SET challenge = NULL WHERE finding_id = ?", _fid(1))
    assert _get(1).challenge == []
    assert _transition(_fid(1), "challenged", {"proposed"}, append_challenge=_challenge(1))
    assert _get(1).challenge == [_challenge(1)]


def test_ut06_27_merged_requires_merged_into(ops_store: Path) -> None:
    """UT06-27 merged without merged_into, or merged_into with another target → ConfigError."""
    _insert(_finding(1))
    with pytest.raises(ConfigError, match="merged_into"):
        _transition(_fid(1), "merged", {"proposed"})
    with pytest.raises(ConfigError, match="merged_into"):
        _transition(_fid(1), "verified", {"proposed"}, merged_into=_fid(2))
    assert _raw(1)["status"] == "proposed"


def test_ut06_27_reexported() -> None:
    """UT06-27 the area's functions are re-exported by herness.store.ops."""
    assert ops.insert_finding is findings.insert_finding
    assert ops.transition_finding is findings.transition_finding
    assert ops.scrub_record_from_findings is findings.scrub_record_from_findings


# UT06-28 ---------------------------------------------------------------------------------


def _seed_reads() -> list[Finding]:
    rows = [
        _finding(1, run=1, task=1, confidence=0.9),
        _finding(2, run=1, task=2, status="verified", entity_id="t2", confidence=0.4),
        _finding(3, run=1, task=1, author_role="skeptic", entity_type="service", entity_id="s1"),
        _finding(4, run=2, task=3, status="rejected"),
    ]
    _insert(*rows)
    return rows


def test_ut06_28_query_findings_filters_and_order(ops_store: Path) -> None:
    """UT06-28 query_findings applies every non-None filter, ordered by created_at, id."""
    f1, f2, f3, f4 = _seed_reads()
    q = findings.query_findings
    assert q(_rid(1), limit=10) == [f1, f2, f3]
    assert q(_rid(2), limit=10) == [f4]
    assert q(_rid(1), statuses={"verified", "rejected"}, limit=10) == [f2]
    assert q(_rid(1), entity_type="team", limit=10) == [f1, f2]
    assert q(_rid(1), entity_ids=["t2", "s1"], limit=10) == [f2, f3]
    assert q(_rid(1), task_ids=[_tid(1)], limit=10) == [f1, f3]
    assert q(_rid(1), author_roles=["skeptic"], limit=10) == [f3]
    assert q(_rid(1), min_confidence=0.5, limit=10) == [f1, f3]
    assert q(_rid(1), statuses=[], limit=10) == []
    assert q(_rid(1), limit=2) == [f1, f2]
    both = q(_rid(1), entity_type="team", entity_ids=["t1"], task_ids=[_tid(1)], limit=500)
    assert both == [f1]


@pytest.mark.parametrize("limit", [0, 501])
def test_ut06_28_limit_bounds(ops_store: Path, limit: int) -> None:
    """UT06-28 limit outside 1-500 raises ValueError."""
    with pytest.raises(ValueError, match="limit"):
        findings.query_findings(_rid(1), limit=limit)
    with pytest.raises(ValueError, match="limit"):
        findings.query_verified_findings_recent(limit=limit)


def test_ut06_28_get_and_list_task_findings(ops_store: Path) -> None:
    """UT06-28 get_findings maps ids to rows (unknown ids absent); list_task_findings orders."""
    f1, f2, f3, _ = _seed_reads()
    assert findings.get_findings([_fid(2), _fid(1), _fid(99)]) == {_fid(1): f1, _fid(2): f2}
    assert findings.get_findings([]) == {}
    assert findings.list_task_findings(_tid(1)) == [f1, f3]
    assert findings.list_task_findings(_tid(99)) == []


def test_ut06_28_invalid_row_raises(ops_store: Path) -> None:
    """UT06-28 a row that does not parse as Finding raises SchemaViolation naming only the id."""
    _insert(_finding(1), _finding(2))
    _sql("UPDATE finding SET entity_type = NULL WHERE finding_id = ?", _fid(1))
    _sql("UPDATE finding SET numbers = '{\"a\": 1}' WHERE finding_id = ?", _fid(2))
    for n in (1, 2):
        with pytest.raises(SchemaViolation, match=f"^finding invalid: finding_id={_fid(n)}$"):
            findings.get_findings([_fid(n)])


def test_ut06_28_recent_verified(ops_store: Path) -> None:
    """UT06-28 verified findings of the max_runs newest done review runs, by run recency."""
    _run_row(1, "funding_review", "done", _T0 + timedelta(hours=1))
    _run_row(2, "org_review", "done", _T0 + timedelta(hours=3))
    _run_row(3, "funding_review", "done", _T0 + timedelta(hours=2))
    _run_row(4, "chat", "done", _T0 + timedelta(hours=9))
    _run_row(5, "funding_review", "failed", _T0 + timedelta(hours=9))
    _run_row(6, "funding_review", "running", None)
    rows = {
        n: _finding(n, run=run, status="verified", entity_id=ent)
        for n, run, ent in (
            (1, 1, "t1"),
            (2, 2, "t1"),
            (3, 2, "t2"),
            (4, 3, "t1"),
            (5, 4, "t1"),
            (6, 5, "t1"),
            (7, 6, "t1"),
        )
    }
    _insert(*rows.values(), _finding(8, run=2, status="proposed"))
    recent = findings.query_verified_findings_recent
    assert recent(entity_type=None, entity_ids=None, limit=50) == [
        rows[2],
        rows[3],
        rows[4],
        rows[1],
    ]
    assert recent(entity_type="team", entity_ids=["t1"], limit=50) == [rows[2], rows[4], rows[1]]
    assert recent(entity_type="service", entity_ids=None, limit=50) == []
    assert recent(entity_type=None, entity_ids=None, limit=50, max_runs=2) == [
        rows[2],
        rows[3],
        rows[4],
    ]
    assert recent(entity_type=None, entity_ids=None, limit=1) == [rows[2]]
    with pytest.raises(ValueError, match="max_runs"):
        recent(entity_type=None, entity_ids=None, limit=1, max_runs=0)


# UT06-95 ---------------------------------------------------------------------------------


def _seed_scrub() -> tuple[Finding, Finding, Finding]:
    by_id = _finding(
        1,
        claim="Spend [[n1]] vs [[n2]] and [[n1]] again",
        numbers=[_number(1, row_key={"issue": _RECORD}), _number(2, _Q2)],
        query_ids=[_Q1, _Q2],
        challenge=[_challenge(1)],
        verification=_verification(),
        status="verified",
    )
    by_key = _finding(
        2,
        claim="Ticket [[n3]] lead time [[n4]]",
        numbers=[_number(3, _Q2, row_key={"k": "ABC-123"}), _number(4)],
        query_ids=[_Q1, _Q2],
    )
    other = _finding(
        3,
        claim="Unrelated [[n1]]",
        numbers=[_number(1, row_key={"issue": "ABC-1234"})],
    )
    _insert(by_id, by_key, other)
    return by_id, by_key, other


def test_ut06_95_scrub_removes_cited_elements(ops_store: Path) -> None:
    """UT06-95 cited elements dropped, markers → [redacted], other data unchanged; idempotent."""
    by_id, by_key, other = _seed_scrub()
    raw_before = {n: _raw(n) for n in (1, 2, 3)}
    scrub = findings.scrub_record_from_findings
    assert _write(lambda conn: scrub(_RECORD, conn=conn)) == 2
    got = findings.get_findings([_fid(1), _fid(2), _fid(3)])
    assert got[_fid(1)].claim == "Spend [redacted] vs [[n2]] and [redacted] again"
    assert got[_fid(1)].numbers == by_id.numbers[1:]
    assert got[_fid(2)].claim == "Ticket [redacted] lead time [[n4]]"
    assert got[_fid(2)].numbers == by_key.numbers[1:]
    assert got[_fid(3)] == other
    for n in (1, 2):
        after = _raw(n)
        changed = {k for k in after if after[k] != raw_before[n][k]}
        assert changed == {"numbers", "claim"}
    assert _write(lambda conn: scrub(_RECORD, conn=conn)) == 0


def test_ut06_95_scrub_key_only_and_edge_ids(ops_store: Path) -> None:
    """UT06-95 an id without two colons is its own key; a substring-only match changes 0 rows."""
    _insert(_finding(1, numbers=[_number(1, row_key={"x": "plain"})]))
    scrub = findings.scrub_record_from_findings
    assert _write(lambda conn: scrub("pla", conn=conn)) == 0
    assert _write(lambda conn: scrub("plain", conn=conn)) == 1
    assert _raw(1)["numbers"] == "[]"
    for bad in ("", "x" * 301, "src:kind:", "::"):
        with pytest.raises(SchemaViolation, match=r"^invalid record_id$"):
            _write(lambda conn, bad=bad: scrub(bad, conn=conn))


def test_ut06_95_reads_skip_fully_scrubbed_rows(ops_store: Path) -> None:
    """UT06-95 after a full scrub every finding read still works; the emptied row is absent."""
    _run_row(1, "funding_review", "done", _T0 + timedelta(hours=1))
    gone = _finding(1, status="verified", numbers=[_number(1, row_key={"x": "plain"})])
    kept = _finding(2, status="verified")
    _insert(gone, kept)
    scrub = findings.scrub_record_from_findings
    assert _write(lambda conn: scrub("plain", conn=conn)) == 1
    assert _raw(1)["numbers"] == "[]"
    assert findings.query_findings(_rid(1), limit=1) == [kept]
    assert findings.get_findings([_fid(1), _fid(2)]) == {_fid(2): kept}
    assert findings.list_task_findings(_tid(1)) == [kept]
    assert findings.query_verified_findings_recent(limit=1) == [kept]


def test_ut06_95_scrub_matches_string_at_depth_four(ops_store: Path) -> None:
    """UT06-95 a string at exactly depth 4 inside an element is matched (depth 5 is not)."""
    at_four = {"a": {"b": {"c": {"d": "plain"}}}}
    _insert(_finding(1, numbers=[_number(1, row_key={"x": "keep"}), _number(2)]))
    _sql(
        "UPDATE finding SET numbers = ? WHERE finding_id = ?",
        core.dump_json([at_four, _number(2)], field="t"),
        _fid(1),
    )
    scrub = findings.scrub_record_from_findings
    assert _write(lambda conn: scrub("plain", conn=conn)) == 1
    assert _raw(1)["numbers"] == core.dump_json([_number(2)], field="t")


def test_ut06_95_scrub_depth_limit_and_invalid_json(ops_store: Path) -> None:
    """UT06-95 values deeper than 4 levels are not matched; invalid numbers JSON raises."""
    deep = {"a": {"b": {"c": {"d": {"e": "plain"}}}}}
    _insert(_finding(1, numbers=[_number(1, row_key={"x": "keep"})]))
    _sql(
        "UPDATE finding SET numbers = ? WHERE finding_id = ?",
        core.dump_json([deep], field="t"),
        _fid(1),
    )
    scrub = findings.scrub_record_from_findings
    assert _write(lambda conn: scrub("plain", conn=conn)) == 0
    _sql("UPDATE finding SET numbers = '{\"plain\": 1}' WHERE finding_id = ?", _fid(1))
    with pytest.raises(SchemaViolation, match=f"^finding invalid: finding_id={_fid(1)}$"):
        _write(lambda conn: scrub("plain", conn=conn))


def test_ut06_95_scrub_unparseable_numbers_raises(
    ops_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT06-95 numbers JSON that load_json rejects raises the finding-invalid error."""
    _insert(_finding(1, numbers=[_number(1, row_key={"x": "plain"})]))

    def reject(text: str | None, *, field: str) -> object:
        msg = f"invalid JSON in {field}"
        raise SchemaViolation(msg)

    monkeypatch.setattr(core, "load_json", reject)
    scrub = findings.scrub_record_from_findings
    with pytest.raises(SchemaViolation, match=f"^finding invalid: finding_id={_fid(1)}$"):
        _write(lambda conn: scrub("plain", conn=conn))


def test_ut06_95_scrub_matches_json_escaped_values(ops_store: Path) -> None:
    """UT06-95 a record id with a quote or backslash is still found in the stored JSON."""
    odd = 'src:kind:A"B\\C'
    _insert(_finding(1, numbers=[_number(1, row_key={"x": odd})]))
    scrub = findings.scrub_record_from_findings
    assert _write(lambda conn: scrub(odd, conn=conn)) == 1
    assert _raw(1)["numbers"] == "[]"

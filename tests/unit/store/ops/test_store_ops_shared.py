"""Unit tests for herness.store.ops.shared (impl 02 U02-55 … U02-60; UT02-43 … UT02-47,
UT02-75, UT02-76, UT02-79). IT02-11 (service map over approved suggestions) is T02-15's.
"""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from structlog.testing import capture_logs
from tests.support.config_tree import write_full_config

from herness.core import config as c
from herness.core.errors import ConfigError, SchemaViolation
from herness.store.errors import NotFoundError, ReviewItemConflict
from herness.store.ops import core, shared
from herness.store.ops.migrate import migrate
from herness.store.ops.shared import (
    ReviewItem,
    approved_mapping_suggestions,
    create_review_item,
    decide_review_item,
    get_review_item,
    list_review_items,
)

pytestmark = pytest.mark.unit

USER = "ab" * 16  # a 32-hex user_ref (spec 09)
T0 = datetime.datetime(2026, 9, 26, 10, 0, tzinfo=datetime.UTC)
_REV = re.compile(r"rev_[0-9A-HJKMNP-TV-Z]{26}")

type AuditCalls = list[tuple[str, str, dict[str, Any]]]


def _t(minutes: int) -> datetime.datetime:
    return T0 + datetime.timedelta(minutes=minutes)


@pytest.fixture
def store(ops_store: Path) -> Path:
    """The interim `ops_store` with migrations 001-006 applied."""
    migrate()
    return ops_store


@pytest.fixture
def audit_calls(monkeypatch: pytest.MonkeyPatch) -> AuditCalls:
    """Audit capture fixture: records every `audit` call made by `shared`."""
    calls: AuditCalls = []

    def fake(event: str, actor: str, **fields: Any) -> None:
        calls.append((event, actor, fields))

    monkeypatch.setattr(shared, "audit", fake)
    return calls


def _status(item_id: str) -> str:
    row = core.read_one("SELECT status FROM review_item WHERE item_id = ?", (item_id,))
    assert row is not None
    return str(row["status"])


def _raw_row(**overrides: object) -> sqlite3.Row:
    values: dict[str, object] = {
        "item_id": "rev_" + "0" * 26,
        "kind": "label_check",
        "payload": '{"a":1}',
        "status": "pending",
        "created_at": "2026-09-26T10:00:00.000000Z",
        "decided_by": None,
        "decided_at": None,
        "note": None,
    } | overrides
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(f"? AS {k}" for k in values)
    row: sqlite3.Row = conn.execute(f"SELECT {cols}", tuple(values.values())).fetchone()
    conn.close()
    return row


# --- UT02-43 --------------------------------------------------------------------------------


def test_ut02_43_create_returns_pending_item_and_round_trips(store: Path) -> None:
    """UT02-43 create: `rev_` ID; pending; payload round-trips."""
    payload = {"service": "payments", "nested": {"n": [1, 2]}, "text": "é"}
    item_id = create_review_item("mapping_suggestion", payload, now=T0)
    assert _REV.fullmatch(item_id)
    item = get_review_item(item_id)
    assert item == ReviewItem(
        item_id=item_id,
        kind="mapping_suggestion",
        payload=MappingProxyType(payload),
        status="pending",
        created_at=T0,
        decided_by=None,
        decided_at=None,
        note=None,
    )
    assert isinstance(item.payload, MappingProxyType)
    assert dict(item.payload) == payload


def test_ut02_43_create_with_conn_joins_the_callers_transaction(store: Path) -> None:
    """UT02-43 create with `conn` inside a `run_write` callback commits with the caller."""

    def fn(conn: sqlite3.Connection) -> str:
        return create_review_item("weight_change", {"w": "1"}, now=T0, conn=conn)

    item_id = core.run_write(fn, op="test_create")
    assert get_review_item(item_id).kind == "weight_change"

    def failing(conn: sqlite3.Connection) -> None:
        create_review_item("weight_change", {"w": "2"}, now=T0, conn=conn)
        msg = "caller fails"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError):
        core.run_write(failing, op="test_create")
    assert len(list_review_items()) == 1


@pytest.mark.parametrize(
    ("kind", "payload", "now"),
    [
        ("label_check", ["not", "a", "mapping"], T0),
        ("bogus", {"a": 1}, T0),
        ("label_check", {"a": 1}, datetime.datetime(2026, 9, 26)),  # noqa: DTZ001 - naive on purpose
        ("label_check", {"a": object()}, T0),
    ],
)
def test_ut02_43_create_rejects_bad_input(
    store: Path, kind: str, payload: object, now: datetime.datetime
) -> None:
    """UT02-43 bad payload, unknown kind or naive `now` → SchemaViolation, nothing written."""
    with pytest.raises(SchemaViolation):
        create_review_item(kind, payload, now=now)  # type: ignore[arg-type]
    assert list_review_items() == []


def test_ut02_43_from_row_parses_and_checks_rows() -> None:
    """UT02-43 `ReviewItem.from_row` parses decided rows and rejects corrupt ones."""
    item = ReviewItem.from_row(
        _raw_row(
            status="approved",
            decided_by="system",
            decided_at="2026-09-26T11:00:00.000000Z",
            note="ok",
        )
    )
    assert (item.status, item.decided_by, item.decided_at, item.note) == (
        "approved",
        "system",
        _t(60),
        "ok",
    )
    for bad in (
        {"payload": "[1]"},
        {"payload": "{bad"},
        {"kind": "nope"},
        {"status": "nope"},
        {"created_at": "yesterday"},
        {"status": "approved"},  # decided fields missing: invariant broken
        {"decided_by": "system"},  # pending with a decider
    ):
        with pytest.raises(SchemaViolation):
            ReviewItem.from_row(_raw_row(**bad))


# --- UT02-44 --------------------------------------------------------------------------------


@pytest.fixture
def real_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[AuditCalls, Path]]:
    """Capture `audit` calls and pass them on to the real T10-05 audit writer."""
    cfg = c.init_config(config_dir=write_full_config(tmp_path / "cfgroot"), env={})
    calls: AuditCalls = []
    real: Callable[..., None] = shared.audit

    def spy(event: str, actor: str, **fields: Any) -> None:
        calls.append((event, actor, fields))
        real(event, actor, **fields)

    monkeypatch.setattr(shared, "audit", spy)
    yield calls, Path(cfg.paths.logs)
    c.reset_config()


def test_ut02_44_approve_updates_row_and_audits_once(
    store: Path, real_audit: tuple[AuditCalls, Path]
) -> None:
    """UT02-44 approve: row updated; one `review_decision` audit call with `note_len`."""
    calls, logs = real_audit
    item_id = create_review_item("label_check", {"question": "q1"}, now=T0)
    note = '{"answer": "yes"}'
    item = decide_review_item(item_id, "approved", decided_by=USER, note=note, now=_t(5))
    assert (item.status, item.decided_by, item.decided_at, item.note) == (
        "approved",
        USER,
        _t(5),
        note,
    )
    assert get_review_item(item_id) == item
    fields = {"item_id": item_id, "kind": "label_check", "status": "approved"}
    fields |= {"decided_by": USER, "note_len": len(note)}
    assert calls == [("review_decision", USER, fields)]
    (line,) = [json.loads(x) for p in logs.glob("audit-*.jsonl") for x in p.read_text().split()]
    assert (line["event"], line["actor"], line["fields"]) == ("review_decision", USER, fields)


@pytest.mark.parametrize("note", ["yes", "[1, 2]", '"answer"', "{bad", "[" * 1999])
def test_ut02_44_label_check_note_must_be_a_json_object(
    store: Path, audit_calls: AuditCalls, note: str
) -> None:
    """UT02-44 a `label_check` note that is not a JSON object string → ConfigError, not echoed."""
    item_id = create_review_item("label_check", {"question": "q1"}, now=T0)
    with pytest.raises(ConfigError) as bad:
        decide_review_item(item_id, "approved", decided_by=USER, note=note, now=_t(1))
    assert note[:4] not in str(bad.value)
    assert _status(item_id) == "pending"
    assert audit_calls == []
    other = create_review_item("weight_change", {"w": "1"}, now=T0)
    assert decide_review_item(other, "approved", decided_by=USER, note=note, now=_t(1)).note == note


def test_ut02_44_note_none_audits_zero_length(store: Path, audit_calls: AuditCalls) -> None:
    """UT02-44 a decision without a note audits `note_len` 0."""
    item_id = create_review_item("weight_change", {"w": "1"}, now=T0)
    decide_review_item(item_id, "rejected", decided_by="system", now=_t(1))
    assert audit_calls[0][2]["note_len"] == 0


# --- UT02-45 --------------------------------------------------------------------------------


def test_ut02_45_decided_item_conflicts_and_unknown_id_not_found(
    store: Path, audit_calls: AuditCalls
) -> None:
    """UT02-45 deciding a decided item → ReviewItemConflict; unknown ID → NotFoundError."""
    item_id = create_review_item("label_check", {"question": "q1"}, now=T0)
    decide_review_item(item_id, "approved", decided_by=USER, now=_t(1))
    with pytest.raises(ReviewItemConflict) as conflict:
        decide_review_item(item_id, "rejected", decided_by=USER, now=_t(2))
    assert (conflict.value.item_id, conflict.value.current_status) == (item_id, "approved")
    assert len(audit_calls) == 1
    unknown = "rev_" + "1" * 26
    for call in (
        lambda: get_review_item(unknown),
        lambda: decide_review_item(unknown, "approved", decided_by=USER, now=_t(3)),
    ):
        with pytest.raises(NotFoundError) as missing:
            call()
        assert (missing.value.kind, missing.value.key) == ("review_item", unknown)


def test_ut02_45_malformed_id_is_not_found_without_echo(store: Path) -> None:
    """UT02-45 a malformed ID raises NotFoundError without echoing the input."""
    with pytest.raises(NotFoundError) as missing:
        get_review_item("rev_<script>")
    assert "<script>" not in str(missing.value)
    assert missing.value.key == "invalid"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "pending"},
        {"decided_by": "Alice"},
        {"note": 5},
        {"now": datetime.datetime(2026, 9, 26)},  # noqa: DTZ001 - naive on purpose
        {"item_id": 7},
    ],
)
def test_ut02_45_decide_rejects_bad_arguments(
    store: Path, audit_calls: AuditCalls, kwargs: dict[str, Any]
) -> None:
    """UT02-45 invalid decision arguments → ConfigError before any write."""
    item_id = create_review_item("label_check", {"question": "q1"}, now=T0)
    args: dict[str, Any] = {"item_id": item_id, "status": "approved"}
    args |= {"decided_by": USER, "note": None, "now": _t(1)} | kwargs
    with pytest.raises(ConfigError):
        decide_review_item(
            args["item_id"],
            args["status"],
            decided_by=args["decided_by"],
            note=args["note"],
            now=args["now"],
        )
    assert _status(item_id) == "pending"
    assert audit_calls == []


# --- UT02-46 --------------------------------------------------------------------------------


def test_ut02_46_list_filters_and_pages_in_created_order(
    store: Path, audit_calls: AuditCalls
) -> None:
    """UT02-46 list with filters and paging: order and filters correct."""
    kinds = ["label_check", "mapping_suggestion", "label_check", "weight_change", "label_check"]
    ids = [create_review_item(k, {"n": str(i)}, now=_t(10 - i)) for i, k in enumerate(kinds)]
    decide_review_item(ids[2], "approved", decided_by=USER, now=_t(20))
    by_created = [ids[4], ids[3], ids[2], ids[1], ids[0]]
    assert [i.item_id for i in list_review_items()] == by_created
    assert [i.item_id for i in list_review_items(limit=2, offset=1)] == by_created[1:3]
    assert [i.item_id for i in list_review_items(kind="label_check")] == [ids[4], ids[2], ids[0]]
    labels_pending = list_review_items(kind="label_check", status="pending")
    assert [i.item_id for i in labels_pending] == [ids[4], ids[0]]
    assert [i.item_id for i in list_review_items(statuses=["approved"])] == [ids[2]]
    assert list_review_items(statuses=("rejected",)) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 5001},
        {"limit": 0},
        {"limit": True},
        {"offset": -1},
        {"status": "pending", "statuses": ("pending",)},
        {"statuses": ()},
        {"statuses": ("pending", "pending")},
        {"statuses": "pending"},
        {"statuses": ("done",)},
        {"status": "done"},
        {"kind": "nope"},
        {"decided_after": "2026-09-26"},
        {"decided_after": datetime.datetime(2026, 9, 26)},  # noqa: DTZ001 - naive on purpose
        {"decided_after": (T0, 5)},
        {"decided_after": (T0, "x" * 65)},
        {"payload_match": {}},
        {"payload_match": {"k": 1}},
        {"payload_match": {"k": "v" * 1025}},
        {"payload_match": ["k"]},
    ],
)
def test_ut02_46_list_rejects_bad_arguments(store: Path, kwargs: dict[str, Any]) -> None:
    """UT02-46 `limit=5001`, `status` with `statuses` and other bad arguments → ConfigError."""
    with pytest.raises(ConfigError):
        list_review_items(**kwargs)


# --- UT02-47 --------------------------------------------------------------------------------


def test_ut02_47_only_approved_suggestions_in_decision_order(
    store: Path, audit_calls: AuditCalls
) -> None:
    """UT02-47 approved and pending suggestions: only approved, ordered."""
    a, b, c_, d, e = (
        create_review_item("mapping_suggestion", {"n": str(i)}, now=_t(i)) for i in range(5)
    )
    label = create_review_item("label_check", {"n": "x"}, now=T0)
    decide_review_item(b, "approved", decided_by=USER, now=_t(30))
    decide_review_item(a, "approved", decided_by=USER, now=_t(40))
    decide_review_item(d, "rejected", decided_by=USER, now=_t(10))
    decide_review_item(e, "approved", decided_by=USER, now=_t(30))
    decide_review_item(label, "approved", decided_by=USER, now=_t(5))
    got = approved_mapping_suggestions()
    assert [i.item_id for i in got] == [*sorted([b, e]), a]
    assert c_ not in {i.item_id for i in got}
    assert all(isinstance(i.payload, MappingProxyType) for i in got)


# --- UT02-75 --------------------------------------------------------------------------------


def test_ut02_75_keyset_pages_decided_items_once(store: Path, audit_calls: AuditCalls) -> None:
    """UT02-75 keyset paging by (`decided_at`, `item_id`) returns every decided item once."""
    ids = [create_review_item("label_check", {"n": str(i)}, now=_t(i)) for i in range(8)]
    decided_at = [_t(50), _t(50), _t(50), _t(60), _t(40), _t(60)]
    for n, (item_id, when) in enumerate(zip(ids[:6], decided_at, strict=True)):
        decide_review_item(item_id, "approved" if n % 2 else "rejected", decided_by=USER, now=when)
    expected = [i for _, i in sorted(zip(decided_at, ids[:6], strict=True))]
    seen: list[str] = []
    cursor: tuple[datetime.datetime, str] = (T0, "")
    while page := list_review_items(
        statuses=("approved", "rejected"), decided_after=cursor, limit=2
    ):
        assert len(page) <= 2
        seen += [i.item_id for i in page]
        last = page[-1]
        assert last.decided_at is not None
        cursor = (last.decided_at, last.item_id)
    assert seen == expected
    assert not set(ids[6:]) & set(seen)
    with pytest.raises(ConfigError):
        list_review_items(decided_after=cursor, offset=1)
    later = list_review_items(decided_after=_t(50))
    assert [i.item_id for i in later] == [i for i in expected if i in {ids[3], ids[5]}]
    everything = list_review_items(statuses=("approved", "rejected", "pending"), decided_after=T0)
    assert [i.item_id for i in everything] == expected  # decided rows only


# --- UT02-76 --------------------------------------------------------------------------------


def test_ut02_76_memory_write_decisions_need_the_callers_transaction(
    store: Path, audit_calls: AuditCalls
) -> None:
    """UT02-76 `memory_write` needs `conn` (R-33) except the system purge rejection (R-54)."""
    mem = create_review_item("memory_write", {"memory_id": "mem_1"}, now=T0)
    label = create_review_item("label_check", {"question": "q1"}, now=T0)
    with pytest.raises(ConfigError, match=r"MemoryStore\.approve or MemoryStore\.reject"):
        decide_review_item(mem, "approved", decided_by=USER, now=_t(1))
    assert _status(mem) == "pending"
    assert audit_calls == []

    def raising(conn: sqlite3.Connection) -> None:
        decide_review_item(mem, "approved", decided_by=USER, now=_t(2), conn=conn)
        msg = "memory update failed"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError):
        core.run_write(raising, op="memory_approve")
    assert _status(mem) == "pending"
    assert len(audit_calls) == 1  # the raising callback audited once before its rollback
    audit_calls.clear()  # the attempt line of the rolled-back transaction (accepted, §7.7)

    def committing(conn: sqlite3.Connection) -> ReviewItem:
        return decide_review_item(mem, "approved", decided_by=USER, now=_t(3), conn=conn)

    assert core.run_write(committing, op="memory_approve").status == "approved"
    assert _status(mem) == "approved"
    assert len(audit_calls) == 1

    mem2 = create_review_item("memory_write", {"memory_id": "mem_2"}, now=T0)
    audit_calls.clear()
    purged = decide_review_item(mem2, "rejected", decided_by="system", now=_t(4))
    assert (purged.status, purged.decided_by) == ("rejected", "system")
    assert len(audit_calls) == 1
    with pytest.raises(ConfigError):  # a user rejection of a memory_write still needs conn
        decide_review_item(
            create_review_item("memory_write", {"memory_id": "mem_3"}, now=T0),
            "rejected",
            decided_by=USER,
            now=_t(5),
        )
    assert decide_review_item(label, "approved", decided_by=USER, now=_t(6)).status == "approved"


def test_ut02_76_commit_failure_after_audit_logs_orphan(
    store: Path, audit_calls: AuditCalls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-76 a write failure after the audit line re-raises and logs `audit_orphan`."""
    item_id = create_review_item("label_check", {"question": "q1"}, now=T0)
    real_run_write = core.run_write

    def run_write_then_fail(fn: Callable[[sqlite3.Connection], object], *, op: str) -> object:
        def wrapped(conn: sqlite3.Connection) -> object:
            fn(conn)
            msg = "disk I/O error"
            raise sqlite3.OperationalError(msg)

        return real_run_write(wrapped, op=op)

    monkeypatch.setattr(shared, "run_write", run_write_then_fail)
    with capture_logs() as logs, pytest.raises(SchemaViolation):
        decide_review_item(item_id, "approved", decided_by=USER, now=_t(1))
    orphans = [e for e in logs if e["event"] == "store.ops.audit_orphan"]
    assert [(e["item_id"], e["log_level"]) for e in orphans] == [(item_id, "error")]
    assert not [e for e in logs if e["event"] == "store.ops.review_item_decided"]
    assert _status(item_id) == "pending"


# --- UT02-79 --------------------------------------------------------------------------------


def test_ut02_79_payload_match_filters_on_every_key(store: Path) -> None:
    """UT02-79 `payload_match` keeps only items matching every key; values are data (RQ-02)."""
    made = {
        (q, p): create_review_item("label_check", {"question": q, "purpose": p}, now=T0)
        for q in ("q1", "q2")
        for p in ("spot_check", "gold")
    }
    got = list_review_items(payload_match={"question": "q1", "purpose": "gold"})
    assert [i.item_id for i in got] == [made["q1", "gold"]]
    tricky = "it's 100% ' OR '1'='1"
    quoted = create_review_item("label_check", {"question": tricky, "purpose": "gold"}, now=T0)
    assert [i.item_id for i in list_review_items(payload_match={"question": tricky})] == [quoted]
    assert list_review_items(payload_match={"question": "it's 100%"}) == []
    assert list_review_items(payload_match={"question": "%"}) == []
    assert list_review_items(kind="mapping_suggestion", payload_match={"question": "q1"}) == []
    with pytest.raises(ConfigError):
        list_review_items(payload_match={"a.b": "x"})
    with pytest.raises(ConfigError):
        list_review_items(payload_match={f"k{n}": "x" for n in range(9)})
    eight = list_review_items(payload_match={f"k{n}": "x" for n in range(8)})
    assert eight == []


def test_ut02_79_payload_match_with_decided_after(store: Path, audit_calls: AuditCalls) -> None:
    """UT02-79 `payload_match` also applies to the decided-order query."""
    a = create_review_item("label_check", {"purpose": "gold"}, now=T0)
    b = create_review_item("label_check", {"purpose": "spot_check"}, now=T0)
    for item_id in (a, b):
        decide_review_item(item_id, "approved", decided_by=USER, now=_t(1))
    got = list_review_items(decided_after=T0, payload_match={"purpose": "gold"})
    assert [i.item_id for i in got] == [a]

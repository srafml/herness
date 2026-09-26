"""Tests for herness.core.jobs.queue: idem keys, payload validation, submit and enqueue
(impl 08 U08-43 to U08-47; T08-12). Claim, cancel, retry and reads: test_jobs_queue_claim.py.
"""

from __future__ import annotations

import decimal
import re
from datetime import UTC, datetime, timedelta
from typing import Any, get_args

import pytest
import structlog
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue
from tests.unit.core.jobs._queue_env import jobs_db  # noqa: F401 - fixture

from herness.core import secrets
from herness.core.errors import SchemaViolation
from herness.core.ids import canonical_json
from herness.core.jobs import queue
from herness.core.jobs.ports import SchedCheck, require_jobs_backend
from herness.core.resilience import process_state
from herness.core.types import JobKind, JobSpec
from herness.store.ops.core import read_one

pytestmark = pytest.mark.unit

KINDS: tuple[JobKind, ...] = get_args(JobKind.__value__)
SECRET = "known-secret-value-4242"  # noqa: S105  # pragma: allowlist secret
_KEYS = st.from_regex(r"[a-z_][a-z0-9_]{0,8}", fullmatch=True)
_LEAVES = st.none() | st.booleans() | st.integers(-(10**6), 10**6) | st.text(max_size=12)


def _counter(kind: str) -> float:
    key = ("herness_jobs_enqueued_total", (("kind", kind),), "jobs")
    return process_state().metric_buffer.counters.get(key, 0.0)


def _nested(depth: int) -> dict[str, Any]:
    payload: dict[str, Any] = {"leaf": 1}
    for _ in range(depth - 1):
        payload = {"a": payload}
    return payload


def _finish(job_id: str) -> None:
    row = queue.claim(owner="h:1:cli", allowed_classes=["none"], job_id=job_id)
    assert row is not None
    now = datetime.now(UTC)
    assert require_jobs_backend().finish_done(job_id, "h:1:cli", {}, now)


# --- UT08-51, PT08-06: default_idem_key ---------------------------------------------------


def test_ut08_51_idem_key_ignores_key_order() -> None:
    """UT08-51 reordered payload keys hash equally; prefix `kind:` and 16 hex chars."""
    first = queue.default_idem_key("sync", {"source": "jira", "entity": {"a": 1, "b": [2, 3]}})
    second = queue.default_idem_key("sync", {"entity": {"b": [2, 3], "a": 1}, "source": "jira"})
    assert first == second
    assert re.fullmatch(r"sync:[0-9a-f]{16}", first)
    assert queue.default_idem_key("review", {}) != queue.default_idem_key("sync", {})


@given(st.dictionaries(_KEYS, _LEAVES, max_size=6), st.randoms(use_true_random=False))
def test_pt08_06_permutations_give_one_key(payload: dict[str, JsonValue], rng: Any) -> None:
    """PT08-06 any key-order permutation of a payload gives the same idem key."""
    items = list(payload.items())
    rng.shuffle(items)
    assert queue.default_idem_key("eval", dict(items)) == queue.default_idem_key("eval", payload)


@given(st.dictionaries(_KEYS, _LEAVES, max_size=4), st.dictionaries(_KEYS, _LEAVES, max_size=4))
def test_pt08_06_distinct_payloads_give_distinct_keys(
    left: dict[str, JsonValue], right: dict[str, JsonValue]
) -> None:
    """PT08-06 distinct payloads give distinct keys in samples."""
    same = canonical_json(left) == canonical_json(right)
    assert (queue.default_idem_key("sync", left) == queue.default_idem_key("sync", right)) is same


# --- UT08-55, ST08-03: validate_payload ---------------------------------------------------


@pytest.mark.usefixtures("jobs_db")
def test_ut08_55_valid_payload_returns_canonical_bytes() -> None:
    """UT08-55 a valid payload returns its canonical JSON bytes; depth 8 is allowed."""
    payload = {"b": [1, 2.5, None, True], "a": "text", "deep": _nested(7)}
    assert queue.validate_payload(payload) == canonical_json(payload).encode("utf-8")


@pytest.mark.usefixtures("jobs_db")
@pytest.mark.parametrize(
    ("payload", "reason", "value"),
    [
        ({"blob": "x" * 70_000}, "larger than 65536 bytes", "x" * 50),
        ({"note": f"id {SECRET} end"}, "known secret value", SECRET),
        (
            {"header": "Authorization: Bearer synthetic_token_x"},
            "credential or URL token",
            "synthetic_token_x",
        ),
        (_nested(9), "nested deeper than 8", "leaf"),
        ({"a b": 1}, "invalid key name", "a b"),
        ({"k": [[[[[[[["x"]]]]]]]]}, "nested deeper than 8", '"x"'),
        ({"k": 1, 2: 3}, "invalid key name", "2"),
        ({"when": datetime(2026, 1, 1, tzinfo=UTC)}, "not JSON: datetime", "2026"),
        ({"amount": decimal.Decimal("1.5")}, "not JSON: Decimal", "1.5"),
        ({"tags": {"a", "b"}}, "not JSON: set", "'a'"),
        ({"raw": b"bytes-value"}, "not JSON: bytes", "bytes-value"),
        ({"x": float("nan")}, "non-finite number", "nan"),
    ],
)
def test_ut08_55_rejected_payloads(payload: dict[str, Any], reason: str, value: str) -> None:
    """UT08-55 oversize, known secret, bearer token, depth 9, bad key and non-JSON values are
    each a SchemaViolation whose message names the reason but never the value."""
    secrets._remember(SECRET)
    with pytest.raises(SchemaViolation) as caught:
        queue.validate_payload(payload)
    assert caught.value.message.startswith("job payload rejected: ")
    assert reason in caught.value.message
    assert value not in caught.value.message


@pytest.mark.usefixtures("jobs_db")
def test_ut08_55_short_known_values_are_ignored_and_non_object_refused() -> None:
    """UT08-55 known values shorter than 8 chars are not matched; a non-object is refused."""
    secrets._remember("abc1234")
    assert queue.validate_payload({"note": "abc1234"}) == b'{"note":"abc1234"}'
    with pytest.raises(SchemaViolation, match="payload is not an object"):
        queue.validate_payload(["a"])  # type: ignore[arg-type]


def _job_count() -> int:
    row = read_one("SELECT COUNT(*) FROM job", ())
    return 0 if row is None else int(row[0])


@pytest.mark.usefixtures("jobs_db")
@pytest.mark.parametrize(
    "payload",
    [
        {"note": f"x{SECRET}y"},
        {"login": "password=synthetic_pw_123"},
        {"blob": "y" * (65_537 - len('{"blob":""}'))},
    ],
    ids=["known-secret", "password", "65537-bytes"],
)
def test_st08_03_secret_or_oversize_payload_stores_no_row(payload: dict[str, JsonValue]) -> None:
    """ST08-03 a secret value, `password=synthetic_pw_123` or 65 537 bytes: SchemaViolation,
    no job row, and a `jobs.job.rejected` warning without the value."""
    secrets._remember(SECRET)
    with structlog.testing.capture_logs() as logs, pytest.raises(SchemaViolation):
        queue.enqueue("sync", payload, "none")
    assert _job_count() == 0
    rejected = [line for line in logs if line["event"] == "jobs.job.rejected"]
    assert [(line["log_level"], line["kind"]) for line in rejected] == [("warning", "sync")]
    assert SECRET not in str(logs)
    assert "synthetic_pw_123" not in str(logs)


# --- UT08-52, UT08-53 core half, UT08-106: submit and enqueue -----------------------------


@pytest.mark.usefixtures("jobs_db")
def test_ut08_52_enqueue_dedupes_until_finished() -> None:
    """UT08-52 same idem key twice → same id, `created` false; after finish a new id."""
    spec = JobSpec(kind="sync", payload={"source": "jira"}, gpu_class="none", idem_key="sync:jira")
    first, created = queue.submit(spec)
    assert created is True
    assert first.startswith("job_")
    assert queue.submit(spec) == (first, False)
    assert queue.enqueue("sync", {"source": "jira"}, "none", idem_key="sync:jira") == first
    _finish(first)
    again = queue.enqueue("sync", {"source": "jira"}, "none", idem_key="sync:jira")
    assert again != first
    assert _counter("sync") == 2.0


@pytest.mark.usefixtures("jobs_db")
def test_ut08_53_dedupe_logs_debug_and_counts_nothing() -> None:
    """UT08-53 (core half) created → INFO + counter; deduped → DEBUG, no counter."""
    with structlog.testing.capture_logs() as logs:
        job_id = queue.enqueue("review", {"pipeline": "p"}, "reasoning")
        assert queue.enqueue("review", {"pipeline": "p"}, "reasoning") == job_id
    enq = [line for line in logs if line["event"] == "jobs.job.enqueued"]
    assert [(line["log_level"], line["created"]) for line in enq] == [
        ("info", True),
        ("debug", False),
    ]
    assert enq[0]["job_id"] == job_id
    assert enq[0]["priority"] == 40
    assert _counter("review") == 1.0


@pytest.mark.usefixtures("jobs_db")
def test_ut08_53_sched_check_blocks_a_second_fire() -> None:
    """UT08-53 a finished job of the same schedule fire blocks re-creation (sched_check)."""
    check = SchedCheck("nightly", "2026-09-01T19:00:00.000000Z")
    spec = JobSpec(
        kind="build_pipeline",
        payload={"schedule": "nightly", "fire_at": "2026-09-01T19:00:00.000000Z"},
        gpu_class="none",
    )
    job_id, created = queue.submit(spec, sched_check=check)
    assert created
    _finish(job_id)
    assert queue.submit(spec, sched_check=check) == (job_id, False)


@pytest.mark.usefixtures("jobs_db")
def test_ut08_106_default_priorities_and_explicit_value() -> None:
    """UT08-106 `priority=None` stores DEFAULT_PRIORITY[kind] for every kind; 10 is kept."""
    assert set(queue.DEFAULT_PRIORITY) == set(KINDS)
    assert queue.MANUAL_PRIORITY == 80
    assert frozenset({"build_pipeline"}) == queue.GPU_SLOT_KINDS
    for kind in KINDS:
        job_id = queue.enqueue(kind, {"n": 1}, "none")
        assert queue.get(job_id).priority == queue.DEFAULT_PRIORITY[kind]
    explicit = queue.enqueue("sync", {"n": 2}, "none", priority=10)
    row = queue.get(explicit)
    assert row.priority == 10
    assert row.max_attempts == 3  # R.jobs.max_attempts["sync"]
    assert row.idem_key == queue.default_idem_key("sync", {"n": 2})


@pytest.mark.usefixtures("jobs_db")
def test_ut08_106_enqueue_keeps_explicit_fields() -> None:
    """UT08-106 explicit scheduled_for and max_attempts are stored as given."""
    when = datetime(2030, 1, 1, tzinfo=UTC)
    job_id = queue.enqueue("eval", {"suite": "g"}, "reasoning", 20, when, max_attempts=7)
    row = queue.get(job_id)
    assert (row.scheduled_for, row.max_attempts, row.status) == (when, 7, "queued")
    assert row.payload == {"suite": "g"}


@pytest.mark.usefixtures("jobs_db")
@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"kind": "bogus"}, "kind"),
        ({"priority": 101}, "priority"),
        ({"gpu_class": "huge"}, "gpu_class"),
        ({"scheduled_for": datetime(2030, 1, 1) + timedelta(0)}, "scheduled_for"),  # noqa: DTZ001
        ({"idem_key": "has space"}, "idem_key"),
    ],
)
def test_ut08_106_invalid_spec_names_the_field(kwargs: dict[str, Any], field: str) -> None:
    """UT08-106 an invalid enqueue argument → SchemaViolation naming the field (U08-47)."""
    args: dict[str, Any] = {"kind": "sync", "payload": {}, "gpu_class": "none"} | kwargs
    with pytest.raises(SchemaViolation) as caught:
        queue.enqueue(**args)
    assert caught.value.message == f"invalid job spec field: {field}"

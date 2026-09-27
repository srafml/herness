"""Unit tests for herness.harness.blackboard (impl 06 U06-51 … U06-59, T06-08).

UT06-35 … UT06-42 and the security tests ST06-01, ST06-05 run on the migrated tmp ops store
with the SQLite jobs backend bound (`bb_env` in `_blackboard_env`).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, get_args

import pytest
import structlog
from pydantic import ValidationError
from tests.unit.harness import _blackboard_env as env_mod
from tests.unit.harness._blackboard_env import (
    META_QID,
    PLANTED_NAME,
    BbEnv,
    add_running_task,
    args,
    challenge,
    checkpoint_of,
    task_spec,
    verification,
)

from herness.core.errors import ConfigError, HernessError, StoreBusy, ToolInputError
from herness.core.jobs.ports import CheckpointKey
from herness.core.jobs.tasks import save_checkpoint
from herness.core.types import FindingStatus
from herness.harness import blackboard as bbmod
from herness.harness.blackboard import Blackboard, FindingFilter
from herness.store.ops import (
    get_findings,
    get_task,
    list_task_findings,
    read_all,
    read_one,
    run_write,
)

pytestmark = pytest.mark.unit

bb_env = env_mod.bb_env  # fixtures
test_redactor = env_mod.test_redactor

_ALL: frozenset[FindingStatus] = frozenset(get_args(FindingStatus.__value__))


def _count() -> int:
    row = read_one("SELECT count(*) AS n FROM finding")
    assert row is not None
    return int(row["n"])


def _post(env: BbEnv, **overrides: object) -> str:
    return env.bb.post(env.ctx(), **args(env.ops_qid, **overrides))


def _status(finding_id: str) -> str:
    return get_findings([finding_id])[finding_id].status


# --- UT06-35 FindingFilter ------------------------------------------------------------------


def test_ut06_35_default_statuses_exclude_superseded() -> None:
    """UT06-35 defaults: effective statuses exclude revised and merged."""
    flt = FindingFilter(run_id="run_x")
    assert flt.effective_statuses() == _ALL - {"revised", "merged"}
    assert FindingFilter(run_id="r", include_superseded=True).effective_statuses() == _ALL
    assert FindingFilter(run_id="r", status={"merged"}).effective_statuses() == {"merged"}
    assert flt.limit == 500


def test_ut06_35_filter_bounds_and_extra_forbidden() -> None:
    """UT06-35 limit 1-500, confidence 0-1, ≤ 50 entity ids, ≤ 200 task ids, extra forbidden."""
    for bad in (
        {"limit": 0},
        {"limit": 501},
        {"min_confidence": 1.5},
        {"entity_ids": {f"e{i}" for i in range(51)}},
        {"task_ids": {f"t{i}" for i in range(201)}},
        {"unknown": 1},
    ):
        with pytest.raises(ValidationError):
            FindingFilter.model_validate({"run_id": "r", **bad})


# --- UT06-36 atomic post --------------------------------------------------------------------


def test_ut06_36_post_writes_row_and_checkpoint(bb_env: BbEnv) -> None:
    """UT06-36 post commits the proposed row and the `state` checkpoint key together."""
    fid = _post(bb_env)
    found = get_findings([fid])[fid]
    assert found.status == "proposed"
    assert (found.task_id, found.author_role, found.run_id) == (
        bb_env.task_id,
        "analyst",
        bb_env.run_id,
    )
    assert found.query_ids == [bb_env.ops_qid]
    state = checkpoint_of(bb_env.task_id)
    assert state is not None
    assert state["state"] == {
        "phase": "running",
        "pending_findings": [fid],
        "proposals": None,
        "pseudonyms": None,
    }


def test_ut06_36_second_post_appends_and_keeps_other_keys(bb_env: BbEnv) -> None:
    """UT06-36 only the `state` key is replaced (R-21); pending_findings accumulates."""
    save_checkpoint(bb_env.task_id, "loop", {"step": 3})
    first = _post(bb_env)
    second = _post(bb_env, claim="Team t1 still had [[n1]] incidents")
    cp = checkpoint_of(bb_env.task_id)
    assert cp is not None
    assert cp["loop"] == {"step": 3}
    assert cp["state"]["pending_findings"] == [first, second]  # type: ignore[index]


def _block_checkpoint_updates() -> None:
    """A trigger failing the checkpoint UPDATE that save_checkpoint runs after `writes`."""
    sql = (
        "CREATE TRIGGER t06_08_block BEFORE UPDATE OF checkpoint ON task"
        " BEGIN SELECT RAISE(ABORT, 'checkpoint blocked'); END"
    )
    run_write(lambda conn: conn.execute(sql), op="test_trigger")


def test_ut06_36_failing_checkpoint_write_leaves_neither(bb_env: BbEnv) -> None:
    """UT06-36 rollback: the checkpoint write fails after the finding insert ran → neither."""
    save_checkpoint(bb_env.task_id, "loop", {"step": 1})
    before = checkpoint_of(bb_env.task_id)
    _block_checkpoint_updates()
    with pytest.raises(HernessError):
        _post(bb_env)
    assert _count() == 0
    assert checkpoint_of(bb_env.task_id) == before


def test_ut06_36_rollback_probe_detects_split_transactions(
    bb_env: BbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT06-36 control: with the insert in its own transaction the same failure leaks a row."""

    def split(
        task_id: str, key: CheckpointKey, value: Mapping[str, object], *, writes: Any
    ) -> None:
        run_write(writes, op="test_split")
        save_checkpoint(task_id, key, value)

    monkeypatch.setattr(bbmod, "save_checkpoint", split)
    _block_checkpoint_updates()
    with pytest.raises(HernessError):
        _post(bb_env)
    assert _count() == 1  # what the real (atomic) test above rules out


def test_ut06_36_fault_point_after_commit(bb_env: BbEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT06-36 `swarm.after_finding_write` fires with role=analyst after the commit."""
    seen: list[tuple[str, dict[str, str], int]] = []

    def record(name: str, **labels: str) -> None:
        seen.append((name, labels, _count()))

    monkeypatch.setattr(bbmod, "fault_point", record)
    _post(bb_env)
    assert seen == [("swarm.after_finding_write", {"role": "analyst"}, 1)]


def test_ut06_36_writer_thread_and_metric(bb_env: BbEnv) -> None:
    """UT06-36 writes run on the `herness-bb-writer` thread; latency goes to the sink."""
    observed: list[tuple[str, float]] = []

    class Sink:
        def record_histogram(self, name: str, value: float, *, component: str) -> None:
            del component
            observed.append((name, value))

    bb = Blackboard(
        bb_env.run_id,
        build_id=bb_env.warehouse.build_id,
        catalog=bb_env.bb._catalog,
        allowed_numerals=(),
        metrics=Sink(),
    )
    try:
        name = bb.run_on_writer_sync(lambda: threading.current_thread().name)
        assert name.startswith("herness-bb-writer")
        assert asyncio.run(bb.run_on_writer(lambda: 5)) == 5
    finally:
        bb.close()
    assert [n for n, _ in observed] == ["herness_harness_blackboard_write_seconds"] * 2


def test_ut06_36_writer_timeout_is_store_busy(bb_env: BbEnv) -> None:
    """UT06-36 a writer busy beyond the timeout → StoreBusy; errors of `fn` pass unchanged."""
    gate = threading.Event()
    bb_env.bb.run_on_writer_sync(lambda: None)
    blocker = bb_env.bb._writer.submit(gate.wait)
    with pytest.raises(StoreBusy, match="blackboard writer timeout"):
        bb_env.bb.run_on_writer_sync(lambda: None, timeout_s=0.05)
    gate.set()
    blocker.result()

    def raise_timeout() -> None:
        raise TimeoutError

    with pytest.raises(TimeoutError):
        bb_env.bb.run_on_writer_sync(raise_timeout)


def test_ut06_36_shared_writer_is_not_shut_down(bb_env: BbEnv) -> None:
    """UT06-36 a Blackboard given a writer leaves it running on close()."""
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="herness-bb-writer")
    bb = Blackboard(
        bb_env.run_id,
        build_id="b",
        catalog=bb_env.bb._catalog,
        allowed_numerals=(),
        writer=pool,
    )
    bb.close()
    assert pool.submit(lambda: 1).result() == 1
    pool.shutdown()


# --- UT06-37 / ST06-01 / ST06-05 rejections --------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"claim": "Team t1 had [[n2]] incidents"}, "marker [[n2]] has no number"),
        ({"claim": "Team t1 had [[nx]] incidents"}, "malformed marker"),
        ({"claim": "Team t1 had many incidents"}, "number n1 is not referenced"),
        ({"claim": "Team t1 had [[n1]] incidents, 42 more"}, "numerals outside markers: 42"),
        ({"entity_id": "t9"}, "unknown team t9"),
        ({"confidence": 3}, "confidence"),
        ({"numbers": []}, "numbers"),
    ],
)
def test_ut06_37_invalid_posts_rejected(
    bb_env: BbEnv, overrides: dict[str, object], fragment: str
) -> None:
    """UT06-37 invalid markers, stray numeral, unknown entity, bad fields → ToolInputError."""
    with pytest.raises(ToolInputError) as info:
        _post(bb_env, **overrides)
    assert fragment in info.value.message
    assert _count() == 0


def test_ut06_37_marker_error_hint_and_log(bb_env: BbEnv) -> None:
    """UT06-37 marker errors carry the hint; each rejection logs `post_rejected` with the rule."""
    with structlog.testing.capture_logs() as logs, pytest.raises(ToolInputError) as info:
        _post(bb_env, claim="no markers here")
    assert info.value.hint == "use [[nX]] markers for every number and cite each NumberRef"
    events = [e for e in logs if e["event"] == "harness.finding.post_rejected"]
    assert events == [
        {
            "event": "harness.finding.post_rejected",
            "log_level": "info",
            "component": "harness.blackboard",
            "run_id": bb_env.run_id,
            "task_id": bb_env.task_id,
            "rule": "markers",
        }
    ]


def test_ut06_37_role_and_run_checked(bb_env: BbEnv) -> None:
    """UT06-37 a non-analyst role or another run's context is refused."""
    for ctx in (bb_env.ctx(role="skeptic"), bb_env.ctx().model_copy(update={"run_id": "run_x"})):
        with pytest.raises(ToolInputError, match="only available to analyst tasks"):
            bb_env.bb.post(ctx, **args(bb_env.ops_qid))
    assert _count() == 0


def test_ut06_37_unexpected_argument_and_unknown_task(bb_env: BbEnv) -> None:
    """UT06-37 extra arguments and a task outside this run are refused."""
    with pytest.raises(ToolInputError, match="unexpected post_finding arguments"):
        _post(bb_env, finding_id="fnd_x")
    ctx = bb_env.ctx(task_id=task_spec(bb_env.run_id, n=9).task_id)
    with pytest.raises(ToolInputError, match="task not found"):
        bb_env.bb.post(ctx, **args(bb_env.ops_qid))
    assert _count() == 0


def test_st06_01_uncited_numbers_rejected_no_row(bb_env: BbEnv) -> None:
    """ST06-01 digits in the claim, a fabricated query_id, a marker to nothing: no row."""
    cases: list[tuple[dict[str, object], str]] = [
        ({"claim": "Team t1 had 7 incidents and [[n1]]"}, "numerals outside markers: 7"),
        ({"claim": "Team t1 had [[n1]] and [[n3]] incidents"}, "marker [[n3]] has no number"),
        ({"query_ids": ["q_" + "9" * 16]}, "unknown query_id q_9999999999999999"),
    ]
    for overrides, message in cases:
        with pytest.raises(ToolInputError) as info:
            _post(bb_env, **overrides)
        assert info.value.message == message
    with pytest.raises(ToolInputError) as fab:
        bb_env.bb.post(bb_env.ctx(), **args("q_" + "e" * 16))
    assert fab.value.message == "unknown query_id q_eeeeeeeeeeeeeeee"
    assert fab.value.hint == "cite query_ids returned by tools in this task"
    assert _count() == 0
    assert checkpoint_of(bb_env.task_id) is None


def test_st06_01_evidence_of_another_build_is_unknown(bb_env: BbEnv) -> None:
    """ST06-01 an ops evidence row of another build does not count; meta.evidence ids do."""
    other = Blackboard(
        bb_env.run_id,
        build_id="20260101-000000-OTHERB",
        catalog=bb_env.bb._catalog,
        allowed_numerals=(),
    )
    try:
        with pytest.raises(ToolInputError, match="unknown query_id"):
            other.post(bb_env.ctx(), **args(bb_env.ops_qid))
    finally:
        other.close()
    fid = bb_env.bb.post(bb_env.ctx(), **args(META_QID))
    assert get_findings([fid])[fid].query_ids == [META_QID]


def test_st06_05_pii_claim_names_types_only(bb_env: BbEnv) -> None:
    """ST06-05 an email and a planted name are rejected naming only the entity types."""
    claim = f"Team t1 had [[n1]] incidents, ask {PLANTED_NAME} at priya@example.com"
    with structlog.testing.capture_logs() as logs, pytest.raises(ToolInputError) as info:
        _post(bb_env, claim=claim)
    err = info.value
    assert err.message == "claim contains personal data (EMAIL, PERSON)"
    text = " ".join([str(err), err.hint or "", repr(dict(err.context)), repr(logs)])
    for secret in ("Priya", "Raman", "priya@example.com", "incidents"):
        assert secret not in text
    assert _count() == 0


def test_st06_05_pii_checked_before_numerals_and_ids(bb_env: BbEnv) -> None:
    """ST06-05 an unmarked phone number and email are rejected as PII; no digit is echoed."""
    claim = "Team t1 had [[n1]] incidents, call +1 415-867-5309 or ops42@example.com about 17"
    with structlog.testing.capture_logs() as logs, pytest.raises(ToolInputError) as info:
        _post(bb_env, claim=claim, entity_id="t9")
    err = info.value
    assert err.message == "claim contains personal data (EMAIL, PHONE)"
    parts = [str(err), err.hint or "", repr(dict(err.context)), repr(dict(err.details)), repr(logs)]
    for fragment in ("415", "867", "5309", "ops42", "17", "t9"):
        assert fragment not in " ".join(parts)
    assert _count() == 0


def test_st06_05_pii_checked_before_marker_validation(bb_env: BbEnv) -> None:
    """ST06-05 a bracketed phone number and email are rejected as PII, not echoed as markers."""
    claim = "Team t1 had [[n1]] incidents, call [[+1 415-867-5309]] or mail [[ops42@example.com]]"
    with structlog.testing.capture_logs() as logs, pytest.raises(ToolInputError) as info:
        _post(bb_env, claim=claim)
    err = info.value
    assert err.message == "claim contains personal data (EMAIL, PHONE)"
    parts = [str(err), err.hint or "", repr(dict(err.context)), repr(dict(err.details)), repr(logs)]
    for fragment in ("415", "867", "5309", "ops42", "example.com", "malformed"):
        assert fragment not in " ".join(parts)
    assert _count() == 0


def test_st06_05_error_messages_never_carry_claim_text(bb_env: BbEnv) -> None:
    """ST06-05 rejection messages for bad fields name paths, never the claim."""
    claim = "secret words " * 200  # over 1,500 chars
    with pytest.raises(ToolInputError) as info:
        _post(bb_env, claim=claim)
    assert info.value.message == "invalid post_finding arguments: claim"
    assert "secret" not in str(info.value)


# --- UT06-38 duplicate and revision posts ---------------------------------------------------


def test_ut06_38_same_post_twice_returns_same_id(bb_env: BbEnv) -> None:
    """UT06-38 an identical second post returns the first id and writes nothing."""
    first = _post(bb_env)
    assert _post(bb_env, confidence=0.1) == first
    assert _count() == 1
    cp = checkpoint_of(bb_env.task_id)
    assert cp is not None
    assert cp["state"]["pending_findings"] == [first]  # type: ignore[index]


def test_ut06_38_revision_task_posts_once(bb_env: BbEnv) -> None:
    """UT06-38 a revision post supersedes its challenged finding; a second post is refused."""
    old = _post(bb_env)
    spec = task_spec(bb_env.run_id, n=50)
    assert asyncio.run(bb_env.bb.challenge(old, challenge(old, "revise"), revision=spec))
    rev_task = add_running_task(bb_env.run_id, n=1, revision_of=old)
    ctx = bb_env.ctx(task_id=rev_task)
    new = bb_env.bb.post(ctx, **args(bb_env.ops_qid, claim="Team t1 had [[n1]] incidents only"))
    found = get_findings([old, new])
    assert found[old].status == "revised"
    assert found[new].supersedes == old
    assert [c.verdict for c in found[new].challenge] == ["revise"]
    with pytest.raises(ToolInputError, match="revision tasks post exactly one finding"):
        bb_env.bb.post(ctx, **args(bb_env.ops_qid, claim="Another [[n1]] claim"))
    assert len(list_task_findings(rev_task)) == 1


def test_ut06_38_revision_of_open_finding_rolls_back(bb_env: BbEnv) -> None:
    """UT06-38 a revision post whose target is not challenged is refused with no row."""
    old = _post(bb_env)
    rev_task = add_running_task(bb_env.run_id, n=2, revision_of=old)
    with pytest.raises(ToolInputError, match="is not open for revision"):
        bb_env.bb.post(bb_env.ctx(task_id=rev_task), **args(bb_env.ops_qid, claim="New [[n1]]"))
    assert list_task_findings(rev_task) == []
    assert checkpoint_of(rev_task) is None


# --- UT06-39 challenge, verify, reject ------------------------------------------------------


def test_ut06_39_uphold_revise_reject_verify(bb_env: BbEnv) -> None:
    """UT06-39 uphold keeps proposed, revise → challenged + task, reject appends, verify."""
    bb = bb_env.bb
    a, b, c = (_post(bb_env, claim=f"Team t1 case {x} had [[n1]]") for x in "abc")
    with structlog.testing.capture_logs() as logs:
        assert asyncio.run(bb.challenge(a, challenge(a)))
    assert _status(a) == "proposed"
    assert [e["to"] for e in logs if e["event"] == "harness.finding.transitioned"] == ["proposed"]
    spec = task_spec(bb_env.run_id, n=60)
    assert asyncio.run(bb.challenge(b, challenge(b, "revise"), revision=spec))
    assert _status(b) == "challenged"
    assert get_task(spec.task_id) is not None
    assert asyncio.run(bb.reject(b, "skeptic_reject", append=challenge(b, "reject")))
    rejected = get_findings([b])[b]
    assert rejected.status == "rejected"
    assert [ch.verdict for ch in rejected.challenge] == ["revise", "reject"]
    assert rejected.verification is None
    assert asyncio.run(bb.mark_verified(a, verification()))
    assert _status(a) == "verified"
    assert asyncio.run(bb.reject(c, "verifier_fail", verification()))
    record = get_findings([c])[c].verification
    assert record is not None
    assert record.reason == "verifier_fail"
    assert not asyncio.run(bb.mark_verified(c, verification()))


def test_ut06_39_revise_on_closed_finding_inserts_no_task(bb_env: BbEnv) -> None:
    """UT06-39 a revise that loses the compare-and-set inserts no revision task."""
    a = _post(bb_env)
    assert asyncio.run(bb_env.bb.mark_verified(a, verification()))
    spec = task_spec(bb_env.run_id, n=61)
    assert not asyncio.run(bb_env.bb.challenge(a, challenge(a, "revise"), revision=spec))
    assert get_task(spec.task_id) is None


def test_ut06_39_challenge_preconditions(bb_env: BbEnv) -> None:
    """UT06-39 reject verdicts, revise without a task and uphold with one → ConfigError."""
    a = _post(bb_env)
    spec = task_spec(bb_env.run_id, n=62)
    for ch, revision in (
        (challenge(a, "reject"), None),
        (challenge(a, "revise"), None),
        (challenge(a), spec),
    ):
        with pytest.raises(ConfigError):
            asyncio.run(bb_env.bb.challenge(a, ch, revision=revision))
    assert _status(a) == "proposed"


# --- UT06-40 supersede ----------------------------------------------------------------------


def test_ut06_40_supersede_copies_history(bb_env: BbEnv) -> None:
    """UT06-40 old → revised; new proposed with supersedes and the challenge history."""
    bb = bb_env.bb
    old = _post(bb_env)
    assert asyncio.run(bb.challenge(old, challenge(old), revision=None))
    spec = task_spec(bb_env.run_id, n=70)
    assert asyncio.run(bb.challenge(old, challenge(old, "revise"), revision=spec))
    template = get_findings([old])[old]
    new = template.model_copy(
        update={"finding_id": "fnd_01J8ZZZZZZZZZZZZZZZZZZZZZZ", "challenge": []}
    )
    assert asyncio.run(bb.supersede(old, new)) == new.finding_id
    found = get_findings([old, new.finding_id])
    assert found[old].status == "revised"
    fresh = found[new.finding_id]
    assert (fresh.status, fresh.supersedes) == ("proposed", old)
    assert [c.verdict for c in fresh.challenge] == ["uphold", "revise"]


def test_ut06_40_supersede_refused(bb_env: BbEnv) -> None:
    """UT06-40 a finding that is not challenged, or does not exist → ToolInputError."""
    old = _post(bb_env)
    new = get_findings([old])[old].model_copy(
        update={"finding_id": "fnd_01J8YYYYYYYYYYYYYYYYYYYYYY"}
    )
    with pytest.raises(ToolInputError, match=f"finding {old} is not open for revision"):
        asyncio.run(bb_env.bb.supersede(old, new))
    missing = "fnd_01J8XXXXXXXXXXXXXXXXXXXXXX"
    with pytest.raises(ToolInputError, match=f"finding {missing} not found"):
        asyncio.run(bb_env.bb.supersede(missing, new))
    assert _count() == 1


# --- UT06-41 merge --------------------------------------------------------------------------


def test_ut06_41_merge_two_of_three(bb_env: BbEnv) -> None:
    """UT06-41 two dups merged into keep; a dup no longer proposed is skipped."""
    keep, d1, d2, d3 = (_post(bb_env, claim=f"Case {x} had [[n1]]") for x in "kabc")
    assert asyncio.run(bb_env.bb.mark_verified(d3, verification()))
    with structlog.testing.capture_logs() as logs:
        assert asyncio.run(bb_env.bb.merge(keep, [d2, d1, d3])) is None
    found = get_findings([keep, d1, d2, d3])
    assert [found[d].status for d in (d1, d2)] == ["merged", "merged"]
    assert {found[d].merged_into for d in (d1, d2)} == {keep}
    assert (found[keep].status, found[d3].status) == ("proposed", "verified")
    assert any(e["event"] == "harness.finding.merge_skipped" for e in logs)


def test_ut06_41_keep_in_dups_is_config_error(bb_env: BbEnv) -> None:
    """UT06-41 keep_id among dup_ids → ConfigError; nothing merged."""
    keep = _post(bb_env)
    with pytest.raises(ConfigError):
        asyncio.run(bb_env.bb.merge(keep, [keep]))
    assert _status(keep) == "proposed"


# --- UT06-42 list_findings ------------------------------------------------------------------


def test_ut06_42_list_filters_and_limit(bb_env: BbEnv) -> None:
    """UT06-42 default filter hides merged; status, entity, task, role, confidence, limit."""
    bb = bb_env.bb
    a = _post(bb_env, claim="A had [[n1]]", confidence=0.9)
    b = _post(bb_env, claim="B had [[n1]]", entity_id="t2", confidence=0.2)
    c = _post(bb_env, claim="C had [[n1]]")
    asyncio.run(bb.merge(a, [c]))

    def ids(**kw: object) -> list[str]:
        flt = FindingFilter.model_validate({"run_id": bb_env.run_id, **kw})
        return [f.finding_id for f in asyncio.run(bb.list_findings(flt))]

    assert ids() == [a, b]
    assert ids(include_superseded=True) == [a, b, c]
    assert ids(status={"merged"}) == [c]
    assert ids(entity_type="team", entity_ids={"t2"}) == [b]
    assert ids(task_ids={bb_env.task_id}, author_roles={"analyst"}, min_confidence=0.5) == [a]
    assert ids(limit=1) == [a]
    with pytest.raises(ToolInputError, match="run_id mismatch"):
        asyncio.run(bb.list_findings(FindingFilter(run_id="run_other")))
    assert len(read_all("SELECT finding_id FROM finding")) == 3

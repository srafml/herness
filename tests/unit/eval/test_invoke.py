"""Tests for herness.eval.invoke (U11-62): UT11-114 and controller checks (T11-28).

The review tests use a fake `Swarm` (any object with an async `start(req, job)`) whose run
writes a draft file; findings come from the migrated tmp ops store (`ops_store`), read
through the public `query_findings` API (R-08). Chat tests use `FakeChatService` (T09-19).
"""

from __future__ import annotations

import contextlib
import shutil
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from tests.support.fake_chat import FakeChatService, all_kinds, error_script
from tests.support.report_drafts import draft_dict, paragraph

from herness.core.errors import ConfigError, ModelUnavailable
from herness.core.types import Finding, ReportDraft
from herness.eval.grading import NumberCarrier
from herness.eval.invoke import PipelineOutput, ReviewCache, invoke_chat, invoke_review
from herness.harness.swarm import RunRequest, RunResult
from herness.store.ops import core, findings

pytestmark = pytest.mark.unit

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "drafts" / "findings_only.json"
_RUN = "run_01J9ZQ4Y8M6V3K2N1P0R5T7W9X"  # pragma: allowlist secret - test ULID
_TASK = "task_01J9ZQ4Y8M6V3K2N1P0R5T7W9D"  # pragma: allowlist secret - test ULID
_F1 = "fnd_01J9ZQ4Y8M6V3K2N1P0R5T7W9A"  # pragma: allowlist secret - test ULID
_F2 = "fnd_01J9ZQ4Y8M6V3K2N1P0R5T7W9B"  # pragma: allowlist secret - test ULID
_F3 = "fnd_01J9ZQ4Y8M6V3K2N1P0R5T7W9E"  # pragma: allowlist secret - test ULID
_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_COVERAGE = {
    "planned_tasks": 1, "done_tasks": 1, "dead_tasks": 0, "must_cover_total": 0,
    "must_cover_done": 0, "verified_findings": 0, "rejected_findings": 0, "publishable": True,
}  # fmt: skip


class _FakeSwarm:
    """Fake `Swarm`: `start` copies `draft` (when set) to the run's draft path, or raises."""

    def __init__(self, tmp: Path, draft: Path | None, *, raises: Exception | None = None,
                 status: str = "partial") -> None:  # fmt: skip
        self.tmp, self.draft, self.raises, self.status = tmp, draft, raises, status
        self.calls: list[tuple[RunRequest, object]] = []

    async def start(self, req: RunRequest, job: object = None) -> RunResult:
        self.calls.append((req, job))
        if self.raises is not None:
            raise self.raises
        path = None
        if self.draft is not None:
            path = self.tmp / "draft.json"
            shutil.copyfile(self.draft, path)
        return RunResult(
            run_id=_RUN, status=self.status, draft_path=None if path is None else str(path),
            coverage=ReportDraft.model_validate_json(_FIXTURE.read_text("utf-8")).coverage,
            dead_task_ids=[], token_usage={}, cost_usd=Decimal("0"),
        )  # fmt: skip


def _clock(*values: float) -> Callable[[], float]:
    it = iter(values)
    return lambda: next(it)


def _finding(  # noqa: PLR0913 - one stored finding, every varying field explicit
    fid: str, entity: str, claim: str, value: int, query: str, *, n: int,
    status: str = "verified",
) -> Finding:  # fmt: skip
    ref = {"id": "n1", "value": value, "unit": "count", "query_id": query,
           "column": "incident_count", "row_key": None}  # fmt: skip
    return Finding.model_validate({
        "finding_id": fid, "run_id": _RUN, "task_id": _TASK, "author_role": "analyst",
        "claim": claim, "entity_type": "candidate", "entity_id": entity, "numbers": [ref],
        "query_ids": [query], "confidence": 0.7, "status": status,
        "created_at": _T0 + timedelta(seconds=n),
    })  # fmt: skip


def _insert(*rows: Finding) -> None:
    core.run_write(lambda conn: [findings.insert_finding(conn, f) for f in rows], op="test_it")


def _store_findings() -> None:
    _insert(
        _finding(_F1, "jira:issue:PAY-12", "Epic PAY-12 targets [[n1]] incidents on its service",
                 14, "q_0123456789abcdef", n=1),
        _finding(_F2, "jira:issue:OPS-7", "Epic OPS-7 targets [[n1]] incidents on its service",
                 9, "q_fedcba9876543210", n=2),
        _finding(_F3, "jira:issue:OPS-7", "Epic OPS-7 is cheap at [[n1]]", 2,
                 "q_fedcba9876543210", n=3, status="rejected"),
    )  # fmt: skip


def _req() -> RunRequest:
    return RunRequest(kind="funding_review", depth="standard", profile="synth")


def test_ut11_114_findings_only_draft(ops_store: object, tmp_path: Path) -> None:
    """UT11-114 a Writer-dead run's `findings_only` draft (R-49): title and both verified
    claims in the final text, two carriers with the findings' NumberRefs, ranked ids from
    the findings' entity ids when `ranked_entities` is empty."""
    draft = ReportDraft.model_validate_json(_FIXTURE.read_text("utf-8"))  # real model
    assert draft.mode == "findings_only"
    assert draft.ranked_entities == []
    _store_findings()
    swarm = _FakeSwarm(tmp_path, _FIXTURE)

    out = invoke_review(cast("Any", swarm), _req(), job=None, now=_clock(10.0, 12.5))

    assert out.error is None
    assert out.draft_mode == "findings_only"
    assert out.run_id == _RUN
    lines = out.final_text.split("\n")
    assert lines[0] == "Funding review"
    assert lines[1:3] == [
        "Epic PAY-12 targets [[n1]] incidents on its service",
        "Epic OPS-7 targets [[n1]] incidents on its service",
    ]
    assert lines[3] == draft.caveats[0]
    p1, p2 = draft.sections[0].paragraphs
    assert out.carriers == [
        NumberCarrier(text=p1.text, numbers=list(p1.numbers)),
        NumberCarrier(text=p2.text, numbers=list(p2.numbers)),
    ]
    assert [n.value for c in out.carriers for n in c.numbers] == [14, 9]
    assert out.numbers == [*p1.numbers, *p2.numbers]
    assert out.ranked_ids == ["jira:issue:PAY-12", "jira:issue:OPS-7"]
    assert out.verified_claims == [p1.text, p2.text]
    assert [f.finding_id for f in out.findings] == [_F1, _F2, _F3]  # every status
    assert out.latency_s == pytest.approx(2.5)
    assert swarm.calls == [(_req(), None)]


def _write_draft(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "src.json"
    path.write_text(ReportDraft.model_validate(data).model_dump_json(), encoding="utf-8")
    return path


def test_cv_t11_28_full_draft(ops_store: object, tmp_path: Path) -> None:
    """Full mode: title, paragraphs, recommendation, caveats, prior-outcomes commentary;
    carriers per paragraph and recommendation; ranked ids from `ranked_entities`."""
    poc = paragraph("Last quarter's fix held at [[n1]]")
    data = draft_dict(run_id=_RUN, prior_outcomes_commentary=poc)
    swarm = _FakeSwarm(tmp_path, _write_draft(tmp_path, data), status="done")

    out = invoke_review(cast("Any", swarm), _req(), job=None, now=_clock(0.0, 1.0))

    assert out.error is None
    assert out.draft_mode == "full"
    assert out.final_text.split("\n") == [
        "Funding review", "Incidents rose to [[n1]]", "Fund the cache rewrite",
        "It saves [[n2]] a year", "Data covers ninety days", "Last quarter's fix held at [[n1]]",
    ]  # fmt: skip
    assert [c.text for c in out.carriers] == [
        "Incidents rose to [[n1]]", "Fund the cache rewrite\nIt saves [[n2]] a year",
        "Last quarter's fix held at [[n1]]",
    ]  # fmt: skip
    assert len(out.numbers) == 4
    assert out.ranked_ids == ["epic-1"]
    assert out.findings == []
    assert out.verified_claims == []

    swarm = _FakeSwarm(tmp_path, _write_draft(tmp_path, draft_dict(run_id=_RUN)))
    out = invoke_review(cast("Any", swarm), _req(), job=None, now=_clock(0.0, 1.0))
    assert out.final_text.split("\n")[-1] == "Data covers ninety days"
    assert len(out.carriers) == 2


def test_cv_t11_28_findings_only_ranked_entities_and_unknown_finding(
    ops_store: object, tmp_path: Path
) -> None:
    """findings_only with `ranked_entities` set uses them; a draft finding missing from the
    store adds no entity id; a finding cited twice yields its entity id once."""
    data = ReportDraft.model_validate_json(_FIXTURE.read_text("utf-8")).model_dump(mode="json")
    _insert(_finding(_F1, "jira:issue:PAY-12", "c", 14, "q_0123456789abcdef", n=1))
    swarm = _FakeSwarm(tmp_path, _write_draft(tmp_path, data))
    out = invoke_review(cast("Any", swarm), _req(), job=None, now=_clock(0.0, 1.0))
    assert out.ranked_ids == ["jira:issue:PAY-12"]  # F2 is not in the store

    data["sections"][0]["paragraphs"][1]["finding_ids"] = [_F1]
    data["ranked_entities"] = [{"rank": 1, "entity_type": "candidate", "entity_id": "x"}]
    swarm = _FakeSwarm(tmp_path, _write_draft(tmp_path, data))
    out = invoke_review(cast("Any", swarm), _req(), job=None, now=_clock(0.0, 1.0))
    assert out.ranked_ids == ["x"]


def test_cv_t11_28_review_errors_never_raise(ops_store: object, tmp_path: Path) -> None:
    """A pipeline exception or a run without a draft becomes `error`, scrubbed and cut."""
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # pragma: allowlist secret
    swarm = _FakeSwarm(tmp_path, None, raises=ConfigError(f"bad {token} " + "x" * 900))
    out = invoke_review(cast("Any", swarm), _req(), job=None, now=_clock(0.0, 3.0))
    assert out.error is not None
    assert out.error.startswith("ConfigError: bad ")
    assert token not in out.error
    assert len(out.error) <= len("ConfigError: ") + 500
    assert out.latency_s == pytest.approx(3.0)
    assert out.draft_mode is None

    out = invoke_review(cast("Any", _FakeSwarm(tmp_path, None, status="failed")), _req(),
                        job=None, now=_clock(0.0, 1.0))  # fmt: skip
    assert out.run_id == _RUN
    assert out.error == f"NoDraft: run {_RUN} ended failed without a draft"


def test_cv_t11_28_invoke_chat(ops_store: object) -> None:
    """Chat: eval session, `eval` user ref and the given mode; final answer text, one
    carrier, entity ids from the name index; error events and exceptions become `error`."""
    chat = FakeChatService(scripts=[all_kinds("live"), error_script()])
    out = invoke_chat(cast("Any", chat), "How many incidents?", mode="live",
                      now=_clock(1.0, 2.0), name_index={"Team One": "team:t1"})  # fmt: skip
    session_id, text, user_ref, mode = chat.calls[0]
    assert session_id.startswith("eval_")
    assert (text, user_ref, mode) == ("How many incidents?", "eval", "live")
    assert out.error is None
    assert out.final_text == "Team One had 42 incidents."
    assert out.run_id.startswith("run_")
    assert out.carriers == [NumberCarrier(text=out.final_text, numbers=out.numbers)]
    assert [n.value for n in out.numbers] == [42]
    assert out.ranked_ids == ["team:t1"]
    assert out.draft_mode is None
    assert out.latency_s == pytest.approx(1.0)

    out = invoke_chat(cast("Any", chat), "q", mode="cloud", now=_clock(0.0, 1.0))
    assert out.error == "ModelUnavailable: The model is not reachable."
    assert out.final_text == ""
    assert out.carriers == []

    boom = FakeChatService(scripts=[all_kinds()], raises=ModelUnavailable("down"))
    out = invoke_chat(cast("Any", boom), "q", mode="live", now=_clock(0.0, 1.0))
    assert out.error == "ModelUnavailable: down"
    assert out.final_text == "Team One had 42 incidents."
    assert out.ranked_ids == []

    out = invoke_chat(cast("Any", FakeChatService()), "q", mode="live", now=_clock(0.0, 1.0))
    assert out.error == "NoFinalEvent: the chat turn ended without a final answer"


def _output(tag: str) -> PipelineOutput:
    return PipelineOutput(run_id=tag, final_text=tag, latency_s=0.0)


def test_cv_t11_28_review_cache_keys() -> None:
    """Same key → factory once; another pipeline, profile, depth or framing question → a
    separate run; a framing question is its own key; a factory exception is an error."""
    cache = ReviewCache()
    calls: list[str] = []

    def factory(tag: str) -> Callable[[], PipelineOutput]:
        def run() -> PipelineOutput:
            calls.append(tag)
            return _output(tag)

        return run

    first = cache.get_or_run("funding_review", "synth", "standard", None, factory("a"))
    again = cache.get_or_run("funding_review", "synth", "standard", None, factory("b"))
    assert first is again
    assert calls == ["a"]
    for args in [("org_review", "synth", "standard", None), ("funding_review", "local",
                 "standard", None), ("funding_review", "synth", "deep", None),
                 ("funding_review", "synth", "standard", "O01"),
                 ("funding_review", "synth", "standard", "O02")]:  # fmt: skip
        cache.get_or_run(*args, factory(str(args)))
    assert len(calls) == 6
    cache.get_or_run("funding_review", "synth", "standard", "O01", factory("c"))
    assert len(calls) == 6

    def bad() -> PipelineOutput:
        msg = "gone"
        raise ModelUnavailable(msg)

    out = cache.get_or_run("org_review", "local", "fast", None, bad)
    assert out.error == "ModelUnavailable: gone"
    assert cache.get_or_run("org_review", "local", "fast", None, bad) is out


def test_cv_t11_28_review_cache_concurrent_callers_share_one_run() -> None:
    """Eight threads asking for the same key: the factory runs once, all get its output."""
    cache = ReviewCache()
    started, release = threading.Event(), threading.Event()
    calls: list[int] = []

    def factory() -> PipelineOutput:
        calls.append(1)
        started.set()
        release.wait(5)
        return _output("shared")

    results: list[PipelineOutput] = []
    lock = threading.Lock()

    def worker() -> None:
        out = cache.get_or_run("funding_review", "synth", "standard", None, factory)
        with lock:
            results.append(out)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    assert started.wait(5)
    release.set()
    for t in threads:
        t.join(5)
    assert calls == [1]
    assert len(results) == 8
    assert all(r is results[0] for r in results)


def test_cv_t11_28_review_cache_owner_interrupted() -> None:
    """When the owning call dies with a non-Exception, waiters get an error output."""
    cache = ReviewCache()
    entered, proceed = threading.Event(), threading.Event()

    def factory() -> PipelineOutput:
        entered.set()
        proceed.wait(5)
        raise KeyboardInterrupt

    def owner() -> None:
        with contextlib.suppress(KeyboardInterrupt):
            cache.get_or_run("p", "x", "fast", None, factory)

    t = threading.Thread(target=owner)
    t.start()
    assert entered.wait(5)
    seen: list[PipelineOutput] = []
    waiter = threading.Thread(
        target=lambda: seen.append(cache.get_or_run("p", "x", "fast", None, factory))
    )
    waiter.start()
    proceed.set()
    t.join(5)
    waiter.join(5)
    assert seen[0].error == "ReviewCacheError: the shared run did not complete"


def test_cv_t11_28_pipeline_output_defaults() -> None:
    """An error-only output needs no pipeline fields."""
    out = PipelineOutput(error="X: y")
    assert (out.run_id, out.final_text, out.carriers, out.numbers) == ("", "", [], [])
    assert out.findings == []
    assert out.draft_mode is None

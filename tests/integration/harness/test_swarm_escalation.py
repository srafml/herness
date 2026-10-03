"""IT06-33: chat escalation to a mini swarm and its summary message (impl 06 U06-134, U06-135).

Real tmp migrated ops store (`ops_store`), the real job queue on the SQLite jobs backend and the
test redactor. The chat adapters are test-local and composed only of T09-03 public functions:
impl 09 has no assistant-row append and no lookup by `meta.escalation_run_id` (carry-over).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import structlog
from tests.support.ops_store import OpsStoreHandle
from tests.support.report_drafts import RUN_ID, ULID, make_draft, number_ref, paragraph
from tests.unit.harness import _blackboard_env as env_mod
from tests.unit.harness._blackboard_env import BUILD_ID, FakeWarehouse

from herness.core.config import HernessConfig, get_config
from herness.core.errors import PolicyViolation, SchemaViolation
from herness.core.jobs import queue
from herness.core.jobs.ports import bind_jobs_backend
from herness.core.types import EntityScope
from herness.harness.blackboard import Blackboard
from herness.harness.findings import EntityCatalog
from herness.harness.swarm import escalation
from herness.harness.swarm.escalation import escalate_to_review, post_escalation_summary
from herness.store.ops import (
    ChatMessageRow,
    RunRow,
    append_chat_message,
    create_chat_session,
    find_run_by_escalation,
    get_run,
    list_chat_messages,
    read_all,
    run_write,
    update_chat_message,
    update_run_fields,
    upsert_assistant_placeholder,
)
from herness.store.ops.jobs import SqliteJobsBackend

pytestmark = pytest.mark.integration

test_redactor = env_mod.test_redactor  # fixture
NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
USER_REF = "0" * 32
QUESTION = "Why did incidents rise for team Falcon?"


@pytest.fixture
def store(ops_store: OpsStoreHandle, test_redactor: object) -> HernessConfig:
    del ops_store, test_redactor
    bind_jobs_backend(SqliteJobsBackend())
    return get_config()


@pytest.fixture
def bb() -> Iterator[Blackboard]:
    board = Blackboard(
        "run_" + "0" * 26,
        build_id=BUILD_ID,
        catalog=EntityCatalog(FakeWarehouse()),
        allowed_numerals=(),
    )
    try:
        yield board
    finally:
        board.close()


@pytest.fixture
def chat(store: HernessConfig) -> tuple[str, str]:
    """A session and the user message that asks for the escalation."""
    del store
    session_id = create_chat_session(USER_REF, now=NOW)
    message_id = append_chat_message(session_id, "user", QUESTION, now=NOW)
    assert message_id is not None
    return session_id, message_id


# --- test-local chat adapters (T09-03 public functions only) ---------------------------------


def find_by_run(session_id: str, run_id: str) -> ChatMessageRow | None:
    for row in list_chat_messages(session_id, limit=500):
        if row["role"] == "assistant" and row["meta"].get("escalation_run_id") == run_id:
            return row
    return None


def append_assistant(session_id: str, **fields: Any) -> str:
    assert fields.pop("role") == "assistant"
    anchor = append_chat_message(session_id, "user", "(escalation result)", now=NOW)
    assert anchor is not None
    row = upsert_assistant_placeholder(session_id, reply_to=anchor, now=NOW)
    assert row is not None
    assert update_chat_message(row["message_id"], **fields)
    return row["message_id"]


def _run(session_id: str, message_id: str, run_id: str = RUN_ID) -> RunRow:
    meta: dict[str, object] = {
        "escalated_from": {"session_id": session_id, "message_id": message_id}
    }
    return RunRow(
        run_id=run_id, kind="funding_review", depth="fast", profile="local", build_id=BUILD_ID,
        status="done", started_at=NOW, finished_at=NOW, token_usage={}, cost_usd=Decimal(0),
        config_hash="cfg_0", meta=meta,
    )  # fmt: skip


def _post(run: RunRow, draft: Any) -> str | None:
    return post_escalation_summary(
        run, draft, append_message=append_assistant, find_message=find_by_run
    )


def _assistant_rows(session_id: str, run_id: str) -> list[ChatMessageRow]:
    return [
        r
        for r in list_chat_messages(session_id, limit=500)
        if r["role"] == "assistant" and r["meta"].get("escalation_run_id") == run_id
    ]


# --- U06-135 post_escalation_summary ---------------------------------------------------------


def test_it06_33_call_twice_one_assistant_row(chat: tuple[str, str]) -> None:
    """IT06-33 posting twice leaves exactly one assistant row; the second call returns its id."""
    session_id, message_id = chat
    text = "Spend reached [[n1]] over [[n2]] teams; see [[n9]] and [[bad]]."
    numbers = [number_ref("n1", "usd", "1200.50"), number_ref("n2", query_id="q_fedcba9876543210")]
    draft = make_draft(
        sections=[
            {"id": "executive_summary", "title": "Summary",
             "paragraphs": [paragraph(text, numbers=numbers), paragraph("Second.")]},
        ]
    )  # fmt: skip
    run = _run(session_id, message_id)
    first = _post(run, draft)
    second = _post(run, draft)
    assert first is not None
    assert second == first
    rows = _assistant_rows(session_id, RUN_ID)
    assert [r["message_id"] for r in rows] == [first]
    row = rows[0]
    assert row["content"] == "Spend reached $1.2K over 3 teams; see [[n9]] and [[bad]]."
    assert (row["status"], row["verified"], row["run_id"]) == ("done", "verified", RUN_ID)
    assert row["query_ids"] == ["q_0123456789abcdef", "q_fedcba9876543210"]
    assert row["meta"]["mode"] == "escalation"
    numbers_meta = row["meta"]["numbers"]
    assert isinstance(numbers_meta, list)
    assert [n["id"] for n in numbers_meta] == ["n1", "n2"]


@pytest.mark.parametrize("mode", ["none", "findings_only", "no_summary", "failed"])
def test_it06_33_fallback_content(chat: tuple[str, str], mode: str) -> None:
    """IT06-33 no draft, findings_only or no summary paragraph → the run-id fallback text."""
    session_id, message_id = chat
    base = make_draft(sections=[])
    failed = base.model_copy(  # passed=False with no items: bypass the consistency validator
        update={"verification": base.verification.model_copy(update={"passed": False})}
    )
    summary_only = [{"id": "executive_summary", "title": "Summary", "paragraphs": [paragraph()]}]
    draft = {
        "none": None,
        "findings_only": make_draft(
            mode="findings_only", recommendations=[], sections=summary_only
        ),
        "no_summary": make_draft(
            sections=[{"id": "executive_summary", "title": "Summary", "paragraphs": []}]
        ),
        "failed": failed,
    }[mode]
    message = _post(_run(session_id, message_id), draft)
    rows = _assistant_rows(session_id, RUN_ID)
    assert [r["message_id"] for r in rows] == [message]
    expected = f"The review finished without an executive summary; see run {RUN_ID}."
    assert rows[0]["content"] == expected
    assert rows[0]["verified"] == (
        "verified" if mode in {"findings_only", "no_summary"} else "partial"
    )
    assert rows[0]["query_ids"] == []


def test_it06_33_redaction_failure_writes_nothing(
    chat: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-33 a failed redaction fails closed: PolicyViolation without text, no row."""
    session_id, message_id = chat
    monkeypatch.setattr(escalation, "redact_text", lambda _t: None)
    with pytest.raises(PolicyViolation) as info:
        _post(_run(session_id, message_id), make_draft())
    assert "rose" not in str(info.value)
    assert _assistant_rows(session_id, RUN_ID) == []


def test_it06_33_redacts_and_bounds_content(
    chat: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-33 the content is redacted before append and capped at the chat limit."""
    session_id, message_id = chat
    seen: list[str] = []

    def fake(text: str | None) -> str:
        assert text is not None
        seen.append(text)
        return "x" * 25_000

    monkeypatch.setattr(escalation, "redact_text", fake)
    _post(_run(session_id, message_id), make_draft())
    assert seen == ["Incidents rose to 3"]
    assert len(_assistant_rows(session_id, RUN_ID)[0]["content"]) == 20_000


@pytest.mark.parametrize(
    "meta",
    [{}, {"escalated_from": None}, {"escalated_from": {"session_id": "ses_x; DROP"}}],
)
def test_it06_33_not_an_escalation(chat: tuple[str, str], meta: dict[str, object]) -> None:
    """IT06-33 a run without a valid escalation source posts nothing and returns None."""
    session_id, message_id = chat
    run = _run(session_id, message_id)
    run = RunRow(**{**{f: getattr(run, f) for f in RunRow.__slots__}, "meta": meta})

    def never(*_a: object, **_k: object) -> Any:
        raise AssertionError

    assert post_escalation_summary(run, None, append_message=never, find_message=never) is None


# --- U06-134 escalate_to_review --------------------------------------------------------------


def _escalate(
    bb: Blackboard, cfg: HernessConfig, session_id: str, message_id: str, question: str = QUESTION
) -> tuple[str, str]:
    return asyncio.run(
        escalate_to_review(
            bb, cfg, session_id=session_id, message_id=message_id, question=question,
            kind="org_review", focus=EntityScope(entity_type="team", entity_ids=["t1"]),
            jobs=queue, now=NOW,
        )
    )  # fmt: skip


@pytest.fixture
def promoted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(escalation, "read_current", lambda: BUILD_ID)


@pytest.mark.usefixtures("promoted")
def test_it06_33_escalate_creates_run_and_one_job(
    bb: Blackboard, store: HernessConfig, chat: tuple[str, str]
) -> None:
    """IT06-33 escalation creates one fast run and one review job; a retry returns the same."""
    session_id, message_id = chat
    with structlog.testing.capture_logs() as logs:
        run_id, job_id = _escalate(bb, store, session_id, message_id)
    assert _escalate(bb, store, session_id, message_id) == (run_id, job_id)
    run = get_run(run_id)
    assert run is not None
    assert (run.kind, run.depth, run.meta["job_id"]) == ("org_review", "fast", job_id)
    assert run.meta["escalated_from"] == {"session_id": session_id, "message_id": message_id}
    request = run.meta["request"]
    assert isinstance(request, dict)
    assert request["budget_override"] == store.pipelines.pipelines.chat.escalation
    assert request["question"] == QUESTION  # the planted name is not in this question
    job = queue.get(job_id)
    assert (job.kind, job.gpu_class, job.priority) == ("review", "reasoning", 75)
    assert job.payload == {"run_id": run_id, "resume": True}
    assert job.idem_key == f"escalate:{session_id}:{message_id}"
    jobs = read_all("SELECT job_id FROM job WHERE kind = 'review'", ())
    assert len(jobs) == 1
    events = [e for e in logs if e["event"] == "harness.chat.escalated"]
    assert len(events) == 1
    assert events[0]["log_level"] == "info"
    assert QUESTION not in repr(events)


@pytest.mark.usefixtures("promoted")
def test_it06_33_escalate_redacts_question(
    bb: Blackboard, store: HernessConfig, chat: tuple[str, str]
) -> None:
    """IT06-33 the question is redacted before it reaches the run request."""
    session_id, message_id = chat
    question = f"Why is {env_mod.PLANTED_NAME} on call so often? " + "q" * 3_000
    run_id, _ = _escalate(bb, store, session_id, message_id, question)
    run = get_run(run_id)
    assert run is not None
    request = run.meta["request"]
    assert isinstance(request, dict)
    stored = str(request["question"])
    assert env_mod.PLANTED_NAME not in stored
    assert len(stored) == 2_000


@pytest.mark.usefixtures("promoted")
def test_it06_33_escalate_redaction_failure(
    bb: Blackboard, store: HernessConfig, chat: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-33 redaction failure raises PolicyViolation and writes no run or job."""
    session_id, message_id = chat
    monkeypatch.setattr(escalation, "redact_text", lambda _t: None)
    with pytest.raises(PolicyViolation) as info:
        _escalate(bb, store, session_id, message_id)
    assert QUESTION not in str(info.value)
    assert find_run_by_escalation(session_id, message_id) is None
    assert read_all("SELECT job_id FROM job", ()) == []


@pytest.mark.parametrize(
    ("session_id", "message_id"),
    [
        ("ses_bad", f"msg_{ULID}"),
        (f"ses_{ULID}", f"msg_{ULID}:x"),
        (f"run_{ULID}", f"msg_{ULID}"),
    ],
)
def test_it06_33_escalate_rejects_bad_ids(
    bb: Blackboard, store: HernessConfig, session_id: str, message_id: str
) -> None:
    """IT06-33 session and message ids must match the id format before any key or write."""
    with pytest.raises(SchemaViolation):
        _escalate(bb, store, session_id, message_id)
    assert read_all("SELECT run_id FROM run", ()) == []


@pytest.mark.usefixtures("promoted")
def test_it06_33_escalate_resumes_run_without_job(
    bb: Blackboard, store: HernessConfig, chat: tuple[str, str]
) -> None:
    """IT06-33 a run left without `meta.job_id` (crash before enqueue) gets its job, no new run."""
    session_id, message_id = chat
    run_id, job_id = _escalate(bb, store, session_id, message_id)

    def clear(conn: Any) -> None:
        update_run_fields(conn, run_id, meta_patch={"job_id": None})

    run_write(clear, op="test_clear_job")
    assert _escalate(bb, store, session_id, message_id) == (run_id, job_id)
    assert len(read_all("SELECT run_id FROM run", ())) == 1
    run = get_run(run_id)
    assert run is not None
    assert run.meta["job_id"] == job_id

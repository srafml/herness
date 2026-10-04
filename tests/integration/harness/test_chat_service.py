"""IT06-11 … IT06-14, IT06-23, IT06-34, IT06-36: `ChatService` turns and the `chat` job (T06-25).

Impl 06 U06-127 … U06-129 and U06-136 (design 06 §5.13): every turn runs the real
`ChatService` on a migrated ops store with the real loop, the shipped `chat` role prompts and
the real job queue; the model is scripted per client key (`_chat_service_env`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import structlog
from tests.integration.harness import _chat_service_env as cs
from tests.integration.harness._chat_service_env import (
    CLOUD,
    LIVE,
    NOW,
    Q1,
    Q2,
    SMALL,
    USER_REF,
    Env,
    Memory,
    answer_script,
    call,
    final,
    ref,
    types_of,
    with_policy,
)
from tests.support import loop_standin as ls
from tests.support.report_drafts import make_draft
from tests.unit.harness._blackboard_env import BUILD_ID
from tests.unit.harness.memory._chat_env import ChatLLM, correction, make_deps

from herness.core.errors import ConfigError, EgressBlocked, NotFound, StoreBusy
from herness.core.jobs import queue
from herness.core.types import (
    ChatEvent,
    CorrectionCapturedEvent,
    ErrorEvent,
    EscalatedEvent,
    FinalEvent,
    ModeEvent,
    TextPart,
    TokenEvent,
    ToolEvent,
    VerificationEvent,
)
from herness.harness.memory import chat as memory_chat
from herness.harness.pipelines import chat as chat_mod
from herness.harness.pipelines.chat_support import EGRESS_NOTICE, NO_VERIFIED_ANSWER
from herness.harness.pipelines.settings import resolve_knobs
from herness.harness.swarm import escalation
from herness.harness.swarm.escalation import post_escalation_summary
from herness.store.ops import (
    ChatMessageRow,
    append_chat_message,
    create_chat_session,
    find_run_by_escalation,
    get_chat_message,
    get_run,
    list_chat_messages,
    read_all,
    select_tasks,
    update_chat_message,
    upsert_assistant_placeholder,
)

pytestmark = pytest.mark.integration

chat_env = cs.chat_env  # fixture

TEXT = "Team One had [[n1]] incidents. Team Two had [[n2]] incidents."
_BLOCK_RE = re.compile(
    r'^<untrusted_data source="chat" record_id="([^"]*)">\n?(.*?)\n?</untrusted_data>$', re.S
)


def _assistant_rows(env: Env) -> list[ChatMessageRow]:
    return [r for r in list_chat_messages(env.session_id) if r["role"] == "assistant"]


def _reply(env: Env, message_id: str) -> ChatMessageRow:
    rows = [r for r in _assistant_rows(env) if r["meta"].get("reply_to") == message_id]
    assert len(rows) == 1
    return rows[0]


def _of[T](events: list[ChatEvent], kind: type[T]) -> list[T]:
    return [e for e in events if isinstance(e, kind)]


# --- IT06-11 live turn ----------------------------------------------------------------------


def test_it06_11_live_turn_order_trim_and_wrapped_question(chat_env: Env) -> None:
    """IT06-11 scripted live chat: events in order, the unsupported number survives the repair
    turn and is trimmed (`partial`), and the question reaches the model only inside
    `<untrusted_data source="chat">` with the message id."""
    env = chat_env
    numbers = [ref("n1", 40), ref("n2", 99, Q2)]
    client = env.script(
        LIVE, answer_script(TEXT, numbers) + answer_script(TEXT, numbers, tool=False)
    )
    message_id = env.ask()
    events = env.turn("live")
    assert types_of(events) == ["mode", "tool", "evidence", "token", "verification", "final"]
    assert events[0] == ModeEvent(mode="live", message="")
    assert _of(events, ToolEvent) == [ToolEvent(name="run_sql", query_id=Q1, ok=True)]
    tokens = "".join(e.text for e in _of(events, TokenEvent))
    assert tokens == "Team One had 40 incidents. Team Two had 99 incidents."
    (verification,) = _of(events, VerificationEvent)
    assert verification.status == "partial"
    assert verification.removed_claims == ["Team Two had [[n2]] incidents."]
    (fin,) = _of(events, FinalEvent)
    assert fin.answer.text == "Team One had [[n1]] incidents."
    assert [n.id for n in fin.answer.numbers] == ["n1"]
    assert fin.answer.query_ids == [Q1]
    assert len(env.verifier.calls) == 2  # first answer, then the repaired one
    # The question reaches the model inside one chat block naming the message.
    first = client.requests[0].messages[0]
    body = json.loads("".join(p.text for p in first.parts if isinstance(p, TextPart))[8:])
    block = _BLOCK_RE.match(body["question"])
    assert block is not None
    assert block.group(1) == message_id
    assert "Team One" in block.group(2)
    repair = client.requests[3].messages[0]
    repair_body = json.loads("".join(p.text for p in repair.parts if isinstance(p, TextPart))[8:])
    assert set(repair_body) >= {"previous", "verifier_report", "question"}
    # One assistant row, done, with the rendered trimmed answer.
    row = _reply(env, message_id)
    assert row["message_id"] == _assistant_rows(env)[0]["message_id"]
    assert (row["status"], row["verified"], row["run_id"]) == ("done", "partial", fin.run_id)
    assert row["content"] == "Team One had 40 incidents."
    assert row["query_ids"] == [Q1]
    assert (row["meta"]["mode"], row["meta"]["model"]) == ("live", LIVE)
    assert isinstance(row["meta"]["latency_ms"], int)
    assert [n["id"] for n in row["meta"]["numbers"]] == ["n1"]  # type: ignore[index, union-attr]
    run = get_run(fin.run_id)
    assert run is not None
    assert (run.kind, run.depth, run.status, run.build_id) == ("chat", "fast", "done", BUILD_ID)
    assert run.meta["request"] == {"kind": "chat", "question": "<redacted>"}
    assert run.meta["session_id"] == env.session_id
    (task,) = select_tasks(fin.run_id)
    assert (task.role, task.status, task.spec.model_role) == ("chat", "done", "chat")
    assert env.memory.saved == [(env.session_id, fin.run_id)]
    ctx = env.memory.contexts[0]
    assert (ctx.run_id, ctx.run_kind, ctx.session_id, ctx.user_ref) == (
        fin.run_id, "chat", env.session_id, USER_REF,
    )  # fmt: skip
    assert ("herness_harness_chat_turns_total", {"mode": "live", "verified": "partial"}) in (
        env.metrics.counters
    )
    assert env.metrics.histograms[0][0] == "herness_harness_chat_latency_seconds"
    assert env.tracers[0].closed


def test_it06_11_repair_fixes_the_number(chat_env: Env) -> None:
    """IT06-11 the repair turn corrects the unsupported number: `verified`, nothing removed."""
    env = chat_env
    bad, good = [ref("n1", 40), ref("n2", 99, Q2)], [ref("n1", 40), ref("n2", 7, Q2)]
    env.script(LIVE, answer_script(TEXT, bad) + answer_script(TEXT, good, tool=False))
    message_id = env.ask()
    events = env.turn("live")
    (verification,) = _of(events, VerificationEvent)
    assert (verification.status, verification.removed_claims) == ("verified", [])
    assert _reply(env, message_id)["content"] == (
        "Team One had 40 incidents. Team Two had 7 incidents."
    )


def test_it06_11_verifier_unavailable_is_unverified(chat_env: Env) -> None:
    """IT06-11 a `ConfigError` from the Verifier (warehouse missing) → `unverified`; the
    session save is skipped for an unverified answer."""
    env = chat_env
    env.verifier.error = ConfigError("warehouse missing")
    env.script(LIVE, answer_script("Team One had [[n1]] incidents.", [ref("n1", 40)]))
    message_id = env.ask()
    events = env.turn("live")
    (verification,) = _of(events, VerificationEvent)
    assert verification.status == "unverified"
    assert types_of(events)[-1] == "final"
    assert _reply(env, message_id)["verified"] == "unverified"
    assert env.memory.saved == []


def test_it06_11_no_user_message_and_foreign_message(chat_env: Env) -> None:
    """IT06-11 no user message → `NotFound` error event (R-19); a message of another session
    → `NotFound` and no assistant row."""
    env = chat_env
    events = env.turn("live")
    assert events == [ErrorEvent(error_type="NotFound", message="no user message to answer",
                                 hint=None)]  # fmt: skip
    service = env.service()
    foreign_session = create_chat_session(USER_REF, now=NOW)
    foreign = append_chat_message(foreign_session, "user", "hello", now=NOW)
    assert foreign is not None
    got = list(service._answer_message(env.session_id, foreign, USER_REF, "live"))
    assert got == [ErrorEvent(error_type="NotFound", message="message not found in session",
                              hint=None)]  # fmt: skip
    assert _assistant_rows(env) == []


def test_it06_11_turn_error_marks_rows_failed(chat_env: Env) -> None:
    """IT06-11 a `HernessError` inside the turn → one `error` event with the R-19 hint, the
    reply row `failed`, the run `failed`; a missing chat client is `ModelUnavailable`."""
    env = chat_env
    env.jobs.keys["live"] = None
    message_id = env.ask()
    with structlog.testing.capture_logs() as logs:
        events = env.turn("live")
    assert types_of(events) == ["mode", "error"]
    err = events[-1]
    assert isinstance(err, ErrorEvent)
    assert (err.error_type, err.hint) == ("ModelUnavailable", "Try again later.")
    row = _reply(env, message_id)
    assert row["status"] == "failed"
    (run,) = read_all("SELECT status FROM run WHERE kind = 'chat'", ())
    assert run["status"] == "failed"
    failed = [e for e in logs if e["event"] == "harness.chat.turn_failed"]
    assert [(e["log_level"], e["error_type"]) for e in failed] == [("error", "ModelUnavailable")]


def test_it06_11_no_build_is_not_found(chat_env: Env) -> None:
    """IT06-11 no promoted build → `NotFound` error event before any run is created."""
    env = chat_env
    env.ask()
    events = env.turn("live", current_build=lambda: None)
    err = events[-1]
    assert isinstance(err, ErrorEvent)
    assert (err.error_type, err.hint) == ("NotFound", None)
    assert read_all("SELECT run_id FROM run", ()) == []


# --- IT06-12 / IT06-34 defer and the chat job ------------------------------------------------


class _Ctx:
    """The `JobContext` surface `chat_job_handler` reads (R-42)."""

    def __init__(self, job_id: str) -> None:
        self.job = queue.get(job_id)


def test_it06_12_defer_twice_then_job(chat_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """IT06-12 mode `defer` called twice: one `chat` job, one `mode` event per call and no
    model call; the job then answers into the same assistant row."""
    env = chat_env
    client = env.script(LIVE, answer_script("Team One had [[n1]] incidents.", [ref("n1", 40)]))
    message_id = env.ask()
    first, second = env.turn("defer"), env.turn("defer")
    defer = ModeEvent(mode="defer", message=chat_mod.MODE_MESSAGES["defer"])
    assert first == second == [defer]
    jobs = read_all("SELECT job_id, kind, priority, idem_key FROM job", ())
    assert len(jobs) == 1
    (job,) = jobs
    assert (job["kind"], job["priority"]) == ("chat", 75)
    assert job["idem_key"] == f"chat:{env.session_id}:{message_id}"
    assert queue.get(job["job_id"]).payload == {
        "session_id": env.session_id, "message_id": message_id,
    }  # fmt: skip
    row = _reply(env, message_id)
    assert row["status"] == "queued"
    assert row["meta"] == {"reply_to": message_id, "mode": "defer", "job_id": job["job_id"]}
    assert client.requests == []
    monkeypatch.setattr(chat_mod, "_service_from_config", env.service)
    outcome = chat_mod.chat_job_handler(_Ctx(job["job_id"]))  # type: ignore[arg-type]
    done = _reply(env, message_id)
    assert done["message_id"] == row["message_id"]
    assert (done["status"], done["verified"]) == ("done", "verified")
    assert done["meta"]["mode"] == "live"
    assert outcome.status == "done"
    assert outcome.result == {"run_id": done["run_id"], "status": "verified"}
    assert len(_assistant_rows(env)) == 1


def test_it06_34_job_run_twice_no_second_model_call(
    chat_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-34 the `chat` job run twice: the second run finds the reply `done` and makes no
    model call; both return the same result."""
    env = chat_env
    client = env.script(LIVE, answer_script("Team One had [[n1]] incidents.", [ref("n1", 40)]))
    env.ask()
    (mode,) = env.turn("defer")
    assert mode.type == "mode"
    (job,) = read_all("SELECT job_id FROM job", ())
    monkeypatch.setattr(chat_mod, "_service_from_config", env.service)
    first = chat_mod.chat_job_handler(_Ctx(job["job_id"]))  # type: ignore[arg-type]
    calls = len(client.requests)
    second = chat_mod.chat_job_handler(_Ctx(job["job_id"]))  # type: ignore[arg-type]
    assert len(client.requests) == calls == 3
    assert second == first
    assert first.result["status"] == "verified"


def test_it06_34_job_payload_and_session_errors(chat_env: Env) -> None:
    """IT06-34 a payload without both ids → `ConfigError`; an unknown session → `NotFound`."""
    env = chat_env
    bad = queue.enqueue("chat", {"session_id": env.session_id}, "reasoning")
    with pytest.raises(ConfigError):
        chat_mod.chat_job_handler(_Ctx(bad))  # type: ignore[arg-type]
    unknown = "ses_" + "0" * 26
    gone = queue.enqueue("chat", {"session_id": unknown, "message_id": "msg_x"}, "reasoning")
    with pytest.raises(NotFound):
        chat_mod.chat_job_handler(_Ctx(gone))  # type: ignore[arg-type]


def test_it06_34_service_from_config(chat_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """IT06-34 the job builds its service from `get_config()` with the default deps."""
    del chat_env
    store = Memory()
    monkeypatch.setattr(chat_mod, "get_memory_store", lambda: store)
    monkeypatch.setattr(chat_mod, "LLMRegistry", lambda models, profile: (models, profile))
    service = chat_mod._service_from_config()
    assert isinstance(service, chat_mod.ChatService)


# --- IT06-13 cloud ---------------------------------------------------------------------------


def test_it06_13_cloud_in_local_runs_as_small_model(chat_env: Env) -> None:
    """IT06-13 profile `local` with `cloud`: the turn runs as `small_model` on the local
    reduced model; the off-network client is never called."""
    env = chat_env
    cloud = env.script(CLOUD, [])
    small = env.script(SMALL, answer_script("Team One had [[n1]] incidents.", [ref("n1", 40)],
                                            client=SMALL))  # fmt: skip
    message_id = env.ask()
    events = env.turn("cloud")
    assert events[0] == ModeEvent(mode="small_model", message=chat_mod.MODE_MESSAGES["small_model"])
    assert cloud.requests == []
    assert len(small.requests) == 3
    row = _reply(env, message_id)
    assert (row["meta"]["mode"], row["meta"]["model"]) == ("small_model", SMALL)
    (run,) = read_all("SELECT run_id FROM run", ())
    (task,) = select_tasks(run["run_id"])
    assert task.spec.model_role == "chat_off_hours"
    assert env.jobs.asked == ["small_model"]


def test_it06_13_egress_blocked_retries_locally_with_notice(
    chat_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-13 hybrid with chat approval keeps `cloud`; a planted name in a tool result makes
    the guard block the next off-network call (`EgressBlocked`): the turn reruns once on the
    local reduced model and the answer starts with the notice."""
    env = chat_env
    hcfg = with_policy(env.cfg, profile="hybrid", approved=True, egress=True)
    planted = ls.resp(calls=[call("run_sql", sql="SELECT owner")], client=CLOUD)
    cloud = env.script(CLOUD, [planted, EgressBlocked("request blocked: personal data")])
    small = env.script(SMALL, answer_script("Team One had [[n1]] incidents.", [ref("n1", 40)],
                                            client=SMALL))  # fmt: skip
    message_id = env.ask()
    events = list(env.service(hcfg=hcfg).answer(env.session_id, "x", USER_REF, "cloud"))
    assert events[0] == ModeEvent(mode="cloud", message=chat_mod.MODE_MESSAGES["cloud"])
    assert len(cloud.requests) == 2
    assert cloud.requests[0].messages  # the first call was off-network
    assert len(small.requests) == 3
    (fin,) = _of(events, FinalEvent)
    assert fin.answer.text.startswith(EGRESS_NOTICE)
    tokens = "".join(e.text for e in _of(events, TokenEvent))
    assert tokens.startswith(EGRESS_NOTICE)
    row = _reply(env, message_id)
    assert row["content"].startswith(EGRESS_NOTICE)
    assert (row["meta"]["mode"], row["meta"]["model"]) == ("cloud", SMALL)
    assert env.jobs.asked == ["cloud", "small_model"]
    assert "get_record" not in {t.name for t in cloud.requests[0].tools}


def test_it06_13_egress_blocked_outside_cloud_fails_the_turn(chat_env: Env) -> None:
    """IT06-13 `EgressBlocked` outside `cloud` is not retried: one `error` event."""
    env = chat_env
    env.script(LIVE, [EgressBlocked("blocked")])
    env.ask()
    events = env.turn("live")
    assert types_of(events) == ["mode", "error"]
    assert events[-1].type == "error"


# --- IT06-14 escalation ----------------------------------------------------------------------


@pytest.fixture
def promoted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(escalation, "read_current", lambda: BUILD_ID)


def _find_summary(session_id: str, run_id: str) -> ChatMessageRow | None:
    for row in list_chat_messages(session_id, limit=500):
        if row["role"] == "assistant" and row["meta"].get("escalation_run_id") == run_id:
            return row
    return None


def _append_summary(session_id: str, **fields: Any) -> str:
    assert fields.pop("role") == "assistant"
    anchor = append_chat_message(session_id, "user", "(escalation result)", now=NOW)
    assert anchor is not None
    row = upsert_assistant_placeholder(session_id, reply_to=anchor, now=NOW)
    assert row is not None
    assert update_chat_message(row["message_id"], **fields)
    return row["message_id"]


@pytest.mark.usefixtures("promoted")
def test_it06_14_budget_stop_escalates_to_mini_run(chat_env: Env) -> None:
    """IT06-14 a budget stop on a review-intent question escalates once: a `fast` mini run
    capped at 8 tasks, one `review` job and an `escalated` event; the run's summary then
    lands in the session as one assistant message."""
    env = chat_env
    burn = ls.resp(calls=[call("run_sql", sql="SELECT 1")], tin=30_000, tout=15_000)
    env.script(LIVE, [burn])
    message_id = env.ask("What should we fund next quarter?")
    events = env.turn("live")
    assert types_of(events) == ["mode", "escalated", "token", "verification", "final"]
    (esc,) = _of(events, EscalatedEvent)
    run = find_run_by_escalation(env.session_id, message_id)
    assert run is not None
    assert (run.run_id, run.meta["job_id"]) == (esc.run_id, esc.job_id)
    assert (run.kind, run.depth) == ("funding_review", "fast")
    request = run.meta["request"]
    assert isinstance(request, dict)
    override = request["budget_override"]
    assert override == env.cfg.pipelines.pipelines.chat.escalation
    knobs = resolve_knobs(env.cfg.pipelines, "funding_review", "fast", override=override)
    assert knobs.max_tasks_per_run <= 8
    jobs = read_all("SELECT job_id FROM job WHERE kind = 'review'", ())
    assert [j["job_id"] for j in jobs] == [esc.job_id]
    (fin,) = _of(events, FinalEvent)
    assert fin.answer.text == NO_VERIFIED_ANSWER
    assert _of(events, VerificationEvent)[0].status == "partial"
    # The mini run's executive summary posted back as one assistant turn (U06-135).
    draft = make_draft()
    summary_id = post_escalation_summary(
        run, draft, append_message=_append_summary, find_message=_find_summary
    )
    again = post_escalation_summary(
        run, draft, append_message=_append_summary, find_message=_find_summary
    )
    assert summary_id is not None
    assert again == summary_id
    summary = get_chat_message(summary_id)
    assert summary is not None
    assert (summary["session_id"], summary["meta"]["mode"]) == (env.session_id, "escalation")
    assert _reply(env, message_id)["status"] == "done"


@pytest.mark.usefixtures("promoted")
def test_it06_14_escalate_tool_once_per_turn(chat_env: Env) -> None:
    """IT06-14 the model's `escalate` tool, called twice, starts one run and one `escalated`
    event; the tool call is streamed as a `tool` event."""
    env = chat_env
    first = {"question": "Rank the teams that should improve", "reason": "needs a review"}
    second = {"question": "Rank all teams by on-call load", "reason": "same review"}
    twice = ls.resp(calls=[call("escalate", "c1", **first), call("escalate", "c2", **second)])
    env.script(LIVE, [twice, ls.resp("draft"), final("A review was started.")])
    message_id = env.ask("Which teams should improve their on-call?")
    events = env.turn("live")
    escalated = _of(events, EscalatedEvent)
    assert len(escalated) == 1
    assert [e.name for e in _of(events, ToolEvent)] == ["escalate", "escalate"]
    run = find_run_by_escalation(env.session_id, message_id)
    assert run is not None
    assert run.kind == "org_review"
    assert len(read_all("SELECT job_id FROM job WHERE kind = 'review'", ())) == 1


# --- IT06-23 one assistant row per turn ------------------------------------------------------


@pytest.mark.parametrize("mode", ["live", "small_model", "cloud", "defer"])
def test_it06_23_one_assistant_row_every_mode(chat_env: Env, mode: Any) -> None:
    """IT06-23 every mode, answered twice: exactly one assistant row for the user message."""
    env = chat_env
    key = LIVE if mode == "live" else SMALL
    env.script(key, answer_script("Team One had [[n1]] incidents.", [ref("n1", 40)], client=key))
    message_id = env.ask()
    first = env.turn(mode)
    second = env.turn(mode)
    rows = _assistant_rows(env)
    assert [r["meta"]["reply_to"] for r in rows] == [message_id]
    if mode == "defer":
        assert first == second
    else:
        assert types_of(first)[-1] == "final"
        assert second == []  # the reply is done: idempotent re-run


# --- IT06-36 correction capture after the answer ---------------------------------------------


class _RealSessionMemory(Memory):
    """Spec 07 chat session units on the real writer with the fake classifier (T07-21)."""

    def __init__(self, deps: memory_chat.ChatDeps) -> None:
        super().__init__()
        self._deps = deps

    def session_save_turn(self, session_id: str, run_id: str) -> str | None:
        self.saved.append((session_id, run_id))
        return memory_chat.session_save_turn(session_id, run_id, deps=self._deps)


def test_it06_36_correction_captured_after_final(chat_env: Env, tmp_path: Path) -> None:
    """IT06-36 a user correction classified by the fake spec 07 classifier: the
    `correction_captured` event follows `final` and the answer never mentions the capture."""
    env = chat_env
    ce = make_deps(tmp_path / "memory", ChatLLM(correction(0.99)))
    env.memory = _RealSessionMemory(ce.deps)
    text = "Noted: Team One had [[n1]] incidents."
    env.script(LIVE, answer_script(text, [ref("n1", 40)]))
    env.ask("Actually the payments service is owned by the platform team.")
    with structlog.testing.capture_logs() as logs:
        events = env.turn("live")
    assert types_of(events)[-2:] == ["final", "correction_captured"]
    (captured,) = _of(events, CorrectionCapturedEvent)
    (fin,) = _of(events, FinalEvent)
    assert fin.answer.text == text
    assert "captur" not in fin.answer.text.lower()
    assert "memory" not in "".join(e.text for e in _of(events, TokenEvent)).lower()
    logged = [e for e in logs if e["event"] == "harness.chat.correction_captured"]
    assert [(e["run_id"], e["memory_id"]) for e in logged] == [(fin.run_id, captured.memory_id)]


def test_it06_36_session_save_failure_after_final(chat_env: Env) -> None:
    """IT06-36 a failing session save after the answer: `final` delivered, WARNING
    `harness.chat.session_save_failed`, no `error` event."""
    env = chat_env
    env.memory = Memory(save=StoreBusy("ops store busy"))
    env.script(LIVE, answer_script("Team One had [[n1]] incidents.", [ref("n1", 40)]))
    message_id = env.ask()
    with structlog.testing.capture_logs() as logs:
        events = env.turn("live")
    assert types_of(events)[-1] == "final"
    assert _of(events, ErrorEvent) == []
    failed = [e for e in logs if e["event"] == "harness.chat.session_save_failed"]
    assert [(e["log_level"], e["error_type"]) for e in failed] == [("warning", "StoreBusy")]
    assert _reply(env, message_id)["status"] == "done"


def test_it06_13_default_deps_cloud_rule_and_unbound_vectors(chat_env: Env) -> None:
    """IT06-13 the process defaults: `cloud` is allowed only for the active profile under the
    R-38 rule, and ticket vector search fails closed until the composition root binds it."""
    env = chat_env
    local = chat_mod.ChatDeps.from_config(env.cfg)
    assert local.data_policy_allows_cloud("local") is False
    approved = with_policy(env.cfg, profile="hybrid", approved=True, egress=True)
    hybrid = chat_mod.ChatDeps.from_config(approved)
    assert hybrid.data_policy_allows_cloud("hybrid") is True
    assert hybrid.data_policy_allows_cloud("premium") is False
    with pytest.raises(NotFound):
        local.vectors.search_tickets([0.0], 1, entity=None, service_id=None)

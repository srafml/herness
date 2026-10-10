"""UT09-91: ``herness chat`` with scripted stdin: each slash command, turns and start-up (U09-99).

Runs the real Typer root app through ``CliRunner`` over a migrated ops store with a
``FakeChatService`` and a fixed chat policy (``tests.support.chat_cli_env``).
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from tests.support.chat_cli_env import BUILD_ID, ChatCli, chat_cli_env
from tests.support.cli_env import CliEnv
from tests.support.fake_chat import (
    ANSWER_TEXT,
    ESCALATED_RUN_ID,
    Q1,
    Q2,
    all_kinds,
    error_script,
    final_event,
    verification,
)
from tests.support.ops_store import OpsStoreHandle

from herness import cli
from herness._cli import _chat_turn, cmd_chat
from herness.core import time as clock
from herness.core.errors import ConfigError, NotFound, StoreBusy
from herness.core.types import ModeEvent, ToolEvent, VerificationEvent
from herness.reports import actions
from herness.reports.rules import UserInputError
from herness.store.ops.chat import create_chat_session, list_chat_messages, update_chat_message

pytestmark = pytest.mark.unit

QUESTION = "How many incidents? Mail bob@example.com"
REDACTED = "How many incidents? Mail [EMAIL]"
OTHER_REF = "f" * 32  # another user's user_ref


@pytest.fixture
def env(
    cli_env: CliEnv, ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ChatCli]:
    with chat_cli_env(cli_env, ops_store, monkeypatch) as chat_env:
        yield chat_env


def _session(env: ChatCli) -> str:
    rows = env.rows("SELECT session_id FROM chat_session ORDER BY created_at DESC")
    return str(rows[0][0])


def _users(session_id: str) -> list[str]:
    return [r["content"] for r in list_chat_messages(session_id) if r["role"] == "user"]


def _ok(result: object) -> str:
    """The stdout of a run that returned 0."""
    assert getattr(result, "exception", None) is None, getattr(result, "exception", None)
    assert getattr(result, "return_value", None) == 0
    return str(getattr(result, "stdout", ""))


# --- slash commands ------------------------------------------------------------------------------


def test_ut09_91_quit_and_end_of_input_exit_0(env: ChatCli) -> None:
    """UT09-91 `/quit` exits 0 at once; an empty line is ignored; end of input also exits 0."""
    out = _ok(env.run("chat", "--new", lines=("", "/quit", "never asked")))
    assert "/quit" in out  # the help line
    assert _users(_session(env)) == []
    assert env.chat.calls == []
    _ok(env.run("chat"))  # no input at all


def test_ut09_91_sessions_lists_only_own_sessions(env: ChatCli) -> None:
    """UT09-91 `/sessions` lists the actor's sessions (id, title, last active), no other user's."""
    _ok(env.run("chat", "--new", lines=("/quit",)))
    mine = _session(env)
    other = create_chat_session(OTHER_REF, now=clock.now())
    env.write("UPDATE chat_session SET title = 'Bob chat' WHERE session_id = ?", other)
    env.write("UPDATE chat_session SET title = 'Funding' WHERE session_id = ?", mine)
    out = _ok(env.run("chat", "--session", mine, lines=("/sessions", "/quit")))
    line = next(x for x in out.splitlines() if f"{mine}  " in x)
    assert "Funding" in line
    assert other not in out
    assert "Bob chat" not in out


def test_ut09_91_evidence_prints_query_and_masked_sample(env: ChatCli) -> None:
    """UT09-91 `/evidence q_…`: SQL, params, build, row count and policy-masked sample rows."""
    sample = [
        {"team": "T1", "n": 42, "title": "secret title", "at": "2026-01-01"},
        {"team": "T2", "n": 7, "title": "x", "at": "2026-01-02"},
    ]
    env.add_evidence(Q1, "SELECT team, n FROM incidents", sample, {"team": "T1"})
    out = _ok(env.run("chat", "--new", lines=(f"/evidence {Q1}", "/quit")))
    assert "SELECT team, n FROM incidents" in out
    assert BUILD_ID in out
    assert "2 rows" in out
    assert '"team": "T1"' in out  # params
    assert "at=2026-01-01" in out  # verbatim-safe date column kept
    assert "secret title" not in out  # always-dropped column (shared sample policy)
    assert "n=42" not in out  # numbers are redacted or dropped by the policy, never raw


def test_ut09_91_evidence_sample_rows_limit(env: ChatCli) -> None:
    """UT09-91 at most ``evidence_sample_rows`` sample rows are printed."""
    rows = [{"at": f"2026-01-{d:02d}"} for d in range(1, 10)]
    env.add_evidence(Q1, "SELECT at FROM t", rows)
    out = _ok(env.run("--set", "app.reports.evidence_sample_rows=3", "chat", "--new",
                      lines=(f"/evidence {Q1}", "/quit")))  # fmt: skip
    assert [x for x in out.splitlines() if "at=2026-01-" in x] == [
        "  at=2026-01-01", "  at=2026-01-02", "  at=2026-01-03",
    ]  # fmt: skip


def test_ut09_91_evidence_validated_and_not_found(env: ChatCli) -> None:
    """UT09-91 a bad id is rejected before any lookup; an unknown id: "Evidence not found."."""
    out = _ok(env.run("chat", "--new", lines=("/evidence q_xyz", f"/evidence {Q2}", "/quit")))
    assert "Error: invalid query id" in out
    assert "Evidence not found." in out


def test_ut09_91_correct_needs_a_question_first(env: ChatCli) -> None:
    """UT09-91 `/correct` before any user row: the hint, no proposal."""
    out = _ok(env.run("chat", "--new", lines=("/correct Team One had 40.", "/quit")))
    assert "Ask a question before correcting a fact." in out
    assert env.memory.calls == []


def test_ut09_91_correct_proposes_with_session_and_latest_user_row(env: ChatCli) -> None:
    """UT09-91 `/correct <text>`: ``propose`` with the session and its latest user row (R-33)."""
    env.chat.scripts = [all_kinds()]
    out = _ok(env.run("chat", "--new", lines=(QUESTION, "/correct Team One had 40.", "/quit")))
    session_id = _session(env)
    user_row = next(r for r in list_chat_messages(session_id) if r["role"] == "user")
    ((name, (proposal,), _kw),) = env.memory.calls
    assert name == "propose"
    assert proposal.provenance.session_id == session_id
    assert proposal.provenance.source_message_id == user_row["message_id"]
    assert proposal.content == "Team One had 40."
    assert "Saved as pending. A reviewer must approve it" in out


def test_ut09_91_retry_reruns_latest_user_row_without_new_row(env: ChatCli) -> None:
    """UT09-91 an error turn offers /retry; `/retry` re-answers the same row, no new user row."""
    env.chat.scripts = [error_script(), all_kinds()]
    out = _ok(env.run("chat", "--new", lines=(QUESTION, "/retry", "/quit")))
    session_id = _session(env)
    assert "Error: The model is not reachable." in out
    assert "Fix: Try again later." in out
    assert "Type /retry to try again." in out
    assert _users(session_id) == [REDACTED]
    assert [c[1] for c in env.chat.calls] == [REDACTED, REDACTED]
    assert ANSWER_TEXT in out


def test_ut09_91_retry_without_question(env: ChatCli) -> None:
    """UT09-91 `/retry` in a session without a user row: "Nothing to retry."."""
    out = _ok(env.run("chat", "--new", lines=("/retry", "/quit")))
    assert "Nothing to retry." in out
    assert env.chat.calls == []


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("/delete everything", "Error: Unknown command."),
        ("/quit now", "Error: Usage: /quit"),
        ("/sessions all", "Error: Usage: /sessions"),
        ("/evidence", "Error: Usage: /evidence q_"),
        ("/correct", "Error: Usage: /correct <text>"),
        ("/retry 2", "Error: Usage: /retry"),
    ],
)
def test_ut09_91_bad_commands_fail_closed(env: ChatCli, line: str, message: str) -> None:
    """UT09-91 unknown commands and bad arguments: a message and no action at all."""
    out = _ok(env.run("chat", "--new", lines=(line, "/quit")))
    assert message in out
    assert "Fix: Commands: /quit" in out
    assert env.chat.calls == []
    assert env.memory.calls == []
    assert _users(_session(env)) == []


def test_ut09_91_parse_command_table() -> None:
    """UT09-91 the parser returns ``(name, argument)`` only for valid commands."""
    assert cmd_chat.parse_command("/evidence  " + Q1) == ("/evidence", Q1)
    assert cmd_chat.parse_command("/correct  a b ") == ("/correct", "a b")
    assert cmd_chat.parse_command("/quit") == ("/quit", "")
    with pytest.raises(UserInputError, match="invalid query id"):
        cmd_chat.parse_command("/evidence q_0123")


# --- questions -----------------------------------------------------------------------------------


def test_ut09_91_question_turn_prints_answer_tag_evidence_and_notices(env: ChatCli) -> None:
    """UT09-91 a live turn: tool lines, the answer, the tag, evidence ids, escalation, notice."""
    env.chat.scripts = [all_kinds()]
    out = _ok(env.run("chat", "--new", lines=(QUESTION, "/quit")))
    session_id = _session(env)
    assert _users(session_id) == [REDACTED]
    assert [(c[0], c[1], c[3]) for c in env.chat.calls] == [(session_id, REDACTED, "live")]
    assert "run_sql · q_0123456789abcdef · ok" in out
    assert "run_sql · - · failed" in out
    assert ANSWER_TEXT in out
    assert "[verified]" in out
    assert f"Evidence: {Q1}" in out
    assert f"Escalated to run `{ESCALATED_RUN_ID}`; the summary will appear" in out
    assert "Your correction was saved for review." in out
    assert "Team One had 42." not in out  # tokens are collected, never printed
    assert QUESTION not in out
    assert "Outside chat hours" not in out  # no banner when live


def test_ut09_91_partial_answer_lists_removed_claims(env: ChatCli) -> None:
    """UT09-91 a partially verified answer prints its tag and the removed claims."""
    env.chat.scripts = [[
        ModeEvent(mode="small_model", message="m"),
        VerificationEvent(result=verification(), status="partial", removed_claims=["X rose"]),
        final_event(),
    ]]  # fmt: skip
    env.mode[0] = "small_model"
    out = _ok(env.run("chat", "--new", lines=(QUESTION, "/quit")))
    assert "Outside chat hours: answering with a reduced model" in out
    assert "[partially verified]" in out
    assert "Removed claims: X rose" in out


def test_ut09_91_defer_polls_until_the_row_is_final(env: ChatCli) -> None:
    """UT09-91 `defer`: the queued banner, then a poll every `poll_queued_s` until done."""
    env.mode[0] = "defer"
    env.chat.scripts = [[ModeEvent(mode="defer", message="queued")]]

    def finish() -> None:
        if len(env.sleeps) == 2:
            (reply,) = env.chat.written
            update_chat_message(reply, status="done", content="Late answer.", verified="verified")

    env.on_sleep = finish
    out = _ok(env.run("chat", "--new", lines=(QUESTION, "/quit")))
    assert "The GPU is running scheduled work. Your question is queued" in out
    assert env.sleeps == [15, 15]
    assert "Late answer." in out
    assert "[verified]" in out


def test_ut09_91_defer_ctrl_c_returns_to_prompt(env: ChatCli) -> None:
    """UT09-91 Ctrl+C during the poll returns to the prompt; the queued row stays queued."""
    env.mode[0] = "defer"
    env.chat.scripts = [[ModeEvent(mode="defer", message="queued")]]

    def interrupt() -> None:
        raise KeyboardInterrupt

    env.on_sleep = interrupt
    out = _ok(env.run("chat", "--new", lines=(QUESTION, "/sessions", "/quit")))
    assert "Stopped waiting; the answer will appear in this chat later." in out
    (reply,) = env.chat.written
    assert (
        next(r for r in list_chat_messages(_session(env)) if r["message_id"] == reply)["status"]
        == "queued"
    )
    assert _session(env) in out.split("Stopped waiting", 1)[1]  # /sessions ran afterwards


def test_ut09_91_failed_reply_and_generator_error(env: ChatCli) -> None:
    """UT09-91 a final without a done row prints the failure; a raising generator an Error."""
    env.chat.write_rows = False
    env.chat.scripts = [[final_event()], [ToolEvent(name="x", query_id=None, ok=True)]]
    env.chat.raises = None
    out = _ok(env.run("chat", "--new", lines=(QUESTION, "/quit")))
    assert "This answer could not be produced." in out
    env.chat.raises = StoreBusy("busy")
    out = _ok(env.run("chat", lines=("Again?", "/quit")))
    assert "Error: " in out
    assert "Type /retry to try again." in out


def test_ut09_91_question_too_long_is_rejected(env: ChatCli) -> None:
    """UT09-91 a question over `chat.max_question_chars`: Error line, no row, no answer."""
    out = _ok(env.run("chat", "--new", lines=("x" * 4001, "/quit")))
    assert "Error: Question must be 1 to 4000 characters." in out
    assert _users(_session(env)) == []
    assert env.chat.calls == []


def test_ut09_91_fatal_errors_end_the_command(
    env: ChatCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-91 a ConfigError during a turn is fatal: it ends the command (U09-84)."""

    def boom(*_a: object, **_k: object) -> None:
        msg = "key missing"
        raise ConfigError(msg)

    monkeypatch.setattr(actions, "post_user_message", boom)
    result = env.run("chat", "--new", lines=(QUESTION, "/quit"))
    assert isinstance(result.exception, ConfigError)


# --- start-up ------------------------------------------------------------------------------------


def test_ut09_91_session_selection(env: ChatCli) -> None:
    """UT09-91 no option reuses the most recent session; `--session` continues an owned one."""
    _ok(env.run("chat", lines=("/quit",)))  # none yet: a new one
    first = _session(env)
    _ok(env.run("chat", lines=("/quit",)))
    assert env.rows("SELECT count(*) FROM chat_session") == [(1,)]
    _ok(env.run("chat", "--new", lines=("/quit",)))
    assert env.rows("SELECT count(*) FROM chat_session") == [(2,)]
    out = _ok(env.run("chat", "--session", first, lines=("/quit",)))
    assert f"Session {first}." in out


def test_ut09_91_history_prints_last_rows_with_tags(env: ChatCli) -> None:
    """UT09-91 the history: user rows as text, answers with their tag, other states briefly."""
    env.chat.scripts = [all_kinds(), error_script()]
    _ok(env.run("chat", "--new", lines=(QUESTION, "Second?", "/quit")))
    session_id = _session(env)
    out = _ok(env.run("chat", "--session", session_id, lines=("/quit",)))
    assert f"you> {REDACTED}" in out
    assert ANSWER_TEXT in out
    assert "[verified]" in out
    assert "This answer could not be produced." in out
    rows = list_chat_messages(session_id)
    update_chat_message(rows[-1]["message_id"], status="queued")
    out = _ok(env.run("chat", "--session", session_id, lines=("/quit",)))
    assert "Queued; the answer will appear here later." in out
    update_chat_message(rows[-1]["message_id"], status="done", content="ok", verified=None)
    out = _ok(env.run("chat", "--session", session_id, lines=("/quit",)))
    assert "[unverified]" in out


def test_ut09_91_other_users_session_not_found(env: ChatCli) -> None:
    """UT09-91 `--session` of another user looks missing (TH09-10); bad ids are rejected."""
    other = create_chat_session(OTHER_REF, now=clock.now())
    result = env.run("chat", "--session", other)
    assert isinstance(result.exception, NotFound)
    result = env.run("chat", "--session", "nope")
    assert isinstance(result.exception, UserInputError)


def test_ut09_91_option_conflicts_and_json(env: ChatCli) -> None:
    """UT09-91 `--new --session` and `--json` are usage errors before any session is made."""
    result = env.run("chat", "--new", "--session", "ses_01J00000000000000000000000")
    assert isinstance(result.exception, UserInputError)
    result = env.run("--json", "chat")
    assert isinstance(result.exception, UserInputError)
    assert str(result.exception) == "chat is interactive and has no --json mode"
    assert env.rows("SELECT count(*) FROM chat_session") == [(0,)]


def test_ut09_91_main_exit_codes_and_help(env: ChatCli, capsys: pytest.CaptureFixture[str]) -> None:
    """UT09-91 through ``main``: `chat --help` exits 0; `--json chat` exits 2."""
    del env
    for argv, code in ((["chat", "--help"], 0), (["--json", "chat"], 2)):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == code
    out = capsys.readouterr().out
    assert "--session" in out
    assert "no --json mode" in out


def test_ut09_91_register_groups_adds_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-91 the root's group registration adds ``chat`` (U09-84 step 4)."""
    saved = list(cli.app.registered_commands)
    try:
        cli._register_groups(cli.app)
        assert "chat" in [c.name for c in cli.app.registered_commands]
    finally:
        cli.app.registered_commands[:] = saved
    del monkeypatch


def test_ut09_91_build_services_wires_the_dashboard_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT09-91 the chat service gets the pipelines config, the registry and the memory store."""
    import herness.harness.memory as memory_mod  # noqa: PLC0415
    import herness.harness.pipelines.chat as chat_mod  # noqa: PLC0415
    from herness.core import resilience  # noqa: PLC0415
    from herness.harness.llm import registry  # noqa: PLC0415

    bound: list[object] = []
    monkeypatch.setattr(memory_mod, "get_memory_store", lambda: "memory")
    monkeypatch.setattr(registry, "LLMRegistry", lambda models, profile: ("llms", models, profile))
    monkeypatch.setattr(resilience, "bind_chain_registry", bound.append)
    monkeypatch.setattr(chat_mod, "ChatService", lambda *a: ("chat", *a))
    cfg = SimpleNamespace(models="models", profile="local", pipelines="pipelines")
    service, memory = cmd_chat.build_services(cfg)  # type: ignore[arg-type]
    llms = ("llms", "models", "local")
    assert (service, memory, bound) == (("chat", "pipelines", llms, "memory"), "memory", [llms])


def test_ut09_91_policy_reads_the_server_side_chat_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT09-91 the mode comes from ``chat_policy`` (R-38); the slot only for ``defer``."""
    from herness.core.jobs import chat_policy  # noqa: PLC0415

    now = clock.now()
    modes = iter(["defer", "live"])
    monkeypatch.setattr(chat_policy, "chat_policy", lambda _now: next(modes))
    monkeypatch.setattr(chat_policy, "chat_next_live_at", lambda at: at)
    assert _chat_turn.policy(now) == ("defer", now)
    assert _chat_turn.policy(now) == ("live", None)

"""IT09-20: ``herness chat --new`` over a migrated ops store with a ``FakeChatService`` (F09-09).

The same row rules as the dashboard's IT09-12: the terminal writes exactly one redacted user row
per turn, never an assistant row (every one is listed in ``FakeChatService.written``) and never a
``chat`` job; the mode comes only from the policy. The slash commands work in the same session.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Iterator

import pytest
from tests.support.chat_cli_env import ChatCli, chat_cli_env
from tests.support.cli_env import CliEnv
from tests.support.fake_chat import ANSWER_TEXT, Q1, all_kinds, error_script
from tests.support.ops_store import OpsStoreHandle

from herness import cli
from herness.core import time as clock
from herness.core.types import ChatMode
from herness.store.ops.chat import create_chat_session, list_chat_messages, update_chat_message

pytestmark = pytest.mark.integration

QUESTION = "How many incidents did Team One have? Mail me at bob@example.com"
REDACTED = "How many incidents did Team One have? Mail me at [EMAIL]"
MODES: tuple[ChatMode, ...] = ("live", "small_model", "defer", "cloud")


@pytest.fixture
def env(
    cli_env: CliEnv, ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ChatCli]:
    with chat_cli_env(cli_env, ops_store, monkeypatch) as chat_env:
        yield chat_env


def _only_session(env: ChatCli) -> str:
    ((session_id,),) = env.rows("SELECT session_id FROM chat_session")
    return str(session_id)


@pytest.mark.parametrize("mode", MODES)
def test_it09_20_one_user_row_per_turn_no_assistant_row_no_job(
    env: ChatCli, mode: ChatMode
) -> None:
    """IT09-20 each mode: one redacted user row per turn, no assistant row, no chat job."""
    env.mode[0] = mode
    env.chat.scripts = [all_kinds(mode)]

    def finish_deferred() -> None:  # spec 06's worker finishing the queued answer
        update_chat_message(env.chat.written[-1], status="done", content="Later.",
                            verified="unverified")  # fmt: skip

    env.on_sleep = finish_deferred
    result = env.run("chat", "--new", lines=(QUESTION, "Second question", "/quit"))
    assert (result.exception, result.return_value) == (None, 0)
    session_id = _only_session(env)
    rows = list_chat_messages(session_id)
    assert [r["content"] for r in rows if r["role"] == "user"] == [REDACTED, "Second question"]
    assert [r["message_id"] for r in rows if r["role"] == "assistant"] == env.chat.written
    assert env.rows("SELECT count(*) FROM job WHERE kind = 'chat'") == [(0,)]
    assert [(c[0], c[1], c[3]) for c in env.chat.calls] == [
        (session_id, REDACTED, mode), (session_id, "Second question", mode),
    ]  # fmt: skip
    assert QUESTION not in result.stdout
    if mode == "defer":
        assert "Your question is queued" in result.stdout
        assert result.stdout.count("Later.") == 2
    else:
        assert result.stdout.count(ANSWER_TEXT) == 2
        assert f"Evidence: {Q1}" in result.stdout


def test_it09_20_slash_commands_in_one_session(env: ChatCli) -> None:
    """IT09-20 /sessions, /evidence, /retry, /correct and /quit in one terminal session."""
    env.chat.scripts = [error_script(), all_kinds()]
    env.add_evidence(Q1, "SELECT count(*) AS n FROM incidents", [{"day": "2026-01-01"}])
    lines = (QUESTION, "/retry", f"/evidence {Q1}", "/sessions", "/correct It was 40.", "/quit")
    result = env.run("chat", "--new", lines=lines)
    assert (result.exception, result.return_value) == (None, 0)
    out = result.stdout
    session_id = _only_session(env)
    assert out.index("Type /retry to try again.") < out.index(ANSWER_TEXT)
    assert "SELECT count(*) AS n FROM incidents" in out
    assert "day=2026-01-01" in out
    assert f"{session_id}  How many incidents" in out
    assert "Saved as pending." in out
    users = [r for r in list_chat_messages(session_id) if r["role"] == "user"]
    assert [u["content"] for u in users] == [REDACTED]  # /retry wrote no row
    ((_name, (proposal,), _kw),) = env.memory.calls
    assert proposal.provenance.source_message_id == users[0]["message_id"]


def test_it09_20_main_wrong_owner_and_resume(
    env: ChatCli, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT09-20 through ``main``: another user's session exits 7 (not found); own one resumes."""
    other = create_chat_session("f" * 32, now=clock.now())
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config-dir", str(env.cli.config_dir), "chat", "--session", other])
    assert exc.value.code == 7
    assert other not in capsys.readouterr().out
    monkeypatch.setattr(sys, "stdin", io.StringIO("/quit\n"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config-dir", str(env.cli.config_dir), "chat", "--new"])
    assert exc.value.code == 0
    assert "Session ses_" in capsys.readouterr().out

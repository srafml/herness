"""ST09-20 (TH09-21, TH09-12) for the terminal chat: ``herness chat --new`` (T09-26).

The dashboard half is ``test_st09_chat.py`` (AppTest, captured structlog events). Here the
real command runs through ``herness.cli.main`` (``chat_cli_env``: migrated ops store,
``FakeChatService`` that writes the assistant row, e-mail masking as the redaction double), so
the INFO-and-above lines land in the real log file and on stderr; a unique marker string with an
e-mail address is typed at the prompt.
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Iterator

import pytest
from tests.support.chat_cli_env import ChatCli, chat_cli_env
from tests.support.cli_env import CliEnv
from tests.support.fake_chat import all_kinds
from tests.support.ops_store import OpsStoreHandle

from herness import cli
from herness.store.ops.chat import list_chat_messages

pytestmark = pytest.mark.integration

MARKER = "zq-cli-marker-5519"
EMAIL = "erin.secret@example.com"
LEVELS = {"info", "warning", "error", "critical"}


@pytest.fixture
def env(
    cli_env: CliEnv, ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ChatCli]:
    with chat_cli_env(cli_env, ops_store, monkeypatch) as chat_env:
        yield chat_env


def test_st09_20_terminal_chat_marker_absent_from_logs_and_stored_text_redacted(
    env: ChatCli, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ST09-20 terminal chat with a unique marker and an e-mail: neither is in any INFO+ log
    line (log file and stderr); the stored user text and the service's input are redacted."""
    env.chat.scripts = [all_kinds()]
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"Question {MARKER} {EMAIL}\n/quit\n"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config-dir", str(env.cli.config_dir), "chat", "--new"])
    assert exc.value.code == 0
    err = capsys.readouterr().err
    files = sorted((env.cli.root / "data" / "logs").glob("herness-*.jsonl"))
    assert files, "no log file written"
    lines = [json.loads(raw) for path in files for raw in path.read_text("utf-8").splitlines()]
    shown = [line for line in lines if line.get("level") in LEVELS]
    assert {"app.chat.message_posted", "cli.command.completed"} <= {e["event"] for e in shown}
    for text in (json.dumps(shown), err):
        assert MARKER not in text
        assert EMAIL not in text
    ((session_id,),) = env.rows("SELECT session_id FROM chat_session")
    users = [r for r in list_chat_messages(session_id) if r["role"] == "user"]
    assert [u["content"] for u in users] == [f"Question {MARKER} [EMAIL]"]
    assert env.chat.calls[0][1] == f"Question {MARKER} [EMAIL]"
    assert env.rows("SELECT count(*) FROM chat_message WHERE content LIKE ?", f"%{EMAIL}%") == [
        (0,)
    ]

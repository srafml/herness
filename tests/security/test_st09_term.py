"""ST09-15: terminal escape sequences in model text, memory or store content (TH09-15).

An answer carrying an OSC 52 clipboard write (``ESC ] 52 ; c ; … BEL``) and a CSI screen clear
(``ESC [ 2 J``) — and the same payloads in tool names, error messages, removed claims, session
titles and evidence cells — reach the terminal chat's output with every escape removed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from tests.support.chat_cli_env import ChatCli, chat_cli_env
from tests.support.cli_env import CliEnv
from tests.support.fake_chat import Q1, final_event, verification
from tests.support.ops_store import OpsStoreHandle

from herness.core.errors import StoreBusy
from herness.core.types import ErrorEvent, ModeEvent, ToolEvent, VerificationEvent

pytestmark = pytest.mark.unit

OSC52 = "\x1b]52;c;cm0gLXJmIH4=\x07"
CLEAR = "\x1b[2J"
EVIL = f"{OSC52}Team One had 42 incidents.{CLEAR}\x9b31m[bold]x[/bold] :warning:"
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@pytest.fixture
def env(
    cli_env: CliEnv, ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ChatCli]:
    with chat_cli_env(cli_env, ops_store, monkeypatch) as chat_env:
        yield chat_env


def _clean(out: str) -> None:
    """No ESC, no control character but newline and tab; sequence bodies only as JSON text."""
    assert "\x1b" not in out
    assert _CONTROL.search(out.replace("\r\n", "\n")) is None
    assert out.count("52;c;") == out.count("\\u001b]52;c;")  # JSON-escaped params only
    assert "[2J" not in out


def test_st09_15_answer_escapes_removed(env: ChatCli) -> None:
    """ST09-15 an answer with OSC 52 and CSI 2J prints without any escape sequence."""
    env.chat.scripts = [[
        ModeEvent(mode="live", message="m"),
        ToolEvent(name=f"run_sql{CLEAR}", query_id=Q1, ok=True),
        VerificationEvent(result=verification(), status="partial", removed_claims=[EVIL]),
        final_event(EVIL),
    ]]  # fmt: skip
    result = env.run("chat", "--new", lines=("How many?", "/quit"))
    assert result.exception is None
    _clean(result.stdout)
    assert "Team One had 42 incidents." in result.stdout
    assert "[bold]x[/bold]" in result.stdout  # markup printed literally, never applied
    assert ":warning:" in result.stdout  # emoji codes printed literally (Console emoji=False)
    history = env.run("chat", lines=("/quit",))  # the stored row, printed as history
    _clean(history.stdout)
    assert "Team One had 42 incidents." in history.stdout


def test_st09_15_errors_titles_and_evidence_cells_cleaned(env: ChatCli) -> None:
    """ST09-15 error text, a session title and an evidence cell lose their escapes too."""
    env.chat.scripts = [[ErrorEvent(error_type="X", message=f"bad{OSC52}", hint=CLEAR + "hint")]]
    env.chat.raises = None
    result = env.run("chat", "--new", lines=("Question?", "/quit"))
    _clean(result.stdout)
    assert "Error: bad" in result.stdout
    env.write("UPDATE chat_session SET title = ?", f"T{OSC52}{CLEAR}itle")
    env.add_evidence(Q1, f"SELECT 1{CLEAR}", [{"day": "2026-01-01"}], {"p": OSC52})
    env.chat.raises = StoreBusy(f"busy{CLEAR}")
    result = env.run("chat", lines=("/sessions", f"/evidence {Q1}", "Again?", "/quit"))
    assert result.exception is None
    _clean(result.stdout)
    assert "Title" in result.stdout
    assert "SELECT 1" in result.stdout

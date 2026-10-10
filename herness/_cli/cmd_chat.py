"""Terminal chat: ``herness chat [--session ID] [--new]`` (impl 09 U09-99, F09-09; design §5.6).

The same modes and messages as the dashboard chat. The CLI writes only the user row of a turn,
through ``herness.reports.actions`` (``ChatService`` writes the answer and, for ``defer``, the
job); the mode comes only from the server-side ``chat_policy`` (R-38); every session read and
write checks ownership (TH09-10). A slash command is parsed and validated before anything runs
(fail closed). Every stored or model-made text is printed through ``safe_terminal_text``
(TH09-15). Store, job and harness modules are imported inside functions so ``herness --help``
stays fast (BT09-07). ``chat`` calls ``check_command_role`` itself (what ``guarded`` does) so the
``--json`` refusal comes first; no ``from __future__ import annotations``, so every option
annotation is a real object whichever way the signature is read (controller addendum, T09-23).
"""

import json
import re
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Final, cast

import typer
from rich.console import Console
from typer._click.globals import get_current_context

from herness._cli import _chat_turn as turn
from herness._cli._chat_turn import COMMANDS, PROMPT, Services, Term
from herness._cli.identity import check_command_role
from herness.core import time as clock
from herness.core.errors import ConfigError, HernessError, PermissionDenied
from herness.reports.rules import Actor, UserInputError, check_id, user_message

if TYPE_CHECKING:
    from herness.cli import GlobalOptions
    from herness.core.config import HernessConfig
    from herness.store.ops.chat import ChatMessageRow

__all__ = ["build_services", "parse_command", "register"]

NO_JSON: Final = "chat is interactive and has no --json mode"
SAVED: Final = "Saved as pending. A reviewer must approve it before it affects answers or scores."
ASK_FIRST: Final = "Ask a question before correcting a fact."
NOTHING_TO_RETRY: Final = "Nothing to retry."
NO_EVIDENCE: Final = "Evidence not found."
# Slash command -> usage; the argument is required for the commands whose usage names one.
USAGE: Final = {
    "/quit": "/quit", "/sessions": "/sessions", "/retry": "/retry",
    "/evidence": "/evidence q_<16 hex>", "/correct": "/correct <text>",
}  # fmt: skip
# The pattern of herness.reports.contract.QUERY_ID_RE (and herness.core.types.harness.evidence);
# copied because importing contract loads the ops store on every `herness` start (BT09-07).
_QUERY_ID: Final = re.compile(r"^q_[0-9a-f]{16}$")


def build_services(cfg: "HernessConfig") -> Services:
    """The chat service and memory store, built as the dashboard builds them (U09-53)."""
    from herness.core.resilience import bind_chain_registry  # noqa: PLC0415 - lazy (BT09-07)
    from herness.harness.llm.registry import LLMRegistry  # noqa: PLC0415
    from herness.harness.memory import get_memory_store  # noqa: PLC0415
    from herness.harness.pipelines.chat import ChatService  # noqa: PLC0415

    memory = get_memory_store()
    llms = LLMRegistry(cfg.models, profile=cfg.profile)
    bind_chain_registry(llms)  # R-04 port read by chat_policy
    return ChatService(cfg.pipelines, llms, memory), memory


def parse_command(line: str) -> tuple[str, str]:
    """``(name, argument)`` of a slash command; UserInputError for anything else (fail closed)."""
    name, _, rest = line.strip().partition(" ")
    arg = rest.strip()
    if name not in USAGE:
        msg = "Unknown command."
        raise UserInputError(msg, hint=COMMANDS)
    if bool(arg) != ("<" in USAGE[name]):
        msg = f"Usage: {USAGE[name]}"
        raise UserInputError(msg, hint=COMMANDS)
    if name == "/evidence":
        check_id(_QUERY_ID, arg, "query")
    return name, arg


# --- slash commands ------------------------------------------------------------------------------


def _owned_latest(term: Term) -> "ChatMessageRow | None":
    """The latest user row of the session, after the ownership check (TH09-10)."""
    from herness.reports.actions import require_session_owner  # noqa: PLC0415 - lazy (BT09-07)
    from herness.store.ops.chat import latest_user_message  # noqa: PLC0415

    require_session_owner(term.actor, term.session_id)
    return latest_user_message(term.session_id)


def _sessions(term: Term, _arg: str) -> None:
    from herness.store.ops.chat import list_chat_sessions  # noqa: PLC0415 - lazy (BT09-07)

    for row in list_chat_sessions(term.actor.user_ref):
        title = row["title"] or "Chat"
        term.say(f"{row['session_id']}  {title}  {row['last_active_at']:%Y-%m-%d %H:%M}")


def _evidence(term: Term, query_id: str) -> None:
    """SQL, params, build, row count and the policy-masked sample rows of one query."""
    from herness.reports._sample_policy import sample_rows  # noqa: PLC0415 - lazy (BT09-07)
    from herness.reports.actions import require_session_owner  # noqa: PLC0415
    from herness.store.ops.ui_reads import ui_get_evidence_rows  # noqa: PLC0415

    require_session_owner(term.actor, term.session_id)
    row = ui_get_evidence_rows([query_id]).get(query_id)
    if row is None:
        term.say(NO_EVIDENCE)
        return
    term.say(f"{query_id} · build {row['build_id']} · {row['row_count']} rows")
    term.say(row["sql"])
    term.say("Params: " + json.dumps(row["params"], ensure_ascii=False, default=str))
    limit = term.cfg.app.reports.evidence_sample_rows
    for sample in sample_rows(row["result_sample"], limit) or []:
        term.say("  " + ", ".join(f"{name}={value}" for name, value in sample.items()))


def _correct(term: Term, statement: str) -> None:
    """A pending correction for the latest user row of the session (R-33)."""
    from herness.reports.actions import propose_correction  # noqa: PLC0415 - lazy (BT09-07)

    latest = _owned_latest(term)
    if latest is None:
        term.say(ASK_FIRST)
        return
    propose_correction(
        term.actor, session_id=term.session_id, statement=statement,
        source_message_id=latest["message_id"], memory=term.svc()[1],
    )  # fmt: skip
    term.say(SAVED, "green")


def _retry(term: Term, _arg: str) -> None:
    latest = _owned_latest(term)
    if latest is None:
        term.say(NOTHING_TO_RETRY)
        return
    turn.answer(term, latest["content"])


# --- command -------------------------------------------------------------------------------------

_HANDLERS: Final[dict[str, Callable[[Term, str], None]]] = {
    "/sessions": _sessions, "/evidence": _evidence, "/correct": _correct, "/retry": _retry,
}  # fmt: skip


def _line(term: Term, line: str) -> bool:
    """Run one input line; False after ``/quit``. Per-turn errors print; fatal ones propagate."""
    try:
        if not line.startswith("/"):
            turn.ask(term, line)
            return True
        name, arg = parse_command(line)
        if name == "/quit":
            return False
        _HANDLERS[name](term, arg)
    except (ConfigError, PermissionDenied):
        raise
    except HernessError as exc:
        term.error(*user_message(exc))
    return True


def _session(actor: Actor, session: str | None, *, new: bool) -> str:
    from herness.reports import actions  # noqa: PLC0415 - lazy (BT09-07)
    from herness.store.ops.chat import list_chat_sessions  # noqa: PLC0415

    if session is not None:
        return actions.require_session_owner(actor, session)["session_id"]
    if not new and (recent := list_chat_sessions(actor.user_ref, limit=1)):
        return recent[0]["session_id"]
    return actions.start_session(actor, now=clock.now())


def chat(
    session: Annotated[str | None, typer.Option("--session", help="Continue session ID.")] = None,
    new: Annotated[bool, typer.Option("--new", help="Start a new session.")] = False,
) -> int:
    """Chat about the data in the terminal (design 09 §5.6)."""
    opts = cast("GlobalOptions", get_current_context().find_root().obj)
    opts.command = "chat"
    if opts.json:
        raise UserInputError(NO_JSON)
    if new and session is not None:
        msg = "Use --new or --session, not both."
        raise UserInputError(msg)
    actor = cast("Actor", check_command_role(opts, "chat"))  # viewer; ui_user_ref_key needed
    con = Console(file=sys.stdout, highlight=False, emoji=False, soft_wrap=True)
    session_id = _session(actor, session, new=new)
    term = Term(con, actor, opts.config(), session_id, build_services)
    turn.history(term)
    while True:
        try:
            line = con.input(PROMPT).strip()
        except EOFError:
            return 0
        if line and not _line(term, line):
            return 0


def register(root: typer.Typer) -> None:
    """Add ``herness chat`` to the root app (U09-84 step 4)."""
    root.command("chat")(chat)

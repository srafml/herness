"""Chat rows of the ops store, owner impl 09 (U09-44 … U09-50, U09-107 … U09-109; R-08, R-09).

Tables from impl 02 migration 005; migration 090 adds the assistant-reply index and
``summary_through_message_id`` (R-11). Not-found is a return value; misuse raises
``SchemaViolation``, a busy store ``StoreBusy``. Content and summaries are never logged.
"""

from __future__ import annotations

import datetime
import enum
import re
import sqlite3
from collections.abc import Callable
from typing import Final, Literal, TypedDict, cast

from herness.core import time as clock
from herness.core.errors import SchemaViolation
from herness.core.ids import new_ulid
from herness.core.logging import get_logger

from . import core

type ChatRole = Literal["user", "assistant", "system"]
type ChatStatus = Literal["queued", "streaming", "done", "failed"]
type ChatVerified = Literal["verified", "partial", "unverified"]
type ChatFeedback = Literal["up", "down"]

_USER_REF_RE: Final = re.compile(r"[0-9a-f]{32}")
_MAX_CONTENT: Final = 20_000
_MAX_NOTE: Final = 1000
_MAX_QUERY_IDS: Final = 500
_MAX_SUMMARY: Final = 6000
_MAX_TITLE: Final = 60  # characters of the first user line kept as the session title


def _query_ids_ok(v: object) -> bool:
    return isinstance(v, list) and len(v) <= _MAX_QUERY_IDS and all(isinstance(q, str) for q in v)


# Fixed allowlist, SET order and validator of the columns update_chat_message sets (TH09-13).
_UPDATABLE: Final[dict[str, Callable[[object], bool]]] = {
    "status": lambda v: v in ("queued", "streaming", "done", "failed"),
    "content": lambda v: isinstance(v, str) and len(v) <= _MAX_CONTENT,
    "verified": lambda v: v in ("verified", "partial", "unverified", None),
    "run_id": lambda v: v is None or isinstance(v, str),
    "query_ids": _query_ids_ok,
    "meta": lambda v: isinstance(v, dict),
    "feedback": lambda v: v in ("up", "down", None),
    "feedback_note": lambda v: v is None or (isinstance(v, str) and len(v) <= _MAX_NOTE),
}
_MESSAGE_SQL: Final = "SELECT * FROM chat_message"
_REPLY_SQL: Final = (
    f"{_MESSAGE_SQL} WHERE session_id = ? AND role = 'assistant'"
    " AND json_extract(meta, '$.reply_to') = ?"
)
_SESSION_BY_ID: Final = "SELECT * FROM chat_session WHERE session_id = ?"
_IN_SESSION: Final = "SELECT 1 FROM chat_message WHERE message_id = ? AND session_id = ?"
_BY_ID: Final = f"{_MESSAGE_SQL} WHERE message_id = ?"

_log = get_logger("store.ops")


_Unset = enum.Enum("_Unset", "UNSET")  # sentinel type of update_chat_message defaults
_UNSET: Final = _Unset.UNSET


class ChatSessionRow(TypedDict):
    """One ``chat_session`` row (impl 02 §4.3.5 plus migration 090); times are aware UTC."""

    session_id: str
    user_ref: str
    title: str | None
    summary: str | None
    summary_through_message_id: str | None
    created_at: datetime.datetime
    last_active_at: datetime.datetime


class ChatMessageRow(TypedDict):
    """One ``chat_message`` row; ``query_ids`` and ``meta`` parsed from their JSON text."""

    message_id: str
    session_id: str
    role: ChatRole
    content: str
    status: ChatStatus
    verified: ChatVerified | None
    feedback: ChatFeedback | None
    feedback_note: str | None
    run_id: str | None
    query_ids: list[str]
    meta: dict[str, object]
    created_at: datetime.datetime


def _json_value[T: (list[object], dict[str, object])](
    text: object, kind: type[T], *, field: str, message_id: str
) -> T:
    """Parse a JSON column; anything but a ``kind`` value becomes empty with a WARNING."""
    try:
        value = core.load_json(str(text), field=field)
    except SchemaViolation:
        value = None
    if isinstance(value, kind):
        return value
    _log.warning("store.chat.bad_json", message_id=message_id, field=field)
    return kind()


def _session(row: sqlite3.Row) -> ChatSessionRow:
    times = {k: clock.parse_utc(row[k]) for k in ("created_at", "last_active_at")}
    return cast("ChatSessionRow", {**dict(row), **times})


def _message(row: sqlite3.Row) -> ChatMessageRow:
    message_id = str(row["message_id"])
    query_ids = _json_value(row["query_ids"], list, field="query_ids", message_id=message_id)
    meta = _json_value(row["meta"], dict, field="meta", message_id=message_id)
    created = clock.parse_utc(row["created_at"])
    parsed = {"query_ids": [str(q) for q in query_ids], "meta": meta, "created_at": created}
    return cast("ChatMessageRow", {**dict(row), **parsed})


def _exists(conn: sqlite3.Connection, sql: str, *params: object) -> bool:
    return conn.execute(sql, params).fetchone() is not None


def create_chat_session(user_ref: str, *, now: datetime.datetime) -> str:
    """Insert a session owned by ``user_ref`` (32 lower-case hex) and return ``ses_<ulid>``."""
    if _USER_REF_RE.fullmatch(user_ref) is None:
        msg = "chat user_ref must be 32 lower-case hex characters"
        raise SchemaViolation(msg)
    stamp = clock.format_utc(now)
    session_id = "ses_" + new_ulid()
    core.run_write(
        lambda conn: conn.execute(
            "INSERT INTO chat_session (session_id, user_ref, created_at, last_active_at)"
            " VALUES (?, ?, ?, ?)",
            (session_id, user_ref, stamp, stamp),
        ),
        op="chat_create_session",
    )
    return session_id


def append_chat_message(
    session_id: str, role: Literal["user", "system"], content: str, *, now: datetime.datetime
) -> str | None:
    """Insert a done user or system row and touch the session; None when it does not exist."""
    if cast("str", role) == "assistant":
        msg = "assistant rows are written only by ChatService"
        raise SchemaViolation(msg)
    if role not in ("user", "system"):
        msg = "chat role must be user or system"
        raise SchemaViolation(msg)
    if len(content) > _MAX_CONTENT:
        msg = f"chat content exceeds {_MAX_CONTENT} characters"
        raise SchemaViolation(msg)
    stamp = clock.format_utc(now)
    message_id = "msg_" + new_ulid()
    lines = content.strip().splitlines() if role == "user" else []
    title = lines[0].strip()[:_MAX_TITLE] if lines else None

    def write(conn: sqlite3.Connection) -> str | None:
        if not _exists(conn, _SESSION_BY_ID, session_id):
            return None
        conn.execute(
            "INSERT INTO chat_message (message_id, session_id, role, content, status,"
            " created_at) VALUES (?, ?, ?, ?, 'done', ?)",
            (message_id, session_id, role, content, stamp),
        )
        conn.execute(
            "UPDATE chat_session SET last_active_at = ?, title = coalesce(title, ?)"
            " WHERE session_id = ?",
            (stamp, title, session_id),
        )
        return message_id

    return core.run_write(write, op="chat_append_message")


def update_chat_message(  # noqa: PLR0913 - signature fixed by U09-46
    message_id: str,
    *,
    status: ChatStatus | _Unset = _UNSET,
    content: str | _Unset = _UNSET,
    verified: ChatVerified | _Unset | None = _UNSET,
    run_id: str | _Unset | None = _UNSET,
    query_ids: list[str] | _Unset = _UNSET,
    meta: dict[str, object] | _Unset = _UNSET,
    feedback: ChatFeedback | _Unset | None = _UNSET,
    feedback_note: str | _Unset | None = _UNSET,
) -> bool:
    """Set only the given columns (``meta`` shallow-merged); False when the row is missing."""
    fields = (status, content, verified, run_id, query_ids, meta, feedback, feedback_note)
    given = zip(_UPDATABLE, fields, strict=True)
    values: dict[str, object] = {col: val for col, val in given if val is not _UNSET}
    if not values:
        msg = "update_chat_message needs at least one field"
        raise SchemaViolation(msg)
    for col, val in values.items():
        if not _UPDATABLE[col](val):
            msg = f"invalid chat {col}: wrong type, value or size"
            raise SchemaViolation(msg)
    if "query_ids" in values:
        values["query_ids"] = core.dump_json(values["query_ids"], field="query_ids")

    def write(conn: sqlite3.Connection) -> bool:
        if "meta" in values:
            row = conn.execute(_BY_ID, (message_id,)).fetchone()
            if row is None:
                return False
            old = _json_value(row["meta"], dict, field="meta", message_id=message_id)
            new = cast("dict[str, object]", values["meta"])
            values["meta"] = core.dump_json({**old, **new}, field="meta")
        columns = [col for col in _UPDATABLE if col in values]
        assignments = ", ".join(f"{col} = ?" for col in columns)
        cursor = conn.execute(
            f"UPDATE chat_message SET {assignments} WHERE message_id = ?",  # noqa: S608 - allowlisted columns
            [*(values[col] for col in columns), message_id],
        )
        return cursor.rowcount == 1

    return core.run_write(write, op="chat_update_message")


def upsert_assistant_placeholder(
    session_id: str, *, reply_to: str, now: datetime.datetime | None = None
) -> ChatMessageRow | None:
    """The single assistant row answering user row ``reply_to``; None if not a user row here."""
    stamp = clock.format_utc(clock.now() if now is None else now)

    def write(conn: sqlite3.Connection) -> sqlite3.Row | None:
        if not _exists(conn, f"{_IN_SESSION} AND role = 'user'", reply_to, session_id):
            return None
        existing: sqlite3.Row | None = conn.execute(_REPLY_SQL, (session_id, reply_to)).fetchone()
        if existing is not None:
            return existing
        message_id = "msg_" + new_ulid()
        conn.execute(
            "INSERT INTO chat_message (message_id, session_id, role, content, status, meta,"
            " created_at) VALUES (?, ?, 'assistant', '', 'streaming', ?, ?)",
            (message_id, session_id, core.dump_json({"reply_to": reply_to}, field="meta"), stamp),
        )
        created: sqlite3.Row = conn.execute(_BY_ID, (message_id,)).fetchone()
        return created

    row = core.run_write(write, op="chat_upsert_placeholder")
    return None if row is None else _message(row)


def latest_user_message(session_id: str) -> ChatMessageRow | None:
    """The user row with the greatest ``(created_at, message_id)`` of the session, or None."""
    row = core.read_one(
        f"{_MESSAGE_SQL} WHERE session_id = ? AND role = 'user'"
        " ORDER BY created_at DESC, message_id DESC LIMIT 1",
        (session_id,),
    )
    return None if row is None else _message(row)


def get_chat_session(session_id: str) -> ChatSessionRow | None:
    """The session row, or None."""
    row = core.read_one(_SESSION_BY_ID, (session_id,))
    return None if row is None else _session(row)


def list_chat_sessions(user_ref: str, *, limit: int = 50) -> list[ChatSessionRow]:
    """The user's sessions, most recently active first; ``limit`` clipped to 1..200."""
    size = max(1, min(200, limit))
    rows = core.read_all(
        "SELECT * FROM chat_session WHERE user_ref = ?"
        " ORDER BY last_active_at DESC, session_id DESC LIMIT ?",
        (user_ref, size),
        max_rows=size,
    )
    return [_session(row) for row in rows]


def list_chat_messages(session_id: str, *, limit: int = 200) -> list[ChatMessageRow]:
    """The newest ``limit`` rows (clipped to 1..500) of the session, oldest first."""
    size = max(1, min(500, limit))
    rows = core.read_all(
        "SELECT * FROM (SELECT * FROM chat_message WHERE session_id = ?"
        " ORDER BY created_at DESC, message_id DESC LIMIT ?) ORDER BY created_at, message_id",
        (session_id, size),
        max_rows=size,
    )
    return [_message(row) for row in rows]


def get_chat_message(message_id: str) -> ChatMessageRow | None:
    """The message row, or None."""
    row = core.read_one(_BY_ID, (message_id,))
    return None if row is None else _message(row)


def find_assistant_message(session_id: str, reply_to_message_id: str) -> ChatMessageRow | None:
    """The assistant row of the session whose ``meta.reply_to`` is the given id, or None."""
    row = core.read_one(_REPLY_SQL, (session_id, reply_to_message_id))
    return None if row is None else _message(row)


def count_user_turns(session_id: str) -> int:
    """Number of user rows in the session; 0 for an unknown session."""
    row = core.read_one(
        "SELECT count(*) AS n FROM chat_message WHERE session_id = ? AND role = 'user'",
        (session_id,),
    )
    return 0 if row is None else int(row["n"])


def set_chat_summary(session_id: str, summary: str, *, through_message_id: str) -> None:
    """Store the rolling summary covering up to ``through_message_id``; never moves back."""
    if len(summary) > _MAX_SUMMARY:
        msg = "chat summary too long"
        raise SchemaViolation(msg)

    def write(conn: sqlite3.Connection) -> None:
        row = conn.execute(_SESSION_BY_ID, (session_id,)).fetchone()
        if row is None:
            _log.warning("store.chat.summary_skipped", session_id=session_id, reason="no_session")
            return
        if not _exists(conn, _IN_SESSION, through_message_id, session_id):
            msg = f"message {through_message_id} is not in session {session_id}"
            raise SchemaViolation(msg)
        stored: str | None = row["summary_through_message_id"]
        if stored == through_message_id:
            return
        if stored is not None and stored > through_message_id:
            _log.debug("store.chat.summary_skipped", session_id=session_id, reason="stale")
            return
        conn.execute(
            "UPDATE chat_session SET summary = ?, summary_through_message_id = ?"
            " WHERE session_id = ?",
            (summary, through_message_id, session_id),
        )

    core.run_write(write, op="chat_set_summary")


def purge_chat(before: datetime.datetime) -> tuple[int, int]:
    """Delete sessions inactive since before ``before`` and their messages; return counts."""
    stamp = clock.format_utc(before)

    def write(conn: sqlite3.Connection) -> tuple[int, int]:
        messages = conn.execute(
            "DELETE FROM chat_message WHERE session_id IN"
            " (SELECT session_id FROM chat_session WHERE last_active_at < ?)",
            (stamp,),
        ).rowcount
        sessions = conn.execute(
            "DELETE FROM chat_session WHERE last_active_at < ?", (stamp,)
        ).rowcount
        return sessions, messages

    sessions, messages = core.run_write(write, op="chat_purge")
    _log.info("store.chat.purged", sessions=sessions, messages=messages, before=stamp)
    return sessions, messages

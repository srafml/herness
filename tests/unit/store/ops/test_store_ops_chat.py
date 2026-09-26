"""Unit tests for herness.store.ops.chat (impl 09 U09-44 … U09-50, U09-107 … U09-109).

UT09-36 … UT09-42 and UT09-104 … UT09-106. Every test runs on a fresh ops store migrated
through 090_chat; rows are written through the chat functions or raw SQL for set-up.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from herness.core import time as clock
from herness.core.errors import SchemaViolation
from herness.store import ops
from herness.store.ops import chat, core
from herness.store.ops.migrate import migrate

pytestmark = pytest.mark.unit

_T0 = datetime.datetime(2026, 9, 26, 10, 0, tzinfo=datetime.UTC)
_USER = "0123456789abcdef" * 2
_OTHER_USER = "ab" * 16


@pytest.fixture(autouse=True)
def migrated(ops_store: Path) -> Path:
    migrate()
    return ops_store


def _at(minutes: int) -> datetime.datetime:
    return _T0 + datetime.timedelta(minutes=minutes)


def _write(sql: str, params: tuple[object, ...] = ()) -> None:
    core.run_write(lambda c: c.execute(sql, params), op="test_write")


def _raw_message(
    message_id: str, session_id: str, role: str, *, meta: str = "{}", minutes: int = 0
) -> None:
    _write(
        "INSERT INTO chat_message (message_id, session_id, role, content, status, meta,"
        " created_at) VALUES (?, ?, ?, 'x', 'done', ?, ?)",
        (message_id, session_id, role, meta, clock.format_utc(_at(minutes))),
    )


def _events(logs: list[dict[str, object]], name: str) -> list[dict[str, object]]:
    """Captured events called ``name``, without the logger's bound ``component``."""
    return [{k: v for k, v in e.items() if k != "component"} for e in logs if e["event"] == name]


def _session_row(session_id: str) -> dict[str, object]:
    row = core.read_one("SELECT * FROM chat_session WHERE session_id = ?", (session_id,))
    assert row is not None
    return dict(row)


def _message_row(message_id: str) -> dict[str, object]:
    row = core.read_one("SELECT * FROM chat_message WHERE message_id = ?", (message_id,))
    assert row is not None
    return dict(row)


def _user_message(session_id: str, text: str = "hello", minutes: int = 1) -> str:
    message_id = chat.append_chat_message(session_id, "user", text, now=_at(minutes))
    assert message_id is not None
    return message_id


# --- UT09-36 create_chat_session -------------------------------------------------------------


def test_ut09_36_create_inserts_session_with_null_title() -> None:
    """UT09-36 create returns `ses_<ulid>`; the row has NULL title and summary, both times now."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    assert session_id.startswith("ses_")
    assert len(session_id) == 4 + 26
    row = _session_row(session_id)
    assert row["title"] is None
    assert row["summary"] is None
    assert row["summary_through_message_id"] is None
    assert row["user_ref"] == _USER
    assert row["created_at"] == row["last_active_at"] == "2026-09-26T10:00:00.000000Z"
    assert chat.create_chat_session(_USER, now=_T0) != session_id


@pytest.mark.parametrize("user_ref", ["short", _USER.upper(), _USER + "0", "g" * 32])
def test_ut09_36_create_rejects_bad_user_ref(user_ref: str) -> None:
    """UT09-36 a user_ref that is not 32 lower-case hex characters raises SchemaViolation."""
    with pytest.raises(SchemaViolation):
        chat.create_chat_session(user_ref, now=_T0)
    assert core.read_one("SELECT count(*) AS n FROM chat_session")["n"] == 0  # type: ignore[index]


def test_ut09_36_create_rejects_naive_now() -> None:
    """UT09-36 a naive `now` raises SchemaViolation."""
    with pytest.raises(SchemaViolation):
        chat.create_chat_session(_USER, now=datetime.datetime(2026, 9, 26))  # noqa: DTZ001


# --- UT09-37 append_chat_message -------------------------------------------------------------


def test_ut09_37_assistant_role_refused() -> None:
    """UT09-37 role assistant is refused: only ChatService writes assistant rows."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    with pytest.raises(SchemaViolation, match="assistant rows are written only by ChatService"):
        chat.append_chat_message(session_id, "assistant", "hi", now=_T0)  # type: ignore[arg-type]
    with pytest.raises(SchemaViolation):
        chat.append_chat_message(session_id, "tool", "hi", now=_T0)  # type: ignore[arg-type]


def test_ut09_37_long_content_refused() -> None:
    """UT09-37 content over 20,000 characters is refused; exactly 20,000 is accepted."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    with pytest.raises(SchemaViolation):
        chat.append_chat_message(session_id, "user", "a" * 20_001, now=_T0)
    assert chat.append_chat_message(session_id, "user", "a" * 20_000, now=_T0) is not None


def test_ut09_37_first_user_row_sets_title() -> None:
    """UT09-37 the first user row sets title = first line, stripped, cut to 60 chars."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    first_line = "  " + "q" * 70 + "  "
    message_id = chat.append_chat_message(
        session_id, "user", f"\n{first_line}\nsecond line", now=_at(5)
    )
    assert message_id is not None
    assert message_id.startswith("msg_")
    session = _session_row(session_id)
    assert session["title"] == "q" * 60
    assert session["last_active_at"] == "2026-09-26T10:05:00.000000Z"
    row = _message_row(message_id)
    assert row["role"] == "user"
    assert row["status"] == "done"
    assert row["query_ids"] == "[]"
    assert row["meta"] == "{}"
    for column in ("verified", "feedback", "feedback_note", "run_id"):
        assert row[column] is None
    assert row["created_at"] == "2026-09-26T10:05:00.000000Z"
    _user_message(session_id, "another question", minutes=6)
    assert _session_row(session_id)["title"] == "q" * 60


def test_ut09_37_system_row_leaves_title_null() -> None:
    """UT09-37 a system row updates activity but never sets the title."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    assert chat.append_chat_message(session_id, "system", "note", now=_at(2)) is not None
    session = _session_row(session_id)
    assert session["title"] is None
    assert session["last_active_at"] == "2026-09-26T10:02:00.000000Z"


def test_ut09_37_blank_user_row_leaves_title_null() -> None:
    """UT09-37 a user row with only whitespace leaves the title NULL for a later row."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    _user_message(session_id, "   \n  ")
    assert _session_row(session_id)["title"] is None
    _user_message(session_id, "real question", minutes=2)
    assert _session_row(session_id)["title"] == "real question"


def test_ut09_37_missing_session_returns_none() -> None:
    """UT09-37 appending to an unknown session returns None and writes nothing."""
    assert chat.append_chat_message("ses_missing", "user", "hi", now=_T0) is None
    assert core.read_one("SELECT count(*) AS n FROM chat_message")["n"] == 0  # type: ignore[index]


# --- UT09-38 update_chat_message -------------------------------------------------------------


def test_ut09_38_feedback_only_leaves_other_columns() -> None:
    """UT09-38 updating feedback fields only changes those two columns."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    user_id = _user_message(session_id)
    placeholder = chat.upsert_assistant_placeholder(session_id, reply_to=user_id, now=_at(2))
    assert placeholder is not None
    message_id = placeholder["message_id"]
    assert chat.update_chat_message(
        message_id,
        status="done",
        content="answer",
        verified="partial",
        run_id="run_1",
        query_ids=["q1", "q2"],
    )
    before = _message_row(message_id)
    assert chat.update_chat_message(message_id, feedback="down", feedback_note="wrong number")
    after = _message_row(message_id)
    assert after["feedback"] == "down"
    assert after["feedback_note"] == "wrong number"
    changed = {k for k in after if after[k] != before[k]}
    assert changed == {"feedback", "feedback_note"}
    assert chat.update_chat_message(message_id, feedback=None, feedback_note=None, verified=None)
    cleared = _message_row(message_id)
    assert (cleared["feedback"], cleared["feedback_note"], cleared["verified"]) == (None,) * 3


def test_ut09_38_meta_merge_keeps_reply_to() -> None:
    """UT09-38 meta is shallow-merged: the stored `reply_to` survives a later meta update."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    user_id = _user_message(session_id)
    placeholder = chat.upsert_assistant_placeholder(session_id, reply_to=user_id, now=_at(2))
    assert placeholder is not None
    message_id = placeholder["message_id"]
    assert chat.update_chat_message(message_id, meta={"mode": "live", "latency_ms": 5})
    assert chat.update_chat_message(message_id, meta={"latency_ms": 7})
    row = chat.get_chat_message(message_id)
    assert row is not None
    assert row["meta"] == {"reply_to": user_id, "mode": "live", "latency_ms": 7}
    assert row["query_ids"] == []


def test_ut09_38_meta_merge_survives_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-38 a busy COMMIT makes run_write re-run the callback; meta still merges once."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    _raw_message("msg_retry", session_id, "assistant", meta='{"reply_to": "msg_u"}')
    real_connection = core.connection
    commits: list[str] = []

    class _BusyFirstCommit:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def __getattr__(self, name: str) -> object:
            return getattr(self._conn, name)

        def execute(self, sql: str, params: object = ()) -> sqlite3.Cursor:
            if sql == "COMMIT" and not commits:
                commits.append(sql)
                msg = "database is locked"
                raise sqlite3.OperationalError(msg)
            return self._conn.execute(sql, params)  # type: ignore[arg-type]

    monkeypatch.setattr(core, "connection", lambda: _BusyFirstCommit(real_connection()))
    assert chat.update_chat_message("msg_retry", status="done", meta={"mode": "live"})
    monkeypatch.undo()
    assert commits == ["COMMIT"]
    row = chat.get_chat_message("msg_retry")
    assert row is not None
    assert row["meta"] == {"reply_to": "msg_u", "mode": "live"}
    assert row["status"] == "done"


def test_ut09_38_missing_row_returns_false() -> None:
    """UT09-38 updating an unknown message returns False, with or without meta."""
    assert chat.update_chat_message("msg_missing", status="done") is False
    assert chat.update_chat_message("msg_missing", meta={"a": 1}) is False


def test_ut09_38_no_field_refused() -> None:
    """UT09-38 a call without any field raises SchemaViolation."""
    with pytest.raises(SchemaViolation):
        chat.update_chat_message("msg_x")


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "bogus"},
        {"status": None},
        {"verified": "yes"},
        {"feedback": "meh"},
        {"content": "a" * 20_001},
        {"content": None},
        {"feedback_note": "n" * 1001},
        {"run_id": 5},
        {"query_ids": ["q"] * 501},
        {"query_ids": "q1"},
        {"query_ids": [1]},
        {"meta": ["not", "a", "dict"]},
    ],
)
def test_ut09_38_invalid_values_refused(fields: dict[str, object]) -> None:
    """UT09-38 enum, cap and type violations raise SchemaViolation before any write."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    message_id = _user_message(session_id)
    before = _message_row(message_id)
    with pytest.raises(SchemaViolation):
        chat.update_chat_message(message_id, **fields)  # type: ignore[arg-type]
    assert _message_row(message_id) == before


def test_ut09_38_bad_stored_meta_is_replaced() -> None:
    """UT09-38 a stored meta that is not an object merges as {} and logs store.chat.bad_json."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    _raw_message("msg_bad", session_id, "assistant", meta="[1]")
    with capture_logs() as logs:
        assert chat.update_chat_message("msg_bad", meta={"mode": "live"})
    assert _message_row("msg_bad")["meta"] == '{"mode":"live"}'
    assert any(e["event"] == "store.chat.bad_json" for e in logs)


# --- UT09-39 upsert_assistant_placeholder ----------------------------------------------------


def test_ut09_39_twice_same_reply_to_gives_one_row() -> None:
    """UT09-39 two calls for the same `reply_to` return the same single assistant row."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    user_id = _user_message(session_id)
    first = chat.upsert_assistant_placeholder(session_id, reply_to=user_id, now=_at(2))
    second = chat.upsert_assistant_placeholder(session_id, reply_to=user_id)
    assert first is not None
    assert second == first
    assert first["role"] == "assistant"
    assert first["content"] == ""
    assert first["status"] == "streaming"
    assert first["meta"] == {"reply_to": user_id}
    assert first["created_at"] == _at(2)
    rows = core.read_all("SELECT message_id FROM chat_message WHERE role = 'assistant'")
    assert len(rows) == 1


def test_ut09_39_default_now_is_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-39 `now=None` stamps the row with the current clock."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    user_id = _user_message(session_id)
    stamp = datetime.datetime(2026, 9, 26, 11, 22, 33, 123456, tzinfo=datetime.UTC)
    monkeypatch.setattr(clock, "now", lambda: stamp)
    row = chat.upsert_assistant_placeholder(session_id, reply_to=user_id)
    assert row is not None
    assert row["created_at"] == stamp


def test_ut09_39_reply_to_must_be_user_row_of_session() -> None:
    """UT09-39 a `reply_to` that is not a user row of that session returns None."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    other_id = chat.create_chat_session(_USER, now=_T0)
    user_id = _user_message(session_id)
    system_id = chat.append_chat_message(session_id, "system", "sys", now=_at(2))
    assert system_id is not None
    assert chat.upsert_assistant_placeholder(other_id, reply_to=user_id) is None
    assert chat.upsert_assistant_placeholder(session_id, reply_to=system_id) is None
    assert chat.upsert_assistant_placeholder(session_id, reply_to="msg_missing") is None


def test_ut09_39_unique_index_blocks_duplicates() -> None:
    """UT09-39 the chat_message_reply index refuses a second assistant row per reply_to."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    _raw_message("msg_a1", session_id, "assistant", meta='{"reply_to":"msg_u"}')
    with pytest.raises(SchemaViolation):
        _raw_message("msg_a2", session_id, "assistant", meta='{"reply_to":"msg_u"}')


# --- UT09-40 latest_user_message --------------------------------------------------------------


def test_ut09_40_same_timestamp_greater_message_id_wins() -> None:
    """UT09-40 two user rows with the same timestamp: the greater message_id is returned."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    _raw_message("msg_01B", session_id, "user", minutes=3)
    _raw_message("msg_01A", session_id, "user", minutes=3)
    _raw_message("msg_01C", session_id, "assistant", minutes=4)
    _raw_message("msg_009", session_id, "user", minutes=1)
    row = chat.latest_user_message(session_id)
    assert row is not None
    assert row["message_id"] == "msg_01B"
    assert chat.latest_user_message("ses_missing") is None


# --- UT09-41 read functions -------------------------------------------------------------------


def test_ut09_41_list_messages_newest_200_ascending() -> None:
    """UT09-41 300 messages: the default list returns the newest 200 in ascending order."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    for i in range(300):
        _raw_message(f"msg_{i:04d}", session_id, "user", minutes=i)
    rows = chat.list_chat_messages(session_id)
    assert [r["message_id"] for r in rows] == [f"msg_{i:04d}" for i in range(100, 300)]
    assert rows[0]["created_at"] == _at(100)
    assert [r["message_id"] for r in chat.list_chat_messages(session_id, limit=0)] == ["msg_0299"]
    assert len(chat.list_chat_messages(session_id, limit=1000)) == 300


def test_ut09_41_list_sessions_ordered_by_activity() -> None:
    """UT09-41 sessions are listed newest activity first, only the user's, limit clipped."""
    older = chat.create_chat_session(_USER, now=_T0)
    newer = chat.create_chat_session(_USER, now=_T0)
    chat.create_chat_session(_OTHER_USER, now=_at(9))
    _user_message(older, minutes=5)
    _user_message(newer, minutes=3)
    rows = chat.list_chat_sessions(_USER)
    assert [r["session_id"] for r in rows] == [older, newer]
    assert rows[0]["title"] == "hello"
    assert rows[0]["last_active_at"] == _at(5)
    assert rows[0]["created_at"] == _T0
    assert [r["session_id"] for r in chat.list_chat_sessions(_USER, limit=-3)] == [older]
    for i in range(205):
        chat.create_chat_session(_USER, now=_at(i))
    assert len(chat.list_chat_sessions(_USER, limit=500)) == 200


def test_ut09_41_get_session_and_message() -> None:
    """UT09-41 PK lookups return the parsed row or None."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    message_id = _user_message(session_id)
    session = chat.get_chat_session(session_id)
    assert session is not None
    assert session["user_ref"] == _USER
    assert session["summary"] is None
    assert session["summary_through_message_id"] is None
    message = chat.get_chat_message(message_id)
    assert message is not None
    assert message["session_id"] == session_id
    assert message["content"] == "hello"
    assert message["verified"] is None
    assert chat.get_chat_session("ses_missing") is None
    assert chat.get_chat_message("msg_missing") is None


def test_ut09_41_bad_json_parsed_as_empty_with_warning() -> None:
    """UT09-41 non-list query_ids / non-object meta read as [] / {} with store.chat.bad_json."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    _write(
        "INSERT INTO chat_message (message_id, session_id, role, status, query_ids, meta,"
        " created_at) VALUES ('msg_bad', ?, 'user', 'done', '{}', '\"text\"', ?)",
        (session_id, "2026-09-26T10:00:00.000000Z"),
    )
    with capture_logs() as logs:
        row = chat.get_chat_message("msg_bad")
    assert row is not None
    assert (row["query_ids"], row["meta"]) == ([], {})
    bad = [e for e in logs if e["event"] == "store.chat.bad_json"]
    assert [e["message_id"] for e in bad] == ["msg_bad", "msg_bad"]
    assert all(e["log_level"] == "warning" for e in bad)
    assert all("text" not in str(e) for e in bad)


def test_ut09_41_invalid_json_text_parsed_as_empty() -> None:
    """UT09-41 JSON text that does not parse at all reads as [] / {} too."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    conn = core.connection()
    conn.execute("PRAGMA ignore_check_constraints = ON")
    try:
        _write(
            "INSERT INTO chat_message (message_id, session_id, role, status, query_ids, meta,"
            " created_at) VALUES ('msg_bad', ?, 'user', 'done', '[', '{', ?)",
            (session_id, "2026-09-26T10:00:00.000000Z"),
        )
    finally:
        conn.execute("PRAGMA ignore_check_constraints = OFF")
    row = chat.get_chat_message("msg_bad")
    assert row is not None
    assert (row["query_ids"], row["meta"]) == ([], {})


# --- UT09-42 purge_chat -----------------------------------------------------------------------


def test_ut09_42_purge_deletes_inactive_sessions_and_messages() -> None:
    """UT09-42 purge deletes only sessions inactive before `before` and their messages."""
    inactive = chat.create_chat_session(_USER, now=_T0)
    active = chat.create_chat_session(_USER, now=_T0)
    for i in range(3):
        _user_message(inactive, f"old {i}", minutes=i)
    _user_message(active, "old", minutes=1)
    _user_message(active, "new", minutes=60)
    with capture_logs() as logs:
        counts = chat.purge_chat(_at(30))
    assert counts == (1, 3)
    assert chat.get_chat_session(inactive) is None
    assert chat.get_chat_session(active) is not None
    assert len(chat.list_chat_messages(active)) == 2
    left = core.read_one("SELECT count(*) AS n FROM chat_message WHERE session_id = ?", (inactive,))
    assert left is not None
    assert left["n"] == 0
    assert _events(logs, "store.chat.purged") == [
        {
            "event": "store.chat.purged",
            "log_level": "info",
            "sessions": 1,
            "messages": 3,
            "before": "2026-09-26T10:30:00.000000Z",
        }
    ]
    assert chat.purge_chat(_at(30)) == (0, 0)


def test_ut09_42_purge_rejects_naive_before() -> None:
    """UT09-42 a naive `before` raises SchemaViolation."""
    with pytest.raises(SchemaViolation):
        chat.purge_chat(datetime.datetime(2026, 9, 26))  # noqa: DTZ001


# --- UT09-104 find_assistant_message ----------------------------------------------------------


def test_ut09_104_find_by_reply_to() -> None:
    """UT09-104 finds the row for msg_A; None for msg_B; another session's row is not returned."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    other_id = chat.create_chat_session(_USER, now=_T0)
    _raw_message("msg_A", session_id, "user", minutes=1)
    _raw_message("msg_B", session_id, "user", minutes=2)
    _raw_message("msg_R", session_id, "assistant", meta='{"reply_to":"msg_A"}', minutes=3)
    _raw_message("msg_X", other_id, "assistant", meta='{"reply_to":"msg_B"}', minutes=3)
    found = chat.find_assistant_message(session_id, "msg_A")
    assert found is not None
    assert found["message_id"] == "msg_R"
    assert found["meta"] == {"reply_to": "msg_A"}
    assert chat.find_assistant_message(session_id, "msg_B") is None
    assert chat.find_assistant_message(other_id, "msg_A") is None


# --- UT09-105 count_user_turns ----------------------------------------------------------------


def test_ut09_105_counts_user_rows_only() -> None:
    """UT09-105 3 user, 2 assistant and 1 system rows count 3; an unknown session counts 0."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    for i, role in enumerate(["user", "assistant", "user", "assistant", "system", "user"]):
        _raw_message(f"msg_{i}", session_id, role, meta=f'{{"reply_to":"msg_u{i}"}}', minutes=i)
    assert chat.count_user_turns(session_id) == 3
    assert chat.count_user_turns("ses_missing") == 0


# --- UT09-106 set_chat_summary ----------------------------------------------------------------


def _summary_state(session_id: str) -> tuple[object, object, object]:
    row = _session_row(session_id)
    return row["summary"], row["summary_through_message_id"], row["last_active_at"]


def test_ut09_106_set_repeat_stale() -> None:
    """UT09-106 set with msg_2 stores; repeat is unchanged; older msg_1 is ignored as stale."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    _raw_message("msg_1", session_id, "user", minutes=1)
    _raw_message("msg_2", session_id, "user", minutes=2)
    chat.set_chat_summary(session_id, "summary two", through_message_id="msg_2")
    assert _summary_state(session_id) == ("summary two", "msg_2", "2026-09-26T10:00:00.000000Z")
    chat.set_chat_summary(session_id, "other text", through_message_id="msg_2")
    assert _summary_state(session_id)[:2] == ("summary two", "msg_2")
    with capture_logs() as logs:
        chat.set_chat_summary(session_id, "summary one", through_message_id="msg_1")
    assert _summary_state(session_id)[:2] == ("summary two", "msg_2")
    skipped = _events(logs, "store.chat.summary_skipped")
    assert [(e["log_level"], e["reason"]) for e in skipped] == [("debug", "stale")]
    assert all("summary one" not in str(e) for e in logs)


def test_ut09_106_first_summary_and_forward_move() -> None:
    """UT09-106 a newer message id moves the summary forward."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    _raw_message("msg_1", session_id, "user", minutes=1)
    _raw_message("msg_2", session_id, "user", minutes=2)
    chat.set_chat_summary(session_id, "s1", through_message_id="msg_1")
    chat.set_chat_summary(session_id, "s2", through_message_id="msg_2")
    assert _summary_state(session_id)[:2] == ("s2", "msg_2")


def test_ut09_106_message_of_another_session_refused() -> None:
    """UT09-106 a message id of another session raises SchemaViolation naming both ids."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    other_id = chat.create_chat_session(_USER, now=_T0)
    _raw_message("msg_9", other_id, "user")
    with pytest.raises(SchemaViolation, match=f"message msg_9 is not in session {session_id}"):
        chat.set_chat_summary(session_id, "s", through_message_id="msg_9")
    assert _summary_state(session_id)[:2] == (None, None)


def test_ut09_106_too_long_refused() -> None:
    """UT09-106 a summary of 6,001 characters raises SchemaViolation; 6,000 is stored."""
    session_id = chat.create_chat_session(_USER, now=_T0)
    _raw_message("msg_1", session_id, "user")
    with pytest.raises(SchemaViolation, match="chat summary too long"):
        chat.set_chat_summary(session_id, "s" * 6001, through_message_id="msg_1")
    chat.set_chat_summary(session_id, "s" * 6000, through_message_id="msg_1")
    assert _summary_state(session_id)[1] == "msg_1"


def test_ut09_106_missing_session_skipped_with_warning() -> None:
    """UT09-106 an unknown session changes nothing and logs summary_skipped (no_session)."""
    with capture_logs() as logs:
        chat.set_chat_summary("ses_missing", "s", through_message_id="msg_1")
    assert _events(logs, "store.chat.summary_skipped") == [
        {
            "event": "store.chat.summary_skipped",
            "log_level": "warning",
            "session_id": "ses_missing",
            "reason": "no_session",
        }
    ]


# --- package re-export ------------------------------------------------------------------------


def test_ut09_36_package_reexports_chat_names() -> None:
    """UT09-36 herness.store.ops re-exports every chat name as the same object."""
    names = [
        "create_chat_session",
        "append_chat_message",
        "update_chat_message",
        "upsert_assistant_placeholder",
        "latest_user_message",
        "get_chat_session",
        "list_chat_sessions",
        "list_chat_messages",
        "get_chat_message",
        "find_assistant_message",
        "count_user_turns",
        "set_chat_summary",
        "purge_chat",
        "ChatSessionRow",
        "ChatMessageRow",
    ]
    for name in names:
        assert getattr(ops, name) is getattr(chat, name), name
        assert name in ops.__all__

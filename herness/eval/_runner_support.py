"""Support of a golden eval run: chat sessions, the name index, traces and evidence re-runs.

Private sibling of `herness.eval.runner` (§2 module map), split from `_runner_question`
for the 400-line limit. `open_session_for` is the EvalDeps-derived `open_session` hook of
`invoke_chat` (U11-62 amendment): it stores only the redacted question. `EvidenceReader`
backs `count_unsupported` (U11-60, DD11-09): ops `evidence`, else the build's
`meta.evidence`, re-run read-only through the SQL guard.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import duckdb

from herness.core.config import get_config
from herness.core.errors import ConfigError, FatalError, HernessError, NotFound
from herness.core.numbers import compile_allowed_patterns
from herness.core.redact import redact_text
from herness.core.resilience import retry_call
from herness.eval.golden import reference_guard
from herness.eval.invoke import EVAL_USER_REF
from herness.store.layout import DataLayout
from herness.store.ops import get_evidence
from herness.store.ops.chat import append_chat_message, create_chat_session
from herness.store.warehouse import read_current

if TYPE_CHECKING:
    from datetime import datetime

__all__ = ["EVAL_USER_REF", "EvidenceReader", "Trace", "current_build", "dataset_kind",
           "load_trace", "name_index", "numerals", "open_session_for"]  # fmt: skip

RERUN_MAX_ROWS: Final = 10_000
_META_SQL: Final = "SELECT sql, params FROM meta.evidence WHERE query_id = ?"
_NAME_SQL: Final = (  # chat_entity_ids name index (U11-57): display name -> entity id
    "SELECT name, team_id FROM core.team",
    "SELECT name, service_id FROM core.service",
    "SELECT name, org_id FROM core.org",
    "SELECT key, record_id FROM core.work_item",
)

type Trace = list[dict[str, object]]


def open_session_for(now: Callable[[], datetime]) -> Callable[[str], str]:
    """The `invoke_chat` hook: a `ses_` session of the eval user holding the redacted
    question as its user message (U11-62 amendment); redaction failure fails closed."""

    def open_session(text: str) -> str:
        redacted = redact_text(text)
        if redacted is None:
            msg = "eval question could not be redacted"
            raise FatalError(msg)
        session_id = retry_call("sqlite_write", create_chat_session, EVAL_USER_REF, now=now())
        stored = retry_call(
            "sqlite_write", append_chat_message, session_id, "user", redacted, now=now()
        )
        if stored is None:
            msg = "eval chat session vanished"
            raise NotFound(msg)
        return session_id

    return open_session


def name_index(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """Lower-cased display names (team, service, org, work-item key) -> entity id."""
    out: dict[str, str] = {}
    cur = con.cursor()
    try:
        for sql in _NAME_SQL:
            try:
                rows = cur.execute(sql).fetchall()
            except duckdb.Error:  # the build has no such table
                continue
            for name, ident in rows:
                if name and ident:
                    out.setdefault(str(name).lower(), str(ident))
    finally:
        cur.close()
    return out


def load_trace(path: Path) -> Trace | None:
    """The JSON object lines of a trace file (unreadable lines skipped); None when missing."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    events: Trace = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _bind(params: object) -> dict[str, object]:
    """The bind values of recorded params (impl 04 `{"bind", "template"}` or plain)."""
    if isinstance(params, str):
        params = json.loads(params)
    if isinstance(params, dict):
        if set(params) == {"bind", "template"} and isinstance(params["bind"], dict):
            return dict(params["bind"])
        return dict(params)
    return {}


class EvidenceReader:
    """`count_unsupported` evidence lookup and re-run on the pinned build (DD11-09)."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self._con = con

    def _meta(self, query_id: str) -> tuple[str, dict[str, object]] | None:
        cur = self._con.cursor()
        try:
            row = cur.execute(_META_SQL, [query_id]).fetchone()
        except duckdb.Error:  # no meta.evidence in this build
            return None
        finally:
            cur.close()
        return None if row is None else (str(row[0]), _bind(row[1]))

    def exists(self, query_id: str, build_id: str) -> bool:
        """Ops `evidence` holds the query for `build_id`, else the build's `meta.evidence`."""
        found = get_evidence(query_id)
        if found is not None:
            return found.build_id == build_id
        return self._meta(query_id) is not None

    def rerun(self, query_id: str) -> Sequence[Mapping[str, object]]:
        """The recorded SQL re-run read-only through the SQL guard; raises on any failure."""
        found = get_evidence(query_id)
        stored = (found.sql, _bind(found.params)) if found is not None else self._meta(query_id)
        if stored is None:
            msg = "no evidence to re-run"
            raise NotFound(msg)
        sql, params = stored
        cur = self._con.cursor()
        try:
            reference_guard(cur).check(sql)
            cur.execute(sql, params or None)
            columns = [str(d[0]) for d in cur.description or ()]
            rows = cur.fetchmany(RERUN_MAX_ROWS + 1)
        finally:
            cur.close()
        if len(rows) > RERUN_MAX_ROWS:
            msg = "re-run result too large"
            raise NotFound(msg)
        return [dict(zip(columns, row, strict=True)) for row in rows]


def current_build(data_root: Path) -> str:
    """The `CURRENT` build of `data_root`, pinned for the run (F11-07 step 2)."""
    try:
        build_id = read_current(layout=DataLayout.from_root(data_root))
    except HernessError as exc:
        msg = "cannot read the current build"
        raise ConfigError(msg, hint=type(exc).__name__) from exc
    if build_id is None:
        msg = "no current build"
        raise ConfigError(msg)
    return build_id


def dataset_kind(con: duckdb.DuckDBPyConnection) -> Literal["synthetic", "real"]:
    """`meta.build.dataset_kind` of the pinned build."""
    cur = con.cursor()
    try:
        row = cur.execute("SELECT dataset_kind FROM meta.build").fetchone()
    except duckdb.Error as exc:
        msg = "the build has no meta.build row"
        raise ConfigError(msg) from exc
    finally:
        cur.close()
    kind = None if row is None else row[0]
    if kind not in ("synthetic", "real"):
        msg = "the build's dataset_kind is unknown"
        raise ConfigError(msg)
    return "synthetic" if kind == "synthetic" else "real"


def numerals() -> tuple[tuple[re.Pattern[str], ...], float]:
    """Allowed numeral patterns (`reports.allowed_numeral_patterns`) and the Verifier's
    float tolerance for `count_unsupported` (U11-60)."""
    cfg = get_config()
    allowed = compile_allowed_patterns(list(cfg.app.reports.allowed_numeral_patterns))
    return allowed, cfg.models.harness.verifier.float_rel_tol

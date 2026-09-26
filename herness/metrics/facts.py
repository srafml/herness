"""Stage 400 hook: split `400_facts.sql` and materialize the fact tables (impl 04 U04-46, U04-47).

The spec 02 build runner calls `materialize_facts`; each statement runs through
`run_recorded(producer="facts")`, so every fact row carries a `meta.evidence` query_id."""

import contextlib
import importlib.resources
import re
import time
from collections.abc import Mapping
from typing import Final

import duckdb

from herness.core.config import get_config
from herness.core.errors import ConfigError, QueryError, SchemaViolation
from herness.core.logging import get_logger
from herness.metrics.catalog import catalog_from_config
from herness.metrics.evidence import IntoSpec, run_recorded
from herness.metrics.render import RenderState, default_binds, make_environment, weight_binds

from herness.metrics.render import _guard, _macros, _pick  # isort: skip - render as U04-39

__all__ = ["FACT_TABLES", "materialize_facts", "split_statements"]

FACT_TABLES: Final[tuple[str, ...]] = (
    *("metrics.org_closure", "metrics.work_item_closure", "metrics.incident_fact"),
    *("metrics.change_fact", "metrics.work_item_fact"),
)
_INPUT_TABLES: Final[tuple[str, ...]] = (
    *("core.org", "core.work_item", "core.incident", "core.team", "core.service", "core.change"),
    *("core.work_item_transition", "core.work_item_link", "enrich.cluster_member"),
    *("enrich.incident_change_link", "meta.build", "meta.evidence"),
)
_MARKER: Final = re.compile(r"^-- @statement (metrics\.[a-z_]+)\s*$")
_JINJA_COMMENT: Final = re.compile(r"\{#.*?#\}", re.DOTALL)
_TABLES_SQL: Final = (
    "SELECT table_schema || '.' || table_name FROM information_schema.tables"
    " WHERE table_catalog = current_database()"
)
_log: Final = get_logger("metrics")


def _problem(prefix: list[str], segments: list[tuple[str, str]], malformed: bool) -> str | None:
    """The first U04-46 violation of a split stage file, or None."""
    if malformed:
        return "malformed statement marker"
    if _JINJA_COMMENT.sub("", "\n".join(prefix)).strip():
        return "only whitespace and Jinja comments may precede the first statement"
    if any(not body.strip() for _, body in segments):
        return "empty statement body"
    if tuple(table for table, _ in segments) != FACT_TABLES:
        return "statements must be exactly " + ", ".join(FACT_TABLES) + " in order"
    return None


def split_statements(text: str, /) -> list[tuple[str, str]]:
    """(table, SELECT template) segments of the stage file in file order (U04-46).

    Raises ConfigError("400_facts.sql: <reason>") for text other than whitespace or Jinja
    comments before the first marker, a malformed marker, a blank body, or a table list
    other than the fact tables in order.
    """
    prefix: list[str] = []
    bodies: list[tuple[str, list[str]]] = []
    malformed = False
    for line in text.splitlines():
        match = _MARKER.match(line)
        if match is not None:
            bodies.append((match.group(1), []))
        else:
            malformed = malformed or line.startswith("-- @statement")
            (bodies[-1][1] if bodies else prefix).append(line)
    segments = [(table, "\n".join(body)) for table, body in bodies]
    reason = _problem(prefix, segments, malformed)
    if reason is not None:
        msg = f"400_facts.sql: {reason}"
        raise ConfigError(msg)
    return segments


def _render(body: str, candidates: Mapping[str, object]) -> tuple[str, dict[str, object]]:
    """Render one body as U04-39 with context `{}`: the SQL and the binds it used."""
    env = make_environment()
    rs = RenderState()
    ctx: dict[str, object] = {"filters": {}, "rs": rs}
    macros = _guard(lambda: _macros(env, ctx))
    sql = _guard(lambda: env.from_string(body).render({**ctx, **macros}))
    return sql, _pick(rs.used, candidates)


def _check_inputs(con: duckdb.DuckDBPyConnection) -> None:
    present = {str(row[0]) for row in con.execute(_TABLES_SQL).fetchall()}
    for table in _INPUT_TABLES:
        if table not in present:
            msg = f"stage 400 input {table} missing"
            raise SchemaViolation(msg)


def _stage_text() -> str:
    path = importlib.resources.files("herness.model").joinpath("sql/400_facts.sql")
    return path.read_text(encoding="utf-8")


def _materialize(
    con: duckdb.DuckDBPyConnection,
    table: str,
    body: str,
    candidates: Mapping[str, object],
    build_id: str,
) -> str:
    start = time.perf_counter()
    sql, bind = _render(body, candidates)
    params = {"bind": bind, "template": {"name": "400_facts", "statement": table}}
    into = IntoSpec(table, "replace", "query_id")
    try:
        rq = run_recorded(con, sql, params, "facts", build_id=build_id, into=into)
    except QueryError as err:
        msg = f"400_facts.sql statement {table} failed: {err.message}"
        raise SchemaViolation(msg) from err
    _log.info(
        "metrics.facts.materialized",
        build_id=build_id,
        table=table,
        row_count=rq.row_count,
        query_id=rq.query_id,
        duration_ms=int((time.perf_counter() - start) * 1000),
    )
    return rq.query_id


def materialize_facts(con: duckdb.DuckDBPyConnection, build_id: str, /) -> list[str]:
    """Materialize the five fact tables in one transaction; their query IDs in `FACT_TABLES`
    order (U04-47). Raises SchemaViolation (missing input, failed statement after ROLLBACK)
    and ConfigError (stage file, template).
    """
    _check_inputs(con)
    segments = split_statements(_stage_text())
    candidates = {**default_binds(catalog_from_config()), **weight_binds(get_config().weights)}
    con.execute("BEGIN TRANSACTION")
    try:
        query_ids = [_materialize(con, t, body, candidates, build_id) for t, body in segments]
    except BaseException:
        with contextlib.suppress(duckdb.Error):  # keep the original error, not the rollback's
            con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")
    return query_ids

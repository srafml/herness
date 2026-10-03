"""Peer groups for spec 07 outcome control groups (impl 04 U04-61, U04-62; design 04 §3.1).

One recorded SELECT of `sql/peer_group.sql.j2` (U04-63) resolves the key, the fallback and the
members, with the rules of `score.org` for teams and orgs (design 04 §5.8 step 2) and §5.8.1
for services and work items. The entity id and the metric name are typed bind parameters,
never rendered (TH04-01); thresholds come from the catalog binds. Python only validates,
renders and copies columns by name.
"""

import contextlib
import datetime
from collections.abc import Callable, Iterator
from typing import Final, Literal, Self, get_args

import duckdb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from herness.core.config import get_config
from herness.core.errors import SchemaViolation, ToolInputError
from herness.core.logging import get_logger
from herness.metrics.catalog import MetricCatalog, catalog_from_config
from herness.metrics.evidence import RecordedQuery, run_recorded
from herness.metrics.render import default_binds, render_named
from herness.metrics.windows import default_window, resolve_as_of
from herness.store.warehouse import open_readonly

__all__ = ["PeerGroupInfo", "peer_group"]

type _PeerEntityType = Literal["team", "org", "service", "work_item"]

_log: Final = get_logger("metrics")

_MAX_ID: Final = 256
_SCORED: Final = frozenset({"team", "org"})
_BUILD_SQL: Final = "SELECT build_id, started_at FROM meta.build"


class PeerGroupInfo(BaseModel):
    """Result of `peer_group` (U04-61): members sorted, the entity itself excluded."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    key: str
    member_ids: list[str]
    size: int = Field(ge=0)
    fallback: Literal["all", "prior_year"] | None
    query_id: str

    @model_validator(mode="after")
    def _size(self) -> Self:
        if self.size != len(self.member_ids):
            msg = "size must equal len(member_ids)"
            raise ValueError(msg)
        return self


def _validate(
    catalog: MetricCatalog, entity_type: str, entity_id: object, metric: str | None
) -> None:
    """U04-62 preconditions: allowlisted type, printable id, enabled metric of the grains."""
    if entity_type not in get_args(_PeerEntityType.__value__):
        msg = f"bad entity type {str(entity_type)[:32]}; allowed: team, org, service, work_item"
        raise ToolInputError(msg)
    if not isinstance(entity_id, str) or not 0 < len(entity_id) <= _MAX_ID:
        msg = f"entity_id must be 1-{_MAX_ID} characters"
        raise ToolInputError(msg)
    if not entity_id.isprintable():
        msg = "entity_id must be printable"
        raise ToolInputError(msg)
    if metric is None:
        return
    found = catalog.get(metric)
    if not found.enabled:
        msg = f"metric {found.name} is disabled"
        raise ToolInputError(msg)
    if entity_type in _SCORED and not set(found.grains) >= _SCORED:
        msg = f"metric {found.name} needs grains team and org for a {entity_type} peer group"
        raise ToolInputError(msg)


@contextlib.contextmanager
def _connection(con: duckdb.DuckDBPyConnection | None) -> Iterator[duckdb.DuckDBPyConnection]:
    """`con` as given (never closed here), else a read-only CURRENT warehouse (as U04-52)."""
    if con is not None:
        yield con
        return
    opened = open_readonly(None)
    try:
        yield opened
    finally:
        opened.close()


def _read_build(con: duckdb.DuckDBPyConnection) -> tuple[str, datetime.datetime]:
    """`build_id` and `started_at` of the one `meta.build` row (as U04-52)."""
    msg = "meta.build must hold one row"
    try:
        rows = con.execute(_BUILD_SQL).fetchall()
    except duckdb.Error as err:
        raise SchemaViolation(msg) from err
    build_id, started_at = rows[0] if len(rows) == 1 else (None, None)
    if not isinstance(build_id, str) or not isinstance(started_at, datetime.datetime):
        raise SchemaViolation(msg)
    return build_id, started_at


def _info(rq: RecordedQuery, entity_type: str, entity_id: str) -> PeerGroupInfo:
    """Copy the U04-62 step 6 columns by name; the query always returns at least one row."""
    index = {name: i for i, (name, _) in enumerate(rq.columns)}
    rows = rq.rows or []
    first = rows[0]
    if not first[index["entity_found"]]:
        msg = f"unknown {entity_type} {entity_id}"
        raise ToolInputError(msg)
    members = [r[index["member_id"]] for r in rows if r[index["member_id"]] is not None]
    return PeerGroupInfo.model_validate(
        {
            "key": first[index["key"]],
            "member_ids": members,
            "size": len(members),
            "fallback": first[index["fallback"]],
            "query_id": rq.query_id,
        }
    )


def peer_group(
    entity_type: _PeerEntityType,
    entity_id: str,
    /,
    *,
    metric: str | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
    on_evidence: Callable[[RecordedQuery], None] | None = None,
) -> PeerGroupInfo:
    """Control group of one entity from one recorded read-only query (U04-62).

    For a team or org the key equals `score.org.peer_group` for that entity and metric on the
    same build. Raises ToolInputError (request, unknown entity), QueryError and StoreBusy.
    """
    catalog = catalog_from_config()
    _validate(catalog, entity_type, entity_id, metric)
    tz = get_config().weights.business_timezone
    with _connection(con) as c:
        build_id, started_at = _read_build(c)
        as_of = resolve_as_of(started_at, tz, catalog.scoring.as_of)
        counts = {str(k): v for k, v in catalog.defaults.windows.items()}
        window = default_window("t12w", as_of, tz, counts)
        candidates: dict[str, object] = {
            **default_binds(catalog),
            **window.binds(),
            "pg_entity_id": entity_id,
            "pg_metric": metric,
        }
        context = {"entity_type": entity_type, "has_metric": metric is not None}
        rendered = render_named("peer_group", context, candidates)
        params = {"bind": rendered.bind, "template": rendered.template}
        timeout = catalog.defaults.compute_timeout_s
        rq = run_recorded(c, rendered.sql, params, None, build_id=build_id, timeout_s=timeout)
    info = _info(rq, entity_type, entity_id)
    if on_evidence is not None:
        on_evidence(rq)
    _log.debug(
        "metrics.peer_group.resolved",
        entity_type=entity_type,
        key=info.key,
        size=info.size,
        fallback=info.fallback,
        query_id=info.query_id,
    )
    return info

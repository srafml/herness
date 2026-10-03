"""Composition helpers of the `MemoryStore` facade (impl 07 U07-97, T07-23; private).

The adapters the facade hands to the units: the spec 05 SQL guard over the `CURRENT` build
(T07-19 note), the compactor's scratchpad ops (R-21), the stand-in model registry for chat
without an `LLMRegistry`, the dict-draft conversion of `write_recommendations` (U07-07 errors
row), the run-id log binding (`memory.feedback.degraded`), relatedness on the `CURRENT` build
and the current config hash. Size-forced sibling of `_facade.py` (impl 07 §2 row).
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Final

import duckdb
from pydantic import JsonValue, ValidationError

from herness.core.config import config_hash, get_config
from herness.core.errors import ConfigError, HernessError, ReportContractError
from herness.core.ids import IdKind, is_valid_id
from herness.core.jobs.tasks import save_checkpoint
from herness.core.logging import bind_ids
from herness.core.types import RecommendationDraft
from herness.harness.llm.base import LLMClient
from herness.harness.llm.settings import ClientConfig
from herness.harness.memory.recall import RelatednessCache
from herness.harness.sql_guard import ALLOWED_SCHEMAS, GuardedQuery, SqlGuard
from herness.store import warehouse
from herness.store.ops.memory import get_task_scratchpad

__all__ = [
    "CurrentGuard", "NoModels", "ScratchpadOps", "current_config_hash", "drafts", "related",
    "run_bound",
]  # fmt: skip

_SCHEMA_SQL: Final = (
    "SELECT lower(table_schema), lower(table_name), lower(column_name), lower(data_type)"
    " FROM information_schema.columns WHERE list_contains(?, lower(table_schema))"
)
_FIELD_MAX: Final = 40  # a field name in a draft error


def _blocked_columns() -> Sequence[str]:
    return get_config().models.harness.sql.blocked_columns


def _schema(build_id: str, open_build: Callable[[str], duckdb.DuckDBPyConnection]
            ) -> dict[str, dict[str, dict[str, str]]]:  # fmt: skip
    con = open_build(build_id)
    try:
        rows = con.execute(_SCHEMA_SQL, [sorted(ALLOWED_SCHEMAS)]).fetchall()
    finally:
        con.close()
    schema: dict[str, dict[str, dict[str, str]]] = {}  # no empty schema: sqlglot needs depth
    for db, table, column, kind in rows:
        schema.setdefault(db, {}).setdefault(table, {})[column] = kind
    return schema


class CurrentGuard(SqlGuard):
    """The spec 05 `SqlGuard` over the `CURRENT` build's schemas (empty schemas dropped).

    Rebuilt when `CURRENT` moves, so a template that names a newly promoted table passes; with
    no promoted build (or an unreadable one) it allows nothing. Lock-protected cache."""

    def __init__(
        self,
        *,
        blocked: Callable[[], Sequence[str]] = _blocked_columns,
        read_current: Callable[[], str | None] = warehouse.read_current,
        open_build: Callable[[str], duckdb.DuckDBPyConnection] = warehouse.open_readonly,
    ) -> None:
        super().__init__({}, ())
        self._cur_blocked, self._cur_read, self._cur_open = blocked, read_current, open_build
        self._cur_lock = threading.Lock()
        self._cur_guard: tuple[str | None, SqlGuard] | None = None

    def check(self, sql: str, *, allow_catalog: bool = False) -> GuardedQuery:
        """`SqlGuard.check` against the current build's schema."""
        return self.current().check(sql, allow_catalog=allow_catalog)

    def current(self) -> SqlGuard:
        """The guard of the `CURRENT` build, built once per build id."""
        try:
            build = self._cur_read()
        except (HernessError, OSError):
            build = None
        with self._cur_lock:
            if self._cur_guard is not None and self._cur_guard[0] == build:
                return self._cur_guard[1]
        try:
            schema = {} if build is None else _schema(build, self._cur_open)
        except (HernessError, OSError, duckdb.Error):
            schema, build = {}, None  # an unreadable build allows nothing; retried next call
        guard = SqlGuard(schema, self._cur_blocked())
        with self._cur_lock:
            self._cur_guard = (build, guard)
        return guard


class ScratchpadOps:
    """`CompactorOps` (U07-76): U07-29 reads and spec 08 `save_checkpoint` "scratchpad" (R-21)."""

    def get_task_scratchpad(self, task_id: str) -> str | None:
        """The task checkpoint's `scratchpad` JSON, else None (U07-29)."""
        return get_task_scratchpad(task_id)

    def save_scratchpad(self, task_id: str, value: dict[str, JsonValue]) -> None:
        """Replace the task checkpoint's `scratchpad` key (T08-16)."""
        save_checkpoint(task_id, "scratchpad", value)


class NoModels:
    """`ChatModels` when the store has no `LLMRegistry`: every model call is a `ConfigError`."""

    def _none(self) -> ConfigError:
        return ConfigError("memory chat needs an LLM registry")

    def model_for(self, model_role: str, depth: str) -> str:
        """Raise ConfigError."""
        raise self._none()

    def client(self, name: str) -> LLMClient:
        """Raise ConfigError."""
        raise self._none()

    def config(self, name: str) -> ClientConfig:
        """Raise ConfigError."""
        raise self._none()


def drafts(
    recs: Sequence[RecommendationDraft | Mapping[str, JsonValue]],
) -> list[RecommendationDraft]:
    """Drafts as `RecommendationDraft`; an invalid dict is a `ReportContractError` naming its
    rank and first invalid field, never a value (U07-07 errors row)."""
    out: list[RecommendationDraft] = []
    for index, rec in enumerate(recs, start=1):
        if isinstance(rec, RecommendationDraft):
            out.append(rec)
            continue
        try:
            out.append(RecommendationDraft.model_validate(dict(rec)))
        except ValidationError as exc:
            rank = rec.get("rank")
            shown = rank if isinstance(rank, int) and not isinstance(rank, bool) else index
            loc = exc.errors()[0]["loc"]
            field = str(loc[0])[:_FIELD_MAX] if loc else "draft"
            msg = f"recommendation rank {shown} invalid: {field}"
            raise ReportContractError(msg) from None
    return out


def run_bound(run_id: str) -> contextlib.AbstractContextManager[None]:
    """Bind `run_id` to every log line of the block (e.g. `memory.feedback.degraded`); an
    invalid id is left to the unit's own check."""
    return bind_ids(run_id=run_id) if is_valid_id(IdKind.RUN, run_id) else contextlib.nullcontext()


def related(cache: RelatednessCache | None, a: str, b: str) -> bool:
    """U07-61 relatedness of two target ids on the `CURRENT` build; False without one."""
    if cache is None:
        return False
    try:
        build = warehouse.read_current()
    except (HernessError, OSError):
        return False
    return build is not None and cache.related(build, a, b)


def current_config_hash() -> str:
    """`config_hash` of the loaded config (U10-11), for the LoRA manifest."""
    return config_hash(get_config())

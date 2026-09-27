"""`sync`/`reconcile` job handlers and the `herness sync` payload builder (impl 01 U01-51
to U01-54; R-39, R-42, R-63). Payloads are validated before any connector is built; connectors
come only from `build_connector` (the registry); logs and results name error classes only.
"""

from __future__ import annotations

import datetime
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Annotated, Final, Literal, NamedTuple, NoReturn, Self, cast

import pydantic
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, JsonValue, ValidationError

from herness.connectors.base import Connector
from herness.connectors.factory import build_connector
from herness.connectors.runner import SyncResult, SyncRunner
from herness.connectors.settings import FilesSettings
from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import CircuitOpen, ConfigError, FatalError, HernessError
from herness.core.jobs.handlers import register_handler
from herness.core.jobs.ports import JobContext
from herness.core.logging import get_logger
from herness.core.resilience import ProcessState, process_state
from herness.core.types import JobKind, JobOutcome

__all__ = ["build_sync_payload", "handle_reconcile", "handle_sync", "register_job_handlers"]

type _Mode = Literal["incremental", "backfill", "reconcile"]
type _Payload = dict[str, JsonValue]

_log = get_logger("connectors.sync")
_MAX_ENTITIES: Final = 64
_OPTION: Final = {"entities": "--entities", "start": "--from", "end": "--to"}

_Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
_Names = Annotated[list[_Name], Field(min_length=1, max_length=_MAX_ENTITIES)]
# Payload dates travel as ISO text (U01-54); anything else meets strict validation.
_Date = Annotated[
    datetime.date | None,
    BeforeValidator(lambda v: datetime.date.fromisoformat(v) if isinstance(v, str) else v),
]
_STRICT: Final = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class SyncPayload(BaseModel):
    """Payload of a `sync` job (U01-51); `entities` needs `source`, dates need `backfill`."""

    model_config = _STRICT

    source: _Name | None = None
    entities: _Names | None = None
    mode: Literal["incremental", "backfill"] = "incremental"
    start: _Date = None
    end: _Date = None

    @pydantic.model_validator(mode="after")
    def _check(self) -> Self:
        if self.entities is not None and self.source is None:
            msg = "entities requires source"
        elif self.mode != "backfill" and (self.start or self.end):
            msg = "start and end require mode backfill"
        elif self.start and self.end and self.start >= self.end:
            msg = "start must be before end"
        else:
            return self
        raise ValueError(msg)


class ReconcilePayload(BaseModel):
    """Payload of a `reconcile` job (U01-52)."""

    model_config = _STRICT

    source: _Name
    entities: _Names | None = None


class _Step(NamedTuple):  # one entity call: its log mode and the runner call (None: skipped)
    mode: _Mode
    call: Callable[[SyncRunner, str], SyncResult | None]


@dataclass
class _Tally:
    """Per-job results, skipped breaker keys and recorded failures (U01-51 steps 5-8)."""

    results: list[JsonValue] = field(default_factory=list)
    skipped: set[str] = field(default_factory=set)
    failed: list[tuple[str, str, HernessError]] = field(default_factory=list)

    def result(self) -> _Payload:
        failed: list[JsonValue] = [
            {"source": s, "entity": e, "error_class": type(x).__name__} for s, e, x in self.failed
        ]
        return {
            "results": self.results,
            "partial": bool(self.failed or self.skipped),
            "skipped_open_circuit": cast("list[JsonValue]", sorted(self.skipped)),
            "failed": failed,
        }

    def finish(self) -> JobOutcome:
        """Step 7-8: raise the first FatalError, else the first failure; else `done`."""
        errors = [exc for _, _, exc in self.failed]
        if errors:
            raise next((e for e in errors if isinstance(e, FatalError)), errors[0])
        return JobOutcome(status="done", result=self.result())


def _parse[M: BaseModel](model: type[M], ctx: JobContext, what: str) -> M:
    try:
        return model.model_validate(dict(ctx.job.payload))
    except ValidationError:
        msg = f"invalid {what} payload"
        raise ConfigError(msg) from None


def _locked(beat: Callable[[str | None], None]) -> Callable[[str], None]:
    """`ctx.heartbeat` behind a lock: the runner reports from backfill slice threads."""
    lock = threading.Lock()

    def progress(note: str) -> None:
        with lock:
            beat(note)

    return progress


def _entities(conn: Connector, requested: list[str] | None) -> list[str]:
    unknown = [entity for entity in requested or () if entity not in conn.entities]
    if unknown:
        msg = f"unknown entity {unknown[0]} for source {conn.name}"
        raise ConfigError(msg, source=conn.name, entity=unknown[0])
    return requested or list(conn.entities)


def _run_source(
    ctx: JobContext, runner: SyncRunner, ents: list[str], step: _Step, t: _Tally
) -> bool:
    """Run `step` per entity of one source; True when the job must yield (step 5)."""
    name = runner.connector.name
    for entity in ents:
        if ctx.should_yield():
            return True
        try:
            done = step.call(runner, entity)
        except CircuitOpen as exc:
            t.skipped.add(exc.key)
            at = {"stream": exc.key, "retry_at": clock.format_utc(exc.retry_at)}
            _log.warning("connectors.sync.skipped_open_circuit", source=name, **at)
            return False
        except HernessError as exc:
            t.failed.append((name, entity, exc))
            ids = {"source": name, "entity": entity, "stream": name, "mode": step.mode}
            _log.error("connectors.sync.failed", **ids, error_class=type(exc).__name__)
            continue
        finally:
            t.skipped.update(runner.skipped_open)
        if done is not None:
            t.results.append(cast("JsonValue", done.to_dict(data_root=runner.data_root)))
        if runner.stopped:
            return True
    return False


def _run_job(
    ctx: JobContext, names: Sequence[str], entities: list[str] | None, step: _Step
) -> JobOutcome:
    """U01-51 steps 4-8 over `names` with `step` as the per-entity call."""
    cfg = get_config()
    tally = _Tally()
    progress = _locked(ctx.heartbeat)
    for name in names:
        conn = build_connector(name, cfg)
        runner = SyncRunner(
            conn,
            cfg.sources.source(name),
            connector_factory=partial(build_connector, name, cfg),
            progress=progress,
            should_stop=ctx.should_yield,
        )
        todo = _entities(conn, entities)
        if _run_source(ctx, runner, todo, step, tally):
            return JobOutcome(status="yield", result=tally.result())
    return tally.finish()


def _midnight(day: datetime.date) -> datetime.datetime:
    return datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.UTC)


def _sync_step(p: SyncPayload) -> _Step:
    if p.mode == "incremental":
        return _Step("incremental", lambda runner, entity: runner.run_incremental(entity))

    def backfill(runner: SyncRunner, entity: str) -> SyncResult:
        now = runner.clock()
        start = (
            _midnight(p.start) if p.start else runner.cfg.backfill_for(entity).resolve_start(now)
        )
        end = _midnight(p.end) if p.end else now
        return runner.run_backfill(entity, start, end)

    return _Step("backfill", backfill)


def _reconcile(runner: SyncRunner, entity: str) -> SyncResult | None:
    """`run_reconcile`, except files entities in `delta` mode, which are skipped (U01-52)."""
    cfg = runner.cfg
    if isinstance(cfg, FilesSettings) and cfg.entities[entity].mode == "delta":
        _log.info(
            "connectors.reconcile.skipped", source=cfg.SOURCE, entity=entity, reason="delta_mode"
        )
        return None
    return runner.run_reconcile(entity)


def handle_sync(ctx: JobContext) -> JobOutcome:
    """Job kind `sync` (U01-51): every requested entity of every requested source, once.

    Raises ConfigError (payload, config) or the first recorded entity error (fatal first).
    """
    p = _parse(SyncPayload, ctx, "sync")
    names = [p.source] if p.source else [n for n, _ in get_config().sources.enabled_sources()]
    return _run_job(ctx, names, p.entities, _sync_step(p))


def handle_reconcile(ctx: JobContext) -> JobOutcome:
    """Job kind `reconcile` (U01-52); `monitoring` is never reconciled (ConfigError)."""
    p = _parse(ReconcilePayload, ctx, "reconcile")
    if p.source == "monitoring":
        msg = "monitoring is not reconciled"
        raise ConfigError(msg, source=p.source)
    return _run_job(ctx, [p.source], p.entities, _Step("reconcile", _reconcile))


class _Registered:
    """U01-53 flag: the `ProcessState` holding our handlers; a fresh state resets it."""

    state: ProcessState | None = None


def register_job_handlers() -> None:
    """Register `handle_sync` (`sync`) and `handle_reconcile` (`reconcile`) once (U01-53)."""
    state = process_state()
    if _Registered.state is state:
        return
    register_handler("sync", handle_sync)
    register_handler("reconcile", handle_reconcile)
    _Registered.state = state


def _conflict(msg: str) -> NoReturn:
    raise ConfigError(msg)


def _checked(kind: JobKind, payload: _Payload, key: str) -> tuple[JobKind, _Payload, str]:
    """Postcondition of U01-54: the payload validates as the handler's model (TH01-07)."""
    model: type[BaseModel] = ReconcilePayload if kind == "reconcile" else SyncPayload
    try:
        model.model_validate(payload)
    except ValidationError as exc:
        loc = exc.errors()[0]["loc"]
        name = str(loc[0]) if loc else "options"
        _conflict(f"invalid {_OPTION.get(name, name)}")
    return kind, payload, key


def _backfill(
    base: _Payload, from_: datetime.date | None, to: datetime.date, today: datetime.date
) -> tuple[JobKind, _Payload, str]:
    if from_ is None:
        _conflict("--backfill requires --from")
    if to > today:
        _conflict("--to must not be after today")
    if from_ >= to:
        _conflict("--from must be before --to")
    payload = base | {"mode": "backfill", "start": from_.isoformat(), "end": to.isoformat()}
    return _checked("sync", payload, f"sync:{base['source'] or 'all'}:backfill:{from_}:{to}")


def build_sync_payload(  # noqa: PLR0913 - the U01-54 contract; keyword-only after `source`
    source: str | None,
    *,
    entities: Sequence[str] = (),
    full: bool = False,
    backfill: bool = False,
    from_: datetime.date | None = None,
    to: datetime.date | None = None,
    reconcile: bool = False,
    today: datetime.date,
) -> tuple[JobKind, dict[str, JsonValue], str]:
    """Map `herness sync` options to (job kind, payload, idem key) (U01-54, R-63).

    Pure; conflicting options → ConfigError naming them."""
    ents: JsonValue = cast("list[JsonValue]", list(entities)) or None
    if entities and source is None:
        _conflict("--entities requires a source")
    if reconcile:
        if full or backfill or from_ is not None or to is not None:
            _conflict("--reconcile cannot be combined with --full, --backfill, --from or --to")
        if source is None:
            _conflict("--reconcile requires a source")
        return _checked("reconcile", {"source": source, "entities": ents}, f"reconcile:{source}")
    base: _Payload = {"source": source, "entities": ents}
    if full and backfill:
        _conflict("--full and --backfill are exclusive")
    if backfill:
        return _backfill(base, from_, to or today, today)
    if from_ is not None or to is not None:
        _conflict("--from and --to require --backfill")
    if full:  # R-63 / §13 D-6: the handler backfills from `backfill.start` to now
        payload = base | {"mode": "backfill", "start": None, "end": None}
        return _checked("sync", payload, f"sync:{source or 'all'}:backfill:{None}:{None}")
    return _checked("sync", base | {"mode": "incremental"}, f"sync:{source or 'all'}")

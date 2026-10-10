"""Page wrapper, block isolation, evidence, badges, banners, sidebar (impl 09 U09-65 - U09-68).

Identity, audit and role failures stop the page before any read (TH09-08). Widgets render only
constant or ``safe_markdown`` text; evidence samples are dataframe string cells (TH09-23); error
displays show the error type and ids only, never a stack trace (design §5.4, §6)."""

from __future__ import annotations

import contextlib
import contextvars
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal, NoReturn, Protocol

import pyarrow as pa
import streamlit as st

from app.common import auth, bootstrap, data, wh
from app.common.sanitize import safe_markdown
from herness.core import time as clock
from herness.core.errors import HernessError, SchemaViolation
from herness.core.logging import get_logger
from herness.core.resilience.metrics import record_histogram
from herness.reports.contract import QUERY_ID_RE, unconfirmed_weight_keys
from herness.reports.rules import Actor, require_role, user_message

if TYPE_CHECKING:
    from app.common.bootstrap import AppServices

__all__ = [
    "PageContext", "banner_strip", "block", "evidence", "page", "sidebar_build", "user_error",
    "verification_badge",
]  # fmt: skip

NOT_AUTHORIZED: Final = "Not authorized"
NO_BUILD: Final = "No promoted build yet. Run `herness pipeline`."
D2_TEXT: Final = "Dollar weights are placeholders and not yet confirmed: "
UNEXPECTED: Final = ("Unexpected error on this page.",
                     "Check the log `data/logs/herness-<date>.jsonl`.")  # fmt: skip
EVIDENCE_SHOWN: Final = 50
CELL_MAX: Final = 200
_CONTROL: Final = frozenset({"StopException", "RerunException"})
_BADGES: Final = {"verified": (":green-badge[Verified]", None), "partial": (
    ":orange-badge[Partially verified]", "Some figures could not be verified and were removed.")
}  # fmt: skip
_UNVERIFIED: Final = (":gray-badge[Unverified]", None)
_LABEL_RE: Final = re.compile(r"[^A-Za-z0-9_.:\-]")
_log = get_logger("app.page")
_page_name: contextvars.ContextVar[str] = contextvars.ContextVar("herness_page", default="")


class _Banner(Protocol):
    """U09-16 ``Banner`` shape (``herness.reports._data.ReportBanner``)."""

    @property
    def text(self) -> str: ...
    @property
    def details(self) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class PageContext:
    """What a page body gets: the session actor, the services, the build and the render time."""

    actor: Actor
    services: AppServices
    build_id: str | None
    now: datetime


class _Halt(BaseException):
    """Raised when ``st.stop()`` returns (no script run context): never continue to a read."""


def _halt() -> NoReturn:
    st.stop()
    raise _Halt  # pragma: no cover - st.stop() raises StopException inside a script run


def _reraise_control(exc: BaseException) -> None:
    if type(exc).__name__ in _CONTROL:
        raise exc


def verification_badge(status: Literal["verified", "partial", "unverified"] | None) -> None:
    """Green "Verified", orange "Partially verified" plus caption, else grey "Unverified"."""
    badge, caption = _BADGES.get(status or "", _UNVERIFIED)
    st.markdown(badge)
    if caption is not None:
        st.caption(caption)


def banner_strip(banners: Sequence[_Banner]) -> None:
    """One ``st.warning`` per banner; its details as a caption list."""
    for banner in banners:
        st.warning(safe_markdown(banner.text))
        if banner.details:
            st.caption("\n".join(f"- {safe_markdown(item)}" for item in banner.details))


def sidebar_build(
    build_id: str | None, data_as_of: datetime | None, unconfirmed_keys: Sequence[str]
) -> None:
    """Sidebar lines: the build, data-as-of time (UTC) and the D2 warning listing keys."""
    with st.sidebar:
        if build_id is None:
            st.caption("No promoted build")
        else:
            st.caption(f"Build `{build_id.replace('`', '')}`")
        if data_as_of is not None:
            st.caption(f"Data as of {data_as_of.astimezone(UTC):%Y-%m-%d %H:%M} UTC")
        if unconfirmed_keys:
            st.warning(D2_TEXT + ", ".join(safe_markdown(key) for key in unconfirmed_keys))


def _error_card(what: str, fix: str, exc: BaseException, title: str | None) -> None:
    """Error/Fix lines; "Details": error type, ``run_id`` / ``job_id`` (attribute or details)."""
    head = f"**{safe_markdown(title)}**\n\n" if title else ""
    st.error(f"{head}Error: {safe_markdown(what)}\n\nFix: {safe_markdown(fix)}")
    details = exc.details if isinstance(exc, HernessError) else {}
    with st.expander("Details", expanded=False):
        st.text(f"Error type: {type(exc).__name__}")
        for key in ("run_id", "job_id"):
            if (value := getattr(exc, key, None) or details.get(key)) is not None:
                st.text(f"{key}: {str(value)[:64]}")
        st.text(f"Log: data/logs/herness-{clock.now():%Y-%m-%d}.jsonl")


def user_error(exc: HernessError, *, title: str | None = None) -> None:
    """``Error: <what>`` / ``Fix: <fix>`` (U09-88) with a collapsed "Details" expander."""
    _error_card(*user_message(exc), exc, title)


@contextmanager
def block(name: str) -> Iterator[None]:
    """Isolate one widget block: a failure shows an error card; the rest of the page renders."""
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - the block error boundary (design §6, DD-21)
        _reraise_control(exc)
        _log.error("app.widget.failed", page=_page_name.get(), block=name,
                   error_type=type(exc).__name__)  # fmt: skip
        title = f"{name} could not load"
        if isinstance(exc, HernessError):
            user_error(exc, title=title)
        else:
            _error_card(*UNEXPECTED, exc, title)


def _json(raw: object) -> object:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            return None
    return raw


def _sample(raw: object) -> list[dict[str, str]] | None:
    """String cells cut to 200 chars; raw-text columns dropped; ``None`` when not stored."""
    parsed = _json(raw)
    if not isinstance(parsed, list):
        return None
    return [{str(k): ("" if v is None else str(v))[:CELL_MAX] for k, v in row.items()
             if str(k) not in wh.FORBIDDEN_TEXT_COLUMNS}
            for row in parsed if isinstance(row, Mapping)]  # fmt: skip


def _meta_rows(ids: list[str]) -> tuple[dict[str, dict[str, object]], str | None]:
    """``meta.evidence`` rows of the current build for ``ids`` (none without a build)."""
    try:
        result = wh.query("evidence.meta", ids=ids)
    except wh.NoCurrentBuild:
        return {}, None
    rows = result.table.to_pylist()
    return {str(row["query_id"]): row for row in rows}, result.build_id


def _entry(query_id: str, row: Mapping[str, object] | None, build_id: str | None) -> None:
    st.subheader(query_id, anchor=f"ev-{query_id}")
    if row is None:
        st.caption("Evidence not found")
        return
    st.code(str(row.get("sql") or ""), language="sql")
    params = _json(row.get("params"))
    st.json(params if isinstance(params, dict | list) else {})
    st.text(f"Build {row.get('build_id') or build_id} · {row.get('row_count')} rows")
    sample = _sample(row.get("result_sample"))
    if sample is None:
        st.caption("Sample not stored for this build")
    else:
        st.dataframe(pa.Table.from_pylist(sample), hide_index=True)


def evidence(query_ids: Sequence[str], *, label: str | None = None) -> None:
    """The evidence expander: one anchored sub-section per query id (design §5.3, §5.5)."""
    ids = list(dict.fromkeys(q for q in query_ids if QUERY_ID_RE.fullmatch(q) is not None))
    shown = ids[:EVIDENCE_SHOWN]
    with st.expander(label or f"Evidence ({len(ids)} queries)"):
        ops = data.ops_read("ui_get_evidence_rows", ids=tuple(shown))
        found: Mapping[str, Mapping[str, object]] = ops if isinstance(ops, Mapping) else {}
        missing = [q for q in shown if q not in found]
        meta, meta_build = _meta_rows(missing) if missing else ({}, None)
        for query_id in shown:
            row = found.get(query_id)
            _entry(query_id, row if row is not None else meta.get(query_id), meta_build)
        if len(ids) > EVIDENCE_SHOWN:
            st.caption(f"… and {len(ids) - EVIDENCE_SHOWN} more")


def _gate() -> tuple[AppServices, Actor]:
    """Steps 2 - 4, fail closed: any failure (config, identity, audit, role) stops the page."""
    try:
        services = bootstrap.get_services()
        actor = auth.current_actor()
        if actor.role == "denied":
            st.error(NOT_AUTHORIZED)
            _halt()
        require_role(actor, "view")
    except Exception as exc:  # noqa: BLE001 - controller ruling: stop on any gate failure
        _boundary(exc, "gate")
        _halt()
    return services, actor


def _boundary(exc: Exception, stage: Literal["gate", "warehouse", "body"]) -> None:
    """``user_error``, or log ``app.page.failed`` (with ``stage``) and a generic card."""
    _reraise_control(exc)
    if isinstance(exc, HernessError) and stage != "gate":
        user_error(exc)
        return
    _log.error("app.page.failed", page=_page_name.get(), error_type=type(exc).__name__,
               stage=stage)  # fmt: skip
    _error_card(*(user_message(exc) if isinstance(exc, HernessError) else UNEXPECTED), exc, None)


def _data_as_of(row: Mapping[str, object]) -> datetime | None:
    """Max of the ISO timestamps in ``source_watermarks`` (like the report header)."""
    marks = _json(row.get("source_watermarks"))
    stamps: list[datetime] = []
    for value in marks.values() if isinstance(marks, dict) else ():
        try:
            stamps.append(clock.parse_iso(str(value)))
        except SchemaViolation:
            continue
    return max(stamps, default=None)


def _sidebar(services: AppServices) -> str | None:
    """Step 5: the current build in the sidebar; returns its id (None without a build)."""
    build_id: str | None = None
    as_of: datetime | None = None
    with block("Sidebar"):  # the sidebar never takes the page down
        keys = unconfirmed_weight_keys(services.cfg.weights.model_dump())
        try:
            build_id, _conn = wh.get_conn()
            rows = wh.query("build.meta", build_id=build_id).table.to_pylist()
            as_of = _data_as_of(rows[0]) if rows else None
        except wh.NoCurrentBuild:
            build_id = None
        except Exception as exc:  # noqa: BLE001 - step 6 or the page's blocks show the card, once
            _reraise_control(exc)
            _log.error("app.widget.failed", page=_page_name.get(), block="sidebar",
                       error_type=type(exc).__name__)  # fmt: skip
            st.sidebar.caption("Build information unavailable")
            return None
        sidebar_build(build_id, as_of, keys)
    return build_id


def page(title: str, body: Callable[[PageContext], None], *, needs_warehouse: bool = True) -> None:
    """The page wrapper (U09-65): identity, role gate, sidebar, empty state, error boundary."""
    started = clock.monotonic()
    _page_name.set(title)
    st.set_page_config(page_title=f"Herness · {title}", layout="wide")
    services, actor = _gate()
    build_id = _sidebar(services)
    if needs_warehouse:
        try:
            build_id, _conn = wh.get_conn()
        except wh.NoCurrentBuild:
            st.info(NO_BUILD)
            _halt()
        except Exception as exc:  # noqa: BLE001 - step 6 never leaks a stack trace
            _boundary(exc, "warehouse")
            _halt()
    try:  # step 7
        body(PageContext(actor, services, build_id, clock.now()))
    except Exception as exc:  # noqa: BLE001 - the page error boundary (ENG §3.4)
        _boundary(exc, "body")
    seconds = clock.monotonic() - started
    _log.debug("app.page.rendered", page=title, duration_ms=int(seconds * 1000), build_id=build_id)
    label = _LABEL_RE.sub("_", title)[:64] or "page"
    with contextlib.suppress(Exception):  # a metric failure never breaks a rendered page
        record_histogram("herness_app_page_seconds", seconds, component="app",
                         labels={"page": label})  # fmt: skip

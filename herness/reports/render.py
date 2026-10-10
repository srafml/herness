"""Report rendering: Jinja environment, ``render_run``, atomic output, PDF, health (impl 09).

U09-23 … U09-27, flow F09-01. Model text reaches the templates only as escaped segments
(``segments_to_html`` Markup or raw ``Segment`` lists the Markdown macros escape); it is never
a template source or name (TH09-01, TH09-25). Files are written tmp + fsync + ``os.replace``,
``manifest.json`` last (TH09-24). The PDF engine may fetch ``data:`` URLs only (TH09-03).
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import importlib
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Final, Literal
from urllib.parse import urlsplit

import duckdb
import jinja2
import markupsafe

from herness.core import config as _config
from herness.core import errors as err
from herness.core import ids
from herness.core import time as clock
from herness.core.logging import get_logger
from herness.core.types import MetricSample, ReportDraft, ReportManifest
from herness.reports import contract
from herness.reports._data import ReportData, load_report_data
from herness.reports._data_slots import TextBlock
from herness.reports._evidence import EvidenceCollector, EvidenceEntry, load_evidence_entries
from herness.reports._markup import Segment, md_escape, segments_to_html, strip_images
from herness.reports.contract import RUN_ID_RE, UncitedHit
from herness.store import ops, warehouse
from herness.store.ops.metrics import record_metric_samples

__all__ = ["REPORT_CSP", "TEMPLATE_VERSION", "build_environment", "health", "render_run"]

REPORT_CSP: Final = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
TEMPLATE_VERSION: Final = "1"

type Format = Literal["html", "md", "pdf"]
type Health = Literal["ok", "degraded", "down"]

_FILE_NAMES: Final[Mapping[str, str]] = {"html": "report.html", "md": "report.md",
                                         "pdf": "report.pdf"}  # fmt: skip
_ALLOWED_FILES: Final = frozenset(_FILE_NAMES.values())
_TEMPLATES: Final = tuple(f"{kind}.{ext}.j2" for kind in ("base", "funding_review", "org_review")
                          for ext in ("html", "md"))  # fmt: skip
_HTML_LIMIT: Final = 5_242_880
_PDF_HINT: Final = "`uv sync --extra pdf` (GTK/Pango on Windows, spec 10)"
_RETIRED_HINT: Final = "Re-run the review: `herness report funding`."
_COMPONENT: Final = "reports"
_log = get_logger("reports.render")


@functools.cache
def build_environment() -> jinja2.Environment:
    """The one Jinja2 environment for both formats (U09-23); package templates only."""
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("herness.reports", "templates"),
        autoescape=jinja2.select_autoescape(enabled_extensions=("html.j2",),
                                            default_for_string=False, default=False),
        undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=True,
        keep_trailing_newline=True, auto_reload=False,
    )  # fmt: skip
    env.filters["md"] = md_escape
    env.globals.update(REPORT_CSP=REPORT_CSP, TEMPLATE_VERSION=TEMPLATE_VERSION)
    return env


_config._RESET_HOOKS.append(build_environment.cache_clear)  # spec 11 config-reset fixture


def _render_template(name: str, r: Mapping[str, Any]) -> str:
    """Render ``name``; any ``jinja2.TemplateError`` becomes SchemaViolation (U09-24)."""
    try:
        return build_environment().get_template(name).render(r=r)
    except jinja2.TemplateError as exc:
        msg = f"report template error: {name}"
        raise err.SchemaViolation(msg, details={"template": name}) from exc


type _Convert = Callable[[list[Segment]], Any]


def _context(data: ReportData, conv: _Convert, **extra: object) -> dict[str, Any]:
    """``r``: every ReportData field with each ``list[Segment]`` passed through ``conv``."""

    def blocks(items: Sequence[TextBlock]) -> list[TextBlock]:
        return [TextBlock(b.where, conv(b.segments)) for b in items]

    r: dict[str, Any] = {f.name: getattr(data, f.name) for f in dataclasses.fields(data)}
    retro = data.retro._replace(blocks=blocks(data.retro.blocks), items=[
        i._replace(summary=conv(i.summary)) for i in data.retro.items])  # fmt: skip
    r.update(
        header=data.header._replace(title=conv(data.header.title)),
        sections={key: blocks(value) for key, value in data.sections.items()},
        cards=[c._replace(headline=conv(c.headline), summary=conv(c.summary))
               for c in data.cards],
        retro=retro, caveats=data.caveats._replace(blocks=blocks(data.caveats.blocks)),
        **extra,
    )  # fmt: skip
    return r


def _md_segments(segments: list[Segment]) -> list[Segment]:
    """Text segments with Markdown images replaced by their alt text (U09-18)."""
    return [dataclasses.replace(s, text=strip_images(s.text)) if s.kind == "text" else s
            for s in segments]  # fmt: skip


def _html_charts(charts: Mapping[str, str]) -> dict[str, markupsafe.Markup]:
    """Chart SVG built by ``charts.py`` (labels escaped there, U09-19 … U09-22) as Markup."""
    return {key: markupsafe.Markup(svg) for key, svg in charts.items()}  # noqa: S704 - chart SVG


def _write_file(out_dir: Path, name: str, data: bytes, tmps: list[Path]) -> str:
    """Write ``data`` to a unique tmp file, fsync, ``os.replace`` to ``name``; SHA-256 hex."""
    tmp = out_dir / f"{name}.{os.getpid()}.{ids.new_ulid()}.tmp"
    tmps.append(tmp)
    with tmp.open("xb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, out_dir / name)
    return hashlib.sha256(data).hexdigest()


def _write_outputs(out_dir: Path, files: Mapping[str, bytes],
                   manifest: ReportManifest) -> ReportManifest:  # fmt: skip
    """Write every file in sorted order, then ``manifest.json`` with their hashes (U09-25).

    ``ReportManifest`` admits only the three report file names (U09-01)."""
    tmps: list[Path] = []
    name = "manifest.json"
    try:
        hashes: dict[str, str] = {}
        for name in sorted(files):
            hashes[name] = _write_file(out_dir, name, files[name], tmps)
        for name in sorted(_ALLOWED_FILES - set(files)):  # the manifest covers every artefact
            (out_dir / name).unlink(missing_ok=True)
        name = "manifest.json"
        final = ReportManifest.model_validate({**manifest.model_dump(), "files": hashes})
        _write_file(out_dir, name, (final.model_dump_json(indent=2) + "\n").encode(), tmps)
    except BaseException as exc:
        for tmp in tmps:
            tmp.unlink(missing_ok=True)
        if isinstance(exc, OSError):
            msg = f"cannot write report file {name}"
            raise err.SchemaViolation(msg, details={"file": name}) from exc
        raise
    return final


def _pdf_engine() -> ModuleType:
    """WeasyPrint, imported lazily; missing (or no GTK/Pango) → ConfigError ``pdf_missing``."""
    try:
        return importlib.import_module("weasyprint")
    except (ImportError, OSError) as exc:
        msg = "PDF engine not installed."
        raise err.ConfigError(msg, hint=_PDF_HINT, details={"code": "pdf_missing"}) from exc


def _render_pdf(html: str) -> bytes:
    """PDF bytes of ``html``; the engine may fetch ``data:`` URLs only (TH09-03)."""
    engine = _pdf_engine()

    def deny_fetch(url: str, *args: object, **kwargs: object) -> object:
        if urlsplit(url).scheme.lower() == "data":
            return engine.default_url_fetcher(url, *args, **kwargs)
        msg = "report PDF fetch refused"
        raise err.EgressBlocked(msg, reason="report_pdf_fetch")

    return bytes(engine.HTML(string=html, base_url=None, url_fetcher=deny_fetch).write_pdf())


def _formats(formats: Sequence[str]) -> list[str]:
    if isinstance(formats, str) or not formats:
        msg = "report formats must be a non-empty list"
        raise err.ConfigError(msg, details={"formats": str(formats)})
    for value in formats:
        if value not in _FILE_NAMES:
            msg = f"unknown report format: {value}"
            raise err.ConfigError(msg, details={"format": str(value)})
    return list(dict.fromkeys(formats))


def _prepare_out_dir(out_dir: Path, run_id: str) -> Path:
    """Create ``out_dir`` (never a symlink) and remove leftover ``*.tmp`` files (F09-01 step 5)."""
    if out_dir.is_symlink() or out_dir.is_junction():
        msg = "report output directory must not be a symlink"
        raise err.ReportContractError(msg, details={"code": "unsafe_out_dir", "run_id": run_id})
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        resolved = out_dir.resolve(strict=True)
        stale = [p for p in resolved.glob("*.tmp") if p.is_file() and not p.is_symlink()]
        for path in stale:
            path.unlink(missing_ok=True)
    except OSError as exc:
        msg = "cannot prepare the report output directory"
        raise err.SchemaViolation(msg, details={"run_id": run_id}) from exc
    if stale:
        _log.debug("reports.render.tmp_cleaned", run_id=run_id, count=len(stale))
    return resolved


def _open_build(build_id: str) -> duckdb.DuckDBPyConnection:
    """The run's warehouse, read-only; a missing or retired file → ``build_retired``."""
    retired = err.ReportContractError(
        f"Build `{build_id}` used by this run was deleted by retention.", hint=_RETIRED_HINT,
        details={"code": "build_retired", "build_id": build_id},
    )  # fmt: skip
    if not warehouse.build_exists(build_id):
        raise retired
    try:
        return warehouse.open_readonly(build_id)
    except err.NotFound as exc:  # deleted between the check and the open
        raise retired from exc


type _Collected = tuple[ReportData, list[EvidenceEntry], list[UncitedHit]]


def _collect(run: ops.RunRow, draft: ReportDraft, strict: bool, top_n: int | None,
             now: datetime) -> _Collected:  # fmt: skip
    """F09-01 steps 6-10 on the run's warehouse (closed afterwards)."""
    cfg = _config.get_config()
    settings = cfg.app.reports
    con = _open_build(draft.build_id)
    try:
        lookups = contract.OpsContractLookups(con, build_id=draft.build_id)
        contract.check_render_contract(draft, lookups=lookups)
        hits = contract.scan_draft_uncited(draft, settings.compiled_numeral_patterns)
        if strict and hits:  # details.where: the bounded, de-duplicated field paths
            where = ", ".join(dict.fromkeys(hit.where for hit in hits))
            msg = f"{len(hits)} numbers in the draft have no evidence"
            details = {"code": "uncited", "run_id": run.run_id, "where": where}
            raise err.ReportContractError(msg, details=details)
        collector = EvidenceCollector()
        data = load_report_data(
            run, draft, wh_con=con, collector=collector, uncited=hits,
            top_n=settings.top_n if top_n is None else top_n, now=now,
            unconfirmed_keys=contract.unconfirmed_weight_keys(cfg.weights.model_dump(mode="python")),
            outcomes_window_days=settings.prior_outcomes_window_days,
        )  # fmt: skip
        entries = load_evidence_entries(collector, con, build_id=draft.build_id,
                                        sample_rows=settings.evidence_sample_rows)  # fmt: skip
    finally:
        con.close()
    return data, entries, hits


def _files(kind: str, formats: Sequence[str], r_html: Mapping[str, Any],
           r_md: Mapping[str, Any]) -> dict[str, bytes]:  # fmt: skip
    """Rendered bytes per requested format; PDF is printed from the HTML."""
    files: dict[str, bytes] = {}
    html = _render_template(f"{kind}.html.j2", r_html) if {"html", "pdf"} & set(formats) else ""
    if "html" in formats:
        files["report.html"] = html.encode()
    if "md" in formats:
        files["report.md"] = _render_template(f"{kind}.md.j2", r_md).encode()
    if "pdf" in formats:
        files["report.pdf"] = _render_pdf(html)
    return files


def _load_run(run_id: str) -> ops.RunRow:
    if RUN_ID_RE.fullmatch(run_id) is None:
        msg = "invalid run_id"
        raise err.ReportContractError(msg, details={"code": "invalid_run_id"})
    run = ops.get_run(run_id)
    if run is None:
        msg = f"run {run_id} not found"
        raise err.NotFound(msg, details={"code": "run_not_found", "run_id": run_id})
    if run.status not in contract.RENDERABLE_RUN_STATUSES:
        msg = f"run not finished: {run_id} is {run.status}"
        details = {"code": "run_not_finished", "run_id": run_id, "status": run.status}
        raise err.ReportContractError(msg, details=details)
    return run


def _render(run_id: str, formats: Sequence[str], out_dir: Path | None, strict: bool,
            now: datetime, top_n: int | None) -> ReportManifest:  # fmt: skip
    wanted = _formats(formats)
    run = _load_run(run_id)
    root = Path(_config.get_config().paths.data) / "reports"
    draft = contract.load_draft(run_id, reports_root=root)
    if draft.kind == "chat":  # only the two review kinds have templates
        msg = "report draft kind is not renderable"
        raise err.ReportContractError(msg, details={"code": "draft_invalid", "where": "kind"})
    out = _prepare_out_dir(root / run_id if out_dir is None else out_dir, run_id)
    data, entries, hits = _collect(run, draft, strict, top_n, now)
    found = {e.query_id for e in entries if e.found}
    linked = sum(1 for qid in data.number_query_ids if qid in found)
    warnings = [f"evidence not found: {e.query_id}" for e in entries if not e.found]
    base = ReportManifest(
        run_id=run_id, kind=draft.kind, build_id=draft.build_id,
        template_version=TEMPLATE_VERSION, rendered_at=now, publishable=data.publishable,
        files={}, numbers_total=data.numbers_total, numbers_linked=linked,
        uncited=[{"where": h.where, "text": h.text} for h in hits],
        evidence_entries=len(entries), warnings=warnings,
    )  # fmt: skip
    extra = {"evidence": entries, "numbers_linked": linked, "manifest": base}
    r_html = _context(data, segments_to_html, **extra)
    r_html["charts"] = _html_charts(data.charts)
    files = _files(draft.kind, wanted, r_html, _context(data, _md_segments, **extra))
    if len(files.get("report.html", b"")) > _HTML_LIMIT:
        base = base.model_copy(update={"warnings": [*warnings, "report.html exceeds 5 MB"]})
    return _write_outputs(out, files, base)


def _record(result: str, now: datetime, manifest: ReportManifest | None, seconds: float) -> None:
    """Render metrics through ``record_metric_samples`` (R-12); a store error is logged only."""
    rows: list[tuple[str, str, float, dict[str, str]]] = [
        ("herness_reports_renders_total", "counter", 1.0, {"result": result})]  # fmt: skip
    if manifest is not None:
        kind: dict[str, str] = {"kind": manifest.kind}
        rows.append(("herness_reports_render_seconds", "histogram", seconds, kind))
        if manifest.uncited:
            rows.append(("herness_reports_uncited_total", "counter", len(manifest.uncited), kind))
    try:
        record_metric_samples([MetricSample.model_validate(
            {"ts": now, "name": name, "kind": kind_, "value": float(value), "labels": labels,
             "component": _COMPONENT}) for name, kind_, value, labels in rows])  # fmt: skip
    except err.HernessError as exc:
        _log.warning("reports.render.metrics_failed", error_type=type(exc).__name__)


def render_run(run_id: str, formats: Sequence[Format], out_dir: Path | None = None,
               strict: bool = True, now: datetime | None = None, *,
               top_n: int | None = None) -> ReportManifest:  # fmt: skip
    """Render a finished run (U09-24, F09-01); errors per the U09-24 table. Never calls a model."""
    at = clock.ensure_utc(now) if now is not None else clock.now()
    start = clock.monotonic()
    try:
        manifest = _render(run_id, formats, out_dir, strict, at, top_n)
    except err.HernessError as exc:
        code = exc.details.get("code", "")
        _log.error("reports.render.failed", run_id=run_id if RUN_ID_RE.fullmatch(run_id) else "",
                   error_type=type(exc).__name__, code=code)  # fmt: skip
        result = "contract_error" if isinstance(exc, err.ReportContractError) else "error"
        _record(result, at, None, 0.0)
        raise
    seconds = clock.monotonic() - start
    _log.info(
        "reports.render.completed", run_id=run_id, build_id=manifest.build_id,
        kind=manifest.kind, formats=",".join(sorted(manifest.files)),
        numbers_total=manifest.numbers_total, numbers_linked=manifest.numbers_linked,
        uncited=len(manifest.uncited), evidence_entries=manifest.evidence_entries,
        duration_ms=int(seconds * 1000),
    )  # fmt: skip
    _record("ok", at, manifest, seconds)
    return manifest


def health() -> tuple[Health, str]:
    """``ok``, ``degraded`` (PDF configured, engine missing) or ``down`` (a template fails)."""
    env = build_environment()
    for name in _TEMPLATES:
        try:
            env.get_template(name)
        except jinja2.TemplateError:
            return "down", f"templates failed to load: {name}"
    if "pdf" in _config.get_config().app.reports.formats:
        try:
            _pdf_engine()
        except err.ConfigError:
            return "degraded", "pdf engine missing"
    return "ok", "templates loaded"

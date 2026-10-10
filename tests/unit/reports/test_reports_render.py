"""Unit tests of ``herness.reports.render`` (impl 09 U09-23 … U09-27; T09-11).

``_write_outputs`` and the environment run on tmp files only; the PDF engine is a fake
``weasyprint`` module in ``sys.modules`` (WeasyPrint is the optional ``pdf`` extra); run rows
go into the migrated ``ops_store`` fixture.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import jinja2
import pytest

from herness.core import config
from herness.core.errors import (
    ConfigError,
    EgressBlocked,
    NotFound,
    ReportContractError,
    SchemaViolation,
    StoreBusy,
)
from herness.core.types import ReportManifest
from herness.reports import render
from herness.reports._markup import md_escape
from herness.store.ops import core

pytestmark = pytest.mark.unit

RUN_ID: Final = "run_01JA0000000000000000000007"
NOW: Final = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _manifest(**changes: Any) -> ReportManifest:
    fields: dict[str, Any] = {
        "run_id": RUN_ID, "kind": "funding_review", "build_id": "20260926-100000-000001",
        "template_version": "1", "rendered_at": NOW, "publishable": True, "files": {},
        "numbers_total": 2, "numbers_linked": 2, "evidence_entries": 1,
    }  # fmt: skip
    return ReportManifest.model_validate(fields | changes)


# --- UT09-80 build_environment ------------------------------------------------------------------


def test_ut09_80_environment_settings() -> None:
    """UT09-80 autoescape for ``.html.j2`` only, StrictUndefined, md filter, globals, cached."""
    env = render.build_environment()
    assert env is render.build_environment()
    assert env.autoescape("report.html.j2") is True  # type: ignore[operator]
    for name in ("report.md.j2", "x.html", None):
        assert env.autoescape(name) is False  # type: ignore[operator]
    assert env.undefined is jinja2.StrictUndefined
    assert (env.trim_blocks, env.lstrip_blocks, env.keep_trailing_newline) == (True, True, True)
    assert env.auto_reload is False
    assert env.extensions == {}
    assert isinstance(env.loader, jinja2.PackageLoader)
    assert env.filters["md"] is md_escape
    assert (
        env.globals["REPORT_CSP"]
        == render.REPORT_CSP
        == ("default-src 'none'; style-src 'unsafe-inline'; img-src data:")
    )
    assert env.globals["TEMPLATE_VERSION"] == render.TEMPLATE_VERSION == "1"
    with pytest.raises(jinja2.UndefinedError):
        env.from_string("{{ r.missing }}").render(r={})


def test_ut09_80_config_reset_clears_environment() -> None:
    """UT09-80 the spec 11 config-reset fixture (``reset_config``) drops the cached environment."""
    first = render.build_environment()
    config.reset_config()
    assert render.build_environment() is not first


def test_ut09_80_template_error_is_schema_violation() -> None:
    """UT09-80 a template error names the template in a SchemaViolation (U09-24)."""
    with pytest.raises(SchemaViolation, match=r"report template error: nope\.html\.j2"):
        render._render_template("nope.html.j2", {})
    with pytest.raises(SchemaViolation, match=r"report template error: funding_review\.md\.j2"):
        render._render_template("funding_review.md.j2", {})


# --- UT09-32 _write_outputs ---------------------------------------------------------------------


def _old_files(out: Path) -> dict[str, bytes]:
    old = {"report.html": b"old html", "report.md": b"old md", "manifest.json": b"{}\n"}
    for name, data in old.items():
        (out / name).write_bytes(data)
    return old


def test_ut09_32_writes_atomically_manifest_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-32 files replaced in sorted order, manifest last with their SHA-256; no tmp left."""
    (tmp_path / "report.pdf").write_bytes(b"stale")  # an earlier PDF render
    order: list[str] = []
    real = os.replace

    def spy(src: Any, dst: Any) -> None:
        assert re.fullmatch(r".+\.\d+\.[0-9A-Z]{26}\.tmp", Path(src).name)
        order.append(Path(dst).name)
        real(src, dst)

    monkeypatch.setattr(render.os, "replace", spy)
    files = {"report.md": b"# md\n", "report.html": b"<html></html>\n"}
    got = render._write_outputs(tmp_path, files, _manifest())
    assert order == ["report.html", "report.md", "manifest.json"]
    assert got.files == {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}
    on_disk = (tmp_path / "manifest.json").read_text(encoding="utf-8")
    assert on_disk.endswith("}\n")
    assert ReportManifest.model_validate_json(on_disk) == got
    assert json.loads(on_disk)["files"] == got.files
    assert sorted(p.name for p in tmp_path.iterdir()) == ["manifest.json", "report.html",
                                                          "report.md"]  # fmt: skip


def test_ut09_32_failure_on_second_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-32 an injected failure on the second file: no tmp left, unreplaced files intact."""
    old = _old_files(tmp_path)
    real = os.replace
    calls: list[str] = []

    def failing(src: Any, dst: Any) -> None:
        calls.append(Path(dst).name)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        real(src, dst)

    monkeypatch.setattr(render.os, "replace", failing)
    files = {"report.html": b"new html", "report.md": b"new md"}
    with pytest.raises(SchemaViolation, match=r"cannot write report file report\.md") as info:
        render._write_outputs(tmp_path, files, _manifest())
    assert isinstance(info.value.__cause__, OSError)
    assert not list(tmp_path.glob("*.tmp"))
    assert (tmp_path / "report.html").read_bytes() == b"new html"  # replaced whole
    assert (tmp_path / "report.md").read_bytes() == old["report.md"]
    assert (tmp_path / "manifest.json").read_bytes() == old["manifest.json"]


def test_ut09_32_non_os_error_cleans_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-32 any other exception also removes this call's tmp files and propagates."""

    def interrupted(_fd: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(render.os, "fsync", interrupted)
    with pytest.raises(KeyboardInterrupt):
        render._write_outputs(tmp_path, {"report.html": b"x"}, _manifest())
    assert list(tmp_path.iterdir()) == []


# --- UT09-34 / ST09-22 PDF ----------------------------------------------------------------------


def test_ut09_34_pdf_engine_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-34 a failing ``weasyprint`` import → ConfigError code ``pdf_missing`` with a hint."""
    monkeypatch.setitem(sys.modules, "weasyprint", None)
    with pytest.raises(ConfigError, match=r"^PDF engine not installed\.$") as info:
        render._render_pdf("<html></html>")
    assert info.value.details["code"] == "pdf_missing"
    assert info.value.hint == "`uv sync --extra pdf` (GTK/Pango on Windows, spec 10)"


def test_ut09_34_pdf_engine_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-34 an OSError on import (GTK/Pango missing on Windows) is the same ConfigError."""

    def broken(name: str) -> types.ModuleType:
        raise OSError(name)

    monkeypatch.setattr(render.importlib, "import_module", broken)
    with pytest.raises(ConfigError) as info:
        render._render_pdf("<html></html>")
    assert info.value.details["code"] == "pdf_missing"


class _FakeEngine:
    """A ``weasyprint`` stand-in: fetches every href/src URL through ``url_fetcher`` and,
    like WeasyPrint, logs and skips a fetch that raises."""

    def __init__(self) -> None:
        self.module = types.ModuleType("weasyprint")
        self.fetched: list[str] = []
        self.refused: list[BaseException] = []
        self.default_calls: list[str] = []
        engine = self

        class HTML:
            def __init__(self, *, string: str, base_url: object, url_fetcher: Any) -> None:
                assert base_url is None
                self.string, self.fetcher = string, url_fetcher

            def write_pdf(self) -> bytes:
                for url in re.findall(r'(?:href|src)="([^"]+)"', self.string):
                    try:
                        self.fetcher(url)
                        engine.fetched.append(url)
                    except Exception as exc:  # noqa: BLE001 - WeasyPrint logs and skips
                        engine.refused.append(exc)
                return b"%PDF-fake"

        def default_url_fetcher(url: str, *_a: object, **_k: object) -> dict[str, object]:
            engine.default_calls.append(url)
            return {"string": b"", "mime_type": "image/png"}

        self.module.HTML = HTML  # type: ignore[attr-defined]
        self.module.default_url_fetcher = default_url_fetcher  # type: ignore[attr-defined]


def test_st09_22_pdf_fetcher_refuses_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """ST09-22 an injected remote stylesheet is refused; only ``data:`` fetches; no socket."""
    fake = _FakeEngine()
    monkeypatch.setitem(sys.modules, "weasyprint", fake.module)
    sockets: list[object] = []

    def spy(*args: object, **_kwargs: object) -> None:
        sockets.append(args)
        raise AssertionError("network call")  # noqa: EM101 - test guard

    monkeypatch.setattr(socket.socket, "connect", spy)
    monkeypatch.setattr(socket, "create_connection", spy)
    html = ('<html><head><link rel="stylesheet" href="https://evil.example/x.css">'
            '<link rel="stylesheet" href="//evil.example/y.css"></head><body>'
            '<img src="data:image/png;base64,AAAA"><img src="file:///etc/passwd">'
            '<img src="DATA:image/png;base64,BBBB"></body></html>')  # fmt: skip
    assert render._render_pdf(html) == b"%PDF-fake"
    assert fake.default_calls == ["data:image/png;base64,AAAA", "DATA:image/png;base64,BBBB"]
    assert fake.fetched == fake.default_calls
    assert len(fake.refused) == 3
    for exc in fake.refused:
        assert isinstance(exc, EgressBlocked)
        assert exc.message == "report PDF fetch refused"
    assert sockets == []


# --- UT09-35 health -----------------------------------------------------------------------------


def _formats(monkeypatch: pytest.MonkeyPatch, formats: list[str]) -> None:
    cfg = types.SimpleNamespace(app=types.SimpleNamespace(
        reports=types.SimpleNamespace(formats=formats)))  # fmt: skip
    monkeypatch.setattr(render._config, "get_config", lambda: cfg)


def test_ut09_35_health_degraded_without_pdf_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-35 PDF configured and the engine missing → ``degraded``."""
    _formats(monkeypatch, ["html", "pdf"])
    monkeypatch.setitem(sys.modules, "weasyprint", None)
    assert render.health() == ("degraded", "pdf engine missing")


def test_ut09_35_health_ok_and_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-35 all six templates load → ``ok``; a template raising on load → ``down``."""
    _formats(monkeypatch, ["html", "md"])
    assert render.health() == ("ok", "templates loaded")
    _formats(monkeypatch, ["pdf"])
    monkeypatch.setitem(sys.modules, "weasyprint", _FakeEngine().module)
    assert render.health()[0] == "ok"
    env = render.build_environment()

    def broken(name: str) -> jinja2.Template:
        if name == "org_review.md.j2":
            msg = "bad"
            raise jinja2.TemplateSyntaxError(msg, 1)
        return env.get_template(name)

    fake_env = types.SimpleNamespace(get_template=broken)
    monkeypatch.setattr(render, "build_environment", lambda: fake_env)
    assert render.health() == ("down", "templates failed to load: org_review.md.j2")


# --- UT09-33 status gate and argument errors ----------------------------------------------------


def _run_row(status: str) -> None:
    def fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO run (run_id, kind, depth, profile, config_hash, build_id, status,"
            " started_at, token_usage) VALUES (?, 'funding_review', 'standard', 'local',"
            " 'cfg_00000000000000aa', NULL, ?, '2026-09-26T10:00:00.000000Z', '{}')",
            (RUN_ID, status),
        )

    core.run_write(fn, op="test_seed")


@pytest.mark.parametrize("status", ["running", "writing", "failed", "canceled"])
def test_ut09_33_unfinished_run_refused(ops_store: object, status: str) -> None:
    """UT09-33 a run that is not done or partial → ReportContractError ``run_not_finished``."""
    _run_row(status)
    with pytest.raises(ReportContractError, match="run not finished") as info:
        render.render_run(RUN_ID, ["html"], now=NOW)
    assert info.value.details["code"] == "run_not_finished"
    assert info.value.details["status"] == status
    assert info.value.details["run_id"] == RUN_ID


def test_ut09_33_unknown_run_and_formats(ops_store: object) -> None:
    """UT09-33 unknown run → NotFound; unknown or empty formats → ConfigError, nothing read."""
    with pytest.raises(NotFound, match=f"run {RUN_ID} not found"):
        render.render_run(RUN_ID, ["html"], now=NOW)
    with pytest.raises(ConfigError, match="unknown report format: docx"):
        render.render_run(RUN_ID, ["html", "docx"])  # type: ignore[list-item]
    for bad in ([], "html"):
        with pytest.raises(ConfigError, match="non-empty list"):
            render.render_run(RUN_ID, bad)  # type: ignore[arg-type]


def test_ut09_33_draft_missing_and_chat_kind(ops_store: Any) -> None:
    """UT09-33 a done run without draft.json → ``draft_missing``; a chat draft is not rendered."""
    _run_row("done")
    with pytest.raises(ReportContractError) as info:
        render.render_run(RUN_ID, ["html"], now=NOW)
    assert info.value.details["code"] == "draft_missing"
    from tests.support.report_drafts import draft_dict  # noqa: PLC0415

    path = Path(ops_store.data_root) / "reports" / RUN_ID / "draft.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(draft_dict(run_id=RUN_ID, kind="chat")), encoding="utf-8")
    with pytest.raises(ReportContractError, match="not renderable") as info:
        render.render_run(RUN_ID, ["md"], now=NOW)
    assert info.value.details["code"] == "draft_invalid"


def test_ut09_33_out_dir_os_error(tmp_path: Path) -> None:
    """UT09-33 an out_dir that cannot be created → SchemaViolation (F09-01 step 5)."""
    blocker = tmp_path / "file"
    blocker.write_bytes(b"")
    with pytest.raises(SchemaViolation, match="cannot prepare the report output directory"):
        render._prepare_out_dir(blocker / "sub", RUN_ID)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "report.html.1.X.tmp").write_bytes(b"partial")
    (tmp_path / "out" / "keep.txt").write_bytes(b"k")
    assert render._prepare_out_dir(tmp_path / "out", RUN_ID) == (tmp_path / "out").resolve()
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["keep.txt"]


def test_ut09_33_metrics_failure_is_logged(ops_store: object,
                                           monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    """UT09-33 a metric write failure never masks the render error (R-12)."""

    def busy(_samples: object) -> int:
        raise StoreBusy("ops busy")  # noqa: EM101 - test stand-in

    monkeypatch.setattr(render, "record_metric_samples", busy)
    with pytest.raises(NotFound):
        render.render_run(RUN_ID, ["html"], now=NOW)


def test_ut09_33_build_deleted_between_check_and_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-33 a build removed after the existence check is still ``build_retired``."""

    def gone(build_id: str) -> object:
        raise NotFound(f"warehouse build {build_id} not found")  # noqa: EM102 - stand-in

    monkeypatch.setattr(render.warehouse, "build_exists", lambda _build_id: True)
    monkeypatch.setattr(render.warehouse, "open_readonly", gone)
    with pytest.raises(ReportContractError, match="deleted by retention") as info:
        render._open_build("20260926-100000-000001")
    assert info.value.details["code"] == "build_retired"
    assert isinstance(info.value.__cause__, NotFound)


def test_ut09_80_package_exports() -> None:
    """UT09-80 ``herness.reports`` exports ``render_run`` (lazily) and ``ReportManifest``."""
    import herness.reports as package  # noqa: PLC0415

    assert package.render_run is render.render_run
    assert package.ReportManifest is ReportManifest
    with pytest.raises(AttributeError):
        _ = package.no_such_name  # type: ignore[attr-defined]

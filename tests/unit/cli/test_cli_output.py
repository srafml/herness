"""Tests for herness._cli.output: exit codes, envelope, CommandResult (T09-20)."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import typer
from pydantic import BaseModel
from rich.console import Console

from herness._cli import output as o
from herness.cli import GlobalOptions
from herness.core import errors as e
from herness.harness.memory.types import MemoryNotFound
from herness.reports.rules import UserInputError

pytestmark = pytest.mark.unit

UsageError = typer.BadParameter.__mro__[1]  # typer's vendored click UsageError


def _circuit(key: str) -> e.CircuitOpen:
    return e.CircuitOpen("open", key=key, retry_at=datetime(2026, 9, 1, tzinfo=UTC))


def _taxonomy() -> list[tuple[BaseException, int]]:
    return [
        (e.ConfigError("c"), 3),
        (e.AuthError("a"), 4),
        (e.SourceUnavailable("s"), 4),
        (e.RateLimited("r"), 4),
        (_circuit("jira"), 4),
        (e.SchemaViolation("v"), 5),
        (e.NotFound("n"), 7),
        (MemoryNotFound("memory", "mem_1"), 7),
        (e.StoreBusy("b"), 8),
        (e.ModelUnavailable("u"), 9),
        (e.ModelRefused("refused"), 9),
        (_circuit("model:local-30b"), 9),
        (_circuit("decider:openjev"), 9),
        (e.BudgetExceeded("x"), 10),
        (e.PermissionDenied("p"), 11),
        (e.ReportContractError("rc"), 12),
        (e.EgressBlocked("eg"), 13),
        (e.PolicyViolation("pv"), 1),
        (e.QueryError("q"), 1),
        (e.JobStateError("j"), 1),
        (ValueError("x"), 1),
    ]


def test_ut09_63_exit_code_table() -> None:
    """UT09-63 every taxonomy class, usage classes, interrupt and others map per R-46."""
    for exc, code in _taxonomy():
        assert o.exit_code_for(exc) == code, type(exc).__name__
    assert o.exit_code_for(UserInputError("bad")) == 2
    assert o.exit_code_for(UsageError("bad")) == 2
    assert o.exit_code_for(typer.BadParameter("bad")) == 2
    assert o.exit_code_for(KeyboardInterrupt()) == 130
    assert set(o.EXIT_CODES) == {*range(15), 130}
    assert all(isinstance(v, str) and v for v in o.EXIT_CODES.values())


def test_ut09_63_exit_code_for_class_name() -> None:
    """UT09-63 class names give the same codes; CircuitOpen uses its key; unknown -> 1."""
    for exc, code in _taxonomy():
        key = getattr(exc, "key", None) if isinstance(exc, e.CircuitOpen) else None
        assert o.exit_code_for_class_name(type(exc).__name__, key=key) == code
    assert o.exit_code_for_class_name("CircuitOpen") == 4
    assert o.exit_code_for_class_name("NoSuchError") == 1
    assert o.exit_code_for_class_name("") == 1


# --- UT09-64 emit and emit_error ----------------------------------------------------------------


def _result(**kw: object) -> o.CliResult:
    def human(console: Console) -> None:
        console.print("rows: [b]2[/b]")

    base: dict[str, object] = {
        "command": "jobs list",
        "data": {"jobs": [{"job_id": "j1"}]},
        "warnings": ["no worker"],
        "human": human,
    }
    base.update(kw)
    return o.CliResult(**base)  # type: ignore[arg-type]


def test_ut09_64_emit_json(capsys: pytest.CaptureFixture[str]) -> None:
    """UT09-64 JSON success: one stdout line with the cli/1 envelope; nothing on stderr."""
    code = o.emit(GlobalOptions(json=True), _result())
    out, err = capsys.readouterr()
    assert code == 0
    assert out.count("\n") == 1
    assert json.loads(out) == {
        "ok": True,
        "command": "jobs list",
        "data": {"schema": "cli/1", "jobs": [{"job_id": "j1"}]},
        "warnings": ["no worker"],
        "error": None,
    }
    assert err == ""


def test_ut09_64_emit_json_null_data(capsys: pytest.CaptureFixture[str]) -> None:
    """UT09-64 `data=None` stays null; a non-zero exit gives ok false."""
    code = o.emit(GlobalOptions(json=True), _result(data=None, warnings=[], exit_code=6))
    payload = json.loads(capsys.readouterr().out)
    assert code == 6
    assert payload["data"] is None
    assert payload["ok"] is False


def test_ut09_64_emit_human(capsys: pytest.CaptureFixture[str]) -> None:
    """UT09-64 human mode: printer output on stdout, warnings on stderr as `Warning: ...`."""
    code = o.emit(GlobalOptions(), _result(warnings=["no \x1b[31mworker[/b]"]))
    out, err = capsys.readouterr()
    assert code == 0
    assert "rows: 2" in out
    assert err == "Warning: no worker[/b]\n"
    o.emit(GlobalOptions(), _result(human=None, warnings=[]))
    assert capsys.readouterr() == ("", "")


def test_ut09_64_emit_unserialisable() -> None:
    """UT09-64 a value json cannot serialise -> SchemaViolation naming the command."""
    with pytest.raises(e.SchemaViolation, match=r"^cannot serialise output of jobs list$"):
        o.emit(GlobalOptions(json=True), _result(data={"x": object()}))


def test_ut09_64_emit_error_json(capsys: pytest.CaptureFixture[str]) -> None:
    """UT09-64 JSON error envelope: type, exit code, message, hint, details."""
    exc = e.StoreBusy("ops locked", hint="retry", details={"code": "lease"})
    code = o.emit_error(GlobalOptions(json=True), "build", exc)
    out, err = capsys.readouterr()
    assert code == 8
    assert out.count("\n") == 1
    assert json.loads(out) == {
        "ok": False,
        "command": "build",
        "data": None,
        "warnings": [],
        "error": {
            "type": "StoreBusy",
            "exit_code": 8,
            "message": "Ops store is locked.",
            "hint": "Retry; check for a stuck process in `herness status`.",
            "details": {"code": "lease"},
        },
    }
    assert err == ""


def test_ut09_64_emit_error_internal_and_usage(capsys: pytest.CaptureFixture[str]) -> None:
    """UT09-64 unexpected exceptions are `InternalError`; usage errors keep their text, exit 2."""
    assert o.emit_error(GlobalOptions(json=True), "x", RuntimeError("secret text")) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["type"] == "InternalError"
    assert error["message"] == "Unexpected error."
    assert "secret text" not in json.dumps(error)
    assert o.emit_error(GlobalOptions(json=True), "x", UsageError("No such option: --nope")) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert (error["type"], error["exit_code"]) == ("UsageError", 2)
    assert error["message"] == "No such option: --nope"


def _raised(exc: e.HernessError) -> e.HernessError:
    """``exc`` after a raise, so it carries a traceback."""
    try:
        raise exc
    except e.HernessError as caught:
        return caught


def test_ut09_64_emit_error_human(capsys: pytest.CaptureFixture[str]) -> None:
    """UT09-64 human error: `Error:` and `Fix:` on stderr; traceback only with --verbose."""
    caught = _raised(e.ConfigError("bad \x1b]0;title\x07config", hint="edit herness.yaml"))
    assert o.emit_error(GlobalOptions(), "status", caught) == 3
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "Error: bad config\nFix: edit herness.yaml\n"
    o.emit_error(GlobalOptions(verbose=True), "status", caught)
    err = capsys.readouterr().err
    assert err.startswith("Error: bad config\nFix: edit herness.yaml\nTraceback")
    assert "ConfigError" in err


class _Model(BaseModel):
    when: date


def test_ut09_64_json_default() -> None:
    """UT09-64 json_default conversions; other types raise TypeError."""
    stamp = datetime(2026, 9, 1, 12, 30, tzinfo=UTC)
    assert o.json_default(Decimal("1.50")) == "1.50"
    assert o.json_default(stamp) == "2026-09-01T12:30:00Z"
    assert o.json_default(date(2026, 9, 1)) == "2026-09-01"
    assert o.json_default(Path("a") / "b") == "a/b"
    assert o.json_default(_Model(when=date(2026, 9, 1))) == {"when": "2026-09-01"}
    assert o.json_default({"b", "a"}) == ["a", "b"]
    assert o.json_default(frozenset({2, 1})) == [1, 2]
    with pytest.raises(TypeError):
        o.json_default(object())


# --- UT09-102 CommandResult ---------------------------------------------------------------------


@pytest.mark.parametrize("code", [0, 1, 3])
def test_ut09_102_command_result_converts(code: int, capsys: pytest.CaptureFixture[str]) -> None:
    """UT09-102 fields copied into CliResult; envelope ok matches exit code 0."""
    result = o.CommandResult(ok=code == 0, data={"n": 1}, warnings=["w"], exit_code=code)
    cli = result.to_cli_result("config validate")
    assert (cli.command, cli.data, cli.warnings, cli.exit_code) == (
        "config validate",
        {"n": 1},
        ["w"],
        code,
    )
    assert cli.human is not None
    assert o.emit(GlobalOptions(json=True), cli) == code
    assert json.loads(capsys.readouterr().out)["ok"] is (code == 0)
    o.emit(GlobalOptions(), cli)
    assert "n" in capsys.readouterr().out
    with pytest.raises(FrozenInstanceError):
        result.ok = True  # type: ignore[misc]


@pytest.mark.parametrize(("ok", "code"), [(False, 2), (False, 130), (True, 1), (False, 0)])
def test_ut09_102_command_result_rejected(ok: bool, code: int) -> None:
    """UT09-102 exit 2 and 130, or ok disagreeing with the code -> SchemaViolation."""
    result = o.CommandResult(ok=ok, data=None, warnings=[], exit_code=code)
    with pytest.raises(e.SchemaViolation, match=rf"^command doctor returned exit code {code}$"):
        result.to_cli_result("doctor")

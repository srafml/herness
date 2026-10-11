"""IT09-14, IT09-15, IT09-16: the CLI command lifecycle across every command (F09-06; T09-26).

The real Typer app runs in-process (``CliRunner`` for ``--help``, ``herness.cli.main`` for the
rest) on ``cli_env`` (config tree with roles, OS user, keyring) and a migrated ``ops_store``
whose data root is the config's ``paths.data``. Jobs are real ops-store rows run by impl 08's
real ``run_inline`` (``--inline``, R-45; the GPU step stubbed) with test handlers injected for
``build_pipeline``; a followed job is failed behind the CLI's back from the ``clock.sleep`` of
the follow loop. ``--json`` envelopes validate against ``tests/fixtures/cli/cli1.schema.json``
(the ``cli/1`` definition of U09-86). The eval-gate row of IT09-15 is in ``test_cli_eval_it``
(it needs the eval environment's fixtures); the ``report`` row of IT09-16 uses ``report_env``.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import jsonschema
import pytest
import typer.main
from tests.support.cli_env import CliEnv, preserved_app
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle
from tests.support.report_env import FUND_RUN, ReportEnv
from typer.exceptions import Abort
from typer.testing import CliRunner

import herness.core.jobs
from herness import cli
from herness._cli.identity import COMMAND_ROLES
from herness.core import errors as e
from herness.core import time as clock
from herness.core.jobs import inline as jobs_inline
from herness.core.jobs.handlers import register_handler
from herness.core.types import JobOutcome

pytestmark = pytest.mark.integration

REPO: Final = Path(__file__).resolve().parents[3]
SCHEMA: Final[dict[str, Any]] = json.loads(
    (REPO / "tests" / "fixtures" / "cli" / "cli1.schema.json").read_text(encoding="utf-8")
)
RETRY_AT: Final = datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC)


def _main(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    code = exc.value.code
    assert isinstance(code, int)
    return code


def _valid(out: str, shape: str | None = None) -> dict[str, Any]:
    """The one stdout line as an envelope valid against ``cli/1`` (and ``$defs/<shape>``)."""
    lines = out.splitlines()
    assert len(lines) == 1, out
    envelope: dict[str, Any] = json.loads(lines[0])
    jsonschema.Draft202012Validator(SCHEMA).validate(envelope)
    if shape is not None:
        sub = {"$defs": SCHEMA["$defs"], "$ref": f"#/$defs/{shape}"}
        jsonschema.Draft202012Validator(sub).validate(envelope["data"])
    return envelope


class Run:
    """``main`` with captured output: ``(code, stdout, stderr)``."""

    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self.capsys = capsys

    def __call__(self, *args: str) -> tuple[int, str, str]:
        self.capsys.readouterr()
        code = _main(list(args))
        out, err = self.capsys.readouterr()
        return code, out, err


@pytest.fixture
def run(
    cli_env: CliEnv, ops_store: OpsStoreHandle, fake_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> Run:  # fmt: skip
    """Admin `root`, reviewer `rev`, viewer `alice` (the default user); a fixed test
    redaction key (job payloads are redacted); no GPU step."""
    del ops_store
    fake_keyring.store[("herness", "redact.hmac_key")] = bytes(range(32)).hex()
    cli_env.write_config(admins=["root"], reviewers=["rev"])
    monkeypatch.setattr(jobs_inline, "_gpu_controller", lambda *args: None)
    return Run(capsys)


def _jobs(cli_env: CliEnv) -> list[tuple[str, str]]:
    with sqlite3.connect(cli_env.root / "data" / "ops.sqlite") as con:
        return list(con.execute("SELECT kind, status FROM job ORDER BY created_at"))


# --- IT09-14 ------------------------------------------------------------------------------------


def _paths(command: object, prefix: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], bool]]:
    """Every command path under ``command`` with whether it is a group (depth first)."""
    children: dict[str, object] = getattr(command, "commands", {}) or {}
    for name in sorted(children):
        child = children[name]
        path = (*prefix, name)
        yield path, bool(getattr(child, "commands", None))
        yield from _paths(child, path)


def test_it09_14_help_on_every_command_exit_0(cli_env: CliEnv) -> None:
    """IT09-14 ``--help`` on the root, every group and every subcommand of the Typer tree:
    exit 0 with a usage line; the leaves cover the whole R-47 command table."""
    cli_env.write_config()
    runner = CliRunner()
    with preserved_app():
        cli._register_groups(cli.app)
        found = list(_paths(typer.main.get_command(cli.app)))
        for path in [(), *(p for p, _ in found)]:
            result = runner.invoke(cli.app, [*path, "--help"])
            assert result.exit_code == 0, (path, result.output)
            assert "Usage:" in result.output, path
            assert result.exception is None, path
    leaves = {" ".join(path) for path, is_group in found if not is_group}
    groups = {" ".join(path) for path, is_group in found if is_group}
    assert leaves == set(COMMAND_ROLES)
    assert groups >= {"config", "secrets", "deploy", "gpu", "jobs", "memory", "review-queue"}


# --- IT09-15 ------------------------------------------------------------------------------------

# U09-87 rows 3-13, checked by class (subclasses map with their parent), then "other" -> 1.
CLASS_CASES: Final[tuple[tuple[Exception, int], ...]] = (
    (e.ConfigError("bad config"), 3),
    (e.AuthError("bad token"), 4),
    (e.SourceUnavailable("source down"), 4),
    (e.RateLimited("slow down", retry_after=1.0), 4),
    (e.CircuitOpen("open", key="source:servicenow", retry_at=RETRY_AT), 4),
    (e.SchemaViolation("bad row"), 5),
    (e.NotFound("no such run"), 7),
    (e.StoreBusy("locked"), 8),
    (e.ModelUnavailable("no model"), 9),
    (e.ModelRefused("refused"), 9),
    (e.CircuitOpen("open", key="model:local-30b", retry_at=RETRY_AT), 9),
    (e.BudgetExceeded("over budget"), 10),
    (e.PermissionDenied("no role"), 11),
    (e.ReportContractError("uncited"), 12),
    (e.EgressBlocked("blocked"), 13),
    (e.FatalError("fatal"), 1),
    (e.QueryError("bad query"), 1),
    (RuntimeError("boom"), 1),
)
CASE_IDS: Final = [f"{type(exc).__name__}-{code}" for exc, code in CLASS_CASES]


def _kind(exc: Exception) -> str:
    return type(exc).__name__ if isinstance(exc, e.HernessError) else "InternalError"


@pytest.mark.parametrize(("exc", "code"), CLASS_CASES, ids=CASE_IDS)
def test_it09_15_command_error_exit_code_by_class(
    run: Run, monkeypatch: pytest.MonkeyPatch, exc: Exception, code: int
) -> None:
    """IT09-15 a command whose call raises ``exc``: exit by class (3-13), else 1; the JSON
    error envelope names the class (``InternalError`` for a non-Herness error)."""

    def raising(**kwargs: object) -> list[object]:
        del kwargs
        raise exc

    monkeypatch.setattr(herness.core.jobs, "list_jobs", raising)
    got, out, _ = run("--json", "jobs", "list")
    assert got == code
    envelope = _valid(out)
    assert (envelope["ok"], envelope["data"]) == (False, None)
    assert (envelope["error"]["type"], envelope["error"]["exit_code"]) == (_kind(exc), code)
    assert run("jobs", "list")[0] == code  # human mode: same code


@pytest.mark.parametrize(("exc", "code"), CLASS_CASES, ids=CASE_IDS)
def test_it09_15_inline_job_handler_error_exit_code_by_class(
    run: Run, cli_env: CliEnv, exc: Exception, code: int
) -> None:
    """IT09-15 a ``build --inline`` job whose handler raises ``exc``: the job row takes the
    error and the CLI exits by class (a non-Herness error is impl 08's FatalError: 1)."""

    def handler(ctx: object) -> JobOutcome:
        del ctx
        raise exc

    register_handler("build_pipeline", handler)
    cli_env.user = "root"
    got, out, _err = run("--json", "build", "--inline")
    assert got == code
    envelope = _valid(out)
    kind = type(exc).__name__ if isinstance(exc, e.HernessError) else "FatalError"
    assert (envelope["error"]["type"], envelope["error"]["exit_code"]) == (kind, code)
    ((job_kind, status),) = _jobs(cli_env)
    assert job_kind == "build_pipeline"
    retryable = isinstance(exc, e.RetryableError)
    assert status == ("queued" if retryable else "failed")  # impl 08 requeues a retryable error


FOLLOWED: Final = (
    ({"class": "MemoryNotFound", "message": "gone"}, 7),  # taxonomy outside herness.core.errors
    ({"class": "SourceUnavailable", "message": "down"}, 4),
    ({"class": "CircuitOpen", "key": "decider:openjev", "message": "open"}, 9),
    ({"class": "BudgetExceeded", "message": "over"}, 10),
    ({"class": "NoSuchErrorClass", "message": "?"}, 1),
)


@pytest.mark.parametrize(("error", "code"), FOLLOWED, ids=[str(c[0]["class"]) for c in FOLLOWED])
def test_it09_15_followed_job_failed_exit_code_by_class_name(
    run: Run, cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch, error: dict[str, str], code: int
) -> None:
    """IT09-15 ``build`` (wait) whose job a worker fails: exit from ``last_error.class``
    (``key`` for CircuitOpen); an unknown class -> 1; the envelope keeps the job data."""
    db = cli_env.root / "data" / "ops.sqlite"

    def fail_job(seconds: float) -> None:  # the worker failing the job between two polls
        del seconds
        stamp = clock.format_utc(clock.now())
        with sqlite3.connect(db) as con:
            con.execute("UPDATE job SET status = 'failed', last_error = ?, finished_at = ?",
                        (json.dumps(error), stamp))  # fmt: skip

    monkeypatch.setattr(clock, "sleep", fail_job)
    cli_env.user = "root"
    got, out, err = run("--json", "build")
    assert got == code
    envelope = _valid(out, "job_data")
    assert envelope["error"]["type"] == error["class"]
    assert envelope["error"]["exit_code"] == code
    assert envelope["data"]["status"] == "failed"
    assert "No worker is running" in err


def test_it09_15_doctor_fail_exit_1(
    cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT09-15 ``doctor`` with a FAIL row (no ops store under ``paths.data``): exit 1, even
    for a ``denied`` user; the envelope is valid ``cli/1`` doctor data."""
    from herness.admin import doctor_host  # noqa: PLC0415 - patched where used

    monkeypatch.setattr(doctor_host, "doctor_checks", lambda cfg: [])  # no host subprocesses
    cli_env.write_config(admins=["root"], default_role="denied")
    capsys.readouterr()
    assert _main(["--json", "doctor"]) == 1
    envelope = _valid(capsys.readouterr().out, "doctor_data")
    rows = {row["name"]: row["status"] for row in envelope["data"]["checks"]}
    assert "FAIL" in rows.values()
    assert envelope["data"]["summary"]["FAIL"] >= 1
    assert envelope["ok"] is False


def test_it09_15_usage_errors_exit_2(run: Run, cli_env: CliEnv) -> None:
    """IT09-15 usage errors exit 2: an unknown option, an out-of-range value, a missing
    argument (click ``UsageError``) and ``--inline --no-wait`` (``UserInputError``); nothing
    is enqueued."""
    cli_env.user = "root"
    for args in (["jobs", "list", "--bogus"], ["jobs", "list", "--limit", "0"],
                 ["decide"], ["no-such-command"]):  # fmt: skip
        assert run(*args)[0] == 2, args
    code, out, _err = run("--json", "build", "--inline", "--no-wait")
    assert code == 2
    assert _valid(out)["error"]["exit_code"] == 2
    assert run("--json", "jobs", "list", "--limit", "0")[0] == 2
    assert _jobs(cli_env) == []


def test_it09_15_partial_run_exit_6(run: Run, cli_env: CliEnv) -> None:
    """IT09-15 a job that finishes with ``partial: true``: exit 6 with the partial warning;
    the envelope carries the job data with ``partial: true`` (R-46)."""

    def handler(ctx: object) -> JobOutcome:
        del ctx
        return JobOutcome(status="done", result={"run_id": "run_partial", "partial": True})

    register_handler("build_pipeline", handler)
    cli_env.user = "root"
    code, out, err = run("--json", "build", "--inline")
    assert code == 6
    envelope = _valid(out, "job_data")
    assert envelope["data"]["partial"] is True
    assert envelope["warnings"] == [
        "Run `run_partial` finished partial: some tasks did not finish."
    ]
    code, _out, err = run("build", "--inline")
    assert code == 6
    assert "Warning: Run `run_partial` finished partial" in err


def _raise_in_command(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    def raising(**kwargs: object) -> list[object]:
        del kwargs
        raise exc

    monkeypatch.setattr(herness.core.jobs, "list_jobs", raising)


def test_it09_15_interrupt_inside_a_command_exit_130(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT09-15 (T09-20 M-5) Ctrl+C inside a command body (click turns it into ``Abort``):
    exit 130 per R-46, no ``cli.command.failed`` error and no error envelope."""
    _raise_in_command(monkeypatch, KeyboardInterrupt())
    code, out, err = run("--json", "jobs", "list")
    assert code == 130
    assert out == ""
    assert "cli.command.failed" not in err


def _prompt_raising(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    """``typer.prompt`` as click's own prompt ends on EOF or Ctrl+C: ``Abort`` from it."""

    def prompt(*args: object, **kwargs: object) -> str:
        del args, kwargs
        try:
            raise exc
        except (EOFError, KeyboardInterrupt):
            raise Abort from None  # click's termui: the original stays as __context__

    monkeypatch.setattr("typer.prompt", prompt)


@pytest.fixture
def secrets_admin(cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch) -> CliEnv:
    """``root`` in an elevated shell (``secrets set`` is an elevated command)."""
    from herness._cli import identity  # noqa: PLC0415 - patched where used

    monkeypatch.setattr(identity, "_is_elevated", lambda: True)
    cli_env.user = "root"
    return cli_env


def test_it09_15_eof_at_a_prompt_exit_2(
    run: Run, secrets_admin: CliEnv, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT09-15 (T09-20 M-5) end of input at the hidden prompt of ``secrets set`` (``Abort``):
    usage exit 2 with the Error/Fix lines (``UserInputError`` envelope with ``--json``), no
    ``cli.command.failed`` error log, nothing stored; also for an ``EOFError`` that escapes a
    command body (Typer's ``Abort from EOFError``)."""
    del secrets_admin
    before = dict(fake_keyring.store)
    _prompt_raising(monkeypatch, EOFError())
    code, out, err = run("secrets", "set", "snow.token")
    assert (code, out) == (2, "")
    assert "Error: input ended before the prompt was answered" in err
    assert "Fix: Run the command in a terminal and answer the prompt." in err
    assert "cli.command.failed" not in err
    _raise_in_command(monkeypatch, EOFError())
    code, out, err = run("--json", "jobs", "list")
    assert code == 2
    error = _valid(out)["error"]
    assert (error["type"], error["exit_code"]) == ("UserInputError", 2)
    assert "cli.command.failed" not in err
    assert fake_keyring.store == before


def test_it09_15_ctrl_c_at_a_prompt_exit_130(
    run: Run, secrets_admin: CliEnv, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT09-15 (T09-20 M-5) Ctrl+C at the hidden prompt (``Abort`` with the interrupt as its
    context): exit 130, no error envelope, no error log, nothing stored."""
    del secrets_admin
    before = dict(fake_keyring.store)
    _prompt_raising(monkeypatch, KeyboardInterrupt())
    code, out, err = run("secrets", "set", "snow.token")
    assert (code, out) == (130, "")
    assert "cli.command.failed" not in err
    assert fake_keyring.store == before


# --- IT09-16 ------------------------------------------------------------------------------------


def test_it09_16_schema_fixture_is_valid_json_schema() -> None:
    """IT09-16 ``tests/fixtures/cli/cli1.schema.json`` is a valid 2020-12 JSON Schema."""
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    assert set(SCHEMA["$defs"]) >= {"list_data", "job_data", "report_data", "doctor_data"}


def test_it09_16_json_list_job_and_doctor_commands_validate(
    run: Run, cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT09-16 ``--json`` on list commands (jobs, metrics, review-queue), job commands
    (``--no-wait``, ``--inline`` done, failed) and ``doctor``: stdout parses and validates."""
    from herness.admin import doctor_host  # noqa: PLC0415 - patched where used

    monkeypatch.setattr(doctor_host, "doctor_checks", lambda cfg: [])
    outcomes: list[Callable[[], JobOutcome]] = [
        lambda: JobOutcome(status="done", result={"build_id": "b1", "promoted": True}),
    ]

    def handler(ctx: object) -> JobOutcome:
        del ctx
        return outcomes[0]()

    register_handler("build_pipeline", handler)
    cli_env.user = "root"
    code, out, _ = run("--json", "build", "--no-wait")
    assert code == 0
    queued = _valid(out, "job_data")["data"]
    assert queued["status"] == "queued"
    code, out, _ = run("--json", "build", "--inline")
    assert code == 0
    done = _valid(out, "job_data")
    assert (done["ok"], done["data"]["status"], done["data"]["partial"]) == (True, "done", False)
    code, out, _ = run("--json", "jobs", "list")
    assert code == 0
    listed = _valid(out, "list_data")["data"]["jobs"]
    assert {row["job_id"] for row in listed} == {queued["job_id"], done["data"]["job_id"]}
    code, out, _ = run("--json", "metrics", "list")
    assert code == 0
    assert _valid(out, "list_data")["data"]["metrics"]
    cli_env.user = "rev"
    code, out, _ = run("--json", "review-queue", "list")
    assert code == 0
    assert _valid(out, "list_data")["data"]["items"] == []
    code, out, _ = run("--json", "doctor")
    assert code in (0, 1)
    assert _valid(out, "doctor_data")["data"]["checks"]


def test_it09_16_json_report_render_validates(
    cli_env: CliEnv, report_env: ReportEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT09-16 ``--json report render RUN`` (a report command): the envelope validates and
    its data has every ``ReportManifest`` field."""
    cli_env.write_config(admins=["root"])
    capsys.readouterr()
    assert _main(["--json", "report", "render", FUND_RUN]) == 0
    envelope = _valid(capsys.readouterr().out, "report_data")
    assert envelope["data"]["run_id"] == FUND_RUN
    assert envelope["ok"] is True
    assert (report_env.reports_root / FUND_RUN / "report.html").is_file()

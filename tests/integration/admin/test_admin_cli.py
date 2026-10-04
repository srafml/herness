"""IT10-11: ``herness config validate`` end to end through the real root app (impl 10 U10-65).

``herness._cli.cmd_admin`` (T09-24) is not on this branch yet, so the test registers a
test-local ``config validate`` command on ``herness.cli.app`` that does what impl 09 U09-98
specifies (guarded, options ``--profile/--offline/--strict``, the ``CommandResult`` converted
and emitted) and drives it through ``herness.cli.main``. T09-24 replaces the wrapper.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, cast

import pytest
import typer
from tests.support.cli_env import CliEnv
from tests.support.config_tree import register_checked_names, write_checked_config

from herness import cli
from herness._cli.identity import guarded
from herness._cli.output import emit
from herness.admin.commands_config import OFFLINE_HASH_WARNING, cmd_config_validate
from herness.reports.rules import Actor

pytestmark = pytest.mark.integration

HASH_RE = re.compile(r"^cfg_[0-9a-f]{16}$")
LINE_RE = re.compile(r"^(error|warn) \S+ \S+: .+$")
PATH = "config validate"


def _config_validate(
    ctx: typer.Context,
    actor: Actor | None,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    offline: Annotated[bool, typer.Option("--offline")] = False,
    strict: Annotated[bool, typer.Option("--strict")] = False,
) -> int:
    """Stand-in for the T09-24 ``config validate`` command (U09-98)."""
    del actor  # role `any`, denied users included (DENIED_ALLOWED)
    opts = cast("cli.GlobalOptions", ctx.find_root().obj)
    result = cmd_config_validate(
        config_dir=opts.config_dir,
        profile=profile,  # type: ignore[arg-type]
        offline=offline,
        strict=strict,
    )
    return emit(opts, result.to_cli_result(PATH))


@pytest.fixture
def registered(cli_env: CliEnv) -> Iterator[CliEnv]:
    """Register ``config validate`` on the root app; restore the app's groups after."""
    saved = list(cli.app.registered_groups)
    group = typer.Typer(name="config")
    group.command("validate")(guarded(PATH)(_config_validate))
    cli.app.add_typer(group, name="config")
    register_checked_names()
    try:
        yield cli_env
    finally:
        cli.app.registered_groups[:] = saved


@pytest.fixture
def validate_app(registered: CliEnv) -> Path:
    """A clean, pinned config dir (every offline cross-check passes)."""
    return write_checked_config(registered.root)


def _run(cfg_dir: Path, *extra: str) -> int:
    args = ["--config-dir", str(cfg_dir), "--json", "config", "validate", *extra]
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert isinstance(exc.value.code, int)
    return exc.value.code


def _envelope(capsys: pytest.CaptureFixture[str], code: int) -> dict[str, object]:
    out = capsys.readouterr().out
    assert out.count("\n") == 1  # exactly one JSON object on stdout (TH09-22)
    payload = json.loads(out)
    assert set(payload) == {"ok", "command", "data", "warnings", "error"}
    assert payload["ok"] is (code == 0)
    assert payload["command"] == PATH
    assert payload["error"] is None
    data = payload["data"]
    assert set(data) == {"schema", "profile", "config_hash", "issues"}
    assert data["schema"] == "cli/1"
    assert data["profile"] == "local"
    assert all(isinstance(line, str) and LINE_RE.fullmatch(line) for line in data["issues"])
    assert isinstance(payload["warnings"], list)
    return cast("dict[str, object]", data)


def _edit(cfg_dir: Path, old: str, new: str) -> None:
    path = cfg_dir / "herness.yaml"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_it10_11_clean_config_exits_0(
    validate_app: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT10-11 clean config: --offline --strict exits 0; JSON envelope validates."""
    code = _run(validate_app, "--offline", "--strict")
    assert code == 0
    data = _envelope(capsys, code)
    assert HASH_RE.fullmatch(str(data["config_hash"]))
    assert data["issues"] == [f"warn config_hash -: {OFFLINE_HASH_WARNING}"]


def test_it10_11_warning_only_config_exits_1(
    validate_app: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT10-11 warning-only config: --offline --strict exits 1 (R-46); 0 without --strict."""
    _edit(validate_app, "d" * 64, '"<sha256>"')
    code = _run(validate_app, "--offline", "--strict")
    assert code == 1
    data = _envelope(capsys, code)
    assert HASH_RE.fullmatch(str(data["config_hash"]))
    assert "warn deploy.large.sha256 herness.yaml: " in str(data["issues"])
    assert _run(validate_app, "--offline") == 0
    _envelope(capsys, 0)


def test_it10_11_error_config_exits_1(
    validate_app: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT10-11 error config (fails to load): --offline --strict exits 1, not 3 (R-46)."""
    _edit(validate_app, "logging: {level: INFO}", "logging: {level: LOUD}")
    code = _run(validate_app, "--offline", "--strict")
    assert code == 1
    data = _envelope(capsys, code)
    assert data["config_hash"] is None
    issues = data["issues"]
    assert isinstance(issues, list)
    assert issues[0].startswith("error logging.level herness.yaml: ")


def test_it10_11_denied_user_may_validate(
    registered: CliEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT10-11 config validate is allowed for a denied user (DENIED_ALLOWED, role any)."""
    cfg_dir = registered.write_config(admins=["root-admin"], default_role="denied")
    assert _run(cfg_dir, "--offline", "--profile", "synth") == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["ok"], out["error"]) == (True, None)
    assert out["data"]["profile"] == "synth"
    assert out["data"]["issues"]  # the unpinned deploy placeholders of this tree warn (C13)
    assert _run(cfg_dir, "--offline", "--strict", "--profile", "synth") == 1

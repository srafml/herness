"""Security tests for ``herness ui`` (T09-22; TH09-05, TH09-09, TH09-28).

The Streamlit child is never started: ``cmd_system._runner`` is replaced by a spy that records
the argument list and environment.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.support.cli_env import CliEnv

from herness import cli
from herness._cli import cmd_system
from herness.core.config import HernessConfig

pytestmark = pytest.mark.unit


def _main(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert isinstance(exc.value.code, int)
    return exc.value.code


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, Any]]]:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def spy(args: list[str], **kw: Any) -> SimpleNamespace:
        calls.append((args, kw))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cmd_system, "_runner", spy)
    return calls


def _flags(argv: list[str]) -> dict[str, str]:
    return {argv[i]: argv[i + 1] for i in range(5, len(argv), 2)}


def _set_bind(cfg_dir: Path, bind: str, *, expose: bool) -> None:
    path = cfg_dir / "herness.yaml"
    proxy = ", expose: {enabled: true, trusted_proxy: 127.0.0.1}" if expose else ""
    text = path.read_text(encoding="utf-8").replace(
        "  ui: {roles:", f"  ui: {{bind: '{bind}'{proxy}, roles:", 1
    )
    path.write_text(text, encoding="utf-8")


def test_st09_23_ui_hardening_flags(
    cli_env: CliEnv, launched: list[tuple[list[str], dict[str, Any]]]
) -> None:
    """ST09-23 `herness ui` argument list: XSRF true, CORS true, usage stats false, headless
    true, bind from config; telemetry and hub access off in the child environment."""
    cfg_dir = cli_env.write_config(admins=["alice"])
    _set_bind(cfg_dir, "::1", expose=False)
    assert _main(["--config-dir", str(cfg_dir), "ui"]) == 0
    (argv, kw), = launched  # fmt: skip
    flags = _flags(argv)
    assert flags["--server.enableXsrfProtection"] == "true"
    assert flags["--server.enableCORS"] == "true"
    assert flags["--browser.gatherUsageStats"] == "false"
    assert flags["--server.headless"] == "true"
    assert flags["--server.address"] == "::1"
    assert (kw["env"]["HF_HUB_OFFLINE"], kw["env"]["DO_NOT_TRACK"]) == ("1", "1")


@pytest.mark.parametrize("expose", [False, True])
def test_st09_06_non_loopback_bind_refused(
    cli_env: CliEnv, launched: list[tuple[list[str], dict[str, Any]]], expose: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """ST09-06 (CLI part) a non-loopback bind, exposure off or on: `herness ui` exits 3
    (ConfigError) and nothing starts."""
    cfg_dir = cli_env.write_config(admins=["alice"])
    _set_bind(cfg_dir, "0.0.0.0", expose=expose)  # noqa: S104 - the attack under test
    assert _main(["--config-dir", str(cfg_dir), "ui"]) == 3
    assert "Error:" in capsys.readouterr().err
    assert launched == []


def test_st09_06_ui_repeats_the_loopback_check(
    cli_env: CliEnv, launched: list[tuple[list[str], dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """ST09-06 (CLI part) a config that slipped past spec 10 validation with a LAN bind is
    still refused by `ui` itself: "dashboard bind must be loopback", exit 3."""
    cfg_dir = cli_env.write_config(admins=["alice"])
    real = cli.GlobalOptions.config

    def lan(self: cli.GlobalOptions, *, startup_validation: bool = True) -> HernessConfig:
        cfg = real(self, startup_validation=startup_validation)
        ui = cfg.security.ui.model_copy(update={"bind": "192.168.1.20"})
        security = cfg.security.model_copy(update={"ui": ui})
        return cfg.model_copy(update={"security": security})

    monkeypatch.setattr(cli.GlobalOptions, "config", lan)
    assert _main(["--config-dir", str(cfg_dir), "ui"]) == 3
    assert "dashboard bind must be loopback" in capsys.readouterr().err
    assert launched == []

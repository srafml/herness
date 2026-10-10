"""ST10-62: a failed blue/green install never moves ``current`` (impl 10 U10-88, U10-112;
TH10-36, TH10-37, TH10-40; Ruling T10-26c).

The running venv is ``<tmp>/inst/venvs/1.0.0`` behind a real ``current`` junction (symlink on
POSIX); ``fake_runner`` answers ``gh``, ``python -m venv``, ``uv`` and the new interpreter.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import structlog
from tests.support.fake_runner import FakeRunner
from tests.support.fake_venv import make_venv, use_install_root, venv_answer
from tests.support.release_bundle import EXT_BYTES, build_bundle, fake_gh, init_release_config

from herness.admin import _install_venv, attest
from herness.admin._install_venv import HISTORY_CLASS, rollback_venv
from herness.admin.attest import ReleaseBundle, verify_bundle
from herness.admin.deploy import DeployHistory
from herness.admin.duckdb_ext import venv_python
from herness.admin.wsl import CmdResult
from herness.core import time as clock
from herness.core.errors import ConfigError, FatalError

pytestmark = pytest.mark.unit

USER = "0f" * 16


def _link(target: Path, at: Path) -> None:
    if sys.platform == "win32":
        import _winapi  # noqa: PLC0415

        _winapi.CreateJunction(str(target), str(at))
    else:
        at.symlink_to(target, target_is_directory=True)


def _real(path: Path | str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _gh(argv: list[str]) -> CmdResult:
    code, out, err = fake_gh(argv[1:])
    return CmdResult(code, out, err, 0.0)


@pytest.fixture
def site(tmp_path: Path, fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> Any:
    root = tmp_path / "inst"
    make_venv(root / "venvs" / "1.0.0")
    _link(root / "venvs" / "1.0.0", root / "current")
    running = use_install_root(monkeypatch, root, "current")
    fake_runner.add(["gh", "attestation", "verify"], _gh)
    fake_runner.add([sys.executable, "-I", "-m", "venv"], venv_answer)
    fake_runner.add(["uv", "pip"], CmdResult(0, "", "", 0.1))
    monkeypatch.setattr(attest, "worker_alive", lambda: False)
    monkeypatch.setattr(_install_venv, "worker_alive", lambda: False)
    built = build_bundle(tmp_path / "bundle")
    cfg = init_release_config(tmp_path / "cfg", ext_sha256=built.ext_sha256)
    return root, running, ReleaseBundle.load(built.root), cfg, fake_runner


def _unchanged(root: Path) -> None:
    """``current`` still names the old venv, the partial venv and every temp link are gone,
    and the deploy history records no switch."""
    assert _real(root / "current") == _real(root / "venvs" / "1.0.0")
    assert sorted(p.name for p in root.iterdir()) == ["current", "venvs"]
    assert sorted(p.name for p in (root / "venvs").iterdir()) == ["1.0.0"]
    assert DeployHistory().last_promoted(HISTORY_CLASS) == []


@pytest.mark.parametrize("failure", ["exit", "not-loaded", "installed-hash"])
def test_st10_62_failed_extension_install_keeps_current(
    tmp_path: Path, site: Any, failure: str
) -> None:
    """ST10-62 the DuckDB extension install in the new venv fails (non-zero exit, extension
    not loaded, installed file differs from the pin): ``FatalError``, ``current`` unchanged,
    the partial ``venvs/1.2.3`` removed, no history entry."""
    root, running, bundle, cfg, runner = site
    installed = tmp_path / "home" / "excel.duckdb_extension"
    installed.parent.mkdir()
    installed.write_bytes(EXT_BYTES + (b"x" if failure == "installed-hash" else b""))
    row = {"loaded": failure != "not-loaded", "installed": True, "extension_version": "v1"}
    row["install_path"] = str(installed)
    code = 1 if failure == "exit" else 0
    runner.add(
        [str(venv_python(root / "venvs" / "1.2.3"))], CmdResult(code, json.dumps(row), "", 0)
    )
    summary = verify_bundle(bundle, cfg)
    with structlog.testing.capture_logs() as logs, pytest.raises(FatalError, match="excel"):
        attest.install_bundle(bundle, summary, venv=running, actor=USER)
    _unchanged(root)
    events = [e["event"] for e in logs]
    assert "deploy.install.refused" in events
    assert "deploy.install.venv_removed" in events
    assert "deploy.install.switched" not in events


@pytest.mark.parametrize("step", ["venv", "sync", "wheel"])
def test_st10_62_failed_venv_or_uv_step_keeps_current(site: Any, step: str) -> None:
    """ST10-62 ``python -m venv``, ``uv pip sync`` or ``uv pip install`` failing leaves
    ``current`` unchanged and removes the partial venv."""
    root, running, bundle, cfg, runner = site
    prefix = {
        "venv": [sys.executable, "-I", "-m", "venv"],
        "sync": ["uv", "pip", "sync"],
        "wheel": ["uv", "pip", "install"],
    }[step]

    def fail(argv: list[str]) -> CmdResult:
        if step == "venv":
            make_venv(Path(argv[-1]))  # a half-made venv is still removed
        return CmdResult(1, "", "boom", 0.1)

    runner.add(prefix, fail)
    with pytest.raises(FatalError, match=r"failed: exit 1$"):
        attest.install_bundle(bundle, verify_bundle(bundle, cfg), venv=running, actor=USER)
    _unchanged(root)


def test_st10_62_rollback_refuses_a_target_outside_venvs(site: Any) -> None:
    """ST10-62 (TH10-40) a deploy-history line whose ``previous`` names a directory outside
    ``<install root>/venvs`` (or one without an interpreter) is never linked as ``current``."""
    root, running, *_ = site
    outside = root.parent / "evil"
    make_venv(outside)
    for previous in (outside, root / "venvs" / "missing"):
        pins = {"version": "1.0.0", "venv": str(root / "venvs" / "1.0.0")}
        pins["previous"] = str(previous)
        DeployHistory().record(HISTORY_CLASS, pins, "cfg_0", clock.now())
        with pytest.raises(ConfigError, match=r"^no previous venv to roll back to$"):
            rollback_venv(running, actor=USER)
        assert _real(root / "current") == _real(root / "venvs" / "1.0.0")

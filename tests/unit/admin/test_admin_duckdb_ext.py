"""UT10-85: offline DuckDB extension install from the release bundle (impl 10 U10-112).

``fake_runner`` stands in for the venv interpreter and returns recorded ``duckdb_extensions()``
JSON; the runner allowlist case covers the U10-76 extension this unit needs.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import structlog
from tests.support.fake_runner import FakeRunner
from tests.support.release_bundle import EXT_BYTES, BundleSpec, build_bundle, init_release_config

from herness.admin import duckdb_ext, wsl
from herness.admin.attest import ReleaseBundle
from herness.admin.duckdb_ext import EXT_INSTALL_SCRIPT, install_duckdb_extensions, venv_python
from herness.admin.wsl import CmdResult
from herness.core.errors import ConfigError, FatalError

pytestmark = pytest.mark.unit


def recorded(install_path: Path, *, loaded: bool = True) -> CmdResult:
    row = {
        "loaded": loaded,
        "installed": True,
        "extension_version": "v1.5.5",
        "install_path": str(install_path),
    }
    return CmdResult(0, json.dumps(row), "", 0.4)


@pytest.fixture
def setup(tmp_path: Path) -> tuple[ReleaseBundle, Path, str]:
    built = build_bundle(tmp_path / "bundle")
    assert built.ext_sha256 is not None
    installed = tmp_path / "home" / ".duckdb" / "excel.duckdb_extension"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(EXT_BYTES)
    return ReleaseBundle.load(built.root), installed, built.ext_sha256


def test_ut10_85_right_pin_installs(
    tmp_path: Path, setup: tuple[ReleaseBundle, Path, str], fake_runner: FakeRunner
) -> None:
    """UT10-85 exact argv (``-I``, EXT_INSTALL_SCRIPT, path, ``excel``); version returned and
    ``deploy.install.extension_installed`` logged."""
    bundle, installed, pin = setup
    cfg = init_release_config(tmp_path, ext_sha256=pin)
    venv = tmp_path / "venv"
    python = str(venv_python(venv))
    fake_runner.add([python], recorded(installed))
    with structlog.testing.capture_logs() as logs:
        versions = install_duckdb_extensions(bundle, cfg, venv=venv)
    assert versions == {"excel": "v1.5.5"}
    ext = str(bundle.duckdb_extensions["excel"])
    assert fake_runner.argvs() == [[python, "-I", "-c", EXT_INSTALL_SCRIPT, ext, "excel"]]
    assert fake_runner.calls[0].timeout_s == 120
    done = [e for e in logs if e["event"] == "deploy.install.extension_installed"]
    assert len(done) == 1
    assert done[0]["log_level"] == "info"
    assert (done[0]["name"], done[0]["extension_version"], done[0]["sha256"]) == (
        "excel",
        "v1.5.5",
        pin,
    )


def test_ut10_85_wrong_pin_fails_before_subprocess(
    tmp_path: Path, setup: tuple[ReleaseBundle, Path, str], fake_runner: FakeRunner
) -> None:
    """UT10-85 a pin that differs from the bundled file → FatalError before any subprocess."""
    bundle, _, _ = setup
    cfg = init_release_config(tmp_path, ext_sha256="e" * 64)
    with structlog.testing.capture_logs() as logs, pytest.raises(FatalError) as caught:
        install_duckdb_extensions(bundle, cfg, venv=tmp_path / "venv")
    assert str(caught.value) == "duckdb extension hash mismatch: excel"
    assert fake_runner.calls == []
    refused = [e for e in logs if e["event"] == "deploy.install.refused"]
    assert [(e["reason"], e["log_level"]) for e in refused] == [("extension_hash", "error")]


def test_ut10_85_no_pin_raises_config_error(
    tmp_path: Path, setup: tuple[ReleaseBundle, Path, str], fake_runner: FakeRunner
) -> None:
    """UT10-85 no ``deploy.release.duckdb_extensions.excel`` → ConfigError."""
    bundle, _, _ = setup
    cfg = init_release_config(tmp_path, ext_sha256=None)
    with pytest.raises(ConfigError, match=r"^deploy\.release\.duckdb_extensions\.excel is not"):
        install_duckdb_extensions(bundle, cfg, venv=tmp_path / "venv")
    assert fake_runner.calls == []


def test_ut10_85_bundle_without_extension_file(tmp_path: Path, fake_runner: FakeRunner) -> None:
    """UT10-85 a pinned extension missing from the bundle → ConfigError naming the file."""

    built = build_bundle(tmp_path / "bundle", BundleSpec(ext_bytes=None))
    cfg = init_release_config(tmp_path, ext_sha256="e" * 64)
    with pytest.raises(ConfigError, match=r"duckdb/excel\.duckdb_extension$"):
        install_duckdb_extensions(ReleaseBundle.load(built.root), cfg, venv=tmp_path / "v")
    assert fake_runner.calls == []


def test_ut10_85_differing_installed_file_fails(
    tmp_path: Path, setup: tuple[ReleaseBundle, Path, str], fake_runner: FakeRunner
) -> None:
    """UT10-85 the file at ``install_path`` differs from the pin → FatalError."""
    bundle, installed, pin = setup
    installed.write_bytes(EXT_BYTES + b"tampered")
    cfg = init_release_config(tmp_path, ext_sha256=pin)
    venv = tmp_path / "venv"
    fake_runner.add([str(venv_python(venv))], recorded(installed))
    with pytest.raises(FatalError, match="excel"):
        install_duckdb_extensions(bundle, cfg, venv=venv)
    missing = recorded(tmp_path / "nowhere.duckdb_extension")
    fake_runner.table.clear()
    fake_runner.add([str(venv_python(venv))], missing)
    with pytest.raises(FatalError, match="excel"):
        install_duckdb_extensions(bundle, cfg, venv=venv)


@pytest.mark.parametrize(
    "result",
    [
        CmdResult(1, "", "Invalid Input Error: signature", 0.1),
        CmdResult(0, "not json", "", 0.1),
        CmdResult(0, json.dumps({"loaded": False, "installed": True}), "", 0.1),
    ],
)
def test_ut10_85_failed_install_raises(
    tmp_path: Path,
    setup: tuple[ReleaseBundle, Path, str],
    fake_runner: FakeRunner,
    result: CmdResult,
) -> None:
    """UT10-85 non-zero exit, unreadable output or an unloaded extension → FatalError."""
    bundle, _, pin = setup
    cfg = init_release_config(tmp_path, ext_sha256=pin)
    venv = tmp_path / "venv"
    fake_runner.add([str(venv_python(venv))], result)
    with pytest.raises(FatalError, match=r"^duckdb extension install failed: excel$"):
        install_duckdb_extensions(bundle, cfg, venv=venv)


def test_ut10_85_venv_python_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-85 the venv interpreter path follows the platform layout."""

    monkeypatch.setattr(duckdb_ext.os, "name", "nt")
    assert venv_python(tmp_path) == tmp_path / "Scripts" / "python.exe"
    monkeypatch.setattr(duckdb_ext.os, "name", "posix")
    assert venv_python(tmp_path) == tmp_path / "bin" / "python"


def test_ut10_85_runner_allows_only_absolute_venv_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT10-85 run_cmd accepts the venv interpreter by absolute path (U10-76 extension) and
    still refuses a bare or misplaced ``python``."""
    started: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[bytes]:
        started.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"{}", b"")

    monkeypatch.setattr(wsl.shutil, "which", lambda name: name)
    monkeypatch.setattr(wsl.subprocess, "run", fake_run)
    for good in (r"C:\herness\.venv\Scripts\python.exe", "/opt/herness/.venv/bin/python"):
        assert wsl.run_cmd([good, "-I", "-c", "pass"], timeout_s=5).returncode == 0
    for bad in ("python", "python.exe", r"C:\Windows\python.exe", r"C:\v\Scripts\pythonw.exe"):
        with pytest.raises(ConfigError, match="command not allowed"):
            wsl.run_cmd([bad, "-c", "pass"], timeout_s=5)
    assert len(started) == 2

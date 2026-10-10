"""IT10-14: ``herness deploy install`` end to end into a temporary venv (impl 10 F10-07).

A ``uv venv`` under ``tmp_path`` plays the running Herness venv; the install builds the new
blue/green venv ``tmp_path/venvs/1.2.3`` with the real ``python -m venv`` and points the
``tmp_path/current`` junction (symlink on POSIX) at it (Ruling T10-26c). Real ``uv`` (offline,
from the local cache) syncs a hashed ``requirements.txt`` and installs a fixture wheel there;
``gh`` is a fake program on ``PATH`` that runs ``fake_gh`` and returns recorded JSON. Only the
new venv interpreter's DuckDB step is answered by ``fake_runner`` (the fixture extension is not
a signed DuckDB build; the real ``excel`` load is the card's recorded acceptance check).
Nothing touches the network or the dev ``.venv``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from tests.support.fake_runner import FakeRunner
from tests.support.fake_venv import use_install_root
from tests.support.release_bundle import (
    EXT_BYTES,
    BundleSpec,
    build_bundle,
    init_release_config,
    write_gh_shim,
)

from herness.admin import attest, commands_deploy
from herness.admin.commands_deploy import cmd_deploy_install
from herness.admin.duckdb_ext import venv_python
from herness.admin.wsl import CmdResult

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
USER = "0f" * 16  # an audit actor: 32 hex characters


def _locked(name: str) -> tuple[str, str]:
    """Version and wheel SHA-256 of ``name`` from ``uv.lock`` (so uv finds it in its cache)."""
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    pkg = next(p for p in lock["package"] if p["name"] == name)
    return pkg["version"], pkg["wheels"][0]["hash"].removeprefix("sha256:")


def test_it10_14_deploy_install_into_temp_venv(
    tmp_path: Path, fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT10-14 cmd_deploy_install: wheel installed, audit ``deploy_install`` written and
    ``.release/<version>/`` created."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not on PATH")
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    venv = tmp_path / "venv"
    subprocess.run(  # noqa: S603 - test setup, fixed argv
        [uv, "venv", "--offline", "--quiet", "--python", sys.executable, str(venv)], check=True
    )
    bin_dir = tmp_path / "bin"
    write_gh_shim(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])

    built = build_bundle(tmp_path / "bundle", BundleSpec(requirements={"six": _locked("six")}))
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    installed_ext = tmp_path / "duckdb-home" / "excel.duckdb_extension"
    installed_ext.parent.mkdir()
    installed_ext.write_bytes(EXT_BYTES)
    row = {"loaded": True, "installed": True, "extension_version": "v1.5.5"}
    row["install_path"] = str(installed_ext)
    fake_runner.passthrough = True
    new_venv = tmp_path / "venvs" / "1.2.3"
    fake_runner.add([str(venv_python(new_venv))], CmdResult(0, json.dumps(row), "", 0.1))
    monkeypatch.setattr(commands_deploy, "_venv_dir", lambda: venv)
    # The temp venv plays the running Herness venv (sys.prefix, sys.executable and the wsl
    # allowlist): its real interpreter runs ``-m venv``; the new interpreter's DuckDB call is
    # answered by fake_runner.
    use_install_root(monkeypatch, tmp_path, "venv")
    monkeypatch.setattr(attest, "worker_alive", lambda: False)

    result = cmd_deploy_install(built.root, cfg=cfg, actor=USER)

    assert result.ok, result
    assert result.data == {
        "version": "1.2.3",
        "wheel_sha256": built.wheel_sha256,
        "duckdb_extensions": "excel=v1.5.5",
        "venv": str(new_venv),
        "previous": "",
        "restart": "required",
    }
    assert os.path.realpath(tmp_path / "current") == os.path.realpath(new_venv)
    assert not any(venv.glob("**/herness-1.2.3.dist-info"))  # the running venv is untouched
    site = next(new_venv.glob("**/site-packages"))
    assert (site / "herness-1.2.3.dist-info").is_dir()
    assert (site / "herness_bundle_probe.py").is_file()
    assert any(site.glob("six-*.dist-info"))
    release = tmp_path / ".release" / "1.2.3"
    assert sorted(p.name for p in release.iterdir()) == sorted(p.name for p in built.root.iterdir())
    (log,) = cfg.paths.logs.glob("audit-*.jsonl")
    lines = [json.loads(raw) for raw in log.read_text(encoding="utf-8").splitlines()]
    assert [(ln["event"], ln["fields"]["action"], ln["fields"]["target"]) for ln in lines] == [
        ("admin_action", "deploy_install", "1.2.3")
    ]
    gh_calls = [c for c in fake_runner.calls if c.argv[0] == "gh"]
    assert len(gh_calls) == 4  # wheel, requirements, extension provenance; wheel SBOM

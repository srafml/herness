"""UT10-88 to UT10-90: blue/green venv install and venv rollback (impl 10 U10-88; T10-26c).

A fake running venv under ``tmp_path`` plays ``sys.prefix`` (``tests/support/fake_venv.py``),
``fake_runner`` answers ``gh``, ``python -m venv``, ``uv`` and the new venv's interpreter, and
``current`` is a real directory junction (Windows) or symlink, so the marker logic runs on the
file system rather than on a mock.
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
from tests.support.release_bundle import (
    EXT_BYTES,
    build_bundle,
    fake_gh,
    init_release_config,
)

from herness.admin import _install_venv, attest, wsl
from herness.admin._install_venv import (
    HISTORY_CLASS,
    TORCH_LINKS,
    install_root,
    rollback_venv,
    staged_venv,
)
from herness.admin.attest import ReleaseBundle, verify_bundle
from herness.admin.deploy import DeployHistory
from herness.admin.duckdb_ext import EXT_INSTALL_SCRIPT, venv_python
from herness.admin.wsl import CmdResult
from herness.core.errors import ConfigError, FatalError, StoreBusy

pytestmark = pytest.mark.unit

USER = "0f" * 16


def link(target: Path, at: Path) -> None:
    """A directory junction on Windows (no privilege needed), a symlink elsewhere."""
    if sys.platform == "win32":
        import _winapi  # noqa: PLC0415

        _winapi.CreateJunction(str(target), str(at))
    else:
        at.symlink_to(target, target_is_directory=True)


def unlink(at: Path) -> None:
    """Remove a junction or symlink, never its target."""
    if at.is_junction():
        os.rmdir(at)
    else:
        at.unlink()


def real(path: Path | str) -> str:
    return os.path.normcase(os.path.realpath(path))


def gh_answer(argv: list[str]) -> CmdResult:
    code, out, err = fake_gh(argv[1:])
    return CmdResult(code, out, err, 0.0)


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every entry under ``root`` with its size and mtime (a write shows up as a change)."""
    return {
        str(p.relative_to(root)): (p.lstat().st_size, p.lstat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
    }


@pytest.fixture
def site(tmp_path: Path, fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install root ``tmp_path/inst`` whose ``current`` junction names ``venvs/1.0.0``, the
    running venv; a verified bundle 1.2.3; every external command faked."""
    root = tmp_path / "inst"
    make_venv(root / "venvs" / "1.0.0")
    link(root / "venvs" / "1.0.0", root / "current")
    running = use_install_root(monkeypatch, root, "current")
    installed = tmp_path / "home" / "excel.duckdb_extension"
    installed.parent.mkdir()
    installed.write_bytes(EXT_BYTES)
    row = {"loaded": True, "installed": True, "extension_version": "v1.5.5"}
    row["install_path"] = str(installed)
    new_python = str(venv_python(root / "venvs" / "1.2.3"))
    fake_runner.add(["gh", "attestation", "verify"], gh_answer)
    fake_runner.add([sys.executable, "-I", "-m", "venv"], venv_answer)
    fake_runner.add(["uv", "pip"], CmdResult(0, "", "", 0.1))
    fake_runner.add([new_python], CmdResult(0, json.dumps(row), "", 0.1))
    monkeypatch.setattr(attest, "worker_alive", lambda: False)
    monkeypatch.setattr(_install_venv, "worker_alive", lambda: False)
    built = build_bundle(tmp_path / "bundle")
    cfg = init_release_config(tmp_path / "cfg", ext_sha256=built.ext_sha256)
    bundle = ReleaseBundle.load(built.root)
    return root, running, bundle, cfg, fake_runner


def install(site: Any) -> dict[str, str]:
    _, running, bundle, cfg, _ = site
    return attest.install_bundle(bundle, verify_bundle(bundle, cfg), venv=running, actor=USER)


# --- UT10-88: install root, no write under sys.prefix -------------------------------------


@pytest.mark.parametrize("running", [".venv", "venvs/1.0.0", "current"])
def test_ut10_88_install_root_of_each_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, running: str
) -> None:
    """UT10-88 a plain ``<root>/.venv``, ``<root>/venvs/<v>`` and the ``<root>/current``
    junction all resolve to ``<root>``; the wsl allowlist names the same ``venvs`` dir."""
    root = tmp_path / "inst"
    if running == "current":
        make_venv(root / "venvs" / "1.0.0")
        link(root / "venvs" / "1.0.0", root / "current")
    venv = use_install_root(monkeypatch, root, running)
    assert install_root(venv) == root
    assert os.path.normcase(str(install_root(venv) / "venvs")) == wsl._VENVS


def test_ut10_88_wsl_install_root_rule_matches(tmp_path: Path) -> None:
    """UT10-88 wsl's import-time ``_VENVS`` follows the same rule as ``install_root`` for the
    real running venv."""
    assert os.path.normcase(str(install_root(Path(sys.prefix)) / "venvs")) == wsl._VENVS


def test_ut10_88_other_venv_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-88 a venv that is not the running one → ConfigError (it would pick the root)."""
    use_install_root(monkeypatch, tmp_path / "inst")
    with pytest.raises(ConfigError, match=r"^venv is not the running Herness venv$"):
        install_root(tmp_path / "elsewhere" / ".venv")


def test_ut10_88_staged_venv_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-88 the target ``venvs/<version>`` must not exist yet (never overwritten, so a kept
    previous venv survives) and must not lie under ``sys.prefix``."""
    root = tmp_path / "inst"
    venv = use_install_root(monkeypatch, root)
    assert staged_venv(venv, "1.2.3") == root / "venvs" / "1.2.3"
    make_venv(root / "venvs" / "1.2.3")
    with pytest.raises(FatalError, match=r"^venv already exists: 1\.2\.3$"):
        staged_venv(venv, "1.2.3")
    inner = use_install_root(monkeypatch, tmp_path / "odd", "venvs")  # a venv named "venvs"
    with pytest.raises(FatalError, match=r"^venv would be inside the running venv$"):
        staged_venv(inner, "1.2.3")


def test_ut10_88_install_never_writes_under_sys_prefix(site: Any) -> None:
    """UT10-88 a full install builds ``<root>/venvs/1.2.3`` and repoints ``current``; nothing
    under ``sys.prefix`` (the running venv, here ``venvs/1.0.0``) is created or changed."""
    root = site[0]
    before = snapshot(root / "venvs" / "1.0.0")
    result = install(site)
    assert snapshot(root / "venvs" / "1.0.0") == before
    assert result["venv"] == str(root / "venvs" / "1.2.3")
    assert result["restart"] == "required"
    assert real(root / "current") == real(root / "venvs" / "1.2.3")
    assert (root / "venvs" / "1.0.0").is_dir()  # the previous venv is kept


# --- UT10-89: commands and allowlist ------------------------------------------------------


def test_ut10_89_venv_creation_and_install_argv(site: Any) -> None:
    """UT10-89 exact argv, timeouts and ``check`` of: ``<running python> -I -m venv
    --without-pip <target>``, ``uv pip sync --require-hashes`` with the torch-only cu128 links
    page, ``uv pip install --no-deps``, then the extension script in the new interpreter."""
    root, _, bundle, _, runner = site
    install(site)
    target = root / "venvs" / "1.2.3"
    python = str(venv_python(target))
    calls = [(c.argv, c.timeout_s, c.check) for c in runner.calls if c.argv[0] != "gh"]
    assert len(calls) == 4
    private = Path(calls[1][0][-1]).parent
    assert private.name.startswith(".verify-")
    assert calls[:3] == [
        ([sys.executable, "-I", "-m", "venv", "--without-pip", str(target)], 300, True),
        (
            ["uv", "pip", "sync", "--require-hashes", "--python", python,
             "--find-links", TORCH_LINKS, str(private / "requirements.txt")],
            1800,
            True,
        ),
        (
            ["uv", "pip", "install", "--no-deps", "--python", python,
             str(private / bundle.wheel.name)],
            600,
            True,
        ),
    ]  # fmt: skip
    ext_argv = calls[3][0]
    assert ext_argv[:4] == [python, "-I", "-c", EXT_INSTALL_SCRIPT]
    assert ext_argv[4:] == [str(private / "duckdb" / "excel.duckdb_extension"), "excel"]
    assert TORCH_LINKS == "https://download.pytorch.org/whl/cu128/torch/"


def test_ut10_89_allowlist_admits_only_staged_interpreters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-89 ``admits``: listed programs, the running interpreter and exactly
    ``<install root>/venvs/<version>/<layout>``; never another root, name or layout."""
    root = tmp_path / "inst"
    use_install_root(monkeypatch, root)
    good = venv_python(root / "venvs" / "1.2.3")
    assert wsl.admits("uv")
    assert wsl.admits(sys.executable)
    assert wsl.admits(str(good))
    assert wsl.admits(str(good).upper()) == (os.name == "nt")
    bad = [
        venv_python(tmp_path / "other" / "venvs" / "1.2.3"),
        venv_python(root / "venvs" / "latest"),
        venv_python(root / "venvs" / ".."),
        venv_python(root / "venvs" / "1.2.3" / "venvs" / "1.2.3"),
        root / "venvs" / "1.2.3" / "python.exe",
        root / "venvs" / ".." / "venvs" / "1.2.3" / good.parent.name / good.name,
        good.parent / "pythonw.exe",
        Path("python"),
    ]
    for path in bad:
        assert not wsl.admits(str(path)), path
    with pytest.raises(ConfigError, match="command not allowed"):
        wsl.run_cmd([str(bad[0]), "-c", "pass"], timeout_s=5)


# --- UT10-90: atomic marker repoint and rollback ------------------------------------------


def test_ut10_90_marker_created_beside_then_renamed(
    site: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-90 the new link is created beside ``current`` under ``.current-new`` (pointing at
    the new venv) and only then renamed onto ``current``; no leftover link remains."""
    root = site[0]
    seen: list[tuple[str, str, str]] = []
    rename, replace = os.rename, os.replace

    def spy(op: Any, name: str) -> Any:
        def call(src: Any, dst: Any) -> None:
            seen.append((name, Path(src).name, Path(dst).name))
            if Path(src).name == ".current-new":
                assert real(src) == real(root / "venvs" / "1.2.3")  # created beside first
            op(src, dst)

        return call

    monkeypatch.setattr(_install_venv.os, "rename", spy(rename, "rename"))
    monkeypatch.setattr(_install_venv.os, "replace", spy(replace, "replace"))
    install(site)
    moves = [(s, d) for _, s, d in seen]
    assert (".current-new", "current") in moves
    if os.name == "nt":  # a junction cannot be renamed over a junction: step it aside first
        assert moves == [("current", ".current-old"), (".current-new", "current")]
    else:
        assert seen == [("replace", ".current-new", "current")]
    assert real(root / "current") == real(root / "venvs" / "1.2.3")
    assert sorted(p.name for p in root.iterdir()) == ["current", "venvs"]


def test_ut10_90_failed_rename_restores_previous_marker(
    site: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-90 when the final rename fails the previous ``current`` is restored, the new link
    and the new venv are removed, and nothing is recorded."""
    root = site[0]
    rename = os.rename

    def failing(src: Any, dst: Any) -> None:
        if Path(src).name == ".current-new":
            msg = "rename refused"
            raise PermissionError(msg)
        rename(src, dst)

    monkeypatch.setattr(_install_venv.os, "rename", failing)
    monkeypatch.setattr(_install_venv.os, "replace", failing)
    with pytest.raises(PermissionError):
        install(site)
    assert real(root / "current") == real(root / "venvs" / "1.0.0")
    assert sorted(p.name for p in root.iterdir()) == ["current", "venvs"]
    assert sorted(p.name for p in (root / "venvs").iterdir()) == ["1.0.0"]
    assert DeployHistory().last_promoted(HISTORY_CLASS) == []


def test_ut10_90_interrupted_switch_recovered(site: Any) -> None:
    """UT10-90 a crash between the two Windows renames leaves ``.current-old`` and no
    ``current``: the next install restores the marker before it starts."""
    root = site[0]
    os.rename(root / "current", root / ".current-old")
    link(root / "venvs" / "1.0.0", root / ".current-new")
    with structlog.testing.capture_logs() as logs:
        install(site)
    assert "deploy.install.marker_restored" in [e["event"] for e in logs]
    assert real(root / "current") == real(root / "venvs" / "1.2.3")
    assert sorted(p.name for p in root.iterdir()) == ["current", "venvs"]


def test_ut10_90_current_must_be_a_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-90 a real directory named ``current`` is never renamed or replaced."""
    root = tmp_path / "inst"
    use_install_root(monkeypatch, root)
    (root / "current").mkdir()
    (root / "venvs" / "1.2.3").mkdir(parents=True)
    with pytest.raises(FatalError, match=r"^current is not a link$"):
        _install_venv._switch(root, root / "venvs" / "1.2.3")
    assert (root / "current").is_dir()


def test_ut10_90_first_switch_from_plain_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-90 from a plain dev venv there is no ``current`` yet: the switch creates it and
    reports no previous venv."""
    root = tmp_path / "inst"
    use_install_root(monkeypatch, root)
    make_venv(root / "venvs" / "1.2.3")
    assert _install_venv._switch(root, root / "venvs" / "1.2.3") is None
    assert real(root / "current") == real(root / "venvs" / "1.2.3")


def test_ut10_90_rollback_restores_previous_marker(site: Any) -> None:
    """UT10-90 after an install, ``rollback_venv`` points ``current`` back at the previous
    venv, records the switch, audits ``deploy_rollback`` and asks for a restart; a second
    rollback goes forward again."""
    root, running, _, cfg, _ = site
    install(site)
    entry = DeployHistory().last_promoted(HISTORY_CLASS, 1)[0]
    assert real(entry["venv"]) == real(root / "venvs" / "1.2.3")
    assert real(entry["previous"]) == real(root / "venvs" / "1.0.0")
    result = rollback_venv(running, actor=USER)
    assert real(root / "current") == real(root / "venvs" / "1.0.0")
    assert result["restart"] == "required"
    assert real(result["venv"]) == real(root / "venvs" / "1.0.0")
    (log,) = cfg.paths.logs.glob("audit-*.jsonl")
    actions = [json.loads(raw)["fields"] for raw in log.read_text(encoding="utf-8").splitlines()]
    assert actions[-1]["action"] == "deploy_rollback"
    assert actions[-1]["target"] == "venv:1.0.0"
    rollback_venv(running, actor=USER)
    assert real(root / "current") == real(root / "venvs" / "1.2.3")


def test_ut10_90_rollback_refusals(site: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-90 rollback refuses with no history, while the worker runs, and when the history
    does not match ``current``; ``current`` is unchanged each time."""
    root, running, *_ = site
    with pytest.raises(ConfigError, match=r"^no previous venv to roll back to$"):
        rollback_venv(running, actor=USER)
    install(site)
    monkeypatch.setattr(_install_venv, "worker_alive", lambda: True)
    with pytest.raises(StoreBusy, match=r"^stop the worker before switching venvs$"):
        rollback_venv(running, actor=USER)
    monkeypatch.setattr(_install_venv, "worker_alive", lambda: False)
    unlink(root / "current")
    link(root / "venvs" / "1.0.0", root / "current")
    with pytest.raises(ConfigError, match=r"^no previous venv to roll back to$"):
        rollback_venv(running, actor=USER)
    assert real(root / "current") == real(root / "venvs" / "1.0.0")

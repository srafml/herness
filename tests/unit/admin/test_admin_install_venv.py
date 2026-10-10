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
from tests.support.fake_venv import ext_prefix, make_venv, use_install_root, venv_answer
from tests.support.release_bundle import (
    EXT_BYTES,
    BundleSpec,
    build_bundle,
    fake_gh,
    init_release_config,
)

from herness.admin import _install_venv, attest, wsl
from herness.admin._install_venv import (
    HISTORY_CLASS,
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
    fake_runner.add(["gh", "attestation", "verify"], gh_answer)
    fake_runner.add([sys.executable, "-I", "-m", "venv"], venv_answer)
    fake_runner.add(["uv", "pip"], CmdResult(0, "", "", 0.1))
    fake_runner.add(ext_prefix(root / "venvs" / "1.2.3"), CmdResult(0, json.dumps(row), "", 0))
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
    junction all resolve to ``<root>``."""
    root = tmp_path / "inst"
    if running == "current":
        make_venv(root / "venvs" / "1.0.0")
        link(root / "venvs" / "1.0.0", root / "current")
    venv = use_install_root(monkeypatch, root, running)
    assert install_root(venv) == root


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
    --without-pip <target>``, ``uv pip sync --require-hashes`` (no extra index: no ``+cu`` pin),
    ``uv pip install --no-deps``, then the extension script through ``uv run --python <new>``
    (Ruling T10-26c: the new interpreter is an argument, never the argv head)."""
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
            ["uv", "pip", "sync", "--no-config", "--require-hashes", "--python", python,
             str(private / "requirements.txt")],
            1800,
            True,
        ),
        (
            ["uv", "pip", "install", "--no-config", "--no-deps", "--python", python,
             str(private / bundle.wheel.name)],
            600,
            True,
        ),
    ]  # fmt: skip
    ext = str(private / "duckdb" / "excel.duckdb_extension")
    script = ["python", "-I", "-c", EXT_INSTALL_SCRIPT, ext, "excel"]
    assert calls[3] == ([*ext_prefix(target), *script], 120, False)
    assert all(argv[0] in wsl.ALLOWED_COMMANDS | {sys.executable} for argv, _, _ in calls)


@pytest.mark.parametrize("pin", ["2.11.0+cu128", "2.11.0+cpu", "2.11.0"])
def test_ut10_89_extra_index_only_for_cuda_pin(tmp_path: Path, site: Any, pin: str) -> None:
    """UT10-89 ``--extra-index-url <deploy.release.extra_index_url>`` is passed to ``uv pip
    sync`` (with ``--require-hashes``) only when requirements.txt pins a ``+cu`` local
    version (impl 00 O-05)."""
    _, running, _, cfg, runner = site
    built = build_bundle(tmp_path / "torch", BundleSpec(requirements={"torch": (pin, "0" * 64)}))
    bundle = ReleaseBundle.load(built.root)
    attest.install_bundle(bundle, verify_bundle(bundle, cfg), venv=running, actor=USER)
    (sync,) = [c.argv for c in runner.calls if c.argv[:3] == ["uv", "pip", "sync"]]
    assert "--require-hashes" in sync
    extra = ["--extra-index-url", "https://download.pytorch.org/whl/cu128"]
    flagged = sync[7:9] == extra
    assert flagged == ("+cu" in pin)
    assert ("--extra-index-url" in sync) == ("+cu" in pin)
    assert cfg.deploy.release.extra_index_url == extra[1]


def test_ut10_89_new_interpreter_not_admitted_as_argv_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-89 the U10-76 allowlist is not widened: the new venv's interpreter as argv head
    is still refused (only uv and the running interpreter drive it)."""
    root = tmp_path / "inst"
    use_install_root(monkeypatch, root)
    with pytest.raises(ConfigError, match="command not allowed"):
        wsl.run_cmd([str(venv_python(root / "venvs" / "1.2.3")), "-c", "pass"], timeout_s=5)


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
    marker = [m for m in seen if "current" in m[1] + m[2]]  # not the bundle copies' renames
    if os.name == "nt":  # a junction cannot be renamed over a junction: step it aside first
        assert marker == [
            ("rename", "current", ".current-old"),
            ("rename", ".current-new", "current"),
        ]
    else:
        assert marker == [("replace", ".current-new", "current")]
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


def test_ut10_90_cleanup_failure_after_switch_keeps_new_venv(
    site: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-90 (review round 1) once ``current`` names the new venv, a failure removing the
    stepped-aside ``.current-old`` (sharing violation, AV scanner) is only logged: the install
    succeeds, ``current`` and ``venvs/1.2.3`` survive, and the next install drops the stale link."""
    root = site[0]
    rmdir = os.rmdir

    def locked(path: Any) -> None:
        if Path(path).name == ".current-old":
            msg = "sharing violation"
            raise PermissionError(msg)
        rmdir(path)

    monkeypatch.setattr(_install_venv.os, "rmdir", locked)
    with structlog.testing.capture_logs() as logs:
        result = install(site)
    assert result["restart"] == "required"
    assert real(root / "current") == real(root / "venvs" / "1.2.3")
    assert venv_python(root / "venvs" / "1.2.3").is_file()
    if os.name == "nt":
        failed = [e["path"] for e in logs if e["event"] == "deploy.install.cleanup_failed"]
        assert failed == [str(root / ".current-old")]
        monkeypatch.setattr(_install_venv.os, "rmdir", rmdir)
        _install_venv._recover(root)
        assert sorted(p.name for p in root.iterdir()) == ["current", "venvs"]
    assert DeployHistory().last_promoted(HISTORY_CLASS, 1)[0]["version"] == "1.2.3"


def test_ut10_90_history_failure_keeps_switch(site: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-90 (review round 1) a deploy-history append failing after the switch is surfaced
    as FatalError naming the switched venv, logged ``deploy.install.history_failed``, and the
    switch is not undone (``current`` and the new venv stay)."""
    root = site[0]

    def busy(*_args: Any) -> None:
        msg = "deploy lock held"
        raise StoreBusy(msg)

    monkeypatch.setattr(DeployHistory, "record", busy)
    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(
            FatalError,
            match=r"^current now names venv 1\.2\.3, but no deploy history line was written$",
        ),
    ):
        install(site)
    assert "deploy.install.history_failed" in [e["event"] for e in logs]
    assert real(root / "current") == real(root / "venvs" / "1.2.3")
    assert venv_python(root / "venvs" / "1.2.3").is_file()


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

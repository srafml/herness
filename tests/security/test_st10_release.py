"""Security tests for release verification and install (impl 10 ST10-22 to 24, ST10-48, ST10-53,
ST10-60; TH10-36, TH10-37, TH10-40, TH10-44, TH10-52).

``fake_runner`` answers ``gh`` with ``fake_gh``, which enforces the repository, signer
workflow, hosted-runner and predicate constraints carried by the argv, and stands in for
``uv`` and the venv interpreter so nothing reaches a real environment.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import structlog
from tests.support.fake_runner import FakeRunner
from tests.support.release_bundle import (
    EXT_BYTES,
    REPO,
    SLSA,
    WORKFLOW,
    Attest,
    BundleSpec,
    build_bundle,
    fake_gh,
    init_release_config,
    statement_json,
)

from herness.admin import attest, commands_deploy
from herness.admin.attest import ReleaseBundle, verify_bundle
from herness.admin.commands_deploy import cmd_deploy_install
from herness.admin.duckdb_ext import EXT_INSTALL_SCRIPT, install_duckdb_extensions, venv_python
from herness.admin.wsl import CmdResult
from herness.core.errors import ConfigError, FatalError

pytestmark = pytest.mark.unit

USER = "0f" * 16  # an audit actor: 32 hex characters
VENV = Path(sys.prefix)  # the running Herness venv; uv and the interpreter are faked


def gh_answer(argv: list[str]) -> CmdResult:
    code, out, err = fake_gh(argv[1:])
    return CmdResult(code, out, err, 0.0)


@pytest.fixture
def env(tmp_path: Path, fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    """``gh`` via ``fake_gh``; ``uv`` succeeds; the venv interpreter reports ``excel`` loaded."""
    venv = VENV
    installed = tmp_path / "home" / "excel.duckdb_extension"
    installed.parent.mkdir()
    installed.write_bytes(EXT_BYTES)
    row = {"loaded": True, "installed": True, "extension_version": "v1.5.5"}
    row["install_path"] = str(installed)
    fake_runner.add(["gh", "attestation", "verify"], gh_answer)
    fake_runner.add(["uv", "pip"], CmdResult(0, "", "", 0.1))
    fake_runner.add([str(venv_python(venv))], CmdResult(0, json.dumps(row), "", 0.1))
    monkeypatch.setattr(commands_deploy, "_venv_dir", lambda: venv)
    monkeypatch.setattr(attest, "worker_alive", lambda: False)
    return fake_runner


def _install(tmp_path: Path, spec: BundleSpec | None = None, pin: str | None = None) -> Any:
    built = build_bundle(tmp_path / "bundle", spec)
    cfg = init_release_config(tmp_path, ext_sha256=pin or built.ext_sha256)
    return cmd_deploy_install(built.root, cfg=cfg, actor=USER)


def _nothing_installed(runner: FakeRunner, tmp_path: Path) -> None:
    assert [argv for argv in runner.argvs() if argv[0] != "gh"] == []
    assert not (tmp_path / ".release").exists()


# --- ST10-22 / ST10-23 (TH10-36) ----------------------------------------------------------


def test_st10_22_wheel_modified_after_signing_refused(tmp_path: Path, env: FakeRunner) -> None:
    """ST10-22 a wheel changed after signing (fake gh exits 1) is refused; venv untouched."""
    built = build_bundle(tmp_path / "bundle")
    with built.wheel.open("ab") as fh:
        fh.write(b"tampered")
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    with structlog.testing.capture_logs() as logs, pytest.raises(FatalError) as caught:
        cmd_deploy_install(built.root, cfg=cfg, actor=USER)
    assert str(caught.value) == f"attestation verification failed: {built.wheel.name}"
    refused = [e for e in logs if e["event"] == "deploy.install.refused"]
    assert [(e["reason"], e["file"], e["log_level"]) for e in refused] == [
        ("provenance", built.wheel.name, "error")
    ]
    _nothing_installed(env, tmp_path)


@pytest.mark.parametrize(
    "claim",
    [
        Attest(repo="mallory/herness"),
        Attest(workflow="acme/herness/.github/workflows/other.yml"),
        Attest(self_hosted=True),
    ],
    ids=["other-repo", "other-workflow", "self-hosted-runner"],
)
def test_st10_23_foreign_attestation_refused(
    tmp_path: Path, env: FakeRunner, claim: Attest
) -> None:
    """ST10-23 an attestation from another repo, another workflow or a self-hosted runner is
    refused; the argv carries every constraint the fake gh enforces."""
    with pytest.raises(FatalError, match=r"^attestation verification failed: herness-"):
        _install(tmp_path, BundleSpec(attest=claim))
    (argv,) = env.argvs()
    assert argv[argv.index("--repo") + 1] == REPO
    assert argv[argv.index("--signer-workflow") + 1] == WORKFLOW
    assert "--deny-self-hosted-runners" in argv
    assert argv[argv.index("--predicate-type") + 1] == "https://slsa.dev/provenance/v1"
    _nothing_installed(env, tmp_path)


# --- ST10-24 (TH10-37) --------------------------------------------------------------------


def _lib(name: str, version: str, licence: dict[str, Any] | None = None) -> dict[str, Any]:
    lic = licence if licence is not None else {"license": {"id": "MIT"}}
    return {"type": "library", "name": name, "version": version, "licenses": [lic]}


def test_st10_24_sbom_missing_refused(tmp_path: Path, env: FakeRunner) -> None:
    """ST10-24 a bundle without ``sbom.cdx.json`` is refused before any check runs."""
    built = build_bundle(tmp_path / "bundle")
    (built.root / "sbom.cdx.json").unlink()
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    with pytest.raises(ConfigError, match=r"^release bundle invalid: sbom\.cdx\.json$"):
        cmd_deploy_install(built.root, cfg=cfg, actor=USER)
    assert env.calls == []


def test_st10_24_sbom_not_attested_refused(tmp_path: Path, env: FakeRunner) -> None:
    """ST10-24 an SBOM without a valid attestation is refused (``sbom_attestation``)."""
    with structlog.testing.capture_logs() as logs, pytest.raises(FatalError) as caught:
        _install(tmp_path, BundleSpec(sbom_attested=False))
    assert str(caught.value) == "attestation verification failed: herness-1.2.3-py3-none-any.whl"
    refused = [e["reason"] for e in logs if e["event"] == "deploy.install.refused"]
    assert refused == ["sbom_attestation"]
    assert env.argvs()[-1][-3:] == ["https://cyclonedx.org/bom", "--format", "json"]
    _nothing_installed(env, tmp_path)


def test_st10_24_loose_sbom_differs_from_attested(tmp_path: Path, env: FakeRunner) -> None:
    """ST10-24 a loose ``sbom.cdx.json`` that differs from the attested predicate is refused."""
    built = build_bundle(tmp_path / "bundle")
    loose = built.root / "sbom.cdx.json"
    data = json.loads(loose.read_text(encoding="utf-8"))
    data["components"] = []
    loose.write_text(json.dumps(data), encoding="utf-8")
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    with pytest.raises(FatalError, match="sbom"):
        cmd_deploy_install(built.root, cfg=cfg, actor=USER)
    _nothing_installed(env, tmp_path)


@pytest.mark.parametrize(
    ("components", "needle"),
    [
        ([_lib("six", "1.16.0")], "six"),
        ([], "six"),
        ([_lib("six", "1.17.0"), _lib("evil-pkg", "1.0")], "evil-pkg"),
    ],
    ids=["version-differs", "requirement-missing", "extra-component"],
)
def test_st10_24_component_mismatch_refused(
    tmp_path: Path, env: FakeRunner, components: list[dict[str, Any]], needle: str
) -> None:
    """ST10-24 an SBOM component version that differs from requirements, a requirement absent
    from the SBOM, or an SBOM library absent from requirements is refused."""
    with pytest.raises(FatalError, match=needle):
        _install(tmp_path, BundleSpec(components=components))
    _nothing_installed(env, tmp_path)


@pytest.mark.parametrize(
    "licence",
    [{"license": {"id": "GPL-3.0-only"}}, {"expression": "GPL-2.0-or-later"}, {}],
    ids=["gpl-id", "gpl-expression", "no-licence"],
)
def test_st10_24_disallowed_licence_refused(
    tmp_path: Path, env: FakeRunner, licence: dict[str, Any]
) -> None:
    """ST10-24 a GPL (or unlicensed) component is refused, naming the component."""
    with pytest.raises(FatalError, match="six"):
        _install(tmp_path, BundleSpec(components=[_lib("six", "1.17.0", licence)]))
    _nothing_installed(env, tmp_path)


def test_st10_24_licence_exception_and_expression_allowed(tmp_path: Path, env: FakeRunner) -> None:
    """ST10-24 control: an allowlisted ``expression`` passes, and a listed exception passes."""
    built = build_bundle(
        tmp_path / "bundle",
        BundleSpec(
            requirements={"six": ("1.17.0", "0" * 64), "Odd_Name": ("2.0", "1" * 64)},
            components=[
                _lib("six", "1.17.0", {"expression": "MIT"}),
                _lib("odd-name", "2.0", {"license": {"id": "GPL-3.0-only"}}),
            ],
        ),
    )
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256, exceptions=("odd-name",))
    summary = verify_bundle(ReleaseBundle.load(built.root), cfg)
    assert (summary["components"], summary["licences_checked"]) == (2, 2)


@pytest.mark.parametrize(
    "line",
    ["six>=1.0", "-e .", "--index-url https://example.invalid/simple", "six==1.17.0\nsix==1.16.0"],
)
def test_st10_24_unpinned_requirements_refused(tmp_path: Path, env: FakeRunner, line: str) -> None:
    """ST10-24 a requirements line that is not one ``==`` pin per name is refused."""
    built = build_bundle(tmp_path / "bundle")
    (built.root / "requirements.txt").write_text(line + "\n", encoding="utf-8")
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    env.table.clear()
    env.add(["gh"], gh_answer_trusting_requirements(built.root))
    with pytest.raises(FatalError, match="requirements"):
        verify_bundle(ReleaseBundle.load(built.root), cfg)


def gh_answer_trusting_requirements(root: Path) -> Any:
    """``fake_gh`` that treats the rewritten ``requirements.txt`` as signed (to reach step 3)."""

    def answer(argv: list[str]) -> CmdResult:
        if argv[3] == str(root / "requirements.txt"):
            return CmdResult(0, statement_json(root / "requirements.txt", SLSA), "", 0.0)
        return gh_answer(argv)

    return answer


# --- ST10-48 (TH10-40) --------------------------------------------------------------------


def _dir_link(link: Path, target: Path) -> None:
    """A directory symlink, or on Windows without the symlink privilege an NTFS junction."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi  # noqa: PLC0415 - Windows only

        _winapi.CreateJunction(str(target), str(link))


def test_st10_48_symlinked_wheel_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ST10-48 a wheel that is a symlink (even to a real wheel) → ConfigError. Where creating
    a file symlink needs a privilege this process lacks (Windows), ``is_symlink`` says so."""
    built = build_bundle(tmp_path / "bundle")
    real = tmp_path / "elsewhere.whl"
    built.wheel.replace(real)
    try:
        built.wheel.symlink_to(real)
    except OSError:
        shutil.copyfile(real, built.wheel)
        wheel, original = built.wheel, Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda p: p == wheel or original(p))
    with pytest.raises(ConfigError, match=r"^release bundle invalid: herness-1\.2\.3"):
        ReleaseBundle.load(built.root)


def test_st10_48_path_outside_root_refused(tmp_path: Path) -> None:
    """ST10-48 a ``../`` bundle path, or a ``duckdb`` directory link leading outside the root,
    → ConfigError."""
    built = build_bundle(tmp_path / "bundle")
    outside = tmp_path / "x.duckdb_extension"
    outside.write_bytes(b"x")
    with pytest.raises(ConfigError, match=r"^release bundle invalid: \.\./x\.duckdb_extension$"):
        attest.checked_file(built.root, "../x.duckdb_extension")
    shutil.rmtree(built.root / "duckdb")
    other = tmp_path / "other"
    other.mkdir()
    (other / "excel.duckdb_extension").write_bytes(EXT_BYTES)
    _dir_link(built.root / "duckdb", other)
    with pytest.raises(ConfigError, match=r"^release bundle invalid: duckdb"):
        ReleaseBundle.load(built.root)


@pytest.mark.parametrize(
    "extra",
    [
        "notes.txt",
        "duckdb/SHA256SUMS",
        "duckdb/excel-2.duckdb_extension",
        "herness-9.9.9-py3-none-any.whl",
    ],
)
def test_st10_48_extra_file_refused(tmp_path: Path, extra: str) -> None:
    """ST10-48 an extra non-hidden file (top level or under ``duckdb/``) → ConfigError."""
    built = build_bundle(tmp_path / "bundle")
    (built.root / extra).write_bytes(b"x")
    with pytest.raises(ConfigError, match=r"^release bundle invalid: "):
        ReleaseBundle.load(built.root)
    (built.root / extra).unlink()
    (built.root / ".hidden").write_bytes(b"x")
    assert ReleaseBundle.load(built.root).version == "1.2.3"


def test_st10_48_missing_or_not_a_directory_refused(tmp_path: Path) -> None:
    """ST10-48 a missing provenance bundle, a directory in a file's place, or a root that is
    not a directory → ConfigError."""
    built = build_bundle(tmp_path / "bundle")
    (built.root / "requirements.txt.provenance.sigstore.json").unlink()
    with pytest.raises(ConfigError, match=r"requirements\.txt\.provenance\.sigstore\.json$"):
        ReleaseBundle.load(built.root)
    (built.root / "requirements.txt.provenance.sigstore.json").mkdir()
    with pytest.raises(ConfigError, match="release bundle invalid"):
        ReleaseBundle.load(built.root)
    with pytest.raises(ConfigError, match="release bundle invalid"):
        ReleaseBundle.load(built.wheel)
    (built.root / "duckdb" / "sub").mkdir()
    with pytest.raises(ConfigError, match="release bundle invalid"):
        ReleaseBundle.load(built.root)


# --- ST10-53 (TH10-44) --------------------------------------------------------------------


def test_st10_53_install_audited_with_version_and_sha256(tmp_path: Path, env: FakeRunner) -> None:
    """ST10-53 a successful install writes ``deploy_install`` with the version and a
    ``sha256:`` detail (plus the pinned extension), then syncs and installs from ``.release``."""
    built = build_bundle(tmp_path / "bundle")
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    result = cmd_deploy_install(built.root, cfg=cfg, actor=USER)
    assert result.ok
    assert result.exit_code == 0
    assert result.data == {
        "version": "1.2.3",
        "wheel_sha256": built.wheel_sha256,
        "duckdb_extensions": "excel=v1.5.5",
    }
    (log,) = cfg.paths.logs.glob("audit-*.jsonl")
    (line,) = [json.loads(raw) for raw in log.read_text(encoding="utf-8").splitlines()]
    assert line["event"] == "admin_action"
    assert line["actor"] == USER
    assert line["fields"] == {
        "action": "deploy_install",
        "target": "1.2.3",
        "detail": [f"sha256:{built.wheel_sha256}", f"ext:excel={built.ext_sha256}"],
    }
    release = tmp_path / ".release" / "1.2.3"
    python = str(venv_python(VENV))
    uv_calls = [c for c in env.calls if c.argv[0] == "uv"]
    assert [(c.argv, c.timeout_s, c.check) for c in uv_calls] == [
        (
            ["uv", "pip", "sync", "--require-hashes", "--python", python,
             str(release / "requirements.txt")],
            1800,
            True,
        ),
        (
            ["uv", "pip", "install", "--no-deps", "--python", python,
             str(release / built.wheel.name)],
            600,
            True,
        ),
    ]  # fmt: skip
    assert env.argvs()[-1][4] == str(release / "duckdb" / "excel.duckdb_extension")


def test_st10_53_keeps_two_newest_release_dirs(tmp_path: Path, env: FakeRunner) -> None:
    """ST10-53 ``.release/`` keeps the two newest version directories (U10-88 step 1)."""
    old = tmp_path / ".release"
    for n, name in enumerate(("0.9.0", "1.0.0", "1.1.0")):
        (old / name).mkdir(parents=True)
        os.utime(old / name, (1_700_000_000 + n, 1_700_000_000 + n))
    (old / "stray.txt").write_text("x")
    _install(tmp_path)
    assert sorted(p.name for p in old.iterdir()) == ["1.1.0", "1.2.3", "stray.txt"]


def test_st10_53_refusals_before_any_change(
    tmp_path: Path, env: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST10-53 a live worker → StoreBusy; a wheel changed after verification → FatalError;
    neither writes ``.release`` nor an audit line."""
    from herness.core.errors import StoreBusy  # noqa: PLC0415

    built = build_bundle(tmp_path / "bundle")
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    bundle = ReleaseBundle.load(built.root)
    summary = verify_bundle(bundle, cfg)
    monkeypatch.setattr(attest, "worker_alive", lambda: True)
    with pytest.raises(StoreBusy, match=r"^stop the worker before installing$"):
        attest.install_bundle(bundle, summary, venv=tmp_path / "venv", actor=USER)
    with pytest.raises(FatalError, match=r"^bundle changed after verification$"):
        attest.install_bundle(
            bundle, {**summary, "wheel_sha256": "0" * 64}, venv=tmp_path / "venv", actor=USER
        )
    assert not (tmp_path / ".release").exists()
    assert list(cfg.paths.logs.glob("audit-*.jsonl")) == []


# --- ST10-60 (TH10-52) --------------------------------------------------------------------


def test_st10_60_altered_extension_refused_before_install(tmp_path: Path, env: FakeRunner) -> None:
    """ST10-60 an extension altered after signing is refused before any ``INSTALL``: by its
    provenance in the full flow, and by the pinned hash (``extension_hash``) in U10-112."""
    built = build_bundle(tmp_path / "bundle")
    ext = built.root / "duckdb" / "excel.duckdb_extension"
    ext.write_bytes(EXT_BYTES + b"altered")
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    with pytest.raises(FatalError, match=r"^attestation verification failed: duckdb/excel"):
        cmd_deploy_install(built.root, cfg=cfg, actor=USER)
    _nothing_installed(env, tmp_path)
    env.calls.clear()
    with structlog.testing.capture_logs() as logs, pytest.raises(FatalError, match="excel"):
        install_duckdb_extensions(ReleaseBundle.load(built.root), cfg, venv=VENV)
    assert env.calls == []
    refused = [e for e in logs if e["event"] == "deploy.install.refused"]
    assert [e["reason"] for e in refused] == ["extension_hash"]


class _FakeConnection:
    def __init__(self, path: str, sql: list[str]) -> None:
        self.path, self.sql = path, sql

    def execute(self, query: str, params: list[object] | None = None) -> _FakeConnection:
        self.sql.append(query)
        return self

    def fetchone(self) -> tuple[object, ...]:
        return (True, True, "v1.5.5", self.path)


@pytest.fixture
def blocked_sockets(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Every socket connect is recorded and refused (impl 10 §11 ``fake_socket``)."""
    attempts: list[object] = []

    def refuse(_sock: socket.socket, address: object) -> None:
        attempts.append(address)
        msg = "network disabled in ST10-60"
        raise OSError(msg)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: refuse(None, a))  # type: ignore[arg-type]
    return attempts


def test_st10_60_install_script_offline_and_signed_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_sockets: list[object]
) -> None:
    """ST10-60 the fixed install script, run with sockets blocked: autoinstall, autoload and
    unsigned extensions off, a local ``INSTALL '<path>'``, ``LOAD excel``, ``NO_PROXY=*``, the
    ``duckdb_extensions()`` row printed, and no socket opened. (A stand-in ``duckdb`` module:
    the real extension load is the card's recorded acceptance check.)"""
    ext = tmp_path / "it's" / "excel.duckdb_extension"
    ext.parent.mkdir()
    ext.write_bytes(EXT_BYTES)
    sql: list[str] = []
    seen: dict[str, Any] = {}

    def connect(database: str, config: dict[str, object]) -> _FakeConnection:
        seen.update(database=database, config=config, no_proxy=os.environ.get("NO_PROXY"))
        return _FakeConnection(str(ext), sql)

    monkeypatch.setitem(sys.modules, "duckdb", types.SimpleNamespace(connect=connect))
    monkeypatch.setattr(sys, "argv", ["-c", str(ext), "excel"])
    monkeypatch.delenv("NO_PROXY", raising=False)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    exec(compile(EXT_INSTALL_SCRIPT, "<EXT_INSTALL_SCRIPT>", "exec"), {"__name__": "__main__"})  # noqa: S102
    assert seen == {
        "database": ":memory:",
        "config": {
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
            "allow_unsigned_extensions": False,
        },
        "no_proxy": "*",
    }
    quoted = str(ext).replace("'", "''")
    assert sql[:2] == [f"INSTALL '{quoted}'", "LOAD excel"]
    assert json.loads(out.getvalue()) == {
        "loaded": True,
        "installed": True,
        "extension_version": "v1.5.5",
        "install_path": str(ext),
    }
    assert blocked_sockets == []


def test_st10_60_install_script_rejects_bad_extension_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ST10-60 the script refuses an extension name outside ``[a-z0-9_]{1,40}`` before DuckDB
    is touched (no SQL built from it)."""
    monkeypatch.setitem(sys.modules, "duckdb", types.SimpleNamespace())
    monkeypatch.setattr(sys, "argv", ["-c", "x.duckdb_extension", "excel; ATTACH"])
    with pytest.raises(SystemExit) as caught:
        exec(compile(EXT_INSTALL_SCRIPT, "<EXT_INSTALL_SCRIPT>", "exec"), {"__name__": "__main__"})  # noqa: S102
    assert caught.value.code == 2


def test_st10_60_correct_extension_installs_with_sockets_blocked(
    tmp_path: Path, env: FakeRunner, blocked_sockets: list[object]
) -> None:
    """ST10-60 (partial) with the correct file the full install flow completes while every
    socket connect in *this* (parent) process is refused, and the flow tried none. ``uv`` and
    the venv interpreter are faked here, so this says nothing about sockets in the DuckDB
    install subprocess: that check (real ``duckdb`` loading ``excel`` with the network off) is
    the card's acceptance item, recorded as not run."""
    result = _install(tmp_path)
    assert result.ok
    assert result.data["duckdb_extensions"] == "excel=v1.5.5"
    assert blocked_sockets == []


# --- Review round 1 -----------------------------------------------------------------------


@pytest.mark.parametrize("pin", ["e" * 64, None], ids=["wrong-pin", "no-pin"])
def test_st10_60_extension_pin_checked_before_audit_and_venv_change(
    tmp_path: Path, env: FakeRunner, pin: str | None
) -> None:
    """ST10-60 a wrong or missing ``excel`` pin is refused by install_bundle before the audit
    line, ``.release`` and any ``uv pip`` step, so the venv is never changed first."""
    built = build_bundle(tmp_path / "bundle")
    cfg = init_release_config(tmp_path, ext_sha256=pin)
    bundle = ReleaseBundle.load(built.root)
    summary = verify_bundle(bundle, cfg)
    with pytest.raises((FatalError, ConfigError), match="excel"):
        attest.install_bundle(bundle, summary, venv=VENV, actor=USER)
    _nothing_installed(env, tmp_path)
    assert list(cfg.paths.logs.glob("audit-*.jsonl")) == []


def test_st10_53_other_venv_refused_before_any_change(tmp_path: Path, env: FakeRunner) -> None:
    """ST10-53 a venv other than the running Herness venv is refused before the audit line,
    ``.release`` and any ``uv pip`` step."""
    built = build_bundle(tmp_path / "bundle")
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    bundle = ReleaseBundle.load(built.root)
    summary = verify_bundle(bundle, cfg)
    with pytest.raises(ConfigError, match=r"^venv is not the running Herness venv$"):
        attest.install_bundle(bundle, summary, venv=tmp_path / "venv", actor=USER)
    _nothing_installed(env, tmp_path)


def test_st10_48_copy_skips_hidden_paths(tmp_path: Path, env: FakeRunner) -> None:
    """ST10-48 hidden files and anything under a hidden directory are not copied into
    ``.release/<version>/`` (``load`` ignores them, so the copy must too)."""
    built = build_bundle(tmp_path / "bundle")
    (built.root / ".cache").mkdir()
    (built.root / ".cache" / "x.bin").write_bytes(b"x")
    (built.root / "duckdb" / ".hidden").write_bytes(b"x")
    (built.root / ".notes").write_bytes(b"x")
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    assert cmd_deploy_install(built.root, cfg=cfg, actor=USER).ok
    release = tmp_path / ".release" / "1.2.3"
    copied = sorted(p.relative_to(release).as_posix() for p in release.rglob("*"))
    assert not [name for name in copied if "/." in "/" + name]
    assert "duckdb/excel.duckdb_extension" in copied


@pytest.mark.parametrize(
    "licenses",
    [["MIT"], [{"license": "MIT"}], "MIT", [None]],
    ids=["str-entry", "license-not-object", "not-a-list", "null-entry"],
)
def test_st10_24_malformed_licence_entry_refused(
    tmp_path: Path, env: FakeRunner, licenses: object
) -> None:
    """ST10-24 a malformed ``licenses`` value is refused (logged ``licence``), not a crash."""
    component = {"type": "library", "name": "six", "version": "1.17.0", "licenses": licenses}
    with structlog.testing.capture_logs() as logs, pytest.raises(FatalError, match="six"):
        _install(tmp_path, BundleSpec(components=[component]))
    refused = [e["reason"] for e in logs if e["event"] == "deploy.install.refused"]
    assert refused == ["licence"]
    _nothing_installed(env, tmp_path)


# --- Review round 2 -----------------------------------------------------------------------


def test_st10_23_foreign_trusted_root_refused_before_gh(tmp_path: Path, env: FakeRunner) -> None:
    """ST10-23 a bundle carrying its own trust root (one that does not match the file-only
    ``deploy.release.trusted_root_sha256`` pin) is refused before any gh call (ruling r2)."""
    built = build_bundle(tmp_path / "bundle")
    (built.root / "trusted_root.jsonl").write_bytes(b'{"mediaType": "attacker-root"}\n')
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    with structlog.testing.capture_logs() as logs, pytest.raises(FatalError) as caught:
        cmd_deploy_install(built.root, cfg=cfg, actor=USER)
    assert str(caught.value) == "attestation verification failed: trusted_root.jsonl"
    refused = [(e["reason"], e["file"]) for e in logs if e["event"] == "deploy.install.refused"]
    assert refused == [("trusted_root", "trusted_root.jsonl")]
    assert env.calls == []
    assert not (tmp_path / ".release").exists()


def test_st10_23_missing_trusted_root_pin_refused(tmp_path: Path, env: FakeRunner) -> None:
    """ST10-23 without the trust-root pin ``deploy.release`` counts as not configured."""
    built = build_bundle(tmp_path / "bundle")
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256, trusted_root_sha256=None)
    with pytest.raises(ConfigError, match=r"^deploy\.release is not configured$"):
        cmd_deploy_install(built.root, cfg=cfg, actor=USER)
    assert env.calls == []


def test_st10_24_requirements_swapped_after_verify_refused(tmp_path: Path, env: FakeRunner) -> None:
    """ST10-24 a ``requirements.txt`` swapped between verify and install is caught by the
    re-hash of every copied subject against ``summary["subjects"]``: no audit, no uv step."""
    built = build_bundle(tmp_path / "bundle")
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    bundle = ReleaseBundle.load(built.root)
    summary = verify_bundle(bundle, cfg)
    assert set(summary["subjects"]) == {
        built.wheel.name,
        "requirements.txt",
        "duckdb/excel.duckdb_extension",
    }
    bundle.requirements.write_text("evil==6.6.6 --hash=sha256:" + "1" * 64 + "\n", "utf-8")
    with pytest.raises(FatalError, match=r"^bundle changed after verification$"):
        attest.install_bundle(bundle, summary, venv=VENV, actor=USER)
    assert [argv for argv in env.argvs() if argv[0] != "gh"] == []
    assert list(cfg.paths.logs.glob("audit-*.jsonl")) == []

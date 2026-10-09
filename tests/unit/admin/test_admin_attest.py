"""UT10-66: ``verify_bundle`` calls ``gh attestation verify`` exactly (impl 10 U10-86, U10-87).

``fake_runner`` answers ``gh`` with ``fake_gh`` (tests/support/release_bundle.py), which checks
the placeholder Sigstore bundles against the flags it is given.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
import structlog
from tests.support.fake_runner import FakeRunner
from tests.support.release_bundle import (
    CDX,
    REPO,
    SLSA,
    WORKFLOW,
    BundleSpec,
    build_bundle,
    fake_gh,
    init_release_config,
    sha256_file,
    statement_json,
)

from herness.admin import attest
from herness.admin.attest import ReleaseBundle, verify_bundle
from herness.admin.wsl import CmdResult
from herness.core.errors import ConfigError, FatalError

pytestmark = pytest.mark.unit


def gh_answer(argv: list[str]) -> CmdResult:
    code, out, err = fake_gh(argv[1:])
    return CmdResult(code, out, err, 0.0)


@pytest.fixture
def gh(fake_runner: FakeRunner) -> FakeRunner:
    fake_runner.add(["gh", "attestation", "verify"], gh_answer)
    return fake_runner


def _argv(bundle: ReleaseBundle, subject: Path, sigstore: Path, predicate: str) -> list[str]:
    return [
        "gh", "attestation", "verify", str(subject), "--bundle", str(sigstore),
        "--repo", REPO, "--signer-workflow", WORKFLOW, "--deny-self-hosted-runners",
        "--custom-trusted-root", str(bundle.trusted_root), "--predicate-type", predicate,
        "--format", "json",
    ]  # fmt: skip


def test_ut10_66_exact_gh_argv_and_summary(tmp_path: Path, gh: FakeRunner) -> None:
    """UT10-66 provenance for wheel, requirements and extension, then the SBOM attestation:
    exact argv, 120 s timeouts, and the summary."""
    built = build_bundle(tmp_path / "bundle")
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    bundle = ReleaseBundle.load(built.root)
    with structlog.testing.capture_logs() as logs:
        summary = verify_bundle(bundle, cfg)
    # T10-26b: every gh path names verify_bundle's private copy, never the download dir.
    copy = ReleaseBundle.load(Path(summary["copy"]))
    assert copy.root.parent == cfg.paths.data / ".release"
    assert copy.root.name.startswith(".verify-")
    ext = copy.duckdb_extensions["excel"]
    prov = copy.provenance_bundles
    assert gh.argvs() == [
        _argv(copy, copy.wheel, prov[copy.wheel.name], SLSA),
        _argv(copy, copy.requirements, prov["requirements.txt"], SLSA),
        _argv(copy, ext, prov["duckdb/excel.duckdb_extension"], SLSA),
        _argv(copy, copy.wheel, copy.sbom_bundle, CDX),
    ]
    assert {call.timeout_s for call in gh.calls} == {120}
    assert summary == {
        "version": "1.2.3",
        "wheel_sha256": built.wheel_sha256,
        "subjects": {
            built.wheel.name: built.wheel_sha256,
            "requirements.txt": sha256_file(bundle.requirements),
            "duckdb/excel.duckdb_extension": built.ext_sha256,
        },
        "sbom_sha256": sha256_file(bundle.sbom),
        "copy": str(copy.root),
        "components": 1,
        "licences_checked": 1,
    }
    for rel in ("trusted_root.jsonl", "sbom.cdx.json", "duckdb/excel.duckdb_extension"):
        assert (copy.root / rel).read_bytes() == (built.root / rel).read_bytes()
    if os.name == "posix":  # NTFS ignores POSIX modes; there the data root's ACL applies
        assert stat.S_IMODE(copy.root.stat().st_mode) == 0o700
        modes = {stat.S_IMODE(p.stat().st_mode) for p in copy.root.rglob("*") if p.is_file()}
        assert modes == {0o600}
    verified = [e for e in logs if e["event"] == "deploy.install.verified"]
    assert verified == [
        {
            "event": "deploy.install.verified",
            "log_level": "info",
            "component": "admin.deploy",
            "version": "1.2.3",
            "wheel_sha256": built.wheel_sha256,
        }
    ]


def test_ut10_66_loads_bundle_fields(tmp_path: Path) -> None:
    """UT10-66 ReleaseBundle.load fills every U10-86 field from the directory."""
    built = build_bundle(tmp_path / "bundle")
    bundle = ReleaseBundle.load(built.root)
    root = built.root
    assert bundle.root == root
    assert bundle.version == "1.2.3"
    assert bundle.wheel == built.wheel
    assert bundle.requirements == root / "requirements.txt"
    assert bundle.sbom == root / "sbom.cdx.json"
    assert bundle.trusted_root == root / "trusted_root.jsonl"
    assert bundle.sbom_bundle == root / f"{built.wheel.name}.sbom.sigstore.json"
    assert bundle.duckdb_extensions == {"excel": root / "duckdb" / "excel.duckdb_extension"}
    assert set(bundle.provenance_bundles) == {
        built.wheel.name,
        "requirements.txt",
        "duckdb/excel.duckdb_extension",
    }


def test_ut10_66_bundle_without_duckdb_dir(tmp_path: Path, gh: FakeRunner) -> None:
    """UT10-66 a bundle without ``duckdb/`` verifies wheel and requirements only."""
    built = build_bundle(tmp_path / "bundle", BundleSpec(ext_bytes=None))
    cfg = init_release_config(tmp_path, ext_sha256=None)
    bundle = ReleaseBundle.load(built.root)
    assert bundle.duckdb_extensions == {}
    verify_bundle(bundle, cfg)
    assert len(gh.calls) == 3


def test_ut10_66_unconfigured_release_raises(tmp_path: Path, gh: FakeRunner) -> None:
    """UT10-66 no ``deploy.release.repo``/``signer_workflow`` → ConfigError, no subprocess."""
    built = build_bundle(tmp_path / "bundle")
    cfg = init_release_config(tmp_path, ext_sha256=None, release=False)
    with pytest.raises(ConfigError, match=r"^deploy\.release is not configured$"):
        verify_bundle(ReleaseBundle.load(built.root), cfg)
    assert gh.calls == []


def test_ut10_66_unreadable_gh_output_refused(
    tmp_path: Path, fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-66 a zero exit whose JSON carries no predicate is refused (SBOM not attested)."""
    built = build_bundle(tmp_path / "bundle")
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    for out in ("not json", "[]", '[{"verificationResult": {}}]'):

        def answer(argv: list[str], out: str = out) -> CmdResult:
            return CmdResult(0, out, "", 0.0) if CDX in argv else gh_answer(argv)

        fake_runner.table.clear()
        fake_runner.add(["gh"], answer)
        with pytest.raises(FatalError, match="sbom"):
            verify_bundle(ReleaseBundle.load(built.root), cfg)
    monkeypatch.setattr(attest, "_MAX_BYTES", 10)
    with pytest.raises(ConfigError, match="release bundle invalid"):
        ReleaseBundle.load(built.root)


def test_ut10_66_unreadable_sbom_components_refused(tmp_path: Path, gh: FakeRunner) -> None:
    """UT10-66 an attested SBOM whose ``components`` are not objects is refused."""
    built = build_bundle(tmp_path / "bundle", BundleSpec(components=["six"]))
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    with pytest.raises(FatalError, match=r"^sbom components unreadable$"):
        verify_bundle(ReleaseBundle.load(built.root), cfg)


def test_ut10_66_unconfigured_trust_root_pin_raises(tmp_path: Path, gh: FakeRunner) -> None:
    """UT10-66 no ``deploy.release.trusted_root_sha256`` → ConfigError, no subprocess."""
    built = build_bundle(tmp_path / "bundle")
    cfg = init_release_config(tmp_path, ext_sha256=None, trusted_root_sha256=None)
    with pytest.raises(ConfigError, match=r"^deploy\.release is not configured$"):
        verify_bundle(ReleaseBundle.load(built.root), cfg)
    assert gh.calls == []


@pytest.mark.parametrize(
    "answer",
    [
        lambda path: statement_json(path, "https://example.invalid/other-predicate"),
        lambda path: statement_json(path, SLSA, digest="0" * 64),
        lambda path: "[]",
        lambda path: '[{"verificationResult": {"statement": {"predicateType": "x"}}}]',
    ],
    ids=["other-predicate", "other-subject-digest", "empty", "no-subject"],
)
def test_ut10_66_provenance_json_must_name_this_file(
    tmp_path: Path, fake_runner: FakeRunner, answer: Callable[[Path], str]
) -> None:
    """UT10-66 a zero gh exit is not enough: the SLSA result must carry the SLSA predicate type
    and this file's own SHA-256 as a subject digest (review round 2, B2)."""
    built = build_bundle(tmp_path / "bundle")
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    fake_runner.add(["gh"], lambda argv: CmdResult(0, answer(Path(argv[3])), "", 0.0))
    with structlog.testing.capture_logs() as logs, pytest.raises(FatalError) as caught:
        verify_bundle(ReleaseBundle.load(built.root), cfg)
    assert str(caught.value) == f"attestation verification failed: {built.wheel.name}"
    refused = [(e["reason"], e["file"]) for e in logs if e["event"] == "deploy.install.refused"]
    assert refused == [("provenance", built.wheel.name)]
    assert len(fake_runner.calls) == 1


def test_ut10_66_private_copy_refuses_oversized_or_swapped_file(
    tmp_path: Path, gh: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-66 the private copy (T10-26b) refuses a file grown past the size limit after
    ``load``, and a file whose opened identity differs from the one checked (a link or a swap
    between the check and the open; simulated, as file links need a privilege on Windows);
    neither leaves anything behind nor reaches ``gh``."""
    built = build_bundle(tmp_path / "bundle")
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)
    bundle = ReleaseBundle.load(built.root)
    monkeypatch.setattr(attest, "_MAX_BYTES", 10)
    with pytest.raises(ConfigError, match=r"^release bundle invalid: duckdb/excel\."):
        verify_bundle(bundle, cfg)
    monkeypatch.undo()
    real_lstat, other = os.lstat, built.root / "sbom.cdx.json"

    def lstat(path: object) -> os.stat_result:
        return real_lstat(other if Path(str(path)).name == "requirements.txt" else str(path))

    monkeypatch.setattr(os, "lstat", lstat)
    with pytest.raises(ConfigError, match=r"^release bundle invalid: requirements\.txt$"):
        verify_bundle(bundle, cfg)
    assert gh.calls == []
    assert list((cfg.paths.data / ".release").iterdir()) == []


def test_ut10_66_sbom_attestation_must_name_the_wheel(
    tmp_path: Path, fake_runner: FakeRunner
) -> None:
    """UT10-66 the SBOM attestation's subject digest must be the wheel's SHA-256 (T10-26b,
    parked minor of T10-26 r2): an SBOM attested for another file is refused, and the private
    copy is removed."""
    built = build_bundle(tmp_path / "bundle")
    cfg = init_release_config(tmp_path, ext_sha256=built.ext_sha256)

    def answer(argv: list[str]) -> CmdResult:
        result = gh_answer(argv)
        if CDX not in argv:
            return result
        doc = json.loads(result.stdout)
        doc[0]["verificationResult"]["statement"]["subject"][0]["digest"]["sha256"] = "0" * 64
        return CmdResult(0, json.dumps(doc), "", 0.0)

    fake_runner.add(["gh"], answer)
    with structlog.testing.capture_logs() as logs, pytest.raises(FatalError) as caught:
        verify_bundle(ReleaseBundle.load(built.root), cfg)
    assert str(caught.value) == "sbom attestation does not name the wheel"
    refused = [(e["reason"], e["file"]) for e in logs if e["event"] == "deploy.install.refused"]
    assert refused == [("sbom_attestation", "sbom.cdx.json")]
    assert list((cfg.paths.data / ".release").iterdir()) == []

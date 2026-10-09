"""UT10-66: ``verify_bundle`` calls ``gh attestation verify`` exactly (impl 10 U10-86, U10-87).

``fake_runner`` answers ``gh`` with ``fake_gh`` (tests/support/release_bundle.py), which checks
the placeholder Sigstore bundles against the flags it is given.
"""

from __future__ import annotations

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
    ext = bundle.duckdb_extensions["excel"]
    prov = bundle.provenance_bundles
    assert gh.argvs() == [
        _argv(bundle, bundle.wheel, prov[bundle.wheel.name], SLSA),
        _argv(bundle, bundle.requirements, prov["requirements.txt"], SLSA),
        _argv(bundle, ext, prov["duckdb/excel.duckdb_extension"], SLSA),
        _argv(bundle, bundle.wheel, bundle.sbom_bundle, CDX),
    ]
    assert {call.timeout_s for call in gh.calls} == {120}
    assert summary == {
        "version": "1.2.3",
        "wheel_sha256": built.wheel_sha256,
        "components": 1,
        "licences_checked": 1,
    }
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
        fake_runner.table.clear()
        fake_runner.add(["gh"], CmdResult(0, out, "", 0.0))
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

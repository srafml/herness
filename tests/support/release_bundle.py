"""Release-bundle fixtures and a fake ``gh attestation verify`` (impl 10 T10-26, §11).

``build_bundle`` writes a U10-86 bundle into ``tmp_path``: a tiny pure-Python wheel, a hashed
``requirements.txt``, a CycloneDX SBOM, a placeholder trusted root and one "Sigstore bundle"
per attested file. Each placeholder bundle is JSON naming what the fake ``gh`` must check: the
subject's SHA-256, repository, signer workflow, runner kind and predicate. ``fake_gh`` enforces
those against the flags it is given, like ``gh`` does, so a test can hand it to ``fake_runner``
(in process) or run this file as the ``gh`` program (``write_gh_shim``; stdlib only).
Digests are computed at run time; no binary or digest literal is committed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = "acme/herness"
WORKFLOW = "acme/herness/.github/workflows/release.yml"
SLSA = "https://slsa.dev/provenance/v1"
CDX = "https://cyclonedx.org/bom"
VERSION = "1.2.3"
EXT_BYTES = b"\x00asm-fake-duckdb-excel-extension\x00" * 8


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class Attest:
    """What one placeholder Sigstore bundle claims about its subject."""

    repo: str = REPO
    workflow: str = WORKFLOW
    self_hosted: bool = False
    predicate: dict[str, Any] = field(default_factory=dict)


@dataclass
class BundleSpec:
    """Knobs for ``build_bundle``; the defaults give a bundle that verifies."""

    version: str = VERSION
    requirements: dict[str, tuple[str, str]] = field(
        default_factory=lambda: {"six": ("1.17.0", "0" * 64)}
    )
    components: list[dict[str, Any]] | None = None  # None: derived from requirements, MIT
    attest: Attest = field(default_factory=Attest)
    sbom_attested: bool = True
    ext_bytes: bytes | None = EXT_BYTES


@dataclass(frozen=True)
class Built:
    root: Path
    wheel: Path
    wheel_sha256: str
    ext_sha256: str | None


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def build_wheel(path: Path, version: str) -> None:
    """A minimal ``herness`` wheel holding one module, ``herness_bundle_probe``."""
    info = f"herness-{version}.dist-info"
    files = {
        "herness_bundle_probe.py": f'VERSION = "{version}"\n'.encode(),
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: herness\nVersion: {version}\n".encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\n"
        b"Tag: py3-none-any\n",
    }
    record = "".join(f"{n},{_record_hash(d)},{len(d)}\n" for n, d in files.items())
    record += f"{info}/RECORD,,\n"
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        zf.writestr(f"{info}/RECORD", record)


def requirements_text(reqs: dict[str, tuple[str, str]]) -> str:
    lines = ["# uv export --frozen --no-emit-project"]
    for name, (version, digest) in reqs.items():
        lines += [f"{name}=={version} \\", f"    --hash=sha256:{digest}"]
    return "\n".join(lines) + "\n"


def sbom_for(spec: BundleSpec) -> dict[str, Any]:
    components = spec.components
    if components is None:
        components = [
            {"type": "library", "name": n, "version": v, "licenses": [{"license": {"id": "MIT"}}]}
            for n, (v, _) in spec.requirements.items()
        ]
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "metadata": {"component": {"type": "application", "name": "herness"}},
        "components": components,
    }


def _claim(subject: Path, predicate_type: str, attest: Attest, predicate: Any) -> str:
    return json.dumps(
        {
            "subject_sha256": sha256_file(subject),
            "repo": attest.repo,
            "signer_workflow": attest.workflow,
            "self_hosted": attest.self_hosted,
            "predicate_type": predicate_type,
            "predicate": predicate,
        }
    )


def build_bundle(root: Path, spec: BundleSpec | None = None) -> Built:
    """Write a release bundle directory under ``root`` and return its key facts."""
    spec = spec or BundleSpec()
    root.mkdir(parents=True, exist_ok=True)
    wheel = root / f"herness-{spec.version}-py3-none-any.whl"
    build_wheel(wheel, spec.version)
    reqs = root / "requirements.txt"
    reqs.write_text(requirements_text(spec.requirements), encoding="utf-8")
    sbom = sbom_for(spec)
    (root / "sbom.cdx.json").write_text(json.dumps(sbom, indent=2), encoding="utf-8")
    (root / "trusted_root.jsonl").write_text('{"mediaType": "trusted-root-placeholder"}\n')
    subjects = [wheel, reqs]
    ext_sha = None
    if spec.ext_bytes is not None:
        (root / "duckdb").mkdir()
        ext = root / "duckdb" / "excel.duckdb_extension"
        ext.write_bytes(spec.ext_bytes)
        ext_sha = sha256_file(ext)
        subjects.append(ext)
    provenance = {"buildDefinition": {"buildType": "https://actions.github.io/buildtypes"}}
    for subject in subjects:
        claim = _claim(subject, SLSA, spec.attest, provenance)
        (root / f"{subject.name}.provenance.sigstore.json").write_text(claim, encoding="utf-8")
    sbom_claim = _claim(wheel, CDX, spec.attest, sbom) if spec.sbom_attested else "{}"
    (root / f"{wheel.name}.sbom.sigstore.json").write_text(sbom_claim, encoding="utf-8")
    return Built(root, wheel, sha256_file(wheel), ext_sha)


def _flags(argv: list[str]) -> tuple[str, dict[str, str], set[str]]:
    subject, values, switches = argv[2], {}, set()
    rest = argv[3:]
    while rest:
        flag = rest.pop(0)
        if flag == "--deny-self-hosted-runners":
            switches.add(flag)
        else:
            values[flag] = rest.pop(0)
    return subject, values, switches


def fake_gh(argv: list[str]) -> tuple[int, str, str]:
    """``gh attestation verify`` against a placeholder bundle: (exit code, stdout, stderr)."""
    if argv[:2] != ["attestation", "verify"]:
        return 2, "", "unsupported"
    subject, values, switches = _flags(argv)
    try:
        claim = json.loads(Path(values["--bundle"]).read_text(encoding="utf-8"))
        checks = [
            Path(values["--custom-trusted-root"]).is_file(),
            values["--format"] == "json",
            claim["subject_sha256"] == sha256_file(Path(subject)),
            claim["repo"] == values["--repo"],
            claim["signer_workflow"] == values["--signer-workflow"],
            not (claim["self_hosted"] and "--deny-self-hosted-runners" in switches),
            claim["predicate_type"] == values["--predicate-type"],
        ]
    except (KeyError, OSError, ValueError):
        return 1, "", "no valid attestation"
    if not all(checks):
        return 1, "", "verification failed"
    statement = {"predicateType": claim["predicate_type"], "predicate": claim["predicate"]}
    return 0, json.dumps([{"verificationResult": {"statement": statement}}]), ""


def write_gh_shim(bin_dir: Path) -> Path:
    """Write a ``gh`` program (``gh.cmd`` on Windows) that runs ``fake_gh`` in a subprocess."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve()
    if sys.platform == "win32":
        shim = bin_dir / "gh.cmd"
        shim.write_text(f'@"{sys.executable}" -I "{here}" %*\r\n', encoding="utf-8")
    else:
        shim = bin_dir / "gh"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{here}" "$@"\n', "utf-8")
        shim.chmod(0o755)
    return shim


def init_release_config(
    root: Path, *, ext_sha256: str | None, exceptions: tuple[str, ...] = (), release: bool = True
) -> Any:
    """``init_config`` on a full config tree under ``root`` with a ``deploy.release`` block."""
    # Imported here so this file stays stdlib-only when it runs as the fake ``gh`` program.
    from tests.support.config_tree import write_full_config  # noqa: PLC0415

    from herness.core import config  # noqa: PLC0415

    cfg_dir = write_full_config(root)
    block: dict[str, Any] = {"repo": REPO, "signer_workflow": WORKFLOW}
    block["licence_exceptions"] = list(exceptions)
    if ext_sha256 is not None:
        block["duckdb_extensions"] = {"excel": ext_sha256}
    if release:
        with (cfg_dir / "herness.yaml").open("a", encoding="utf-8") as fh:
            fh.write(f"  release: {json.dumps(block)}\n")
    return config.init_config(config_dir=cfg_dir, env={})


if __name__ == "__main__":
    code, out, err = fake_gh(sys.argv[1:])
    sys.stdout.write(out)
    sys.stderr.write(err)
    raise SystemExit(code)

"""Release bundle verification and install (impl 10 U10-86 to U10-88; ENG E4, R-58).

``herness deploy install`` refuses a release unless ``gh attestation verify`` proves SLSA
Build L2 provenance of the wheel, ``requirements.txt`` and each DuckDB extension, and the
attested CycloneDX SBOM equals the loose SBOM, matches the pinned requirements and carries
only allowlisted licences. Only then is the bundle copied to ``.release/<version>/``, audited
and installed into the Herness venv (TH10-36, TH10-37, TH10-40, TH10-44). Every check and the
install read a private copy made before the first hash, never the download dir (T10-26b).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn

from herness.admin._attest_copy import copy_files, private_dir, private_dir_of, remove_private
from herness.admin.duckdb_ext import check_extensions, install_duckdb_extensions, venv_python
from herness.admin.wsl import run_cmd
from herness.core import config as _config
from herness.core.audit import audit
from herness.core.config import HernessConfig, get_config
from herness.core.errors import ConfigError, FatalError, SchemaViolation, StoreBusy
from herness.core.ids import canonical_json
from herness.core.jobs import worker_alive
from herness.core.logging import get_logger
from herness.core.settings import ReleaseDeploy

__all__ = ["ALLOWED_LICENCES", "ReleaseBundle", "checked_file", "install_bundle", "verify_bundle"]

_log = get_logger("admin.deploy")

_WHEEL_RE: Final = re.compile(r"^herness-(\d+\.\d+\.\d+[a-z0-9.+-]*)-py3-none-any\.whl$")
_EXT_RE: Final = re.compile(r"^([a-z0-9_]{1,40})\.duckdb_extension$")
_PIN_RE: Final = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;\\]+)")
_MAX_BYTES = 200 * 1024 * 1024
_PROV: Final = ".provenance.sigstore.json"
_SLSA: Final = "https://slsa.dev/provenance/v1"
_CDX: Final = "https://cyclonedx.org/bom"
_FIXED: Final = ("requirements.txt", "sbom.cdx.json", "trusted_root.jsonl")
ALLOWED_LICENCES: Final = frozenset(
    {"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "PSF-2.0", "Python-2.0", "ISC", "MPL-2.0"}
)  # ENG §5.6
_KEEP_RELEASES: Final = 2
_ROOT_REFUSED: Final = "attestation verification failed: trusted_root.jsonl"


def _invalid(rel: str) -> NoReturn:
    msg = f"release bundle invalid: {rel}"
    raise ConfigError(msg)


def checked_file(root: Path, rel: str) -> Path:
    """``root/rel`` if it stays inside ``root``, is no symlink, is a file and ≤ 200 MiB."""
    path = root / rel
    try:
        inside = path.resolve().is_relative_to(root.resolve())
        ok = inside and not path.is_symlink() and path.is_file()
        ok = ok and path.stat().st_size <= _MAX_BYTES
    except OSError:
        ok = False
    if not ok:
        _invalid(rel)
    return path


def _visible(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir() if not p.name.startswith(".")}


def _extensions(root: Path) -> list[str]:
    """Names of the ``duckdb/<name>.duckdb_extension`` files; any other entry is invalid."""
    directory = root / "duckdb"
    if not (directory.exists() or directory.is_symlink()):
        return []
    inside = directory.resolve().is_relative_to(root.resolve())
    if directory.is_symlink() or directory.is_junction() or not inside or not directory.is_dir():
        _invalid("duckdb")
    names = []
    for entry in sorted(_visible(directory)):
        match = _EXT_RE.fullmatch(entry)
        if match is None:
            _invalid(f"duckdb/{entry}")
        names.append(match.group(1))
    return names


@dataclass(frozen=True)
class ReleaseBundle:
    """A release bundle directory produced by release CI (U10-86; ENG §5.6, R-58).

    ``provenance_bundles`` is keyed by the subject's bundle-relative POSIX path.
    """

    root: Path
    wheel: Path
    version: str
    requirements: Path
    sbom: Path
    trusted_root: Path
    provenance_bundles: dict[str, Path]
    sbom_bundle: Path
    duckdb_extensions: dict[str, Path]

    @classmethod
    def load(cls, root: Path) -> ReleaseBundle:
        """Check the layout of ``root``; ConfigError on a missing, extra or unsafe file."""
        if not root.is_dir() or root.is_symlink():
            _invalid(root.name)
        names = _visible(root)
        wheels = [m for n in sorted(names) if (m := _WHEEL_RE.fullmatch(n))]
        if len(wheels) != 1:
            _invalid(wheels[1].group(0) if wheels else "herness-<version>-py3-none-any.whl")
        wheel, version = wheels[0].group(0), wheels[0].group(1)
        exts = _extensions(root)
        subjects = [wheel, "requirements.txt", *(f"duckdb/{n}.duckdb_extension" for n in exts)]
        expected = {*_FIXED, *(s.rsplit("/", 1)[-1] + _PROV for s in subjects)}
        expected |= {wheel, f"{wheel}.sbom.sigstore.json", *(["duckdb"] if exts else [])}
        for rel in sorted(expected ^ names):
            _invalid(rel)
        files = {rel: checked_file(root, rel) for rel in sorted(expected - {"duckdb"})}
        return cls(
            root=root,
            wheel=files[wheel],
            version=version,
            requirements=files["requirements.txt"],
            sbom=files["sbom.cdx.json"],
            trusted_root=files["trusted_root.jsonl"],
            provenance_bundles={s: files[s.rsplit("/", 1)[-1] + _PROV] for s in subjects},
            sbom_bundle=files[f"{wheel}.sbom.sigstore.json"],
            duckdb_extensions={n: checked_file(root, f"duckdb/{n}.duckdb_extension") for n in exts},
        )


def _files(bundle: ReleaseBundle) -> dict[str, Path]:
    """Every bundle file by its bundle-relative POSIX path."""
    paths = [bundle.wheel, bundle.requirements, bundle.sbom, bundle.trusted_root]
    paths += [bundle.sbom_bundle, *bundle.provenance_bundles.values()]
    paths += bundle.duckdb_extensions.values()
    return {p.relative_to(bundle.root).as_posix(): p for p in paths}


def _sha256(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _refuse(reason: str, file: str, message: str) -> NoReturn:
    _log.error("deploy.install.refused", reason=reason, file=file)
    raise FatalError(message)


def _gh_verify(bundle: ReleaseBundle, release: ReleaseDeploy, subject: Path, predicate: str) -> str:
    """One ``gh attestation verify`` offline against the bundled trusted root (V-22)."""
    rel = subject.relative_to(bundle.root).as_posix()
    sigstore = bundle.provenance_bundles[rel] if predicate == _SLSA else bundle.sbom_bundle
    argv = [
        "gh", "attestation", "verify", str(subject), "--bundle", str(sigstore),
        "--repo", str(release.repo), "--signer-workflow", str(release.signer_workflow),
        "--deny-self-hosted-runners", "--custom-trusted-root", str(bundle.trusted_root),
        "--predicate-type", predicate, "--format", "json",
    ]  # fmt: skip
    result = run_cmd(argv, timeout_s=120)
    if result.returncode != 0:
        reason = "provenance" if predicate == _SLSA else "sbom_attestation"
        _refuse(reason, rel, f"attestation verification failed: {rel}")
    return result.stdout


def _check_provenance(rel: str, stdout: str, digest: str) -> None:
    """Defence in depth on gh's JSON: SLSA predicate type and this file's SHA-256 as subject."""
    try:
        statement = json.loads(stdout)[0]["verificationResult"]["statement"]
        ok = statement["predicateType"] == _SLSA and any(
            s["digest"]["sha256"] == digest for s in statement["subject"]
        )
    except (ValueError, LookupError, TypeError):
        ok = False
    if not ok:
        _refuse("provenance", rel, f"attestation verification failed: {rel}")


def _attested_sbom(bundle: ReleaseBundle, stdout: str, wheel_sha256: str) -> dict[str, Any]:
    """The verified SBOM predicate, attested for this wheel; it must equal the loose
    ``sbom.cdx.json`` (U10-87 step 3; the subject check is T10-26b defence in depth)."""
    try:
        statement = json.loads(stdout)[0]["verificationResult"]["statement"]
        predicate = statement["predicate"]
        named = any(s["digest"]["sha256"] == wheel_sha256 for s in statement["subject"])
        loose = json.loads(bundle.sbom.read_text(encoding="utf-8"))
        same = canonical_json(loose) == canonical_json(predicate)
    except (ValueError, LookupError, TypeError, SchemaViolation, OSError):
        _refuse("sbom_attestation", "sbom.cdx.json", "sbom attestation unreadable")
    if not named:
        _refuse("sbom_attestation", "sbom.cdx.json", "sbom attestation does not name the wheel")
    if not (same and statement.get("predicateType") == _CDX and isinstance(predicate, dict)):
        _refuse("sbom_mismatch", "sbom.cdx.json", "sbom does not match its attestation")
    return predicate


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements(path: Path) -> dict[str, str]:
    """``{normalised name: version}`` from ``uv export`` output; only ``==`` pins allowed."""
    text = path.read_text(encoding="utf-8").replace("\\\r\n", " ").replace("\\\n", " ")
    pins: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = _PIN_RE.match(line)
        name = _norm(match.group(1)) if match else ""
        if match is None or pins.setdefault(name, match.group(2)) != match.group(2):
            _refuse("requirements", "requirements.txt", "requirements.txt is not pinned with ==")
    return pins


def _check_components(components: list[dict[str, Any]], pins: dict[str, str]) -> int:
    """Every SBOM library matches its requirement pin, and every pin is in the SBOM."""
    libs = {
        _norm(str(c.get("name"))): str(c.get("version"))
        for c in components
        if c.get("type") == "library"
    }
    bad = sorted(n for n in libs.keys() | pins.keys() if libs.get(n) != pins.get(n))
    if bad:
        msg = f"sbom components differ from requirements: {', '.join(bad)}"
        _refuse("components", "sbom.cdx.json", msg)
    return len(libs)


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _licences(component: dict[str, Any]) -> list[str]:
    entries = component.get("licenses") or []
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        return []  # malformed: no licence found, so the component is refused (unless excepted)
    found = [e.get("expression") or _dict(e.get("license")).get("id") for e in entries]
    return [str(item) for item in found] if all(found) else []


def _check_licences(components: list[dict[str, Any]], exceptions: Iterable[str]) -> int:
    """Every component's licences are allowlisted, or it is a listed exception (U10-87 step 5)."""
    excepted = {_norm(e) for e in exceptions}
    bad = sorted(
        str(c.get("name"))
        for c in components
        if _norm(str(c.get("name"))) not in excepted
        and not (_licences(c) and set(_licences(c)) <= ALLOWED_LICENCES)
    )
    if bad:
        _refuse("licence", "sbom.cdx.json", f"licence not allowed: {', '.join(bad)}")
    return len(components)


def verify_bundle(bundle: ReleaseBundle, cfg: HernessConfig) -> dict[str, Any]:
    """Verify provenance, the attested SBOM, components and licences (U10-87; TH10-36/37).
    Ruling T10-26b: hashes, pin and ``gh`` read only a private copy (``summary["copy"]``)."""
    release = cfg.deploy.release
    if not (release.repo and release.signer_workflow and release.trusted_root_sha256):
        msg = "deploy.release is not configured"
        raise ConfigError(msg)
    private = private_dir(cfg.paths.data)
    try:
        copy_files(_files(bundle), bundle.root, private, _MAX_BYTES)
        return _verify(ReleaseBundle.load(private), release)
    except BaseException:
        remove_private(private)
        raise


def _verify(copy: ReleaseBundle, release: ReleaseDeploy) -> dict[str, Any]:
    # Ruling T10-26 r2: the bundled trust root must match its file-only pin before any gh call.
    if _sha256(copy.trusted_root) != release.trusted_root_sha256:
        _refuse("trusted_root", "trusted_root.jsonl", _ROOT_REFUSED)
    files = [copy.wheel, copy.requirements, *copy.duckdb_extensions.values()]
    subjects = {f.relative_to(copy.root).as_posix(): _sha256(f) for f in files}  # hashed first
    sbom_sha256 = _sha256(copy.sbom)
    for rel, digest in subjects.items():
        _check_provenance(rel, _gh_verify(copy, release, copy.root / rel, _SLSA), digest)
    digest = subjects[copy.wheel.name]
    sbom = _attested_sbom(copy, _gh_verify(copy, release, copy.wheel, _CDX), digest)
    components = sbom.get("components") or []
    if not isinstance(components, list) or not all(isinstance(c, dict) for c in components):
        _refuse("components", "sbom.cdx.json", "sbom components unreadable")
    count = _check_components(components, _requirements(copy.requirements))
    licences = _check_licences(components, release.licence_exceptions)
    _log.info("deploy.install.verified", version=copy.version, wheel_sha256=digest)
    return {
        "version": copy.version,
        "wheel_sha256": digest,
        "subjects": subjects,
        "sbom_sha256": sbom_sha256,
        "copy": str(copy.root),
        "components": count,
        "licences_checked": licences,
    }


def _check_unchanged(root: Path, wheel: str, summary: dict[str, Any]) -> None:
    """Re-hash the wheel, every provenance subject and the SBOM under ``root`` (U10-88)."""
    verified = {**summary["subjects"], "sbom.cdx.json": summary["sbom_sha256"]}
    if verified.get(wheel) != summary["wheel_sha256"] or any(
        _sha256(root / rel) != sha for rel, sha in verified.items()
    ):
        msg = "bundle changed after verification"
        raise FatalError(msg)


def _prune(releases: Path) -> None:
    """Keep the two newest version directories under ``.release/``."""
    dirs = sorted((p for p in releases.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    for old in dirs[:-_KEEP_RELEASES]:
        shutil.rmtree(old)


def _repo_root() -> Path:
    """The config dir's parent, as for ``<repo root>/.env`` (impl 10 U10-18)."""
    return _config._Cache.root or Path.cwd()


def install_bundle(
    bundle: ReleaseBundle, summary: dict[str, Any], *, venv: Path, actor: str
) -> dict[str, str]:
    """Install a verified release into the Herness venv (U10-88; the only target install path).
    Ruling T10-26b: only from ``summary["copy"]``, removed on success or failure."""
    cfg = get_config()
    private = private_dir_of(summary.get("copy"), cfg.paths.data)
    try:
        copy = ReleaseBundle.load(private)
        if copy.version != bundle.version:
            msg = "bundle changed after verification"
            raise FatalError(msg)
        return _install(copy, summary, cfg, venv=venv, actor=actor)
    finally:
        remove_private(private)


def _install(
    copy: ReleaseBundle, summary: dict[str, Any], cfg: HernessConfig, *, venv: Path, actor: str
) -> dict[str, str]:
    _check_unchanged(copy.root, copy.wheel.name, summary)
    if worker_alive():
        msg = "stop the worker before installing"
        raise StoreBusy(msg)
    check_extensions(copy, cfg, venv=venv)  # U10-112 pins before the audit and any venv change
    releases = _repo_root() / ".release"
    archive = releases / copy.version  # kept for rollback; the install reads the private copy
    archive.mkdir(parents=True, exist_ok=True)
    copy_files(_files(copy), copy.root, archive, _MAX_BYTES)
    os.utime(archive)
    _check_unchanged(archive, copy.wheel.name, summary)
    _prune(releases)
    digest = str(summary["wheel_sha256"])
    pins = cfg.deploy.release.duckdb_extensions
    detail = [f"sha256:{digest}", *(f"ext:{n}={pins[n]}" for n in sorted(pins))]
    audit("admin_action", actor, action="deploy_install", target=copy.version, detail=detail)
    python = str(venv_python(venv))
    sync = ["uv", "pip", "sync", "--require-hashes", "--python", python, str(copy.requirements)]
    run_cmd(sync, timeout_s=1800, check=True)
    wheel = ["uv", "pip", "install", "--no-deps", "--python", python, str(copy.wheel)]
    run_cmd(wheel, timeout_s=600, check=True)
    versions = install_duckdb_extensions(copy, cfg, venv=venv)
    return {
        "version": copy.version,
        "wheel_sha256": digest,
        "duckdb_extensions": ",".join(f"{n}={v}" for n, v in sorted(versions.items())),
    }

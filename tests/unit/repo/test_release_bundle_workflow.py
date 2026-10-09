"""UT00-82, UT00-83: release.yml publishes exactly the U10-86 bundle (T00-15b, TH10-36/37).

The workflow stages the bundle in ``bundle/`` on the build job: the wheel, ``requirements.txt``,
``sbom.cdx.json``, ``duckdb/<name>.duckdb_extension`` for ``windows_amd64``, one Sigstore
bundle per attested subject (kept from each attest step's ``bundle-path`` output) and the
trusted root from ``gh attestation trusted-root``. UT00-83 derives that tree from the parsed
workflow and checks that ``ReleaseBundle.load`` (the consumer, impl 10 U10-86) accepts it.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.support.release_bundle import VERSION, build_bundle, build_wheel

from herness.admin.attest import ReleaseBundle

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
STAGE = "bundle/"
WHEEL = f"herness-{VERSION}-py3-none-any.whl"
PROVENANCE = "actions/attest-build-provenance@"
SBOM_ATTEST = "actions/attest-sbom@"
BUNDLE_PATH = re.compile(r"^\$\{\{\s*steps\.([A-Za-z0-9_-]+)\.outputs\.bundle-path\s*\}\}$")
TRUSTED_ROOT = re.compile(r"gh attestation trusted-root\s*>\s*(\S+)")
KEEP_TARGET = '"bundle/${subject##*/}$SUFFIX"'


def _doc() -> dict[str, Any]:
    doc = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def _steps(doc: dict[str, Any], job: str) -> list[dict[str, Any]]:
    return list(doc["jobs"][job]["steps"])


def _runs(steps: list[dict[str, Any]]) -> list[str]:
    return [str(s.get("run", "")) for s in steps]


def _attests(steps: list[dict[str, Any]], action: str) -> list[dict[str, Any]]:
    return [s for s in steps if str(s.get("uses", "")).startswith(action)]


def _keeps(steps: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Steps that keep an attest step's Sigstore bundle, keyed by that step's ``id``."""
    kept: dict[str, dict[str, Any]] = {}
    for step in steps:
        match = BUNDLE_PATH.match(str(step.get("env", {}).get("SRC", "")))
        if match:
            assert match.group(1) not in kept, match.group(1)
            kept[match.group(1)] = step
    return kept


def _rel(path: str) -> str:
    """A staged path as a bundle-relative POSIX path, the wheel glob made concrete."""
    assert path.startswith(STAGE), path
    rel = path.removeprefix(STAGE)
    if "*" in rel:
        assert fnmatch(WHEEL, rel), rel
        rel = WHEEL
    return rel


def _declared_tree(doc: dict[str, Any]) -> set[str]:
    """Every file the build job stages in ``bundle/``, as the workflow declares it."""
    steps = _steps(doc, "build")
    sbom = _attests(steps, SBOM_ATTEST)
    assert len(sbom) == 1
    tree = {_rel(s["with"]["subject-path"]) for s in _attests(steps, PROVENANCE)}
    tree |= {_rel(sbom[0]["with"]["subject-path"]), _rel(sbom[0]["with"]["sbom-path"])}
    for keep in _keeps(steps).values():
        env = keep["env"]
        assert KEEP_TARGET in keep["run"], keep.get("name")
        tree.add(_rel(env["SUBJECT"]).rsplit("/", 1)[-1] + env["SUFFIX"])
    roots = [m.group(1) for r in _runs(steps) for m in TRUSTED_ROOT.finditer(r)]
    assert len(roots) == 1, roots
    tree.add(_rel(roots[0]))
    return tree


def test_ut00_82_release_publishes_the_u10_86_bundle() -> None:
    """UT00-82 subjects, one attestation step and one kept bundle each, SBOM, trusted root."""
    doc = _doc()
    text = RELEASE.read_text(encoding="utf-8")
    build = doc["jobs"]["build"]
    steps = _steps(doc, "build")
    runs = _runs(steps)
    assert build["runs-on"] == "windows-latest"  # the target platform (U10-86)
    assert "/windows_amd64/excel.duckdb_extension.gz" in text
    for absent in ("linux_amd64", "SHA256SUMS", ".tar.gz", "dist/"):
        assert absent not in text, absent

    assert any(r.strip() == "uv build --wheel --out-dir bundle" for r in runs)
    export = next(r for r in runs if "uv export" in r)
    assert "--no-emit-project" in export
    assert "bundle/requirements.txt" in export
    assert any("-o bundle/sbom.cdx.json" in r and "check_licences" in r for r in runs)

    provenance = _attests(steps, PROVENANCE)
    subjects = [s["with"]["subject-path"] for s in provenance]
    assert subjects == [
        "bundle/herness-*-py3-none-any.whl",
        "bundle/requirements.txt",
        "bundle/duckdb/excel.duckdb_extension",
    ]
    (sbom,) = _attests(steps, SBOM_ATTEST)
    assert sbom["with"] == {
        "subject-path": "bundle/herness-*-py3-none-any.whl",
        "sbom-path": "bundle/sbom.cdx.json",
    }
    keeps = _keeps(steps)
    for step in [*provenance, sbom]:
        keep = keeps.pop(step["id"])
        suffix = ".provenance.sigstore.json" if step in provenance else ".sbom.sigstore.json"
        assert keep["env"]["SUBJECT"] == step["with"]["subject-path"]
        assert keep["env"]["SUFFIX"] == suffix
        assert steps.index(keep) == steps.index(step) + 1  # before the next attest step
    assert keeps == {}

    trusted = next(i for i, r in enumerate(runs) if TRUSTED_ROOT.search(r))
    load = next(i for i, r in enumerate(runs) if "ReleaseBundle.load" in r)
    offline = next(i for i, r in enumerate(runs) if "--custom-trusted-root" in r)
    upload = next(i for i, s in enumerate(steps) if "upload-artifact@" in s.get("uses", ""))
    assert trusted < load < offline < upload
    assert steps[upload]["with"]["path"] == "bundle/"

    publish = doc["jobs"]["publish"]
    assert publish["permissions"] == {"contents": "write"}
    release = next(r for r in _runs(publish["steps"]) if "gh release create" in r)
    assert '"$TAG" "$ASSET"' in release
    for name, job in doc["jobs"].items():
        writes = job.get("permissions", {}).get("contents") == "write"
        assert writes == (name == "publish"), name


def test_ut00_83_declared_bundle_tree_loads(tmp_path: Path) -> None:
    """UT00-83 the tree release.yml declares is the U10-86 layout ``ReleaseBundle.load`` reads."""
    tree = _declared_tree(_doc())
    root = tmp_path / "bundle"
    for rel in sorted(tree):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel == WHEEL:
            build_wheel(path, VERSION)
        else:
            path.write_bytes(b"{}\n")

    bundle = ReleaseBundle.load(root)

    assert bundle.version == VERSION
    assert bundle.wheel.name == WHEEL
    assert sorted(bundle.provenance_bundles) == sorted(
        [WHEEL, "requirements.txt", "duckdb/excel.duckdb_extension"]
    )
    assert sorted(bundle.duckdb_extensions) == ["excel"]
    fixture = build_bundle(tmp_path / "fixture").root
    encoded = {p.relative_to(fixture).as_posix() for p in fixture.rglob("*") if p.is_file()}
    assert tree == encoded  # the T10-26 fixture builder encodes the same layout

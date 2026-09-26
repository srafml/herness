"""ST00-06: the hosted workflows are hardened against CI compromise (TH00-12, §3.8 rules)."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
RELEASE = WORKFLOWS / "release.yml"
# ci.yml lands with T00-14; release.yml with T00-15 (skipped until it exists).
WORKFLOW_NAMES = ("ci.yml", "release.yml")
PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
PINNED_LINE = re.compile(r"uses:\s*[^\s#]+@[0-9a-f]{40}\s+#\s*v\d+(\.\d+)*\b")
# Any expression (whitespace-tolerant) that reads event data, head_ref or inputs.
FORBIDDEN_IN_RUN = re.compile(
    r"\$\{\{(?:(?!\}\}).)*?\b(github\s*\.\s*(event\s*[.\[]|head_ref)|inputs\s*[.\[])"
)
HOSTED_RUNNERS = frozenset({"ubuntu-latest", "windows-latest"})
MATRIX_REF = re.compile(r"^\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}$")


def _load(path: Path) -> dict[Any, Any]:
    if path != CI and not path.exists():
        pytest.skip(f"{path.name} not written yet")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), path.name
    return doc


def _triggers(doc: dict[Any, Any]) -> Any:
    # YAML 1.1 reads the bare key `on` as the boolean True.
    return doc["on"] if "on" in doc else doc[True]


def _steps(doc: dict[Any, Any]) -> Iterator[dict[str, Any]]:
    for job in doc["jobs"].values():
        yield from job.get("steps", [])


def _uses(doc: dict[Any, Any]) -> Iterator[str]:
    for job in doc["jobs"].values():
        if "uses" in job:
            yield job["uses"]
    for step in _steps(doc):
        if "uses" in step:
            yield step["uses"]


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(key)
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


def _check_pinned(path: Path, doc: dict[Any, Any]) -> None:
    third_party = [ref for ref in _uses(doc) if not ref.startswith("./")]
    assert third_party, path.name
    for ref in third_party:
        assert PINNED.match(ref), f"{path.name}: {ref}"
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip().removeprefix("- ")
        if stripped.startswith("uses:") and "./" not in stripped:
            assert PINNED_LINE.search(stripped), f"{path.name}: {line.strip()}"


def _check_untrusted_input(path: Path, doc: dict[Any, Any]) -> None:
    triggers = _triggers(doc)
    names = [triggers] if isinstance(triggers, str) else list(triggers)
    assert "pull_request_target" not in names, path.name
    for step in _steps(doc):
        script = step.get("run", "")
        assert not FORBIDDEN_IN_RUN.search(script), f"{path.name}: {step.get('name', script[:40])}"


def _runners(job: dict[str, Any]) -> list[str]:
    """Runner labels of a job, with a ``${{ matrix.<key> }}`` runs-on expanded."""
    runs_on = job["runs-on"]
    labels = runs_on if isinstance(runs_on, list) else [runs_on]
    resolved: list[str] = []
    for label in labels:
        ref = MATRIX_REF.match(str(label))
        if ref is None:
            resolved.append(str(label))
            continue
        matrix = job.get("strategy", {}).get("matrix", {})
        values = list(matrix.get(ref.group(1), []))
        values += [e[ref.group(1)] for e in matrix.get("include", []) if ref.group(1) in e]
        assert values, f"matrix.{ref.group(1)} has no values"
        resolved.extend(str(value) for value in values)
    return resolved


def _check_runners_and_checkout(path: Path, doc: dict[Any, Any]) -> None:
    assert not any("self-hosted" in s for s in _strings(doc["jobs"])), path.name
    for name, job in doc["jobs"].items():
        if "uses" in job:  # a reusable-workflow call has no runner of its own
            continue
        assert set(_runners(job)) <= HOSTED_RUNNERS, f"{path.name}:{name}"
    for step in _steps(doc):
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step.get("with", {}).get("persist-credentials") is False, path.name


def _check_ci_contract(doc: dict[Any, Any]) -> None:
    triggers = _triggers(doc)
    assert set(triggers) == {
        "push",
        "pull_request",
        "schedule",
        "workflow_dispatch",
        "workflow_call",
    }
    assert triggers["schedule"] == [{"cron": "17 3 * * *"}]
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["pull_request"]["branches"] == ["main"]
    env = doc["env"]
    assert env | {"UV_FROZEN": "1", "PYTHONUTF8": "1", "HERNESS_ENV": "test"} == env
    assert re.fullmatch(r"\d+\.\d+\.\d+", env["OSV_SCANNER_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", env["OSV_SCANNER_SHA256"])
    jobs = doc["jobs"]
    timeouts = {name: job["timeout-minutes"] for name, job in jobs.items()}
    assert timeouts == {"lint": 15, "types": 20, "test": 30, "audit": 20}
    assert "if" not in jobs["audit"]
    for name in ("lint", "types", "test"):
        assert "schedule" in jobs[name]["if"]
    matrix_os = [entry["os"] for entry in jobs["test"]["strategy"]["matrix"]["include"]]
    assert matrix_os == ["ubuntu-latest", "windows-latest"]


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_st00_06_workflow_hardening(name: str) -> None:
    """ST00-06 pinned actions, read-only token, no untrusted input, hosted runners, no creds.

    ci.yml must exist and also matches the U00-59 triggers, env, jobs and timeouts;
    release.yml (T00-15) is skipped until it exists.
    """
    path = WORKFLOWS / name
    doc = _load(path)
    _check_pinned(path, doc)
    assert doc.get("permissions") == {"contents": "read"}, name
    _check_untrusted_input(path, doc)
    _check_runners_and_checkout(path, doc)
    if path == CI:
        _check_ci_contract(doc)


def test_st00_08_release_workflow() -> None:
    """ST00-08 release.yml: trigger, job topology, build permissions, attestations, order.

    Trigger is tag push ``v*.*.*`` only; ``build`` needs ``ci``; ``build`` has
    ``id-token: write`` and ``attestations: write`` and no other job does; ``build``
    calls ``attest-build-provenance`` on the wheel and sdist and ``attest-sbom``; the
    tag-verification and version-match steps precede ``uv build``.
    """
    doc = _load(RELEASE)
    triggers = _triggers(doc)
    assert set(triggers) == {"push"}
    assert triggers["push"] == {"tags": ["v*.*.*"]}

    jobs = doc["jobs"]
    assert jobs["build"]["needs"] == "ci"
    for name, job in jobs.items():
        has_id_token = job.get("permissions", {}).get("id-token") == "write"
        assert has_id_token == (name == "build"), name

    build = jobs["build"]
    assert build["permissions"]["id-token"] == "write"
    assert build["permissions"]["attestations"] == "write"

    steps = build["steps"]
    run_texts = [s.get("run", "") for s in steps]
    uses_list = [s.get("uses", "") for s in steps]

    build_idx = next((i for i, r in enumerate(run_texts) if r.strip() == "uv build"), None)
    tag_verify_idx = next(
        (i for i, r in enumerate(run_texts) if "verification.verified" in r), None
    )
    version_check_idx = next(
        (i for i, r in enumerate(run_texts) if "does not match project version" in r), None
    )
    assert build_idx is not None
    assert tag_verify_idx is not None
    assert version_check_idx is not None
    assert tag_verify_idx < build_idx
    assert version_check_idx < build_idx

    provenance_idx = next(
        (i for i, u in enumerate(uses_list) if "attest-build-provenance@" in u), None
    )
    sbom_attest_idx = next((i for i, u in enumerate(uses_list) if "attest-sbom@" in u), None)
    assert provenance_idx is not None
    assert sbom_attest_idx is not None
    subject_path = steps[provenance_idx]["with"]["subject-path"]
    assert "dist/*.whl" in subject_path
    assert "dist/*.tar.gz" in subject_path

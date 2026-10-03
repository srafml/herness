"""UT08-103: impl 08 layering contracts (impl 08 §2; R-03, R-04, R-06)."""

import ast
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from importlinter.cli import lint_imports

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
_CORE_STORE = "herness.core must not import herness.store or herness.harness"
_SETTINGS = "resilience-settings-light"
_TYPES_JOBS = "types-jobs-light"
_HTTPX_CLIENTS = {"Client", "AsyncClient", "HTTPTransport", "AsyncHTTPTransport"}
_CORE_BASE = {
    "herness.core.logging",
    "herness.core._log_pipeline",
    "herness.core.time",
    "herness.core.numbers",
}
# R-03: herness.core.resilience.settings may import, among Herness modules, only these.
_SETTINGS_ALLOWED = ("herness.core.types", "herness.core.errors")
_SETTINGS_PATH = ROOT / "herness" / "core" / "resilience" / "settings.py"
_SETTINGS_PACKAGE = "herness.core.resilience"
_TYPES_JOBS_FORBIDDEN = _CORE_BASE | {
    "herness.core.types.decisions",
    "herness.core.types.memory",
    "herness.core.types.reports",
    "herness.core.types.swarm",
    "herness.core.types._ownership",
}


def _contracts() -> dict[str, dict[str, Any]]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {c["name"]: c for c in data["tool"]["importlinter"]["contracts"]}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _child_modules(dotted: str) -> set[str]:
    """Direct submodules and subpackages of the Herness package `dotted`, from the tree."""
    folder = ROOT.joinpath(*dotted.split("."))
    found: set[str] = set()
    for entry in folder.iterdir():
        if entry.name.startswith("__"):
            continue
        if entry.is_dir() and (entry / "__init__.py").is_file():
            found.add(f"{dotted}.{entry.name}")
        elif entry.suffix == ".py":
            found.add(f"{dotted}.{entry.stem}")
    return found


def _settings_forbidden() -> set[str]:
    """R-03: every Herness top-level package and core / resilience module but the allowed."""
    modules = _child_modules("herness") | _child_modules("herness.core")
    modules |= _child_modules(_SETTINGS_PACKAGE)
    keep = {"herness.core", _SETTINGS_PACKAGE, f"{_SETTINGS_PACKAGE}.settings"}
    return modules - keep - set(_SETTINGS_ALLOWED)


def _settings_import_allowed(module: str) -> bool:
    top = module.split(".", 1)[0]
    if top in sys.stdlib_module_names or top == "pydantic":
        return True
    return any(module == ok or module.startswith(f"{ok}.") for ok in _SETTINGS_ALLOWED)


def _import_targets(source: str, package: str) -> list[str]:
    """Every module an import statement names, anywhere in `source` (TYPE_CHECKING blocks and
    function bodies included); relative imports are resolved against `package`."""
    targets: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join([*parts, base]) if base else ".".join(parts)
            if _settings_import_allowed(base):
                targets.append(base)
            else:  # `from herness.core import errors` names the submodule
                targets.extend(f"{base}.{alias.name}" for alias in node.names)
    return targets


def test_ut08_103_contracts_declared() -> None:
    """UT08-103 the core/store, R-03 settings and closed-base contracts name the 08 packages."""
    contracts = _contracts()
    core_store = contracts[_CORE_STORE]
    assert core_store["type"] == "forbidden"
    assert core_store["source_modules"] == ["herness.core"]
    expected = ["herness.store"]
    if (ROOT / "herness" / "harness" / "__init__.py").is_file():
        expected.append("herness.harness")
    assert sorted(core_store["forbidden_modules"]) == sorted(expected)
    settings = contracts[_SETTINGS]
    assert settings["source_modules"] == ["herness.core.*.settings"]
    for module in ("herness.core.ids", "herness.core.time", "herness.core.logging"):
        assert module in settings["forbidden_modules"]
    closed = contracts["core base is closed"]["forbidden_modules"]
    assert {"herness.core.resilience", "herness.core.jobs"} <= set(closed)


def test_ut08_103_settings_contract_allows_only_types_and_errors() -> None:
    """UT08-103 R-03: resilience settings may import no other Herness module than types/errors."""
    settings = _contracts()[_SETTINGS]
    assert settings["type"] == "forbidden"
    assert set(settings["forbidden_modules"]) >= _settings_forbidden()
    assert "herness.core.types" not in settings["forbidden_modules"]
    assert "herness.core.errors" not in settings["forbidden_modules"]


def test_ut08_103_settings_imports_allowlist() -> None:
    """UT08-103 R-03: every import in herness/core/resilience/settings.py is stdlib, pydantic,
    herness.core.types(.*) or herness.core.errors (AST allowlist; cannot drift as modules grow)."""
    source = _SETTINGS_PATH.read_text(encoding="utf-8")
    for module in _import_targets(source, _SETTINGS_PACKAGE):
        assert _settings_import_allowed(module), module


@pytest.mark.parametrize(
    ("source", "rejected"),
    [
        ("from . import deciders", "herness.core.resilience.deciders"),
        ("from .ports import X", "herness.core.resilience.ports.X"),
        ("from ..redact_patterns import X", "herness.core.redact_patterns.X"),
        ("from herness.core import ids", "herness.core.ids"),
        ("if TYPE_CHECKING:\n    import herness.core.time", "herness.core.time"),
        ("def f():\n    import httpx", "httpx"),
    ],
)
def test_ut08_103_settings_allowlist_rejects(source: str, rejected: str) -> None:
    """UT08-103 the settings allowlist resolves relative, TYPE_CHECKING and local imports."""
    targets = _import_targets(source, _SETTINGS_PACKAGE)
    assert rejected in targets
    assert not _settings_import_allowed(rejected)


def test_ut08_103_settings_allowlist_accepts() -> None:
    """UT08-103 the settings allowlist keeps stdlib, pydantic, types(.*) and errors."""
    source = (
        "from __future__ import annotations\nimport re\nfrom pydantic import BaseModel\n"
        "from herness.core import errors, types\nfrom ..types.jobs import JobSpec\n"
        "from herness.core.errors import HernessError"
    )
    targets = _import_targets(source, _SETTINGS_PACKAGE)
    assert "herness.core.types.jobs" in targets
    assert all(_settings_import_allowed(module) for module in targets), targets


def test_ut08_103_types_jobs_contract_declared() -> None:
    """UT08-103 herness.core.types.jobs: an import-linter contract keeps it to errors, ids and
    types.harness among Herness modules (the third-party part is the AST test below)."""
    contract = _contracts()[_TYPES_JOBS]
    assert contract["type"] == "forbidden"
    assert contract["source_modules"] == ["herness.core.types.jobs"]
    assert set(contract["forbidden_modules"]) >= _TYPES_JOBS_FORBIDDEN
    allowed = {"herness.core.errors", "herness.core.ids", "herness.core.types.harness"}
    assert not allowed & set(contract["forbidden_modules"])


def test_ut08_103_lint_imports_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-103 every import-linter contract is kept on the repository (run in process)."""
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(sys, "path", list(sys.path))  # lint_imports prepends the cwd
    status = lint_imports(
        str(ROOT / "pyproject.toml"), no_cache=True, is_debug_mode=True, no_logo=True
    )
    assert status == 0


def test_ut08_103_types_jobs_imports_allowed() -> None:
    """UT08-103 herness.core.types.jobs imports only pydantic, stdlib, errors, ids, harness."""
    allowed = {"herness.core.errors", "herness.core.ids", "herness.core.types.harness"}
    for module in _imports(ROOT / "herness" / "core" / "types" / "jobs.py"):
        top = module.split(".", 1)[0]
        assert top in sys.stdlib_module_names or top == "pydantic" or module in allowed, module


def test_ut08_103_jobs_never_construct_httpx_clients() -> None:
    """UT08-103 no module under herness/core/jobs/ constructs an httpx client (R-06)."""
    for path in sorted((ROOT / "herness" / "core" / "jobs").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "httpx":
                names = {alias.name for alias in node.names}
                assert not names & _HTTPX_CLIENTS, path
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                owner = node.func.value
                is_httpx = isinstance(owner, ast.Name) and owner.id == "httpx"
                assert not (is_httpx and node.func.attr in _HTTPX_CLIENTS), path

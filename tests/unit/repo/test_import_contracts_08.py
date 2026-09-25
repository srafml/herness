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
_HTTPX_CLIENTS = {"Client", "AsyncClient", "HTTPTransport", "AsyncHTTPTransport"}


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

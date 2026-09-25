"""Tests for tools.check_type_ownership (U00-47)."""

import shutil
from pathlib import Path

import pytest

from herness.core.types._ownership import TYPE_OWNERS
from tools.check_type_ownership import main

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NAMES_05 = sorted(n for n, o in TYPE_OWNERS.items() if o == "05")
NAMES_06 = sorted(n for n, o in TYPE_OWNERS.items() if o == "06")


def _classes(names: list[str]) -> str:
    return "".join(f"class {name}:\n    pass\n\n\n" for name in names)


def _tree(tmp: Path, reexports: dict[str, list[str]] | None = None) -> Path:
    types_dir = tmp / "herness" / "core" / "types"
    types_dir.mkdir(parents=True)
    shutil.copy(ROOT / "herness/core/types/_ownership.py", types_dir / "_ownership.py")
    lines = ['"""Shared types."""']
    everything: list[str] = []
    for sub, names in (reexports or {}).items():
        lines.append(f"from herness.core.types.{sub} import {', '.join(sorted(names))}")
        everything += names
    lines.append(f"__all__: tuple[str, ...] = {tuple(sorted(everything))!r}")
    (types_dir / "__init__.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = main(["--root", str(root)])
    return code, capsys.readouterr().out


def test_ut00_49_missing_type(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-49 an owner submodule missing a registered name gives OWN010."""
    root = _tree(tmp_path, {"harness": NAMES_05})
    _write(
        root, "herness/core/types/harness.py", _classes([n for n in NAMES_05 if n != "NumberRef"])
    )
    code, out = _run(root, capsys)
    assert code == 1
    assert "OWN010" in out
    assert "NumberRef" in out


def test_ut00_50_unregistered_and_wrong_owner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT00-50 an extra public name gives OWN011; another owner's name gives OWN012."""
    root = _tree(tmp_path, {"harness": NAMES_05})
    _write(root, "herness/core/types/harness.py", _classes([*NAMES_05, "Extra", "Finding"]))
    _, out = _run(root, capsys)
    assert "OWN011 unregistered Extra" in out
    assert "OWN012 wrong owner Finding" in out


def test_ut00_51_forbidden_imports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-51 duckdb, a core module and a non-sibling submodule each give OWN020."""
    root = _tree(tmp_path, {"swarm": NAMES_06})
    body = "import duckdb\nimport herness.core.config\nfrom herness.core.types.memory import Kind\n"
    _write(root, "herness/core/types/swarm.py", body + _classes(NAMES_06))
    _, out = _run(root, capsys)
    assert out.count("OWN020") == 3


def test_ut00_52_no_owner_submodules(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-52 with no owner submodule the check passes with six pending lines."""
    code, out = _run(_tree(tmp_path), capsys)
    assert code == 0
    assert out.count("INFO pending owner") == 6


def test_ut00_53_import_via_submodule(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-53 importing a type from a submodule outside the package gives OWN041."""
    root = _tree(tmp_path)
    _write(root, "herness/harness/x.py", "from herness.core.types.harness import NumberRef\n")
    _, out = _run(root, capsys)
    assert "OWN041" in out


def test_ut00_54_declared_elsewhere(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-54 behavioral names in core.types give OWN043; in a foreign module OWN042."""
    root = _tree(tmp_path, {"harness": NAMES_05})
    _write(root, "herness/core/types/harness.py", _classes([*NAMES_05, "Tracer"]))
    _write(root, "herness/harness/other.py", "class ModelChain:\n    pass\n")
    _, out = _run(root, capsys)
    assert "OWN043" in out
    assert "OWN042" in out


def test_ut00_72_package_form(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-72 package-form submodules are accepted; two forms and extra code are not."""
    half = len(NAMES_05) // 2
    first = _tree(tmp_path / "a", {"harness": NAMES_05})
    init = (
        f"from .llm import {', '.join(NAMES_05[:half])}\n"
        f"from .evidence import {', '.join(NAMES_05[half:])}\n"
        f"__all__ = {tuple(NAMES_05)!r}\n"
    )
    _write(first, "herness/core/types/harness/__init__.py", init)
    _write(first, "herness/core/types/harness/llm.py", _classes(NAMES_05[:half]))
    _write(first, "herness/core/types/harness/evidence.py", _classes(NAMES_05[half:]))
    assert _run(first, capsys)[0] == 0
    second = _tree(tmp_path / "b", {"harness": NAMES_05})
    _write(second, "herness/core/types/harness.py", _classes(NAMES_05))
    _write(second, "herness/core/types/harness/__init__.py", init)
    assert "OWN003" in _run(second, capsys)[1]
    third = _tree(tmp_path / "c", {"harness": NAMES_05})
    _write(third, "herness/core/types/harness/__init__.py", init + "class Extra:\n    pass\n")
    _write(third, "herness/core/types/harness/llm.py", _classes(NAMES_05[:half]))
    _write(third, "herness/core/types/harness/evidence.py", _classes(NAMES_05[half:]))
    assert "OWN033" in _run(third, capsys)[1]


def test_ut00_73_settings_imports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-73 settings modules may import only stdlib, pydantic, core.types, core.errors."""
    root = _tree(tmp_path)
    body = (
        "import typing\nimport pydantic\nimport herness.core.errors\nimport herness.core.types\n"
        "import httpx\nimport herness.core.config\nimport herness.core.types.harness\n"
    )
    _write(root, "herness/connectors/settings.py", body)
    _, out = _run(root, capsys)
    lines = [line for line in out.splitlines() if "OWN050" in line]
    assert len(lines) == 3
    for name in ("httpx", "herness.core.config", "herness.core.types.harness"):
        assert any(line.endswith(name) for line in lines)


def test_ut00_73_private_sibling_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT00-73 `_*_settings.py` modules are scanned; a settings module may import its sibling."""
    root = _tree(tmp_path)
    _write(root, "herness/connectors/__init__.py", "")
    _write(root, "herness/metrics/__init__.py", "")
    body = (
        "from herness.connectors._http_settings import X\n"
        "from herness.metrics._other_settings import Y\n"
        "import herness.connectors.client\n"
    )
    _write(root, "herness/connectors/settings.py", body)
    _write(root, "herness/connectors/_http_settings.py", "import pydantic\nimport httpx\n")
    _, out = _run(root, capsys)
    lines = sorted(line for line in out.splitlines() if "OWN050" in line)
    assert len(lines) == 3
    assert any("_http_settings.py" in line and line.endswith("httpx") for line in lines)
    assert any(line.endswith("herness.metrics._other_settings") for line in lines)
    assert any(line.endswith("herness.connectors.client") for line in lines)


def test_ut00_81_declared_module_or_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT00-81 definitions inside the declared package pass; elsewhere give OWN042."""
    good = _tree(tmp_path / "a")
    _write(good, "herness/core/jobs/ports.py", "class JobContext:\n    pass\n")
    _write(good, "herness/core/resilience/chain.py", "class ModelChain:\n    pass\n")
    assert "OWN042" not in _run(good, capsys)[1]
    bad = _tree(tmp_path / "b")
    _write(bad, "herness/harness/other.py", "class JobContext:\n    pass\n")
    out = _run(bad, capsys)[1]
    assert "OWN042" in out
    assert "JobContext" in out


def test_st00_15_redefinition_outside(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """ST00-15 a shared type redefined elsewhere gives OWN040 at that file."""
    root = _tree(tmp_path, {"harness": NAMES_05})
    _write(root, "herness/core/types/harness.py", _classes(NAMES_05))
    _write(root, "herness/metrics/x.py", "class NumberRef:\n    pass\n")
    code, out = _run(root, capsys)
    assert code == 1
    assert "herness/metrics/x.py:1: OWN040" in out

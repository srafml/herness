"""UT01-01 / R-03: settings_base loads only allowed modules in a fresh interpreter (T01-01)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
PYDANTIC = {
    "pydantic",
    "pydantic_core",
    "typing_extensions",
    "typing_inspection",
    "annotated_types",
}
HERNESS = {
    "herness",
    "herness.connectors",
    "herness.connectors.settings_base",
    "herness.core",
    "herness.core.errors",
    "herness.core.types",
}
PROBE = (
    "import json, sys; before = set(sys.modules); "
    "import herness.connectors.settings_base; "
    "print(json.dumps(sorted(set(sys.modules) - before)))"
)


def _allowed(name: str) -> bool:
    top = name.split(".", maxsplit=1)[0]
    if top in sys.stdlib_module_names or top in PYDANTIC:
        return True
    return name in HERNESS or name.startswith("herness.core.types.")


def test_ut01_01_r03_settings_base_imports_in_fresh_interpreter() -> None:
    """UT01-01 (R-03 acceptance check) importing settings_base loads only allowed modules."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROBE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    loaded = json.loads(result.stdout)
    assert "herness.connectors.settings_base" in loaded
    assert "pydantic" in loaded  # proves third-party imports are observed, not cached
    assert [name for name in loaded if not _allowed(name)] == []

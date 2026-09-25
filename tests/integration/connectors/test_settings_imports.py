"""R-03: `herness.connectors.settings` loads only allowed modules in a fresh interpreter."""

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
    "herness.connectors.settings",
    "herness.connectors.settings_base",
    "herness.connectors.settings_entities",
    "herness.core",
    "herness.core.errors",
    "herness.core.types",
}
PROBE = (
    "import json, sys; before = set(sys.modules); "
    "import herness.connectors.settings; "
    "print(json.dumps(sorted(set(sys.modules) - before)))"
)


def _allowed(name: str) -> bool:
    top = name.split(".", maxsplit=1)[0]
    if top in sys.stdlib_module_names or top in PYDANTIC:
        return True
    return name in HERNESS or name.startswith("herness.core.types.")


def test_ut01_02_r03_settings_imports_in_fresh_interpreter() -> None:
    """UT01-02 (R-03 acceptance check) importing settings loads only allowed modules."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROBE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=120,
    )
    loaded = json.loads(result.stdout)
    assert "herness.connectors.settings" in loaded
    assert "pydantic" in loaded  # proves third-party imports are observed, not cached
    assert "herness.model.settings" not in loaded
    assert [name for name in loaded if not _allowed(name)] == []

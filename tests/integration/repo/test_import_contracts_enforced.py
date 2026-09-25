"""ST00-10: a planted upward import from the core base breaks C1 and C4."""

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]


def test_st00_10_upward_import_rejected(tmp_path: Path) -> None:
    """ST00-10 lint-imports fails naming 'herness layers' and 'core base is closed'."""
    shutil.copytree(ROOT / "herness", tmp_path / "herness")
    shutil.copytree(ROOT / "tools", tmp_path / "tools")
    (tmp_path / "herness" / "harness").mkdir()
    (tmp_path / "herness" / "harness" / "__init__.py").write_text("", encoding="utf-8")
    errors = tmp_path / "herness" / "core" / "errors.py"
    errors.write_text(
        errors.read_text(encoding="utf-8") + "\nimport herness.harness\n", encoding="utf-8"
    )
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    layers_marker = 'name = "herness layers"\ntype = "layers"\nlayers = [\n'
    assert layers_marker in text
    text = text.replace(layers_marker, layers_marker + '    "herness.harness",\n', 1)
    config = tomllib.loads(text)["tool"]["importlinter"]
    layers = next(c for c in config["contracts"] if c["name"] == "herness layers")["layers"]
    assert layers[0] == "herness.harness"
    base = next(c for c in config["contracts"] if c["name"] == "core base order")["layers"]
    sources = [name.strip() for layer in base for name in layer.split("|")]
    if any(c["name"] == "core base is closed" for c in config["contracts"]):
        # C4 forbids every existing top-level package of C1 other than herness.core
        # (impl 00 §2 row C4); the plant adds one, so extend the existing contract's
        # forbidden_modules rather than leave it unaware of the plant (it would
        # otherwise report KEPT, since "herness.harness" is not a real package).
        closed_marker = 'name = "core base is closed"'
        closed_start = text.index(closed_marker)
        forbidden_marker = "forbidden_modules = ["
        fm_start = text.index(forbidden_marker, closed_start)
        fm_end = text.index("]", fm_start)
        text = text[:fm_end].rstrip().rstrip(",") + ', "herness.harness"' + text[fm_end:]
    else:
        text += (
            '\n[[tool.importlinter.contracts]]\nname = "core base is closed"\n'
            'type = "forbidden"\n'
            f"source_modules = {sources!r}\n"
            'forbidden_modules = ["herness.harness"]\n'
        ).replace("'", '"')
    patched = tomllib.loads(text)["tool"]["importlinter"]["contracts"]
    closed = next(c for c in patched if c["name"] == "core base is closed")
    assert "herness.harness" in closed["forbidden_modules"]
    (tmp_path / "pyproject.toml").write_text(text, encoding="utf-8")
    lint = shutil.which("lint-imports")
    assert lint is not None
    result = subprocess.run(  # noqa: S603
        [lint],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=300,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )
    assert result.returncode != 0
    assert "herness layers BROKEN" in result.stdout
    assert "core base is closed BROKEN" in result.stdout

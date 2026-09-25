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
    # C1 is the first layers contract: put the planted package on top of it.
    text = text.replace("layers = [\n", 'layers = [\n    "herness.harness",\n', 1)
    config = tomllib.loads(text)["tool"]["importlinter"]
    base = next(c for c in config["contracts"] if c["name"] == "core base order")["layers"]
    sources = [name.strip() for layer in base for name in layer.split("|")]
    closed = 'name = "core base is closed"'
    if closed in text:
        head, tail = text.split(closed, 1)
        tail = tail.replace("forbidden_modules = [", 'forbidden_modules = ["herness.harness", ', 1)
        text = head + closed + tail
    else:
        text += (
            '\n[[tool.importlinter.contracts]]\nname = "core base is closed"\n'
            'type = "forbidden"\n'
            f"source_modules = {sources!r}\n"
            'forbidden_modules = ["herness.harness"]\n'
        ).replace("'", '"')
    (tmp_path / "pyproject.toml").write_text(text, encoding="utf-8")
    lint = shutil.which("lint-imports")
    assert lint is not None
    result = subprocess.run(  # noqa: S603
        [lint],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )
    assert result.returncode != 0
    assert "herness layers" in result.stdout
    assert "core base is closed" in result.stdout

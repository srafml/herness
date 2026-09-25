"""ST00-10: a planted upward import from the core base breaks C1 and C4."""

import os
import re
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
    harness_init = tmp_path / "herness" / "harness" / "__init__.py"
    if not harness_init.exists():  # T05-04 creates the real package
        harness_init.parent.mkdir()
        harness_init.write_text("", encoding="utf-8")
    errors = tmp_path / "herness" / "core" / "errors.py"
    errors.write_text(
        errors.read_text(encoding="utf-8") + "\nimport herness.harness\n", encoding="utf-8"
    )
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # Put herness.harness on top of C1 and into C4, whatever packages later cards added.
    current = {c["name"]: c for c in tomllib.loads(text)["tool"]["importlinter"]["contracts"]}
    if current["herness layers"]["layers"][0] != "herness.harness":
        text = text.replace(
            'name = "herness layers"\ntype = "layers"\nlayers = [\n',
            'name = "herness layers"\ntype = "layers"\nlayers = [\n    "herness.harness",\n',
            1,
        )
    if "herness.harness" not in current.get("core base is closed", {}).get("forbidden_modules", []):
        text = re.sub(
            r'(name = "core base is closed"[^\[]*?\nsource_modules = \[[^\]]*\]\s*'
            r"forbidden_modules = \[)",
            r'\1"herness.harness", ',
            text,
            count=1,
        )
    config = tomllib.loads(text)["tool"]["importlinter"]
    layers = next(c for c in config["contracts"] if c["name"] == "herness layers")["layers"]
    assert layers[0] == "herness.harness"
    base = next(c for c in config["contracts"] if c["name"] == "core base order")["layers"]
    sources = [name.strip() for layer in base for name in layer.split("|")]
    closed = [c for c in config["contracts"] if c["name"] == "core base is closed"]
    if closed:
        assert "herness.harness" in closed[0]["forbidden_modules"]
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
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=300,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )
    assert result.returncode != 0
    assert "herness layers" in result.stdout
    assert "core base is closed" in result.stdout

"""UT00-58: import-linter contracts list exactly the modules that exist (U00-52)."""

import tomllib
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
BASE = [
    "herness.core.logging",
    "herness.core._log_pipeline",
    "herness.core.types",
    "herness.core.ids",
    "herness.core.time",
    "herness.core.numbers",
    "herness.core.errors",
]


def _exists(module: str) -> bool:
    path = ROOT / module.replace(".", "/")
    return (path / "__init__.py").is_file() or path.with_suffix(".py").is_file()


def _flatten(layers: list[str]) -> list[str]:
    return [name.strip() for layer in layers for name in layer.split("|")]


def _config() -> dict[str, Any]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["importlinter"]


def test_ut00_58_contracts_match_repository() -> None:
    """UT00-58 C1-C6 list exactly the existing modules; one settings exception."""
    config = _config()
    contracts = {c["name"]: c for c in config["contracts"]}
    assert config["root_packages"] == [p for p in ("herness", "app", "tools") if _exists(p)]
    top = {
        f"herness.{p.name}" for p in (ROOT / "herness").iterdir() if (p / "__init__.py").is_file()
    }
    c1 = contracts["herness layers"]
    assert set(_flatten(c1["layers"])) == top
    exception = ["herness.core.config -> herness.**.settings"]
    if _exists("herness.core._config_sections"):  # config.py's private sibling (T10-03b)
        exception.append("herness.core._config_sections -> herness.**.settings")
    assert c1["ignore_imports"] == exception
    c2 = contracts["herness never imports app or tools"]
    assert c2["forbidden_modules"] == [p for p in ("app", "tools") if _exists(p)]
    base = {m for m in BASE if _exists(m)}
    assert set(_flatten(contracts["core base order"]["layers"])) == base
    core = ROOT / "herness" / "core"
    others = {f"herness.core.{p.stem}" for p in core.glob("*.py") if p.stem != "__init__"}
    others |= {f"herness.core.{p.name}" for p in core.iterdir() if (p / "__init__.py").is_file()}
    forbidden = (others - set(BASE)) | (top - {"herness.core"})
    if forbidden:
        assert set(contracts["core base is closed"]["forbidden_modules"]) == forbidden
    else:
        assert "core base is closed" not in contracts

    def _settings_name(name: str) -> bool:
        return name == "settings" or name.startswith("settings_") or name.endswith("_settings")

    settings = sorted(
        ".".join(p.relative_to(ROOT).with_suffix("").parts)
        for p in (ROOT / "herness").rglob("*settings*")
        if (p.suffix == ".py" and _settings_name(p.stem))
        or (p.is_dir() and _settings_name(p.name) and (p / "__init__.py").is_file())
    )
    assert ("settings modules are leaves" in contracts) == bool(settings)
    if settings:
        c6 = contracts["settings modules are leaves"]
        assert sorted(c6["source_modules"]) == settings
        assert not set(settings) & set(c6["forbidden_modules"])
        if _exists("herness.core.ids"):
            assert "herness.core.ids" in c6["forbidden_modules"]
    for contract in config["contracts"]:
        names = _flatten(contract.get("layers", []))
        names += contract.get("source_modules", []) + contract.get("forbidden_modules", [])
        for name in names:
            assert "*" in name or _exists(name), f"{contract['name']} lists missing {name}"

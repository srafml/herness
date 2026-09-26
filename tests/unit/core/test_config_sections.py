"""Tests for the real owner sections of the root config (impl 10 U10-08, U10-16; T10-03b).

The `app`, `memory` and `pipelines` sections of `HernessConfig` are the owners' models
(`AppConfig`, `MemoryConfig`, `PipelinesConfig`), loaded from the repository's shipped files
(copied by `tests.support.config_tree.write_repo_config`); `injection_patterns.txt` goes through
the memory owner's `parse_injection_patterns` (T07-02 ruling).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.support.config_tree import SHIPPED, register_checked_names, write_repo_config

from herness.core import _config_sections as sections
from herness.core import config as c
from herness.core import config_validate as cv
from herness.core import registry
from herness.core.errors import ConfigError
from herness.harness.memory.settings import MemoryConfig, parse_injection_patterns
from herness.harness.pipelines.settings import PipelinesConfig
from herness.reports.settings import AppConfig

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    register_checked_names()
    yield
    c.reset_config()
    registry.reset_registry()
    cv.reset_owner_validators()


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    return write_repo_config(tmp_path)


def _raw(stem: str) -> dict[str, Any]:
    data = yaml.safe_load((SHIPPED / f"{stem}.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    data.pop("version", None)
    return data


def _load(cfg_dir: Path, overrides: tuple[str, ...] = ()) -> c.HernessConfig:
    return c.load_config("local", overrides, config_dir=cfg_dir, env={})


def test_ut10_76_app_section_is_the_owner_app_config(cfg_dir: Path) -> None:
    """UT10-76 `cfg.app` is `AppConfig` equal to the shipped `app.yaml`; unknown keys fail."""
    cfg = _load(cfg_dir)
    assert isinstance(cfg.app, AppConfig)
    assert cfg.app == AppConfig.model_validate(_raw("app"))
    assert cfg.app.version == 1
    assert cfg.app.reports.formats == ["html", "md"]
    (cfg_dir / "app.yaml").write_text("version: 1\napp: {page_row_limit: 1}\n", "utf-8")
    with pytest.raises(ConfigError, match=r"app\.app\.page_row_limit \(app\.yaml\)"):
        _load(cfg_dir)


def test_ut10_76_pipelines_section_is_the_owner_pipelines_config(cfg_dir: Path) -> None:
    """UT10-76 `cfg.pipelines` is `PipelinesConfig` equal to the shipped `pipelines.yaml`."""
    cfg = _load(cfg_dir)
    assert isinstance(cfg.pipelines, PipelinesConfig)
    assert cfg.pipelines == PipelinesConfig.model_validate(_raw("pipelines"))


def test_ut10_76_memory_section_is_the_owner_memory_config(cfg_dir: Path) -> None:
    """UT10-76 `cfg.memory` is `MemoryConfig`: the shipped file plus the parsed patterns."""
    text = (SHIPPED / "injection_patterns.txt").read_text(encoding="utf-8")
    cfg = _load(cfg_dir)
    assert isinstance(cfg.memory, MemoryConfig)
    patterns = parse_injection_patterns(text)
    assert cfg.memory == MemoryConfig.model_validate(
        _raw("memory") | {"injection_patterns": patterns}
    )
    assert cfg.memory.outcome.window_weeks == 10  # R-34
    assert "(act|behave) as (an?|the) " in cfg.memory.injection_patterns  # trailing space kept


def test_ut10_76_injection_pattern_error_names_the_physical_line(cfg_dir: Path) -> None:
    """UT10-76 a bad pattern fails load with the owner's ConfigError naming its file line."""
    text = "# header\n\n   # indented comment\nok pattern\n(unclosed\n"
    (cfg_dir / "injection_patterns.txt").write_bytes(b"\xef\xbb\xbf" + text.encode())
    with pytest.raises(ConfigError, match=r"^injection_patterns line 5: ") as info:
        _load(cfg_dir)
    assert info.value.context["line"] == 5
    assert "(unclosed" not in info.value.message


def test_ut10_76_crlf_patterns_keep_trailing_spaces_and_drop_line_ends(cfg_dir: Path) -> None:
    """UT10-76 CRLF files: `\\r` is not part of a pattern; trailing spaces stay."""
    (cfg_dir / "injection_patterns.txt").write_bytes(b"# c\r\nyou are now \r\nsystem prompt\r\n")
    assert _load(cfg_dir).memory.injection_patterns == ("you are now ", "system prompt")


def test_ut10_76_higher_layer_patterns_are_kept_and_validated(cfg_dir: Path) -> None:
    """UT10-76 a `--set memory.injection_patterns` list wins; MemoryConfig still checks it."""
    cfg = _load(cfg_dir, ("memory.injection_patterns=[only this]",))
    assert cfg.memory.injection_patterns == ("only this",)
    with pytest.raises(ConfigError, match=r"memory\.injection_patterns\.0 \(memory\.yaml\)"):
        _load(cfg_dir, ("memory.injection_patterns=['(']",))


def test_ut10_76_memory_with_patterns_is_inert_outside_a_load() -> None:
    """UT10-76 outside `load_config` (or for a non-mapping) the section passes unchanged."""
    raw: dict[str, Any] = {"injection_patterns": ["x"]}
    assert sections.memory_with_patterns(raw) is raw
    assert sections.memory_with_patterns(None) is None
    assert c.SourcesFileConfig is sections.SourcesFileConfig
    assert c.ModelsFileConfig is sections.ModelsFileConfig

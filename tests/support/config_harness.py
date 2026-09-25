"""Test-local settings harness for the config layer sources (impl 10 U10-16 … U10-18).

``herness.core.config`` (T10-03) does not exist yet; this class wires the four sources in
the U10-08 priority order so the ``load`` rows can be exercised at source level.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from herness.core import config_sources as cs
from herness.core.settings import SecurityConfig

STEMS = (
    "sources",
    "mappings",
    "decisions",
    "metrics",
    "weights",
    "models",
    "pipelines",
    "memory",
    "resilience",
    "app",
)


class Harness(BaseSettings):
    """Minimal root model: ``security`` typed, every other section a plain mapping."""

    model_config = SettingsConfigDict(extra="forbid", frozen=True)

    profile: str = "local"
    paths: dict[str, Any] = {}
    security: SecurityConfig = SecurityConfig()
    logging: dict[str, Any] = {}
    retention: dict[str, Any] = {}
    backup: dict[str, Any] = {}
    deploy: dict[str, Any] = {}
    sources: dict[str, Any] = {}
    mappings: dict[str, Any] = {}
    decisions: dict[str, Any] = {}
    metrics: dict[str, Any] = {}
    weights: dict[str, Any] = {}
    models: dict[str, Any] = {}
    pipelines: dict[str, Any] = {}
    memory: dict[str, Any] = {}
    resilience: dict[str, Any] = {}
    app: dict[str, Any] = {}
    eval: dict[str, Any] | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            cs.GuardedEnvSource(settings_cls),
            cs.FilteredDotEnvSource(settings_cls),
            cs.ProfileYamlSource(settings_cls),
            cs.FilesYamlSource(settings_cls),
        )


def write_config(root: Path) -> Path:
    """Write a minimal valid ``config/`` tree under root and return the config dir."""
    cfg = root / "config"
    (cfg / "profiles").mkdir(parents=True)
    (cfg / "herness.yaml").write_text("version: 1\nlogging:\n  level: INFO\n", encoding="utf-8")
    for stem in STEMS:
        (cfg / f"{stem}.yaml").write_text("version: 1\n", encoding="utf-8")
    (cfg / "eval.yaml").write_text("version: 1\ngolden: {}\n", encoding="utf-8")
    (cfg / "injection_patterns.txt").write_text(
        "# comment\n\nignore previous\n  system:  \n", encoding="utf-8"
    )
    for name in ("local", "hybrid", "premium", "synth"):
        (cfg / "profiles" / f"{name}.yaml").write_text("version: 1\n", encoding="utf-8")
    return cfg


def load(
    cfg: Path,
    profile: cs.ProfileName = "local",
    env: dict[str, str] | None = None,
    **init: Any,
) -> Harness:
    """Build the harness inside a load context, as ``load_config`` will."""
    with cs.load_context(profile, cfg, env or {}):
        return Harness(profile=profile, **init)

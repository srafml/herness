"""Root config: model, load, cache, hash and effective dict (impl 10 U10-01, U10-08 to U10-14).

Composes every owner ``settings.py`` model (named import-linter exception, R-03). Sections
are validated only by their owners' models; this module adds the load rules of U10-09.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar, Final

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from herness.connectors.settings import SourcesConfig
from herness.core import config_sources as cs
from herness.core import config_view as view
from herness.core import time as clock
from herness.core.config_sources import SDK_SOURCE_KINDS, ProfileName
from herness.core.config_view import ConfigIssue
from herness.core.errors import ConfigError
from herness.core.logging import get_logger
from herness.core.resilience.settings import ResilienceConfig
from herness.core.settings import (
    BackupConfig,
    DataPolicyConfig,
    DeployConfig,
    LoggingConfig,
    PathsConfig,
    RetentionConfig,
    SecurityConfig,
)
from herness.enrich.settings import DecidersSettings, DecisionsConfig
from herness.eval.settings import EvalConfig
from herness.harness.llm.settings import ModelsConfig
from herness.metrics.settings import MetricsCatalogConfig, WeightsConfig
from herness.model.settings import BuildSettings, DqSettings, MappingsConfig

__all__ = [
    "GATED_PROFILES",
    "SDK_SOURCE_KINDS",
    "ConfigIssue",
    "HernessConfig",
    "ModelsFileConfig",
    "ProfileName",
    "SecurityConfig",
    "SourcesFileConfig",
    "config_hash",
    "effective_dict",
    "get_config",
    "init_config",
    "load_config",
    "reset_config",
]

GATED_PROFILES: Final[frozenset[str]] = frozenset({"hybrid", "premium"})
_log = get_logger("core.config")


class _Stub(BaseModel):
    """Closed stand-in for an owner model not yet on the branch (Ruling R2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _PipelinesStub(_Stub):
    """``pipelines.yaml`` until T06-03 lands ``PipelinesConfig``."""


class _MemoryStub(_Stub):
    """``memory.yaml`` until T07-02 lands ``MemoryConfig``; U10-16 adds the patterns."""

    injection_patterns: tuple[str, ...] = ()


class _AppStub(_Stub):
    """``app.yaml`` until T09-01 lands ``AppConfig``."""


class SourcesFileConfig(SourcesConfig):
    """``sources.yaml``: connector sections (impl 01) plus ``dq`` and ``build`` (impl 02, R-69)."""

    dq: DqSettings = DqSettings()
    build: BuildSettings = BuildSettings()


class ModelsFileConfig(ModelsConfig):
    """``models.yaml``: ``models`` and ``harness`` (impl 05) plus ``deciders`` (impl 03, R-76).

    ``_PREFIX = None`` leaves a root-level error of this file a ``ValidationError`` so that
    ``load_config`` can name ``models.yaml`` in the ``ConfigError``.
    """

    _PREFIX: ClassVar[str | None] = None  # type: ignore[assignment]  # owner narrowed it to str
    deciders: DecidersSettings


class HernessConfig(BaseSettings):
    """The immutable root config (U10-08, design 10 §3.1); built only by ``load_config``."""

    model_config = SettingsConfigDict(
        env_prefix="HERNESS_", env_nested_delimiter="__", extra="forbid", frozen=True
    )

    profile: ProfileName = "local"
    paths: PathsConfig = PathsConfig()
    security: SecurityConfig = SecurityConfig()
    logging: LoggingConfig = LoggingConfig()
    retention: RetentionConfig = RetentionConfig()
    backup: BackupConfig = BackupConfig()
    deploy: DeployConfig
    sources: SourcesFileConfig
    mappings: MappingsConfig
    decisions: DecisionsConfig
    metrics: MetricsCatalogConfig
    weights: WeightsConfig
    models: ModelsFileConfig
    pipelines: _PipelinesStub
    memory: _MemoryStub
    resilience: ResilienceConfig
    app: _AppStub
    eval: EvalConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def _in_load_context(cls, data: object) -> object:
        _require_context()
        return data

    @field_validator("metrics", "weights", mode="before")
    @classmethod
    def _restore_version(cls, value: object) -> object:
        # U10-16 drops the checked ``version: 1``; these two owner models declare it (impl 04).
        return {"version": 1, **value} if isinstance(value, dict) else value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """CLI overrides, env, dotenv, profile overlay, files (U10-16 to U10-18)."""
        _require_context()
        return (
            init_settings,
            cs.GuardedEnvSource(settings_cls),
            cs.FilteredDotEnvSource(settings_cls),
            cs.ProfileYamlSource(settings_cls),
            cs.FilesYamlSource(settings_cls),
        )


def _require_context() -> None:
    if cs.current_load_context() is None:
        msg = "HernessConfig must be built by load_config"
        raise ConfigError(msg)


# --- U10-09 load_config -------------------------------------------------------------------


def _check_file_only(overrides: Sequence[str]) -> None:
    for item in overrides:
        key = item.partition("=")[0]
        if key.split(".")[0] in {"security", "profile"}:
            msg = f"security.* and profile are file-only: {key[:100]}"
            raise ConfigError(msg)


def _check_gate(profile: ProfileName, policy: DataPolicyConfig) -> None:
    """Gated profiles and chat approval need a recorded approval (U10-09 step 5, R-38)."""
    recorded = policy.approved_by is not None and policy.approved_on is not None
    flags = {"hybrid": policy.hybrid_approved, "premium": policy.premium_approved}
    needed: list[str] = [profile] if profile in GATED_PROFILES else []
    needed += ["chat"] if policy.chat_approved else []
    for name in needed:
        if not (recorded and flags.get(name, True)):
            msg = f"profile {name} requires recorded approval in herness.yaml security.data_policy"
            raise ConfigError(msg, hint="record the approval in herness.yaml")


def _absolute_paths(cfg: HernessConfig, config_dir: Path) -> HernessConfig:
    """Resolve relative ``paths.*`` against the config dir's parent (U10-09 step 7)."""
    base = config_dir.resolve().parent
    update = {
        name: value if value.is_absolute() else (base / value).resolve()
        for name, value in cfg.paths
    }
    return cfg.model_copy(update={"paths": cfg.paths.model_copy(update=update)})


def load_config(
    profile: ProfileName | None = None,
    overrides: Sequence[str] = (),
    config_dir: Path = Path("config"),
    env: Mapping[str, str] | None = None,
) -> HernessConfig:
    """Build and validate the config from files, profile, dotenv, env and ``--set`` (U10-09)."""
    started = clock.monotonic()
    if not config_dir.is_dir():
        msg = f"config dir not found: {config_dir}"
        raise ConfigError(msg)
    context_env = dict(os.environ if env is None else env)
    name = cs.resolve_profile(profile, context_env)
    with cs.load_context(name, config_dir, context_env):
        override_dict = cs.parse_overrides(overrides)
        _check_file_only(overrides)
        try:
            cfg = HernessConfig(**override_dict, profile=name)
        except ValidationError as exc:
            raise view.validation_error(exc, cs.FILE_STEMS) from None
    _check_gate(name, cfg.security.data_policy)
    cs.check_profile_egress(name, cfg.security)
    cfg = _absolute_paths(cfg, config_dir)
    # T10-12: run_cross_checks(cfg, offline=True, include_registry=False) (U10-09 step 8).
    duration_ms = round((clock.monotonic() - started) * 1000)
    _log.info(
        "config.load.completed", profile=name, config_hash=config_hash(cfg), duration_ms=duration_ms
    )
    return cfg


# --- U10-10 process cache -----------------------------------------------------------------


class _Cache:
    config: HernessConfig | None = None


_CACHE_LOCK: Final = threading.Lock()
_RESET_HOOKS: list[Callable[[], None]] = []  # U10-45, U10-56, U10-58 append at import


def init_config(
    profile: ProfileName | None = None,
    overrides: Sequence[str] = (),
    config_dir: Path = Path("config"),
    env: Mapping[str, str] | None = None,
) -> HernessConfig:
    """Load the config and cache it for ``get_config`` (U10-10)."""
    with _CACHE_LOCK:
        cfg = load_config(profile, overrides, config_dir, env)
        if _Cache.config is not None:
            _log.warning("config.cache.replaced", profile=cfg.profile)
        _Cache.config = cfg
        return cfg


def get_config() -> HernessConfig:
    """The cached config; loads it with the defaults on first use (U10-10)."""
    with _CACHE_LOCK:
        if _Cache.config is None:
            _Cache.config = load_config()
        return _Cache.config


def reset_config() -> None:
    """Clear the cache and call every reset hook (U10-10)."""
    with _CACHE_LOCK:
        _Cache.config = None
    for hook in _RESET_HOOKS:
        hook()


# --- U10-11 config_hash, U10-12 effective_dict ----------------------------------------------

_KEY_ID_PROVIDER: Callable[[HernessConfig], str] | None = None  # set by herness.core.redact


def effective_dict(cfg: HernessConfig, *, redact_secrets: bool = True) -> dict[str, Any]:
    """JSON-shaped view for ``config show``, snapshots and hashing (U10-12)."""
    data = view.posix_paths(cfg.model_dump(), cfg.model_dump(mode="json"))
    return view.mask_secrets(data) if redact_secrets else data  # type: ignore[no-any-return]


def _key_id(cfg: HernessConfig) -> str:
    if _KEY_ID_PROVIDER is None:
        return "unresolved"
    try:
        return _KEY_ID_PROVIDER(cfg)
    except ConfigError:  # the key secret is absent
        return "unresolved"


def config_hash(cfg: HernessConfig, *, key_id: str | None = None) -> str:
    """``cfg_`` + 16 hex of the canonical effective dict (U10-11), stable across OS."""
    key = key_id if key_id is not None else _key_id(cfg)
    directory = view.directory_sha256(cfg.security.redaction.directory_file)
    return view.hash_effective(effective_dict(cfg), key_id=key, directory=directory)

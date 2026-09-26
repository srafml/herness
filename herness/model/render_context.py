"""The Jinja context of the build SQL files (impl 02 U02-84).

Templates see configuration values only (design 02 §4.1): DQ thresholds, custom field
column names, the lake inventory and the configured entity names. Free-text values (enum
maps, service overrides, class lists) never enter the context; they reach DuckDB as Arrow
tables (U02-87, TH02-10).
"""

from __future__ import annotations

import dataclasses
import types
from collections.abc import Mapping
from typing import Final, Protocol

from herness.core.errors import ConfigError
from herness.core.ids import is_valid_build_id
from herness.model.lakeinfo import LakeInventory
from herness.model.settings import CustomFieldsConfig, DqSettings

# Sources whose entities are defined by configuration rather than fixed (U02-78).
CONFIGURED_SOURCES: Final[tuple[str, ...]] = ("files", "mongodb", "snowflake", "dataverse")


class _SourceSection(Protocol):
    @property
    def entities(self) -> Mapping[str, object]: ...


class _SourcesFile(Protocol):
    """Structural view of the composed ``sources.yaml`` model (T10-03, R-69)."""

    @property
    def dq(self) -> DqSettings: ...

    @property
    def sources(self) -> object: ...


class _MappingsSection(Protocol):
    @property
    def custom_fields(self) -> CustomFieldsConfig: ...


class _BuildConfig(Protocol):
    """Structural view of ``T10-03 (herness.core.config.HernessConfig)``."""

    @property
    def sources(self) -> _SourcesFile: ...

    @property
    def mappings(self) -> _MappingsSection: ...


@dataclasses.dataclass(frozen=True, slots=True)
class RenderContext:
    """The only values SQL templates may see."""

    build_id: str
    dq: DqSettings
    custom_fields: CustomFieldsConfig
    lake: LakeInventory
    extra_entities: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        frozen = types.MappingProxyType(dict(self.extra_entities))
        object.__setattr__(self, "extra_entities", frozen)

    def template_vars(self) -> dict[str, object]:
        """Return the context fields plus ``raw_root`` (POSIX string of the raw root)."""
        return {
            "build_id": self.build_id,
            "dq": self.dq,
            "custom_fields": self.custom_fields,
            "lake": self.lake,
            "extra_entities": self.extra_entities,
            "raw_root": self.lake.root.as_posix(),
        }


def _configured_entities(sections: object) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for name in CONFIGURED_SOURCES:
        section: _SourceSection | None = getattr(sections, name, None)
        if section is not None:
            out[name] = tuple(section.entities)
    return out


def build_render_context(
    cfg: _BuildConfig, inventory: LakeInventory, build_id: str
) -> RenderContext:
    """Copy the template values out of ``cfg``; raises ConfigError for an invalid build id."""
    if not is_valid_build_id(build_id):
        msg = "invalid build id"
        raise ConfigError(msg)
    return RenderContext(
        build_id=build_id,
        dq=cfg.sources.dq,
        custom_fields=cfg.mappings.custom_fields,
        lake=inventory,
        extra_entities=_configured_entities(cfg.sources.sources),
    )

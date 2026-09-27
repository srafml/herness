"""Build a configured connector through the registry (impl 01 U01-55, ENG §2.2).

The class is resolved only by `herness.core.registry.get("connector", name)`; nothing in a
job payload selects a module or a class. Construction makes no network call.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Final, cast

from herness.connectors.base import Connector
from herness.connectors.settings import FilesSettings, MonitoringSettings
from herness.connectors.settings_base import SourceSettings
from herness.core import registry
from herness.core import time as clock
from herness.core.config import HernessConfig
from herness.core.errors import ConfigError

__all__ = ["build_connector"]

# U01-55 step 3: the Jira custom-field keys, in the order the connector requests them.
_JIRA_FIELDS: Final = ("story_points", "team", "estimate_cost_usd", "epic_link")
# The time module is referenced only here; inside the functions `clock` always means the
# injected callable (the U01-55 parameter name), never the module.
_DEFAULT_CLOCK: Final[Callable[[], datetime]] = clock.now


def _kwargs(
    name: str, settings: SourceSettings, cfg: HernessConfig, clock: Callable[[], datetime]
) -> dict[str, object]:
    """Connector-specific keyword arguments by source name (U01-55 step 3)."""
    if isinstance(settings, FilesSettings):
        inbox = settings.inbox
        root = inbox if inbox.is_absolute() else cfg.paths.data.parent / inbox
        return {"inbox_root": root.resolve(strict=False)}
    if name == "jira":
        fields = cfg.mappings.custom_fields.jira
        ids = (getattr(fields, key) for key in _JIRA_FIELDS)
        return {"custom_field_ids": tuple(value for value in ids if value)}
    if isinstance(settings, MonitoringSettings):
        enabled = [(tool, a) for tool, a in settings.adapters.items() if a.enabled]
        adapters = [registry.get("monitoring_adapter", tool)(a, clock=clock) for tool, a in enabled]
        return {"adapters": adapters}
    return {}


def build_connector(
    name: str, cfg: HernessConfig, *, clock: Callable[[], datetime] = _DEFAULT_CLOCK
) -> Connector:
    """Construct the enabled source `name` from `cfg` (U01-55); no network call is made.

    Raises ConfigError for a source that is not configured or disabled, or a name the
    registry does not resolve.
    """
    settings = cfg.sources.source(name)
    if not settings.enabled:
        msg = f"source {name} is disabled"
        raise ConfigError(msg, source=name)
    cls = registry.get("connector", name)
    return cast("Connector", cls(settings, clock=clock, **_kwargs(name, settings, cfg, clock)))

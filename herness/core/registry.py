"""Implementation registry: register, resolve and list plugins by (kind, name) (design 10 §3.2).

Built-ins are lazily imported import strings (`_BUILTINS`), so this module creates no
upward import edge for import-linter. Third-party plugins load once per process through
the `herness.plugins` entry-point group; every load is visible at WARNING (TH10-38).
"""

from __future__ import annotations

import importlib
import importlib.metadata
import re
import threading
from collections.abc import Callable
from typing import Any, Final, Literal, TypeVar

from herness.core.errors import ConfigError
from herness.core.logging import get_logger

__all__ = ["RegistryKind", "available", "get", "register", "reset_registry"]

RegistryKind = Literal[
    "connector", "monitoring_adapter", "decider", "llm_client", "tool", "embedder", "renderer"
]
_KINDS: Final[frozenset[str]] = frozenset(
    {"connector", "monitoring_adapter", "decider", "llm_client", "tool", "embedder", "renderer"}
)
_NAME_RE: Final = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_ENTRY_POINT_GROUP: Final = "herness.plugins"

_T = TypeVar("_T")

# Module-level registry state (accepted ENG §2.3 exception, mirrors herness.core.logging._State).
_REG_LOCK: Final = threading.RLock()
_REGISTRY: dict[tuple[str, str], object] = {}
_BUILTINS: dict[tuple[RegistryKind, str], str] = {
    ("llm_client", "openai_compat"): "herness.harness.llm.openai_compat:OpenAICompatClient",
    ("llm_client", "anthropic"): "herness.harness.llm.anthropic_client:AnthropicClient",
}
_entry_points_loaded = False

_logger = get_logger("core.registry")


def _validate(kind: str, name: str) -> None:
    if kind not in _KINDS:
        msg = "invalid registry kind: " + str(kind)[:40]
        raise ConfigError(msg, kind=str(kind)[:40])
    if _NAME_RE.fullmatch(name) is None:
        msg = "invalid registry name: " + name[:80]
        raise ConfigError(msg, name=name[:80])


def _store(kind: str, name: str, obj: object) -> None:
    with _REG_LOCK:
        key = (kind, name)
        existing = _REGISTRY.get(key)
        if existing is not None and existing is not obj:
            msg = "duplicate registration " + kind + ":" + name
            raise ConfigError(msg, kind=kind, name=name)
        _REGISTRY[key] = obj


def register(kind: RegistryKind, name: str) -> Callable[[_T], _T]:
    """Register the decorated object under (kind, name). Raises ConfigError."""
    _validate(kind, name)

    def decorator(obj: _T) -> _T:
        _store(kind, name, obj)
        return obj

    return decorator


def _dist_info(ep: importlib.metadata.EntryPoint) -> tuple[str | None, str | None]:
    dist = ep.dist
    return (None, None) if dist is None else (dist.name, dist.version)


def _load_entry_points() -> None:
    global _entry_points_loaded  # noqa: PLW0603 - module registry state (ENG §2.3)
    if _entry_points_loaded:
        return
    for ep in importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP):
        try:
            ep.load()
        except Exception as exc:
            _logger.exception(
                "registry.plugin.failed", entry_point=ep.name, error_type=type(exc).__name__
            )
            continue
        distribution, version = _dist_info(ep)
        _logger.warning(
            "registry.plugin.loaded",
            entry_point=ep.name,
            distribution=distribution,
            version=version,
        )
    _entry_points_loaded = True


def get(kind: RegistryKind, name: str) -> Any:  # noqa: ANN401 - an arbitrary class or factory
    """Resolve the class or factory registered under (kind, name). Raises ConfigError."""
    _validate(kind, name)
    with _REG_LOCK:
        obj = _REGISTRY.get((kind, name))
        if obj is not None:
            return obj
        module_attr = _BUILTINS.get((kind, name))
        if module_attr is not None:
            module_name, _, attr = module_attr.partition(":")
            try:
                module = importlib.import_module(module_name)
            except ImportError as exc:
                msg = "cannot import " + module_name
                raise ConfigError(msg, module=module_name) from exc
            _store(kind, name, getattr(module, attr))
            return _REGISTRY[(kind, name)]
        _load_entry_points()
        obj = _REGISTRY.get((kind, name))
        if obj is not None:
            return obj
        names = ", ".join(available(kind))
        msg = f"unknown {kind} '{name}'; available: {names}"
        raise ConfigError(msg, kind=kind, name=name)


def available(kind: RegistryKind) -> list[str]:
    """List resolvable names for kind: registered, built-in and plugin-provided."""
    with _REG_LOCK:
        _load_entry_points()
        names = {n for (k, n) in _REGISTRY if k == kind}
        names |= {n for (k, n) in _BUILTINS if k == kind}
        return sorted(names)


def reset_registry() -> None:
    """Clear registrations and the entry-point flag; keep `_BUILTINS` (test reset)."""
    global _entry_points_loaded  # noqa: PLW0603 - module registry state (ENG §2.3)
    with _REG_LOCK:
        _REGISTRY.clear()
        _entry_points_loaded = False

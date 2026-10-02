"""Enrichment: question set, embeddings, deciders, distillation and clustering (impl 03, L3).

Lazy facade (PEP 562): herness.core.config imports herness.enrich.settings, so an eager
import of `embed` here would be circular and would load LanceDB with the config.
`herness.enrich.health` is always the function: reach the module with
`importlib.import_module("herness.enrich.health")` (`import ... as` yields the function)."""

import sys
import types
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from herness.enrich.embed import embed_query
    from herness.enrich.health import health
    from herness.enrich.purge import purge_record

__all__ = ["embed_query", "health", "purge_record"]
_HOMES = {"embed_query": "embed", "health": "health", "purge_record": "purge"}


def __getattr__(name: str) -> object:
    home = _HOMES.get(name)
    if home is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    return getattr(import_module(f"{__name__}.{home}"), name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})


class _Facade(types.ModuleType):
    def __setattr__(self, name: str, value: object) -> None:  # drop only the submodule binding
        if not (name == "health" and isinstance(value, types.ModuleType)):
            super().__setattr__(name, value)


sys.modules[__name__].__class__ = _Facade

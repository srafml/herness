"""Enrichment: question set, embeddings, deciders, distillation and clustering (impl 03, L3).

Lazy facade (PEP 562): herness.core.config imports herness.enrich.settings, so an eager
import of `embed` here would be circular and would load LanceDB with the config."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from herness.enrich.embed import embed_query

__all__ = ["embed_query"]


def __getattr__(name: str) -> object:
    if name == "embed_query":
        from herness.enrich.embed import embed_query  # noqa: PLC0415 - lazy, see docstring

        return embed_query
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)

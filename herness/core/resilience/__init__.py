"""Resilience API of impl 08 (delta D08-01), re-exported lazily (PEP 562) so importing
`herness.core.resilience.settings` does not import the rest of the package. The export map
and its type-checking imports live in the private sibling `_exports` (T08-23).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from herness.core.resilience._exports import EXPORTS as _EXPORTS

if TYPE_CHECKING:
    from herness.core.resilience._exports import *  # noqa: F403 - typed re-exports only

__all__: tuple[str, ...] = tuple(sorted(_EXPORTS))


def __getattr__(name: str) -> object:
    """Import the owning submodule of `name` on first access (PEP 562)."""
    submodule = _EXPORTS.get(name)
    if submodule is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    module = importlib.import_module(f"{__name__}.{submodule}")
    # `classify` and `breaker` name their (callable) submodules too: always hand out the
    # module, never the function, so `import herness.core.resilience.<name> as m` works.
    value: object = module if name == submodule else getattr(module, name)
    globals()[name] = value
    return value

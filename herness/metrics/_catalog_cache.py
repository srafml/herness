"""Per-process memo of the loaded, validated catalog (impl 04 U04-24 memo, T04-24).

Key: SHA-256 of the catalog file bytes, of every SQL template its render reads and of the
weights JSON. The weights are the key's only config part because they are the only config
input of `validate_catalog` / `load_catalog`. INVARIANT: any new config input read by either
function MUST be added to the key (today: weights only), or catalogs go silently stale.

The build step receives the very bytes that were hashed, so key and content cannot diverge.
A failing build raises and stores nothing. In memory only: nothing is shared across processes.

One entry only (latest): a process uses one catalog path, so alternating between two paths
costs a reload but never serves a stale or foreign catalog; equal bytes at two paths share the
entry (the catalog holds no path). One lock serialises loads, so concurrent callers get one
object; it is also held while building, so a first load blocks other loaders and resets.
"""

import hashlib
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final, cast

from herness.core import config as _config

__all__ = ["cached_catalog", "reset_catalog_cache"]

_SQL_DIR: Final = Path(__file__).parent / "sql"
_LOCK: Final = threading.Lock()
_ENTRY: list[tuple[str, object]] = []


def _key(data: bytes, weights_json: str) -> str:
    """Digest of the catalog bytes, every SQL template and the weights."""
    digest = hashlib.sha256(data)
    for template in sorted(_SQL_DIR.glob("*.j2")):
        digest.update(template.name.encode() + b"\0" + template.read_bytes())
    digest.update(weights_json.encode())
    return digest.hexdigest()


def cached_catalog[T](path: Path, weights_json: str, build: Callable[[bytes], T]) -> T:
    """The catalog for `path` and weights; `build(bytes)` runs on a miss, errors uncached."""
    with _LOCK:  # held during the build: concurrent callers wait and share one object
        data = path.read_bytes()
        key = _key(data, weights_json)
        if _ENTRY and _ENTRY[0][0] == key:
            return cast("T", _ENTRY[0][1])
        catalog = build(data)
        _ENTRY[:] = [(key, catalog)]
        return catalog


def reset_catalog_cache() -> None:
    """Drop the memo (tests; wired to `reset_config`)."""
    with _LOCK:
        _ENTRY.clear()


_config._RESET_HOOKS.append(reset_catalog_cache)

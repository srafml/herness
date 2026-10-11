"""Per-process memo of the loaded, validated catalog (impl 04 U04-24 memo, T04-24).

Keyed by the digest of the catalog file and of the SQL templates its render reads, plus the
weights (the only config input of `validate_catalog`; narrower than `config_hash`), so an
edit or a change reloads. A failing load raises and stores nothing. In memory only: nothing is
shared across processes. One lock serialises loads, so concurrent callers get one object.
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


def _key(path: Path, weights_json: str) -> str:
    """Digest of the catalog bytes, every SQL template and the weights (the config input)."""
    digest = hashlib.sha256(path.read_bytes())
    for template in sorted(_SQL_DIR.glob("*.j2")):
        digest.update(template.name.encode() + b"\0" + template.read_bytes())
    digest.update(weights_json.encode())
    return digest.hexdigest()


def cached_catalog[T](path: Path, weights_json: str, build: Callable[[], T]) -> T:
    """The catalog for `path` and weights; `build` runs on a miss, its errors propagate uncached."""
    with _LOCK:
        key = _key(path, weights_json)
        if _ENTRY and _ENTRY[0][0] == key:
            return cast("T", _ENTRY[0][1])
        catalog = build()
        _ENTRY[:] = [(key, catalog)]
        return catalog


def reset_catalog_cache() -> None:
    """Drop the memo (tests; wired to `reset_config`)."""
    with _LOCK:
        _ENTRY.clear()


_config._RESET_HOOKS.append(reset_catalog_cache)

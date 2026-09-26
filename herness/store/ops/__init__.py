"""Public ``herness.store.ops`` namespace (impl 02 U02-62, §2.3, R-08).

Re-exports only. Each area adds one block of ``from .<area> import`` lines and one block of
``__all__`` names, headed by a comment with the owning spec number and area name, in the
import order of impl 02 §2.3 rule 5: ``core``, ``migrate``, ``shared``, then the other areas
in the row order of the area table. ``herness.store.ops.<name>`` and
``herness.store.ops.<area>.<name>`` are the same object. Importing this package opens no
connection.
"""

# 02 core
from .core import (
    OPS_JSON_MAX_BYTES,
    connection,
    dump_json,
    load_json,
    read_all,
    read_one,
    reset_connections,
    run_write,
)

__all__ = [  # noqa: RUF022 - block order of impl 02 U02-62, not sorted
    # 02 core
    "connection",
    "run_write",
    "read_one",
    "read_all",
    "dump_json",
    "load_json",
    "reset_connections",
    "OPS_JSON_MAX_BYTES",
]

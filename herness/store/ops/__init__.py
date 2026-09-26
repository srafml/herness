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

# 01 ingest
from .ingest import (
    FileIngestRow,
    SliceRow,
    Watermark,
    ensure_slices,
    get_file_ingest,
    get_watermark,
    list_watermarks,
    mark_slice_done,
    mark_slice_failed,
    mark_slice_running,
    record_file_ingest,
    set_watermark,
)

# 02 migrate
from .migrate import (
    MIGRATION_RANGES,
    MigrationReport,
    migrate,
    ops_health,
    pending_migrations,
    schema_version,
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
    # 02 migrate
    "MIGRATION_RANGES",
    "migrate",
    "pending_migrations",
    "schema_version",
    "ops_health",
    "MigrationReport",
    # 01 ingest
    "Watermark",
    "SliceRow",
    "FileIngestRow",
    "get_watermark",
    "set_watermark",
    "list_watermarks",
    "ensure_slices",
    "mark_slice_running",
    "mark_slice_done",
    "mark_slice_failed",
    "get_file_ingest",
    "record_file_ingest",
]

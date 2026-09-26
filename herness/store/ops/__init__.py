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

# 02 migrate
from .migrate import (
    MIGRATION_RANGES,
    MigrationReport,
    migrate,
    ops_health,
    pending_migrations,
    schema_version,
)

# 06 runs
from .runs import (
    RunRow,
    TaskRow,
    count_open,
    count_tasks,
    find_run_by_escalation,
    find_run_by_job,
    get_run,
    get_task,
    get_task_by_dedup,
    insert_run,
    insert_tasks,
    ready_tasks,
    select_runs,
    select_tasks,
    set_run_status,
    update_run_fields,
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
    # 06 runs
    "RunRow",
    "TaskRow",
    "insert_run",
    "get_run",
    "find_run_by_job",
    "find_run_by_escalation",
    "select_runs",
    "set_run_status",
    "update_run_fields",
    "insert_tasks",
    "get_task",
    "get_task_by_dedup",
    "select_tasks",
    "ready_tasks",
    "count_open",
    "count_tasks",
]

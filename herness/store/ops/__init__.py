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

# isort: split
# 02 shared
from .shared import (
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    approved_mapping_suggestions,
    create_review_item,
    decide_review_item,
    get_review_item,
    list_review_items,
)

# isort: split
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

# isort: split
# 05 evidence
from .evidence import (
    finding_statuses,
    get_evidence,
    record_evidence,
    record_evidence_use,
    scrub_record_from_evidence,
)

# isort: split
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

# isort: split
# 10 privacy
from .privacy import (
    DeletionRequest,
    create_deletion_request,
    deleted_record_ids,
    get_deletion_request,
    open_deletion_request,
    record_deletion_step,
    set_deletion_status,
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
    # 02 shared
    "ReviewItem",
    "ReviewKind",
    "ReviewStatus",
    "create_review_item",
    "get_review_item",
    "list_review_items",
    "decide_review_item",
    "approved_mapping_suggestions",
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
    # 05 evidence
    "record_evidence",
    "record_evidence_use",
    "get_evidence",
    "finding_statuses",
    "scrub_record_from_evidence",
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
    # 10 privacy
    "DeletionRequest",
    "create_deletion_request",
    "get_deletion_request",
    "open_deletion_request",
    "set_deletion_status",
    "record_deletion_step",
    "deleted_record_ids",
]

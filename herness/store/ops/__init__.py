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
    count_review_items,
    create_review_item,
    create_review_item_if_absent,
    decide_review_item,
    get_review_item,
    list_review_items,
    update_review_payload,
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
# 06 findings
from .findings import (
    get_findings,
    insert_finding,
    list_task_findings,
    query_findings,
    query_verified_findings_recent,
    scrub_record_from_findings,
    transition_finding,
)

# isort: split
# 09 chat
from .chat import (
    ChatMessageRow,
    ChatSessionRow,
    append_chat_message,
    count_user_turns,
    create_chat_session,
    find_assistant_message,
    get_chat_message,
    get_chat_session,
    latest_user_message,
    list_chat_messages,
    list_chat_sessions,
    purge_chat,
    set_chat_summary,
    update_chat_message,
    upsert_assistant_placeholder,
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

# isort: split
# 07 memory
from .memory import (
    UNCHANGED,
    EvidenceRow,
    FindingFact,
    MemoryItemRow,
    Unchanged,
    count_proposals,
    entity_candidates,
    evidence_rows,
    existing_query_ids,
    find_memory_item,
    finding_facts,
    fts_candidates,
    fts_check_and_rebuild,
    get_memory_items,
    get_task_scratchpad,
    insert_memory_item,
    maintenance_rows,
    pending_embedding_count,
    session_memory_ids,
    touch_memory_items,
    update_memory_item,
)

# isort: split
# 07 closed_loop
from .closed_loop import (
    DecisionRow,
    DueMeasurement,
    OutcomeRow,
    PromotionSource,
    RecommendationRow,
    SimilarityRow,
    accepted_since,
    dead_task_count,
    due_measurements,
    insert_decision,
    insert_outcome,
    insert_recommendations,
    latest_decisions,
    latest_outcomes,
    outcome_exists,
    outcomes_for_similarity,
    rec_memory_ids,
    recent_done_runs,
    recent_runs_with_recommendations,
    run_findings_for_promotion,
    run_recommendations,
    task_spec,
    treated_targets,
)

# isort: split
# 09 ui_reads
from .ui_reads import (
    UiDecisionRow,
    UiEvidenceRow,
    UiFindingRow,
    UiJobLiteRow,
    UiMemoryItemRow,
    UiOutcomeRow,
    UiRecommendationRow,
    UiResilienceEventRow,
    UiRunRow,
    UiSourceHealthRow,
    UiTaskRow,
    ui_evidence_ids_present,
    ui_failed_jobs_since,
    ui_finding_ids_with_status,
    ui_finding_query_ids,
    ui_finding_status_counts,
    ui_get_evidence_rows,
    ui_get_recommendation,
    ui_job_exists,
    ui_job_status_counts,
    ui_jobs_for_run,
    ui_latest_decisions,
    ui_list_findings_for_entity,
    ui_list_memory_items,
    ui_list_outcomes,
    ui_list_recommendations,
    ui_list_resilience_events,
    ui_list_run_findings,
    ui_list_runs,
    ui_list_source_health,
    ui_list_tasks,
    ui_oldest_queued_age_s,
    ui_run_rec_ids,
    ui_task_status_counts,
)

# fmt: off
__all__ = [  # noqa: RUF022 - block order of impl 02 U02-62, not sorted
    # 02 core
    "connection", "run_write", "read_one", "read_all", "dump_json", "load_json",
    "reset_connections", "OPS_JSON_MAX_BYTES",
    # 02 migrate
    "MIGRATION_RANGES", "migrate", "pending_migrations", "schema_version", "ops_health",
    "MigrationReport",
    # 02 shared
    "ReviewItem", "ReviewKind", "ReviewStatus", "create_review_item",
    "create_review_item_if_absent", "get_review_item", "list_review_items", "count_review_items",
    "decide_review_item", "update_review_payload", "approved_mapping_suggestions",
    # 01 ingest
    "Watermark", "SliceRow", "FileIngestRow", "get_watermark", "set_watermark", "list_watermarks",
    "ensure_slices", "mark_slice_running", "mark_slice_done", "mark_slice_failed",
    "get_file_ingest", "record_file_ingest",
    # 05 evidence
    "record_evidence", "record_evidence_use", "get_evidence", "finding_statuses",
    "scrub_record_from_evidence",
    # 06 runs
    "RunRow", "TaskRow", "insert_run", "get_run", "find_run_by_job", "find_run_by_escalation",
    "select_runs", "set_run_status", "update_run_fields", "insert_tasks", "get_task",
    "get_task_by_dedup", "select_tasks", "ready_tasks", "count_open", "count_tasks",
    # 06 findings
    "insert_finding", "transition_finding", "query_findings", "get_findings", "list_task_findings",
    "query_verified_findings_recent", "scrub_record_from_findings",
    # 09 chat
    "create_chat_session", "append_chat_message", "update_chat_message",
    "upsert_assistant_placeholder", "latest_user_message", "get_chat_session", "list_chat_sessions",
    "list_chat_messages", "get_chat_message", "find_assistant_message", "count_user_turns",
    "set_chat_summary", "purge_chat", "ChatSessionRow", "ChatMessageRow",
    # 10 privacy
    "DeletionRequest", "create_deletion_request", "get_deletion_request", "open_deletion_request",
    "set_deletion_status", "record_deletion_step", "deleted_record_ids",
    # 07 memory
    "MemoryItemRow", "FindingFact", "EvidenceRow", "Unchanged", "UNCHANGED", "insert_memory_item",
    "get_memory_items", "find_memory_item", "update_memory_item", "touch_memory_items",
    "fts_candidates", "entity_candidates", "count_proposals", "existing_query_ids", "finding_facts",
    "evidence_rows", "get_task_scratchpad", "maintenance_rows", "fts_check_and_rebuild",
    "pending_embedding_count", "session_memory_ids",
    # 07 closed_loop
    "RecommendationRow", "DecisionRow", "OutcomeRow", "DueMeasurement", "SimilarityRow",
    "PromotionSource", "insert_recommendations", "run_recommendations", "insert_decision",
    "latest_decisions", "insert_outcome", "outcome_exists", "due_measurements", "latest_outcomes",
    "treated_targets", "recent_runs_with_recommendations", "accepted_since",
    "outcomes_for_similarity", "rec_memory_ids", "dead_task_count", "run_findings_for_promotion",
    "task_spec", "recent_done_runs",
    # 09 ui_reads
    "UiRunRow", "UiTaskRow", "UiEvidenceRow", "UiRecommendationRow", "UiDecisionRow",
    "UiOutcomeRow", "UiFindingRow", "UiMemoryItemRow", "UiResilienceEventRow", "UiSourceHealthRow",
    "UiJobLiteRow", "ui_evidence_ids_present", "ui_get_evidence_rows", "ui_run_rec_ids",
    "ui_get_recommendation", "ui_finding_ids_with_status", "ui_finding_query_ids",
    "ui_finding_status_counts", "ui_task_status_counts", "ui_list_tasks", "ui_list_runs",
    "ui_list_recommendations", "ui_list_findings_for_entity", "ui_list_run_findings",
    "ui_jobs_for_run", "ui_latest_decisions", "ui_list_outcomes", "ui_list_memory_items",
    "ui_list_resilience_events", "ui_list_source_health", "ui_job_exists", "ui_job_status_counts",
    "ui_oldest_queued_age_s", "ui_failed_jobs_since",
]
# fmt: on

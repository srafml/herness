# T08-16 report — Task helpers

Status: DONE_WITH_CONCERNS (one spec note: foreign exceptions from `writes` are re-raised classified by run_write's retry layer)
Worktree: D:\herness\.claude\worktrees\agent-a6a5ad6121e37da59 (branch worktree-agent-a6a5ad6121e37da59, base 88a7f75)
Commits: bb2267c wip(T08-16): task helpers, TaskSqlMixin and core tests green; 65040a8 feat(jobs): T08-16 task helpers (final; pre-commit hooks all passed)

## Files (lines / budget)
- herness/core/jobs/tasks.py (new) 208 / 260 — RecoverySummary, recover_run_tasks, claim_task, build_checkpoint_envelope, save_checkpoint, complete_task, fail_task, release_task; constants CHECKPOINT_SCHEMA_VERSION=1, CHECKPOINT_MAX_BYTES=4_194_304, RESULT_MAX_BYTES=4_194_304, LAST_ERROR_MESSAGE_CHARS=2048.
- herness/store/ops/tasks.py (new) 187 / 300 — TaskSqlMixin (recover_tasks, claim_task, save_checkpoint, complete_task, fail_task, release_task), each one run_write (ops task_recover/task_claim/task_checkpoint/task_complete/task_fail/task_release).
- herness/store/ops/jobs.py 336 -> 337: SqliteJobsBackend(WorkerSqlMixin, TaskSqlMixin); docstring reflowed (ruling 1).
- herness/store/ops/__init__.py 310 -> 316 / 400: "# 08 tasks" import block after "# 08 jobs" (isort: split) + __all__ entry in the same block position (ruling 3). UT02-68 green.
- pyproject.toml: ops-areas-acyclic ignore "herness.store.ops.jobs -> herness.store.ops.tasks" with comment (ruling 1).
- tests/unit/core/jobs/test_jobs_tasks.py (new, 532 lines): UT08-66..71, UT08-109 via bound SqliteJobsBackend on the real migrated ops_store.
- tests/unit/store/ops/test_store_ops_tasks.py (new, 211 lines): mixin-level UT08-66..71/109 (counts, index plan, guards, loop_dropped return, attempt added to last_error).
- ports.py and herness/core/jobs/__init__.py untouched (ruling 2).

## Units
U08-57..U08-63, U08-97 all implemented.
- save_checkpoint (U08-60/97): in one BEGIN IMMEDIATE: SELECT checkpoint WHERE task_id AND status='running' (none -> JobStateError before writes run), build_checkpoint_envelope, writes(conn), guarded UPDATE (0 rows -> JobStateError, rollback). Backend returns loop_dropped; core logs WARNING jobs.checkpoint.loop_dropped. Stored checkpoint that is not a JSON object -> SchemaViolation("checkpoint envelope invalid").
- fail_task (U08-62): core decides retryable = isinstance(err, RetryableError) and builds last_error {class, message=redact_text(str(err))[:2048] ("" if redaction fails, fail closed), at}; backend reads attempts in the same txn, adds "attempt" (row attempts) completing the U08-50 shape, picks pending/dead, guarded UPDATE.
- recover (U08-57): three UPDATEs with updated_at=now (third only with retry_dead, attempts=0); summary + INFO jobs.tasks.recovered. EXPLAIN QUERY PLAN test proves all three use index task_run_status.
- release (U08-63): MAX(attempts-1,0); 0 rows -> DEBUG jobs.task.release_skipped.
- Argument checks: run_id regex and max_task_attempts (int>=1, bool rejected) -> ConfigError; task_id must be task_<ULID> (ids.is_valid_id) -> ConfigError; unknown checkpoint key -> ConfigError; non-object value -> SchemaViolation.

## Tests
RED: `PYTHONUTF8=1 uv run pytest tests/unit/core/jobs/test_jobs_tasks.py -q -p no:logging` before the mixin was wired -> 27 failed, 8 passed (AttributeError: backend has no task methods).
GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/core/jobs tests/unit/store/ops -q -p no:logging` -> 736 passed. Coverage (branch): core/jobs/tasks.py 100%, store/ops/tasks.py 100%.
Acceptance: UT08-69 test_ut08_69_save_writes_rollback / complete_writes_rollback / foreign_error_rolls_back / update_guard_rolls_back: finding row absent after the callback raises, checkpoint/status unchanged. UT08-109 test_ut08_109_concurrent_saves_keep_both_keys (thread A holds the write lock inside writes while thread B saves scratchpad on its own per-thread connection; both keys survive) + many_concurrent_rounds (15 barrier-synchronised racing rounds) + sequential state/loop/scratchpad/state.
Gates: ruff format/check clean, mypy (all) clean, lint-imports 13 kept, check_module_size 0, check_type_ownership 0; pre-commit hooks passed on the wip commit.

## Deviations / spec notes
1. "any exception -> rollback and re-raise" (U08-60/61): a HernessError from `writes` propagates unchanged; a foreign exception (e.g. RuntimeError) is rolled back but re-raised as the T08-07 retry layer's classified HernessError (FatalError "store call failed: ...") with the original as __cause__, because run_write wraps attempts in retry_call (core.py, never edited). Tests assert this. Spec 05/06/07 callers should raise HernessError subclasses from writes if they need the exact type.
2. last_error "attempt" is added by the backend (it alone reads attempts inside the transaction; the port's fail_task takes a prebuilt last_error). Shape equals U08-50.
3. Redaction of last_error.message uses herness.core.redact.redact_text (as classify does); tests install a fixed test redactor locally (same pattern as tests/unit/core/resilience/conftest.py test_redactor).
4. complete_task size check (<= 4 MiB canonical JSON) is done in core before the transaction; backend re-serialises with canonical_json.
5. Exports: names not added to herness.core.jobs._EXPORTS (U08-98 owns it); tests import from herness.core.jobs.tasks.

## Concerns
- Deviation 1 above (classification of foreign writes errors) — behaviour of the existing run_write/retry path, not changed here.

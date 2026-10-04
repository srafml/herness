# T08-16 review — Task helpers (verify agent)

Worktree: D:\herness\.claude\worktrees\agent-a6a5ad6121e37da59 · head 65040a8 · base 88a7f75 · read-only; tree left clean.

## Verification run
- `PYTHONUTF8=1 uv run pytest tests/unit/core/jobs/test_jobs_tasks.py tests/unit/store/ops/test_store_ops_tasks.py -q -p no:logging` → 48 passed.
- Branch coverage: herness/core/jobs/tasks.py 100 % (95 stmts, 24 branches), herness/store/ops/tasks.py 100 % (86 stmts, 10 branches).
- Wider: `tests/unit/core/jobs tests/unit/store/ops` → 736 passed (incl. UT02-68 export map).
- ruff check + ruff format --check clean; mypy clean on 5 touched files; lint-imports 13 kept / 0 broken; tools.check_module_size and tools.check_type_ownership clean (sizes: core tasks.py 208/260, store tasks.py 187/300, jobs.py 337/400, ops/__init__ 316/400).
- Mutation (reverted): read of `task.checkpoint` moved outside the `BEGIN IMMEDIATE` transaction in `TaskSqlMixin.save_checkpoint` → `test_ut08_109_concurrent_saves_keep_both_keys` and `test_ut08_109_many_concurrent_rounds` both FAIL. The UT08-109 concurrency tests are not vacuous. File restored with checkout; working tree status clean afterwards.

## Spec per unit
- U08-57 recover_run_tasks / RecoverySummary ✅ — one run_write(op="task_recover"); three §5.12 UPDATEs, each with updated_at; dead reset only with retry_dead (attempts = 0); running → pending with attempts unchanged; failed only when attempts < cap; done never matched; INFO jobs.tasks.recovered with counts; run_id regex and max_task_attempts ≥ 1 (bool rejected) → ConfigError. EXPLAIN QUERY PLAN test proves all three use index task_run_status.
- U08-58 claim_task ✅ — exact SQL; the only statement that increments attempts (`attempts + 1` appears only in _CLAIM); returns rowcount == 1.
- U08-59 build_checkpoint_envelope ✅ — steps 1–5 verbatim (new env {schema_version:1}; version/extra-key check → SchemaViolation("checkpoint envelope invalid"); key replaced, others kept; 4 194 304-byte cap with loop drop and loop_dropped = True; still over → "checkpoint exceeds 4 MiB without loop"). Canonical JSON via ids.canonical_json.
- U08-60 save_checkpoint ✅ — read + envelope + writes(conn) + guarded UPDATE in one run_write(op="task_checkpoint") (BEGIN IMMEDIATE); not running → JobStateError (checked before writes run, and again by the UPDATE guard); any exception rolls back; WARNING jobs.checkpoint.loop_dropped in core. Key-scoped (R-21).
- U08-61 complete_task ✅ — result canonical JSON ≤ 4 MiB (checked in core before the txn) else SchemaViolation; writes + guarded UPDATE to done in one run_write; 0 rows → JobStateError.
- U08-62 fail_task ✅ — attempts read inside the txn; pending only for RetryableError with attempts < cap, else dead; never writes 'failed'; guarded UPDATE; last_error = {class, message=redact_text(str(err))[:2048] ("" on redaction failure, fail closed), at, attempt} = U08-50 shape; no raw secret/ticket text (redacted, test proves an e-mail is removed).
- U08-63 release_task ✅ — MAX(attempts - 1, 0), running guard; 0 rows → DEBUG jobs.task.release_skipped, no raise.
- U08-97 TaskSqlMixin ✅ — each method one run_write; only single-row reads by task_id; recovery via (run_id, status) index; no SELECT over all tasks; core tasks.py runs no SQL (goes through require_jobs_backend()); SqliteJobsBackend(WorkerSqlMixin, TaskSqlMixin) per ruling.

Tests: UT08-66 ✅, UT08-67 ✅, UT08-68 ✅ (5 MB loop dropped + scratchpad kept + WARNING; 5 MB state → SchemaViolation; version 2 / extra key → SchemaViolation), UT08-69 ✅ (acceptance: finding row absent after writes raises, for save and complete; also foreign-error and UPDATE-guard rollback), UT08-70 ✅ (ModelUnavailable, OutputValidationError, BudgetExceeded, CircuitOpen at attempts 1 and 3 with max 3), UT08-71 ✅, UT08-109 ✅ (acceptance: sequential state/loop/scratchpad/state → exact envelope; concurrent loop + scratchpad saves keep both keys, proven non-vacuous by mutation). TH08-10 caps (checkpoint 4 MiB, result 4 MiB per U08-61) enforced.

## ⚠️ Builder concerns — judgment
1. Foreign (non-Herness) exception from `writes` is re-raised as the run_write retry layer's classified HernessError with the original as `__cause__`, not the bare original. Acceptable: rollback is guaranteed (run_write rolls back on BaseException), this is the existing U02-38 / T08-07 run_write + retry_call contract (not changed by this card), U08-60's precondition limits `writes` to SQL (sqlite errors are mapped by run_write anyway), and HernessError subclasses propagate unchanged. Worth a one-line note to impl 05/06/07 callers; no fix required in this card.
2. Backend adds `attempt` to last_error. Acceptable and correct: only the backend reads attempts inside the transaction; the resulting shape equals U08-50. Forced by the fixed port signature (ruling 2).
3. Extra argument checks (task_id format → ConfigError, unknown checkpoint key → ConfigError, non-object value → SchemaViolation, bool max_task_attempts rejected). Acceptable: defensive, consistent with U08-57's "Invalid arguments → ConfigError"; no spec path relies on looser behaviour.

## Findings
Critical: none.
Important: none.
Minor:
- M1 herness/core/jobs/tasks.py:166 + herness/store/ops/tasks.py:140 — complete_task serialises `result` to canonical JSON twice (cap check in core, then again in the backend), up to 2 × 4 MiB of work per completion. Harmless; could pass the bytes through if the port is ever revised.
- M2 herness/store/ops/tasks.py:130,186 — save_checkpoint stamps updated_at with its own `_now()` while every other mixin method takes `now` from core; forced by the fixed port signature (ruling 2). Informational only.
- M3 herness/core/jobs/tasks.py:68 — `_encode` message hard-codes "exceeds 4 MiB" regardless of `limit`; correct today (only caller uses 4 MiB). Nit.
- M4 tests/unit/core/jobs/test_jobs_tasks.py:522-532 — test_ut08_109_many_concurrent_rounds does not collect worker-thread exceptions or assert the threads finished after join(30); a failure still surfaces through the final envelope assert, so coverage is intact. Nit.

## Verdict
Approved

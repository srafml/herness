# T08-15 report: Outcome application (U08-49, U08-50)

## Files
- `herness/core/jobs/outcomes.py` (new, 240 lines; budget 260, hard limit 400): `FailureAction`, `decide_failure`, `finish_job`, constants `SKIP_ON_OPEN_CIRCUIT`, `MESSAGE_MAX_CHARS`, `MESSAGE_WITHHELD`.
- `tests/unit/core/jobs/test_jobs_outcomes.py` (new): UT08-58 (acceptance-check test + parametrized table 19 leaf classes, CircuitOpen future/past and RateLimited with/without retry_after variants x attempts {2,3}=below/at max(3) x kinds {sync, review} = 84 cases, plus reconcile and backoff-source tests), UT08-59 (21 tests).
- Not touched: `ports.py`, `herness/core/jobs/__init__.py` (tests import `herness.core.jobs.outcomes`).

## Evidence
- RED: `pytest tests/unit/core/jobs/test_jobs_outcomes.py` -> `ImportError: cannot import name 'outcomes' from 'herness.core.jobs'` (collection error) before the module existed.
- GREEN: card tests 110 passed; coverage outcomes.py 100% line / 100% branch (142 stmts, 38 branches).
- `pytest tests/unit/core/jobs -q -p no:logging`: 440 passed.
- ruff format/check clean, mypy (touched files) clean, lint-imports 13 kept 0 broken, check_module_size rc 0.

## Behaviour / decisions
- Every completion write (`finish_done/yield/requeue/failed`, `finalize_canceled`) is the backend's lease_owner-guarded statement; False -> WARNING `jobs.job.lease_lost` (`job_id`, `owner`, both cut to 64 chars) and return `lease_lost`, with no event, metric or chain advance (tested for done, yield, requeue, failed and canceled paths; row unchanged).
- Missing job on re-read (step 2): treated as `lease_lost` (WARNING only, no write, no event/metric). Spec is silent; chosen per brief guidance.
- `now` = `clock.now()` read once after the fault point; `duration_s = max(now - attempt_started_at, 0)`.
- Metrics: `herness_jobs_finished_total{kind,status}` with status done/yield/requeued/failed and `herness_jobs_run_seconds{kind}` recorded after every successful completion write (including yield and requeue); none on canceled or lease_lost.
- Events (`component="jobs"`, `target=row.kind`, `job_id`): `job_done` {kind, attempt, duration_s, partial}; `job_yield` {kind, attempt, stop_reason}; `job_failed` {kind, attempt, error_type}; `retry` {target:"job", policy:"job_backoff", attempt, error_type, wait_s}. The `jobs.job.done/.yielded/.failed` log lines come from `record_event` (its §8.1 mapping), so no duplicate log is written.
- `last_error.message = redact_text(str(err))[:2048]`; when `redact_text` fails (returns None) the placeholder `MESSAGE_WITHHELD` is stored (fail closed; never the raw text). Raw message never logged (tested).
- `decide_failure`: at max attempts for a backoff error the rng is not consumed (early return); CircuitOpen `retry_at` floored at `now`.
- Chain advance: only when `payload` has `schedule`; row re-read after the write (skip if gone); `advance_chain` HernessError is caught and logged ERROR `jobs.schedule.error` (`schedule`, `error_type`) mirroring the scheduler's error boundary - the job is already finished and chain repair re-runs the advance (F08-11). Spec does not say what finish_job does on a chain error; this is a decision.
- `stop_reason` typed with the ports `StopReason` alias (same Literal as the spec).

## Concerns
- None blocking. Decisions above (missing row -> lease_lost; chain error swallowed+logged; redaction-failure placeholder; run_seconds recorded on yield/requeue) are for verifier review.

## Commits
- 5055caf wip(T08-15): outcomes module and UT08-58/59 tests (all code; all pre-commit hooks passed incl. pytest-unit)
- fcad6f8 feat(jobs): outcome application (T08-15) (empty commit carrying the final card subject; hooks passed)

## Fix round 1 (review minors 1, 2, 5)
- (1) UT08-58 literal rows: `test_ut08_58_literal_rows` has 10 hard-coded rows with no oracle: SchemaViolation, BudgetExceeded and OutputValidationError below max give failed; StoreBusy below max gives requeue at 12:00:17 (backoff patched to 17 s) and at max gives failed; RateLimited(30) gives 12:00:30; RateLimited without retry_after gives 12:00:17; CircuitOpen on review gives requeue at retry_at, or failed at max; CircuitOpen on reconcile at max gives done with the literal partial result.
- (2) `test_ut08_59_refinishing_a_final_row_is_lease_lost` [done, failed]: a finished scheduled row is finished again with done, yield, requeue and failed outcomes. Each returns lease_lost. The row, events, counters and histograms are unchanged, and there is no chain advance.
- (5) `_apply_error`: a requeue decision without scheduled_for now raises `JobStateError("requeue decision without scheduled_for", job_id=...)` instead of falling through to failed. It is tested by `test_ut08_59_requeue_without_time_raises` (row unchanged, no event).
- outcomes.py is 249 lines (budget 260), with 100% line and branch coverage. Card tests: 123 passed. tests/unit/core/jobs: 453 passed. ruff, mypy and check_module_size are clean. m3 and the spec docs are unchanged.

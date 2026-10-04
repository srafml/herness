# T08-14 report: Scheduler and planned rekey

Worktree: D:\herness\.claude\worktrees\agent-a526ae10d1a12b778 (branch worktree-agent-a526ae10d1a12b778, base de08abc)
Commits: b509dd9 wip (scheduler + unit tests), 49d3ac8 wip (IT08-03), 73e0dc0 `feat(jobs): add scheduler, chains and planned rekey (T08-14)`.

## What was built
- `herness/core/jobs/scheduler.py` (372 lines, budget 390): `ScheduleEntry`, `SchedulerReport`,
  `collect_schedules` (U08-71), `run_scheduler` (U08-72), `advance_chain` (U08-73),
  `schedule_rekey` (U08-74); constants `SOURCE_SYNC_CATCH_UP` 1 h, `SOURCE_RECONCILE_CATCH_UP` 12 h (O08-03),
  `REPAIR_WINDOW` 24 h, `NEXT_KEY_REF = "redact.hmac_key.next"`, `REKEY_PRIORITY` 70.
  - Sources read via `cfg.sources.enabled_sources()` (no connectors import), `cfg.backup.nightly_at`,
    `cfg.resilience.schedule` (maintenance + jobs); business tz = `clock.zone(cfg.weights.business_timezone)`.
  - Unparsable cron (or catch-up text) -> ERROR `jobs.schedule.error` {schedule, error_type}, entry skipped.
  - Firing: `submit(JobSpec, sched_check=SchedCheck(name, ts(F)))`, idem `sync:<source>` / `sched:<name>:<F>`;
    `schedule_fired` event + `herness_jobs_schedule_fired_total{schedule}` only when created;
    `schedule_missed` + `herness_jobs_schedule_missed_total{schedule}` once per process via bounded (1000, oldest evicted) set
    and only when `sched_fired` is false. `nightly` gets `rekey_night: true` when `rekey_on(local day of F)`.
  - Per-entry HernessError boundary (log, count, continue); chain repair over `recent_scheduled(now-24h)`.
  - `schedule_rekey`: `secrets.resolve` at call time; key must be 64 hex (ConfigError without value);
    key_id = sha256(bytes.fromhex(v))[:8]; enqueue maintenance/decider/70 at first `schedule.rekey.cron` fire >= now+min_notice_h,
    idem `rekey:<key_id>`; `rekey_planned` {fire_at, key_id}. The secret value never enters payload/event/log (asserted).
- Tests: `tests/unit/core/jobs/_sched_env.py` (fixture `sched_db`: jobs_db + jira/servicenow sources.yaml + fake clock
  + empty missed set; helpers), `tests/unit/core/jobs/test_jobs_scheduler.py` (UT08-82, UT08-79, UT08-54, UT08-83, UT08-80),
  `tests/unit/core/jobs/test_jobs_scheduler_rekey.py` (UT08-81, UT08-80 rekey reason),
  `tests/integration/jobs/test_jobs_scheduler_rekey_night.py` (IT08-03, real SqliteJobsBackend on the migrated ops store).

## Deviations / interpretations (spec notes proposed)
1. U08-73 step 4 says `sched_check=None` for chain steps. With None, chain repair (every tick over 24 h of finished rows)
   RE-CREATES a step once it finished, because the job INSERT dedupes only active idem keys. The build passes
   `sched_check=SchedCheck(name, f"{F}:{k}")`: the store's sched key `sched:<name>:<F>:<k>` equals the step's idem key,
   so any existing step job (any status) blocks re-creation; the payload clause never matches (payload fire_at is F).
   Tested (UT08-80 Tuesday: repair after step 1 done/failed never makes a new job). Proposed spec note on U08-73.
2. U08-74 step 3 says `next_after(earliest - 1 minute)`; built as `next_after(earliest - 1 microsecond)` so a sub-minute
   `earliest` can never yield a fire before `earliest` (the spec's stated intent "first fire >= earliest"). Same result at whole minutes.
3. U08-74 key check: value must be 64 hex chars (the spec 10 redaction-key rule of `redact._load_key`), else ConfigError
   "redact.hmac_key.next must be 64 hex characters" (value never included).
4. Chain repair rows also get an error boundary (`jobs.schedule.error` with the row's schedule, counted in `errors`);
   the spec names the boundary only for entries. `chains_advanced` counts newly created step jobs only;
   `advance_chain` returns the (existing) step job id on repeats.
5. `schedule_fired`/`schedule_missed` events carry `target=<schedule>`; chain events targets per spec.
6. `rekey_night` is added for the entry named `nightly` only (spec wording).

## Carry-overs
- `herness.core.jobs.schedule_rekey` (and the other scheduler names) are NOT exported from `herness.core.jobs`
  (jobs/__init__.py untouched per dispatch; parallel group edits it) -> add `_EXPORTS` entries in the export card (U08-98 or the next jobs/__init__ edit).
- IT08-03 parts needing unbuilt symbols: the build's `gpu_scope("decider")` swap (JobContext implementation / supervisor, not in tree)
  and the supervisor's actual preempt at 21:00. Covered instead: GPU-slot claim order (rekey 70 before build 60, build class none on GPU slot,
  exclusive_kinds keeps them apart) and `windows.preempt_deadline("decider", ...)` is None through 23:00. Re-check both in the supervisor card.
- Supervisor must call `run_scheduler(clock.now())` every `R.jobs.reaper_interval_s` (supervisor card).

## Line counts
scheduler.py 372/390 (check_module_size exit 0). Tests: _sched_env 125, test_jobs_scheduler ~395, test_jobs_scheduler_rekey 132, IT 102.

## Test summary
- RED: `pytest tests/unit/core/jobs/test_jobs_scheduler*.py` -> ImportError: cannot import name 'scheduler' (2 collection errors).
- GREEN: card tests 27 passed; scheduler.py coverage 100 % line / 100 % branch.
- `pytest tests/unit/core/jobs tests/integration/jobs/test_jobs_scheduler_rekey_night.py` -> 261 passed.
- IT08-03 mutation checks: disabling the `rekey` skip reason, or rekey priority 10, both fail IT08-03.
- ruff check/format clean, mypy clean (247 files), lint-imports 13 kept, check_type_ownership ok, check_module_size exit 0,
  `--require-test-ids` on card files passes; commit hooks (incl. pytest-unit, detect-secrets) pass.

# T08-14 review: Scheduler and planned rekey

Worktree agent-a526ae10d1a12b778, base de08abc, head 73e0dc0. Reviewer: verify agent (read-only; mutation probes reverted with `git checkout --`, tree clean afterwards).

### Spec Compliance
- ✅ U08-71 `collect_schedules` / `ScheduleEntry`: sync (enabled + schedule, 1 h, idem `sync`), reconcile (12 h, `ReconcileSettings.schedule` is non-optional with default so "has reconcile.schedule" always holds), maintenance `MM HH * * *` from validated `backup.nightly_at` (regex-validated in settings) with purge `then`, every `S.jobs` entry; bad cron / catch-up text -> ERROR `jobs.schedule.error {schedule, error_type}`, skipped (scheduler.py:146-160). Cron parsing is bounded (cron.py digit cap 9, 5 fields, ConfigError only). Sources read through `cfg.sources.enabled_sources()`, no connectors import.
- ✅ U08-72 `run_scheduler`: latest fire, catch-up/missed, `rekey_night` for `nightly`, idem `sync:<source>` / `sched:<name>:<ts(F)>`, `submit(..., sched_check=SchedCheck(name, ts(F)))`, events + counters only when created, per-entry HernessError boundary, 24 h chain repair (scheduler.py:203-274). `_missed_reported` bounded 1000, oldest evicted, lock-guarded.
- ✅ U08-73 `advance_chain` with one justified deviation (see below): failed -> single `chain_broken`; skip reasons disabled / skip_on (business-tz weekday) / rekey (standard review on a rekey local day), `chain_skipped` de-duplicated via `count_events` (scheduler.py:285-337).
- ✅ U08-74 `schedule_rekey`: `secrets.resolve` at call time, 64-hex check identical to `redact._load_key` (redact.py:263-268), key_id = sha256(bytes)[:8], maintenance/decider/70, idem `rekey:<key_id>`, `rekey_planned {fire_at, key_id}` (scheduler.py:340-372).
- ✅ UT08-82 (test_jobs_scheduler.py:46-139), ✅ UT08-79 (145-197, 381-394; acceptance "exactly one job per missed fire" holds incl. restart with empty missed set), ✅ UT08-54 (203-222), ✅ UT08-83 (228-251), ✅ UT08-80 (267-378 + rekey reason in test_jobs_scheduler_rekey.py:117-132), ✅ UT08-81 (test_jobs_scheduler_rekey.py:48-114), ✅ IT08-03 reduced scope (test_jobs_scheduler_rekey_night.py) - see ⚠️.
- ✅ TH08-02: secret value never enters payload, event detail, log or error text (asserted at rekey test:74-75, 112).
- ✅ No edits to `herness/core/jobs/__init__.py` or `ports.py` (diff stat: 5 new files only).

Deviations judged:
1. U08-73 `sched_check=None` -> `SchedCheck(name, f"{F}:{k}")`: **correct, necessary and safe**. With the spec's None, chain repair re-creates a finished step because the INSERT only conflicts on active idem keys (`_job_sql.py` INSERT `ON CONFLICT ... WHERE status IN ('queued','running')`). Mutation probe confirms: reverting to None fails `test_ut08_80_tuesday_steps_follow_done_and_stop_on_failure`. Collision check against `SqliteJobsBackend.insert_job` (store/ops/jobs.py:129-134) / `SCHED_SEEN`: key `sched:<name>:<F>:<k>` equals exactly the step's own idem key; the payload clause (`$.fire_at = '<F>:<k>'`) can never match because payload `fire_at` is always a plain ts; top-level fires use `sched:<name>:<F>` (no `:k` suffix) so no cross-match. Needs a spec note on U08-73 (builder proposed one).
2. U08-74 `earliest - 1 µs` vs spec `- 1 minute`: correct reading of the stated intent "first fire >= earliest". `next_after` floors to the minute and requires `fire > t`; with −1 minute a sub-minute `earliest` (e.g. 19:00:30) would return 19:00 < earliest. Identical at whole minutes; UT08-81 expectations (Fri 10:00 -> Sat 19:00; Sat 10:00 -> next Sat 19:00) met. Probe with −1 minute fails `test_ut08_81_notice_boundary_counts_the_exact_minute`. Spec note recommended.
3. 64-hex validation: matches the spec 10 key rule; error message carries only the ref name.
4. Error boundary on chain-repair rows and `target=<schedule>` on fired/missed events: additive, harmless.

- ⚠️ Cannot verify here / carried over: IT08-03 `gpu_scope("decider")` swap and real supervisor preemption at 21:00 (JobContext/supervisor not in tree). The "no preemption" assertion (IT:86) only exercises `windows.preempt_deadline` with a test-chosen `class_since`, i.e. it checks window policy rather than the supervisor; the builder disclosed this honestly. Must be re-checked in the supervisor card, along with the supervisor calling `run_scheduler(clock.now())` every `reaper_interval_s`.
- ⚠️ `schedule_rekey` et al. not exported from `herness.core.jobs` (spec names `herness.core.jobs.schedule_rekey`); deliberately left for the export card per dispatch.

### Strengths
- Idempotence is layered correctly: idem key + sched check in one `BEGIN IMMEDIATE` insert, missed set + `sched_fired`, `count_events` for chain events; tested across a simulated restart.
- Tests are behavioural and non-vacuous. Mutation probes run (all reverted):
  - chain step `sched_check=None` -> UT08-80 Tuesday fails;
  - `_report_missed` ignoring `sched_fired` -> UT08-79 fails;
  - `_TICK` = 1 minute -> UT08-81 boundary fails;
  - dropping `rekey_night` marking -> UT08-80 rekey test and IT08-03 fail.
- IT08-03 uses the real `SqliteJobsBackend` on the migrated ops store (`_queue_env.jobs_db`), real claim ordering and `exclusive_kinds`.
- Clock only via `herness.core.time` (no `datetime.now`/`utcnow`); local-day bounds via `resolve_local` (DST gap/fold aware).

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
1. tests/unit/core/jobs/_sched_env.py:5-6,34 - business timezone is the shipped `America/New_York`; §11 `cfg_default` names `Europe/London` for DST cases and there is no scheduler test across a DST transition (weekday/local-day on the change day, `rekey_on` local-day bounds). Logic delegates to tested `resolve_local`, so risk is low; add one DST-day case when convenient.
2. tests/integration/jobs/test_jobs_scheduler_rekey_night.py:81 - loop seed `local(SAT, "19:30")` is earlier than the current fake time 19:40 (first tick lands at 20:00; 21:00 is still ticked); cosmetic, seed from 19:30 reads as if time went backwards.
3. tests/integration/jobs/test_jobs_scheduler_rekey_night.py:16-17 - integration test imports fixtures/helpers from `tests.unit.core.jobs._sched_env`; consider moving the shared env to `tests/support/` later.
4. scheduler.py:136-137 vs 130-133 - spec step 2 wording "sources with `src.reconcile.schedule`" is always true today (non-optional default); if `ReconcileSettings.schedule` ever becomes optional, `_Source(..., None, ...)` would hit `CronExpr.parse(None)` (not a ConfigError). Informational.
5. Spec notes to file: U08-73 sched_check for chain steps; U08-74 `earliest − 1 µs`.

### Evidence
- `PYTHONUTF8=1 .venv\Scripts\python.exe -m pytest <3 card files> -q -p no:logging --cov=herness.core.jobs.scheduler --cov-branch` -> 27 passed; scheduler.py 100 % line / 100 % branch (235 stmts, 54 branches).
- `pytest tests/unit/core/jobs -q -p no:logging` -> 260 passed.
- `lint-imports` -> 13 kept, 0 broken. scheduler.py 372 lines (budget 390). pytestmark unit/integration present; every test docstring names its ID.

### Assessment
**Task quality:** Approved
**Reasoning:** All four units and six test rows are met with real, mutation-sensitive tests on the real store; the two deviations are correct fixes of spec defects (chain re-creation, sub-minute notice) and need spec notes, not code changes. IT08-03's supervisor-dependent parts are honestly carried over.

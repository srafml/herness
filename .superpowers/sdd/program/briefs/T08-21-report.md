# T08-21 report — Supervisor, child entry and `run_worker` (U08-87, U08-88, U08-89)

Status: DONE_WITH_CONCERNS (see Concerns). Worktree `agent-a475763e8e03bde6c`, base 9c8f34a.
Commits: aee5506 wip, 31cc55b wip, final `feat(jobs): supervisor, child entry and run_worker (T08-21)` (SHA: see "Final commit" at the bottom).

## Files (lines / budget)
| File | Lines | Budget |
|---|---|---|
| herness/core/jobs/supervisor.py (`WorkerOptions`, `Supervisor`, `run_worker`) | 361 | 400 |
| herness/core/jobs/child.py (`child_main`, `call_bootstrap`, `outcome_message`) | 128 | 150 |
| herness/core/jobs/_supervisor_boot.py (private: timings, worker row, GPU lock, dead owners, lease_expired events, signals) | 147 | 180 (new §2 row) |
| herness/core/jobs/_supervisor_child.py (private: `ChildRun` spawn/drain/reap/lease/stall/preempt, error rebuild) | 280 | 320 (new §2 row) |
| herness/core/jobs/_supervisor_gpu.py (private: `GpuSlot` controller + `herness-gpu` executor, child GPU requests, arbiter step, restart check) | 220 | 260 (new §2 row) |
| docs/impl/08-resilience-and-jobs.impl.md | §2: 3 private-sibling rows; §8.1: 2 new log-event rows | |
| .secrets.baseline | line numbers of the two impl-08 entries only (no audited entry dropped) | |
| tests/support/worker_bootstrap.py (test bootstrap `tests.support.worker_bootstrap:bootstrap`, `write_worker_config`, fake handlers by `payload.mode`, `python -m` entry) | 153 | |
| tests/support/worker_env.py (`worker_env` fixture; subprocess worker with a list argv, CREATE_NEW_PROCESS_GROUP on Windows; `interrupt`, `wait_for`) | 179 | |
| tests/integration/jobs/conftest.py, tests/unit/core/jobs/conftest.py (fixture registration) | 5 + 5 | |
| tests/integration/jobs/test_jobs_worker.py (IT08-04..07, IT08-09..11) | 227 | |
| tests/integration/jobs/test_jobs_worker_signals.py (IT08-08) | 90 | |
| tests/integration/jobs/test_jobs_worker_gpu.py (IT08-13, ST08-09) | 121 | |
| tests/unit/core/jobs/_supervisor_env.py (`sup_env`: store + config + thread-backed fake spawn context; no process is started) | 231 | |
| tests/unit/core/jobs/test_jobs_supervisor.py / _child.py / _gpu.py / _st08.py, test_jobs_child.py | 352 / 257 / 289 / 81 / 163 | |
| tests/bench/test_jobs_worker_bench.py (BT08-11) | ~70 | |

Gates at head: ruff format/check clean, mypy strict 0 errors (297 files), lint-imports 13 kept / 0 broken, check_module_size exit 0, check_type_ownership exit 0; `pytest tests/unit/core/jobs tests/integration/jobs` 615 passed; `--require-test-ids --collect-only` clean. Pre-commit hooks ran on every commit (never skipped).

## Tests (per ID)
- IT08-04 in-process Supervisor, concurrency 1, 3 `none` jobs: all `done`, each in a distinct child pid (not the test's), `job_done` events, `herness_jobs_finished_total` / `_run_seconds` / `_queue_wait_seconds` rows in metric_sample (finish_job through real children — carry-over b).
- IT08-05 cancel while running: the `loop` handler yields → `canceled` finalised (child exit 0); the stop-ignoring handler is terminated ≥ 2 s (`cancel_grace_s` 2) later and finalised `canceled`; `jobs.job.canceled` for both.
- IT08-06 `stall_timeout_s` 3, silent handler: child killed (exitcode ≠ 0), job requeued with `last_error` ModelUnavailable/"stalled", one `retry` event, `jobs.job.stalled` logged; attempt 2 done.
- IT08-07 `os._exit(9)`: exitcode 9, `last_error` ModelUnavailable/"child_crash", attempt 2 done.
- IT08-08 (subprocess worker; Windows CTRL_BREAK_EVENT, POSIX SIGINT): one signal → job `queued`, attempts 0, checkpoint kept, `job_yield` stop_reason shutdown, worker exit 0, row `stopped`; two signals (stop-ignoring checkpoint handler) → exit 0, job still `running` under the old owner; the next worker start requeues it (one `lease_expired`, outcome queued, no attempt charge) and reclaims it.
- IT08-09 two `running` rows owned by `<host>:<dead pid>:cpu0`: `start()` requeues both in < 10 s, attempts unchanged (1), `lease_expired` events with outcome `queued`.
- IT08-10 backend raising StoreBusy on every call: 9 failed ticks leave the child untouched (alive, no stop sent); the 10th → one CRITICAL `jobs.supervisor.store_unavailable`, draining, exit code 1; ticks 11-12 still fail; the shutdown stop reaches the child (exit 0) and `stop()` returns 1.
- IT08-11 `run_worker(once=True)` with 2 queued: exactly one done, exit 0; nothing queued → `jobs.worker.nothing_to_do`, exit 0.
- IT08-13 (fake_gpu) chat-window noon: queued review p40/p75 and chat p75 (reasoning): the arbiter swaps to reasoning; chat and review-75 are done; review-40 stays queued; claimable_counts with the rule → 0 (carry-over f).
- ST08-09 a second GPU worker subprocess exits 1 (`jobs.worker.gpu_lock_held`) while the in-process worker holds the lock; `request_gpu_class("large")` in the chat window → WARNING `jobs.gpu.request_rejected` (requested large, window chat), `requested_class` cleared, no compose command for llamacpp-large (carry-over d, arbiter half).
- BT08-11 idle worker (shipped timings: tick_s 2, heartbeat_s 30, reaper 30; 2 CPU slots, no GPU slot) for 600 s: **cpu_percent 0.000 % of one core** (psutil.Process.cpu_percent over 600 s; one full-length run). Two short dry runs with a cpu_times cross-check: 0.000 s CPU over 30-40 s while the same process showed 0.77 s since start — idle ticks stay below the 15.6 ms Windows CPU-accounting granularity.
- UT08-62 (core half, carry-over a) expired leases at 1/3 and 3/3: the tick's reaper → queued with LeaseExpired / failed; `lease_expired` events with their outcomes; `herness_jobs_lease_expired_total{kind}` for both kinds.
- ST08-13 (supervisor half, carry-over c) pickled bytes / JSON with an extra field / a parent→child `stop` sent by the child → the child is marked crashed, terminated, job requeued `child_crash`; the pickle payload is never unpickled (unit test in tests/unit/core/jobs/test_jobs_supervisor_st08.py).
- ST08-01 (gpu_request half, carry-over d) a GPU child's `service_start("openjev")` while reasoning is loaded → `ConfigError` reply, no compose argv; a CPU-slot child's request → `ConfigError("GPU control requires the GPU slot")`. Service names outside `ServiceName` never pass `decode_message` (crash path).
- CV-T08-21 unit tests: options validation; start config errors / validator issues / GPU lock held → 1; dead-owner parsing; signals (first drains, second terminates, job stays running; handlers installed before start so an interrupt during start drains); `once`; unexpected loop error → 1 with children terminated; shutdown grace → yield without charge; failed-tick reset and store-unavailable; spawn OSError → child_crash; worker row current_jobs; ChildRun drain / EOF / replies / stops / terminate→kill / lease cancel / stall / preempt / a failed finish keeps the first verdict; error rebuild (CircuitOpen key+retry_at, RateLimited retry_after, unknown class → FatalError); GpuSlot requests, failed swap postpones the class 10 min, restart check, arbiter plan branches, preload skipped in once mode, GPU-slot end to end (in-job require_class answered with the previous class, row gpu_class_loaded); child_main (bootstrap errors, redaction + 2 KB cut, withheld on redaction failure, lease not ours → exit 1, missing handler → ConfigError outcome, unencodable outcome → SchemaViolation outcome, closed pipe ignored).

Coverage (`pytest tests/unit/core/jobs tests/integration/jobs --cov=herness.core.jobs --cov-branch`): supervisor.py 100 % line, 1 partial branch (99 % combined); _supervisor_boot 100 %; _supervisor_child 100 %; _supervisor_gpu 100 %; child.py 100 % line, 1 partial branch (99 %). Unit tests alone: supervisor.py 97 %, the others ≥ 99 %.

## Deviations from the spec (with reasons)
1. `WorkerOptions.concurrency: int | None` (spec `int`): `None` = `R.jobs.cpu_slots`, resolved after the bootstrap loaded config (`run_worker` cannot read config before step 1).
2. Error outcomes carry `CircuitOpen` `key`/`retry_at` and `RateLimited` `retry_after` in `outcome.result`, so the supervisor rebuilds the real error for U08-49 (otherwise an open circuit would retry at once and a 429 would use job backoff). An unknown `error_class` becomes `FatalError`.
3. Preempt-after-grace and the shutdown requeue call `finish_job(row, owner, JobOutcome(status="yield"), stop_reason="preempt" | "shutdown")` instead of `backend.finish_yield` directly: the same owner-guarded statement (preempt resumes at `next_window_allowing`, shutdown at now) plus the `job_yield` event and metric the spec asks for; a canceled job is finalised instead.
4. Cancel-grace expiry passes `ModelUnavailable("child did not stop")` to finish_job (which finalises the canceled job or logs lease lost, as specified).
5. While draining a tick runs b, c and h (so the row shows `draining`); a and d-g are skipped (spec: b-c).
6. `--once`: at most one job is claimed; preload swaps are skipped; the loop ends with `nothing_to_do` on a tick that claimed nothing, with no child and an idle GPU executor.
7. Arbiter step: a `requested_class` equal to the loaded class is cleared; one outside the worker's `--gpu-classes` is rejected like `request_not_allowed` (same log and clear); a swap target outside the worker's classes is ignored. The chat-window rule applies to the GPU slot only (CPU slots claim `none` without it), as step 7g says.
8. Crash recovery (step 4) records `lease_expired` events only; the counter comes from the step 7a reaper (spec text).
9. The step 5 handlers are installed at the start of `run()` (before steps 1-4), so an interrupt during start-up drains instead of ending with a KeyboardInterrupt traceback (R-46: exit 0/1 only).
10. New log events (rows added to §8.1): ERROR `jobs.worker.start_failed` (unexpected exception in `run()`, exit 1), WARNING `jobs.gpu.restart_failed` (the step 7a restart raised). Store errors during the shutdown steps are logged as `jobs.supervisor.tick_failed`.
11. On Windows a normal pipe close raises BrokenPipeError: treated as end of input (not a bad frame); other OSError (e.g. an oversized frame) and SchemaViolation → crash (ST08-13).
12. The ST08-13 / ST08-01 supervisor halves live in tests/unit/core/jobs (unit marker), like the child half in test_jobs_context.py.

## Carry-overs
Closed: a (UT08-62 core half: event + counter per reaped row; recovery events), b (IT08-04 finish_job via real children, events + metrics), c (ST08-13 supervisor half; stall → terminate → ModelUnavailable("stalled")), d (ST08-01 gpu_request half; request_rejected + clear on request_not_allowed), e (run_scheduler + reap_expired + run_due_probes + restart check + flush_metrics every reaper_interval_s), f (chat-window rule via queue._claim + claimable_counts, IT08-13), g (child_main: bind_ids, redact + 2 KB cut before building the message, ctx.close() before conn.close(), SIGINT/SIGBREAK ignored, exit 1 when the lease is not ours).
Remaining: h (IT08-03 re-check with the real supervisor — not cheap: the rekey-night test runs on a fake clock while children run on the real one; left for a later card). The `herness_jobs_queue_depth_count{gpu_class}` gauge (§8.2 row "U08-87 step 7a through U08-103") is not emitted: the step 7a algorithm text does not list it and claimable_counts under-counts `none` jobs; needs a query decision. chat_policy.py and resilience/_state.py untouched (wiring card). jobs/__init__.py and ports.py untouched: U08-98 must export `WorkerOptions`, `Supervisor`, `run_worker`, `child_main`.

## Concerns
- Windows console group: a CTRL_BREAK / Ctrl+C arriving during a job child's ~1 s start-up (before `child_main` step 1 ignores SIGINT/SIGBREAK) kills that child → `child_crash` with an attempt charge. Seen while writing IT08-08 (the tests now signal only after the child has checkpointed). multiprocessing spawn offers no creation flags to give children their own process group; a fix (own group per child) is a design decision for the controller.
- On Windows the venv `python.exe` is a launcher: `Popen.pid` is not the worker's pid; the tests match worker rows by appearance.
- BT08-11 reads 0.000 % (below the Windows accounting granularity): the < 1 % threshold holds with a wide margin, but it is not a precise number.
- `jobs.worker.config_invalid` is also logged for a bad bootstrap string or import error (no `paths`, `error_type` only).

## Final commit
8061e24 `feat(jobs): supervisor, child entry and run_worker (T08-21)` on branch worktree-agent-a475763e8e03bde6c (after wip aee5506, 31cc55b). All pre-commit hooks passed, pytest-unit included. The final commit was first made with a leftover wip subject and then amended once (local, unmerged) to carry the final subject.

## Fix round 1 (review T08-21-review.md)
- I1: IT08-13 now keeps ticking 15 more times at chat-window noon after chat/p75 are done, checking that the GPU slot never runs the p40 review, then asserts p40 is `queued` with attempts 0. New unit tests: `test_cv_t08_21_gpu_claim_uses_the_arbiter_filter` (the GPU `_claim` gets the plan's classes, min_priority 70 and exempt `["chat"]`; CPU claims get `["none"]`, None, ()) and `test_cv_t08_21_plan_chat_window_filter` (claimable_counts and the claim filter get (70, ["chat"]) at noon and (None, []) at night; also covers M2).
- Bug found while writing the I1 unit test and fixed: in step 7g, an empty CPU claim ran `return` instead of `break`. On a worker with both CPU slots and a GPU slot, the GPU slot was never arbitrated or claimed while the CPU queue was empty (only IT08-13 / ST08-09 with concurrency 0 hid it). Now `break`.
- I3: `test_cv_t08_21_reaper_step_runs_once_per_interval`: `run_scheduler` and `run_due_probes` run on the first tick and at +1 s (reaper_interval_s 1), not at +0.4 / +0.9 / +1.5 s.
- M1: `--once` with a spawn OSError: the job is finished as `child_crash` (requeued) and the worker drains and ends (exit 0) instead of looping. Test `test_cv_t08_21_once_spawn_failure_ends_the_loop`.
- M3: `test_cv_t08_21_child_main_closes_context_before_pipe` asserts the order ctx.close → conn.close.
- M7: a start ConfigError logs `jobs.worker.config_invalid` with `paths` = ["bootstrap"], ["HERNESS_FAULTS"] or ["resilience"] (the stage that failed); run_worker option errors already had ["options"].
- Mutation checks (each applied alone, `pytest tests/unit/core/jobs tests/integration/jobs -x`, then reverted with git checkout; tree clean after):
  - `min_priority=None` at the GPU claim: red (test_cv_t08_21_gpu_claim_uses_the_arbiter_filter). IT08-13 alone stays green under this single mutation, because the chat rule in claimable_counts already keeps the arbiter idle. With the whole chat rule removed, IT08-13 goes red.
  - exempt kinds `[]` in chat_rule: red (test_cv_t08_21_plan_chat_window_filter).
  - `run_scheduler(now)` removed: red. `run_due_probes(now)` removed: red (test_cv_t08_21_reaper_step_runs_once_per_interval).
  - `ctx.close()` removed: red (test_cv_t08_21_child_main_closes_context_before_pipe).
- Tests: `pytest tests/unit/core/jobs tests/integration/jobs` 620 passed. Coverage: supervisor.py 100 % line and branch; _supervisor_boot/_child/_gpu 100 %; child.py 100 % line, 1 partial branch. Gates clean. supervisor.py 364/400.
- Not changed (parked per the controller): I2 queue-depth gauge, M4, M5, M6, M8.

## Fix round 2 (IT08-13 fails after noon)
Commit e287f09 `test(jobs): make T08-21 worker tests clock-independent (IT08-13)` (test code only; no product change; hooks ran, none skipped).

Cause: `_chat_noon()` ticked the supervisor at *today's* 12:00 business time, but the jobs were enqueued at the real current time (`scheduled_for = clock.now()`). The arbiter step `GpuSlot.plan(now=tick)` calls `claimable_counts(now=tick)`, which only counts jobs with `scheduled_for <= tick`. Once the real clock passed noon in America/New_York, nothing was claimable, the arbiter never swapped to `reasoning`, and `_drive_at` timed out. (The claim itself reads `clock.now()` rather than the tick instant. In production the loop always ticks with `clock.now()`, so that is not a product bug. It is why only the arbiter half saw the mismatch.)

Tests changed:
- tests/integration/jobs/test_jobs_worker_gpu.py: `_chat_noon()` is now 12:00 on a fixed past day `CHAT_DAY = 2026-06-17`, and it asserts that this instant is in the chat window and before `clock.now()`. IT08-13 enqueues its three jobs with `scheduled_for = noon`, and the final `claimable_counts` uses `now=noon`. All assertions are unchanged: chat and p75 are done, `loaded == "reasoning"`, the GPU slot never runs p40 over 15 extra ticks, p40 stays `queued` with attempts 0, the counts with the rule are `{"reasoning": 0}`, and attempt 1 for both jobs. ST08-09 uses the same fixed noon.
- tests/unit/core/jobs/test_jobs_supervisor_gpu.py: `_local(hour)` used "the next Wednesday on or after today", which has the same latent bug. On a Wednesday after 12:00 or 23:00, `test_cv_t08_21_plan_requests_and_arbiter`, `test_cv_t08_21_supervisor_gpu_slot_end_to_end` and `test_cv_t08_21_plan_chat_window_filter` failed. RED was reproduced at Wed 2026-10-07 23:30 with HEAD's version: 3 failed, 5 passed. These tests now use the fixed Wednesday `DAY = 2026-06-17`, and their jobs are scheduled at or before the instants they plan or tick at (`_local(0)`, `night`, `noon`). `test_cv_t08_21_plan_preload_only_when_allowed` uses the same fixed day.
- tests/support/worker_env.py (`WorkerEnv.enqueue`) and tests/unit/core/jobs/_supervisor_env.py (`SupEnv.enqueue`): new optional `scheduled_for`.
- Also scanned, nothing to fix: IT08-04..11 in test_jobs_worker.py, including IT08-06 stall and IT08-10, whose ticks are relative to `clock.now()`. Also test_jobs_worker_signals.py, test_jobs_supervisor.py, test_jobs_supervisor_child.py (the `test_cv_t08_21_stall_*` tests are relative to `run.last_hb`), test_jobs_supervisor_st08.py, test_jobs_claim_concurrency.py (fixed T0 throughout) and test_jobs_scheduler_rekey_night.py (fake Clock throughout). None of them mixes a fixed or business-time tick with real-time enqueue, or assumes a time of day or DST offset.

Time-of-day proof: a scratch pytest plugin (since deleted) shifted the in-process `herness.core.time.now` so the business clock read the target time and kept running. It was run on test_jobs_worker_gpu.py and test_jobs_supervisor_gpu.py (10 tests):
- Before the fix, IT08-13 passed at 08:00 and failed at 13:00 (`condition not reached while driving the supervisor`).
- After the fix, all 10 tests passed at Fri 2026-10-02 08:00, 13:00 and 23:30, and at Wed 2026-10-07 23:30.
- A sweep of both jobs suites at Wed 13:00 failed only IT08-08 (two tests), IT08-01 and UT08-56. These are artefacts of the shift, not real dependencies: the first two use subprocess workers that run on the real clock, and UT08-56 builds its "future" job with `datetime.now()`. All of them pass on the real clock.

Gates: `pytest tests/integration/jobs tests/unit/core/jobs` gave 620 passed. `pytest -m "(unit or integration) and not slow"` gave 8109 passed, 13 skipped, 1 xfailed. ruff check and format, mypy (297 files), lint-imports (13 kept), check_module_size and check_type_ownership were all clean, and `pre-commit run --all-files` passed.

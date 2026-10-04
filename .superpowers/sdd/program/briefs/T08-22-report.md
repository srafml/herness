# T08-22 report — Inline runs, status, health and resume (impl 08, U08-90..U08-93)

Worktree: D:\herness\.claude\worktrees\agent-a3571a55e85686f72 (branch worktree-agent-a3571a55e85686f72, base 9e88eeb)
Commits: wip 4fe00f5 `wip(T08-22): run_inline, status module and IT08-12 tests`; final: see the FINAL line at the end.

## Implemented
- `herness/core/jobs/inline.py` (167/200): `run_inline(job_id)` per U08-90 steps 1-8. Owner `<host>:<pid>:cli`
  (`inline_owner()`), claim over all four classes by job id (None -> `JobStateError("job not claimable")`);
  GPU lock + `GpuController(worker_id=None, ComposeRunner(), LoopbackHttp())` when class != none or kind in
  `GPU_SLOT_KINDS`; held lock -> `finish_yield(now, now)` then re-raise; swap with reason `inline` only when the
  class differs from the detected one, a `ModelUnavailable` -> `finish_job(error)` then re-raise; daemon heartbeat
  thread (`herness-inline-heartbeat`, every `heartbeat_s`, lease `now + lease_s`, False -> `request_stop("cancel")`);
  SIGINT -> `request_stop("shutdown")` (main thread only); `run_handler(InlineJobContext, resolve_handler)` (a
  missing handler's ConfigError is the outcome, as in child_main); `finish_job(...)` with `ctx.stop_reason`;
  an ExitStack unwinds signal -> heartbeat -> lock; `bind_ids(job_id=...)` around the run (w16-s08b carry-over).
- `herness/core/jobs/status.py` (319/330): `StatusSnapshot` (TypedDict, the 15 §3.17 keys in table order),
  `status_snapshot(now=None)`, `ComponentHealth` + `health(now=None)`, `ResumeResult` + `enqueue_resume`.
- `herness/core/jobs/_status_views.py` (110/130, NEW private sibling): the nested view TypedDicts plus
  `window_view` and `next_job_view`. The §2 row was added to docs/impl/08 in the same commit (wip 4fe00f5).
- Reads go only through the ports, all bounded: `list_workers`, `queue_stats(now, now-24h)`,
  `list_jobs(status="running", limit=200)`, `list_jobs(status="queued", kind="maintenance", limit=200)`,
  `health_list(["open","half_open"])`, `event_counts`, `latest_event` x2, `run_row`; `gpu_state()` (cached, 2 s
  timeout) for the services of the local host's alive GPU worker only.

## Tests (IDs)
- IT08-12: tests/integration/jobs/test_jobs_inline.py. Case 1 ends `done` with the swap performed (compose up of
  vllm-reasoning, a gpu_swap event, the lock held during the handler and free afterwards). Case 2 raises a real
  `signal.raise_signal(SIGINT)` from the handler in the main thread (deterministic on Windows): the result is
  `yield`, the job is queued with attempts 0, job_yield has stop_reason shutdown, the lock is released and the
  previous SIGINT handler is restored. Also a resume-from-checkpoint test and 12 cv tests (CPU job takes no lock,
  build_pipeline takes the lock without a swap, no re-swap when loaded, unclaimable, held lock, failed swap,
  handler error, missing handler, cancel via heartbeat, heartbeat StoreBusy retried, off-main-thread, exports).
- UT08-100/101/102 + cv: tests/unit/core/jobs/test_jobs_status.py (15 tests). The acceptance test parses the
  §3.17 U08-91 table from docs/impl/08 and asserts `list(snapshot) == keys` and `StatusSnapshot.__required_keys__`.
- BT08-10: tests/bench/test_jobs_resume_bench.py (integration + slow).
- Clock: the status tests pin `herness.core.time.now` to 2026-09-01T00:00Z (no freezegun, see deviation 3). The
  inline tests do not depend on the time of day (inline runs ignore windows). BT08-10 uses two windows that cover
  the whole day and both allow `reasoning`.

## Carry-overs
- (a) IT08-12: DONE (above). Note in tests/unit/core/jobs/test_jobs_context_inline.py updated.
- (b) Resume without duplicate side effects: DONE in
  `test_it08_12_resume_continues_from_the_checkpoint_without_repeating` (step1 + save_state, then SIGINT -> yield;
  the second run_inline reads load_state, skips step1, and effects == [step1, step2]).
- (c) IT01-08: DONE. tests/integration/connectors/test_sync_jobs_flow.py now calls the real `run_inline` (the
  `ClaimedJobContext` stand-in is removed); the file has 5 passing tests.
- (d) IT08-03 real-supervisor re-check: NOT DONE (not cheap). The real supervisor runs job children as spawn
  processes on the real clock, while IT08-03 walks a fake clock from Sat 18:59 to 23:00. Carry-over to an impl 08
  follow-up or the U08-98 wiring card (owner: the controller assigns it).
- (e) `herness_jobs_queue_depth_count` gauge: NOT built here (ruling). Spec note: docs/impl/08 lines ~734 and
  ~2314 assign it to U08-87 step 7a via U08-103; U08-92 does not use it. Owner: T08-21 follow-up or a spec
  decision (it needs a queued-per-class port query; ports.py is at 300/300).
- (f) Real/fake clock mixing in test_jobs_queue_claim.py:60, IT08-08 and IT08-01: this card does not touch them,
  so they stay parked (owner: impl 08 test-hygiene follow-up).
- (g) jobs/__init__.py exports: NOT edited. Carry-over to the U08-98 export-map card: `run_inline` (inline.py);
  `StatusSnapshot`, `status_snapshot`, `ComponentHealth`, `health`, `ResumeResult`, `enqueue_resume` (status.py).
- (h) BT08-10: DONE as far as the tree allows (there is no CLI). Path: enqueue_resume, then the real in-process
  Supervisor (GPU slot, reasoning preloaded on fake_gpu) claims the job, spawns the child and runs the handler.
  The handler is the bootstrap fake handler, so the fake LLM is never reached (the tree has no real review
  handler or task layer yet). Measured over 3 runs: claimed in 0.053 / 0.050 / 0.017 s; first task (handler) done
  in 1.325 / 1.319 / 1.237 s; limit 30 s. The CLI path is T09-22's.

## Deviations / spec notes
1. New private sibling `_status_views.py` (status.py was 388 lines as one file); the §2 row is mirrored (budget 130).
2. New log event `jobs.inline.heartbeat_failed` (WARNING, job_id, error_type): a heartbeat that raises a
   HernessError is logged and retried at the next beat instead of killing the thread. §8.1 row added.
3. The status tests pin `clock.now` with monkeypatch instead of the freezegun `fake_clock`. With freezegun active,
   a lazy import of pandas (through lancedb/pyarrow from herness.enrich.embed) crashed the interpreter with the
   known stack overflow, the same root cause as the pre-existing tests/unit/core/jobs/test_jobs_gpu.py issue. The
   status code reads time only through `clock.now`.
4. `health()` and `status_snapshot()` apply the U08-54 liveness rule at the given `now` rather than calling
   `worker_alive()`, which reads the clock itself, so a passed `now` is honoured consistently. The rule and its
   constants are the same (gpu.ALIVE_*).
5. Snapshot instants are aware UTC `datetime`s (the spec does not name a format; spec 09 renders them). The ok
   reason of `health()` is "ok". Degraded reasons are joined with "; " in this order: breaker open, dead letters
   in 24 h, faults enabled, gpu unavailable. "breaker open" counts state `open` only; half_open does not degrade.
6. `planned_rekey` reads queued maintenance jobs through `list_jobs(..., limit=200)`, not `rekey_on`, which also
   returns running/done rows and needs a range. The earliest `scheduled_for` wins.
7. `running.slot` is the lease-owner suffix (`gpu`, `cpuN`, `cli`); `heartbeat_note` comes from the workers'
   current_jobs.
8. `breakers.probe_due` uses `breaker.probe_due(row, _family(key))` (the private `_family` is imported from
   breaker); it is None for half_open.
9. A ConfigError from resolve_handler in run_inline becomes the applied outcome (job failed) and is then raised,
   mirroring child_main.

## Sizes
inline.py 167/200; status.py 319/330; _status_views.py 110/130. check_module_size exit 0.

## Coverage (card tests: status unit + inline integration + IT01-08)
inline.py 100% line / 100% branch; status.py 100% / 100%; _status_views.py 100%.

## Mutation probes (script C:\Users\santh\AppData\Local\Temp\w24-s08-T08-22\mutate.py)
- skip finish_job in run_inline: RED (killed)
- skip the GPU lock release (lock entered outside the ExitStack): RED (killed)
- drop the status key `dead_letters`: RED (killed)

## Gates (before the final commit)
ruff check pass | ruff format --check pass (912) | mypy 0 issues (336 files) | lint-imports 13 kept 0 broken |
check_type_ownership 0 | check_module_size 0 | tests/unit/core/jobs + tests/integration/jobs + the IT01-08 file
(`-p pandas`, not slow): 659 passed | BT08-10 passed 3 times.
Known pre-existing issue: test_jobs_gpu.py crashes with a stack overflow unless pandas is preloaded (run with `-p pandas`).

FINAL: 8f616da feat(jobs): add inline runs, status snapshot, health and resume (T08-22) — all hooks passed, NO SKIP; worktree clean.

## Fix round 1 (review Approved; Minor M5 and M1)
- M5: `_beat` now catches any `Exception` of one beat (ENG §3.4 thread boundary, `noqa: BLE001`), logs WARNING
  `jobs.inline.heartbeat_failed` with job_id and error_type only (no message), and beats again. Test: the heartbeat cv
  test is parametrized with StoreBusy and RuntimeError and asserts the log entry (level, error_type, no message text).
- M1: step 2 fails safe. Apart from a swap `ModelUnavailable` (applied by `finish_job`), any exit from step 2 (the
  held-lock ConfigError, KeyboardInterrupt before the SIGINT handler exists, a detection error) calls
  `finish_yield(job_id, owner, now, now)` with no attempt charge and re-raises. The spec step order is kept and the
  heartbeat is not moved. Test: `test_cv_t08_22_interrupt_during_step_2_yields_the_job` uses a fake controller and
  covers KeyboardInterrupt in swap, KeyboardInterrupt in detect, and RuntimeError in detect: job queued, attempts 0,
  no last_error, lock released, handler not run.
- Spec note added under U08-90 in docs/impl/08: no heartbeat during step 2 (start_timeout_s can exceed lease_s, so a
  reaper may requeue the job and the run ends as lease lost), Ctrl+C during step 2 yields the job, and the heartbeat
  catches any Exception.
- Mutations (script C:\Users\santh\AppData\Local\Temp\w24-s08-T08-22\mutate2.py): narrowing the catch back to
  HernessError: RED (1 failed); dropping the step-2 finish_yield: RED (4 failed). Files restored after each probe.
- Sizes: inline.py 174/200. Coverage of inline.py: 100% line / 100% branch.
- Gates: ruff check pass | format pass (912) | mypy 0 (336) | lint-imports 13 kept | check_module_size 0 |
  tests/unit/core/jobs + tests/integration/jobs + IT01-08 file (`-p pandas`, not slow): 663 passed.
- FINAL fix round 1: 3d64968 fix(jobs): harden inline heartbeat and step-2 interrupt (T08-22). All hooks passed with no SKIP; the commit includes a .secrets.baseline line-number refresh (LF).

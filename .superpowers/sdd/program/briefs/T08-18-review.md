# T08-18 review — GPU controller and state reader (verify agent)

Worktree agent-a6a5ad6121e37da59, head fb20827 (code in 0cbaea2), base 65040a8. Read-only; one mutation run and reverted, `git status` clean.

## Evidence run
- `PYTHONUTF8=1 uv run pytest tests/unit/core/jobs/test_jobs_gpu.py -q -p no:logging --cov=herness.core.jobs.gpu --cov-branch` → 33 passed; gpu.py 99% (283 stmts, 1 miss = line 57 `_gpu()` real-config path, always monkeypatched; partial branches 168->174, 272->274).
- ruff check + format --check clean; mypy (gpu.py + test) clean. gpu.py 391 lines (budget 400).
- Acceptance mutation: `_swap_steps` changed to bring up the target before stopping the other class (VRAM wait dropped; the verbs/VRAM asserts of the UT08-91 main test temporarily removed). `test_ut08_91_swap_decider_to_reasoning` then failed at `fake_gpu.assert_single_class()` (`running_classes` showed two classes). The check is not vacuous. Reverted with `git checkout --`.
- Focused checks: grep `subprocess|shell|httpx|argv` in gpu.py → none (docstring mention only); no `httpx.Client/AsyncClient` under herness/core/jobs (UT08-103 holds). `worker_alive` exists nowhere in herness/ (confirms deviation 1). `retry_call` takes only a `PolicyName` (confirms deviation 2).

### Spec Compliance
- ✅ U08-81 `GpuController`: constructor/properties, internal lock, kill_service_hook on construction (gpu.py:110-113), swap steps 1-8 (gpu.py:196-247), service_start/stop/healthy (267-292), detect_loaded_class (294-313), restart_service rate limit via `count_events` over 1 h vs `restart_max_per_hour` (315-330). Events gpu_swap / gpu_swap_failed / service_start / service_stop / service_restart; histogram `herness_jobs_gpu_swap_seconds{from,to}`, counter `herness_jobs_gpu_swap_failed_total{to}`; health_reset(endpoints) on success, breaker failures on start/warmup.
- ✅ U08-83 `WorkerGpuState` / `gpu_state()`: 5 s list_workers cache, 5 s per-service health cache with 2 s timeout, caches under a lock (gpu.py:333-380).
- ✅ U08-102 `request_gpu_class`: alive gpu_slot=1 worker → `set_requested_class` + INFO `jobs.gpu.class_requested`; else `no_worker` with no write (383-391). Only writes `requested_class`; never touches compose (TH08-09 holds).
- ✅ Tests UT08-91..94, UT08-96, UT08-108 present with IDs; gpu_state rows as `test_cv_t08_18_*` (controller ruling).
- ✅ TH08-01: gpu.py builds no argv and parses no output; all compose/VRAM/HTTP goes through gpu_services (`ComposeRunner`, `wait_vram_free`, `LoopbackHttp`). Failure logs carry service + error_type only; foreign exceptions become `ModelUnavailable("gpu <what> failed")` with no foreign text (gpu.py:79-84; test_jobs_gpu.py:311).
- ✅ TH08-09: no GpuLock bypass in gpu.py (it takes no lock, per ruling); no path makes a second class resident — step 3 stops every running configured service before VRAM wait and start; detect stops all on >1 class; request_gpu_class only writes the request.
- ⚠️ Cannot verify here: that the supervisor (later card) distinguishes its own `requested_class` write (swap step 2) from an external request and clears it after a failed swap (Minor 2); FT08-09/FT08-12/BT08-06..08 belong to other cards.

### Deviations judged
1. Private `_alive` instead of U08-54 — accepted. `worker_alive` is absent from the tree and is specified as `worker_alive() -> bool` over any row, not a per-row predicate; the rule matches (status starting/running/draining, heartbeat > now − 3×heartbeat_s; 89 s / 91 s tested). Consolidate when U08-54 lands (spec note).
2. Own health poll instead of `retry_call` — accepted. `retry_call` accepts only a PolicyName, so a replaced `GPU_HEALTH_POLICY` cannot be passed; the loop uses the replaced policy's attempts/timeout/max_elapsed with `full_jitter_delay` and `process_state().sleep/rng`. Small overshoot of max_elapsed (Minor 4).
3. Step 3 stops the target's own running services too — accepted as correct. Under the single-class invariant the target's services only run when target == loaded (or as leftovers after a failure); step 1 already returns 0.0 when loaded-and-healthy, so step 3 is reached only when the target is unhealthy, and the literal spec step 3 would leave it holding VRAM so step 4 would always fail `vram_not_freed`. It does not change UT08-91 (decider→reasoning stops only openjev) and causes no needless restarts (decider has no start_on_entry services, so swap("decider") while decider is loaded is always 0.0). Side effect: a non-entry service of the target (started via service_start) is stopped and not restarted on an unhealthy re-entry — acceptable, the supervisor swaps only with the GPU slot free. Spec note for impl 08 step 3.
4. `requested_class` left set after failure — acceptable for this card (spec silent), but see Minor 2.
5. Failed restart still records `service_restart` (reason `failed`) — accepted; a failing restart counts toward the cap and cannot loop (design §5.3 intent). Spec note.
6. `gpu_state` singleton in module `_Holder` — see Minor 1.
7. Settings via private `_gpu()` monkeypatched in tests — acceptable (same expression as `gpu_services._gpu`); leaves line 57 uncovered (Minor 3).

### Strengths
- Fail-closed swap is thorough: any exception in steps 3-6 → target services stopped quietly, loaded/worker `none`, `gpu_swap_failed` with the step, failure counter, `ModelUnavailable` without foreign text; breaker failures only for start/warmup as spec step 8 says.
- Tests run the real `ComposeRunner` over `fake_gpu` and a real migrated ops store; they assert argv order, worker row transitions (`swapping` then class), events, breaker rows, histogram/counter keys and single-class history.
- Stop-with-kill-fallback and stop-errors-logged-not-raised paths are covered (sticky services, failing stop/kill).

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/core/jobs/gpu.py:370-380 — `_Holder` is a second piece of module-level mutable state, contrary to the impl 08 `ProcessState` row ("the only module-level mutable state of 08", D08-19), and it is not reset by `reset_process_state`. No test calls `gpu_state()` unpatched today, but once a consumer test does, its 5 s loaded-class/health caches (and its `LoopbackHttp`) leak across tests. The fix needs a `gpu_state` slot on `ProcessState` in `_state.py` (outside this card's Files) — controller decision / follow-up card; until then tests must keep patching `gpu._Holder.reader`.
2. herness/core/jobs/gpu.py:231,237 — after a failed swap `requested_class` stays = target. Because step 2 writes it for every swap (including arbiter-driven ones), the supervisor must not treat that value as an external request, or U08-70 step 1 retries the failing swap every free tick and bypasses `postpone_class`. Record for the supervisor card (supervisor algorithm step g, impl 08 ~line 1780: "set by someone other than this supervisor"); clearing it on failure here or there would be simplest.
3. herness/core/jobs/gpu.py:56-57 — `_gpu()` duplicates `gpu_services._gpu` and is never exercised unpatched (the only uncovered line). Low risk (identical expression).
4. herness/core/jobs/gpu.py:168-173 — the health poll checks the deadline before sleeping, so a start can exceed `start_timeout_s` by up to `cap_s` (10 s) plus one 5 s GET; the "Complexity and limits" bound is loose by that much. Clamp the sleep to the remaining time if exactness matters.
5. herness/core/jobs/gpu.py:355-367 — `WorkerGpuState.service_healthy` releases the lock during the GET, so concurrent callers can each issue a GET inside one 5 s window ("at most every 5 s" is best-effort under concurrency). Acceptable; note only.
6. herness/core/jobs/gpu.py:228 — `swap("none")` with `loaded == "none"` returns 0.0 without stopping stray services left after a failed stop/kill (step 1 as specified). The next swap to a real class stops them first, so single-class still holds; note only.

### Assessment
**Task quality:** Approved
**Reasoning:** All three units match the spec with well-justified deviations, TH08-01/TH08-09 hold, fail-closed paths are tested, and the single-class acceptance check is proven non-vacuous by mutation; the remaining items are Minor or belong to the controller/supervisor card.

# T08-18 report — GPU controller and state reader

Worktree: D:\herness\.claude\worktrees\agent-a6a5ad6121e37da59 (branch worktree-agent-a6a5ad6121e37da59, base 65040a8)
Commits: 0cbaea2 wip(T08-18): GPU controller, state reader and request_gpu_class with tests; fb20827 feat(jobs): T08-18 GPU controller and state reader (empty marker commit; all code is in 0cbaea2). Full `pytest -m unit` run: 6726 passed, 11 skipped, 1 xfailed. Note: the first final-commit attempt without PYTHONUTF8=1 failed the pytest-unit hook (the known Windows-locale impl 00 test); with PYTHONUTF8=1 hooks pass.

## Files
| File | Lines | Budget |
|------|-------|--------|
| herness/core/jobs/gpu.py (new) | 391 | 400 (ENG hard limit 400) |
| tests/unit/core/jobs/test_jobs_gpu.py (new) | 614 | — |
No other file touched (jobs/__init__.py, ports.py, gpu_services.py, gpu_lock.py, fake_gpu.py unchanged).

## Units
- U08-81 `GpuController(*, worker_id, runner, http)`: `loaded`, `class_since`, `swap` (steps 1-8), `service_start`, `service_stop`, `service_healthy`, `detect_loaded_class`, `restart_service`; internal `threading.Lock`; construction sets `process_state().kill_service_hook` (a closure calling `runner.kill`). Events gpu_swap / gpu_swap_failed / service_start / service_stop / service_restart (component `jobs`); histogram `herness_jobs_gpu_swap_seconds{from,to}`; counter `herness_jobs_gpu_swap_failed_total{to}`; `ops.health_reset(endpoints(target))` on success, `breaker(k).record_failure(ModelUnavailable)` on start/warm-up failure. All compose/VRAM/HTTP goes through gpu_services (ComposeRunner, LoopbackHttp, wait_vram_free); gpu.py builds no argv and parses no output. No GpuLock acquisition (ruling 2).
- U08-83 `WorkerGpuState` / `gpu_state()`: `loaded_class()` reads `list_workers()` at most every 5 s (clock.monotonic), alive worker with gpu_slot=1 else "none"; `service_healthy(name)` `LoopbackHttp.healthy(svc, timeout_s=2)` at most every 5 s per service; caches under a lock (the health GET runs outside it).
- U08-102 `request_gpu_class(cls)`: alive gpu_slot=1 worker → `set_requested_class` + INFO `jobs.gpu.class_requested` (worker_id, class) → "requested"; else "no_worker" with no write.

## Tests (tests/unit/core/jobs/test_jobs_gpu.py, 33 functions)
UT08-91 (6), UT08-92 (5), UT08-93 (3), UT08-94 (6 incl. parametrised), UT08-96 (3), UT08-108 (6), CV T08-18 (5: 5 s loaded_class cache, "none" without alive worker, per-service health cache with 2 s timeout, gpu_state process-wide, store error not swallowed).
Acceptance: every UT08-91..93/96 test that runs compose calls `fake_gpu.assert_single_class()` (fake_gpu's `running_classes` history, recorded after every compose command). UT08-94 seeds two running classes as its setup; the test asserts every history entry after the first stop has <= 1 class.
RED: `mv gpu.py away; pytest tests/unit/core/jobs/test_jobs_gpu.py` → `ImportError: cannot import name 'gpu' from 'herness.core.jobs'`.
GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/core/jobs -q -p no:logging --cov=herness.core.jobs.gpu --cov-branch` → 244 passed; gpu.py 99% (283 stmts, 1 miss; 56 branches, 2 partial).
Gates: ruff check/format clean, mypy (gpu.py + test) clean, lint-imports 13 kept 0 broken, check_module_size exit 0, check_type_ownership exit 0, C901/PLR0913 clean; commit hooks (incl. pytest-unit) passed.

## Deviations / decisions
1. U08-54 `worker_alive` does not exist in the tree (its card has not landed; it is also specified as `worker_alive() -> bool` over any row, not per row). gpu.py has a private `_alive(row, now, heartbeat_s)` with the same rule (status in starting/running/draining and heartbeat_at > now - 3 x R.jobs.heartbeat_s). When U08-54 lands, switch to a shared per-row helper.
2. Health poll: `retry_call` takes only a PolicyName, so `dataclasses.replace(GPU_HEALTH_POLICY, max_elapsed_s=...)` cannot be passed to it without importing the private `retry._run`. gpu.py polls itself under the replaced policy's attempts / base_s / cap_s / max_elapsed_s / timeout_s with `full_jitter_delay` and `process_state().sleep`/`rng`, raising `ModelUnavailable("unhealthy")`. Side effect: no `retry` events per poll (they would be noise during a 900 s start).
3. Swap step 3 stops every running configured service, the target's own included (spec: services not in svc_of(target)). Step 3 is only reached for a target that is not loaded-and-healthy; if a target service still ran (loaded but unhealthy), the step 4 VRAM check could never pass. UT08-91 re-entry test covers it.
4. Fail closed (ruling 5): any exception in steps 3-6 (not only 5-6) sets loaded/worker `none`, stops the target's services (errors logged with service + error_type only), records `gpu_swap_failed` with step `stop` | `vram` | `start` | `warmup`, bumps the failure counter and raises `ModelUnavailable` (foreign errors become `ModelUnavailable("gpu swap failed")`, no foreign text kept). Breaker failures only for `start`/`warmup` (spec step 8). `requested_class` is left as set in step 2 on failure (spec silent; supervisor card decides whether to clear it).
5. `restart_service` failure: the service is stopped, its breakers fail, a `service_restart` event with reason `failed` is still written (so a failing restart counts toward `restart_max_per_hour` and cannot loop), then `ModelUnavailable` is raised. Spec describes only the success path.
6. `service_start` failure records breaker failures for `endpoints(loaded class)` (for openjev: `decider:openjev`).
7. `gpu_state()`'s singleton lives in a module-level `_Holder` (ProcessState has no slot for it and `_state.py` is not this card's file) — a second piece of mutable module state beside ProcessState (ENG §2.3 / D08-19); it is not reset by `reset_process_state`. Tests construct `WorkerGpuState` directly or monkeypatch `_Holder.reader`.
8. Settings are read through a module-level `_gpu()` (get_config) that tests monkeypatch to fake_gpu's settings (fake_gpu patches only `gpu_services._gpu`).

## Concerns / spec notes
- Test IDs: UT08-84 (cited by U08-83) is the chat_policy row; gpu_state tests use `test_cv_t08_18_...` per ruling 3.
- Spec notes for 08 impl: U08-81 step 3 should read "every running service" (item 3); U08-81 restart failure path (item 5); U08-54 should offer a per-row predicate for U08-83/U08-102.
- detail `reason` for service_start/service_stop is "requested" and for service_restart "breaker_open" / "failed" (spec gives no values).

## Fix round 1 (verify M2)
- M2 fixed: every swap failure path (stop / vram / start / warmup) now writes `gpu_class_loaded='none'` and `requested_class=NULL` in the same `update_worker` call (`_swap_failed` → `_set_loaded("none", requested_class=None)`), so the arbiter does not retry a failing swap on every free tick. This supersedes deviation 4's "requested_class is left set".
- Tests: UT08-92 warm-up failure and the vram-failure test assert `requested_class is None`. tests/unit/core/jobs: 244 passed; ruff, mypy and check_module_size clean; gpu.py 391/400.
- Spec notes (impl 08 U08-81): step 3 should read "every running configured service (the target's own included)"; the step 4 and step 8 failure paths should also clear `requested_class` (worker `gpu_class_loaded='none'`, `requested_class=NULL`).

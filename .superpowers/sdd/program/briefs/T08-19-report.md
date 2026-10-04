# T08-19 Chat policy — build report

Worktree: D:\herness\.claude\worktrees\agent-ab0965800086f7879 (branch worktree-agent-ab0965800086f7879, base 8bb8194)
Checkpoint: 7d6dec3 wip(T08-19): chat policy module and unit tests. Final: 2451ea4 feat(jobs): T08-19 chat policy (adds the bench file; module and unit tests landed in 7d6dec3).

## Built
- herness/core/jobs/chat_policy.py — 152 lines (budget 170). `chat_policy` (U08-75), `chat_model_profile` (U08-76),
  `chat_next_live_at` (U08-77), `SWAP_ETA_S = 360`.
  - chat_policy: first local key of chain_for("chat","fast") (none -> ConfigError "chat chain has no local client");
    live when gpu_state().loaded_class()=="reasoning" and service_healthy("vllm-reasoning") and breaker("model:"+key)
    not open; else in_hours_unavailable in the `chat` window, off_hours otherwise; cloud -> small_model unless
    herness.core.egress.cloud_chat_allowed(cfg); small_model -> defer when breaker("model:"+first chat_off_hours key) open.
  - GPU state is only read (gpu_state() reader); no request_gpu_class / swap.
  - Caches: WorkerGpuState caches the worker row and health 5 s; CircuitBreaker.state() already caches its row 5 s
    (breaker._entry, BREAKER_CACHE_S), so no new cache was needed.
  - chat_next_live_at: alive worker with gpu_class_loaded "swapping" and requested_class "reasoning" -> now+360 s (UTC);
    else walk windows after the active one (window_at(w.end_at)) for <= 8 days, first with preload or classes containing
    reasoning -> start_at; else None.
- tests/unit/core/jobs/test_jobs_chat_policy.py — 428 lines, 16 tests (UT08-84 x6, UT08-85 x4, UT08-86 x5, ST08-07 x1).
  UT08-84 table: itertools.product over windows{chat,reviews} x loaded{reasoning,decider,swapping} x vLLM{t,f} x chat
  breaker x off-hours breaker x egress x profile{local,hybrid} x approval x (in_hours, off_hours) in {small_model,defer,cloud}^2
  = 6912 rows vs an independent oracle of the pseudocode; explicit asserts that cloud never appears with egress off,
  hybrid without approval, or local; every ChatMode occurs at least once.
- tests/bench/test_jobs_chat_policy_bench.py — 128 lines, BT08-09 [integration, slow]: real config, migrated ops store,
  real breakers, process-wide WorkerGpuState against fake_gpu stub health servers, 2 000 warm calls.

## BT08-09
- live path p95 = 0.0021 ms; fallback path (decider loaded, Tue 22:00 reviews window) p95 = 0.032 ms. Threshold 5 ms.

## Tests / gates
- RED: with the module absent -> ModuleNotFoundError: No module named 'herness.core.jobs.chat_policy' (collection error).
- GREEN: card tests 16 unit + 2 bench passed; tests/unit/core/jobs 320 passed; chat_policy.py coverage 100 % line, 100 % branch.
- ruff format / ruff check / mypy (253 files) / lint-imports (13 kept) / check_module_size rc 0 / check_type_ownership rc 0.
- Full suite not run (per dispatch).

## Deviations / spec notes
1. chat_model_profile("cloud") also returns None (WARNING `jobs.chat.cloud_not_allowed`) while cloud_chat_allowed(cfg) is
   false — defense in depth for TH08-07 / ST08-07 ("claude-opus never yielded for any mode"). Spec U08-76 only names
   `jobs.chat.no_cloud_client`; the new event name is an addition.
2. chat_policy: empty chain_for("chat_off_hours","fast") with mode small_model -> defer (fail closed; spec step 5 assumes a key).
3. Worker liveness in chat_next_live_at replicates the U08-54 rule (gpu._alive is private; U08-54 worker_alive does not exist)
   using the public constants gpu.ALIVE_HEARTBEATS / ALIVE_STATUSES; wall clock (clock.now()) not the `now` argument, as gpu.py does.
4. chat_model_profile("live") with no local key for that depth -> None (spec silent).
5. No edits to jobs/__init__.py or ports.py; chat_policy names are not yet in the lazy export map (U08-98).

## Concerns
- None blocking. Deviation 1 adds a log event name; reviewers may prefer to fold it into the spec U08-76 row.

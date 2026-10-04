# T08-19 Chat policy — review (verify agent)

Worktree agent-ab0965800086f7879, base 8bb8194, head 2451ea4 (7d6dec3 + 2451ea4). Files: herness/core/jobs/chat_policy.py (152 lines), tests/unit/core/jobs/test_jobs_chat_policy.py (428), tests/bench/test_jobs_chat_policy_bench.py (130). The worktree is clean after the probes.

### Spec Compliance
- ✅ U08-75 `chat_policy`: step 1: first local key, and ConfigError "chat chain has no local client" (chat_policy.py:68-72). Step 2: live requires loaded_class reasoning, health of vllm-reasoning and a breaker that is not open (:73-75). Step 3: window selection (:78-80). Step 4: cloud becomes small_model unless cloud_chat_allowed(cfg) (:81-82). Step 5: small_model becomes defer when the off-hours breaker is open (:83-86). Step 6: return (:87).
- ✅ U08-76 `chat_model_profile`: live → first local key; small_model → chat_off_hours[0]; cloud → first off-network key, or None with WARNING `jobs.chat.no_cloud_client`; defer → None (:99-113). There is one addition: an extra cloud_not_allowed guard (deviation 1, accepted below).
- ✅ U08-77 `chat_next_live_at`: returns now+360 s (UTC) for an alive worker that is swapping with requested_class reasoning (:144-145). Otherwise it walks the windows after the active one through window_at(end_at), up to 8 days, and takes the first window that preloads reasoning or allows it in classes. When there is none it returns None (:146-152).
- ✅ Fail-closed (focus 1): the module only returns `cloud` when the configured mode is `cloud` and cloud_chat_allowed(cfg) is true. Every other path ends in live, small_model or defer. An exception from window_at, gpu_state or the breaker propagates to the caller and never turns into cloud. Mutation probes:
  - Probe M1 removed the cloud_chat_allowed check from chat_policy. Result: UT08-84 full_truth_table, UT08-84 cloud_needs_every_gate and ST08-07 went red.
  - Probe M2 removed the guard in chat_model_profile. Result: UT08-85 cloud_not_allowed and ST08-07 went red.
  - Probe M3 removed both checks. Result: 4 tests went red.
  - Other probes, each with at least one red test: M4 dropped the vLLM health check; M5 dropped the off-hours breaker check; M6 set SWAP_ETA to 300; M7 dropped worker liveness; M8 swapped the in-hours and off-hours modes. All 8 probes were killed, and each was reverted; `git status` is clean.
- ✅ GPU class state is only read (focus 2): the module calls only gpu_state().loaded_class() / service_healthy() and require_jobs_backend().list_workers(). A grep for request_gpu, swap(, set_requested, upsert and write in chat_policy.py found nothing.
- ✅ UT08-84 (focus 5): itertools.product covers every axis of the row: window {chat, reviews}, loaded {reasoning, decider, swapping}, vLLM {t, f}, chat breaker, off-hours breaker, egress, profile {local, hybrid}, approval, and config modes {small_model, defer, cloud}^2 for (in_hours, off_hours), which gives 6 912 rows. The expected values come from an independent oracle. The real cloud_chat_allowed runs (it is not patched). There are explicit assertions that no cloud occurs with egress off, in hybrid without approval, or in local, and that every ChatMode is reached. Additional tests cover premium, enterprise and missing-purpose gates, the real window_at, the ConfigError path and the empty off-hours chain.
- ✅ UT08-85: mode x depth (fast/deep) values, the no_cloud_client warning, and the None paths.
- ✅ UT08-86: Tue 19:30 → Tue 21:00 UTC; Tue 03:00 → 06:00; swap → now + 360 s with tzinfo UTC; SWAP_ETA_S == 360. A stale or stopped worker, or a swap to another class, is ignored. When no window allows or preloads reasoning, the result is None.
- ✅ ST08-07 chat part (focus 4): profile local, chat chains start with claude-opus, both modes set to cloud, egress on or off, loaded class decider, swapping or none, windows chat and reviews. Every call returns `small_model`. The resolved client is always local-small-cpu, and claude-opus is never yielded by chat_model_profile for any mode or depth.
- ✅ BT08-09 (focus 3): the report records live p95 0.0021 ms and fallback p95 0.032 ms. I re-ran the bench and got live 0.0027 ms and fallback 0.0338 ms, against a threshold of 5 ms. The bench is sound:
  - It uses the real config, a migrated ops store, real SQLite breakers, and the process-wide WorkerGpuState against the fake_gpu stub health servers.
  - One warm call precedes each of 2 000 timed calls, so the 5 s caches are hot.
  - It asserts the mode before timing and computes p95 from the sorted list.
- ✅ Gates (focus 7):
  - Test IDs, naming and docstrings follow the conventions. pytestmark is unit for the unit file and [integration, slow] for the bench, per global-constraints (no `benchmark` marker exists in pyproject).
  - Module size is 152/170, and tools/check_module_size and check_type_ownership both return rc 0.
  - ruff format and ruff check are clean on the touched files. The project `mypy` is clean (253 files), and lint-imports reports 13 contracts kept.
  - chat_policy.py coverage is 100 % line and 100 % branch.
  - No edits to jobs/__init__.py or ports.py (diff stat has 3 files).
  - `pytest tests/unit/core/jobs -q -p no:logging` gives 320 passed, and the bench file gives 2 passed.
- ⚠️ Cannot verify from diff: the lazy export of the chat_policy names from `herness.core.jobs` (U08-98 export map) is not in this card; the report states it is left for later. The card's Files row lists only chat_policy.py, so this is not a defect of this card.

### Deviations (focus 6)
1. Extra `cloud_not_allowed` guard in chat_model_profile. **Accept.** It is defense in depth for TH08-07 and adds a second layer that ST08-07 exercises. It only removes an off-network key and never adds one. The new log event name should be folded into the spec U08-76 row (Minor, spec follow-up).
2. An empty chat_off_hours chain with mode small_model returns defer. **Accept.** It fails closed. Step 5 assumes a key exists; without one there is no model to answer, so defer is the only safe mode (UT08-84 empty_off_hours_chain_defers).
3. The liveness rule is copied (`_alive` replicates gpu._alive with the public ALIVE_HEARTBEATS / ALIVE_STATUSES constants) and uses the wall clock. **Accept with a Minor finding.** The copy duplicates logic in gpu.py, and the two rules can drift. The wall clock matches gpu.py's behaviour. A shared public `worker_alive` helper would be better once U08-54 exposes one.
4. live with no local client for the requested depth returns None. **Accept.** The spec is silent, and chat_policy already raises ConfigError for the fast depth. None makes the caller fall through, so the result is never an off-network key.

### Strengths
- The module is small and clear, and step numbers map directly onto the code. The only I/O on the hot path is cached reads.
- The oracle-based 6 912-row truth table uses the real R-38 gate. It catches every probed mutation of the policy.
- The fail-closed design is checked on two layers (policy and profile).

### Issues
#### Critical (Must Fix)
- None.
#### Important (Should Fix)
- None.
#### Minor (Nice to Have)
1. herness/core/jobs/chat_policy.py:116-119: `_alive` duplicates the gpu.py liveness rule, which is a drift risk. Expose a public `worker_alive` in gpu.py (U08-54) and reuse it when that is in scope.
2. herness/core/jobs/chat_policy.py:108: the new event `jobs.chat.cloud_not_allowed` is not in spec U08-76. Record it in the spec or the log-event catalogue.
3. tests/unit/core/jobs/test_jobs_chat_policy.py:159, 220 and 283, and tests/bench/test_jobs_chat_policy_bench.py:88: strict mypy on the test files reports 6 errors, including no-any-return, a `**dict[str, object]` kwargs mismatch, a `str` passed where ChatMode is expected, and the bench `Chains.config` returning a local `Client` instead of `ClientInfo`, so the bench fake does not structurally satisfy ChainRegistry. Tests are outside the project mypy scope (files = herness, tools), so this is cosmetic.
4. herness/core/jobs/chat_policy.py:48: `chains.config(key).off_network is off_network` uses an identity comparison on bool. It works for real bools but would silently skip a truthy non-bool. `bool(...) == off_network` is more robust.

### Assessment
**Task quality:** Approved
**Reasoning:** All three units match the spec, and the fail-closed cloud rule is proven by mutation probes that turn UT08-84, UT08-85 and ST08-07 red. GPU state is only read, and BT08-09 is far below the 5 ms threshold. The findings are Minor polish only.

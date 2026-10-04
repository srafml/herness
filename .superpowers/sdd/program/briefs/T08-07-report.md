# T08-07 Retry execution: build report

Worktree `D:\herness\.claude\worktrees\agent-a54ac4e57fe054746`, branch `worktree-agent-a54ac4e57fe054746`, base 221fd6e.
Checkpoints: 1ccd813 (retry.py, shim re-export, breaker), f191680 (tests, ST08-04/05, BT08-01, claim guard); final commit 0808cc5 `feat(resilience): T08-07 retry execution` (empty closing commit; the work is in the two checkpoints).

## Files and line counts

| File | Lines | Budget | Change |
|------|-------|--------|--------|
| herness/core/resilience/retry.py | 340 | 340 | new: retry_call, aretry_call, retrying, retry_page, call_with_timeout; private _run(p, fn, key, family, tracer, target), _invoke, _ainvoke, _Retry wiring, _StopAfterDelay |
| herness/core/resilience/breaker.py | 367 (was 377) | 380 | _call_with_timeout deleted; _run_probe calls retry.call_with_timeout (late importlib import: retry imports breaker); new public `CircuitBreaker.hot()` for the U08-29 async hot path |
| herness/store/ops/_shims.py | 12 (was 62) | 80 | interim retry_call and the `# T08-07` marker deleted; `retry_call` and `fault_point` are re-exports |
| herness/core/resilience/__init__.py | 104 (was 92) | 120 | five exports + TYPE_CHECKING block |
| herness/core/resilience/policies.py | 162 (was 149) | 220 | see D1, D2 |
| herness/core/resilience/_state.py | 139 (was 138) | 140 | field `policies_cfg` (D2) |
| herness/store/ops/resilience.py | 229 | 320 | probe-claim SQL stale-due guard (D4) |
| herness/store/ops/core.py | 280 | - | byte-identical (not touched) |
| docs/impl/02-data-model.impl.md | - | - | §2 `_shims.py` row: both names are re-exports; module kept because core never changes |
| docs/impl/08-resilience-and-jobs.impl.md | - | - | resilience area SQL text (line ~1870): stale-due guard mirrored (D4) |

## Shim replacements
1. `_shims.retry_call is herness.core.resilience.retry_call` (UT02-28 identity test). core.py still calls `_shims.retry_call("sqlite_write", attempt)`.
2. `breaker._call_with_timeout` deleted; `_run_probe` uses `retry.call_with_timeout(lambda: fn(name), PROBE_TIMEOUT_S)`; the UT08-104 helper test is re-pointed to the public `resilience.call_with_timeout`.

## Rulings applied
- The retry emission (`_Retry.emit`, the spec's `_emit_retry`) never breaks run_write: ops unbound -> row skipped, WARNING `resilience.call.retry_scheduled` (kind, target, detail) logged, metric counted; a thread-local re-entrancy guard skips the row of a retry raised while a retry event is being written.
- Emission runs in tenacity `before_sleep`, after the failed attempt returned (after run_write's rollback). Test `test_ut08_25_sqlite_write_retry_through_run_write`: a StoreBusy inside real run_write gives a retry row (policy sqlite_write) and the metric is flushed into metric_sample during the retry, with no nested-write ConfigError.
- UT08-25 tracer path goes through `_run(..., tracer=recording tracer, target="llm")`; ModelChain wiring is a carry-over.
- ST08-04: immediate re-raise, no sleep, retry_after == 86400 for `Retry-After: 86400` and for an HTTP date a year ahead (clamped by retry_after_max_s via classify/parse_retry_after). The requeue half is a carry-over to T08-12.

## Design notes
- Hot path: attempt 1 runs `_invoke` directly; only on failure is `tenacity.Retrying` / `AsyncRetrying` built, and attempt 1's exception is replayed as tenacity's attempt 1 (same attempt numbering, wait, stop and before_sleep).
- Elapsed accounting: `_StopAfterDelay(stop_base)` measures `clock.monotonic` from the start of attempt 1 (tenacity's own `time.monotonic` is not used); stop = `stop_after_attempt(attempts) | _StopAfterDelay(max_elapsed_s)`. Sleeps: `process_state().sleep` / `.asleep`; wait: `FullJitterRetryAfter(p, process_state().rng)`; `reraise=True`.
- Async: breaker guard / record_success run on the loop only while `breaker(key).hot()` (fresh cached closed row with 0 failures), else via `asyncio.to_thread`; failures and retry events always go through `to_thread`.
- `retry_call` / `aretry_call` take `name, fn` positional-only (`/`) so `**kw` can carry a `name` keyword for `fn`.

## Deviations / interpretations (need controller attention)
- D1 (run_write without a config): the real `retry_call` resolves `policy("sqlite_write")` through `get_config()`. The repo's default config cannot load until T04-08 (`metrics: []` fails validation), so every run_write in tests without an explicit config (including the ops_store fixture's migrate()) raised ConfigError, and each failed load costs about 50 ms. Fix in `policy()`: while no config is cached (`_config._Cache.config is None`, the same private access secrets.py already uses), `sqlite_write` returns `SQLITE_WRITE_DEFAULT` (design 08 §7: 6 attempts, base 0.2 s, cap 5 s, 30 s; the old shim values) without loading a config. Once a config is cached, the configured policy is used.
- D2 (policy() speed): `policy()` computed `config_hash(get_config())` on every call (about 2 ms: model_dump + hash), which alone breaks BT08-01 and adds 2 ms to every run_write. Added an identity fast path: the config is frozen, so `ProcessState.policies_cfg = (cfg, hash)` lets the same object skip re-hashing (UT08-06 "a new hash rebuilds" still holds: a changed policies_hash forces the slow path). policy() is now about 1 us.
- D3 (foreign exceptions from run_write callbacks): per U08-28 / U02-38 a non-HernessError raised inside `run_write(fn)` is now classified (FatalError "store call failed: <Type>: unclassified <Type>", original as `__cause__`) instead of passing through. Updated 8 store tests that raised RuntimeError/ValueError only to force a rollback: test_store_ops_core (UT02-27 x2), _memory (UT07-07), _metrics (UT08-110), _runs (UT06-22: `set_run_status`'s ValueError now arrives as FatalError), _shared (UT02-43, UT02-74, UT02-76). One run of `(unit or integration) and not slow` over everything outside tests/unit/store/ops and tests/unit/core/resilience (after the swap, before D4) found no other affected test: 4716 passed; the single failure IT00-02 passed on re-run (dirty-tree transient during the run).
- D4 (T08-06 bug found by ST08-05): the probe-claim SQL trusted the caller's `probe_due`; with 20 concurrent callers, callers holding a cached row from before a failed probe re-claimed the just re-opened row with the stale due (9 probe calls at t=300 s, trips=10). Added `AND COALESCE(opened_at, updated_at) < :probe_due` to the open branch (a row re-opened at or after the caller's due cannot be claimed with it); store unit test `test_st08_05_claim_with_a_due_from_a_stale_row_loses`; spec SQL text updated in impl 08.
- D5: new public method `CircuitBreaker.hot()` (not in U08-24's list) for the U08-29 rule "the cached hot path stays on the loop".
- D6: BT08-01 lives in tests/bench/test_resilience_bench.py with `[integration, slow]` (repo benchmark pattern; `benchmark` is not a registered marker and a timing assertion in the unit pre-commit hook would be flaky), not `unit + benchmark` as the §11 row says.
- D7: TDD order: retry.py was written before its tests (no RED capture for the new tests); the existing shim/store tests went RED against the new code first (17 failures, resolved as above).

## Tests (IDs -> functions)
- UT08-08 test_ut08_08_retry_after_honoured_within_cap
- UT08-09 test_ut08_09_retry_after_over_cap_raises_at_once
- UT08-20 test_ut08_20_retries_until_success_or_attempts
- UT08-21 test_ut08_21_stops_after_the_attempt_crossing_max_elapsed (FakeClock), test_ut08_21_elapsed_follows_the_herness_clock (only clock.monotonic patched)
- UT08-22 test_ut08_22_circuit_open_is_not_retried
- UT08-23 test_ut08_23_foreign_error_is_classified_and_retried, _base_exceptions_pass_through, _unknown_policy
- UT08-24 test_ut08_24_decorated_sync_and_async, _async_classifies_and_stops, _async_breaker_hot_path_and_writes
- UT08-25 test_ut08_25_retry_rows_metric_and_trace, _unbound_ops_skips_row_keeps_log_and_metric, _nested_retry_while_writing_an_event_skips_its_row, _sqlite_write_retry_through_run_write
- UT08-26 test_ut08_26_breaker_guard_and_records
- UT08-27 test_ut08_27_retry_page_uses_source_policy_and_breaker
- UT08-28 test_ut08_28_call_with_timeout, _hook_failure_is_logged_and_bad_timeout_rejected (all in tests/unit/core/resilience/test_resilience_retry.py)
- ST08-04 tests/security/test_st08_retry.py::test_st08_04_huge_retry_after_reraises_at_once[seconds|http_date] (unit)
- ST08-05 tests/integration/jobs/test_resilience_retry_storm.py::test_st08_05_retry_storm_is_bounded_by_the_breaker (integration; 20 threads, 5 s ticks over 600 fake seconds; 8 <= first-burst calls <= 28, exactly one probe call at 300 s, trips == 2) + store part tests/unit/store/ops/test_store_ops_resilience.py::test_st08_05_claim_with_a_due_from_a_stale_row_loses
- BT08-01 tests/bench/test_resilience_bench.py::test_bt08_01_retry_wrapper_overhead: measured mean overhead 2.53 us (< 50 us)
- UT02-28 rewritten (tests/unit/store/ops/test_store_ops_shims.py): identity checks for both shims; sqlite_write without a config (SQLITE_WRITE_DEFAULT, 4 calls / at most 6 attempts); non-retryable errors not retried
- UT08-104 call_with_timeout test re-pointed to the public function

## Gates
- `pytest tests/unit/core/resilience tests/unit/store/ops tests/security/test_st08_retry.py tests/security/test_st02_ops_core.py tests/integration/jobs tests/bench/test_resilience_bench.py`: 906 passed, 1 skipped (symlink privilege).
- Coverage (branch): retry.py 100 % (176 statements, 32 branches); breaker.py 100 %; _shims.py 100 %; store/ops/resilience.py 100 %; _state.py 100 %; policies.py 97 % (lines 115-116: pre-existing defensive branch).
- ruff format/check clean, mypy 0 errors, lint-imports 13 kept, check_module_size exit 0, check_type_ownership exit 0, `--require-test-ids` collection clean.

## Carry-overs
- ModelChain (later card) calls `_run(p, fn, "model:<key>", "model", tracer, target)` for the UT08-25 tracer path.
- T08-12 queue: ST08-04 requeue at `now + retry_after`.
- Controller: rule on D1-D4 (policy fallback without a config, policy() fast path, foreign-exception classification in run_write, probe-claim SQL guard and its spec text).

## Fix round 1 (review Approved; D1-D7 accepted)
- m1: `_Retry.emit` sets the thread-local `_emitting` guard over its whole body (metric + event), restoring the previous value in `finally`; a sqlite_write retry inside the metric flush that the retry counter triggers now logs and counts but writes no row and cannot start a second retried write chain. Regression test `test_ut08_25_retry_inside_a_triggered_metric_flush_writes_no_row` (RED on the old code: rows ['sqlite_write', 'tool_store']; GREEN now: ['tool_store']).
- m2: `_ainvoke` skips `asyncio.to_thread` for a failure when there is no breaker key (one merged except branch: HernessError kept, foreign exception classified). Test `test_ut08_24_async_failure_without_breaker_key_stays_on_the_loop` (RED on the old code: `_fail` hopped to a thread; GREEN: only the two retry-event hops).
- m3: impl 08 U08-24 method table gains `hot()`.
- m4: impl 02 T02-04 Files row: `_shims.py` no longer "(interim)"; matches the §2 row.
- retry.py 339/340 lines; coverage 100 % (177 statements, 36 branches). Tests: resilience, store/ops, ST08-04, ST02, integration/jobs: 907 passed, 1 skipped. ruff, mypy, lint-imports, module size, type ownership clean.
- Commit c9dccbc `fix(resilience): T08-07 review round 1` (.secrets.baseline: two docs/impl/08 line numbers shifted by the m3 line; no entries dropped).

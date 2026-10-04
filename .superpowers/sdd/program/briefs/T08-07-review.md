# T08-07 Retry execution: review

Reviewer: verify agent (read-only). Worktree `D:\herness\.claude\worktrees\agent-a54ac4e57fe054746`, base 221fd6e, head 0808cc5.

## Verdict: Approved (with Minor follow-ups)

No Critical or Important findings. The Minor items below can go into the fix loop or be carried; none blocks the merge.

## Spec conformance

| Unit | Result | Notes |
|------|--------|-------|
| U08-28 `retry_call` | ✅ | `_should_retry` matches step 3 (RetryableError, not CircuitOpen, RateLimited over the cap re-raises at once; `==` cap is retried, as "exceeds" says). `_invoke` matches step 4 (guard outside the try, so CircuitOpen from the guard is not recorded; HernessError recorded and re-raised; foreign exception classified with `family`, recorded, `raise mapped from exc`). Only `except Exception`, so KeyboardInterrupt/SystemExit/CancelledError pass through. Stop = `stop_after_attempt(attempts) | _StopAfterDelay(max_elapsed_s)` on `clock.monotonic` from the start of attempt 1; sleeps `process_state().sleep`; wait `FullJitterRetryAfter(p, process_state().rng)`; `reraise=True`. Emission: `record_event("retry", component="resilience", target=key or p.name, detail={7 §4.4 fields}, tracer)` plus `herness_resilience_retries_total{policy,error_class}`. |
| Fast path (attempt 1 outside tenacity) | ✅ | Semantically identical: `_replay` re-raises attempt 1's exception as tenacity's attempt 1, so `attempt_number`, `_should_retry`, stop, wait and `before_sleep` see the same sequence; stop is timed from attempt 1's start (`start` taken before `_invoke`). Mutation "start measured at tenacity construction" is caught by UT08-21. |
| U08-29 `aretry_call` | ✅ | `AsyncRetrying`, `process_state().asleep`, async `before_sleep` via `to_thread`; guard/record_success on the loop only while `breaker(key).hot()`, else `to_thread`; failure records and events always `to_thread`. |
| U08-30 `retrying` | ✅ | `iscoroutinefunction` dispatch, `functools.wraps` on both wrappers. |
| U08-31 `retry_page` | ✅ | `retry_call("source_http_page", fn, breaker_key=source)`. |
| U08-32 `call_with_timeout` | ✅ | Daemon thread `herness-timeout`, join, hook failure logged WARNING `resilience.timeout.hook_failed` (`error_type`) and not raised, `ModelUnavailable("call timed out after <t>s")`, exception re-raised unchanged; `not timeout_s > 0` → ConfigError (also rejects NaN). |
| Shim replacement 1 | ✅ | `_shims.retry_call is herness.core.resilience.retry_call` (checked in a fresh interpreter too); `git diff 221fd6e..HEAD -- herness/store/ops/core.py` is empty; UT02-28 shim tests rewritten; impl 02 §2 `_shims` row updated. run_write semantics: config-less → `SQLITE_WRITE_DEFAULT` (6 attempts, 0.2/5 s, 30 s); config loaded → configured policy (scratch probe: YAML `attempts: 3` → 3, reload to 4 → 4, `reset_config` → default again). StoreBusy retried (RetryableError), SchemaViolation/ConfigError (nested) not retried. |
| Shim replacement 2 | ✅ | `breaker._call_with_timeout` removed; `_run_probe` resolves `retry.call_with_timeout` late via `importlib`; `import herness.core.resilience.breaker` alone and `import herness.core.resilience.retry` alone both succeed in fresh interpreters; breaker tests green; UT08-104 re-pointed to the public function. |
| Ledger rulings | ✅ | Unbound ops → row skipped, WARNING `resilience.call.retry_scheduled` + metric kept (UT08-25 test). Re-entrancy guard (thread-local, set around `record_event`) with test. Emission in `before_sleep` after run_write's rollback; real run_write StoreBusy test flushes the metric with no nested-write ConfigError. |

## ⚠️ Items (no action required, recorded for the controller)

- ⚠️ D1: a process that never calls `get_config()` / `init_config()` runs every `run_write` with the design §7 defaults even when `config/resilience.yaml` sets other `sqlite_write` values; the configured values take over from the first cached config. Acceptable for pilot (values equal the shipped YAML), but the behaviour differs from the spec literal ("policy(name)" → `get_config()` loads the default config).
- ⚠️ D3 widens the error surface of `run_write`: any non-Herness exception from a callback (e.g. `runs.set_run_status` ValueError on empty `allowed_from`, `findings` ValueError on bad limits if ever called inside a callback, pydantic `ValidationError`) now arrives as `FatalError` with the original as `__cause__`. No spec (impl 02 U02-38 Errors lists only HernessErrors; impl 06/07/08 define no foreign-exception contract through run_write) and no production caller catches a foreign exception around `run_write`. Note: a foreign exception that `classify` maps to a retryable class (e.g. a `TimeoutError`/`OSError` raised inside a callback, which U02-38 forbids anyway) would now be retried by `sqlite_write`.
- ⚠️ `hot()` then `guard()` in `_breaker_io`: if the 5 s cache TTL lapses between the two calls, `allow()` does one `health_get` on the event loop. Tiny window, harmless.

## Findings

### Critical
None.

### Important
None.

### Minor

1. **Metric flush triggered from `emit` is outside the re-entrancy guard** — `herness/core/resilience/retry.py:122-133`. `record_counter` (line 122) runs before `_emitting.active` is set; when the flush interval has elapsed it calls `flush_metrics` → `insert_metric_samples` → `run_write` → `sqlite_write` retries, whose own `emit` then writes a `retry` row (and runs a further 30 s-budget `record_event` write) inside the outer `before_sleep`. Reproduced with a scratch backend whose `insert_metric_samples` hits one StoreBusy: rows `['sqlite_write', 'tool_store']` for one outer retry. Depth is bounded (the flush resets `last_flush`; the inner `record_event` is guarded), so this is not unbounded recursion, but it is the budget-multiplication path the ledger ruling wanted to exclude. Fix: set `_emitting.active = True` before `record_counter` and keep it for the whole emit body (check `active` first; when already active, log + count only), e.g. move the `record_counter` call inside the `try` after `_emitting.active = True` and do the unbound/active check before it; add a test with a backend whose `insert_metric_samples` retries.
2. **Needless thread hop on async failures without a breaker** — `retry.py:170,174`: `await asyncio.to_thread(_fail, key, err)` runs even when `key is None` (a no-op). Fix: `if key is not None: await asyncio.to_thread(_fail, key, err)`.
3. **D5 not reflected in the spec** — `herness/core/resilience/breaker.py:207` adds public `CircuitBreaker.hot()`, which is not in U08-24's method list. Fix: add `hot()` to the U08-24 row in `docs/impl/08-resilience-and-jobs.impl.md` (one line: "True while the cache holds a fresh closed row with 0 failures; the U08-29 on-loop hot path").
4. **Stale "(interim)" in impl 02 card** — `docs/impl/02-data-model.impl.md:3748`: T02-04 Files still says `herness/store/ops/_shims.py` (interim). Fix: drop "(interim)" to match the updated §2 row.
5. **D4 guard vs. a sub-microsecond cooldown** — `herness/store/ops/resilience.py:39-40`: `cooldown_s` is only `_PosFloat`; a value below 1 µs makes `timedelta(seconds=cooldown)` zero, so `probe_due == opened_at` and the new `COALESCE(opened_at, updated_at) < :probe_due` guard can never pass (breaker stranded open; `run_due_probes` claims with the same due). Not reachable with sane config. Fix: enforce `cooldown_s >= 1` (or `>= 0.001`) in `herness/core/resilience/settings.py:158` BreakerSettings, or document the floor.

## Deviations

- **D1 (sqlite_write default without cached config)** — Accept. Checked: no path where a loaded config is ignored (the check is `_Cache.config is None`, and every config load goes through `init_config`/`get_config`, which set the cache); the default is never cached into `policies_cache`, so no stale policy survives a reload or `reset_config` (scratch probe above). The private `_Cache` access mirrors `secrets.py:239`. See ⚠️ for the never-loaded case.
- **D2 (policies_cfg identity fast path)** — Accept. `HernessConfig` is `frozen=True`; the tuple holds a strong reference, so `id` reuse cannot fake a hit; `init_config` produces a new object → re-hash; `reset_process_state` builds a fresh `ProcessState` (`policies_cfg=None`); the hit also requires `seen[1] == policies_hash`. Two lock sections can race only into an extra re-hash, never a wrong policy. UT08-06 still covers "new hash rebuilds".
- **D3 (foreign exceptions in run_write callbacks → FatalError)** — Accept as spec-conformant: U02-38 step (2) is literally `retry_call("sqlite_write", attempt)` and U08-28 says foreign exceptions never escape; U02-38 Errors lists only HernessErrors. Searched `herness/` for `except ValueError/TypeError/RuntimeError/KeyError` around run_write callers and impl 02/06/07/08 for such contracts: none. The 8 updated tests only raised foreign exceptions to force a rollback. The broad non-slow suite (see Tests) found no other regression. Not a regression.
- **D4 (probe-claim stale-due guard)** — Accept. Correct: a failed probe re-opens with `opened_at = now ≥ claim time ≥ caller's due`, so a stale due always loses (equal timestamps under a fake clock lose too, `<` is strict); a fresh reader's due is `opened_at + cooldown > opened_at`. `opened_at NULL` in state open falls back to `updated_at`, the same base `probe_due()` uses in Python, so the fresh due is still strictly later. `run_due_probes` reads a fresh row before claiming. Only strand case: cooldown below 1 µs (Minor 5). Spec SQL text updated in impl 08.
- **D5 (`CircuitBreaker.hot()`)** — Accept; needed for the U08-29 "hot path on the loop" rule. Add it to U08-24's text (Minor 3).
- **D6 (BT08-01 as `[integration, slow]` in tests/bench)** — Accept: global-constraints.md line 8 prescribes `[integration, slow]` for benchmarks and `benchmark` is not a registered marker (`--strict-markers`).
- **D7 (tests written after code)** — Accept with a note: mutation probes compensate (all six killed, see below); no action.

## Tests

- Every card test ID has a function: UT08-08, UT08-09, UT08-20..UT08-28 (tests/unit/core/resilience/test_resilience_retry.py), ST08-04 (tests/security/test_st08_retry.py, unit), ST08-05 (tests/integration/jobs/test_resilience_retry_storm.py + store part in test_store_ops_resilience.py), BT08-01 (tests/bench/test_resilience_bench.py). Names carry the IDs, docstrings start with them, every file sets `pytestmark`.
- Requested run (`tests/unit/core/resilience tests/unit/store/ops tests/security/test_st08_retry.py tests/security/test_st02_ops_core.py tests/integration/jobs tests/bench/test_resilience_bench.py -p no:logging`): **906 passed, 1 skipped** (symlink privilege) in 71 s.
- Coverage of retry.py (branch): **100 %** (176 statements, 32 branches).
- ST08-05 plus its store test run 6 times in a row: 6/6 passed (0.8–3.4 s).
- Mutation probes (patched at pytest_configure from a scratch plugin; tracked files untouched), all killed:
  cap check removed (UT08-09), CircuitOpen check removed (UT08-22), re-entrancy guard disabled (UT08-25 nested), unbound-ops skip removed (UT08-25 unbound), stop timer started at tenacity construction instead of attempt 1 (UT08-21), `__cause__` dropped (UT08-23).
- Broad suite for D3 (`tests/unit tests/integration tests/security -m "not slow" -p no:logging`): 5615 passed, 5 failed. ST05-13(a) and IT00-01 are the known-red base failures in the ledger. ST03-02 (x2) and ST10-46 fail only in combined runs (log-capture pollution; both pass in isolation, with tests/security alone and with tests/unit/core + tests/unit/store); the same 3 fail identically on a `git archive 221fd6e` snapshot run with the same command → **pre-existing, not caused by T08-07**. ⚠️ Worth a ledger note: they are not in the known-red list yet.

## Static gates

ruff check: all passed. ruff format --check: 495 files formatted. mypy: no issues (202 files). lint-imports: 13 kept, 0 broken. check_module_size: exit 0 (retry.py 340/340). check_type_ownership: exit 0.

# T08-06 report: Circuit breakers and probes

Worktree: D:\herness\.claude\worktrees\agent-aec003253ba12f7d9 (branch worktree-agent-aec003253ba12f7d9, base a59bb45)

## What was built
- `herness/core/resilience/breaker.py` (U08-23..U08-27): pure `breaker_transition` / `probe_due` (design 08 §5.3 table);
  `CircuitBreaker` (5 s per-process cache `BREAKER_CACHE_S`, write-through `health_apply`, cross-process probe claim
  via `health_claim_probe` with `HALF_OPEN_STALE_S = 600` stale re-claim, hot path without I/O); `breaker(key)` registry
  (key regex, `ConfigError`, instance created under `ProcessState.lock`); `guard(key)` (`CircuitOpen` with
  `retry_at = probe_due` or `now + cooldown_s`); `register_probe` / `run_due_probes` (open rows, due check, off-network
  rule, claim, 30 s timeout, success/failure recording, substitute `ModelUnavailable("probe failed: <Type>")`).
  Events (`breaker_open|half_open|close`) and `herness_resilience_breaker_transitions_total{key,to_state}` are recorded
  only after `health_apply` / the claim returns, never inside the write and never under `ProcessState.lock`.
- Callable-module pattern for `breaker` (same as `classify`): `herness.core.resilience.breaker` is the module and
  `breaker(key)` works on it.
- Private `_call_with_timeout` (U08-32 algorithm, daemon thread `herness-timeout`) marked `# T08-07: replace with call_with_timeout` (ruling 1).
- `_state.py`: placeholder `type CircuitBreaker = Any` replaced by a TYPE_CHECKING import (140 -> 138 lines; `Any` import dropped).
- `__init__.py`: exports `CircuitBreaker`, `breaker`, `guard`, `register_probe`, `run_due_probes` (+ TYPE_CHECKING block) (80 -> 92 lines).

## Files and line counts
| File | Lines | Budget |
|------|-------|--------|
| herness/core/resilience/breaker.py (new) | 373 | 380 |
| herness/core/resilience/_state.py | 138 | 140 |
| herness/core/resilience/__init__.py | 92 | 120 |
| tests/unit/core/resilience/test_resilience_breaker.py (new) | 585 | - |
| tests/support/breaker_race.py (new, spawn targets) | 59 | - |
| tests/integration/jobs/test_resilience_breaker_processes.py (new) | 91 | - |

## Tests
- `pytest tests/unit/core/resilience tests/unit/store/ops tests/integration/jobs -q -p no:logging`: 874 passed, 1 skipped (pre-existing skip).
- New: UT08-12 (14 table rows + missing row), UT08-13 (trips 1..8 + defensive), UT08-14, UT08-15, UT08-16 (spy backend: 100 allow() within 5 s -> 1 health_get; transition written at once), UT08-17, UT08-18, UT08-19, UT08-104, IT08-01, BT08-05.
- IT08-01: two spawned processes, 100 open due breakers, barrier start; every race exactly one True (100 allow() per process). ~0.8 s.
- BT08-05: ~5.7 s wall per test run (spawn + 1 s + ~4 s visibility); marked `integration` only (not slow; < 30 s). Ran 4x stable.
- Coverage breaker.py 100 % line / 100 % branch (243 stmts, 64 branches); _state.py and __init__.py 100 %.
- Gates: ruff format/check clean, mypy (183 files) clean, lint-imports 13 kept / 0 broken, check_module_size exit 0, check_type_ownership exit 0.
- Per controller dispatch the full `(unit or integration) and not slow` suite was not run; the pre-commit `pytest-unit` hook ran on each commit and passed.

## Deviations / interpretations
1. U08-23 rule 7 says `open + force_open` is "unchanged except last_error (failure)"; the general sentence says failure and force_open both set `last_error`. Implemented the general sentence: `open + force_open` updates `last_error` (no state change, no event).
2. `half_open + success -> closed` also sets `opened_at = None`, mirroring the store's `health_reset` close. `force_open` and `half_open + failure` leave `failures` unchanged (spec lists only state/opened_at/trips).
3. Event detail `reason`: failure-driven open = error class name; force_open = `auth` for `AuthError` else the class name; half-open claim = `probe`; close = none. `probe_due` is included only when the row is open.
4. Metric label `key` is cut to 64 chars (label value regex of U08-19); valid breaker keys can be up to 75 chars (`monitoring:` + 64).
5. `record_success` hot path checks the 5 s-fresh row (`_row`), so it reads at most once per 5 s; with a fresh closed/0 row it does no I/O.
6. A cache entry whose read time is in the future (clock stepped back) is treated as stale. `probe_due` with trips 0 uses the first-trip cooldown; `opened_at` None falls back to `updated_at` (defensive; never produced by the state table).
7. `run_due_probes(now)` claims with the given `now`; `record_success/failure` stamp with `clock.now()` (same as the caller path).
8. Ruling 2 applied: `model:` probe skipped when the chain registry is unbound or `config(name)` raises; off-network skipped unless `cfg.security.egress.enabled`.
9. IT08-01 read as "100 races": 100 open keys, each process calls `allow()` once per key (100 calls per process), each race exactly one winner (stronger than one key x 100 calls, which it also implies).
10. BT08-05: B primes its cache, signals, polls `state()`; A opens 1 s later. When A opens in the same instant B refreshed, visibility is 5 s + one poll + one read (inherent to `BREAKER_CACHE_S = 5`); that worst-case phase failed 1 of 3 runs on Windows (15 ms sleep granularity), so the test opens at an arbitrary (1 s) phase and asserts <= 5 s.
11. Spawn targets live in `tests/support/breaker_race.py` (importable in the child; `tests/support/egress_harness.race_worker` precedent). `tests/integration/jobs/` has no `__init__`/conftest, like the other integration dirs.

## Carry-overs
- T08-07: replace `breaker._call_with_timeout` with `call_with_timeout` (U08-32).
- U08-98 `bind_core_backends` not built: tests bind `SqliteResilienceBackend` directly.

## Commits
- a141b2b wip(T08-06): breaker module, exports and unit tests
- 3f868a9 feat(resilience): circuit breakers and probes (T08-06) — all pre-commit hooks passed, including pytest-unit.

## Fix round 1 (review T08-06-review.md)
- I-1: `allow()` now takes the cached (row, read time). When the row is open, the probe looks due and the cache was read before `due`, it re-reads with `health_get`, recomputes via `_claimable`, and claims only if still due. The half-open event detail is built from the fresh/claimed row. `herness/store/ops/resilience.py` untouched. Regression `test_ut08_19_stale_cache_rechecks_probe_due` (two `CircuitBreaker` instances over one store: B caches trips-1 row 3 s before due, A claims + fails -> trips 2; B.allow() 1 s later is False; after 120 s B claims with trips 2 in the event).
- M-1: `test_ut08_15_last_error_redacted_before_cut` (test_redactor, email straddling char 500).
- M-2: `test_ut08_104_probe_due_boundary` (opened exactly 300 s ago is due, 299 s is not).
- M-3: `run_due_probes` skips a stored key failing the regex with DEBUG `resilience.probe.invalid_key` (no key text); `test_ut08_104_invalid_stored_key_is_skipped`.
- M-4: `_network_ok` logs DEBUG `resilience.probe.client_unknown` with `error_type` only; asserted in `test_ut08_104_model_probe_network_rules`.
- M-6: half-open event `reason = probe` asserted in `test_ut08_19_open_probe_claimed_once`.
- M-7: `_EXPORTS` sorted (order checked).
- M-5 parked per ruling.
- Mutation check: disabling the I-1 re-read and cutting before redaction each fail exactly the new test.
- To fit the budget: `_row` inlined into `_entry(now)[0]`, module/`breaker_transition`/`_network_ok` docstrings shortened. breaker.py 379 / 380.
- Results: card + package tests 878 passed, 1 skipped (pre-existing); breaker.py 100 % line / 100 % branch; ruff, mypy, lint-imports (13 kept), check_module_size, check_type_ownership all clean.

## Fix round 2 (re-review round 1)
- I-1a: `allow()` re-reads whenever the row is open, looks due and `read_at < now` (was `< due`), so a row cached after it fell due (via `state()`, `retry_at()` or `record_failure` while open) is no longer trusted. Regression `test_ut08_19_cache_read_after_due_is_rechecked[record_failure|state]`.
- I-1b: `run_due_probes` re-reads each listed row with `health_get` (only when a probe fn exists) right before computing `due`, skips it unless still open, and claims with the fresh row. Regression `test_ut08_104_rechecks_rows_probed_during_the_tick` (another breaker probes and fails `jira` during the `confluence` probe; the tick does not probe `jira`).
- Mutation check: reverting either change fails exactly the new tests.
- Optional mypy cleanup done: `mypy` on test_resilience_breaker.py and test_resilience_breaker_processes.py is clean (module imported as `import herness.core.resilience.breaker as bmod`; typed probe fn; `functools.partial` instead of a default-arg lambda).
- Budget: module docstring, `_to_open` and `_emit` tightened; breaker.py 377 / 380.
- Results: card + package tests 881 passed, 1 skipped (pre-existing); breaker.py 100 % line / 100 % branch; ruff, mypy, lint-imports (13 kept), check_module_size, check_type_ownership clean.
- Note (not changed): `run_due_probes` still claims with the tick's `now` argument; only the row is re-read.

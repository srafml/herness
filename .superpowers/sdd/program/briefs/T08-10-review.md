# Review: T08-10 Decider chain and loop-signal policy

Base 58c5367, card commit 68093c2. Scope: `herness/core/resilience/deciders.py`,
`herness/core/resilience/loop_policy.py`, `herness/core/resilience/__init__.py`,
`tests/unit/core/resilience/test_resilience_deciders.py`,
`tests/unit/core/resilience/test_resilience_loop_policy.py`.

## Spec compliance (per unit)

- U08-39 `DeciderChain` -- Spec ok. `order`/`gpu`/`resolve` params, default `resolve` via
  `herness.core.registry.get("decider", n)()`, per-name availability rules (`openjev`:
  `loaded_class()=="decider"` and `service_healthy("openjev")`; `jev`:
  `cfg.security.egress.enabled`; `llm`: `loaded_class()=="reasoning"` and
  `service_healthy("vllm-reasoning")`; `laya`/other: always), `decider_cloud` only for
  `jev`, `breaker_key="decider:"+name`, `fault_point("decider.batch", model=name)`, crash
  conversion to `ModelUnavailable("decider crash: <Type>")` (closes impl 03 T03-14 M5),
  `ModelUnavailable`/`CircuitOpen` fallback with WARNING `resilience.decider.fallback`
  (`from`, `reason`), other errors propagate, exhausted to `([], remaining)` -- all present
  and match `herness/core/resilience/deciders.py` line for line against the brief algorithm.
  Empty `order` raising `ConfigError` is an explicit, reasonable addition (mirrors
  `ModelChain`), not a deviation from any documented behavior.
- U08-40 `loop_signal_policy` -- Spec ok. Reads `state.loop_signals` (not `nudges`, per
  R-66/D08-04), compares to `R.loop.stop_on_signal_no`, on stop writes one `guard_stop`
  `resilience_event` row via `record_event` with `run_id`/`task_id` from the tracer and
  `detail={cause, step}`, increments `herness_resilience_guard_stops_total{cause}`, returns
  `"nudge"`/`"stop"` per spec. Matches `herness/core/resilience/loop_policy.py` verbatim.
- `herness/core/resilience/__init__.py` -- Spec ok. Only `DeciderChain` (submodule
  `deciders`) and `loop_signal_policy` (submodule `loop_policy`) added to `_EXPORTS` and the
  `TYPE_CHECKING` block, alphabetically placed, per the ruling. Nothing else touched.
- UT08-43 -- Spec ok, literal case present (`test_ut08_43_crash_retried_skip_then_decide`:
  laya RuntimeError -> `ModelUnavailable`, retried 3x under `decider_local`
  (`config/resilience.yaml` `decider_local.attempts: 3`, confirmed), openjev skipped, llm
  decides, `([outputs], [])`).
- UT08-44 -- Spec ok, literal case present (`test_ut08_44_all_entries_skipped_defers_items`:
  loaded `decider`, openjev unhealthy, order `[openjev, llm]` returns `([], items)`).
- UT08-45 -- Spec ok, literal case present, both signals (`loop_signals=1,nudges=3` gives
  nudge with no event; `loop_signals=2,nudges=0` gives stop with one `guard_stop` row plus
  trace event carrying the tracer run_id/task_id, cause, step).
- ST08-14 (unit part only, per ruling) -- Spec ok. Stub loop drives `loop_signal_policy`
  directly across repeats of the same `repeat` `LoopSignal`; nudges then stops with
  `guard_stop` row. HarnessHooks integration half correctly left as carry-over (documented
  in report and test docstring); not this card scope.

## Verification run

`PYTHONUTF8=1 uv run pytest tests/unit/core/resilience/test_resilience_deciders.py tests/unit/core/resilience/test_resilience_loop_policy.py -q -p no:logging`
result: 10 passed, matching the report.

Coverage (`--cov-branch`) on the two new modules: 100% line / 100% branch on both
`deciders.py` (65 stmts, 12 branches) and `loop_policy.py` (16 stmts, 2 branches) -- well
above the 90/85 bar.

`ruff check`, `ruff format --check`, `mypy` on `deciders.py`, `loop_policy.py`, `__init__.py`
and both new test files: all clean, 0 issues.

Module sizes: `deciders.py` 122/170, `loop_policy.py` 42/90, `__init__.py` 114/120 -- all
within the section 2 budgets.

`decider.batch` confirmed present in `NAMED_POINTS` (`herness/core/resilience/faults.py`);
`GpuStateReader.loaded_class()`/`service_healthy()` and `TracerLike.run_id`/`task_id`
signatures confirmed to match usage in both new modules.

## Test-quality: mutation probes (scratch copies, worktree untouched)

Two obvious mutants on named, spec-called-out behavior survive the full test suite
(confirmed by loading a mutated scratch copy of `deciders.py` into `sys.modules` via a
pytest plugin, PYTHONPATH-injected, before running the real, unmodified test file):

1. `openjev` service_healthy check can be silently dropped. No test ever drives `openjev`
   to true availability (`loaded_class()=="decider"` and `service_healthy("openjev")` both
   true, so the chain actually calls it). Every existing test hits `openjev` only through
   its two unavailable causes (wrong GPU class in UT08-43; right class but unhealthy in
   UT08-44). Mutating `_available` for `openjev` at `herness/core/resilience/deciders.py:88`
   to `return loaded == "decider"` (dropping the health check entirely) still passes all 7
   tests in `test_resilience_deciders.py` unchanged.
2. `decider_cloud` being reserved for `jev` only is unverified. Mutating
   `herness/core/resilience/deciders.py:106`
   (`policy_name: PolicyName = "decider_cloud" if name == "jev" else "decider_local"`) to
   always `"decider_local"` also passes all 7 tests unchanged -- no test distinguishes the
   two policies effect (both have `attempts: 3` in `config/resilience.yaml`, so retry counts
   look identical either way; nothing asserts which policy name was actually passed to
   `retry_call`).

Both are on requirements the brief calls out explicitly by name (per-decider availability
rule, "policy decider_cloud only for jev"), so the >=90/85 coverage numbers overstate how
well those two specific rules are protected against regression. The delivered code is
correct on inspection and matches the brief and `config/resilience.yaml` -- this is a test
gap, not an implementation bug.

## Issues

### Critical (Must Fix)
None.

### Important (Should Fix)
- `tests/unit/core/resilience/test_resilience_deciders.py` -- no test exercises the
  `openjev` truly-available branch (`loaded_class()=="decider"` and
  `service_healthy("openjev")` both true, entry actually called). Add a case (e.g.
  `fake_gpu_state.loaded="decider"`, `fake_gpu_state.healthy.add("openjev")`, a
  `StubDecider("openjev", outputs=[...])`) so a regression in that specific per-name rule is
  caught. Mutation-confirmed gap, see above.
- `tests/unit/core/resilience/test_resilience_deciders.py` -- no test asserts that `jev`
  specifically uses the `decider_cloud` policy (vs `decider_local` for everything else).
  Since both policies currently share `attempts: 3`, add an assertion that distinguishes
  them directly (e.g. monkeypatch/spy `retry_call` to capture the policy name argument for a
  `jev` entry, or assert on a config field that differs between the two policies such as
  `retry_after_cap_s`/`cap_s` via a `RateLimited` case). Mutation-confirmed gap, see above.

### Minor (Nice to Have)
- No test asserts the WARNING `resilience.decider.fallback` log `from`/`reason` field
  values (`herness/core/resilience/deciders.py:113`) -- e.g. that `reason` is specifically
  `"unavailable"` for `ModelUnavailable` vs `"circuit_open"` for `CircuitOpen`. The code is
  correct on inspection and the end-to-end fallback behavior is tested; only the exact log
  field values are unverified (a `caplog` assertion would close this).
- The exact text of the crash-conversion message (`"decider crash: <Type>"`,
  `herness/core/resilience/deciders.py:39`) is not asserted anywhere (only its type-crash to
  `ModelUnavailable` to retry/fallback behavior is proven, via
  `test_ut08_43_crash_retried_skip_then_decide`). Low value to add given the message is not
  otherwise observable through the public API.

## Assessment

Verdict: Needs fixes.

Reasoning: The implementation is spec compliant, matches the brief algorithm for both units
line for line, closes T03-14 M5 as required, keeps `__init__.py` to the ruled scope, and is
clean on ruff/mypy/module-size/coverage-percentage. However, two of the brief explicitly
named per-decider rules (`openjev` health-gated availability, and `decider_cloud` being
reserved for `jev`) have zero test coverage against regression -- confirmed by scratch-copy
mutation probes that survive the full suite untouched -- despite the report 100%/100%
coverage claim. Given this card is Size S and the fix is two small, additive test cases (no
production code change needed), fixing the test suite before sign-off is the right bar; the
shipped `deciders.py`/`loop_policy.py` code itself needs no changes.

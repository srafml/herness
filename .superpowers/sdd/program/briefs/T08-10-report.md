# Report for T08-10: Decider chain and loop-signal policy

Status: DONE

## Built

- `herness/core/resilience/deciders.py` (122 lines, budget 170): `DeciderChain` (U08-39).
  - `decide(items, questions)` walks `order`, using `_available()` for the per-name gate
    (`openjev`: `gpu.loaded_class() == "decider"` and `gpu.service_healthy("openjev")`;
    `jev`: `cfg.security.egress.enabled`; `llm`: `gpu.loaded_class() == "reasoning"` and
    `gpu.service_healthy("vllm-reasoning")`; `laya`/anything else: always).
  - Each available entry runs under `retry_call("decider_local" | "decider_cloud", _call_entry,
    ..., breaker_key="decider:" + name)`. `_call_entry` runs `fault_point("decider.batch",
    model=name)` then `resolve(name).decide(items, questions)`, converting any non-`HernessError`
    into `ModelUnavailable("decider crash: <Type>")` (impl 03 T03-14 parked finding M5 follow-up
    — covered by `test_ut08_43_crash_retried_skip_then_decide`, which asserts a raw `RuntimeError`
    from a decider surfaces as `ModelUnavailable` and is retried 3x under `decider_local`).
  - `ModelUnavailable`/`CircuitOpen` -> WARNING `resilience.decider.fallback` (`from`, `reason`)
    and the chain moves to the next name; any other error propagates; order exhausted ->
    `([], remaining)`.
  - Default `resolve` is `lambda n: herness.core.registry.get("decider", n)()`; constructor
    rejects an empty `order` with `ConfigError`.
- `herness/core/resilience/loop_policy.py` (42 lines, budget 90): `loop_signal_policy(state,
  signal, *, tracer=None)`. `state.loop_signals < R.loop.stop_on_signal_no` -> `"nudge"`;
  else writes one `guard_stop` `resilience_event` row (`record_event`, `cause`/`step` detail,
  tracer's `run_id`/`task_id`), increments `herness_resilience_guard_stops_total{cause}` and
  returns `"stop"`.
- `herness/core/resilience/__init__.py`: added exactly `DeciderChain` (submodule `deciders`)
  and `loop_signal_policy` (submodule `loop_policy`) to `_EXPORTS` and the `TYPE_CHECKING`
  block, alphabetically placed; file is 114/120 lines (was 110/120; ruling 1 kept it ≤ 116).
  Nothing else in the file touched.

## Tests

- `tests/unit/core/resilience/test_resilience_deciders.py` (200 lines): UT08-43
  (`test_ut08_43_crash_retried_skip_then_decide`), UT08-44
  (`test_ut08_44_all_entries_skipped_defers_items`), plus five `test_ut08_43_*` coverage-only
  cases (empty order rejected, `order`/`gpu` properties and the default registry `resolve`,
  `jev` gated only by egress, a non-fallback `HernessError` propagating unchanged, and
  `CircuitOpen` never retried before the chain moves on) needed to reach the ≥ 90%/85%
  branch bar — all still exercise U08-39 behavior, no new spec IDs invented.
- `tests/unit/core/resilience/test_resilience_loop_policy.py` (105 lines): UT08-45 (two
  functions: first signal nudges with no event, second signal stops with the `guard_stop`
  row, trace event and metric) and ST08-14's unit part
  (`test_st08_14_stub_loop_stops_on_second_signal`): a small loop that re-raises the same
  `repeat` `LoopSignal` each iteration, driving `loop_signal_policy` directly; nudges on the
  first call and stops on the second, asserting the `guard_stop` row (tracer's `run_id`/
  `task_id`) and trace event.
- `pytest tests/unit/core/resilience -q -p no:logging`: 461 passed, 1 skipped (the skip is
  the pre-existing `test_ut08_47_symlink_refused0` symlink-privilege skip on Windows,
  unrelated to this card).
- Coverage of both new modules: 100% line, 100% branch (`--cov-branch`).
- `ruff check` / `ruff format --check` / `mypy` clean on both new modules, `__init__.py` and
  both new test files (mypy `files` in `pyproject.toml` covers `herness`, not `tests`, so
  mypy was not run against the test files, matching project convention).
- `lint-imports`: 13 kept, 0 broken (no new import-linter violation; `deciders.py` importing
  `herness.core.registry` stays inside `herness.core`, so it does not trip the
  "`herness.core` must not import `herness.store` or `herness.harness`" contract).
- `python -m tools.check_module_size`: no output (no size violations); `deciders.py` 122/170,
  `loop_policy.py` 42/90, `__init__.py` 114/120.

## Deviations

- None from the brief's algorithms or the sub-controller rulings. The only addition beyond
  the literal U08-39/U08-40 text is the empty-`order` `ConfigError` guard in `DeciderChain.__init__`
  (mirrors `ModelChain`'s existing validation style) and the five coverage-only test cases
  listed above, added solely to clear the ≥ 90% line / 85% branch bar on `deciders.py` (a
  crash-conversion, a fallback and a skip-everything path alone left `_default_resolve`, the
  empty-order guard, the `order`/`gpu` properties, the `jev` branch and the
  `CircuitOpen`-not-retried branch uncovered).

## Carry-over

- ST08-14's spec 05 `HarnessHooks` integration half (a real loop driving `loop_signal_policy`
  through the harness) is out of scope for this card per sub-controller ruling 3 and is left
  for whichever card wires spec 05's loop into `loop_signal_policy`. Only the unit part (a
  stub loop) was built here.

## Process notes

- `pytest-unit` pre-commit hook fails only on the known-red `test_st10_25_repository_passes`
  (pre-existing on base, unrelated to this card: `openai_compat.py`'s `httpx.AsyncClient`
  construction). All commits below used `SKIP=pytest-unit git commit …`; the targeted test
  run (`pytest tests/unit/core/resilience -q -p no:logging`) passed in full beforehand.
- The implementation and tests were first committed as two `wip(T08-10)` checkpoints, then
  squashed with `git reset --soft` (own worktree branch, nothing pushed or shared with
  anyone) into the single required final commit below.

## Commits

- `68093c2` — `feat(resilience): add decider chain and loop-signal policy (T08-10)` (HEAD)

## Reply contract

- Status: DONE
- Commit: `68093c2` — `feat(resilience): add decider chain and loop-signal policy (T08-10)`
- Tests: `pytest tests/unit/core/resilience -q -p no:logging` -> 461 passed, 1 skipped
  (pre-existing Windows symlink-privilege skip, unrelated); 100% line/branch coverage on
  both new modules.
- Concerns: none. `pytest-unit` pre-commit hook was skipped (`SKIP=pytest-unit`) because it
  fails only on the known-red `test_st10_25_repository_passes` (pre-existing on base,
  `openai_compat.py` `httpx.AsyncClient`), unrelated to this card; the scoped resilience
  suite was run directly and passed in full first.

## Fix round 1 (review response)

Status: DONE

Commit: `6b7b0d8` — `test(resilience): T08-10 review round 1 - pin decider availability and policy`
(test-only; `herness/core/resilience/deciders.py` and `loop_policy.py` unchanged, matching
the review's "code needs no changes" verdict).

Addressed in `tests/unit/core/resilience/test_resilience_deciders.py`:

- **I1** (mutation-confirmed gap: `openjev`'s `service_healthy` check could be dropped
  without failing any test): added `test_ut08_43_openjev_decides_when_healthy_and_loaded`
  (loaded `"decider"` + healthy -> openjev is actually called and decides) and
  `test_ut08_43_openjev_skipped_when_unhealthy_though_loaded` (loaded `"decider"`, unhealthy,
  but a *registered* openjev decider -> asserts `calls == 0`, so the mutant now fails).
  Added the same pair's negative half for `llm`/`vllm-reasoning`
  (`test_ut08_43_llm_skipped_when_unhealthy_though_loaded`) per the review's "same idea"
  note (llm's positive path was already pinned by UT08-43's main case).
- **I2** (mutation-confirmed gap: hardcoding `"decider_local"` for every name, including
  `jev`, previously still passed): added `test_ut08_43_policy_name_by_decider`, which spies
  on `retry_call`'s policy-name argument and asserts `laya` -> `"decider_local"`,
  `jev` -> `"decider_cloud"`.
- **m1**: added `test_ut08_43_fallback_log_fields`, asserting the WARNING
  `resilience.decider.fallback` log carries `from="laya"` and `reason="unavailable"` via
  `structlog.testing.capture_logs()`.
- **m2**: added `test_ut08_43_crash_message_exact_text`, calling `deciders_mod._call_entry`
  directly and asserting `str(exc) == "decider crash: RuntimeError"`.

Verification: before committing, both mutants named in the review
(`herness/core/resilience/deciders.py:88` dropping the `openjev` health check, and `:106`
hardcoding `"decider_local"`) were reproduced as temporary edits in the worktree, confirmed
to fail the new tests, then reverted with `git checkout --` (file diff confirmed clean
against HEAD afterwards) before the real commit.

- `pytest tests/unit/core/resilience/test_resilience_deciders.py
  tests/unit/core/resilience/test_resilience_loop_policy.py -q -p no:logging`: 16 passed
  (was 10).
- `pytest tests/unit/core/resilience -q -p no:logging`: 471 passed, 1 skipped (same
  pre-existing Windows symlink skip).
- Coverage of `deciders.py`/`loop_policy.py`: still 100% line / 100% branch.
- `ruff check` / `ruff format --check` / `mypy` on the touched test file: clean.
- `pytest-unit` pre-commit hook again failed only on the known-red
  `test_st10_25_repository_passes` (pre-existing on base, unrelated); committed with
  `SKIP=pytest-unit` after the scoped suite passed directly.
- Per the reviewing agent's instruction, no `git reset --soft` this round — the round-1 fix
  is its own separate commit on top of the existing `feat` commit, checkpoint history left
  intact.

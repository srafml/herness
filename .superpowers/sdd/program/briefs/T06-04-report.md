# T06-04 report — RunBudget and gates

Status: DONE (final SHA: see bottom)
Worktree: D:\herness\.claude\worktrees\agent-ad5ff153e1e6a70de, branch worktree-agent-ad5ff153e1e6a70de, base ae18ed5.

## Implemented
- herness/harness/budget.py (170/170 lines): RunBudget (U06-25..U06-27; implements core.types BudgetLedger:
  charge, snapshot -> dict[str, JsonValue]; restore), new_phase_budgets (U06-28).
- herness/harness/gates.py (140/170 lines): CallGate (U06-29), build_gates (U06-30), TaskSlots (U06-31),
  private _LoopSemaphore (lazy asyncio.Semaphore per running loop).
- tests/unit/harness/test_budget.py: UT06-14..UT06-17, PT06-05 (hypothesis max_examples=200, 8 threads + barrier).
- tests/unit/harness/test_gates.py: UT06-18..UT06-20.
- No pyproject change: new modules fall under the existing `herness layers` harness layer (lint-imports 13 kept, 0 broken).
  gates imports ClientConfig only under TYPE_CHECKING (spec §2: gates imports nothing from herness.harness at runtime).

## Evidence
- RED: `uv run pytest tests/unit/harness/test_budget.py tests/unit/harness/test_gates.py` -> ModuleNotFoundError herness.harness.gates (2 collection errors).
- GREEN: same command -> 50 passed.
- Coverage (pytest --cov --cov-branch): budget.py 100% (100 stmts, 24 branches); gates.py 99% (86 stmts, 16 branches, 1 partial:
  _LoopSemaphore.release when no semaphore was ever built — defensive, unreachable through the public API).
- `PYTHONUTF8=1 uv run pytest tests/unit/harness -q -p no:logging` -> 606 passed, 1 skipped (symlink privilege, pre-existing).
- ruff format/check clean, mypy clean on touched files, lint-imports 13 kept, check_module_size exit 0, check_type_ownership clean;
  pre-commit hooks (incl. pytest-unit) passed on commit.

## Spec readings / concerns
1. restore (U06-27): "exhausted is recomputed against the current cap". I also recompute cost_cap_reached against the current
   cost cap (instead of copying the snapshot flag), so a resume with a changed hybrid cost cap behaves consistently; both
   snapshot flags must still be present and bool (else SchemaViolation). Malformed counters (non-int, bool, negative,
   non-decimal cost string) also raise SchemaViolation("budget snapshot invalid: run_id=<id>").
2. charge: exhausted is captured inside the lock and the raise happens after release on that captured value, so exactly the
   charges that leave the ledger exhausted raise (PT06-05: a charge passes iff the running total after it is below the cap).
3. new_phase_budgets: arithmetic done in Decimal (Decimal(str(writer_reserve))) to avoid float floor errors (6e6*0.85);
   writer_reserve outside (0,1) -> ConfigError. Caps < 1 after the split surface as the RunBudget ConfigError.
4. build_gates "missing max_concurrency counts as 1 / missing chat_reserved_slots as 0": cannot occur with typed ClientConfig
   (max_concurrency required, chat_reserved_slots default 0; ClientConfig also rejects reserve >= max_concurrency). Handled
   cheaply with getattr defaults and tested with SimpleNamespace stand-ins. ClientConfig unchanged.
5. CallGate/TaskSlots: semaphore built lazily and rebuilt if used from a different running loop (spec: gates never shared across
   loops; each asyncio.run builds its own). Rebinding while slots are held is not supported (not a spec case).
6. Constructor ConfigError message for bad caps is "budget caps invalid: tokens_cap must be >= 1 and cost_cap >= 0" (spec gives
   no exact text); TaskSlots(size<1) -> ConfigError("task slots must be >= 1") (spec gives no precondition; added).
7. budget.py is exactly at its 170-line budget.

## Final
Status: DONE_WITH_CONCERNS (spec readings 1, 4, 6 above; no budget overrun).
Commit: 04b9b5d feat(harness): RunBudget ledger, call gates and task slots (T06-04) (single card commit; the wip checkpoint
a135b85 was folded in with a soft reset before the final commit, nothing pushed). Hooks passed incl. pytest-unit.
Lines: budget.py 170/170, gates.py 140/170. Tests: card 50 passed; tests/unit/harness 606 passed, 1 skipped.

## Fix round 1 (review findings, controller ruling)

Scope: herness/harness/gates.py, herness/harness/budget.py, tests/unit/harness/test_gates.py,
tests/unit/harness/test_budget.py only. Nothing else touched (pipelines/* left alone for the concurrent reviewer).

- Minor 1 (gates.py `TaskSlots.for_run`): computed the product in `Decimal` (`Decimal(analyst_max_concurrency) *
  Decimal(str(oversubscribe))`, same pattern as `new_phase_budgets`) before `math.ceil`, so `100 * 1.1` no longer
  rounds up through float error (`100.00000000000001` -> ceil 111); it is now exactly `110`. Regression test added:
  `test_ut06_20_for_run_exact_product_does_not_round_up` (UT06-20).
- Minor 2 (budget.py `snapshot`): replaced `str(Decimal)` with `format(d, "f")` for `cost_cap`, `cost_used` and
  `cost_remaining`, so values that `Decimal.__str__` would render in exponent form (e.g. `Decimal("1E-7")` ->
  `"1E-7"`, `Decimal("1E+1")` -> `"1E+1"`) are always plain decimal strings (`"0.0000001"`, `"10"`). Regression test
  added: `test_ut06_16_snapshot_decimal_strings_are_plain` (UT06-16).
- Minor 3 (budget.py `restore`): done, within the 170-line hard cap. Added `_EXTRA_KEYS` (the 5 snapshot keys
  `restore` never read: `tokens_cap`, `tokens_used`, `tokens_remaining`, `cost_cap`, `cost_remaining`) and an
  `absent = any(key not in snap for key in _EXTRA_KEYS)` check folded into the existing name/counts/cost condition,
  so a snapshot missing any of the full 12 keys now raises `SchemaViolation("budget snapshot invalid: run_id=<id>")`
  (previously only 7 of 12 were checked, the other 5 being unused by `restore` and so silently accepted even when
  absent). The `SchemaViolation` is now only constructed at the two actual raise sites (removed the unconditional
  `bad = SchemaViolation(...)` built on every call, valid or not); each raise site carries `# noqa: EM102` (same
  established pattern as `herness/core/redact.py`), since assigning the message to a variable first, at both sites,
  did not fit the 170-line budget without a bigger, less readable rewrite. Test list extended:
  `test_ut06_16_missing_key_is_schema_violation` now parametrizes over all 12 snapshot keys (was 5), including the 5
  newly-enforced ones (UT06-16).

### Evidence
- `PYTHONUTF8=1 uv run pytest tests/unit/harness/test_budget.py tests/unit/harness/test_gates.py -q -p no:logging
  --cov=herness.harness.budget --cov=herness.harness.gates --cov-branch --cov-report=term-missing` -> 59 passed;
  coverage budget.py 100% (100 stmts/24 branches), gates.py 99% (88 stmts/16 branches, 1 pre-existing partial branch
  in `_LoopSemaphore.release`, same as builder report).
- `PYTHONUTF8=1 uv run pytest tests/unit -q -p no:logging` (full unit suite) -> 4071 passed, 4 skipped, 1 xfailed.
- `ruff format` (no diff), `ruff check --fix` (all checks passed, no unused-noqa), `mypy` on the 4 touched files
  (success, no issues).
- `uv run python -m tools.check_module_size` -> exit 0.
- Lines: `herness/harness/budget.py` 170/170 (unchanged, at the hard cap), `herness/harness/gates.py` 142/170
  (+2 for the `Decimal` import and the two-line `for_run` body).

### Final
Status: DONE. Commit: see SHA appended below.

Fix round 1 commit: a6f5b58387f9c7fc0bc12830815292c1ee07b2e4
fix(harness): exact slot ceil, plain decimal snapshots, full snapshot key check (T06-04)
(worktree-agent-ad5ff153e1e6a70de). Pre-commit hooks passed incl. pytest-unit, mypy, import-linter,
module-size, type-ownership, detect-secrets.

## Fix round 2

Fixed N1 (non-blocking style Minor from review r1): `herness/harness/budget.py` `restore()` carried
`# noqa: EM102` on both `raise SchemaViolation(f"...")` call sites (lines ~127, ~129), which does not meet the
global-constraints.md:10 bar for a suppression (no reason given, and the finding was foreseen, not novel), and a
compliant line-neutral form existed per the review's recommended rewrite.

Applied the review's exact 5-line rewrite: merged the boolean-flag validity check into the single `absent`
condition (`flags = (snap.get("exhausted"), snap.get("cost_cap_reached")); absent = any(k not in snap for k in
_EXTRA_KEYS) or any(type(f) is not bool for f in flags)`), collapsing the two separate
`if ...: raise SchemaViolation(f"...")  # noqa: EM102` sites into one `if ...: msg = f"..."; raise
SchemaViolation(msg)`. No `noqa` remains. No behaviour change: both paths still raise `SchemaViolation("budget
snapshot invalid: run_id=<id>")` before any mutation, on exactly the same set of invalid snapshots.

### Evidence
- `PYTHONUTF8=1 uv run pytest tests/unit/harness/test_budget.py tests/unit/harness/test_gates.py -q -p no:logging`
  -> 59 passed.
- `ruff format` (1 file left unchanged), `ruff check --fix` (all checks passed), `mypy herness/harness/budget.py`
  (success, no issues).
- `uv run python -m tools.check_module_size` -> exit 0.
- `herness/harness/budget.py`: 170/170 lines (unchanged, still at the hard cap).
- Diff touches only `herness/harness/budget.py` (the `restore` method), nothing else.

### Final
Status: DONE. Commit SHA: fba8adb73d668e3c6bcad1843e4dde50f5600f15
refactor(harness): assign snapshot error message before raise (T06-04)

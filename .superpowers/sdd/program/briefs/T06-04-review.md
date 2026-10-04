# T06-04 review — RunBudget and gates (commit 04b9b5d, base ae18ed5)

### Spec Compliance
- ✅ Spec compliant (no Missing / Extra / Misunderstood items that block the card).

| Item | Status | Notes |
|------|--------|-------|
| U06-25 RunBudget | ✅ | Signature matches (name Literal, tokens_cap, cost_cap=None, kw-only cost_cap_raises, run_id). Preconditions -> ConfigError (budget.py:39-41). One threading.Lock, no await inside. `exhausted`/`cost_cap_reached` properties, sticky via `_update_flags` (only set True); counters only increase except in `restore`. Satisfies core `BudgetLedger` protocol (mypy-checked in test). |
| U06-26 charge | ✅ | Negative amount -> ConfigError("negative charge") before any mutation (budget.py:69-71). Add + calls+=1 under lock; flags per steps 3-4; `exhausted` captured under lock, raise after release with exact message "run budget exhausted: run_id=<id> phase=<name>" (budget.py:72-81). Counters include the raising call. |
| U06-27 snapshot/restore | ✅ | All 12 keys; tokens_remaining = max(0, cap-used); cost_cap/cost_remaining decimal string or None; cost_used decimal string (budget.py:92-111). restore: wrong name / missing name, tokens_in, tokens_out, calls, cost_used, flags -> SchemaViolation("budget snapshot invalid: run_id=<id>"); caps stay current; exhausted recomputed against current caps (budget.py:113-134). See Minor 2, 3. |
| U06-28 new_phase_budgets | ✅ | All kw-only; floor((1-r)*T) in Decimal; writer = remainder; cost analysis ROUND_DOWN to 0.01, writer = remainder (budget.py:146-170). Verified 6e6/0.15/15 -> 5,100,000/900,000, 12.75/2.25. |
| U06-29 CallGate | ✅ | Signature, properties size/in_flight/max_in_flight; ConfigError("gate size must be >= 1: client=<name>"); t0 before acquire, on_wait(client, elapsed) after acquire; lazy per-loop semaphore (gates.py:26-86). |
| U06-30 build_gates | ✅ | Sorted keys; review max(1, max_conc - reserved); chat reserved if >=1 else max_conc; missing -> 1 / 0 via getattr (gates.py:89-107). |
| U06-31 TaskSlots | ✅ | Signature, for_run = max(1, ceil(n*o)), free = size - held, ConfigError("slot released twice") (gates.py:110-140). See Minor 1 (float ceil). |
| UT06-14 | ✅ | 30+30 then 25+25 (=60 then 50): raises, totals 110, sticky; plus precondition and negative-charge cases. |
| UT06-15 | ✅ | cost cap 1, raises=False, cost 2 -> no raise, cost_cap_reached; raising variant too. |
| UT06-16 | ✅ | Round trip equality, wrong name, missing keys, malformed values, recompute vs current caps. |
| UT06-17 | ✅ | Exact spec numbers plus rounding case and reserve range. |
| UT06-18 | ✅ | gate 2, 10 coroutines, max_in_flight == 2, on_wait 10 times; release on exception; per-loop reuse. |
| UT06-19 | ✅ | 6/2 -> review 4, chat 2; reserve 0 chat -> 6; floor 1; defaults. |
| UT06-20 | ✅ | for_run(6, 1.5) -> 9 (via free()); double release -> ConfigError. |
| PT06-05 | ✅ | Hypothesis max_examples=200, 8 threads behind a Barrier; asserts totals, calls, cost, exhausted iff total >= cap, raised iff total >= cap, sum(passed) < cap. |
| Test naming / docstrings / pytestmark | ✅ | Every function `test_ut06_NN_` / `test_pt06_05_`, docstring first line starts with ID, module `pytestmark = pytest.mark.unit`. |
| Coverage | ✅ | Re-run: budget.py 100% (24 branches), gates.py 99% (1 partial 42->exit, defensive). 50 passed in 1.97 s. |
| Budgets | ✅ | budget.py 170/170 (at limit), gates.py 140/170; check_module_size exit 0. mypy (4 files) and ruff check/format clean. |

- ⚠️ Cannot verify from diff / by rule: lint-imports (13 kept) and check_type_ownership claims taken from report (not re-run; full suite not run by instruction). gates.py imports `ClientConfig` only under TYPE_CHECKING, so the runtime "imports nothing from herness.harness" rule holds.
- ⚠️ Builder spec readings, judged:
  1. restore recomputes `cost_cap_reached` against the current cost cap instead of copying the snapshot flag — accepted. It is consistent with "caps stay those of the current knobs"; the spec wording "sets counters and flags from a snapshot" is looser, but recompute gives the same result when the caps are unchanged and the right one when a hybrid cost cap changes. Snapshot flags are still required and must be bool.
  2. Raise decided on the flag captured under the lock — correct; it makes PT06-05 "raise iff cap reached" exact.
  3. Decimal arithmetic for the split — correct; avoids float floor errors.
  4. getattr defaults for fields `ClientConfig` always has — acceptable; tested with SimpleNamespace stand-ins.
  5. Per-loop semaphore rebuild — accepted; rebinding while slots are held is outside the spec (Minor 4).
  6. Unspecified constructor messages ("budget caps invalid: ...", "task slots must be >= 1") — acceptable; no secrets or personal data.

### Strengths
- The lock scope and raise semantics are exactly as specified, and PT06-05 really exercises contention (Barrier plus 8 threads, 200 examples) with a strict check (sum(passed) < cap).
- restore validates every counter strictly (type(int), no bool, >= 0, finite non-negative decimal) and leaves the ledger untouched on failure (tested).
- The lazy per-loop semaphore handles the "each asyncio.run builds its own" rule and is covered by a test that runs asyncio.run twice.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
1. herness/harness/gates.py:123 — `math.ceil(analyst_max_concurrency * oversubscribe)` uses float, so exact products round up one slot too many: `TaskSlots.for_run(100, 1.1)` gives 111 (100*1.1 = 110.00000000000001), not 110. The builder used Decimal(str(x)) in new_phase_budgets for the same reason; do the same here for consistency. Impact: one extra task slot for some knob values (the range is 1.0-4.0).
2. herness/harness/budget.py:95-104 — `str(Decimal)` can produce exponent form: a tiny per-call cost gives `cost_used: "1E-7"`, and a cap given as `Decimal("1E+1")` gives `cost_cap: "1E+1"` (probed). It is still a valid decimal for `Decimal()`, but it is unexpected for `run.token_usage` readers and the UI. Consider `format(d, "f")`.
3. herness/harness/budget.py:119-128 — restore only checks 7 of the 12 snapshot keys. A snapshot missing the derived keys (`tokens_cap`, `tokens_used`, `tokens_remaining`, `cost_cap`, `cost_remaining`) is accepted, although the spec says "missing key -> SchemaViolation". Harmless (they are derived or knob-owned), but the literal reading is every key. Also, `bad` is built eagerly on every call (budget.py:118).
4. herness/harness/gates.py:34-43 — if a gate or slot is re-entered from a new loop while a permit from the old loop is still held, the late `release()` goes to the new semaphore and raises its value above `size`. This is out of spec scope (gates are never shared across loops), so a comment or an assert on `in_flight == 0` at rebind would be enough.
5. tests/unit/harness/test_budget.py:47-51 — the protocol test only asserts `callable(...)`; the real check is mypy's structural assignment. Acceptable, but the runtime asserts add nothing.

### Assessment
**Task quality:** Approved
**Reasoning:** All units U06-25..U06-31 and tests UT06-14..UT06-20 / PT06-05 (200 examples) match the brief. Coverage is 100% / 99% and the budgets hold (budget.py exactly at 170). The remaining items are minor precision and format polish.

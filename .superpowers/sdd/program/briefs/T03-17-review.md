# Review: T03-17 — Primary rules and resolution reference

Base `a5f6be3` → head `46ee5c3`. Files changed: `herness/enrich/decide.py`, `tests/unit/enrich/test_decide.py`.

### Spec Compliance

- U03-70 `primary_decider_for` — ✅ Spec compliant (teacher computation, override lookup, laya-accepted gate, openjev/jev enabled checks all match the algorithm text exactly; never returns a disabled backend, `llm` correctly used as the unconditional terminal fallback per D7).
- U03-71 `chain_after` — ❌ Issues found: violates its own stated invariant ("never contains `primary`") and the postcondition ("every member at or before primary's position removed") when `primary == "jev"` but `cfg.escalation_chain` is authored with the `"openjev"` placeholder (the documented swap convention). See Critical-1.
- U03-72 `Resolution` — ✅ Spec compliant. Field names, order and types match U03-72 exactly (`status, answer, probability, decider, decider_version, escalated, decided_at, agreement, review_status`).
- U03-73 `gate` — ✅ Spec compliant. `p_calibrated >= threshold`, boundary is inclusive (`>=`, not `>`) as required.
- U03-74 `resolve_pair` — ✅ Spec compliant on every algorithm branch verified (see "Check especially" below). ⚠️ One judgment call flagged under Important-1: PT03-08's property test excludes the human branch entirely rather than only the part of the invariant genuinely in tension.

### ⚠️ Items

- **Deviation 1 (`deciders: DecidersSettings` param on `primary_decider_for`/`chain_after`)**: judged acceptable. This is a real, unavoidable consequence of the R-76 settings split (`enabled` flags moved out of `DecisionsConfig` into `config/models.yaml`'s `DecidersSettings`); both units' algorithms textually depend on `deciders.jev.enabled`/`deciders.openjev.enabled`, which cannot be satisfied without this parameter. Documented in the module docstring (`herness/enrich/decide.py:7-11`) and the test module docstring. Not a defect.
- **Deviation 2 (`resolve_pair` 7 kw-only args, `noqa: PLR0913`)**: the signature is verbatim from U03-74 (brief line 60), which is binding, and conflicts with global-constraints.md's `≤6 arguments (PLR0913)` rule. Per reviewer-rules calibration ("if the brief mandates something this rubric calls a defect, report it as Important labeled plan-mandated"), reporting as **Important — plan-mandated** below rather than silently accepting or treating as Critical. The `noqa` carries a one-line reason (`decide.py:204`), which is the right way to record the conflict.
- **PT03-08 `human=None` restriction**: only the `escalated` sub-property is genuinely in tension with a human correction (the algorithm hard-codes `escalated=False, decider="human"` regardless of whether `decider != primary`, per U03-74 step 2's literal text). The `final ⇔ fields set` sub-property is **not** in tension — the human branch always returns `status="final"` with every field populated, so it holds trivially and could have been asserted for every drawn `human` value while only special-casing (or skipping) the `escalated` assertion when `decider == "human"`. Restricting the entire property to `human=None` drops property-based coverage of the human path for `final ⇔ fields set`, even though only `escalated` needed the carve-out. See Important-1.

### Check especially — `resolve_pair` vs. every step of U03-74

Verified by reading `_machine_result`/`resolve_pair` (decide.py:168–252) against each algorithm step, and cross-checked against exact-match example tests:

- Ensemble escalation relative to `laya` row (step 1a): `escalated = laya is None or ens.answer != laya.answer` (decide.py:173-176) — matches invariant carve-out exactly; covered by `test_ut03_71_ensemble_row_wins_and_escalates_without_laya`, `..._ensemble_matches_laya_is_not_escalated`, `..._ensemble_disagrees_with_laya_is_escalated`.
- Gate on `p_cal` (step 1b): `gate(primary_row.p_cal, threshold)`, not escalated — matches; covered by `test_ut03_71_primary_pass`.
- Chain member taken whatever its probability (step 1c): loop returns first present row with no gate call, `escalated=True` — matches; covered by `test_ut03_71_primary_fail_with_chain_row`.
- Human confirm and correct (step 2): both branches match spec text verbatim, including `probability=1.0`, `decider="human"`, `decider_version="human"`, `escalated=False`, `review_status` split on whether `m` existed — covered by `test_ut03_71_human_confirms_machine_result`, `..._human_corrects_machine_result`, `..._human_label_with_no_machine_result_is_confirmed`.
- Pending vs none (step 3): `review_status = "pending" if pending_review else "none"` — matches; covered by `test_ut03_71_pending_review` and the default-`none` assertions elsewhere.
- `out_of_scope` only when no primary row exists (step 4): `"out_of_scope" if not in_scope and primary not in rows else "queue"` (decide.py:239-241) — matches exactly, including the case where a below-threshold primary row is present but the item is out of scope (still `queue`, not `out_of_scope`); covered by `test_ut03_71_none_out_of_scope_and_no_primary_row` and `test_ut03_71_out_of_scope_but_primary_row_present_still_queues`.

### Findings

#### Critical (Must Fix)

1. **`chain_after` returns `primary` itself and mis-slices the chain when `primary == "jev"` but the config's `escalation_chain` uses the `"openjev"` placeholder.** `herness/enrich/decide.py:97-113`, specifically the membership test at line 98 (`if primary == "laya" or primary not in chain:`). The slice is computed against the *pre-swap* literal chain, so when `primary` is the string `"jev"` and `cfg.escalation_chain` literally contains only `"openjev"` (the normal authoring convention per the §5.5 comment / this same function's own swap logic), `primary not in chain` is true and the function falls into the "return the whole chain" branch — the same branch used for `primary == "laya"`. This both violates the stated invariant "never contains `primary`" and the postcondition "every member at or before primary's position removed."
   Reproduced: with `cfg.escalation_chain = ("openjev", "llm")` and `deciders.jev.enabled = True`, `chain_after("jev", cfg=cfg, deciders=deciders)` returns `('jev', 'llm')` instead of the expected `('llm',)`.
   This is reachable in practice: `primary_decider_for` is spec'd (and tested, UT03-68 case `override_jev_enabled_returns_jev`, `test_decide.py:138-147`) to return `"jev"` from a question override even when `cfg.escalation_chain` is authored with `"openjev"` — exactly the composition `chain_after(primary_decider_for(...), ...)` that design 03 §5.7 expects callers to use. No UT03-69 case exercises `primary="jev"` against an `"openjev"`-only chain, so the gap went uncaught. Fix: compute the swapped/canonical chain first, then locate `primary`'s position in that canonical form (or special-case `primary == "jev"` to match against `"openjev"`'s position when `jev` is enabled).

#### Important (Should Fix)

1. **PT03-08 excludes the human branch entirely, not just the sub-property in tension.** `tests/unit/enrich/test_decide.py:543-582`; the `@given(...)` strategy never draws `human` (always implicitly `None` via the fixed `human=None` call at line 559). As discussed under ⚠️ above, only `escalated ⇔ decider != primary` is genuinely incompatible with the human-correction branch; `final ⇔ fields set` holds unconditionally for that branch too (it always returns `status="final"` with every field set) and was droppable from property coverage without need. The human path is however still covered by exact-equality example tests (`test_ut03_71_human_confirms_machine_result`, `..._human_corrects_machine_result`, `..._human_label_with_no_machine_result_is_confirmed`), so this is a coverage-strength gap rather than an unverified behavior, but it is a real narrowing of the property test's scope relative to the spec's own property wording. Recommend extending the hypothesis strategy to also draw `human: tuple[str,datetime] | None`, asserting `final ⇔ fields set` unconditionally, and only special-casing (or skipping) the `escalated` assertion when `decider == "human"`.
2. **`resolve_pair`'s 7 keyword-only parameters exceed the global 6-argument limit — plan-mandated.** `herness/enrich/decide.py:204`. The signature is verbatim from the binding U03-74 unit spec (brief line 60) and cannot be reduced without deviating from the spec; `# noqa: PLR0913` with an inline reason is the correct way to record this per project convention. Flagged per reviewer-rules calibration (brief-mandated defects are still reported, as Important) — no action needed beyond acknowledgment; do not treat as a build error.

#### Minor (Nice to Have)

1. **Report's test count is off by one.** T03-17-report.md claims "13 table-driven UT03-71 cases"; the file has 12 distinct `test_ut03_71_*` functions (verified via `grep -n "^def test_ut03_71"`). Cosmetic — all 12 are meaningful, distinctly named, and pass; no coverage is actually missing here (see "Check especially" above, all algorithm branches are hit). Worth a one-line correction in the report for accuracy.
2. **`chain_after` line 102 is a long, dense one-liner** (`swapped = tuple("jev" if name == "openjev" and deciders.jev.enabled else name for name in after)`). Passes `ruff check` clean and is well-commented at the call site, but a short local helper or two-line form would read slightly easier — pure polish, not required.

### Table coverage check (UT03-68, UT03-69, UT03-71)

- UT03-68 (9 cases, `test_decide.py:97-174`): covers laya-degraded/accepted/not-accepted, override disabled→teacher(llm), override jev enabled, override jev disabled→teacher(openjev), override llm, jev-in-chain-and-enabled-is-teacher, jev-in-chain-but-disabled→openjev-is-teacher. All rows the spec's algorithm branches require are present. ✅
- UT03-69 (5 cases, `test_decide.py:189-221`): jev replaces openjev in whole chain, primary=openjev leaves `[llm]`, disabled members removed, dedupe + llm-last, primary at end → empty. Missing: `primary="jev"` against an `"openjev"`-only chain — this is exactly the gap that let Critical-1 through. ❌ (incomplete relative to what U03-70/U03-71 composition requires, even though every literal UT03-69 row from the spec table's example is present)
- UT03-71 (12 cases, `test_decide.py:270-517`): primary pass, primary fail with chain row, none in-scope (queue), none out-of-scope+no-primary-row (out_of_scope), out-of-scope-with-primary-row (still queue), pending review, human confirms, human corrects, human label with no machine result, ensemble wins/escalates without laya, ensemble matches laya (not escalated), ensemble disagrees with laya (escalated). This is a complete enumeration of every case named in the spec's UT03-71 row ("human confirm and correct, ensemble, primary pass, primary fail with chain row, none in/out of scope, pending review"). ✅
- Test IDs: every test function name carries its unit's ID (`test_ut03_68_...`, `test_ut03_69_...`, `test_ut03_70_...`, `test_ut03_71_...`, `test_pt03_08_...`) per the impl spec's §11 requirement. ✅

### Other checks

- `decide.py` line budget: 252 lines (module-map budget 260). ✅ within budget.
- `ruff check` on both changed files: clean (re-verified independently). `pytest tests/unit/enrich/test_decide.py`: 30/30 passed (re-verified independently, matches report).

### Assessment

**Task quality:** Needs fixes
**Reasoning:** `resolve_pair`, `gate`, and `Resolution` are correct and thoroughly tested against every algorithm branch and both spec tables. `chain_after`, however, has a genuine, spec-invariant-violating bug reachable through the documented `primary_decider_for` → `chain_after` composition whenever a question override selects `"jev"` against an `"openjev"`-authored escalation chain — this must be fixed (and a UT03-69 case added for it) before this task can be approved. The PT03-08 scope-narrowing and the plan-mandated `PLR0913` deviation are worth a look but do not block on their own.

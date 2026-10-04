# Review for T03-01: Shared decision types

## Spec Compliance

| ID | Item | Verdict |
|----|------|---------|
| U03-01 | `QuestionType`, `Entity` (Literal closed sets) | Y |
| U03-02 | `Question` (all 10 fields + 8 validator rules a-h) | Y |
| U03-03 | `QuestionSet` (version pattern, <=64 questions, unique ids, `_by_id` PrivateAttr) | Y |
| U03-04 | `QuestionSet.get` (returns question or `ConfigError("unknown question <qid> in <version>")`) | Y |
| U03-05 | `QuestionSet.for_entity` (order-preserving filter, same version) | Y |
| U03-06 | `DecisionInput` (all fields/bounds; no self-check of `content_hash==content_hash(text)`, correctly left to construction sites per spec's own Invariants row) | Y |
| U03-07 | `Answer` (finite/[0,1] values, sum +/-1e-3, answer in distribution, probability match +/-1e-6) | Y |
| U03-08 | `DecisionOutput` (error-only-when-empty-answers, error class-name regex) | Y |
| UT03-01 | `Question(type="text")` -> `ValidationError` | Y |
| UT03-02 | Bad question shapes (1 option, 256 options, 3-tuple levels, levels-on-bool) | Y (adapted per sub-controller ruling b: exercises `Question` directly, `load_question_set` not in scope) |
| UT03-03 | bool-word option keys (`true`,`No`,`yes`,`FALSE`) rejected | Y (adapted, ruling b) |
| UT03-04 | `get`/unknown `get`/`for_entity` x2 | Y |
| UT03-05 | `content_hash == content_hash(text)` | Y (adapted, ruling b -- constructs `DecisionInput` directly instead of via `build_inputs`/`pair_inputs`) |
| UT03-06 | distribution sum/range/membership rules | Y (ruling a: 1e-3 tolerance implemented; "1.002 accepted" in the spec's literal test row correctly rejected as a transcription error) |
| UT03-07 | `error` with non-empty answers; malformed `error` string | Y |
| PT03-01 | any perturbation beyond 1e-3 rejected | PARTIAL -- see Minor #1, property test only generates upward (sum-increasing) perturbations |

Acceptance checks: `mypy --strict herness/core` 0 errors, `lint-imports` clean (types imports only `errors`), `check_type_ownership` exits 0 for owner "03" -- all evidenced in the build report and consistent with the diff. `herness/core/types/_ownership.py` (read directly in the worktree, unchanged by this diff) already pre-declares `QuestionType`/`Entity`/... under owner "03" from the T00-08 foundation work, so U03-01's ownership-table requirement was already satisfied and needed no new edit here.

## Items needing attention (not clear pass/fail from diff alone)
- PT03-01 coverage is directionally partial (see Minor #1).
- Sub-controller rulings (a) and (b) are correctly applied and I found no reason to dispute either.

## Findings

### Critical (Must Fix)
None.

### Important (Should Fix)
1. **Module size -- plan-mandated overage.** `herness/core/types/decisions.py` is 212 lines against the module map's stated budget of 130 (brief line 90), a 63% overage -- well under the ENG 400-line hard limit but a real deviation from what the brief mandates. Global constraints state budgets "come from the owning spec's Section 2 module map," and the reviewer rubric requires checking file-size budgets under quality; per the calibration rule, a brief-mandated item that the rubric would otherwise treat as lower severity is reported as Important, labeled plan-mandated. The implementer's rationale (readability/testability of ~8 named validation rules across 5 models) is reasonable and is disclosed prominently in the report, but a stated rationale does not downgrade a finding. Recommend either a program ruling accepting the overage for this card, or a follow-up pass that trims by consolidating some of the smaller single-line raise blocks (e.g., `_check_levels`/`_check_options` sub-checks) -- file: `herness/core/types/decisions.py:1-212`.

### Minor (Nice to Have)
1. `tests/unit/enrich/test_decisions.py:196-208` (`test_pt03_01_distribution_sum_tolerance`) only perturbs the distribution sum upward (`_DELTA` is drawn from `[2e-3, 0.3]`, always added to `value_a`); it never exercises a downward perturbation (sum < 1 - 1e-3). The validator itself is symmetric (`abs(total - 1.0) > _SUM_TOL`), so the functional risk is low, but the property test as written covers only one direction of "any perturbation beyond 1e-3."
2. Several "supporting" tests added beyond the spec's rows don't carry a Test ID consistent with the global-constraints naming rule ("every test function name contains its ID ... first line of its docstring starts with the ID"): `test_question_dynamic_options_and_descriptions` (`tests/unit/enrich/test_decisions.py:211`) has no test ID anywhere in its name; `test_ut03_04_question_set_limits` (`:95`), `test_ut03_06_answer_probability_mismatch` (`:136`), and `test_ut03_06_answer_out_of_range_values` (`:142`) reuse an existing UT-id in the function name but their docstrings open with "Supporting U0x-xx" (a *unit* ID, not a *test* ID) rather than a UT-id. Harmless functionally -- these are additive coverage, not replacements for spec rows -- but a convention drift worth tidying.
3. `DecisionOutput.answers` (`herness/core/types/decisions.py:238`) is given `Field(default_factory=dict, max_length=64)`; the brief's U03-08 Signature shows `answers: dict[str, Answer]` with no default. Harmless (an empty-answers `DecisionOutput` with `error=None` is a valid state under the stated postconditions either way) but it's an unrequested addition to the public signature.

## Verification performed
- Cross-checked every field, bound, and validator rule of `Question`, `QuestionSet`, `QuestionSet.get`, `QuestionSet.for_entity`, `DecisionInput`, `Answer`, and `DecisionOutput` in `herness/core/types/decisions.py` (diff) against the verbatim unit specs U03-01 through U03-08 read directly from `docs/impl/03-enrichment.impl.md:158-288` in the worktree (the brief only reproduced U03-01 and U03-08 verbatim).
- Read `herness/core/errors.py:71-257` to confirm `ConfigError(msg)` matches the positional-message constructor `QuestionSet.get` calls, and that `ConfigError` is the class the spec names.
- Read `herness/core/types/_ownership.py` directly (not touched by this diff) to confirm the "03" owner row already lists `QuestionType, Entity, Question, QuestionSet, DecisionInput, Answer, DecisionOutput` from prior foundation work, so U03-01's TYPE_OWNERS requirement needed no new edit.
- Ran `wc -l herness/core/types/decisions.py tests/unit/enrich/test_decisions.py` to verify the reported 212-line module size (confirmed: 212 / 257).
- Did not re-run the test suite; relied on the report's RED/GREEN/coverage/gate evidence, which is complete and consistent with the diff.

## Assessment
**Task quality:** Needs fixes
**Reasoning:** Type-by-type and test-by-test the implementation matches the spec exactly, including two correctly-applied sub-controller rulings and no functional or correctness defects. The one Important finding is the module-size overage against the brief's own 130-line budget (212 actual) -- a real, sizeable plan-mandated deviation rather than a quality nit, so it should go back for a trim or an explicit program ruling before this card is accepted as-is. The Minor items (partial-direction property test, a few unlabeled supporting tests, one unrequested field default) do not block acceptance on their own.

---

## Re-review round 1

Scope: the four findings from the initial review only (module size, PT03-01 direction, test naming, unrequested `answers` default). Fix commit `f6287c6`, diff `2d215d9..f6287c6` (`T03-01-fix1.diff`), updated report `T03-01-report.md` "Fix round 1" section.

### Finding-by-finding

1. **Important — module size.** `herness/core/types/decisions.py` trimmed 212 -> 177 lines (confirmed via `wc -l`), still 36% over the 130-line module-map budget (well under the 400-line hard limit). The trim is genuine and mechanical: a shared `_Frozen(BaseModel)` base absorbs the repeated `model_config` line across all five models, and a module-level `_raise_if(bad, msg)` helper replaces every `if cond: msg = "..."; raise ValueError(msg)` block with a single call. I read every changed validator line-by-line against the pre-fix version (`Question._check`/`_check_options`/`_check_levels`, `QuestionSet._check_unique`, `Answer._check`, `DecisionOutput._check_error`) and confirmed every condition and short-circuit order is preserved exactly (e.g., the `self.levels is None` raise in `_check_levels` still aborts before the `for description in self.levels or ()` line executes, since `_raise_if`'s internal `raise` propagates immediately; the "answer not a key of distribution" check in `Answer._check` still runs before the dict-indexed probability-mismatch check, so no new `KeyError` risk). Only error-message *text* changed in a couple of places (e.g. "choice question needs 2-255 static options" -> "static options need 2-255"); no test asserts message text, so this is cosmetic. I independently re-ran `uv run pytest tests/unit/enrich/test_decisions.py --cov=herness.core.types.decisions --cov-branch`: 18 passed, 100% line/branch coverage, matching the pre-fix numbers. `mypy --strict herness/core` and `ruff check` also re-run clean.

   Per the sub-controller's stated position — if the trim is genuine and the refactor introduces no behavior change, the residual overage is an accepted deviation rather than a blocker — I accept that ruling here: the trim is real (63%->36% over budget, -35 lines, via two legitimate DRY extractions) and independently verified behavior-preserving. **No longer treated as a blocking finding.** Downgraded to a non-blocking note: 177 vs. 130 remains an accepted deviation per this ruling, not a defect to re-flag on future passes of this card.

2. **Minor 1 — PT03-01 unidirectional.** Fixed. `test_pt03_01_distribution_sum_tolerance` (`tests/unit/enrich/test_decisions.py`) now draws a random `sign` via `st.sampled_from([1.0, -1.0])` and perturbs `value_a` by `sign * delta`, with `assume(0.0 <= perturbed_a <= 1.0)` and `assume(abs(perturbed_a + value_b - 1.0) > _SUM_TOL)` guarding validity. This now exercises both the sum pushed above `1 + 1e-3` and below `1 - 1e-3`. Resolved.

3. **Minor 2 — test naming/ID conformance.** Fixed. `test_question_dynamic_options_and_descriptions` (previously carrying no test ID at all) is renamed `test_ut03_02_question_additional_shape_rules` with a docstring opening `"""UT03-02 (supporting U03-02): ...`. `test_ut03_04_question_set_limits`, `test_ut03_06_answer_probability_mismatch`, and `test_ut03_06_answer_out_of_range_values` now open their docstrings with the UT-id already present in their function names (`"""UT03-04 (supporting U03-03): ..."`, etc.) instead of a bare unit ID. All test functions in the file now satisfy the global-constraints naming rule. Resolved.

4. **Minor 3 — unrequested `answers` default.** Fixed. `DecisionOutput.answers` is now `Field(max_length=_MAX_ANSWERS)` with no default, matching the brief's U03-08 signature exactly (`answers: dict[str, Answer]`, no default shown). Confirmed no test relied on the omitted-default path (all `DecisionOutput(...)` call sites in the test file pass `answers=` explicitly, both before and after the fix). Resolved.

### Regression check (`_Frozen` / `_raise_if` refactor)

Independently re-ran (not just trusting the report):
- `uv run pytest tests/unit/enrich/test_decisions.py -q --cov=herness.core.types.decisions --cov-branch` -> 18 passed, 100% line/branch coverage (matches pre-fix).
- `uv run mypy --strict herness/core` -> success, 10 source files, 0 errors.
- `uv run ruff check herness/core/types/decisions.py tests/unit/enrich/test_decisions.py` -> all checks passed.

No regressions found. Line-by-line comparison of every validator against the pre-fix version confirms identical conditions, short-circuit order, and control flow; only cosmetic error-message text differs in a few spots, which no test depends on.

### Verdict

**Task quality: Approved**
**Reasoning:** All four findings from the initial review are resolved or, in the case of the module-size overage, accepted as a genuine, behavior-preserving, independently-verified deviation per the sub-controller's ruling. No new issues were introduced by the fix — the `_Frozen`/`_raise_if` refactor is a faithful mechanical transform, confirmed by an independent test/coverage/mypy/ruff run. No open findings remain.

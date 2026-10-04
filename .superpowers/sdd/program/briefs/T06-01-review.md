# T06-01 review (Shared swarm types)

Reviewed commit c49da43 in worktree agent-a65b4b3b805e83310, checked against the brief, impl 06 §3.1 (U06-01..U06-12, U06-140), DECISIONS R-01/R-21/R-22/R-23/R-28/R-29/R-75 and the global constraints. Checks run: the owner-05 `Budgets` bounds (herness/core/types/harness/tooling.py:100-104), the `LoopCheckpoint.from_envelope` precedent (harness/agent.py:135-145), the private module-level helpers in the other owners' type modules, and the module-size tool scope. Known and expected, not flagged: the OWN010/OWN031 failures and the UT00-48 / IT00-02 failures stay red until T06-02 adds its 8 names.

### Spec Compliance
- ✅ Spec compliant.
  - U06-01 vocabularies ✅ (all 11 match exactly; `SKEPTIC_CHECKS` is in declared order; the PEP 695 `type` form has a precedent in jobs.py:29-45)
  - U06-02 EntityScope ✅ · U06-03 TaskInputs ✅ · U06-04 TaskBudget ✅ (fields, `to_budgets`, `scaled` formula exactly as specified) · U06-05 TaskSpec ✅ (both invariants) · U06-06 PlannedTask ✅ (no budget/tools/priority/model_role/id fields; `extra="forbid"`, TH06-04)
  - U06-07 Finding ✅ (all 3 invariants; `created_at` is aware and normalised to UTC) · U06-08 correctly absent (R-75) · U06-09 CheckResult ✅ · U06-10 Challenge ✅ (exactly one result per check, including duplicates; revise ⇒ actions) · U06-11 CrossCheck ✅ · U06-12 VerificationRecord ✅ (reason grammar, ≤ 200 chars) · U06-140 SwarmTaskState ✅
  - Tests: UT06-01 ✅ · UT06-02 ✅ · UT06-03 ✅ · UT06-04 ✅ · UT06-06 ✅ · UT06-07 ✅ · UT06-93 ✅ (duplicates rejected, round trip equal, `SchemaViolation` on a stored invalid value). Acceptance check `herness.core.types.TaskSpec is herness.core.types.swarm.TaskSpec` ✅ (test_swarm_tasks.py:497-503).
  - All models use `extra="forbid", frozen=True, strict=False` per the impl 06 §3 convention. Line budgets: tasks.py 299/300, swarm/__init__.py 21/40.
- ⚠️ Cannot verify from diff: the gate results (ruff, mypy strict, lint-imports, 80 passed, 100 % line and branch coverage) are the builder's claims and were not re-run. `--require-test-ids` does not exist on this branch; the test names were checked by eye and all follow `test_ut06_NN_`.

### Strengths
- The invariants follow the spec closely, and each error message names what failed.
- Upper and lower bounds are tested, not only one side.
- `from_envelope` mirrors the existing `LoopCheckpoint.from_envelope` (R-29), so the two checkpoint keys are read the same way.
- `SKEPTIC_CHECKS` is derived from the Literal, so the two cannot drift apart.
- ID patterns are applied wherever the field is a known ID kind.

### Issues

#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. **Plan-mandated: `TaskBudget` accepts values that `to_budgets` cannot convert.** herness/core/types/swarm/tasks.py:117-141 against herness/core/types/harness/tooling.py:100-103.
   - Spec 05 `Budgets` requires `max_steps` ≤ 200, `max_tokens` ≥ 1,000 and `wall_clock_s` ≤ 86,400, all strict.
   - U06-04 allows `max_tokens` ≥ 1, puts no upper bound on steps or wall clock, and has `scaled` floor tokens at 1.
   - So a valid `TaskBudget`, including a child budget from spawn-broker step 5 (`parent.budget.scaled(settings.child_budget_factor)`, impl 06 line 893), can make `to_budgets` raise a raw pydantic `ValidationError`. That is neither `SchemaViolation` nor the documented contract, and it happens at task start.
   - The builder followed the spec, so this needs a controller ruling. Recommendation: make `TaskBudget` carry the `Budgets` bounds (`max_steps` 1–200, `max_tokens` ≥ 1,000, `wall_clock_s` 1–86,400) and have `scaled` floor `max_tokens` at 1,000. A tighter bound at config and spawn time is safer (TH06-03). Also add a UT06-02 case where `to_budgets` succeeds on a budget at the lower bound after `scaled`.

#### Minor (Nice to Have)
1. herness/core/types/__init__.py:7. The `# noqa: I001 - compact swarm block` sits on the `decisions` import line. Ruff reports I001 once per contiguous import block, so this turns off import sorting for every owner's import in the file, and the comment wrongly suggests it covers only the swarm block. Consider moving the explanation to a comment above the block.
2. herness/core/types/swarm/tasks.py:233-235. `CrossCheck.values: list[float]` accepts NaN and inf. Pydantic serialises those to `null` in JSON by default, so a stored `finding.verification` would fail on read-back. Consider `list[Annotated[float, Field(allow_inf_nan=False)]]`; `Finding.confidence` already does this.
3. herness/core/types/swarm/tasks.py:284-292. `from_envelope` treats `{"state": None}` as present and raises `SchemaViolation`, while `LoopCheckpoint.from_envelope` (harness/agent.py:137-139) treats `loop: None` as absent. The spec says "when the key is present", so this is compliant, but the two readers are inconsistent. Align them, or add a test that pins the chosen behaviour.
4. herness/core/types/swarm/tasks.py:131. `Budgets(**self.model_dump(), ...)` couples the two field sets: a later `TaskBudget` field would break `Budgets(extra="forbid")`. Listing the four fields explicitly costs about 3 lines, but the file is at 299/300, so this is fine to accept.
5. herness/core/types/swarm/tasks.py:13-14. `AfterValidator` and `model_validator` are imported from `pydantic.functional_validators`, and `ValidationError` from `pydantic_core`, only to save lines. This works and is allowed by OWN020, but it is unusual. Acceptable.

### Builder concerns: reviewer view
- **`herness/core/types/__init__.py` at exactly 150/150 lines.** This does not block T06-01. It blocks T06-02 (8 names), T07-01 and T09-01. Almost all of the lines are the one-name-per-line `__all__`. I recommend a controller ruling before T06-02 is dispatched, choosing one of two options:
  - Preferred: allow a compact `__all__` (several names per line under `# fmt: off`, still in plain sorted order for OWN032), which frees about 60 lines.
  - Otherwise: raise the impl 00 budget to about 220.
- **`TaskBudget.max_tokens` ≥ 1 against spec 05 ≥ 1,000.** This is a real cross-spec defect and is wider than the builder reported: the `max_steps` ≤ 200 and `wall_clock_s` ≤ 86,400 caps also differ. See Important 1. It needs a ruling.
- **Added classmethod `SwarmTaskState.from_envelope`.** Agree with it. The spec describes the behaviour but gives it no name. A method on the type is not a module-level function (R-75), and it copies the R-29 `LoopCheckpoint.from_envelope` pattern. Record it as a named addition in the impl 06 §13 deltas, so U06-53, U06-89 and the writer call it rather than repeating the try/except. See Minor 3 for the `None` handling.
- **Open choices.**
  - Patterns on `run_id`, `task_id`, `supersedes` and `merged_into`, the 1–200 char bound on `PlannedTask` ids, and `phase` `min_length=1`: agree. These are stricter and consistent with §3 ID conventions.
  - `scaled` raising `ValueError` for a factor outside (0, 1] or NaN: agree. This is a programming error, not data, and the spec names no class.
  - The reason grammar that rejects `"withdrawn:"` (empty detail): agree.
- **RUF022 noqa on the top-level `__all__`.** Agree. RUF022's isort-style order (SCREAMING_CASE first) truly conflicts with OWN032's plain `sorted()`. A noqa that gives its reason is the least invasive fix. The subpackage `__all__` using RUF022 order is acceptable because OWN032 does not check it. A longer-term cleanup would be for OWN032 to accept either order. That is optional and not for this card.
- **Private module-level helpers `_unique` and `_reject_reason`** (tasks.py:56-68). Not a violation of R-75 in practice: every owner module has the same pattern (decisions.py:34, harness/agent.py:148, evidence.py:29, jobs.py:60), and the ownership check allows it.

### Assessment
**Task quality:** Approved
**Reasoning:** Every unit and test row in the card is implemented faithfully, with thorough boundary tests and no extra scope. The one Important item is a cross-spec bound conflict between U06-04 and spec 05 `Budgets`, mandated by the plan and needing a controller ruling. The `__init__.py` line-budget ruling is needed before T06-02.

## Re-review round 1 (commit 78e790c, diff c49da43..78e790c)

Scope: Important 1, Minor 1 to 3, the `__all__` compaction, and a regression check. Beyond reading the diff, I ran one focused check because this round changes sorted order, formatting and the tests:
- `__all__` is in plain sorted order (True); it has 75 names, none duplicated.
- `pytest tests/unit/core/types/test_swarm_tasks.py`: 83 passed.
- `ruff check` on herness/core/types and the test file: clean.
- `ruff format --check` on herness/core/types: clean.
- Line counts: herness/core/types/__init__.py 108/150, tasks.py 300/300.

| Finding | Status | Evidence |
|---|---|---|
| Important 1: TaskBudget and spec 05 Budgets bounds | ✅ Fixed | tasks.py:117-120 now requires `max_steps` 1–200, `max_tokens` ≥ 1,000 and `wall_clock_s` 1–86,400. `scaled` floors tokens at 1,000 (tasks.py:135). Because factor ≤ 1, `scaled` can never go above an upper bound, so every valid `TaskBudget` converts with `to_budgets`. New tests (test_swarm_tasks.py:276-297) cover the floor after `scaled` and a successful `to_budgets`, the widest valid budget converting, and a rejection just outside each bound. The existing `max_tokens=-1` rejection case still holds. |
| Minor 1: `noqa: I001` on the decisions import | ✅ Fixed | Removed. The swarm import is now a normal isort-formatted block, and ruff check is clean without the suppression. |
| Minor 2: CrossCheck NaN and inf | ✅ Fixed | tasks.py:231 has `allow_inf_nan=False` on each value. The test covers NaN, +inf and -inf. |
| Minor 3: `from_envelope` with `state: None` | ✅ Fixed | tasks.py:292-294 treats a null `state` the same as an absent one, matching `LoopCheckpoint.from_envelope`. Pinned by a test (test_swarm_tasks.py:514). |
| `__all__` compaction | ✅ | Under `# fmt: off` / `# fmt: on`, in plain sorted order for OWN032, and still carrying the RUF022 noqa with its reason. The file has 42 lines of headroom, enough for the remaining owners (T06-02, T07-01, T09-01) at about 3 names per line plus one import block each. |
| Regressions | None found | Minors 4 and 5 were accepted as-is in the first review. tasks.py is now exactly at its 300-line budget, so any later growth belongs in `drafts.py` (T06-02), as planned. |

Record this: U06-04 in impl 06 still says `max_tokens` ≥ 1 and `scaled` floors at `max(1, …)`. The code now follows the controller ruling, which aligns U06-04 with spec 05 `Budgets`. Enter this in the impl 06 §13 deltas, or amend U06-04, so the spec and the code agree. `from_envelope` (see the first review) also belongs in those deltas. Neither is a code finding.

**Round 1 verdict:** Approved. All four findings are fixed with tests and nothing regressed.

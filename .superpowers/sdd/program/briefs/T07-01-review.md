# T07-01 review (verify agent)

Commit reviewed: d69bd05 (base 311ed52). Worktree: D:\herness\.claude\worktrees\agent-ad60debef6d692056

### Spec Compliance
- ✅ Spec compliant. I checked U07-01..U07-17 field by field against impl 07 §3.1/§3.2. Every name, type, default, bound, pattern and validator in the spec is present:
  - Literals and KIND_LAYER (a MappingProxyType, 3/5/3 split).
  - Provenance is strict, frozen and extra=forbid, with the human→author_ref and agent→author_role+run_id invariants.
  - MemoryItem is lax and AwareDatetime. It enforces KIND_LAYER, plus a 2,000-char content limit, or 8,000 for sql_template/qa_pair.
  - MemoryProposal is strict with 1–8,000 chars, at most 20 numbers and unique ids.
  - RecallHit requires the six keys, final == score and unconfirmed == pending_approval.
  - MemoryRunContext.from_tool_ctx(ctx, *, run_meta) raises ToolInputError("run kind unknown for <run_id>").
  - RecommendationDraft has summary 1–400 (R-30), finding_ids 1–50 matching FINDING_ID_RE, `^n[0-9]+$` refs and a closed target_type.
  - PriorRecommendation requires exactly 8 outcome keys. PriorContext fills tally with 0 and rejects unknown keys.
  - ConfidenceAdjustment bounds are 0.05–0.95, 0–1 and −0.25–0.15. SimilarOutcome is present.
  - RecallFilters, ProposeResult (merge rule), ChatTurn/SessionContext, PromotionReport/ExportReport (counts ≥ 0) and ContextStats ordering are all present.
  - MemoryNotFound(kind, ident) subclasses NotFound. It has the message "<kind> not found: <ident>", details {kind, ident} and survives pickling.
  - Deviations are all tightenings with no conflict: extra=forbid on every model, and PriorRecommendation.confidence bounded 0–1.
- R-01/R-75: the types live in the core.types.memory submodule, are re-exported, and hold data only. check_type_ownership exits 0 (only 06 and 09 are pending), so the acceptance import rule is enforced by OWN020. The file imports only pydantic, the stdlib, herness.core.errors and herness.core.types.harness.
- TH07-02: handled at the type level. Provenance is strict and extra=forbid, with author invariants. MemoryRunContext identity comes only from ToolContext and run_meta, never from model arguments. The run/task match check belongs to later write-path cards.
- ⚠️ Cannot verify from this card:
  - The spec §2 forbidden contract "memory must not import swarm/blackboard/pipelines" is deferred, because those modules do not exist and UT00-58 rejects contracts that name missing modules. This is justified, but it must be added when T06 creates them.
  - The literal acceptance command `pytest -k "UT07-01 or ..."` uses hyphens. The underscore form (`UT07_01 ...`) selects 11 tests, which pass.

### Gates (run by me)
- `pytest -k "UT07_01 or UT07_02 or UT07_03"`: 11 passed.
- New unit tests plus ST00-10: 20 passed.
- `mypy --strict herness/core herness/harness/memory`: 0 errors (21 files).
- `lint-imports`: 8 kept, 0 broken.
- `ruff check`: clean. `ruff format --check`: 82 files formatted.
- `tools.check_type_ownership`: exit 0.
- Branch coverage: 100 % for core/types/memory.py and for harness/memory/types.py.
- `tools.check_module_size`: exit 1, with MS001 `herness/core/types/memory.py 263 > 150`. This is the known open item; the sub-controller rules on it. Not counted against the builder.
- core/types/__init__.py `__all__`: 67 names, code-point sorted, and every previously exported name is kept.

### Budget assessment (herness/core/types/memory.py, 263 vs 150)
- The module has 10 models, 3 literals and 1 map, with about 90 field lines that the spec fixes. It also needs 7 spec-required validators. Formatting with ruff and two blank lines between classes adds roughly 40 lines.
- Some trimming is possible: fold the `_Strict`/`_Frozen` bases and shorten the docstrings, for about 15–25 lines. The realistic floor is about 230–240.
- Reaching 150 would need a second submodule, and the module map and R-01 ownership table assign owner 07 exactly one submodule. So 150 cannot be met without breaking the spec.
- I support the builder's proposal to raise the budget to about 270, still under the 400 hard limit.

### ST00-10 change (tests/integration/repo/test_import_contracts_enforced.py:21-45)
- Legitimate, not a weakening. The old code called `mkdir()` without a guard and always injected `herness.harness` into C1 and C4. Once this card creates `herness/harness` and edits pyproject, that code breaks: it raises FileExistsError or produces duplicate layers.
- The upward import is still planted in core/errors.py (line 26).
- The test still asserts that `layers[0] == "herness.harness"` (line 48) and that C4 forbids herness.harness (line 53). It still requires lint-imports to fail naming both contracts (lines 75-77).
- When the repo already lists harness, those assertions now check the committed config instead of a synthetic one, which is stricter.
- The test passes on this branch.

### Strengths
- The tests are meaningful and cover the edges: each bound on each side, strictness (`"0.5"` and `"1"` rejected), naive datetimes, extra fields, missing required-nullable fields, pickling, re-export identity, and core/harness ID-pattern parity.
- ID patterns cannot be imported upward from L0, so the core module keeps private copies. UT07-68 guards them against drift.
- The `_extra_attrs` use for MemoryNotFound is correct: without it, kind and ident would be lost on unpickle.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
1. tests/unit/harness/memory/test_memory_types_local.py:26,48,65,74,92,110,122,134: these type-level tests use IDs of later behavioural tests (UT07-24/32/44/52/68/78/80/81). The repo-wide ID check (IT11-31) will then see those IDs as present before their real behavioural tests exist. That can hide a missing test on the later cards.
   - Suggest `test_rf_…` names, or an explicit note in the later cards' briefs that these functions are type-level only.
2. herness/harness/memory/types.py:4-18 (`# fmt: off` plus `# noqa: I001`) and herness/core/types/__init__.py:84-97 (compacted `__all__` with `# noqa: RUF022`): formatter and linter suppressions used only to fit line budgets.
   - The reformatted `__all__` will also conflict at merge with every parallel card that adds shared types.
   - If the budget is raised, prefer the standard one-name-per-line layout.
3. herness/core/types/memory.py:61-66 and herness/harness/memory/types.py:30-31: the `_Frozen` base is defined in both modules. Trivial duplication, forced by the L0/L4 boundary and OWN041's private-name rule; acceptable.
4. herness/core/types/memory.py:149: `len(self.components) != 6` needs `# noqa: PLR2004`. Comparing `set(self.components)` with a `_COMPONENT_KEYS` constant would be clearer and consistent with `_OUTCOME_KEYS`/`_TALLY_KEYS`.
5. The pyproject.toml C1/C4 `herness.harness` entries and herness/harness/__init__.py duplicate the same edits in the parallel group. They need a de-duplicating merge, as the report already flags.

### Assessment
**Task quality:** Approved
**Reasoning:** Every unit U07-01..U07-17 matches impl 07 exactly, and UT07-01..03 are thorough and pass. All acceptance gates pass except the module-size budget, which is out of scope and awaiting a ruling (recommend about 270). The ST00-10 edit keeps every assertion and is a legitimate fix.

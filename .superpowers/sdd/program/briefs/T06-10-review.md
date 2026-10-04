# Review: T06-10 Pipeline base

Worktree: D:\herness\.claude\worktrees\agent-ad5ff153e1e6a70de
Commit under review: 6e34a6b (checkpoint 30cc76e), base 04b9b5d.

## Spec compliance (unit/test rows)

| ID | Item | Result |
|----|------|--------|
| U06-66 | `Pipeline` protocol (kind, deterministic_tasks, must_cover, planner_input, challenge_priority, writer_input, ranked_entities, recommendation_drafts) | ✅ — exact signature set, `@runtime_checkable`, docstring states the `must_cover` before `ranked_entities` invariant |
| U06-67 | `PlanContext` fields/config | ✅ — all 13 fields present with correct types; `dq_warnings`/`portfolio` sub-shapes (`_DqWarning`/`_Portfolio` TypedDicts, `extra="forbid"` via `@with_config`) match the spec's named keys and order exactly; `model_config = ConfigDict(extra="forbid", frozen=True, strict=False)` matches verbatim |
| U06-68 | `default_challenge_priority` | ✅ — `math.log10(1 + float(impact_usd(f))) * f.confidence`, matches spec formula exactly (base.py:139-141); verified pure, no side effects |
| U06-69 | `build_ranked_entities` | ✅ — first-appearance walk of `draft.recommendations` using the passed `entity_type` (not `rec.target_type`), then `remaining` sorted by `(rank, id)`, ranks numbered from 1 (base.py:144-163); test `test_ut06_48_build_ranked_entities_recommendation_order_then_must_cover_by_rank` exercises dedup + interleaved ranks correctly |
| U06-70 | `to_recommendation_drafts` | ✅ — every `finding_ids` entry across all recs checked for presence + `status == "verified"`, `ReportContractError("recommendation cites unverified finding: <id>")` on failure (base.py:170-177); valid path maps via `RecommendationDraft.model_validate` preserving `summary` markers (verified: test asserts `summary == "It saves [[n2]] a year"`) |
| U06-75 | `get_pipeline` factory | ✅ — `_PIPELINE_MODULES` dict maps `funding_review`→`herness.harness.pipelines.funding_review:FundingReviewPipeline`, `org_review`→`herness.harness.pipelines.org_review:OrgReviewPipeline` (module/class names match spec's U06-71/U06-73 owners exactly); unknown kind (incl. `"chat"`) raises `ConfigError("no review pipeline for kind <kind>")` verbatim; import is lazy (inside function, only on the found branch) |
| UT06-48 | Priority formula, ranked-entity ordering, Pipeline/PlanContext structural checks | ✅ — 6 tests, all present and correctly asserting behaviour (not just non-crash) |
| UT06-49 | `ReportContractError` + valid-draft spec-07 mapping | ✅ — 2 tests; covers both missing-finding and wrong-status branches, and a full field-by-field check of the mapped `RecommendationDraft` |

Extra (beyond card's required Tests row, not a defect): a partial `UT06-53` test covers only the unknown-kind branch of `get_pipeline`, correctly deferring the happy-path (`funding_review`/`org_review`) coverage to T06-11/T06-12 since those modules don't exist yet. This matches U06-75's own unit-table `Tests: UT06-53` reference and is explicitly called out as an intentional partial in the report.

## Ruling conformance
- Ruling 1 (private `_RecordedResult` Protocol stand-in for `RecordedResult`, `RecordedReader` type alias in base.py): present at base.py:33-45, structured exactly like `warehouse.py`'s `_CachedRow` precedent (not `@runtime_checkable`, consistent with that precedent). Not flagged.
- Ruling 2 (`pipelines/__init__.py` lazy `_EXPORTS` gains base.py's names): `_EXPORTS` now maps `Pipeline`, `PlanContext`, `get_pipeline` → `herness.harness.pipelines.base`, matching the spec's own §2 module-map row for `__init__.py`. Not flagged.

## OWN041 / layering
- All `herness.core.types` symbols (`Depth`, `EntityScope`, `Finding`, `RankedEntity`, `RecommendationDraft`, `ReportDraft`, `RunKind`, `TaskSpec`) are imported from the top-level `herness.core.types` package, not submodules — confirmed present in `herness/core/types/__init__.py`'s `__all__`. Compliant.
- `lint-imports`: 13/13 contracts kept (verified by direct run).
- `tools.check_type_ownership`: exit 0 (verified by direct run; base.py's local `_DqWarning`/`_Portfolio` TypedDicts live in `herness.harness.pipelines`, not `herness.core.types`, so ownership rules don't apply to them).

## Static gates (verified directly, scoped to task files)
- `mypy` on `herness/harness/pipelines/base.py` + `__init__.py`: 0 errors.
- `ruff check` + `ruff format --check` on base.py, `__init__.py`, test file: 0 issues, all formatted.
- `uv run python -m tools.check_module_size`: no violations (base.py 228/260, `__init__.py` 34/40 per report; tool run produced no findings).
- `PYTHONUTF8=1 uv run pytest tests/unit/harness/pipelines -q -p no:logging --cov=herness.harness.pipelines.base --cov-branch`: 84 passed. Coverage: **95% line** (4/79 stmts missed), **91.7% branch** (11/12) — both above the 90%/85% floor. Uncovered lines 225-228 are exactly the valid-kind body of `get_pipeline`, correctly deferred (funding_review/org_review don't exist yet).

## Test naming/docstring/pytestmark
- `pytestmark = pytest.mark.unit` set at module level in `test_pipelines_base.py`.
- All 9 test function names contain their ID with `_` (e.g. `test_ut06_48_...`, `test_ut06_49_...`, `test_ut06_53_...`); every docstring's first line starts with the matching ID. Compliant.

## ⚠️ Items
- `base.py`'s `__all__` additionally exports `RecordedReader` (and implicitly relies on the private `_RecordedResult`), which is not one of the six names in the spec's §2 module-map row for `base.py` (`Pipeline`, `PlanContext`, `default_challenge_priority`, `build_ranked_entities`, `to_recommendation_drafts`, `get_pipeline`). This is exactly what controller ruling 1 pre-approves (RecordedReader is the U06-71/72/73 signature dependency that has to live somewhere), so not counted as a defect — flagged only for visibility since it's a public-API surface beyond the literal module-map list.

## Findings

None — Critical, Important, or Minor.

## Verdict

**Approved**

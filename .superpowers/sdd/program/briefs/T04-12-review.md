# Review: T04-12 Scoring context

Commit reviewed: b23419e62e8fa0aa53dd7bcc50769e74e276f86b (eb3c9f5..b23419e), worktree
`D:\herness\.claude\worktrees\agent-a1b97ebc42c67b5df`.

## Spec compliance (U04-54)

- ✅ `StepContext`: frozen `slots=True` dataclass with `build_id: str`, `catalog: CatalogView`
  (program-ruled carve-out for absent `MetricCatalog`, T04-03), `weights: WeightsConfig`,
  `as_of: date`, `tz: str`, `disabled_metrics: frozenset[str]`. (`context.py:37-77`)
- ✅ `StepContext.binds()` returns `default_binds(catalog) ∪ weight_binds(weights) ∪
  default_window("t12w", as_of, tz, catalog.defaults.windows).binds() ∪ {s_count_metrics,
  s_unconfirmed_models, unconfirmed}`, exactly per U04-54's formula. Verified by
  `test_ut04_110_step_context_binds_is_union` (`test_metrics_context.py:146-157`), which
  independently recomputes the base union and diffs the extra keys. (`context.py:41-54`)
- ✅ `s_count_metrics`: sorted enabled (`catalog.names()` default `enabled_only=True`) metric
  names with `aggregation in ("count", "snapshot")` — both valid `Aggregation` literals
  (`settings.py:68`). Verified with a disabled `count` metric correctly excluded
  (`test_metrics_context.py:160-171`). (`context.py:55-57`)
- ✅ `s_unconfirmed_models`: sorted `UsdModel` values whose `WEIGHT_USES["lever_<model>"]`
  block set has an unconfirmed block, via `unconfirmed_blocks` — matches U04-54's closing
  paragraph exactly. All seven `lever_*` keys exist in `WEIGHT_USES`
  (`settings.py:236-242`), so `unconfirmed_blocks` never hits its `ConfigError` path here.
  (`context.py:58-59`)
- ✅ `BIND_TYPES` extended with `s_count_metrics`, `s_unconfirmed_models` as `VARCHAR[]`
  (`_binds.py:18`), left to this card by T04-04 per the brief.
- ✅ `StepResult`: frozen `slots=True` dataclass, four fields, types match U04-54.
  (`context.py:91-98`)
- ✅ `ScoringReport`: pydantic `BaseModel`, `ConfigDict(frozen=True, extra="forbid",
  strict=True)`, six fields matching U04-54 exactly. Verified frozen/forbid/strict all three
  ways (attribute set, extra field, non-strict coercion) in
  `test_ut04_110_scoring_report_is_frozen_forbid_strict`. (`context.py:101-111`)
- ✅ `disabled_metrics` is stored but intentionally unused by `binds()` — confirmed against
  the spec: it's consumed later by `run_metrics_step` (U04-58, "`sc.catalog.names()` minus
  `sc.disabled_metrics`", spec line 1342), not by `context.py`. Correct scope for an S-size
  card gated to U04-54 only.
- ✅ Module budget: `context.py` 79/80 lines; `_binds.py` 175/200 lines (module map row 64
  of the brief). No cycle introduced: `context.py` imports only `_binds`, `settings`,
  `windows` (all same-layer L3) plus stdlib/pydantic.
- ✅ Fallout in `test_metrics_render.py` (UT04-26 `BIND_TYPES` coverage assertion) correctly
  updated to subtract the two new step-bind names before comparing to `default_binds ∪
  weight_binds` output — necessary and minimal, does not touch `render.py`.

## Program ruling: base `unconfirmed` bind default

Traced every consumer of `StepContext.binds()`'s `unconfirmed` scalar in the binding spec
(`docs/impl/04-metrics-and-scoring.impl.md`):
- U04-66 (funding, line 1512): explicitly overrides `unconfirmed = bool(unconfirmed_blocks(sc.weights, WEIGHT_USES["funding"]))` before rendering.
- U04-68 (org, line 1548): explicitly overrides `unconfirmed=false`.
- U04-69/U04-70 (levers, line 1566 step 8): the SQL template computes
  `unconfirmed = list_contains(s_unconfirmed_models, model)` per row — it never reads the
  scalar `unconfirmed` bind at all, override or not.
- Portfolio (U04-54 invariants line, `WEIGHT_USES["portfolio"]`): same override pattern as
  funding is implied.

So the scalar `unconfirmed` bind from the base `StepContext.binds()` is never read
unoverridden by any currently-specified consumer — the builder's `bool(s_unconfirmed_models)`
default is inert in practice, consistent with U04-66/U04-68/U04-69. No fix needed; this is
correctly flagged as a judgment call in the report rather than a defect, and my independent
read of the spec supports the choice made.

## Verification performed

- `PYTHONUTF8=1 uv run pytest tests/unit/metrics -q -p no:logging` → 339 passed.
- `uv run mypy` → Success: no issues found in 45 source files.
- `uv run ruff check herness/metrics tests/unit/metrics` → All checks passed.
- `uv run python -m tools.check_module_size` → exit 0, no output.
- `uv run lint-imports` → Contracts: 8 kept, 0 broken (checked layering claim for the new
  cross-module imports in `context.py`).
- Test-ID convention: all 6 new tests named `test_ut04_110_*` with docstrings starting
  `"""UT04-110 ...`, module sets `pytestmark = pytest.mark.unit`. Compliant.

No concurrent-edit interference observed; `_solver.py` (T04-19) is untouched by this commit.

## Issues

### Critical (Must Fix)
None.

### Important (Should Fix)
None.

### Minor (Nice to Have)
- `context.py:53-65` (`_step_binds`): the base `unconfirmed` default's rationale (why
  `bool(s_unconfirmed_models)` is safe as a placeholder — i.e., that every current consumer
  overrides or ignores it) is explained in the builder's report but not in a code comment.
  A one-line comment would save the next reader (T04-13+) the trace-through done above.

Verdict: Approved

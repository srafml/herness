# Report for T06-10: Pipeline base

Status: in progress (report written before final commit per process; updated after).

## Files

- `herness/harness/pipelines/base.py` (new): `Pipeline` protocol (U06-66), `PlanContext`
  (U06-67), `default_challenge_priority` (U06-68), `build_ranked_entities` (U06-69),
  `to_recommendation_drafts` (U06-70), `get_pipeline` factory (U06-75), plus the private
  `_RecordedResult` Protocol stand-in for `RecordedResult` (U05-35, ruling 1) and the
  `RecordedReader` type alias.
- `herness/harness/pipelines/__init__.py` (edited): `_EXPORTS` now maps `Pipeline`,
  `PlanContext`, `get_pipeline` to `herness.harness.pipelines.base` (ruling 2); docstring
  updated to say T06-10 owns these three and a later card adds `ChatService`. This matches
  the spec's §2 module-map row for `__init__.py`, which lists exactly those four names.
- `tests/unit/harness/pipelines/test_pipelines_base.py` (new): UT06-48, UT06-49, plus a
  partial UT06-53 (only the unknown-kind `ConfigError` path of `get_pipeline`, since
  `funding_review`/`org_review` land in later cards T06-71/73).

## Spec readings

- `docs/impl/06-swarm-and-pipelines.impl.md` card at line 2428 (T06-10 row) and unit specs
  U06-66 … U06-70, U06-75 (lines ~904-1005).
- §3.7 "Pipelines" preamble (line 900-902) for the `RecordedReader` definition and the
  DuckDB SQL conventions used by later pipeline cards.
- §2 module map row for `herness/harness/pipelines/base.py` (260-line budget) and for
  `herness/harness/pipelines/__init__.py` (re-export list `Pipeline`, `PlanContext`,
  `get_pipeline`, `ChatService`; 40-line budget).
- `herness/harness/warehouse.py` `_CachedRow` Protocol (lines 41-48) as the precedent pattern
  for the private `RecordedResult` stand-in.
- `herness/core/types/swarm/drafts.py` for `ReportDraft`, `RecommendationItem`, `RankedEntity`
  shapes and the private-`TypedDict` + `with_config(extra="forbid")` pattern reused for
  `_DqWarning`/`_Portfolio`.
- `herness/core/types/memory.py` for `RecommendationDraft`'s exact field set (rank, kind,
  target_type, target_id, summary, numbers, expected_metric, expected_delta_ref,
  expected_usd_ref, finding_ids).
- `herness/harness/pipelines/settings.py` for `DepthKnobs`/`resolve_knobs` (used to build a
  real `DepthKnobs` in `PlanContext` tests without hand-writing every knob).
- `herness/harness/findings.py` for `impact_usd` (used, unmodified) and its Finding-building
  test-fixture style, reused for this card's local fixtures.
- `pyproject.toml` `[tool.importlinter]` contracts (herness layers, settings-modules-are-leaves)
  confirmed `herness.harness.pipelines.base` importing `herness.core.*` and
  `herness.harness.findings`/`herness.harness.pipelines.settings` is allowed.

## Design notes / decisions

- `get_pipeline` resolves the two review kinds through a `_PIPELINE_MODULES` dict lookup
  (`.get(kind)`) rather than an `if/elif` chain on the `Literal` value, to avoid mypy
  `warn_unreachable` flagging the trailing `ConfigError` raise as statically unreachable
  once both literal arms return. The dict lookup gives the exact same runtime behaviour
  (`ConfigError("no review pipeline for kind <kind>")` for `"chat"` or anything else) while
  staying reachable to the type checker, and the import stays lazy (only inside the
  non-`None` branch, never imported at module load).
- `to_recommendation_drafts` builds `RecommendationDraft` via `.model_validate({...})` rather
  than the keyword constructor: `RecommendationItem.target_type` is a free `str` (spec 06),
  while `RecommendationDraft.target_type` is `Literal["service","team","org","work_item"]`
  (spec 07). `model_validate` lets pydantic validate the literal at the boundary (raising its
  own `ValidationError` for a genuinely bad value, same as the keyword form would if mypy
  allowed it) without a false-positive mypy `arg-type` error from the static `str`/`Literal`
  mismatch.
- `PlanContext.dq_warnings` and `.portfolio` use private `TypedDict`s (`_DqWarning`,
  `_Portfolio`) with `@with_config(extra="forbid")`, matching the `_ActionLever`/`_Removed`/
  `_DeadTask` pattern in `herness/core/types/swarm/drafts.py`, rather than a bare
  `dict[str, JsonValue]`, since the spec names their exact keys.
- `Pipeline` is `@runtime_checkable`, matching every other pluggable-edge Protocol in the
  codebase (`Tool`, `AsyncTool`, `BudgetLedger`, `TraceEmitter`, `WarehouseHandle`); `kind` is
  a plain class-level annotation (not a `@property`) per the spec's own `kind: RunKind`
  phrasing, which a conforming class or a `@property` can both satisfy structurally.
- `_RecordedResult` (private) is not `@runtime_checkable`, matching the `_CachedRow` precedent
  in `warehouse.py`, since it is a typing-only stand-in for a future concrete type, not a
  pluggable edge resolved by isinstance checks.

## Test summary

`PYTHONUTF8=1 uv run pytest tests/unit/harness/pipelines -q -p no:logging` → 84 passed
(includes this card's 9 new tests plus the pre-existing `test_chat_support.py` and
`test_pipelines_settings.py` suites, unaffected).

New tests (`tests/unit/harness/pipelines/test_pipelines_base.py`, 9 tests):
- `test_ut06_48_fake_pipeline_satisfies_protocol` — structural conformance to `Pipeline`.
- `test_ut06_48_plan_context_forbids_unknown_field_and_is_frozen` — U06-67 `extra="forbid"`,
  `frozen=True`.
- `test_ut06_48_default_challenge_priority_log_formula` / `..._zero_without_usd` — U06-68
  formula, including the no-usd-number case.
- `test_ut06_48_build_ranked_entities_recommendation_order_then_must_cover_by_rank` /
  `..._no_recommendations` — U06-69 ordering (first-appearance recs, then must-cover by
  rank then id).
- `test_ut06_49_unverified_finding_raises_report_contract_error` — missing and
  non-`verified`-status findings both raise `ReportContractError` naming the finding id.
- `test_ut06_49_valid_draft_maps_to_spec_07_shape` — full field-by-field check of the mapped
  `RecommendationDraft`, including that `summary` keeps its `[[n2]]` marker unresolved.
- `test_ut06_53_get_pipeline_unknown_kind_raises_config_error` — partial UT06-53 coverage:
  `"chat"` and an arbitrary bogus kind both raise `ConfigError` with the exact message. The
  valid-kind branches (`funding_review`/`org_review`, which lazily import modules T06-71/73
  have not created yet) are intentionally not exercised here.

Coverage of `herness/harness/pipelines/base.py` (`--cov-branch`): 94.9% line (75/79
statements), 91.7% branch (11/12 branches) — both above the 90%/85% floor. The only uncovered
lines (225-228) are the valid-kind body of `get_pipeline` (module lookup, import, construct,
return), which needs `FundingReviewPipeline`/`OrgReviewPipeline` from later cards.

Static gates run (all pass): `ruff format`, `ruff check --fix`, `mypy` (on
`herness/harness/pipelines/base.py` and `herness/harness/pipelines/__init__.py`; the test
file is outside `[tool.mypy] files`), `lint-imports` (all 13 contracts kept), `python -m
tools.check_module_size` (base.py 228/260 lines; `__init__.py` 34/40 lines — no violations
reported for either).

## Concerns

- None blocking. `T06-71`/`T06-73` (funding_review/org_review) will need `get_pipeline`'s
  happy-path branches covered once those modules exist (their own UT06-53 test).
- Did not touch `herness/harness/budget.py` or `herness/harness/gates.py` (T06-04, under
  concurrent review) per instructions.

## Final status

Status: DONE.

Final commit: `6e34a6b` — `feat(pipelines): Pipeline protocol, PlanContext and shared helpers (T06-10)`.
Checkpoint commit: `30cc76e` — `wip(T06-10): Pipeline protocol, PlanContext and shared helpers`.

The final commit's real pre-commit hooks all passed (no `--no-verify`, no
`PRE_COMMIT_ALLOW_NO_CONFIG`): check-merge-conflicts, end-of-file-fixer, trailing-whitespace,
mixed-line-ending, detect-private-key, check-added-large-files, ruff-check, ruff-format,
mypy, import-linter, detect-secrets, module-size, pytest-unit (full project unit suite) —
all Passed.

Line counts: `herness/harness/pipelines/base.py` 228/260 budget lines;
`herness/harness/pipelines/__init__.py` 34/40 budget lines.

Coverage of base.py: 94.9% line, 91.7% branch (see Test summary above); both above the
90%/85% floor. Uncovered lines are the valid-kind body of `get_pipeline`, which needs
`FundingReviewPipeline`/`OrgReviewPipeline` from T06-71/T06-73.

Did not touch herness/harness/budget.py or herness/harness/gates.py (T06-04, concurrently
under review).

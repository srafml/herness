# T06-03 report — Pipelines configuration

Status: DONE_WITH_CONCERNS
Commit: e546005 feat(harness): add pipelines configuration (T06-03)
Worktree: D:\herness\.claude\worktrees\agent-a5564c121bc7aa6dc

## What was built
- `herness/harness/pipelines/settings.py` (U06-22..24): `PipelinesConfig` (version Literal[1], swarm, depth, pipelines, hybrid) with every key/type/default/bound of impl 06 §9; sub-models `SwarmSettings` (+ `DedupSettings`, `CoverageSettings`, `SkepticSettings`, `CrosscheckSettings`), `BudgetSettings` (max_steps/max_tokens/wall_clock_s >= 1, `to_task_budget()` -> `TaskBudget` with max_cost_usd 0), `KSamples` (1-9, on_reject >= default), `DepthConfig` (k_samples field validator: int n -> (n,n), `{reject: k}` -> (1,k), anything else incl. bool rejected), `DepthKnobs` (non-strict, frozen, extra=forbid; U06-23 fields/defaults), `FundingPipelineConfig`, `OrgPipelineConfig` (org_specialties non-empty per depth), `ChatPipelineConfig` (escalation keys validated with the same U06-24 allowlist), `PipelineSections`, `HybridConfig` (max_cost_usd_per_run Decimal, `strict=False` so YAML int 15 is accepted), `ReviewKind` alias, `resolve_knobs`.
  All config sub-models: extra=forbid, frozen, strict. `depth` and every per-depth map must have exactly fast/standard/deep. In-code defaults equal the shipped file (`PipelinesConfig() == load(shipped)` is asserted).
  Imports: stdlib, pydantic, `herness.core.types` (package root, per check_type_ownership OWN041), `herness.core.errors`.
- `config/pipelines.yaml`: verbatim design 06 §7 block plus a 3-line header comment.
- `herness/harness/pipelines/__init__.py` (30 lines): PEP 562 `__getattr__` + `__dir__` over an empty `_EXPORTS` map (no owner modules exist yet); imports nothing eagerly (D06-34). Later cards add Pipeline/PlanContext/get_pipeline/ChatService entries.
- `pyproject.toml`: `herness.harness.pipelines.settings` added to the sources of "settings modules are leaves" (UT00-58 passes).
- Tests: `tests/unit/harness/pipelines/test_pipelines_settings.py` (UT06-11, UT06-12, UT06-13, ST06-15; 45 test cases). UT06-11 loads the shipped YAML with yaml.safe_load + model_validate; also checks unknown keys report their loc path, bound violations, missing depth keys, lazy package, and (subprocess) that importing the settings module loads no other herness.harness module.

## Decisions / deviations
1. resolve_knobs step 4: a pydantic `ValidationError` from `DepthKnobs` (e.g. override `max_tasks_per_run: 0`, or config budget below TaskBudget bounds) is re-raised as `ConfigError("budget_override gives invalid knobs: <field paths>")` so callers only see HernessError. Spec step 4 says only "validate"; flagging for review.
2. Per-depth maps in pipelines.* must contain all three depths (spec is silent; a missing depth would otherwise silently default K_* to 0 or fail late).
3. `pipelines.chat.escalation` is validated at load with the U06-24 allowlist ("keys per U06-24"); a bad key raises ValueError inside validation -> ValidationError, per U06-22 Errors.
4. `pipelines.chat.budget` is a `BudgetSettings` (§9 bounds >= 1), not a `TaskBudget`; ChatService can call `.to_task_budget()`.
5. `herness config validate` acceptance check and HernessConfig field deferred to T10-03 (program ruling).

## RED / GREEN
- RED: `uv run pytest tests/unit/harness/pipelines` -> `ModuleNotFoundError: No module named 'herness.harness.pipelines'` (collection error).
- GREEN: `pytest -k "UT06_11 or UT06_12 or UT06_13 or ST06_15 or UT00_58"` -> 46 passed.
- Coverage (--cov-branch): pipelines/__init__.py 100 %, settings.py 100 % line and branch (173 stmts, 24 branches).
- Full `pytest -m "(unit or integration) and not slow"`: 1270 passed, 1 failed — IT00-02 `test_it00_02_check_scripts_pass_on_repo`, solely because of the module budget below.

## Gates
ruff format/check clean; mypy: no issues (60 files); lint-imports: 10 kept, 0 broken; check_type_ownership: exit 0; check_module_size: exit 1 — `herness/harness/pipelines/settings.py:0: MS001 323 lines > budget 260`.

## Line counts vs budgets
- settings.py: 323 lines vs budget 260 (ENG hard limit 400) -> OVER by 63.
- __init__.py: 30 vs 40.

## Concerns
- MODULE BUDGET (needs ruling): settings.py is 323 lines vs 260. Contributors: the §9 surface itself (13 public models, each needing a docstring), the in-code defaults for the per-depth rows (~30 lines: `_DEPTH_DEFAULTS`, `_by_depth`), and ruff-format layout. Already compacted (shared `_DepthFields` base for DepthConfig/DepthKnobs, allowlist helper shared by load-time and resolve-time checks, compact fmt:skip tables). Dropping the per-depth code defaults would only reach ~290, still over, and would lose §9 "Default" parity. Proposed ruling: raise the impl 06 §2 budget for settings.py to 330. Until then IT00-02 (check_module_size) fails on this branch.
- Deviation 1 (ValidationError -> ConfigError in resolve_knobs) for reviewer confirmation.

## Fix round 1 (review T06-03-review.md)
Commit: 7654d99 fix(harness): bound pipeline budgets and cost cap at load (T06-03)

- Important-1: `BudgetSettings` now uses the `TaskBudget` bounds (max_steps 1-200, max_tokens >= 1000, wall_clock_s 1-86400). `depth.<d>.analyst_budget` and `pipelines.chat.budget` values that TaskBudget would reject now fail at load with a ValidationError that carries the key path. `to_task_budget()` cannot fail on a loaded config any more, so it stays outside the try. New UT06-11 tests: `test_ut06_11_budget_below_task_budget_bounds_rejected_at_load` (max_tokens 500 -> loc depth.standard.analyst_budget.max_tokens), plus three parametrized bound cases (max_steps 201, wall_clock_s 86401, chat budget max_tokens 500).
- Minor-2: the resolve_knobs error message is now `invalid <kind> <depth> knobs: <paths>` and no longer mentions budget_override. The docstring was updated to match.
- Minor-3: `hybrid.max_cost_usd_per_run` is strict again, with a BeforeValidator `_usd` that accepts int, float or Decimal only (bool and str are rejected) and converts via `Decimal(str(v))`. §9 says nothing about precision, so no decimal_places bound was added. Tests: "15.123456" and True are rejected; `test_ut06_11_cost_cap_accepts_numbers` checks int, float and Decimal.
- Minor-1 (mutable dict/list inside frozen models): parked. Converting to Mapping or tuple types would change the spec-declared field types (`dict[Depth, ...]`, `list[Specialty]`) and would cost more than a few lines against the 330 cap.

Gates: ruff format and check clean; mypy has no issues; lint-imports 10 kept; check_type_ownership exits 0.
Tests: targeted run (UT06-11/12/13, ST06-15, UT00-58) 53 passed; coverage of settings.py and `__init__.py` is 100 % line and branch.
Full suite: 1277 passed, 1 failed. The failure is IT00-02: MS001 330 > 260, still pending the user's budget ruling.
settings.py: 330 lines, at the pending ruled cap.

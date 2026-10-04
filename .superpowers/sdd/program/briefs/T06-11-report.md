# T06-11 report: Funding pipeline (U06-71, U06-72; UT06-50, UT06-51)

Worktree: D:\herness\.claude\worktrees\agent-aa59b05d0b0c0d39b (branch worktree-agent-aa59b05d0b0c0d39b, base 88a7f75)
Commits: 317fde0 (wip shared helpers + R2), c158e9d (wip pipeline + tests), 29e00c3 feat(harness): T06-11 funding review pipeline

## What was built
- `herness/harness/pipelines/funding_review.py` (new): `FundingReviewPipeline` (`kind = "funding_review"`).
  - `deterministic_tasks` (U06-72): the 4 reads exactly as specified (candidates, cluster fixes,
    incident presence in standard/deep with `window_end - window_days`, change share in deep), all
    through the `RecordedReader`; `?` written as named params `$k`, `$focus_ids`, `$ids`, `$since`.
    Tasks in spec order (delivery [+ ops], cluster ops [+ change when share > 0.20], retrospective),
    exact objective templates, U06-72 step 7 fields (query ids = score row `query_ids` + selection
    query id, `candidate_ids = [id]`, DQ matches per group tables, prior-rec notes cut to 1,500).
  - `must_cover`, `planner_input`, `writer_input` (funding outline), `challenge_priority` (U06-68),
    `ranked_entities` (U06-69, "candidate"), `recommendation_drafts` (U06-70).
- R1 applied: `herness/harness/pipelines/_review_common.py` (new, private) holds the shared helpers
  (`read_rows`, `str_list`, `unique`, `retro_run_ids`, `prior_lines`, `dq_matches`,
  `challenge_summary`, `finding_row`, `task_inputs`, `analyst_task`, `retro_task`, `planner_input`,
  `writer_input`, and the stand-ins `base_priority` `# T06-15` / `default_tools` `# T06-13`, the latter
  now with the full U06-101 signature). `org_review.py` imports them; org behaviour unchanged (all
  UT06-52/UT06-53 tests green). impl 06 §2 module map row added in the same commit (L4, deps none,
  budget 260). No import-linter contract lists pipelines modules (UT00-58 green, lint-imports 13 kept).
- R2 applied: `base.RecordedReader = Callable[[str, dict[str, JsonValue]], RecordedResult]`
  (`herness.harness.tools.RecordedResult`); the private Protocol stand-in is gone. The org test's
  `_Reader` now returns a real `RecordedResult`.

## Line counts vs budgets
| File | Lines | Budget |
|------|------:|-------:|
| herness/harness/pipelines/funding_review.py | 234 | 360 |
| herness/harness/pipelines/_review_common.py | 208 | 260 (new row) |
| herness/harness/pipelines/org_review.py | 183 | 330 |
| herness/harness/pipelines/base.py | 216 | 260 |
`python -m tools.check_module_size` exit 0.

## Coverage (pytest tests/unit/harness/pipelines, --cov-branch)
funding_review.py 100 % line / 100 % branch; _review_common.py 100 % / 100 %; org_review.py 100 % / 100 %.

## Tests
`tests/unit/harness/pipelines/test_pipelines_funding_review.py` (17 tests, UT06-50 x 12 incl. 3
parametrised depths, UT06-51 x 4). Tiny build (R3): local tmp `wh-<build_id>.duckdb` with
`score.funding`, `score.funding_attribution`, `core.incident`, `enrich.incident_change_link`; the reader
is `tools.execute_recorded` bound to a real `ToolContext` (`tools_standin.make_ctx` over
`open_warehouse`, FakeOps), so query ids/evidence rows/evidence uses come from the real recording path.
Covered: fast/standard/deep counts and order (15/20/21 tasks, 2/3/4 reads), specialties, must-cover,
priorities (149/94/135/84), focus ignores K (and a non-candidate focus selects nothing), exact SQL and
params of the 4 reads, no other I/O (AST import/open check), QueryError propagation (missing
change-link table), retrospective, notes, DQ matching; planner_input/writer_input keys and outline,
ranked_entities from cached ranks, get_pipeline, default_tools stand-in scope.
Run: `uv run pytest tests/unit/harness/pipelines tests/unit/repo/test_import_contracts.py -q` -> 122 passed.
RED: before the module existed the new file failed collection (ModuleNotFoundError funding_review).
Gates: ruff format/check clean, mypy (pipelines) clean, lint-imports 13 kept, check_module_size 0,
pre-commit hooks (incl. pytest-unit) passed on every commit.

## Spec readings / deviations
- SN1 `must_cover` ranks cache: `ctx.portfolio["selected"]` ids get their rank from the two selection
  reads when present; a selected id outside the selection (e.g. beyond `K_candidates`, or unknown)
  is in the must-cover set but not in the ranked cache (no 5th query; spec says 4 queries).
- SN2 Focus: "focus ids of type candidate" read literally — a focus whose `entity_type` is not
  `candidate` filters with an empty id list, so nothing is selected (and steps 3-4 are skipped).
- SN3 Focus applies to both selections (step 2 is "same"), so cluster fixes are filtered too.
- SN4 Steps 3/4 are skipped when their id list is empty (no candidate / no cluster selected).
- SN5 `$since` is passed as an ISO date string and compared as `CAST($since AS DATE)`
  (execute_recorded params must be JSON values).
- SN6 Ops / change tasks cite the score row's `query_ids` + selection query id only (as specified);
  the incident / share query ids are recorded as evidence uses of the planner task but not added.
- SN7 Retrospective DQ matching uses the candidate group tables (spec silent; org uses its tables).
- SN8 `inputs.query_ids` order: score row ids first, then the selection query id.
- SN9 `default_tools` stand-in has the full U06-101 signature but implements the non-crosscheck
  analyst only (raises ValueError otherwise); T06-13 replaces it.
- SN10 Module map lists `herness.metrics.portfolio` as an extra import of funding_review.py; nothing
  in U06-71/72 needs it, so it is not imported.

## Carry-overs / concerns
- C1 (cross-spec): spec 07 `RecommendationDraft.target_type` is `service|team|org|work_item`; a
  funding draft recommendation with `target_type="candidate"` fails U06-70 validation. The UT06-51
  draft uses `work_item`. Whoever owns the writer/ReportDraft contract should decide the funding
  target type (candidate ids vs work_item/cluster).
- C2 `_base_priority`/`_default_tools` stand-ins (now `_review_common.base_priority/default_tools`)
  to be replaced by T06-15 / T06-13.
- C3 Switch the local tiny builds to spec 11 `tiny_build` when T11-17 lands.

## Fix round 1
- Minor 1: test `_Reader.__call__` / `calls` in test_pipelines_org_review.py now take
  `dict[str, JsonValue]` (matches the retyped `RecordedReader`). Also fixed the other mypy findings
  in both test files: `checks: list[dict[str, Any]]` in `_challenge`, and `findings: Any` instead of
  `# type: ignore[misc]` on the writer-input unpack. `uv run mypy` on both test files + pipelines: 0 errors.
- Minor 3: `_review_common.dq_matches` cut to 100 now carries a comment citing the
  `TaskInputs.dq_warnings` limit (U06-03); behaviour unchanged.
- Checks: `uv run pytest tests/unit/harness/pipelines -q` 121 passed; ruff clean; check_module_size 0.

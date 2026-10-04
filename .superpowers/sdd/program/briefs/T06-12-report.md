# T06-12 report: Org pipeline (U06-73, U06-74)

Status: DONE_WITH_CONCERNS (spec notes only; all gates green)
Commit: a951daf feat(pipelines): add org review pipeline (T06-12) — all pre-commit hooks passed (incl. module-size, pytest-unit)
Worktree/branch: agent-a6f90285b80e9f10c / worktree-agent-a6f90285b80e9f10c (base c3eee74)

## Files
- herness/harness/pipelines/org_review.py (new) — 327 / 330 lines (L4). `OrgReviewPipeline` + module-level "as funding" helpers.
- tests/unit/harness/pipelines/test_pipelines_org_review.py (new) — UT06-52 (11 functions incl. parametrized depth), UT06-53 (6 functions).
- No change to base.py, settings.py, pipelines/__init__.py (the lazy `_EXPORTS` has no FundingReviewPipeline either, so exporting pipeline classes is not the established pattern).

## RED / GREEN
- RED: `pytest tests/unit/harness/pipelines/test_pipelines_org_review.py` -> ModuleNotFoundError: herness.harness.pipelines.org_review (collection error).
- GREEN: `PYTHONUTF8=1 pytest tests/unit/harness/pipelines -q -p no:logging --cov=herness.harness.pipelines.org_review --cov-branch` -> 103 passed; org_review.py 100% line, 100% branch (147 stmts, 26 branches).
- ruff format/check clean, mypy --strict clean on org_review.py, lint-imports 13 kept / 0 broken, check_module_size exit 0, check_type_ownership clean. Full suite not run (dispatch: no full-suite runs); pre-commit pytest-unit hook ran on commit.

## Controller rulings applied
1. No funding_review.py. "As funding" behaviours implemented as module-level helpers (`_read`, `_str_list`, `_unique`, `_retro_run_ids`, `_prior_lines`, `_dq_matches`, `_challenge_summary`, `_finding_row`) under a divider saying T06-11 can lift them; planner_input/writer_input dict shapes per U06-71; challenge_priority = default_challenge_priority; recommendation_drafts = to_recommendation_drafts.
2. `_base_priority(rank, must_cover)` (exact U06-91 formula, `# T06-15: replace with herness.harness.swarm.planner.base_priority`) and `_default_tools(*, child_depth, knobs)` (sorted U06-101 analyst list minus request_subtask when child_depth >= knobs.max_spawn_depth, `# T06-13: replace with herness.harness.swarm.routing.default_tools`). No herness/harness/swarm created.
3. UT06-52 uses a tmp DuckDB (score.org, score.action_lever with VARCHAR[] query_ids, core.team) and `_Reader` implementing RecordedReader with a distinct query_id per call (`q_%016x` of the call index); module docstring notes the switch to tiny_build when T11-17 lands.
4. UT06-53: new-file tests cover get_pipeline("org_review") -> OrgReviewPipeline (isinstance Pipeline; the previously uncovered valid-kind branch) and writer_input levers rows keeping their query_id; the existing "chat"/unknown -> ConfigError test in test_pipelines_base.py stays as is.

## Spec notes (readings chosen; please confirm)
- SN1 Named parameters: U05-35 `execute_recorded` takes `params: dict` referenced as `$<key>`, so the U06-74/U06-73 `?` placeholders are written `$ids`, `$k` (K_teams), `$focus_ids`. SQL text is otherwise verbatim.
- SN2 Focus: "the limit is dropped and ids are filtered" implemented as `AND entity_id IN (SELECT unnest($focus_ids))` with `focus.entity_ids` regardless of focus.entity_type (a non-team focus therefore selects no teams).
- SN3 Rollup query: adds `list(team_id ORDER BY team_id) AS team_ids` to the U06-74 step-3 SELECT, because "rollup priority = base_priority(min rank of its selected teams, False)" needs each org's selected teams and the verbatim query returns only org_id, n.
- SN4 must_cover: the top M_must teams are taken from the (cached) K_teams selection, not a separate query, so every must-cover team has a task (coverage rule). If M_must > K_teams, must-cover is capped at the selection. Team read happens once per pipeline instance (per run).
- SN5 inputs.query_ids for team tasks: [teams selection qid] + (when the team has levers) [lever read qid] + each lever row's `query_ids` (top 3 by delta_usd DESC, metric), deduped in order. The team query has no query_ids column, so the "score row's query_ids" of U06-72 has no org equivalent. Rollup: [rollup qid, teams qid]. Retrospective: [].
- SN6 Notes: team tasks start with "Top action levers: m1, m2, m3." (omitted when the team has no levers), then prior-rec lines for rows with target_id == team id; rollup: prior-rec lines for target_id == org id; retrospective: prior-rec lines for rows whose run_id is among its run ids (U06-72 "same target_id" matches nothing for scope run). Cut to 1,500 chars; empty -> None.
- SN7 DQ matching: `json.dumps(details)` substring match against the task's entity ids plus the five org DQ tables (also used for the retrospective task, which U06-72 gives no table list); capped at 100 names (TaskInputs max).
- SN8 challenge_summary = `{"verdict": <last challenge verdict>, "notes": [note of each concern/fail check of the last challenge]}`, None without challenges.
- SN9 planner_input score_row: team -> {"entity_id", "rank"}; org rollup -> {"org_id", "n", "team_ids"}; retrospective/other -> None. writer_input keeps `portfolio` (as funding) although the org outline has no portfolio section.
- SN10 Scope period_start/end left None (spec gives no period for org tasks); `window_end` is stored (public attribute) but unused by org (it is used by funding's incident window).
- SN11 Levers are sorted per team in Python by (delta_usd DESC, metric) since the QUALIFY query has no outer ORDER BY.

## Concerns
- 327/330 lines: little headroom; if T06-11 lifts the helpers into base.py (228/260 now) org_review.py shrinks by ~60 lines.
- Checkpoint `wip(T06-12)` commit was blocked once by the module-size hook (347 > 330) before trimming; no wip commit landed, the card is one commit.

## Fix round 1 (commit 22d6693 fix(pipelines): address T06-12 review round 1 (T06-12))
- Minor 1: `_dq_matches` now uses `json.dumps(details, ensure_ascii=False)`, so non-ASCII ids/table names match. New test `test_ut06_52_dq_warnings_match_non_ascii_team_id` (stub reader, team id "équipe-ü").
- Minor 3: `self.window_end` carries a comment: kept for the U06-73 signature; org reads no window.
- Minor 2 (lever tie-break): not changed (parked by controller).
- Gates: pytest tests/unit/harness/pipelines -> 104 passed; org_review.py 100% line / 100% branch; ruff, format, mypy clean; check_module_size exit 0; all pre-commit hooks passed.
- org_review.py: 327 / 330 lines (unchanged).

# T04-18 report — Action levers (`score.action_lever`)

Worktree: D:\herness\.claude\worktrees\agent-a73d386da299b6d89 (branch worktree-agent-a73d386da299b6d89, base de4f9ca)
Commits: 334cde5 wip(T04-18): levers query, run_levers_step and tests; bf07142 feat(metrics): add action levers (T04-18) (empty marker commit, see below).

## Files
| File | Lines | Budget |
|---|---|---|
| herness/metrics/sql/levers.sql.j2 (new, U04-69) | 176 | 200 |
| herness/metrics/levers.py (new, U04-70/71) | 56 | 130 |
| herness/metrics/_catalog_checks.py (marker comment only) | 129 | unchanged |
| tests/unit/metrics/test_metrics_levers.py (new) | ~460 | — |

scoring.py, render.py, _macros.sql.j2, _binds.py, shared fixtures/oracles: untouched. STEPS["levers"] stays None with its `# T04-21:` marker; `run_levers_step(con, sc, /) -> StepResult` matches StepFn.

## Design
- One recorded SELECT (run_recorded, producer "score", IntoSpec("score.action_lever","replace","query_ids",(org_qid,))). Org qid = `SELECT min(query_ids[1]) FROM score.org`; empty score.org -> no upstream id.
- Missing score.org -> SchemaViolation("score.org missing; run step org first"); QueryError -> SchemaViolation("levers failed: ...") (mirrors run_org_step).
- Reused macros: lkp, observed_days, owner_team (event owner team). New local Jinja macros only inside levers.sql.j2 (in_window, by_entity); nothing added to _macros.
- Entity scope: team = team_id (owner team of the service for events); org = metrics.org_closure ancestors of the record org (service org for events). Window [as_of_ts - INTERVAL 12 MONTH, as_of_ts).
- Money sums stay DECIMAL (exact, deterministic), cast to DOUBLE only for delta math; avg = sum/count.
- Unknown model -> CASE yields NULL -> no row (TH04-10: only the 7 USD_MODELS). rationale_template = lkp(s_templates) only; template_params keys exactly LEVER_PLACEHOLDERS.
- LEVER_PLACEHOLDERS: single source in _catalog_checks, re-exported by levers.py (levers -> context -> catalog -> _catalog_checks, so the reverse import would cycle). USD_MODELS per U04-71.
- Rendered SQL length: 8,399 chars (cap 20,000).

## Tests (22, all green) — tests/unit/metrics/test_metrics_levers.py
Hand-computed entity (real stage-400 facts via tests.support.metrics_funding.warehouse): team T1 "Payments"/org O1 "Ops", service S1 crit 1.
N=3 (I1, I2, I3; 60 impact min P1 each -> downtime 10000 each), Σdowntime 30000, Σtoil 300 (I2: 2 business h x 1.5 x 100), avg total 10100, avg toil 100; canceled incident and another team's incident ignored.
D=3 (C1 failed via linked I3, C2 ok, C3 backed_out; C4 canceled, C5 out of window), F=2, CC=10000 -> CC/F=5000. E=4 (major events in window; info and out-of-window ignored).
observed_days = 2026-01-18..2026-04-01 = 73 -> factor 365/73 = 5.
mttr x=10 (median 6, quartile 4 of 10,8,6,4,2); rates x=0.5 (median 0.25, quartile 0.125 of 0.5,0.375,0.25,0.125,0.0625).
| ID | model | peer_median | top_quartile |
|---|---|---|---|
| UT04-92 | mttr: 30300 x (x-t)/x x 5 | 30300x0.4x5 = 60600.00 | 30300x0.6x5 = 90900.00 |
| UT04-93 | repeat: 3 x (x-t) x 10100 x 5 | 37875.00 | 56812.50 |
| UT04-94 | reopen: 3 x (x-t) x 100 x 0.5 x 5 | 187.50 | 281.25 |
| UT04-95 | reassign: 3 x (x-t) x 0.5 x 100 x 5 | 187.50 | 281.25 |
| UT04-96 | sla (penalty 1000): 3 x (x-t) x 1000 x 5 | 3750.00 | 5625.00 |
| UT04-97 | cfr: 3 x (x-t) x (400 + 5000) x 5 | 20250.00 | 30375.00 |
| UT04-98 | noise: 4 x (x-t) x 3/60 x 100 x 5 | 25.00 | 37.50 |
Extra: UT04-92 org entity via closure (60600.00, entity_name "Ops", lone org has no improving quartile); UT04-92 x=0 skipped; UT04-97 F=0 -> backout only (2x0.25x400x5 = 1000.00 / 1500.00).
UT04-99: non-improving targets / z<=0 / NULL value skipped; shipped sla penalty 0 skips sla; rank > top_entities skipped; TH04-10 metrics outside s_lever_models_k (cycle_time_days, unplanned_work_ratio, made_up_metric) give no row; unconfirmed per model (confirming toil + cost_per_engineer_hour clears reassign/noise only); missing score.org; query error; query_ids [own, org qid] + one evidence row, SQL < 20000; empty score.org output types; rendered bind set.
UT04-100: all 14 rows (7 models x 2 targets): keys == LEVER_PLACEHOLDERS, values from query/config, template formats; USD_MODELS / LEVER_PLACEHOLDERS identity with validator.

RED evidence: with levers.py absent, `pytest tests/unit/metrics/test_metrics_levers.py` -> `ModuleNotFoundError: No module named 'herness.metrics.levers'` (collection error). First GREEN run also caught a test-design error in UT04-99 (fixed in the test, not the code).
GREEN: test_metrics_levers.py 22 passed; `pytest tests/unit/metrics` 718 passed. Coverage levers.py 100% line / 100% branch.
Gates: ruff format/check clean, mypy clean (344 files), lint-imports 13 kept, check_module_size exit 0; commit hooks passed (no skip).

## Deviations / spec notes
1. CC: Σ total_usd of distinct non-excluded incidents linked (core.incident.caused_by_change_id ∪ enrich.incident_change_link score >= d_change_link_min_score) to the entity's failed changes with actual_end in the window; the incidents themselves are not separately window-filtered by opened_at (spec text ambiguous).
2. cfr uses the impl formula CC/F (design §5.9 says "avg total_usd of change-caused incidents per failed change" — same meaning).
3. top_quartile grouped by (entity_type, peer_group, metric); peer_group strings are type-prefixed so equivalent to the spec.
4. Higher-is-better handled generically (0.75 quantile, target > x) though validator step 7 makes all lever metrics lower-is-better.
5. Events: all noise-severity events in the window (no incident_id IS NULL filter, unlike funding), per U04-69 text.
6. Final commit is an empty marker commit (all content landed in the wip checkpoint; no amend).

## Fix round 1 (review T04-18-review.md)
- M-1 (product): levers.sql.j2 cc_e now also requires `f.opened_at < as_of_ts` (CC still defined by the entity's failed changes in the window; incidents opened at/after as_of are cut). levers.sql.j2 stays 176 lines.
- I-1: rank and z rules now run on T1 (which has facts): rank 2 with top_entities 1 -> 0 rows, top_entities 2 -> 1 row; z in {0, -0.5} -> 0 rows, control z 0.1 -> 1 row (control uses other binds so it is a new query, not a nondeterministic rerun).
- M-2 fixture: I8 canceled now in window (2026-02-15) and caused by C3, with money forced on its fact row (excluded facts carry NULL money, which would hide a missing exclusion); I7 opened 2026-04-05 (after as_of) caused by C3; new successful change C6 -> D = 4 (N = 3). New test: C1 not failed -> F 1, CC 0 -> 2000.00 / 3000.00.
  Updated hand values: cfr D 4 x (x-t) x 5400 x 5 = 27000.00 / 40500.00; F=0 test D 3 x (x-t) x 400 x 5 = 1500.00 / 2250.00; UT04-100 cfr n_basis 4. Other models unchanged (N, sums, E, observed_days 73 unchanged).
- M-3: org O1 now has parent O0 "Group"; the org test uses O0 (2-level closure), 60600.00, entity_name "Group".
- M-4: new UT04-100 test removes mttr_hours from s_metric_units_k/v (patched StepContext.binds) -> unit 'other'.
- Mutants re-run (test file minus the bind-set test), each restored byte-exact: M06, M08, M34, M12, M13, M14, M17, M20, M26 and M-1 (drop the as_of bound) all KILLED.
- Commit e82d92d fix(metrics): bound lever CC window and pin skip rules (T04-18). Tests: test_metrics_levers.py 26 passed; tests/unit/metrics 722 passed. ruff format/check, mypy, lint-imports, check_module_size clean.

# T04-20 report: Portfolio API and step

Status: DONE_WITH_CONCERNS (see Concerns 1 and 2)
Worktree: D:\herness\.claude\worktrees\agent-ade75c0900a082736 (base bff6f4b)
Commits: 22f7586 wip(T04-20): portfolio API, input SQL, step wiring and tests; final: feat(metrics): add portfolio API and step (T04-20)

## Files (lines / budget)
- herness/metrics/portfolio.py: 358 / 360 (new). Scenario, PortfolioRow, PortfolioResult, resolve_scenario, optimize_portfolio, run_portfolio_step, PORTFOLIO_READBACK_SQL.
- herness/metrics/sql/portfolio_input.sql.j2: 84 / 110 (new). Uses only template-local SQL plus the existing shared `owner_team` and `p` macros. _macros.sql.j2 is unchanged.
- herness/metrics/scoring.py: 390 / 390. `"portfolio": run_portfolio_step` plus its import. The line was reclaimed by a docstring reflow: lines 5-6 of the module docstring became one line. No helper moved and no §2 row was added.
- tests/unit/metrics/test_metrics_portfolio.py (new, 22 functions, 33 cases).
- tests/support/metrics_scoring.py (additive): `patches()` adds a triple that sets `_STEP_FUNCS["portfolio"] = None`. See Concern 1.
- Not touched: _solver.py, render.py, _macros.sql.j2, metrics_tiny CSVs, metrics_oracle.py.

## Tests per ID (all in test_metrics_portfolio.py)
- UT04-103: test_ut04_103_scenario_fields, _resolve_named_custom_and_object, _unknown_scenario (6 params), _overlap_and_invalid_custom_budget
- UT04-107: test_ut04_107_persist_on_read_only_connection
- UT04-108: test_ut04_108_persist_false_records_ops_evidence. Read-only file and a fresh migrated ops_store. con=None goes through compute.open_readonly, which is patched. It checks the ops evidence row (run_id, build_id, row_count), that the file bytes are unchanged, that no score.portfolio table exists, and that the opened connection is closed.
- UT04-109: test_ut04_109_unconstrained_budget_and_capacity, test_ut04_109_run_scoring_wires_portfolio_step (wiring proof: run_scoring(steps=["portfolio"]) on metrics_tiny-derived data gives score.portfolio rows for lean, base, stretch and unconstrained)
- UT04-122: test_ut04_122_persist_without_connection. open_readonly and get_config are patched to raise, which proves no file is opened.
- ST04-08: test_st04_08_huge_budget_and_lists_rejected, test_st04_08_capacity_must_be_finite_and_bounded (inf, nan, 1e13, 1e30, 0, -1)
- ST04-09: test_st04_09_read_only_and_into_allowlist_refused
- ST04-14: test_st04_14_build_id_of_another_build (persist True and False; nothing written and no evidence recorded)
- Extra: UT04-101 (input rows, the U04-79 shape on a hand graph; SQL under the cap; the PortfolioRow rank invariant) and UT04-105 (the step part: an INFEASIBLE mandatory set gives a warning and no rows; a ConfigError scenario gives a warning and the loop continues).
- test_cv_t04_20_*: persist rows plus meta.evidence (input and read-back); determinism over 5 runs, and persist=False equals persist=True; a budget sweep that checks no overspend, no duplicate or negative allocation, ranks 1..n, and blockers honoured; SchemaViolation without score.funding; blocks_cycle; the solved log event.

## RED then GREEN (ST04-08/09/14)
RED: the module was first written without the guards (no le=1e12 / max_length=1000 / capacity bound, no read-only check, no build_id check). Running `pytest tests/unit/metrics/test_metrics_portfolio.py -k st04` gave 7 failed, 3 passed:
- test_st04_08_huge_budget_and_lists_rejected: `Failed: DID NOT RAISE ValueError`
- test_st04_08_capacity_must_be_finite_and_bounded[inf], [1e13], [1e30]: `DID NOT RAISE ValueError` ([nan], [0], [-1] were already rejected by gt=0)
- test_st04_09_read_only_and_into_allowlist_refused: `QueryError: Cannot execute statement of type "INSERT" on database "wh" which is attached in read-only mode!` (it reached run_recorded's evidence write instead of being refused up front)
- test_st04_14_build_id_of_another_build[True]/[False]: `Failed: DID NOT RAISE ConfigError`

GREEN after the guards: the same file gives 33 passed. `pytest tests/unit/metrics tests/fault/metrics -q -p no:logging` gives 722 passed. The WIP commit ran every pre-commit hook (ruff, mypy, import-linter, module-size, type-ownership, pytest-unit) and all passed.

## Gates
ruff check: clean. ruff format --check: 1010 files formatted. mypy (whole configured tree, 363 files): no issues. lint-imports: 13 kept, 0 broken. check_module_size: exit 0. check_type_ownership: exit 0.
Coverage of herness/metrics/portfolio.py: 100% line (219 statements), 100% branch (40 branches).

## Rendered portfolio_input SQL
3,389 characters raw and 3,104 normalized (cap 20,000). Bind parameters used: as_of_ts, s_window_days, w_cf_effort_hours, w_hours_per_point.

## Spec notes and deviations
1. TH04-08 hardening (sub-controller ruling 2; carry-over from T04-19 Minor 1): `Scenario.team_capacity_points` values must be finite, > 0 and <= 1e12 (`allow_inf_nan=False`). The number of entries in the dict is not bounded; that is left to a spec decision.
2. Read-back evidence key (deviation from the verbatim U04-80 step 10 params). The verbatim params `{"bind": {"scenario"}, "template": {"name", "scenario"}}` produce the same query_id for any re-run of a scenario name on the same build. With different settings (config change, then re-score of the same build; or an ad-hoc persist=True Scenario reusing a configured name), the stored rows differ, and run_recorded raises "nondeterministic result for q_...". A test hit this. The template therefore also carries `"inputs"`: 16 hex of sha256 over canonical_json of the input query_id, the effective scenario, weights.portfolio and team_capacity_points_per_quarter. Same inputs give the same query_id, so solver nondeterminism is still caught. The spec row should be amended.
3. Custom USD text gets `enforce_team_capacity=True` (the Scenario default), exactly as U04-78 writes `Scenario(name=..., budget_usd=...)`, not `portfolio.enforce_team_capacity`. Flag it if the config value was intended.
4. `Scenario` raises ValidationError for its own invariants, overlap included. resolve_scenario converts ValidationError to ConfigError("invalid scenario: ...") and raises ConfigError("candidate <id> is both mandatory and excluded") for an overlap after merging with config. Budget 0 is allowed only for `unconstrained`.
5. Row flags (T04-19 report item 2): `rows[i].flags` is `outcome.row_flags[cid]`, so every candidate has an entry. `no_team` and `no_points` appear only when capacity is enforced and the candidate has an effort; a candidate without effort carries only `no_estimate` (plus `blocked_by_noncandidate`). Result flags are the solver flags, plus `blocks_cycle` when ordering broke a cycle, plus `infeasible_mandatory` for INFEASIBLE/MODEL_INVALID.
6. Reuse: the connection handling and the meta.build read are `compute._connection` and `compute._read_build`, imported as private names. The read-only test is the same `current_setting('access_mode')` check scoring uses (scoring cannot be imported, because of a cycle). `existing_tables` comes from `_scoring_checks`.
7. The read-back runs even for INFEASIBLE scenarios: it records the zero-row state. PORTFOLIO_READBACK_SQL adds `ORDER BY candidate_id`.
8. The step warning counts `mandatory=` as the number of distinct `weights.portfolio.mandatory` IDs. Configured-name scenarios have no own lists, so this is the effective count.

## Concerns
1. Wiring portfolio while funding stays unwired (sub-controller ruling 1): a default `run_scoring` (steps=None, which is also what the build pipeline's score stage uses when score_steps is unset) now fails at step portfolio with SchemaViolation "score.funding missing; run step funding first". U04-81 requires that precondition. Before this card the step was skipped with a warning. The existing full-run tests (UT04-110/112/..., FT04-01 child) keep their old expectations through the additive `patches()` triple. T04-21 must wire funding and then drop that triple. Until T04-21 merges, real builds that score with default steps will fail.
2. Private-name imports from compute (`_connection`, `_read_build`) were chosen to avoid duplicating helpers. A reviewer may prefer making them public in compute.py, which is not in this card's files.

## Fix round 1

Fix agent, round 1 (review D:\herness\.superpowers\sdd\program\briefs\T04-20-review.md, sub-controller ruling). Checkpoints: 3a7d130 (I1), f125799 (I2 + m2); final commit `fix(metrics): portfolio review round 1 (T04-20)` (m1 + this section).

- **I1** tests/integration/model/test_model_build_enrich_score.py (IT02-26): added `monkeypatch.setitem(scoring_module._STEP_FUNCS, "portfolio", None)` next to the metrics/check replacements, with the comment `# T04-21: remove once funding is wired (portfolio needs score.funding)`. U04-81 is unchanged: SchemaViolation still raised when score.funding is missing. RED before: 1 failed (SchemaViolation at step portfolio). GREEN: tests/integration/model 129 passed. A grep of tests/ for other default-steps run_scoring or score-stage runs outside tests/support/metrics_scoring.py patches() matched only the files the reviewer already ran (fake hooks, explicit steps, or patches()). No other patch was needed.
- **I2** herness/metrics/portfolio.py `_input_query`: the portfolio_input template params now carry `"upstream": [sorted distinct unnest(score.funding.query_ids)]`. These are identifiers only, no data, and they are added to the rendered template dict after `render_named`, because the render context accepts only scalar identifiers. A funding change therefore yields a new input query_id; identical inputs keep the same one. New test `test_cv_t04_20_rescore_after_funding_change` reproduces the review probe: funding + step; then cost_per_engineer_hour x2, funding rerun, step rerun on the same build. It checks that the new budget equals the new Σ effort, the stored rows match, the input query_id is new, the meta.evidence params carry the upstream ids, and a same-inputs rerun keeps the same query_id. RED before the fix: SchemaViolation "nondeterministic result for q_1e9c54e7cc604f46" (template=portfolio_input). GREEN after the fix.
  - **Spec note:** U04-80 step 4 amendment: portfolio_input template params include the score.funding input query ids. (Concretely: `template = {**render_named("portfolio_input", {}, binds).template, "upstream": <sorted distinct query_ids of score.funding>}`; cf. F04-04's upstream attribution ID.)
- **m1** test_ut04_101_rendered_input_sql_under_evidence_cap: the vacuous meta.evidence check is replaced. The test now asserts that the ops evidence row for the input query holds the rendered SQL, and that meta.evidence has no row for that query_id and no `"portfolio_input"` template row (persist=False records ops evidence only).
- **m2** new `test_cv_t04_20_input_query_timeout[True|False]`: a spy on run_recorded asserts that the input query gets `timeout_s=compute_timeout_s` for persist=False and `None` for persist=True. Mutation M25 (`timeout = None`) is now KILLED: [False] fails.
- **Parked as ruled:** over-wide digest, capacity dict size, private compute imports, budget headroom.
- **Line counts:** portfolio.py 360/360 (the module docstring was shortened by one line to absorb the change, with no behaviour change), scoring.py 390/390 (untouched), portfolio_input.sql.j2 84/110. check_module_size exit 0.
- **Gates:** tests/unit/metrics/test_metrics_portfolio.py 36 passed; tests/unit/metrics + tests/fault/metrics 725 passed; tests/integration/model 129 passed; ruff check and ruff format --check clean; mypy (configured tree) no issues in 363 files; lint-imports 13 kept, 0 broken.

## Fix round 2

Fix agent, round 2 (review "## Re-review round 1"). Final commit `fix(metrics): portfolio review round 2 (T04-20)`.

- **I3** herness/metrics/portfolio.py `_input_query`: the upstream funding-ID read now runs only when `FUNDING_TABLE in existing_tables(con)`; otherwise `upstream = []`. The recorded input query then reads the missing score.funding inside `run_recorded` and raises QueryError, exactly as at c90c951. U04-80 Errors are therefore again only ConfigError, QueryError or StoreBusy. New test `test_cv_t04_20_missing_funding_query_error` (persist=False, read-only copy without score.funding gives QueryError). RED before: raw `_duckdb.CatalogException: Table with name funding does not exist!`. GREEN after.
- **m3** (upstream read not under the compute timeout): parked as ruled, unchanged.
- **Line counts:** portfolio.py 360/360. The `_input_query` docstring went from 2 lines to 1 to absorb the extra line, with no behaviour change. scoring.py 390/390 (untouched). check_module_size exit 0.
- **Gates:** test_metrics_portfolio.py 37 passed; tests/unit/metrics + tests/fault/metrics 726 passed; ruff check and ruff format clean; mypy no issues in 363 files; lint-imports 13 kept, 0 broken.

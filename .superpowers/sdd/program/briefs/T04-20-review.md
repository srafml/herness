# T04-20 review (VERIFY): Portfolio API and step

Worktree: D:\herness\.claude\worktrees\agent-ade75c0900a082736 at c90c951 (wip 22f7586 plus an empty closing commit), base bff6f4b.
Verdict: **Needs fixes**. One Important item must be fixed: a test outside the card's patch goes red. The other Important item is a plan-mandated spec gap; fix it or carry it over with an amendment.

### Spec Compliance
- U04-76 Scenario ✅. The model is frozen, forbids extra fields and is strict. `budget_usd` accepts an int (not a bool), decimal text and Decimal. It must be >= 0 and <= 1e12, and > 0 unless the name is `unconstrained`. Name pattern ok. Each list is limited to 1000 IDs of at most 256 characters. Capacities must be > 0. An overlap of `mandatory` and `excluded` is rejected. Extra hardening: capacities must also be finite and <= 1e12.
- U04-77 PortfolioRow/PortfolioResult ✅. `order_rank` is NULL exactly when the row is not selected; a validator enforces this (portfolio.py:121-126). Rows are built sorted by candidate_id (portfolio.py:237). `selected` is in order_rank order. `solver_status` is the four-value Literal from `_solver.SolverStatus`.
- U04-78 resolve_scenario ✅. The algorithm and both error messages match the spec text. `unknown scenario <name>` is cut at 64 characters to defend against huge input. A 13-digit custom amount above 1e12 gives `ConfigError("invalid scenario: ...")`, not "unknown scenario". That is acceptable because it is still a ConfigError.
- U04-79 portfolio_input.sql.j2 ✅. The columns, order and types match the spec. `expected_impact_usd` is the specified CAST, and UT04-101 checks it against score.funding. Points are the sum over non-done subtree items via `work_item_closure` and are NULL when none have points. The cluster-fix owning service reuses U04-65 step 6 exactly: the same window, `excluded`/NULL filter and tie order as funding_score.sql.j2:34-41. The owner team comes from the shared `owner_team` macro, and cluster-fix points are `w_cf_effort_hours / w_hours_per_point`. Descendants are sorted candidates at depth >= 1. Blockers are sorted and distinct. `noncandidate_blockers` counts the non-done, non-candidate `from_key` items. The input is ONE recorded SELECT through `run_recorded` with the shared result_hash (portfolio.py:192-205). The real run shows a single `template=portfolio_input` row for each scenario.
- U04-80 optimize_portfolio ✅ with one documented deviation. Steps 1-13 are present. In step 10 the read-back template params carry an extra `inputs` key (concern 2, accepted). The read-back SQL also adds `ORDER BY candidate_id`. The error messages for the persist-without-con, read-only and build_id cases are verbatim.
- U04-81 run_portfolio_step ✅. The DDL is verbatim (portfolio.py:52-57). The step loops over the scenarios in config order and then `unconstrained`. Both warning formats are verbatim. A missing `score.funding` raises SchemaViolation.
- ⚠️ BT04-05 and BT04-08 (< 30 s per scenario) are outside this card's tests. In the real run each scenario took about 0.06 s on the tiny fixture.

### Strengths
- All three guard mutations (TH04-08, TH04-09, TH04-14) are killed by their own ST test when run on its own (see table). Solver nondeterminism, simulated by a per-run row flag, is still caught: the read-back raises SchemaViolation "nondeterministic result".
- Coverage of portfolio.py is 100% line (219 statements) and 100% branch (40 branches).
- persist=False is checked end to end: a read-only file, a fresh ops store, file bytes unchanged and the opened connection closed.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. **A test outside the card's patch goes red after the wiring.** tests/integration/model/test_model_build_enrich_score.py:561 (`test_it02_26_scoring_checkpoint_survives_yield_and_resume`) runs the real `run_scoring` with default steps. It patches only `metrics` and `check` (line 580) and now fails with `SchemaViolation` at step `portfolio`, stage `score`. Causality was confirmed with a probe: setting `"portfolio": None` in scoring.py makes it pass, and the probe was reverted. The builder's report and `patches()` miss this test because it does not use `tests/support/metrics_scoring.patches()`. Fix (test setup only, same pattern as the support triple): add `monkeypatch.setitem(scoring_module._STEP_FUNCS, "portfolio", None)` next to line 580, with a "T04-21 drops this" note. Do not soften U04-81.
2. **Plan-mandated spec gap: re-scoring the same build after a weights change that affects funding fails at portfolio_input.** U04-80 step 4 renders `portfolio_input` with `{}` template params. The input query_id therefore depends only on the SQL, the four used binds and the build, not on the `score.funding` query it reads. A probe showed the failure. Funding and the step under w1 passed. With `cost_per_engineer_hour` x2, re-running funding passed (sum of effort 23500 -> 42500), but the portfolio step then raised `SchemaViolation nondeterministic result for q_1e9c54e7cc604f46`. This path is reachable: a config change invalidates the scoring checkpoint (scoring.py:235), and stage `score` can run alone on an existing build. The `inputs` digest of concern 2 does not help, because the input query fails first. Fix, mirroring F04-04's "upstream attribution ID": put the upstream funding query_id(s) into the portfolio_input template params, for example `{"upstream": sorted distinct unnest(score.funding.query_ids)}`. Either needs a spec amendment (below) and the fix here, or a carry-over to T04-21 with the amendment. The controller decides.

#### Minor (Nice to Have)
1. The meta.evidence check in tests/unit/metrics/test_metrics_portfolio.py:208-209 proves nothing. persist=False writes nothing to meta.evidence, so the rows it checks are the funding rows. Assert that no `portfolio_input` row exists instead.
2. Mutation M25 survived: forcing `timeout = None` for persist=False (portfolio.py:203) keeps every test green. Add an assertion that `run_recorded` gets `compute_timeout_s` when persist=False.
3. The `inputs` digest (portfolio.py:239-241) is wider than it needs to be. It hashes all of `weights.portfolio`, including the other scenarios' budgets, and the capacity block's `unconfirmed` flag. Changing another scenario therefore gives this scenario's read-back a new query_id. This is harmless: there are no false conflicts and no missed nondeterminism.
4. The number of entries in `Scenario.team_capacity_points` is not limited (TH04-08). The spec sets no limit, so this needs a spec decision.
5. portfolio.py:26 imports the private `compute._connection` and `compute._read_build` (concern 4). Carry over to make them public.
6. If `existing_tables` or the solver changes, the 358/360 and 390/390 line budgets leave no room to absorb it.

### Mutation table (each mutation applied alone to herness/metrics/portfolio.py; byte-exact restore; tree clean afterwards)
| # | Mutation | Result | Killing test |
|---|----------|--------|--------------|
| M01 | budget `le=1e12` removed | KILLED | test_st04_08_huge_budget_and_lists_rejected |
| M02 | mandatory `max_length=1000` removed | KILLED | test_st04_08_huge_budget_and_lists_rejected |
| M03 | excluded `max_length=1000` removed | KILLED | test_st04_08_huge_budget_and_lists_rejected |
| M04 | ID `max_length=256` removed | KILLED | test_st04_08_huge_budget_and_lists_rejected |
| M05 | capacity `le`/`allow_inf_nan` removed | KILLED | test_st04_08_capacity_must_be_finite_and_bounded[inf] |
| M06 | read-only refusal removed | KILLED | test_ut04_107; on its own, test_st04_09_read_only_and_into_allowlist_refused |
| M07 | build_id check removed | KILLED | test_st04_14[True] and, on its own, [False] |
| M08 | persist-without-con check removed | KILLED | test_ut04_122_persist_without_connection |
| M09 | ops evidence for persist=False removed | KILLED | test_ut04_108_persist_false_records_ops_evidence |
| M10 | meta.evidence read-back removed | KILLED | test_cv_t04_20_persist_writes_rows_and_evidence |
| M11 | input not recorded when persist | KILLED | test_cv_t04_20_persist_writes_rows_and_evidence |
| M12 | budget x10 passed to solver (overspend) | KILLED | test_ut04_108; on its own, test_cv_t04_20_budget_sweep_invariants |
| M13 | duplicated ID in `selected` | KILLED | test_ut04_108; on its own, test_cv_t04_20_budget_sweep_invariants |
| M14 | negative expected impact | KILLED | test_ut04_109_unconstrained_budget_and_capacity |
| M15 | unconstrained enforces capacity | KILLED | test_ut04_109_unconstrained_budget_and_capacity |
| M16 | merged overlap check removed | KILLED | test_ut04_103_overlap_and_invalid_custom_budget |
| M17 | DELETE before INSERT removed | KILLED | test_ut04_109_unconstrained_budget_and_capacity |
| M18 | INSERT also on INFEASIBLE | KILLED | test_ut04_105_step_infeasible_mandatory_and_rejected_scenarios |
| M19 | `inputs` dropped from read-back key | KILLED | test_cv_t04_20_persist_writes_rows_and_evidence (nondeterministic result) |
| M20 | digest built from query_id only | KILLED | test_cv_t04_20_persist_writes_rows_and_evidence |
| M21 | step stops swallowing ConfigError | KILLED | test_ut04_105 |
| M22 | score.funding precondition removed | KILLED | test_cv_t04_20_step_needs_funding_and_flags_cycles |
| M23 | rows sorted descending | KILLED | test_ut04_109 |
| M24 | config mandatory not merged | KILLED | test_ut04_103_resolve_named_custom_and_object |
| M25 | persist=False timeout dropped | **SURVIVED** | none (Minor 2) |
| M26 | step warning text shortened | KILLED | test_ut04_105 |
| M27 | solver nondeterminism (row flag changes on every run) | KILLED | test_cv_t04_20_deterministic_and_persist_modes_agree (read-back SchemaViolation) |

RED then GREEN for ST04-08/09/14: each guard was disabled one at a time and the matching ST test went red (M01-M05, M06 on its own, M07 on its own). With the guards restored, all 33 cases pass.

### Gates (run by the verifier)
- tests/unit/metrics/test_metrics_portfolio.py: 33 passed. With `--require-test-ids` it also gives 33 passed, so every function carries a UT04/ST04 ID or the `test_cv_` review prefix. `pytestmark = pytest.mark.unit` follows the existing metrics ST04 convention.
- tests/unit/metrics plus tests/fault/metrics: 722 passed.
- Coverage of portfolio.py: 100% line, 100% branch.
- `tools.check_module_size`: exit 0. Line counts: portfolio.py 358/360, portfolio_input.sql.j2 84/110, scoring.py 390/390.
- ruff check and ruff format --check: clean. mypy over the configured tree: no issues in 363 files. lint-imports: 13 kept, 0 broken.
- Real wiring run, done by the verifier with a temp script. `run_scoring(BUILD_ID, steps=["portfolio"])` and `steps=["portfolio","check"]` were run on a metrics_tiny-derived warehouse where the real funding step had run. Both gave steps_done `['validate','portfolio'(, 'check')]` and `score.portfolio` = 20 rows (base, lean, stretch and unconstrained, 5 each, OPTIMAL, 3 selected). meta.evidence held one input query per scenario and one read-back per scenario. Default steps on a warehouse without funding gave `SchemaViolation score.funding missing; run step funding first` at step portfolio, which is expected.

### Rulings on the builder's concerns
1. **Behaviour change (default steps, funding unwired -> SchemaViolation at portfolio).** The spec behaviour stands. (a) The `patches()` triple (tests/support/metrics_scoring.py:59-63) only replaces `scoring._STEP_FUNCS` with a copy where `portfolio` is None. It changes no assertion, and the tests that use it keep their expectations. UT04-109's wiring test strips that triple and proves the real step is wired. (b) Default-steps `run_scoring` and score-stage runs outside the patch that the verifier found and ran:
   - tests/integration/model: test_model_build_enrich_score.py, test_model_build_dq.py, test_model_build_promote.py and test_model_meta_rows.py. All pass except **test_it02_26_scoring_checkpoint_survives_yield_and_resume (RED, Important 1)**. The others use fake scoring hooks or explicit steps.
   - tests/unit/model (handler, payload): pass, because they use fakes.
   - tests/security/test_st02_promote.py and tests/fault/model/test_model_promote_fault.py: pass (fake hooks).
   - tests/support/build_kill.py: a stub `_scoring`, so it is unaffected.
   - tests/support/scoring_kill.py and tests/fault/metrics/test_metrics_scoring_fault.py: use `patches()` and pass.
   - tests/unit/store, tests/integration/jobs, tests/unit/core/jobs/test_jobs_scheduler.py, tests/security/test_st07_lifecycle.py and tests/integration/test_security_e2e.py: 815 passed.
   - There is no tests/integration/metrics scoring run and no impl 09 CLI package or tests on this tree (tests/**/cli* matches only egress HTTP clients).
   Fix: extend the same "portfolio -> None" patch to IT02-26 (Important 1). T04-21 removes both.
2. **`inputs` digest on the read-back.** Accepted. The digest is sha256 over canonical_json of: the input query_id (SQL, binds and build), the effective scenario (budget, merged mandatory/excluded, capacities, enforce flag), `weights.portfolio` (horizon_quarters, solver settings, lists, scenarios) and `team_capacity_points_per_quarter` (teams, default). Those are every argument of `solve_portfolio`/`order_selected` and everything that sets the persisted rows. It contains no timestamps or other run-varying material; the dumps are of frozen config/pydantic models. It is wider than needed (Minor 3), which is safe. M27 shows that real nondeterminism (same inputs, different rows) is still caught. One gap: the input query_id does not cover the upstream funding data, so a weights change affecting funding fails one step earlier at the input query (Important 2).
3. **Custom USD text keeps `enforce_team_capacity=True`.** This matches the spec text. U04-78 says `Scenario(name=..., budget_usd=Decimal(text))` with no `enforce_team_capacity`, so the U04-76 default (True) applies, and design §3.1 has the same default. Spec ambiguity note: with `portfolio.enforce_team_capacity: false`, a custom amount via persist=False enforces capacity while a configured scenario with the same budget does not, so the two give different selections. The spec owner should confirm the intent. If the config value was meant, amend U04-78 to `Scenario(..., enforce_team_capacity=portfolio.enforce_team_capacity)`. No code change in this card.
4. **Private imports `compute._connection` / `compute._read_build`.** Accepted as a carry-over. No defect: the connection is closed on exit (UT04-108 checks this), and `_read_build` raises SchemaViolation on a malformed meta.build. Carry-over: make them public in compute.py, or move them to a shared private module, in a card that owns compute.py.

### Spec notes and amendments (drafts)
- **U04-80 step 10 (read-back key)**, replace the run_recorded clause with: "then `run_recorded(con, PORTFOLIO_READBACK_SQL, {"bind": {"scenario": name}, "template": {"name": "portfolio_readback", "scenario": name, "inputs": inputs}}, "score", build_id)`, where `inputs` = the first 16 hex characters of `sha256_hex(canonical_json({"input": rq.query_id, "scenario": <effective Scenario>.model_dump(mode="json"), "portfolio": weights.portfolio.model_dump(mode="json"), "capacity": weights.team_capacity_points_per_quarter.model_dump(mode="json")}))`, and `PORTFOLIO_READBACK_SQL` selects every `score.portfolio` column except `query_ids` with `WHERE scenario = CAST($scenario AS VARCHAR) ORDER BY candidate_id`. The digest keys the read-back to everything that determines the stored rows. Re-solving a scenario name with other settings on the same build is therefore a new query, while the same inputs giving different rows still raise SchemaViolation("nondeterministic result for <qid>")."
- **U04-80 step 4 (upstream key, Important 2)**: "`render_named("portfolio_input", {"upstream": <sorted distinct query_ids of score.funding>}, binds)`, so a re-score of the same build after a funding-affecting config change records a new input query (cf. F04-04 upstream attribution ID)."
- **U04-78 (concern 3)**: state explicitly whether a custom USD scenario takes `enforce_team_capacity = portfolio.enforce_team_capacity` or the Scenario default True.
- **U04-76 (TH04-08)**: optionally limit the number of `team_capacity_points` entries (for example <= 1000) and state "finite, <= 1e12" for the values.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation matches U04-76…U04-81, and every guard is mutation-proven. Wiring the portfolio step turns IT02-26 red outside the support patch, and that must be patched the same way. The upstream-key spec gap (Important 2) needs either the fix here or a recorded carry-over with the amendment.

Findings: Critical 0, Important 2, Minor 6. Worktree status: clean (all probes reverted, verified with status --short and diff).

## Re-review round 1

Scoped re-verify of fix round 1 (worktree agent-ade75c0900a082736, head fc241d7, round-1 base c90c951). Round-1 diff touches only herness/metrics/portfolio.py, tests/integration/model/test_model_build_enrich_score.py and tests/unit/metrics/test_metrics_portfolio.py.

### Findings

- **I1 ✅** IT02-26 (test_model_build_enrich_score.py:582-583) sets `_STEP_FUNCS["portfolio"] = None` with the comment `# T04-21: remove once funding is wired (portfolio needs score.funding)`. U04-81 is unchanged (portfolio.py:342-344 still raises SchemaViolation, and CV test :583-590 still passes). A grep of tests/ for run_scoring, _STEP_FUNCS and score-stage runs found only these: fake `_load_run_scoring` hooks (st02_promote, build_kill, build_handler), explicit `steps=[...]` calls, runs inside `patches()` (scoring_kill, scoring unit and fault tests), and the patched IT02-26. tests/integration/model: 129 passed.
- **I2 ✅ (fix correct; see new I3 for the regression it introduced)** portfolio.py:61 and :203-204 merge `upstream` (sorted distinct `unnest(score.funding.query_ids)`) into the rendered template params after render_named.
  - (a) A funding change gives a new input query_id, and re-scoring the same build after a weights change succeeds (test_cv_t04_20_rescore_after_funding_change). Mutation "drop upstream" (template = rendered.template) brings back `SchemaViolation nondeterministic result for q_1e9c54e7cc604f46` and the test is KILLED. Mutation "truncate upstream to [:1]" is KILLED too.
  - (b) Probe: two identical `optimize_portfolio("4000")` runs on one build give the same query_id, and meta.evidence holds exactly 1 `portfolio_input` row. Its result_hash is shared, because run_recorded's nondeterminism check passed.
  - (c) Probe: after the step, `UPDATE score.funding SET effort_cost_usd = effort_cost_usd + 1` leaves query_ids, and so the input query_id, unchanged. The rerun raises `SchemaViolation ... nondeterministic`, so real nondeterminism is still caught.
  - (d) The new param holds IDs only. The recorded params on the fixture are `{"template":{"name":"portfolio_input","upstream":["q_c8f9…","q_f01e…"]}, "bind":{…}}`, 214 chars in total. score.funding always carries exactly **2** distinct ids, because funding.py:35 writes IntoSpec(..., "query_ids", (attribution.query_id,)) on every row: the funding_score id and the attribution id. The 20,000-char Evidence cap applies only to `Evidence.sql` (core/types/harness/evidence.py:89), and `params` is unaffected by this change. Params are bounded separately by `_MAX_PARAMS_BYTES = 65_536` (metrics/_recorded.py:29,67), which allows about 3,100 ids at 21 bytes each. Probe with forced distinct ids: 100 → OK, 2,000 → OK, 4,000 → `ConfigError("params too large")`. That error comes before any evidence is written, so evidence cannot break. In run_portfolio_step it would become a per-scenario warning, but it cannot be reached through the real funding writer.
  - (e) There is still one recorded SELECT. The upstream read is an unrecorded read of identifiers that stores no number, which fits ENG §2.3 "every stored number comes from a recorded SELECT". The meta.evidence `portfolio_input` count is 1 (probe). For persist=False, UT04-101 shows the ops row only.
- **m1 ✅** UT04-101 now checks the real ops evidence (`get_evidence`, SQL tokens equal the rendered SQL) and the absence of a meta.evidence row. Mutation "skip record_evidence for persist=False" is KILLED (None evidence). Mutation "producer always 'score'" (persist=False also writes meta.evidence) is KILLED (count ≠ 0).
- **m2 ✅** test_cv_t04_20_input_query_timeout: mutation `timeout = None` is KILLED ([False] fails), and `timeout = compute_timeout_s` always is KILLED ([True] fails).
- **Docstring trim ✅** The only changes to portfolio.py are the 3-line module docstring shortened to 2 lines, the `_UPSTREAM` constant, and `_input_query` (docstring plus the 2 changed lines). No other behaviour changed.

### New findings

- **Important I3: the fix leaks a raw DuckDB exception from `optimize_portfolio` when score.funding is absent** (herness/metrics/portfolio.py:203). The new `con.execute(_UPSTREAM)` runs outside run_recorded. Calling `optimize_portfolio(..., persist=False, con=…)` on a warehouse without score.funding now raises `_duckdb.CatalogException` ("Table with name funding does not exist"). At c90c951 the same call raised `QueryError` (verified by a probe that temporarily swapped in the c90c951 module: `herness.core.errors.QueryError`, with query_id q_1e9c54e7cc604f46). The U04-80 Errors row is `ConfigError, QueryError, StoreBusy`, and the docstring at :311 says the same. Today no promoted warehouse has score.funding until T04-21 wires funding, so the first read-only caller (T04-21 CLI or API) would get an unmapped driver error. run_portfolio_step is unaffected because it pre-checks at :342. Suggested fix, within the 360-line cap: raise `QueryError` from `_input_query` when `FUNDING_TABLE not in existing_tables(con)`, or wrap the upstream read as `except duckdb.Error as err: raise QueryError(str(err)) from err`. Free one line elsewhere if needed. Add a CV test asserting `pytest.raises(QueryError)` for persist=False with score.funding dropped.
- **Minor m3** (portfolio.py:203): the upstream read is not covered by the persist=False `compute_timeout_s` interrupt. It is a DISTINCT over a 2-value list column on a candidate-sized table, so the risk is negligible. No action needed beyond noting it.

### Gates (fc241d7)

- tests/unit/metrics/test_metrics_portfolio.py --require-test-ids: 36 passed. Coverage of portfolio.py: 221 stmts, 40 branches, 100% / 100%.
- tests/integration/model: 129 passed.
- tests/unit/metrics + tests/fault/metrics: 729 passed. That count is 725 card tests plus 4 temporary probe tests collected in the same run; the probes are deleted.
- ruff check: clean. ruff format --check: 1010 files formatted. mypy: no issues (363 files). lint-imports: 13 kept, 0 broken. tools/check_module_size.py: exit 0.
- Line counts: portfolio.py 360/360, scoring.py 390/390.
- All mutation and probe edits reverted. portfolio.py is byte-identical to fc241d7, and `git status --short` is empty.

### U04-80 step 4 amendment (final wording)

> 4. `r = render_named("portfolio_input", {}, binds)`; `upstream` = the sorted distinct `query_ids` of `score.funding` (`SELECT DISTINCT unnest(query_ids) AS q FROM score.funding ORDER BY q`; identifiers only, no data). `rq = run_recorded(con, r.sql, {"bind": r.bind, "template": {**r.template, "upstream": upstream}}, "score" if persist else None, build_id=…, timeout_s=None if persist else compute_timeout_s)`. The `upstream` key is merged into the rendered template params after rendering, not passed as render_named context, so the input query_id changes whenever score.funding is rebuilt from different inputs (cf. F04-04 upstream attribution IDs). A missing `score.funding` raises `QueryError`.

### Verdict

**Needs fixes.** I1, I2, m1 and m2 are resolved and verified by mutation. One new Important finding, I3 (raw CatalogException in place of QueryError when score.funding is missing, introduced by the I2 fix), must be fixed. m3 is informational.

## Re-review round 2

Scoped re-verify of b81fb8c (round-2 base fc241d7). The diff touches only herness/metrics/portfolio.py (`_input_query`: docstring shortened to one line, plus the `funded` guard) and tests/unit/metrics/test_metrics_portfolio.py (new test_cv_t04_20_missing_funding_query_error).

### Findings

- **I3 ✅** portfolio.py now reads the funding ids only when `FUNDING_TABLE in existing_tables(con)` and otherwise uses `upstream = []`. The recorded input query then fails inside run_recorded and raises `QueryError`. The new test drops score.funding, uses a read-only copy with persist=False, and expects `QueryError`; it passes. Mutation `funded = True` turns it red with `_duckdb.CatalogException: Table with name funding does not exist`, so the test is KILLED.
- **I2 no regression ✅** When the table exists, `upstream` is still included, and test_cv_t04_20_rescore_after_funding_change passes. Mutation "drop upstream" (template = rendered.template) is KILLED: the nondeterministic SchemaViolation comes back. Mutation `funded = False` is KILLED by the same test.
- **Docstring ✅** The only non-guard change is the `_input_query` docstring, which now fits on one line. There is no behaviour change.
- **Parked fixer concern (raw duckdb error when score.funding exists but has a corrupted schema): I agree with Minor.** score.funding is written only by the funding step through IntoSpec with a fixed `query_ids VARCHAR[]` column (funding.py:35). A missing or mistyped column would mean the warehouse is already corrupt, and portfolio_input itself reads score.funding, so it would fail as well. No action is needed for T04-20.
- No new Critical, Important or Minor findings. Round-1 m3 (the upstream read is outside the persist=False timeout) stays informational.

### Gates (b81fb8c)

- Card file with --require-test-ids: 37 passed. Coverage of portfolio.py: 222 stmts, 40 branches, 100% / 100%.
- tests/unit/metrics + tests/fault/metrics: 726 passed.
- ruff check: clean. ruff format --check: 1010 files formatted. mypy: no issues (363 files). lint-imports: 13 kept, 0 broken. tools/check_module_size.py: exit 0.
- Line counts: portfolio.py 360/360, scoring.py 390/390.
- All mutations reverted. portfolio.py is byte-identical to b81fb8c, and `git status --short` is empty.

### Verdict

**Approved.**

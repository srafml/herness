# T06-11 review (verify agent): Funding pipeline (U06-71, U06-72; UT06-50, UT06-51)

Worktree agent-aa59b05d0b0c0d39b, head 29e00c3, base 88a7f75. Controller rulings R1, R2, R3 applied as given and not flagged.

### Spec Compliance
- ✅ U06-72 step 1 (candidates): the SQL matches the spec exactly apart from `?` becoming `$k`. With focus, `LIMIT` is dropped, `AND candidate_id IN (SELECT unnest($focus_ids))` is added, and only focus ids of type `candidate` are passed (funding_review.py:102-111).
- ✅ U06-72 step 2 (clusters): `= 'cluster_fix'`, `K_clusters`, same focus handling.
- ✅ U06-72 step 3 (incident presence): the SQL matches the spec. It runs only in standard or deep, with `since = window_end - window_days` (funding_review.py:183-190). `CAST($since AS DATE)` is needed because params must be JSON values (SN5). A test checks `since == "2025-09-26"`.
- ✅ U06-72 step 4 (change share): the SQL matches the spec. It runs only in deep (funding_review.py:192-197).
- ✅ Exactly 4 reads, all through `reader`: `common.read_rows` is the only I/O. The module imports no duckdb, store, llm or network module; an AST test enforces this. In the test, the reader is the real `execute_recorded` over a `ToolContext`, so each read gets an evidence row and an evidence use.
- ✅ Step 5 (order and depth rules): delivery, plus ops when `n > 0` (none in fast); cluster ops, plus change when deep and share `> 0.20` strictly (a boundary test uses c2 with share exactly 0.2); then the retrospective when some prior rec has a non-null outcome, using up to 2 distinct run_ids (_review_common.py:91-94, 164-172).
- ✅ Step 6: all 5 objective templates match the spec word for word (checked against the spec text).
- ✅ Step 7 fields: a new task_id; role and model_role `analyst`; scope candidate or run; must_cover taken from `must_cover(ctx)`; priority from U06-91 (retrospective: `(None, True)`, giving 50); query_ids = the score row's `query_ids` plus the selection qid, deduplicated; `candidate_ids=[id]`; the DQ table sets per group match the spec; notes are cut to 1,500; tools use the U06-101 signature through the stand-in; budget is `analyst_budget`; `compute_dedup_key` is used. Every `TaskSpec` goes through pydantic validation when it is built.
- ✅ Errors: `QueryError` propagates (tested with the change-link table missing). Nothing is logged, and the one error message (the stand-in's `ValueError`) contains no data.
- ✅ U06-71 must_cover: top `M_must` candidates, plus portfolio `selected`, plus the top 3 cluster fixes as `candidate:<id>`, plus `run:<id>`. The (id, rank) cache feeds `ranked_entities`.
- ✅ U06-71 planner_input: keys and deterministic row keys match the spec, including `score_row` (None for the retrospective).
- ✅ U06-71 writer_input: keys and the funding outline match the spec. `challenge_summary` holds the last verdict and the concern/fail notes.
- ✅ U06-71 other methods: `challenge_priority` uses U06-68 and `recommendation_drafts` uses U06-70.
- ✅ UT06-50: fast, standard and deep counts, order and specialties; must-cover; priorities 149/94/135/84; focus ignores K; exact SQL and params; the retrospective, notes and DQ.
- ✅ UT06-51: planner and writer keys and outline; ranked_entities from the cached ranks; get_pipeline.
- ✅ R1: org_review behaviour is unchanged. I compared it line by line against 88a7f75. The retrospective still uses the same template, the same DQ tables and the 100 cap. Team and rollup inputs are identical; `candidate_ids` stays `[]`, as the TaskInputs default was. Priorities and tools are unchanged; the org specialties never include crosscheck, so the stricter stand-in cannot raise. `writer_input` gives the same dict plus `levers`. All UT06-52/53 tests are green.
- ✅ R2: `RecordedReader = Callable[[str, dict[str, JsonValue]], RecordedResult]` (base.py:49). The unused Protocol stand-in is removed.
- ⚠️ Cannot verify from diff / spec questions:
  - C1 (cross-spec, agree with the builder): spec 07 `RecommendationDraft.target_type` is `Literal["service","team","org","work_item"]` (herness/core/types/memory.py:192), so a funding recommendation that targets a `candidate` (as ranked_entities does) or a `cluster_fix` fails U06-70. This needs a spec 07 or DECISIONS ruling; it is outside T06-11.
  - SN1 / must-cover coverage: a portfolio-selected id outside the step 1 selection (for example `e11` in fast with K_candidates=10; test line 265) is a must-cover entity that has no deterministic task and no cached rank, so `ranked_entities` leaves it out. The spec allows only 4 queries, and portfolio `order_rank` is not a `score.funding.rank`. This is literally compliant, but the coverage rule could then mark such runs non-publishable unless the model part of the planner adds tasks. Needs a spec owner ruling.
  - SN2: a focus whose `entity_type` is not `candidate` selects nothing. This is a literal reading and is tested.
  - SN3: focus also filters clusters. Reasonable, since step 2 says "same".
  - SN4: steps 3 and 4 are skipped when their id list is empty. Harmless.
  - SN6: ops and change tasks do not cite the incident or share query ids. This matches the spec text.
  - SN7: the retrospective uses the candidate DQ tables. The spec says nothing on this.
  - SN8: query_id order is the score row's ids first, then the selection qid. This follows the spec's wording.
  - SN9: the stand-in covers only the non-crosscheck analyst. OK until T06-13.
  - SN10: the module-map dependency `herness.metrics.portfolio` is not imported. Nothing needs it, so there is no issue.
  - DQ matching by substring: `e01` also matches details that mention `e010`. This is a literal reading of "JSON text contains the candidate id" and is the same approach org uses.
  - UT06-50's acceptance check against the spec 11 `tiny_build` is deferred under R3 (local tmp DuckDB) until T11-17 lands.

### Gates (re-run by verifier)
- `pytest tests/unit/harness/pipelines -q --cov-branch`: 121 passed. funding_review.py, _review_common.py and org_review.py each have 100% line and 100% branch coverage.
- ruff check and format: clean.
- mypy on the project scope (herness, tools): no issues in 243 files.
- lint-imports: 13 contracts kept.
- check_module_size: exit 0.
- Line counts against §2 budgets:
  - funding_review.py: 234 of 360
  - _review_common.py: 208 of 260 (new §2 row added in the same commit)
  - org_review.py: 183 of 330
  - base.py: 216 of 260
- Tests: every test has a UT06-50 or UT06-51 docstring prefix, and `pytestmark = pytest.mark.unit` is set.

### Strengths
- Reads use the real `execute_recorded` path in tests, so evidence rows and uses are asserted rather than mocked.
- The share boundary (exactly 0.2) and the window boundary (an incident before the window) are both tested.
- Selections are read once and cached across `must_cover` and `deterministic_tasks` (asserted with a count of 2 reads).
- The R1 extraction is clean and preserves org behaviour.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
- tests/unit/harness/pipelines/test_pipelines_org_review.py:101: `_Reader.__call__` still takes `params: dict[str, object]`, which no longer matches the retyped `RecordedReader` (the dict value type is invariant). Running mypy on the tests reports about 27 arg-type errors. Tests are outside the configured mypy scope, so the gate stays green, but the test double should be `dict[str, JsonValue]`.
- tests/unit/harness/pipelines/test_pipelines_funding_review.py:469: `test_ut06_51_default_tools_stand_in_scope` tests the T06-13 stand-in (U06-101 behaviour) under a UT06-51 id. Label it as a stand-in test, or drop it when T06-13 lands.
- herness/harness/pipelines/_review_common.py:119: DQ names are silently cut to 100 to fit `TaskInputs.dq_warnings` max_length. The spec does not mention this cap; it is carried over from org. Consider a comment citing the TaskInputs bound.

### Assessment
**Task quality:** Approved
**Reasoning:** The 4 reads, the task plan and the planner and writer inputs match U06-71 and U06-72 exactly. All I/O goes through the recorded reader, and all gates pass. The remaining points are the cross-spec C1 question, the must-cover coverage question and minor test-typing polish.

Verdict: Approved

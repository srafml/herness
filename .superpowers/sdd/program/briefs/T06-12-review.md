# T06-12 review (Org pipeline, U06-73 / U06-74) — commit a951daf

**Verdict: Approved** (no Critical or Important findings; 3 Minor items that can be fixed now or later)

### Spec Compliance
- ✅ U06-73 OrgReviewPipeline: kind; `__init__(reader, *, window_end)`; must_cover = top M_must teams, taken from the cached K_teams selection, as `team:<id>`, plus `run:<id>` for up to 2 retrospective runs; caches `(team_id, min rank)` for ranked_entities (org_review.py:197-201, 319-321). planner_input keys and deterministic item keys match U06-71 exactly (277-293). writer_input: same keys as funding, plus the exact 7-item org outline and `levers`. The levers are read with the verbatim U06-73 SELECT for the selected teams, and every row gets the read's `query_id` (299-317). challenge_priority = U06-68 and recommendation_drafts = U06-70.
- ✅ U06-74 step 1: SQL is verbatim apart from `$k` / `$focus_ids`. With focus there is no LIMIT and an `IN unnest` filter (44-47, 99-104). Step 2: lever SQL is verbatim with QUALIFY top 3 (48-52). Step 3: rollup SQL is verbatim plus `list(team_id ORDER BY team_id)` (54-57). SN3 is justified because the rollup priority needs each org's selected teams, and the change is minimal.
- ✅ Tasks: one task per team per `knobs.org_specialties` entry, in team-rank order; one `org` task per rollup; a retrospective task as in funding (outcome not null, distinct run_ids, at most 2, scope `run`, `base_priority(None, True)`).
- ✅ All five objective templates are character-exact (62-73), and the retrospective template matches U06-72.
- ✅ Fields: new task_id, role/model_role analyst, scope team/org/run, must_cover from must_cover(ctx) (rollup False, retrospective True), priority per U06-91 (rollup uses the min rank of its selected teams, False). `inputs.query_ids` = teams qid + lever read qid + each lever's query_ids, deduplicated. Notes start with the "Top action levers: a, b, c." line (metric names only), followed by prior-rec lines, cut to 1500 chars (empty becomes None). DQ matching covers the entity ids plus the five spec tables. Tools come from the U06-101 analyst list without request_subtask when child_depth >= max_spawn_depth. budget = analyst_budget; dedup_key = compute_dedup_key(...).
- ✅ Controller rulings applied: the "as funding" helpers sit under a liftable divider (107); `_base_priority` is exact U06-91 with a `# T06-15:` marker (86-89); `_default_tools` has a `# T06-13:` marker (92-96); UT06-52 runs on a tmp DuckDB with a fake RecordedReader and a docstring note about tiny_build; UT06-53 covers get_pipeline("org_review") and the writer levers' query_id.
- ✅ UT06-52 covers every Expected clause. Specialties per depth: parametrized over fast/standard/deep. Lever query ids: test_ut06_52_lever_query_ids_and_notes, including the cut of the 4th lever. Rollup for ≥ 2 teams: o1 and o2 are rolled up and o3 (one team) is not, with a K_teams variant. Extra coverage: focus, must_cover and priority, caching (3 reads), retrospective, the 1500-char cut, and DQ matching.
- ✅ UT06-53: writer_input lever rows keep their query_id and have the exact column set. `get_pipeline("org_review")` is covered here, and "chat" → ConfigError is covered by the existing test in test_pipelines_base.py (per ruling 4).
- ✅ Spec notes SN1-SN11 are all reasonable readings and minimal. SN5 (the lever read qid is added alongside the rows' query_ids) and SN6 (retrospective notes are matched on run_id) are the only readings beyond the spec text, and both are defensible.
- ⚠️ SN2: a focus whose entity_type is not `team` (for example `org`) selects no teams, silently. The spec says only "ids are filtered". Confirm with the spec owner whether an org focus should expand to its teams.
- ⚠️ `score.action_lever` can hold two rows for one metric (one per target_kind, peer_median and top_quartile, spec 04). The verbatim top-3 QUALIFY can then give "Top action levers: mttr, mttr, cfr." and break ties between equal (delta_usd, metric) rows nondeterministically. This follows the spec as written; it is a spec-level question.
- ⚠️ writer_input `levers` rows carry raw DuckDB values (Decimal delta_usd, DOUBLE). JSON serialization is the writer step's job (a later card) and cannot be verified here.

### Gates (re-run by reviewer in worktree)
ruff check and ruff format --check pass on both files. mypy --strict org_review.py passes. lint-imports: 13 kept, 0 broken. tools/check_module_size.py exits 0, with org_review.py at 327/330. `pytest tests/unit/harness/pipelines -q`: 103 passed, no warnings.

### Strengths
- SQL stays verbatim, and each deviation is commented in the code.
- Reads are cached per run (one teams read shared by must_cover and deterministic_tasks, as the test asserts).
- The helpers are org-agnostic, ready for T06-11 to lift.
- Tests check real values: exact query_id lists, priorities and notes text.

### Issues
#### Critical
None.
#### Important
None.
#### Minor
1. herness/harness/pipelines/org_review.py:150 — `json.dumps(w["details"])` uses the default `ensure_ascii=True`, so an entity id or table name with non-ASCII characters is escaped to `\uXXXX` and never matches. Fix: `json.dumps(w["details"], ensure_ascii=False)` (or the core canonical_json helper).
2. herness/harness/pipelines/org_review.py:227-230 — the Python sort key `(-delta_usd, metric)` does not break ties between rows with the same metric and different target_kind, so notes and query_id order can vary between runs on the same build. Fix: add `str(r.get("target_kind"))` as a third sort key, or dedupe metric names in the notes line if the spec owner agrees (see ⚠️ 2).
3. herness/harness/pipelines/org_review.py:181 — `window_end` is stored but unused (SN10). Add a one-line comment saying it is kept for the U06-73 signature and get_pipeline parity, so a later reader does not remove it.

### Assessment
**Task quality:** Approved
**Reasoning:** U06-73 and U06-74 are implemented faithfully with minimal, justified deviations and the controller rulings are applied correctly. UT06-52 and UT06-53 cover every Expected clause, and all gates pass. The remaining items are Minor or spec-level questions.

# T04-07 review — Stage 400: change and work-item facts

**Verdict: Approved** (no Critical or Important findings; 3 Minor)

Reviewed: worktree agent-ac236b1768a14c0e9, head 2249bce (base f43b1f9). Read-only.

### Spec Compliance
- ✅ U04-44 `metrics.change_fact` (400_facts.sql:160-194): columns in design 04 §4.2 order (record_id, type, service_id, team_id, org_id, actual_end, deployed, failed, linked_incident_count, lead_time_h, + query_id from run_recorded). deployed = `actual_end IS NOT NULL AND coalesce(outcome,'') <> 'canceled'`; failed = `deployed AND (list_contains(d_failure_outcomes, coalesce(outcome,'')) OR linked_incident_count > 0)`; lead_time_h = seconds/3600.0 only when both NOT NULL and opened_at <= actual_end; org_id via LEFT JOIN core.team. `failed ⇒ deployed` holds by construction. No fan-out: `linked` is GROUP BY change_id, core.team join is the same pattern incident_fact already uses (400_facts.sql:89).
- ✅ Builder interpretation 1 (linked_incident_count counts only distinct, non-excluded incidents present in metrics.incident_fact, including those reached via core.incident.caused_by_change_id): matches U04-44 text "distinct non-excluded incidents (`metrics.incident_fact`) with caused_by_change_id = record_id or an enrich.incident_change_link row"; UNION + count(DISTINCT) de-duplicates an incident linked both ways (test C1/IA). Consistent with catalog metric 17 pair sources.
- ✅ Builder interpretation 2 (link score inclusive, `>=`): the spec says `score ≥ d_change_link_min_score`; also matches incident_fact.change_caused (400_facts.sql:70-71). Tested at exactly 0.7 (IB).
- ✅ d_change_link_min_score / d_failure_outcomes come from `p(...)` default binds (config catalog defaults), recorded in the evidence bind; UT04-35 asserts change_fact binds are exactly these two and work_item_fact none.
- ✅ U04-45 `metrics.work_item_fact` (400_facts.sql:195-235): §4.2 order; first_in_progress_at = min(at) FILTER to_category='in_progress'; done_at = CASE status_category='done' THEN coalesce(max done transition, resolved_at) (OI04-07: NULL when not currently done); cycle_days = seconds/86400.0 when both NOT NULL and done_at >= first_in_progress_at; is_unplanned = type='bug' (NULL→false) OR key in UNION(from_key, to_key) of link_type='mentions_incident'. `moves` grouped by record_id and `mentions` is a distinct UNION, so no fan-out; row count = core.work_item (tested).
- ✅ TH04-04: both statements are plain SELECTs materialized via the unchanged `_materialize` → run_recorded path (CTAS + shared result_hash + meta.evidence); no Python arithmetic; no data-built identifiers; no text column read (short_description/description/summary are populated with "free text" in the fixture and absent from the output column list). ST04-04 scan (tests/unit/metrics/test_metrics_catalog.py:510-521) includes 400_facts.sql and passes.
- ✅ Statement order: change_fact follows incident_fact, work_item_fact last; `_SHIPPED_TABLES` and the `# T04-07:` marker removed, `_problem` validates against full FACT_TABLES (facts.py:57-58); `_INPUT_TABLES` adds core.change, core.work_item_transition, core.work_item_link (facts.py:30-34).
- ✅ Acceptance: `materialize_facts` returns five query IDs (test_ut04_35_materialize_records_evidence asserts len 5 and one evidence row each); IDs stable across two runs (test_ut04_35_materialize_is_repeatable: second run == first, evidence count stays 5).
- ✅ Tests UT04-32 (x4) / UT04-33 (x3) in tests/unit/metrics/test_metrics_facts_delivery.py: pytestmark unit, UT-prefixed docstrings; cover deployed/failed/counts incl. excluded incident, unknown incident, below/at threshold, both-ways link; lead time 6.0/0.0/24.0 and the three NULL cases; min in_progress, max done, resolved_at fallback, not-done NULL, negative cycle NULL; unplanned via bug, from_key, to_key, other link types ignored, NULL type → false; column order/types; invariants; empty change input.
- ⚠️ Cannot verify in this review: BT04-01 (<90 s stage 400) at scale — builder's scratch probe reports 83.2 s on 5M incidents / 2M work items (little margin; hashing dominates, T04-05 scope). Uniqueness of core.team.team_id / core.work_item.key is assumed from stage 200 (same assumption as incident_fact).

### Gates (run by reviewer)
- `PYTHONUTF8=1 uv run pytest tests/unit/metrics tests/integration/metrics -q -p no:logging` → 516 passed, 1 xfailed (T04-08, expected).
- facts.py coverage: 100 % line, 100 % branch (88 stmts, 18 branches).
- `uv run ruff check .` clean; `ruff format --check` clean; `uv run mypy` no issues (226 files); `tools.check_module_size` exit 0.
- Budgets: 400_facts.sql 235/400, facts.py 148/150, render.py 299/300 (unchanged).

### Strengths
- SQL follows the column tables literally; CAST to DOUBLE before division avoids DECIMAL arithmetic; is_unplanned and linked_incident_count are never NULL.
- Fixture rows deliberately exercise every branch of both column tables, and invariants are asserted as SQL aggregates over the whole table.
- UT04-35 evidence/row-count check was rewritten so it still proves something for the empty change_fact.

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
1. tests/unit/metrics/test_metrics_facts.py:543 — the missing-input test only drops core.org; the three newly added `_INPUT_TABLES` entries (facts.py:31-32) are not exercised. A parametrize over the new inputs would pin the extension.
2. facts.py is at 148/150 lines — next edit will need a split; note for T04-08+.
3. BT04-01 margin (report Perf section): 83 s vs 90 s; track under T04-05 hashing / BT04-09 rather than here.

### Assessment
**Task quality:** Approved
**Reasoning:** Both statements match U04-44/U04-45 derivations and §4.2 order exactly, the builder's two interpretations are the literal reading of the spec, TH04-04 holds, five stable query IDs are proven by tests, and all gates pass with 100 % coverage of facts.py.

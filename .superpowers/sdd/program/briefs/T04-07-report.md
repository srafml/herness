# T04-07 report — Stage 400: change and work-item facts

Status: DONE_WITH_CONCERNS (concern: stage 400 wall time close to BT04-01 on large synthetic volumes, see Perf)
Worktree/branch: D:\herness\.claude\worktrees\agent-ac236b1768a14c0e9 / worktree-agent-ac236b1768a14c0e9 (base f43b1f9)
Commit: 2249bce feat(metrics): stage 400 change_fact and work_item_fact (T04-07) (wip checkpoint d131a2a was folded into it with a soft reset; all pre-commit hooks passed on both)

## What was built
- herness/model/sql/400_facts.sql (235 / 400): two new `-- @statement` bodies after metrics.incident_fact.
  - metrics.change_fact (U04-44): `cause` = UNION of (core.incident.caused_by_change_id, record_id) and
    (enrich.incident_change_link change_id, incident_id WHERE score >= p('d_change_link_min_score'));
    `linked` = count(DISTINCT incident) joined to metrics.incident_fact WHERE NOT excluded (so links to
    incidents missing from core.incident do not count); deployed = actual_end NOT NULL AND
    coalesce(outcome,'') <> 'canceled'; failed = deployed AND (list_contains(p('d_failure_outcomes'),
    coalesce(outcome,'')) OR linked_incident_count > 0); lead_time_h = date_diff seconds / 3600.0 when both
    NOT NULL and opened_at <= actual_end; org_id via LEFT JOIN core.team. Columns in design 04 §4.2 order;
    linked_incident_count CAST to INTEGER, 0 when none.
  - metrics.work_item_fact (U04-45): per-record min(at) FILTER to_category='in_progress' and max(at) FILTER
    to_category='done' from core.work_item_transition; done_at only when status_category='done' (last done
    transition, else resolved_at; OI04-07); cycle_days = seconds/86400.0 when both NOT NULL and done_at >=
    first_in_progress_at; is_unplanned = coalesce(type='bug', false) OR key in UNION(from_key, to_key) of
    mentions_incident links (never NULL). Columns in §4.2 order. No text column read (TH04-04; also the Jinja
    comments avoid the ST04-04 forbidden words).
- herness/metrics/facts.py (148 / 150): `_SHIPPED_TABLES` and its T04-07 marker removed; split_statements
  validates against the full FACT_TABLES; `_INPUT_TABLES` adds core.change, core.work_item_transition,
  core.work_item_link (core.team, core.work_item, core.incident, enrich.incident_change_link already there);
  materialize_facts docstring says five IDs. Coverage 100 % line/branch.

## Tests
- New tests/unit/metrics/test_metrics_facts_delivery.py (UT04-32 x4, UT04-33 x3): 6 changes / 5 incidents
  (one excluded) / 5 links (below/at threshold, excluded incident, unknown incident) and 6 work items / 11
  transitions / 4 links. Covers deployed/failed/linked_incident_count incl. excluded incidents and the 0.7
  threshold (>=), lead_time_h 6.0/0.0/24.0 and NULL (opened_at NULL, actual_end NULL, opened_at > actual_end),
  first_in_progress_at (min), done_at (max done, resolved_at fallback, not-done -> NULL), cycle_days incl.
  negative -> NULL, is_unplanned via bug and via mentions_incident from_key and to_key (other link types
  ignored), column order/types, row counts = source, invariants (failed => deployed, lead_time_h/cycle_days
  NULL or >= 0, counts >= 0 non-NULL), single query_id per table = query_ids[3]/[4], empty change input.
- tests/unit/metrics/test_metrics_facts.py (UT04-35) updated: GOOD/_stage texts carry five statements,
  len(query_ids) == 5, evidence count 5, all FACT_TABLES checked (evidence/row-count check rewritten so an
  empty table — change_fact on metrics_tiny — is covered), binds: change_fact uses exactly
  d_change_link_min_score + d_failure_outcomes, work_item_fact none.
- RED: `pytest tests/unit/metrics/test_metrics_facts_delivery.py tests/unit/metrics/test_metrics_facts.py`
  -> 17 failed, 21 passed (tables missing / three statements). GREEN: 38 passed.
- `PYTHONUTF8=1 uv run pytest tests/unit/metrics tests/integration/metrics -q -p no:logging` -> 516 passed,
  1 xfailed (T04-08). ST04-04 schema scan passes (had to drop the words from a Jinja comment first).
- Gates: ruff format/check clean, mypy clean on touched files, check_module_size exit 0; pre-commit hooks
  (incl. import-linter, type-ownership, pytest-unit) passed on the wip commit. No fixture CSV / CORE_DDL change
  (rows are inserted in the test), so the metrics_tiny DDL guard is untouched.

## Perf (BT04-01 / BT04-09 carry-over)
Scratch probe (not committed): metrics_tiny DDL, synthetic rows via DuckDB range(): 5,000,000 core.incident
(2 % excluded, 5 % caused_by_change_id), 2.5M cluster_member, 1,000,000 core.change, 1,000,000
incident_change_link, 2,000,000 core.work_item (parent_key hierarchy), 6,000,000 transitions, 200,000
work_item_links; full materialize_facts (run_recorded incl. parallel hash + evidence), duration_ms from the
metrics.facts.materialized log:
- change_fact 1M rows: 5.7 s; work_item_fact 2M rows: 13.5 s (the two new statements: ~19 s).
- org_closure 0.04 s; work_item_closure 5.0M rows 12.9 s; incident_fact 5M rows 51.0 s.
- stage 400 total 83.2 s vs BT04-01 < 90 s (met, but little margin; incident_fact was 36-39 s in T04-06's run,
  so machine load varies). Hashing dominates as in T04-06; not optimised here per the dispatch.

## Deviations / spec notes
- linked_incident_count joins the union of cause pairs to metrics.incident_fact (non-excluded), so a link or
  caused_by to an incident id not in core.incident is not counted — per the U04-44 wording "(metrics.incident_fact)".
- Link threshold is inclusive (score >= d_change_link_min_score), as in incident_fact.
- is_unplanned is coalesced to false for NULL type (BOOLEAN column never NULL).
- New test file instead of growing test_metrics_facts.py (already 560 lines).

## Concerns
- Stage 400 on 5M incidents + 2M work items measured 83.2 s against the 90 s BT04-01 budget; with larger
  work-item volumes (closure + fact both scale with work items) the budget could be exceeded. The fix lever is
  T04-05 hashing (out of scope).
- facts.py 148/150.

## Fix round 1 (review Minor 1)
- 534ebc9 test(metrics): cover new stage 400 inputs in missing-input check (T04-07): test_ut04_35_missing_input_raises parametrized over core.org, core.change, core.work_item_transition, core.work_item_link (also asserts no meta.evidence row). Test-side only; facts.py unchanged (148/150). tests/unit/metrics: 518 passed, 1 xfailed; ruff/mypy clean; hooks passed.

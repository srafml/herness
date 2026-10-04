# T04-11 report: Delivery metrics #20-#26 (catalog complete, 28 metrics)

Status: DONE_WITH_CONCERNS (a spec-text bug fixed in the macro, see Deviations). Checkpoint 7fc22f8 (wip); final commit 502e590 feat(metrics): delivery metrics #20-#26 (T04-11), all pre-commit hooks passed incl. pytest-unit, no SKIP.

## Implemented
- config/metrics.yaml: #20 throughput, #21 cycle_time_days, #22 carryover_rate, #23 backlog_age_days,
  #24 wip_count, #25 unplanned_work_ratio, #26 epic_predictability per U04-48 tables A and B, inserted
  between #19 and #27. Each is one recorded SELECT ending filter_clause / entity_filter / GROUP BY ALL.
  No Python arithmetic. #22-#24 use CROSS JOIN period_spine() with cat_at_join/cat_at (names cs, ce, cn);
  #22 builds its "not done at end" test with a template-local `{% set OPEN %}`. #26 joins a per-epic
  derived table over metrics.work_item_closure (depth >= 1, story/bug/task, created_at <=
  coalesce(first_in_progress_at, created_at)) giving committed_pts and done_pts (CASE, so an epic with
  nothing done gives 0, not NULL); value = sum(done)/sum(committed), sample_size = count(DISTINCT e.record_id).
- Carry-overs closed: scoring.org.metrics is now the full design 04 §7.1 scorecard (added cycle_time_days
  0.10, unplanned_work_ratio 0.10, epic_predictability 0.05; sum 1.0, checked against §7.1). All `T04-11`
  markers removed (metrics.yaml header + scoring comment, test_metrics_settings.py, test_metrics_render.py,
  test_metrics_catalog.py). Settings test restored to the full scorecard and sum 1.0 (settings.py has no
  sum validator; the test asserts it). UT04-13 expects all 28 names. UT04-26 now asserts the shipped
  scorecard equals the §7.1 one instead of injecting it. No other T04-09/10/11 markers in the tree.
- Budget ruling applied: config/metrics.yaml 782 lines -> budget 790 (raised from 760), mirrored in the
  impl 04 §2 row in the same change with the note "budget raised from 760 (T04-11 ruling): 16 required
  metadata keys per entry, anchors rejected by the config loader". The docs edit was allowed.
  Note: tools.check_module_size only enforces `.py` rows, so the yaml budget is not machine-checked.

## Files changed
- config/metrics.yaml (596 -> 782; budget 790)
- herness/metrics/sql/_macros.sql.j2 (138 -> 138; `"at"` quoted in cat_at_join, no new macro)
- docs/impl/04-metrics-and-scoring.impl.md (§2 row only)
- tests/fixtures/metrics_tiny/core.work_item.csv (+story_points column, +W7..W11)
- tests/fixtures/metrics_tiny/core.work_item_transition.csv (new, 12 rows)
- tests/fixtures/metrics_tiny/core.work_item_link.csv (new, 1 row: PAY-10 mentions INC0001)
- tests/support/metrics_oracle.py (new, 427 lines, test-side, no budget)
- tests/unit/metrics/test_metrics_delivery.py (new: UT04-55..61, 11 tests)
- tests/unit/metrics/test_metrics_catalog_props.py (new: PT04-03, PT04-04)
- tests/unit/metrics/test_metrics_catalog.py (UT04-13 names), test_metrics_settings.py (full scorecard),
  test_metrics_render.py (UT04-25 quoted "at"; UT04-26 shipped scorecard),
  test_metrics_facts.py (UT04-34 closure rows for W7..W11), test_metrics_facts_delivery.py (DELETE list
  also clears core.work_item_transition/core.work_item_link; assertions unchanged).
Fixture rows: 48 -> 66 (> 60 guide, allowed by ruling). #1-#19/#27/#28 expectations unchanged.

## Fixture (America/New_York; T1/S1 in O2, T2/S2 in O3; O3 < O2 < O1)
W3 story (W2 epic) T1 in_progress 12-15; W5 epic T2 01-05 -> 03-20; W6 story (W5) T2 todo, created 12-06 09:00;
W7 epic (W1) T1 01-10 -> 03-01; W8 story (W5) T2 12-20 -> 02-10 (52 d), 3 pts; W9 bug (W7) T1 created 12-15
09:00, 02-15 -> 02-20 (5 d), 2 pts; W10 task (W7) T1 created 01-12, done 02-01 (no cycle), unplanned by link;
W11 story (W7) T1 12-28 -> 03-05 (67 d).

## Expected values (compute_metric, quarter, every declared grain, unfiltered and with S/T/O(/W) filters)
Q1 = 2026-01-01; Q4 = 2025-10-01 spine row (snapshot 2026-01-01 00:00 -05). Tuples are (value, num, den, n).
- UT04-55 throughput (min 1): S1/T1 3; S2/T2 1; O1/O2 4; O3 1; work_item W1/W7 3, W5/W8/W9/W10/W11 1.
  Filter work_item_type=bug -> T1 1.
- UT04-56 cycle_time_days (shipped 10 -> NULL checked; min 1): S1/T1 36 (NULL,2,2); S2/T2/O3 52 (n1);
  O1/O2 52 (n3); W1/W7 36, W5/W8 52, W9 5, W11 67.
- UT04-57 carryover_rate (shipped 10 checked): T1 1/2; T2 0/1; O1/O2 1/3; O3 0/1; W1 1/2, W2/W3 1/1,
  W5/W7/W8/W11 0/1. Month spine at O1: Jan 3/3, Feb 2/3, Mar 1/2.
- UT04-58 backlog_age_days (shipped 10 checked): Q4 S1/T1 16.625, S2/T2/O3 25.625, O1/O2 21.125 (n2);
  Q1 S2/T2/O1/O2/O3 116 d - 10 h = 115.5833; work_item Q4 W1/W7/W9 16.625, W5/W6 25.625; Q1 W5/W6 115.5833.
- UT04-59 wip_count (min 1): Q4 T1 2, T2 1, O1/O2 3, O3 1; Q1 T1 1, O1/O2 1; work_item Q4 W1 2,
  W2/W3/W5/W7/W8/W11 1; Q1 W1/W2/W3 1. t12w -> one row 2026-01-07, value 1.
- UT04-60 unplanned_work_ratio (shipped 20 checked): S1/T1 2/3; S2/T2/O3 0/1; O1/O2 2/4.
- UT04-61 epic_predictability (shipped 3 checked): T1 2/3 (2,3,1); T2 3/4 (3,4,1); O1/O2 5/7 (5,7,2);
  O3 3/4; W1/W7 2/3, W5 3/4. An epic with no committed child is skipped (tested).

## PT04-03 / PT04-04 scope; metrics_oracle coverage
- tests/support/metrics_oracle.py: pure-Python table-A formulas over fact rows (grain mapping incl. org
  closure and work-item ancestors, period/spine arithmetic in the business tz, cat_at with the done >
  in_progress > todo tie rule, interpolated median). Covers 20 metrics: incident #1-#4, #8-#11, #13;
  change #15, #16, #18, #19; delivery #20-#26. Not covered (they need core/enrich joins or weight binds):
  #5, #6, #7, #12, #14, #17, #27, #28; these keep their hand-computed UTs.
- PT04-03: random incident/change/work-item fact rows (+transitions, closure) written straight into the
  fact tables; a random covered metric x declared grain x period (week, month, quarter, t12w, t12m) must
  equal the oracle row for row (value, numerator, denominator, sample_size) with min_n 1, and satisfy
  durations >= 0, ratios in [0,1], count/snapshot values non-negative integers. Mutation check: dropping
  'task' from the oracle's types made it fail.
- PT04-04: counts/sums (incident_count, p1p2_count, throughput, toil_hours_est) at quarter = sum of month
  values; ratios/means (mttr_hours, repeat, reopen, sla, cfr, emergency, unplanned): quarter num/den = sums
  of month num/den and value = their quotient. Asserts the default quarter and month windows coincide.
  Excluded: change_count (calendar-week denominator; months without deployments have no row, so the
  weeks do not add up; found by Hypothesis), spine metrics and medians (not additive).
- Runtime at the commit profile (200 examples): PT04-03 ~13 s, PT04-04 ~18 s.

## Sizes / checks
- metrics.yaml 782 / 790 (new). _macros.sql.j2 138 / 260 (unchanged count). render.py 299 untouched.
- Longest rendered SQL: 3,020 chars (carryover_rate / work_item / quarter, all filters + entity_ids).
  Per new metric max: carryover 3,020; backlog 2,546; wip 2,495; epic 2,216; unplanned 1,748; cycle 1,732;
  throughput 1,691. Far under 20,000.
- validate_catalog on the full shipped catalog: 0 errors, 2 warnings. Both are the pre-existing
  "usd_model unused" for mttr_p50_hours and mttr_business_hours (table B gives them usd_model mttr, but the
  §7.1 scorecard does not include them). The full catalog does NOT clear them; they follow the spec and are
  not a defect of this card. "No issues" therefore holds for errors only.
- 28-metric compute time on metrics_tiny (every metric at every declared grain, quarter, shipped catalog,
  97 compute_metric calls, one connection): 3.5-4.8 s (~40-50 ms per call).
- Gates: ruff format/check clean, mypy clean, lint-imports 13 kept, check_type_ownership 0,
  check_module_size 0, --require-test-ids ok. tests/unit/metrics + tests/integration/metrics: 597 passed;
  tests/fault/metrics 2 passed.
- RED: test_metrics_delivery.py before the entries -> ToolInputError "unknown metric throughput" (all 10).
  First GREEN attempt: DuckDB "syntax error at or near ','" in cat_at_join (bare `at`), fixed below.

## Deviations / spec notes
- The U04-35 cat_at_join spec text `SELECT record_id, at, ... GROUP BY record_id, at ... >= <name>.at` does
  not parse in DuckDB (`at` is a keyword; the core DDL quotes it). The macro now emits `"at"` in all three
  places, and UT04-25's expected text is updated. The spec text in docs/impl §U04-35 still shows bare
  `at`; a spec erratum is suggested (not edited: only the §2 row was in scope).
- #20/#21/#25: `done_at IS NOT NULL` is implied by the window range predicate, not written separately.
- #22 numerator filter coalesce(cat_at(end_ts), '') <> 'done' is written once via `{% set OPEN %}`.
- UT04-34 closure expectation extended with the new items (fact test, not a metric expectation).

## Concerns
- validate_catalog keeps its 2 pre-existing warnings (see above); the controller decides whether
  "no issues" means errors only.
- Spec erratum for cat_at_join `at` quoting (above).
- The config/metrics.yaml line budget is not enforced by tools.check_module_size (.py only).


## Fix round 1 (review T04-11-review.md, base 502e590)
1. epic_predictability NaN: value is now `sum(k.done_pts) / nullif(sum(k.committed_pts), 0)`, so committed
   scope that is all 0 story points gives value NULL (numerator 0, denominator 0) instead of NaN.
   - New UT04-61 test `test_ut04_61_zero_committed_points_is_null`: W6/W8 set to 0 points; T2 -> (None, 0, 0, n 1),
     T1 2/3 unchanged, O2 2/3 over 3 points (n 2). RED without the fix: `('T2', nan, 0.0, 0.0, 1)`.
   - PT04-03: story_points strategy now includes 0.0, and the oracle returns None for a zero committed sum.
     The random draw almost never reaches "done epic whose committed children are all 0 points" (with the
     fix reverted, 200 random examples still passed), so I added a fixed PT04-03 case
     `test_pt04_03_zero_point_epic_matches_oracle` (SQL = oracle at every grain and period, value NULL).
     It fails without nullif.
   - Other catalog denominators checked:
     - `count(*)` (all ratios, coverage): >= 1 per group, so no zero.
     - change_count weeks: every row has a deployment day d with greatest(ps, window_start_date) <= d <
       least(period_end, window_end_date), so weeks >= 1/7 and never 0.
     - #27/#28 `sum(m.v * m.rc) / sum(m.rc)`: evaluated only when bool_and(rc > 0) over >= 1 pair, so
       sum(rc) > 0; otherwise avg(v).
     - mttr/mtta/durations use avg/median (no division by a group count that can be 0).
     - #26 was the only template that could divide by zero.
2. PT04-04 comment added: customer_impact_minutes, incident_cost_usd and change_caused_incident_count are
   additive but excluded because the generator does not fill the columns they read (impact_h,
   impact_estimated, total_usd; core.incident / enrich.incident_change_link causes).
3. max_examples capped for PT04-03/04: `min(settings().max_examples, 300)` (commit profile keeps 200;
   nightly becomes 300, verified by importing the module under the nightly profile).
4. Oracle split: tests/support/metrics_oracle.py 427 -> 306 lines; fact row and Frame types moved to
   tests/support/_metrics_oracle_types.py (129 lines), re-exported from metrics_oracle.

Tests: tests/unit/metrics + tests/integration/metrics + tests/fault/metrics 601 passed. ruff format/check,
mypy, lint-imports (13 kept), check_type_ownership 0, check_module_size 0, --require-test-ids ok.
metrics.yaml still 782 / 790.
Fix round 1 commit: ffb15eb fix(metrics): epic_predictability zero-point NaN (T04-11 review); all hooks passed incl. pytest-unit, no SKIP. (A first attempt ran the hooks but git aborted because another agent deleted the shared-scratchpad message file; retried with a per-agent subfolder.)

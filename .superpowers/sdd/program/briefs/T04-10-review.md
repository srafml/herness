# T04-10 review — Change and monitoring metrics #15–#19, #27–#28 + period_end_date

Reviewed: worktree agent-a2c8bd85a91038b5b, head 6667ce3 (base 6f70ffe). Read-only.

**Verdict: Approved**

### Spec Compliance
- ✅ Spec compliant.
  1. ✅ Table A/B for all 7 metrics (config/metrics.yaml:379-569):
     - Anchors: #15/#16/#18/#19 on `c.actual_end` (TIMESTAMPTZ, window_start/window_end); #17 on `f.opened_at`; #27/#28 on the DATE `m.date` with `window_start_date`/`window_end_date` and `period_start_date('m.date')`.
     - Populations: `c.deployed`; #17 `NOT f.excluded`; #27/#28 metric_name pairs.
     - value, numerator, denominator and sample_size match column for column.
     - #18 has `coverage` and requires_columns `core.change.opened_at`.
     - Table B metadata matches: domain, grains, aggregation, unit, better, min n, usd_model `cfr`, filters S T O X / S T O, owner, enabled.
     - `unweighted`: #27 emits it only at org grain, as table A says. #28 emits it at every grain. That matches table A ("all grains: weighted rule of #27"; "`unweighted` as #27") and design §5.2 row 28, because #28 weights at the service grain too.
     - #17 pairs come from `core.incident.caused_by_change_id` UNION `enrich.incident_change_link` with `score >= p('d_change_link_min_score')` (:453). The entity comes from the change, the filter is `filter_clause('change','c')` and the value is `count(DISTINCT f.record_id)`.
     - #15 weeks follow DD04-05 verbatim. `any_value(WEEKS)` (:398-399) is exact. WEEKS depends only on PS, which is the grouped period_start expression, and on constant binds, so it is constant within each (entity, period_start) group. Partial edges are tested with a custom month window [01-05, 03-01): Jan 27/7 weeks, Feb 4. t12w (01-07..04-01) gives 12 weeks, 3/12, because PS and period_end_date collapse to window_start_date and window_end_date. t12m reduces the same way.
  2. ✅ `period_end_date` (_macros.sql.j2:83-87): week/month/quarter → `CAST(ps + INTERVAL 1 <period> AS DATE)`; t12w/t12m → `p('window_end_date')`. Tested at all five periods, including the used-bind set. render.py is untouched at 299/300. _macros is 138/260.
  3. ✅ Each metric is one recorded SELECT ending with filter_clause, entity_filter and GROUP BY ALL. There is no Python arithmetic and no compute.py/render.py change. `validate_catalog` on the shipped file (21 metrics) gives 0 errors and only 2 pre-existing warns (mttr_p50/business usd_model unused). UT04-13 also checks it.
  4. ✅ I hand-checked every expected value against the new CSVs:
     - Changes: CH1-4 are deployed; CH5 has no actual_end; CH6 is canceled. CH1 and CH4 fail via I3's links (0.8/0.9 ≥ 0.7) and CH2 fails via `unsuccessful`. T1 is in O2, T2 in O3, and O3 is under O2 under O1.
     - change_count: S1 3/(90/7), O1 4/(90/7).
     - change_failure_rate: 2/3, 1/1, 3/4.
     - change_caused_incident_count: 1 per entity via I3, DISTINCT at O1/O2; after I1→CH2 it is 2 at S1/T1/O1/O2; I4 is excluded.
     - change_lead_time_hours: median(6, 2) = 4, cov 2/3; O1 median(2, 6, 24) = 6, cov 3/4.
     - emergency_change_ratio: 1/3, 0/1, 1/4.
     - availability_pct: S1 99.25; O1 495500/5000 = 99.1; team T1 = 398500/4000 = 99.625; with request_count dropped, unweighted 295.5/3 = 98.5.
     - error_rate: 50/4000 = 0.0125, 100/5000 = 0.02; with request_count dropped, 0.015 and 0.08/3.
     - Every declared grain is run unfiltered and with a keep-all filter. Where the shipped min_sample_size (10 or 7) would hide the values, the test first shows NULL plus `insufficient_sample`, then lowers the minimum and checks the value.
     - Fixture edits are neutral for #1-#14. I3 was already change_caused via the 0.8 link, and the suite passes unchanged. In test_metrics_facts_delivery.py, :99 adds core.change to the pre-clear list, because the new fixture rows would otherwise mix with the test's own CHANGES. :208 deletes core.change so the empty-input case stays empty. The assertions are identical in both.
  5. ✅ Scorecard `change_failure_rate: 0.15` (metrics.yaml:582) matches design §7.1 (spec line 522). The partial sum is 0.75. Marker comments now name only T04-11's weights (cycle_time_days 0.10, unplanned_work_ratio 0.10, epic_predictability 0.05).
  6. ✅ Evidence SQL is asserted `< 20_000` for every grain and filter case. The longest reported is ~2.6k.
- ⚠️ Cannot fully verify from the diff:
  - Headroom for T04-11 in config/metrics.yaml: 596/760 leaves 164 lines for #20-#26. The builder notes that YAML anchors are rejected by the _StrictLoader. This needs a budget ruling or a macro-extraction plan before T04-11. It is not a T04-10 defect.
  - The metrics_tiny fixture is now 48 rows (the report says 47), against the 60-row cap in design §10.1. That leaves about 12 rows for T04-11's work-item transitions and epics.

### Tests run
`PYTHONUTF8=1 uv run pytest tests/unit/metrics tests/integration/metrics -q -p no:logging` gives **585 passed** (92.6s). The only noise is uv's `VIRTUAL_ENV` mismatch warning. That comes from the environment, not the code.

### Strengths
- The #15 any_value workaround is justified and proven exact, and the tests cover partial and rolling windows.
- The #17 tests exercise both pair sources, dedup across entities, the excluded-incident path, the sub-threshold link and the change_type filter applied to the causing change.
- The #27/#28 tests cover the weighted-to-unweighted fallback and the owner-team filter behaviour, where S2 has no owner row.

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
- config/metrics.yaml:537, :568: `m.v IS NOT NULL` drops (date, service) pairs whose metric rows all have NULL values. Table A counts pairs, so the unweighted denominator could differ in that edge case. The builder documents this as a deviation, and it is semantically sensible. Consider recording it in the spec deltas.
- config/metrics.yaml:508-538: at service grain, #27 renders `CASE WHEN false THEN … ELSE avg(m.v) END`. It is harmless, but a service-grain branch would read more cleanly. Style only.
- tests/unit/metrics/test_metrics_change.py (UT04-52): no case has a link score exactly equal to `change_link_min_score`, so the `>=` boundary is not exercised. The current links are 0.5, 0.8 and 0.9.

### Assessment
**Task quality:** Approved
**Reasoning:** All seven entries and the period_end_date macro match U04-48 tables A/B and U04-35. Every hand-computed value checks out against the new fixtures. The shipped catalog validates cleanly, and the metrics suite passes (585).

# T04-09 review — Ops metrics #5–#14 (head 6f70ffe, base a46205f)

**Verdict: Approved**

### Spec Compliance
- ✅ 1. Table A/B, #5–#14: every entry matches, checked field by field. Source/anchor: resolved_at for #5, #9–#11, #13; opened_at for #6, #7, #8, #14; e.ts for #12. Populations: NX / resolved / `service_id IS NOT NULL` / `sla_breached IS NOT NULL` / `list_contains(d_noise_severities, e.severity)`. value/num/den/n all match. Optional columns: coverage on #5 and #6, estimated_count on #7. Metadata also matches: grains (no cluster for #6 or #12), aggregation, unit, better, min n (10/10/1/20/20/20/20/50/1/1), estimate (yes only for #13 and #14), uses_weights, usd_model (mttr/repeat/reopen/reassign/sla/noise), filters (PSTOC; PSTO for #6; STOV for #12), requires_columns `[core.incident.acknowledged_at]` on #6, owner sre-analytics, enabled true.
- ✅ 2. Each metric is one SELECT in the YAML with no Python arithmetic. Every template uses entity_col/entity_join, period_start, p('window_*'), filter_clause, entity_filter and GROUP BY ALL. The #6 join alias `i` does not collide with the macro aliases (oc_/sv_/ow_/wc_ prefixes).
- ✅ 3. Catalog load: UT04-13 validates the shipped file with validate_catalog (no errors) plus load_catalog names #1–#14. It passes in the suite run below.
- ✅ 4. Values checked by hand against the fixture CSVs:
  - #5: resolve_bh = 2, 4, 6, giving 4.0 (12/3, n=3).
  - #6: I1 +15 min, I2 +45 min, I3's ack comes before open so it is NULL, I4 is excluded. Result 30.0 (60/2, n=2), coverage 2/3, flagged low_coverage.
  - #12: noise severities are critical/major/minor/warning. That gives E1–E5 (E6 is info). E2, E4 and E5 have no incident, so 3/5.
  - #7–#11, #13, #14 equal the design §10.1 values (150, 1/3, 1/3, 2/3, 1/3, 6.8, 20680). toil = 2×1.5 + 4×0.2 + 6×0.5 against weights.yaml effort_factor.
  - Every declared grain is covered, with and without filters. Where the fixture n is below the shipped min_sample_size, the test first asserts the shipped NULL + insufficient_sample result and then lowers the minimum.
  - The fixture changes (acknowledged_at column, core.event.csv, core.service_map.csv) did not touch the #1–#4 expectations: test_metrics_compute.py is unchanged and passes.
- ✅ 5. Scorecard weights match design §7.1 (mttr 0.15, repeat 0.15, sla 0.10, reopen 0.05, reassign 0.05, noise 0.10; partial sum 0.60). The marker comments at config/metrics.yaml:389 and test_metrics_settings.py list only change_failure_rate (T04-10) and cycle_time_days, unplanned_work_ratio, epic_predictability (T04-11).
- ✅ 6. TH04-04: the output columns are numeric aggregates only. The text columns severity and incident_id appear only in WHERE/FILTER predicates, never in the SELECT list.
- ✅ 7. render.py and _macros.sql.j2 are unchanged (diff stat: 7 files, none under herness/). config/metrics.yaml is 405/760 lines. check_module_size rc=0.
- ⚠️ config/metrics.yaml line budget (forward risk, not this card's defect): #5–#14 took about 26 lines per entry. The 355 remaining lines leave about 25 per entry for 14 metrics, and several are larger (#17, #22–#24, #26–#28). T04-10/T04-11 will likely go over 760 unless they compact the metadata, for example with flow-style mappings or several keys per line.

### Runs
- `PYTHONUTF8=1 uv run pytest tests/unit/metrics -q -p no:logging`: 571 passed (88.7 s).
- `uv run pytest tests/fault/metrics tests/integration/metrics -q`: 3 passed. These are the other consumers of the metrics_tiny fixture.
- `uv run python -m tools.check_module_size`: rc=0.

### Strengths
- The templates match the #1–#4 style exactly. The ratio metrics use `count(*) FILTER (...)`, and the population predicates are copied from the spec.
- The tests cover the optional-column flag paths (low_coverage, estimate) with in-test UPDATEs, without changing the §10.1 fixture rows.
- #11's NULL-sla exclusion and #12's severity filter are exercised directly.

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
1. config/metrics.yaml:160: the template-local `{%- set ACK = "..." %}` (135 chars) repeats the CASE expression 5 times in the rendered SQL. This is acceptable. It renders in the sandbox, adds no new identifiers or bind parameters, and keeps _macros.sql.j2 (owned by T04-10) untouched. It stays readable because it is defined once. If another metric needs a guarded duration, T04-10 should promote it to a macro.
2. tests/unit/metrics/test_metrics_ops.py:42: ALL_FILTERS uses all-inclusive values, so the "with every allowed filter" pass proves only that the filters bind and render, not that they narrow results. The only narrowing check is #12 `severity=major` (line 211). Narrowing per filter is presumably covered by the T04-08 request-path tests, so this is not blocking.
3. tests/unit/metrics/test_metrics_ops.py:116: `_shipped_insufficient` checks only the team grain. That is enough, since the min_sample gate does not depend on grain.

### Assessment
**Task quality:** Approved
**Reasoning:** All ten entries match U04-48 tables A and B exactly as recorded, grain-agnostic SELECTs. The UTs assert the §10.1 values and values hand-verified from the fixture at every declared grain, and the scorecard weights and markers are correct. The only open item is a forward line-budget risk for T04-10/11.

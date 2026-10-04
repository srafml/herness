# T04-11 review: Delivery metrics #20-#26 (head 502e590, base 6667ce3)

### Spec Compliance
- ✅ 1. Table A #20-#26: populations, anchors and columns match. #20/#21/#25 anchor `w.done_at` in window, types story/bug/task (the `done_at IS NOT NULL` test is implied by the range predicate). #21 denominator/sample_size = count after `cycle_days IS NOT NULL`. #22-#24 `CROSS JOIN period_spine()` with `cat_at_join`/`cat_at` named `cs`/`ce`/`cn`; `period_start = spine.period_start`; #22 numerator `coalesce(cat_at(end_ts),'') <> 'done'`. #26 follows §5.2.1: `work_item_closure` depth >= 1, story/bug/task, `created_at <= coalesce(first_in_progress_at, created_at)`, `pts = coalesce(story_points,1)`, done = `done_at <= e.done_at`, inner join skips epics with no committed child, value = Σdone/Σcommitted, sample_size = count(DISTINCT e.record_id). Table B metadata for all 7 entries (domain, grains, aggregation, unit, better, min n, owner, filters incl. #26 without W) matches exactly.
- ✅ 2. Each entry is one recorded SELECT ending `filter_clause` / `entity_filter` / `GROUP BY ALL`. No Python arithmetic. Grain and window come from `compute_metric` (T04-08 path). All 28 load. I ran `validate_catalog` on the shipped file: 0 errors, 2 warnings (`usd_model unused`: mttr_p50_hours, mttr_business_hours). **Judgement: this is a spec conflict, not a card defect.** Table B gives #4/#5 `usd_model: mttr`, the §7.1 scorecard leaves them out, and U04-26 step 10 warns on exactly that combination. No compliant catalog can clear both warnings. The acceptance check is met for errors. Record it as a spec erratum: either drop usd_model from #4/#5 in table B, or narrow step 10.
- ✅ 3. Quoting `"at"` in `cat_at_join` (`herness/metrics/sql/_macros.sql.j2:129`) is correct and necessary. I confirmed that DuckDB 1.5.5 rejects bare `at` (`Parser Error: syntax error at or near "from"`). The macro had no working caller before this card, so nothing regresses. UT04-25 has been updated, and the full metrics suite passes.
- ✅ 4. I recomputed every UT04-55…61 expectation by hand from the fixture CSVs for every declared grain and both Q4/Q1 spine rows: throughput, cycle 36/52/67, carryover 1/2, 0/1, 1/3 plus month 3/3, 2/3, 1/2, backlog 16.625/25.625/21.125/115.583, WIP 2/1/3 and t12w 1, unplanned 2/3, 0/1, 2/4, epic 2/3, 3/4, 5/7. All are correct. The shipped min_n NULL + `insufficient_sample` is checked before min_n is lowered. The fixture changes only touch work_item, transition and link tables. Other test edits are neutral: the UT04-34 closure rows are extended, and facts_delivery also clears the two new tables.
- ✅ 5. PT04-03 and PT04-04 hold as specified. `tests/support/metrics_oracle.py` is an independent pure-Python implementation with its own grain, spine, cat_at and median logic, so it is not a re-render of the SQL. Coverage of 20/28 is justified: the 8 uncovered metrics need core/enrich joins or weight binds and keep their hand-computed UTs. The PT04-04 exclusions are justified: change_count uses a calendar-week denominator and is not a count, wip is a snapshot, and spine metrics and medians are not additive. Runtime at the commit profile is 30.3 s (PT04-04 18.0 s, PT04-03 12.4 s).
- ✅ 6. The org scorecard is exactly the §7.1 list and sums to 1.0. A grep of config/, tests/ and herness/ finds no `T04-09/10/11` markers. UT04-13 names all 28 metrics. The §2 row now reads 790 with the reason, and metrics.yaml is 782 lines.
- ✅ 7. render.py is unchanged at 299 lines. The longest rendered SQL is 3,020 chars, well under 20,000, and `_check` asserts `< 20_000` on every call.
- ⚠️ Could not verify from the diff: the builder's measured longest-SQL figure (3,020). It is only bounded by the UT assert.

Tests run: `pytest tests/unit/metrics tests/integration/metrics tests/fault/metrics` → 599 passed in 142 s.

### Strengths
- The hand-computed fixture is designed with care. W10 is uncommitted because it was created after the epic started, W11 is committed but finishes after the epic, and W10 is unplanned via a link. Every branch of #22-#26 is exercised.
- The oracle is genuinely independent, and the builder ran a mutation check against it.
- The quoting fix is minimal and the deviation is documented.

### Issues
#### Critical (Must Fix)
- none

#### Important (Should Fix)
- `config/metrics.yaml` epic_predictability `sql` (the `sum(k.done_pts) / sum(k.committed_pts)` line, ~yaml:678): when every committed child of an entity's epics has `story_points = 0` (common in Jira), the value is **NaN**. This breaks the U04-48 postcondition "every ratio in [0, 1]". I reproduced it: on metrics_tiny with W6/W8 set to 0 points, T2 returned `value=nan, numerator=0.0, denominator=0.0, flags=[]`, and the row was recorded and hashed. Fix: use `/ nullif(sum(k.committed_pts), 0)`, so the value becomes NULL. Add a UT case, and add `0.0` to the PT04-03 story_points strategy (`tests/unit/metrics/test_metrics_catalog_props.py:143`) with an oracle that returns None. The generator currently leaves out 0, which is why the property test did not catch this.

#### Minor (Nice to Have)
- `docs/impl/04-metrics-and-scoring.impl.md` U04-35 still shows bare `at` in `cat_at_join`. It needs a spec erratum to match the macro and UT04-25 (the builder flagged this).
- Spec conflict: table B vs §7.1 vs U04-26 step 10 (the 2 `usd_model unused` warnings; see item 2). The controller should rule and record it as an erratum.
- PT04-04 `SUMMED` (`test_metrics_catalog_props.py:60`) leaves out the count/sum metrics `customer_impact_minutes`, `incident_cost_usd` and `change_caused_incident_count`. PT04-04 does not need the oracle; the real limit is the generator, which does not fill `impact_h`/`total_usd`/core links. This is acceptable, but a comment should say so.
- The PT04-03/04 commit-profile cost of about 30 s lands on the pre-commit pytest-unit hook. The nightly profile, at 10k examples, would be about 25 min for these two tests. Consider a lower `max_examples` for these tests.
- `tests/support/metrics_oracle.py` is 427 lines, over the ENG 400-line module limit. It is test-side and has no §2 budget, so this is noted only.
- `config/metrics.yaml`'s 790-line budget is not machine-checked, because `check_module_size` only checks `.py` files (the builder noted this).

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The spec match, hand values, oracle and carry-overs are all sound. One real defect remains: epic_predictability emits NaN for zero-point committed scope, which breaks the ratio postcondition. It is a one-line `nullif` fix plus tests. The validate_catalog warnings are a spec conflict and do not count against the card.

## Re-review round 1 (head ffb15eb, base 502e590)

1. ✅ Important — epic_predictability NaN: `config/metrics.yaml:679` now `sum(k.done_pts) / nullif(sum(k.committed_pts), 0)`; numerator/denominator columns unchanged (0/0), so flags logic untouched. DuckDB check: `sum(0.0)/sum(0.0)` -> nan, with nullif -> NULL, confirming both the bug and the fix. New `test_ut04_61_zero_committed_points_is_null` (tests/unit/metrics/test_metrics_delivery.py) pins T2 -> (None, 0, 0, n 1) and O2 2/3 over 3 pts (n 2); without nullif T2 value would be nan and the `None` equality fails. PT04-03: story_points strategy includes 0.0; oracle `_epic_metric` returns None when c == 0; fixed case `test_pt04_03_zero_point_epic_matches_oracle` asserts SQL == oracle at every grain x period and `(None, 0.0, 0.0)` directly, so it would fail without the fix.
2. ✅ Other denominators spot-checked in config/metrics.yaml: all `/ count(*)` ratios (137, 164, 215, 241, 267, 293, 319, 421, 477, 500, 575, 653) are grouped aggregates over >= 1 row; change_count (395-398) weeks = days between greatest(ps, ws_date) and least(pe, we_date) for a group that contains a deployment inside that range, so > 0; #27/#28 (714, 745) divide by sum(m.rc) only under `bool_and(coalesce(m.rc > 0, false))`, so sum > 0; 602 divides by a constant. #26 was the only zero-denominator path.
3. ✅ PT04-04 exclusion comment present (test_metrics_catalog_props.py, above SUMMED/RATIOS).
4. ✅ `MAX_EXAMPLES = min(settings().max_examples, 300)` evaluated at module import, after conftest `load_profile("commit")` and Hypothesis' pytest_configure profile switch, so commit stays 200 and nightly (10,000) is capped to 300; derandomize still inherited from the profile.
5. ✅ Oracle split: tests/support/metrics_oracle.py 306 lines, tests/support/_metrics_oracle_types.py 129 lines; `metrics_oracle.__all__` still exports Change/Facts/Frame/Incident/WorkItem/COVERED/OracleRow/oracle (import checked). tests/support is collect_ignore'd, so no pytestmark/test-ID rule applies to the new module.

Tests: `uv run pytest tests/unit/metrics tests/integration/metrics tests/fault/metrics -q -p no:logging` -> 601 passed (141.8 s).

New findings: none.

Verdict: **Approved**

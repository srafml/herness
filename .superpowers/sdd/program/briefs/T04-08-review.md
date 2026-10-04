# T04-08 review (verify agent) - Compute API and metrics #1-#4

Worktree agent-aa288cfffa689d3a0, base 02bd94a, head 6cb7c65 (a322c1b wip + 6cb7c65 feat).

### Spec Compliance
- ✅ U04-49 MetricRow: frozen/forbid/strict, sample_size >= 0, flags from METRIC_FLAGS and sorted (compute.py:58-80).
- ✅ U04-50 MetricResult: fields as specified, result_sample <= 50, a row_count == len(rows) validator, and recorded() rebuilds a copy of the RecordedQuery held in a PrivateAttr (compute.py:83-119). No public fields added (spec note in the report).
- ✅ U04-51 validate_metric_request: positional-only; step order disabled -> period -> grain -> filters/IDs; messages match the spec text; the 500-ID cap, 1-256 printable chars, 1-500 filter values, priority ints 1-5 (bool rejected), and the U04-28 enum sets match exactly (compute.py:125-146, _request.py:15-95).
- ✅ U04-52 compute_metric: catalog_from_config + get_config().weights; open_readonly(None) when con is None, closed in finally, a given con is never closed (compute.py:152-162); requires_columns checked through information_schema with bound params (compute.py:165-171); meta.build -> resolve_as_of; custom/default window; render_metric_query; run_recorded(con, sql, {bind, template}, None, build_id=..., timeout_s=defaults.compute_timeout_s) (compute.py:246-248); rows copied by column name from the wrapper rows, with no arithmetic (compute.py:203-212); log metrics.compute.completed with the section 8 fields only; counter outcome ok/input_error/query_error; flags = sorted static_flags.
- ✅ U04-53 metric_series: a single compute_metric call with window=(start, end); callback called once with recorded(); returns (rows, query_id) (compute.py:319-337).
- ✅ U04-48 #1-#4: SQL expressions, anchors, populations (NX, priority <= 2, resolved = NX AND resolve_h IS NOT NULL), grains, aggregation, unit, better, min n (1/1/10/10), owner sre-analytics, usd_model (null/null/mttr/mttr), filters P S T O C, enabled, requires_columns [] all match Tables A/B. Every template ends with filter_clause, entity_filter, GROUP BY ALL; anchors use p(window_start)/p(window_end). version: 1. Unknown keys are rejected: I checked that MetricsCatalogConfig rejects an injected "bogus" key. The file loads through the config tree (UT10-76 bare repo tree now loads for local/synth; the config_tree stand-in is gone).
- ✅ Accepted rulings applied as described: (a) scoring.org.metrics = {mttr_hours: 0.15} with the "# T04-09/10/11:" marker (metrics.yaml:128-133); the validator is untouched (UT04-13 asserts zero error issues). (b) mttr_hours min_sample_size stays 10; the acceptance value 4.0 is shown via shipped_catalog(mttr_hours=3) (test_metrics_compute.py:161-168), and UT04-64 asserts NULL + insufficient_sample on the shipped min. (c) "# T04-17:" peer_group re-export marker (compute.py:30). (d) the _request.py private split has a section 2 row in docs/impl/04 (line 81, budget 110; the file is 95 lines).
- ⚠️ Cannot verify from diff: the p95 < 2 s budget (BT04-07, not this card); the real open_readonly read-only behaviour (tests stub it; T02-09 owns that).

### Must-check results
1. The only number source is run_recorded. _rows does a by-name copy, and MetricResult carries rq.query_id/sql/params/result_hash/result_sample/row_count/build_id. ✅
2. entity_type is checked against metric.grains and period against the Period literal before render. Filter keys go through the allowlist, and values are typed lists bound as params. ST04-01 asserts result.sql == benign.sql for 4 injection strings in entity_ids and all 4 ID filters, with no rows returned and the fact table intact. ST04-11 asserts that PII-like values are absent from all log records and from the rejection message. ✅
3. open_readonly(None) is used and closed on success and on error; a given con stays open (test_ut04_64_opens_and_closes_readonly_when_con_is_none). ✅
4. See U04-48 above. ✅
5. Rulings (a)-(d) are confirmed. ✅
6. All IDs are present in names and docstrings: UT04-36/37/38/39/64/65/66/67/68/69/70, ST04-01/07/11. I measured coverage myself: compute.py 169 stmts / 28 branches at 100 % line and branch; _request.py 67 stmts / 30 branches at 100 %/100 %. Carry-overs are closed: the UT04-13 xfail is removed (the test now expects a clean load), the config_tree METRIC_ENTRY stand-in is dropped, and the T01-06 default data_root test test_ut01_29_default_data_root_is_config_paths_data exists. Out-of-card edits keep their intent: UT04-26 injects the design section 7.1 scorecard, so the s_org_* pairs and the lower-better intersection are still fully exercised with unchanged assertions; the settings test swaps sum == 1.0 for an exact-dict check with a restore marker. ✅
7. compute.py is 337/340 lines. render.py (299), facts.py (148) and evidence.py (386) are unchanged (empty diff). check_module_size exits 0. ✅

Gates run: pytest tests/unit/metrics + test_runner + test_config_load + test_config_templates gave 625 passed. ruff check is clean, ruff format --check is clean, mypy reports 0 issues in 246 files, and lint-imports shows 13 kept / 0 broken.

### Strengths
- A tight, readable adapter: the context-managed connection, the by-name row copy, and outcome counting in one place.
- The error messages carry keys and rules only; the one caller string echoed (period/grain) is cut to 32 chars and is spec-mandated text.
- The tests are behavioural on metrics_tiny at every grain, including the injection SQL-equality check and the evidence round trip.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. compute.py:174-183 _read_build duplicates herness/metrics/_recorded.py:85-94 read_build_id (same message and one-row rule, plus started_at). A shared helper returning (build_id, started_at) would keep the rule in one place. This is outside the card's files, so it is optional.
2. tests/unit/metrics/test_metrics_compute.py:533-539 (ST04-11): the timed-out call's QueryError message and context are not included in the checked text. The timeout message is fixed text, so the risk is low, but the assertion does not cover the error path the docstring names.
3. compute.py:297-302: StoreBusy, SchemaViolation and ConfigError propagate without incrementing the counter. The spec defines only the three labels, so this is not a defect; note it for T08-05, when the real sink lands.
4. tests/unit/metrics/test_metrics_compute.py:441, 462: connection-lifecycle and meta.build tests are filed under UT04-64. The ID is allowed (an ID may be shared), but UT04-64's spec row is "sample below min_sample_size". An rf_/cv_ ID would describe them more accurately.

### Assessment
**Task quality:** Approved
**Reasoning:** All five units and metrics #1-#4 match the binding spec and Tables A/B, and the accepted rulings are applied as described. The security properties (bound values, unchanged SQL, no values in logs, read-only open and close) are demonstrated by tests, coverage is 100 %, and all gates are green. The findings are polish only.

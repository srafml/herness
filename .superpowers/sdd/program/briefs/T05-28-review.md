# T05-28 review (verify agent): Benchmarks and eval gate

Worktree agent-a7e9a895ab17b13ed, head f64b43a, base bff6f4b. Reviewed against brief, spec 05 §10.1 / §11 / §12 / §13.2 D5, ledger w28-s05 rulings, reviewer rules.

**Verdict: Needs fixes** (one Important: the BT05-07 data extension from the concern 1 ruling. Every other check passes.)

### Spec Compliance
- ❌ One issue: the BT05-07 dataset is narrower than spec. The row says "every catalog metric, team grain", but the bench now requires rows for only 4 metrics. The fix is cheap (see Important 1).
- ⚠️ Cannot verify here: BT05-10 (no real vLLM), ET05-02 (D5 not approved, no HERNESS_ANTHROPIC_IT), and §10.1 "on the reference machine" (this host is loaded and is not the reference box). The `full` build and `tests/fixtures/sql_ok/` are stand-ins (existing carry-overs).

### Per-ID results (my single run, host loaded by other agents; median of 3 repeats)

| ID | Spec target (verbatim §10.1/§11) | Statistic | Measured (repeats) | Report | Verdict |
|----|----------------------------------|-----------|--------------------|--------|---------|
| BT05-01 | mean per-step overhead < 20 ms | mean | 0.545 ms (0.520, 0.551, 0.545) | 0.668 ms | ✅ MET |
| BT05-02 | mean < 5 ms | mean | 0.231 ms (0.242, 0.230, 0.231) | 0.301 ms | ✅ MET |
| BT05-03 | p95 < 25 ms | p95 | 1.209 ms (1.209, 1.330, 1.209) | 1.521 ms | ✅ MET (stand-in corpus) |
| BT05-04 | p95 < 2 s | p95 | 18.87 ms (18.87, 18.47, 19.89) | 23.49 ms | ✅ MET (stand-in full-like build) |
| BT05-05 | p95 < 50 ms | p95 | list 0.058 ms (0.051, 0.058, 0.067); describe 0.221 ms (0.221, 0.254, 0.208) | 0.073 / 0.330 ms | ✅ MET |
| BT05-06 | p95 < 300 ms | p95 | 262.4 ms (262.4, 258.4, 265.3) | 272.2 ms (run 1: 391 ms) | ✅ MET (load-sensitive, about 12 % headroom) |
| BT05-07 | p95 < 2 s | p95 | 67.7 ms (72.0, 67.7, 65.2); 26 metrics, 13 of them empty | 89.3 ms | ✅ threshold MET; ❌ dataset scope (Important 1) |
| BT05-08 | p95 < 10 ms per pair | p95 | 0.0787 ms (0.074, 0.079, 0.085) | 0.126 ms | ✅ MET |
| BT05-09 | p95 < 3 s; `verify_answer` p95 ≤ 5 s | p95 | finding 0.0398 s (0.039, 0.040, 0.040); answer 0.0396 s (0.040, 0.040, 0.038) | 0.053 / 0.054 s | ✅ MET |
| BT05-10 | p95 < 30 ms | p95 | not run (HERNESS_LLM_URL unset) | not run | ⏭ SKIPPED (skipif, not xfail; no network by default) |
| BT05-11 | enqueue mean < 0.2 ms; writer ≥ 1,000 events/s | mean / throughput | 0.0113 ms (0.0113, 0.0113, 0.0116); 45,157 ev/s (45,656, 45,157, 44,299) | 0.0182 ms / 28,500 ev/s | ✅ MET |
| ET05-01 | 0 unsupported numbers in verified outputs; tool-call success ≥ 90 % (Phase 3 gate) | Σ unsupported / Σ total; ok tool_call / all tool_call | 0 of 7; 0.9167 over 12 calls; max steps 3 | same | ✅ MET (in-process proxy) |
| ET05-02 | ≥ 80 % of input tokens read from cache | cache_read share over steps ≥ 2 | not run (D5 skip reason shown) | not run | ⏭ SKIPPED |

Runs:
- Bench: `10 passed, 1 skipped, 2 deselected in 240 s`, exit 0.
- Eval: `3 passed, 1 skipped in 3.1 s`.

### Checks
1. **Targets and thresholds.**
   - Each docstring quotes its target verbatim (BT05-01..11, ET05-01, ET05-02).
   - Thresholds are identical to §10.1: 20 ms / 5 ms / 25 ms / 2,000 ms / 50 ms / 300 ms / 2,000 ms / 10 ms / 3 s and 5 s / 30 ms / 0.2 ms and 1,000 events/s.
   - Dataset constants are unchanged vs `bff6f4b`: STEPS 30, RUNS 200, CALLS 1,000, QUERIES 200, CALLS 100, VECTORS 100k, QUERIES 100, K 20, INCIDENTS 200k, CALLS_PER_METRIC 25, BULK_ROWS 200k, N_EVENTS 100k, 10,000 pairs.
   - The new code only adds warm-up and REPEATS = 3 with a median gate. tests/support/bench_stats.py:21 refuses fewer than 3 repeats.
   - Each bench uses the statistic its row names: mean for BT05-01, BT05-02 and the BT05-11 enqueue; p95 everywhere else.
   - Measurement is in-process with perf_counter. BT05-01 excludes model and tool time. BT05-04 opens a fresh handle per repeat, so no repeat measures result-cache hits.
   - Only the BT05-07 metric set changed (Important 1).
2. **Markers.**
   - Every file has a module-level pytestmark: [integration, slow] for the benches, plus gpu for BT05-10, and eval for ET05-01/02.
   - Fast gate `-m "(unit or integration) and not slow" --collect-only tests/bench tests/eval`: no tests collected (40 deselected).
   - `--require-test-ids --collect-only`: 40 collected, no ID errors. Test names and docstrings carry IDs.
3. **No production change.** `--name-only bff6f4b..HEAD` lists 20 paths, all under tests/.
4. **Skips and ET05-01.**
   - BT05-10 skips unless HERNESS_LLM_URL is set (test_harness_token_count_bench.py:79) and asserts exact=True.
   - ET05-02 has two skipifs: `security.data_policy.hybrid_approved` (false at config/herness.yaml:17) and HERNESS_ANTHROPIC_IT=1 (test_harness_prompt_cache_eval.py:43-51).
   - ET05-01 uses fixed YAML scripts with FakeLLMClient and makes no network call. It loads thresholds from config/eval.yaml and pins them (== 0, == 0.90).
   - The tool-success definition matches impl 11 U11-62 (docs/impl/11 line 1030: "`tool_call` with `ok = true` / all `tool_call`").
   - The 12 calls are q1 1 + q2 2 + q3 2 + q4 2 + q5 3 + q6 2. One call in q4 is refused by the guard, so 11/12 is honest.
   - Mutation probes, both reverted:
     - (a) Removing "about 57" from c1_stray_number.yaml made test_et05_01_control_stray_number_fails_the_gate FAIL.
     - (b) Changing config/eval.yaml tool_success_min.local from 0.90 to 0.70 made the main test FAIL (pinned threshold) and the c2 control FAIL (the gate no longer trips).
     - The negative controls are real.
5. **TH05-08.**
   - BT05-01 asserts stop_reason == "final" and steps == 30 on every run (test_harness_loop_bench.py:104-105).
   - ET05-01 runs `_assert_bounded` (final, steps ≤ 8, tokens ≤ 100k, < 120 s) on every golden and control run.
   - ST05-08 is referenced in the golden eval docstring and in the report.
6. **Reproduction.** All numbers fall within the report's band, mostly faster (host load was lower this run). Nothing missed.

### Strengths
- One helper gives every bench the same median-of-3 report line, with a guard on the repeat count.
- ET05-01 drives the real loop, hooks, tools, ops store evidence, Tracer and Verifier. It also runs spec 11 `count_unsupported` independently of the Verifier, and pins the thresholds against config drift.
- The base BT05-04 bench measured result-cache hits; the fresh handle per repeat fixes that.

### Issues
#### Critical
None.

#### Important
1. **BT05-07 requires rows for only 4 metrics** (tests/bench/test_harness_warehouse_tools_part2_bench.py:270-279 and :308). The spec row is "every catalog metric, team grain".
   - My run lists exactly 13 empty metrics:
     - core.event: alert_noise_ratio.
     - 5 change metrics: change_count, change_failure_rate, change_caused_incident_count, change_lead_time_hours, emergency_change_ratio.
     - 7 work-item metrics: throughput, cycle_time_days, carryover_rate, backlog_age_days, wip_count, unplanned_work_ratio, epic_predictability.
   - Empty queries are cheaper than real ones, so the p95 understates the target.
   - Extending the data is cheap (see the concern 1 ruling). Extend `_SYNTHETIC` (or move it to a tests/support module) and restore "every timed metric returns rows" (`assert not empty`).
   - Even without the extension, the guard at :308 should cover all 13 incident-backed metrics, not 4. Today these 9 could go empty without failing: mttr_p50_hours, mttr_business_hours, mtta_minutes, customer_impact_minutes, repeat_incident_rate, reopen_rate, reassignment_rate, toil_hours_est, incident_cost_usd.

#### Minor
1. The LanceVectors test adapter prints a Lance `_distance` deprecation WARN line on every semantic_search query, which floods the bench output (test_harness_warehouse_tools_part2_bench.py; already present at base). Fix: select `_distance` explicitly or call `disable_scoring_autoprojection` in the adapter.
2. BT05-08 computes p95 as `sorted[int(0.95*(n-1))]` (test_store_ops_bench.py, pairs/p95s), while the other benches use `statistics.quantiles(n=100)[94]`. Unchanged from base and harmless at n = 10,000.
3. The pre-existing mypy --strict errors (concern 3) remain at test_harness_loop_bench.py:99 and test_harness_warehouse_tools_bench.py:50.

### Rulings on the builder's concerns
1. **BT05-07.**
   - (a) The two exclusions are justified. config/metrics.yaml:695-698 gives `availability_pct` grains [service, org], and :726-729 gives `error_rate` grains [service, org]. Neither supports team grain, so they cannot run in a team-grain bench. Record a spec note: the §10.1 row should read "every team-grain catalog metric".
   - (b) Extending the stand-in data is CHEAP and fits one fix round. Add about 7 `INSERT ... SELECT FROM range()` statements, modelled on the existing incident insert and on tests/fixtures/metrics_tiny/*.csv:
     - `core.service_map`: S_i → T_{i%50}, role owner. The event→team OWNER join needs it (herness/metrics/sql/_macros.sql.j2:10).
     - `core.event`: ts, service_id, severity from the noise severities, incident_id NULL on some rows; at least 50 per team-window for min_sample_size.
     - `core.change`: opened_at, actual_start/end, team_id, service_id; types including emergency; outcomes including failed.
     - `enrich.incident_change_link`: incident_id/change_id pairs, for change_caused_incident_count.
     - `core.work_item`: types epic/story/bug/task with parent_key pointing at epics, status_category, created_at, story_points, team_id, service_id.
     - `core.work_item_transition`: to_category in_progress/done, with `at`.
     - Optionally `core.work_item_link` (mentions_incident), for is_unplanned.
   - `materialize_facts` already runs (part2:233) and builds change_fact, work_item_fact and work_item_closure (herness/model/sql/400_facts.sql).
   - Estimated 40-60 lines. Part2 is at 311 of its 400-line budget (pyproject.toml:97), or the data can move into tests/support.
   - Ruling: Important finding. Extend the data and restore the every-metric-returns-rows assertion.
2. **BT05-06 headroom.** My run measured 262 ms (about 12 % headroom). Record it as load-sensitive and a known-flake candidate. The threshold is unchanged and must never be loosened. Accepted.
3. **Pre-existing mypy errors.** Confirmed present at base: bff6f4b test_harness_loop_bench.py:95 `registry=None` and test_harness_warehouse_tools_bench.py:44 `make_build(**FULL_LIKE)`; only their line numbers moved. Leave them.
4. **ET05-01 proxy.** Accepted as a carry-over. Re-point to `herness eval --suite golden` when T11-30 / T09-24 / U11-75 land.
5. **Layout.** Spec §10.1 says tests/bench/harness/ with pytest-benchmark; the tree is flat tests/bench/ with perf_counter and median-of-3. Record a spec note; accepted (pre-existing layout, ledger ruling).

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Thresholds and dataset sizes are intact, the measurements are sound and reproduced, and the skips and eval controls are correct. But BT05-07 times 13 empty metrics without failing and guards only 4, and the data extension that restores the spec dataset fits in one fix round.

Worktree status after the probes: clean (head f64b43a).


---

## Re-review round 1 (head e55c892, scoped to I-1)

**Verdict: Approved.** I-1 is closed. BT05-06 missed its target in my run, but under load and not because of this diff; see check 5.

Scope: `--name-only f64b43a..HEAD` lists only tests/bench/test_harness_warehouse_tools_part2_bench.py (366 lines, within its 400-line budget). No production file changed.

### Checks
1. **I-1 is closed.**
   - Seven stand-in tables are now filled: service_map (owner + support), event, change, incident_change_link, work_item (epics with children), work_item_transition, work_item_link.
   - The fixture logs: facts 200,000 incident, 100,000 change, 160,000 work_item; 272,000 transitions.
   - My run printed `metrics with an empty result: []` for all 26 timed metrics.
   - The emptiness check is per call: any call with `row_count == 0` adds its metric to `empty`, and `assert not empty` fails the test (part2:325-347, :364).
   - Exclusions are pinned exactly: `set(catalog.names()) - set(names) == {"availability_pct", "error_rate"}` (part2:326).
2. **Nothing else loosened.**
   - The BT05-07 threshold (`p95_ms < 2000`) is unchanged.
   - VECTORS, QUERIES, K, INCIDENTS, TEAMS, SERVICES and CALLS_PER_METRIC are unchanged; the incident INSERT is byte-identical. The new constants EVENTS, CHANGES, LINKS and ITEMS only add data.
   - BT05-06's code is untouched.
3. **Mutation probe (reverted).** I made both `core.work_item_transition` SELECTs `WHERE false`, which gives 0 transitions. BT05-07 went RED: `AssertionError: metrics with an empty result: ['carryover_rate', 'cycle_time_days', 'epic_predictability', 'wip_count']`. The data does real work and the assertion bites.
4. **One run of the part2 file** (host loaded by other agents; median of 3 repeats):
   - BT05-06: p95 301.2 ms (291.0, 305.2, 301.2), target < 300 ms → MISSED WITH LOAD NOTE. Builder measured 295.8 ms; my round-0 run measured 262.4 ms.
   - BT05-07: p95 327.3 ms (369.7, 316.1, 327.3), target < 2000 ms → MET. Builder measured 378.7 ms (418 ms alone).
   - Result: 1 failed (BT05-06), 1 passed, 286 s.
5. **Does the BT05-07 fixture hurt BT05-06? No.**
   - `--setup-plan` shows module fixture `metrics_con` is SET UP only after BT05-06 has run and its function fixtures have torn down. It is requested only by BT05-07 and built lazily.
   - BT05-06 is first in file order, so the heavy DuckDB build and its memory never coexist with BT05-06's timing loop.
   - BT05-06's miss comes from its known thin headroom under host load (concern 2: 391 ms in the builder's run 1, 262 ms in my round-0 run, 301 ms now).
   - Not a finding against this fix, and no fixture isolation is needed.
   - Never loosen the threshold. Record BT05-06 as a known flake candidate under load, to be judged on the reference machine.
6. **Lint and types.** `ruff check`: all checks passed. `ruff format --check`: already formatted. `mypy --strict` on the file: no issues.

### Findings
- Critical: none. Important: none.
- Minor (carried from round 0, not blocking): Lance `_distance` deprecation-warning spam in BT05-06 output.

### Worktree status
Clean after reverting the probe. HEAD e55c892.

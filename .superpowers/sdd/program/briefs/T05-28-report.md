# T05-28 report: Benchmarks and eval gate (impl 05 harness core)

Branch worktree-agent-a7e9a895ab17b13ed, base bff6f4b. Tests-only card: no file under herness/ or config/ touched.
Status: DONE_WITH_CONCERNS (see Concerns).

## Measured vs target (host loaded by parallel agents; numbers from the full bench run 2 unless noted)

Method for every bench: in-process, warm-up first, the stated dataset measured REPEATS = 3 times, gate on the median; uniform stderr line `BT05-xx <stat> <median> <unit> (target ...) repeats=[...]` (tests/support/bench_stats.py).

| ID | spec target (verbatim) | file::function | markers | dataset | method | measured median (repeats) | verdict |
|----|------------------------|----------------|---------|---------|--------|---------------------------|---------|
| BT05-01 | mean per-step overhead < 20 ms | tests/bench/test_harness_loop_bench.py::test_bt05_01_loop_overhead_per_step | integration, slow | FakeLLMClient + instant fake tool, 30-step script, 200 runs (as spec) | 5 warm-up runs; 3 x 200 runs | 0.668 ms (0.692, 0.668, 0.661); run 1: 0.604 | MET |
| BT05-02 | mean < 5 ms | tests/bench/test_harness_dispatch_bench.py::test_bt05_02_dispatch_overhead_per_call | integration, slow | 1 no-op sync tool, 1,000 calls (as spec) | 50 warm-up; 3 x 1,000 (distinct args) | 0.301 ms (0.301, 0.314, 0.298); run 1: 0.315 | MET |
| BT05-03 | p95 < 25 ms | tests/bench/test_harness_sql_guard_bench.py::test_bt05_03_sql_guard_p95_under_25ms | integration, slow | stand-in: test-local accepted corpus (>= 200) on the stand-in schema (sql_ok/ and `full` not built) | 1 warm-up check; 3 passes, p95 per pass | 1.521 ms (1.524, 1.521, 1.503); run 1: 1.393 | MET |
| BT05-04 | p95 < 2 s | tests/bench/test_harness_warehouse_tools_bench.py::test_bt05_04_run_sql_typical_aggregate_p95 | integration, slow | stand-in full-like build (500k incidents, 200 services, 36 months, 20 MB), 200 distinct aggregates | fresh handle per repeat (empty result cache) + 1 warm-up aggregate; 3 x 200 | 23.49 ms (23.39, 23.49, 23.66) | MET |
| BT05-05 | p95 < 50 ms | tests/bench/test_harness_warehouse_tools_bench.py::test_bt05_05_list_and_describe_p95 | integration, slow | same full-like build | 1 warm call; 3 x 100 calls each | list_tables 0.073 ms (0.073, 0.073, 0.075); describe_table 0.330 ms (0.330, 0.345, 0.280) | MET |
| BT05-06 | p95 < 300 ms | tests/bench/test_harness_warehouse_tools_part2_bench.py::test_bt05_06_semantic_search_k20_p95 | integration, slow | stand-in: 100k random 1024-d vectors (LanceDB), tiny-st CPU encoder (bge-m3 not in test env) | 1 warm-up query; 3 x 100 queries | run 2: 272.2 ms (272.2, 269.0, 287.9); run 1: 391.2 ms (391.2, 377.1, 417.0) | MET in run 2; MISSED WITH LOAD NOTE in run 1 (heavier host load; about 10 % headroom) |
| BT05-07 | p95 < 2 s | tests/bench/test_harness_warehouse_tools_part2_bench.py::test_bt05_07_get_metric_team_13_weeks_p95 | integration, slow | stand-in metrics_tiny schema, 200k incidents, 50 teams, 200 services; 26 team-grain catalog metrics x 25 windows of 13 weeks | 1 warm-up call per metric; 3 passes | 89.3 ms (87.6, 89.3, 90.4) (separate run after the fix) | MET (see Concern 1: the base bench could not run) |
| BT05-08 | p95 < 10 ms per pair | tests/bench/test_store_ops_bench.py::test_bt05_08_evidence_and_use_pair_write_p95 | integration, slow | real WAL ops store, 10,000 pairs (as spec) | 50 warm-up pairs; 3 x 10,000 new pairs | 0.126 ms (0.127, 0.126, 0.124); run 1: 0.112 | MET |
| BT05-09 | p95 < 3 s; verify_answer p95 <= 5 s | tests/bench/test_harness_verifier_bench.py::test_bt05_09_verifier_per_finding_and_answer_p95 | integration, slow | stand-in verifier build + 200k bulk rows, 100 findings (3-5 numbers, 2-3 queries) | 1 warm-up finding and answer; 3 x 100, fresh Verifier each | finding 0.053 s (0.058, 0.053, 0.053); answer 0.054 s (0.057, 0.054, 0.052) | MET |
| BT05-10 | p95 < 30 ms | tests/bench/test_harness_token_count_bench.py::test_bt05_10_token_count_vllm_endpoint_p95 | integration, slow, gpu | real vLLM via HERNESS_LLM_URL (IT05-08 convention), 200 calls | 5 warm-up; 3 x 200; asserts exact=True (no estimate fallback) | not measured (code path exercised once against a temporary loopback /tokenize stub outside the repo; not a result) | SKIPPED (HERNESS_LLM_URL not set; no real vLLM on this host) |
| BT05-11 | enqueue mean < 0.2 ms; writer >= 1,000 events/s | tests/bench/test_harness_tracing_bench.py::test_bt05_11_enqueue_mean_and_writer_throughput | integration, slow | 100,000 events without payload (as spec) | 1,000-event warm-up tracer; 3 fresh tracers x 100,000 | enqueue 0.0182 ms (0.0188, 0.0182, 0.0180); writer 28,500 ev/s (27,855, 28,500, 28,777); run 1: 0.0114 ms / 44,446 ev/s | MET |
| ET05-01 | 0 unsupported numbers in verified outputs; tool-call success >= 90 % (Phase 3 gate) | tests/eval/harness/test_harness_golden_eval.py::test_et05_01_golden_zero_unsupported_and_tool_success (+ 2 negative-control functions) | eval | fixed synthetic: 6 scripted chat questions (tests/fixtures/llm_scripts/et05_01_golden/) on the stand-in build; local profile | real run_agent + HarnessHooks + warehouse tools + ops store evidence + Tracer + Verifier + spec 11 count_unsupported; thresholds read from config/eval.yaml | unsupported 0 of 7 numbers (rate 0); tool-call success 11/12 = 0.9167; all 6 answers pass the Verifier; max steps 3 | MET (in-process equivalent; CLI not built) |
| ET05-02 | >= 80 % of input tokens read from cache | tests/eval/harness/test_harness_prompt_cache_eval.py::test_et05_02_writer_task_reads_80pct_of_input_from_cache | eval | one scripted hybrid writer task (3 steps, real Messages API) | share = cache_read / (input + cache_read + cache_write) over steps >= 2 | not run | SKIPPED (D5: config/herness.yaml hybrid_approved=false; also needs HERNESS_ANTHROPIC_IT=1) |

Negative controls (ET05-01): c1_stray_number ("about 57") makes the Verifier fail (n_uncited=1), count_unsupported >= 1, gate unsupported_ok=False; c2_failing_tools (3 refused queries of 4) gives a combined rate 12/16 = 0.75 < 0.90, gate tool_ok=False. Eval run: 3 passed, 1 skipped (ET05-02).

Fast gate: `pytest -m "(unit or integration) and not slow" --collect-only tests/bench tests/eval` collects none (40 deselected). `pytest --require-test-ids --collect-only tests/bench tests/eval`: 40 collected, no ID errors. Checkpoint commit d017b8d passed all pre-commit hooks (ruff, mypy, import-linter, detect-secrets, fixtures-pii-scan, module-size, pytest-unit).

## Files changed
New: tests/support/bench_stats.py; tests/bench/test_harness_token_count_bench.py; tests/eval/harness/test_harness_golden_eval.py; tests/eval/harness/test_harness_prompt_cache_eval.py; tests/fixtures/llm_scripts/et05_01_golden/q1..q6 (6 yaml); tests/fixtures/llm_scripts/et05_01_controls/c1_stray_number.yaml, c2_failing_tools.yaml.

Modified (measurement method only; thresholds and dataset sizes unchanged; each docstring now quotes the section 10.1 row):
- tests/bench/test_harness_loop_bench.py: repeats; asserts stop_reason final and steps == 30 per run (TH05-08).
- tests/bench/test_harness_dispatch_bench.py: repeats, distinct args per repeat.
- tests/bench/test_harness_sql_guard_bench.py: median of per-pass p95 (was one pooled p95 over 3 passes).
- tests/bench/test_harness_tracing_bench.py: warm-up + 3 fresh tracers.
- tests/bench/test_harness_verifier_bench.py: warm-up + repeats; body split into helpers.
- tests/bench/test_harness_warehouse_tools_bench.py: BT05-04 fresh handle per repeat so no repeat hits the result cache; BT05-05 repeats.
- tests/bench/test_harness_warehouse_tools_part2_bench.py: BT05-06/07 warm-up + repeats; BT05-07 metric set and empty-result handling (Concern 1).
- tests/bench/test_store_ops_bench.py: BT05-08 warm-up + repeats with distinct query ids (BT02-06/07 untouched).

## TH05-08 (runaway loop) coverage
Runaway cases (repeated calls, no progress, step/token/wall-clock/cost limits, identical-call rejection) are covered by ST05-08 (tests/security/test_st05_loop.py, T05-23). Here: BT05-01 asserts every run stops on `final` at exactly the scripted 30 steps; ET05-01 asserts every run (golden and controls) completes with stop_reason final, steps <= 8, tokens <= 100,000 and wall clock < 120 s (max observed 3 steps).

## Carry-overs
- ET05-01: re-point to `herness eval --suite golden` (spec 11 golden suite, tests/eval/golden.yaml U11-75) when T11-30 / T09-24 land; the fixtures here are a stand-in, not the golden suite.
- BT05-03..07, 09: re-point to the spec 11 `full` build and tests/fixtures/sql_ok/ when built (existing carry-overs from T05-17/18/22).
- BT05-10: run on the dev box with HERNESS_LLM_URL (real vLLM) and record.
- ET05-02: run when D5 approves hybrid (hybrid_approved recorded) with HERNESS_ANTHROPIC_IT=1.

## Concerns and spec notes
1. BT05-07 could not pass at base: it asserted rows for every catalog metric at team grain, but (a) availability_pct and error_rate do not support team grain (ToolInputError) and (b) 13 metrics over alerts, changes and work items return empty results on the stand-in data (incidents only). Change: time only the 26 team-grain metrics; empty results are listed on stderr instead of failing; the incident-backed metrics (incident_count, mttr_hours, p1p2_count, sla_breach_rate) must still return rows. Threshold unchanged. A reviewer may prefer extending the stand-in data instead.
2. BT05-06 is close to its budget on this host (272 ms vs 300 ms in run 2, 391 ms in run 1 under heavier load). Expect failures under load until it is measured on the reference machine.
3. Pre-existing mypy --strict errors in two modified bench files, not on changed lines: test_harness_loop_bench.py:99 (HarnessHooks registry=None) and test_harness_warehouse_tools_bench.py:50 (make_build **dict). New files are mypy-clean (--explicit-package-bases).
4. ET05-01 is a proxy: chat role only, 6 questions, stand-in build. The metrics follow impl 11 U11-62 (sum unsupported / sum total; tool_call events with ok / all tool_call events, read from the JSONL trace).
5. Spec section 10.1 places benches in tests/bench/harness/ with pytest-benchmark; the tree keeps them flat in tests/bench/ with perf_counter (pre-existing layout, unchanged).

## Fix round 1 (review Important 1, BT05-07 dataset scope)

File: tests/bench/test_harness_warehouse_tools_part2_bench.py (366 lines; tests only).
- Stand-in data extended with 7 `INSERT ... SELECT FROM range()` statements over the same two-year span as the incidents: core.service_map (owner + support row per service, 400 rows; owner team matches the incident team), core.event (EVENTS = 400,000, every 150 s, 4/5 noise severities, 1/4 incident-linked; about 80 per team-week vs min sample 50), core.change (CHANGES = 100,000, every 10 min; 10 % emergency, unsuccessful/backed_out failures, 2 % canceled; lead time 4-203 h), enrich.incident_change_link (LINKS = 20,000, 3/4 above the 0.7 score), core.work_item (ITEMS = 160,000: 1 epic per 8 items, children story/bug/task with parent_key; 80 % done, 10 % in progress, 10 % todo), core.work_item_transition (272,000 rows: in-progress and done moves), core.work_item_link (mentions_incident, 1 in 15 items). materialize_facts builds 200,000 incident, 100,000 change and 160,000 work_item fact rows.
- Restored the strict check: every timed team-grain metric must return rows on every call (`assert not empty`; the empty set is still printed on stderr). Exclusions pinned to exactly {availability_pct, error_rate} (grains [service, org], config/metrics.yaml), with a comment. Volumes are sized so most team-week rows meet the catalog min_sample_size (rows below it are still returned, flagged insufficient_sample; flags were not tallied).
- Threshold (p95 < 2 s), incident sizes, CALLS_PER_METRIC, windows unchanged.

Measured (host loaded by parallel agents):
| ID | run | median p95 | repeats | result |
|----|-----|-----------|---------|--------|
| BT05-07 | alone (-k bt05_07) | 418.0 ms | 403.6, 418.0, 446.6 | MET, 26 metrics, 0 empty |
| BT05-07 | full file | 378.7 ms | 378.7, 345.6, 385.1 | MET, 26 metrics, 0 empty |
| BT05-06 | full file | 295.8 ms | 291.4, 295.9, 295.8 | MET (about 1 % headroom under load; see Concern 2) |

BT05-07 p95 rose from 89 ms to about 380-420 ms because the work-item snapshot metrics (wip_count, backlog_age_days, carryover_rate: ASOF join over the transitions per spine week) now do real work (about 350-450 ms per call); still about 5x under the 2 s target.
ruff format/check clean; mypy --strict on the file: no issues.

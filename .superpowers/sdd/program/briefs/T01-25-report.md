# T01-25 report: Phase 6 fault suite and benchmarks (tests only)

Branch worktree-agent-a2c1fd54b5f5969c4, base 10f9733. Commits: wip 96e93de (FT01-01/03), final 26306cd "test(connectors): T01-25 Phase 6 fault suite and benchmarks".

## Files (all under tests/, no herness/ change)
- tests/fault/connectors/_sn_child.py, _sn_env.py, test_servicenow_crash_fault.py (FT01-01, FT01-03)
- tests/fault/connectors/test_source_resilience_fault.py (FT01-04, FT01-05 + one xfail twin, FT01-06)
- tests/bench/connectors/_sn_load.py, _sources.py (support)
- tests/bench/connectors/test_connectors_servicenow_bench.py (BT01-01, BT01-03), test_connectors_deletion_bench.py (BT01-02), test_connectors_incremental_bench.py (BT01-05), test_connectors_live_rates_bench.py (BT01-06)

## Per-row proof
- FT01-01: real child process (HERNESS_FAULTS plan `connector.before_watermark kill nth=2 source=servicenow`, exit code 15) runs the backfill sync job over the seeded incident_table cassette in 3 one-day slices; slices after kill = done/running/pending; rerun gives all done. A second tree/ops store runs the no-crash job. Asserts: crashed lake rows are a superset (killed slice read twice, row count = clean + slice-2 rows) and after the real staging+core build every core.* table and stg.sn_incident are equal to the no-crash run (240 staged).
- FT01-03: 10 one-day slices (rows only in slices 4-6), kill nth=4. Asserts 3 `slice_completed` events and sync_slice = 3 done/1 running/6 pending after kill; restart logs exactly 7 slice_completed, 10 done, watermark None before, = min(end, newest record) = 2026-02-22T11:45 after.
- FT01-04: in-process Jira runner over the cloud_sync_it cassette, fault plan `http.page http_429 retry_after=7 count=3 source=jira`. Asserts 3 stored `retry` events (attempt 1..3, retry_after_s 7, wait_s >= 7, target jira); fault_point called 3+9 times and the 9 recorded requests each served once (only the first page repeated); `set_watermark` called once, watermark advanced.
- FT01-05: 401 on first page via SourceHttp: AuthError, one request, no retry events; `breaker("jira").force_open(err)` then state open, one breaker_open event (trips 1), next call raises CircuitOpen with no new request. See concern 1: a strict-reading twin is xfail.
- FT01-06: handle_sync on ServiceNow with every table answering 503: run 1 = 6 attempts then SourceUnavailable (job fails), breaker still closed; run 2 = 2 more 503 (8 = threshold) opens the breaker and the job is `done` with skipped_open_circuit == ["servicenow"]; run 3 makes no HTTP call (requests + token calls unchanged), still done.
- BT01-01 / 03 / 02 / 05: see numbers below. BT01-06: written, skipped unless HERNESS_BT01_06_SANDBOX (config dir) is set; reason names V-1/V-2 and open questions (b) 22/23; when enabled it syncs the first entity of each enabled sandbox source, asserts the lower bounds and writes <paths.data>/bench/bt01_06-<time>.json.

## Measurements (full spec size on this loaded PC)
- BT01-01 20,498 rows/s (1M rows, 48.8 s), threshold >= 5,000: pass
- BT01-03 peak RSS 1,074 MiB (BT01-01 run + reconcile of 5M keys, 5,000 tombstones), limit 1.5 GB: pass (1,180 MiB in an earlier run)
- BT01-05 300,000 records over 7 sources in 24.0 s, limit 300 s: pass
- BT01-02 with/without ratio 1.40-1.50 (without 2.57 s, with 3.9 s; 1M SN-shaped rows, 100k deleted ids), target 1.05: FAILS, marked xfail(strict=False) as open item (BT01-04 precedent)
- Timings of final runs: tests/fault/connectors 7 passed + 1 xfailed in 16.4 s (22 s wall incl. uv); tests/bench/connectors 4 passed, 2 skipped (xlsx BT01-04, BT01-06), 2 xfailed (BT01-02, BT01-04 parquet) in 203.8 s (3m29 wall). Durations: BT01-03 62.8 s, BT01-01 48.8 s, BT01-02 31.0 s, BT01-05 24.0 s; FT01-01 5.1 s, FT01-03 3.1 s.

## Deviations
- respx is not used: the connectors' egress client is httpx2, which respx does not patch; httpx2.MockTransport is used (as the unit/integration suites do). FT01-06 uses SourceUnavailable counts from the 503 stub; breaker threshold is 8 (config/resilience.yaml), so 8 consecutive 503 open it (the row says 10).
- tools.synth.api_pages (T11-15) is not on the tree: BT01-01/03 use a card-local generator (tests/bench/connectors/_sn_load.py), arithmetic pages in constant memory, same record shape as sn_cassettes. Sources of BT01-05 are the existing unit-test fakes (FakeSnowflake, mongomock, FakeAdapter, Server, Replay-like Jira handler).
- Env overrides for dry runs: HERNESS_BT01_ROWS, HERNESS_BT01_KEYS, HERNESS_BT01_05_RECORDS; defaults are the spec sizes and were used for the final runs. Thresholds unchanged.
- Pre-commit runs the unit suite on each commit (~20 min here), so only two commits were made (wip after FT01-01/03, final with the rest) instead of one wip per file.

## Concerns
1. FT01-05: no code in herness/ calls breaker.force_open on an AuthError (grep: only breaker.py defines it; impl 08 §9.2 says "spec 01 calls force_open"). A 401 leaves the breaker closed. The main FT01-05 test therefore calls force_open itself as the card names it; test_ft01_05_auth_error_opens_the_breaker_without_a_manual_force_open (xfail, non-strict) documents the gap. Needs a ruling / source card.
2. BT01-02 misses its target (1.4-1.5x vs 1.05x): DeletionFilter.apply runs pc.is_in with a 100k-id value set per 10k-row batch (~12 ms/batch, hash set rebuilt per call) and reload() rebuilds the array at each 500k-row checkpoint. Not a hardware limit; a source fix (e.g. a prebuilt set / anti-join once per checkpoint) is needed. xfail non-strict with the numbers in the reason.
3. Stored resilience events drop string detail fields (error_type, policy, breaker_key, reason): only attempt, wait_s, retry_after_s, failures, trips, probe_due and the target column survive in resilience_event, so the tests assert those.
4. The machine was shared with other agents during measurement; numbers are noisy but pass margins (4x, 28 percent, 12x) are large.

## Fix round 1 (commit 4216d02 "fix(connectors): T01-25 review round 1 (minors)")
- c.reset_config() moved from the end of the test bodies into autouse teardown fixtures (test_source_resilience_fault.py, test_connectors_deletion_bench.py).
- BT01-02 xfail now `raises=AssertionError`, reason states ~1.4-1.5x measured (1.3-1.7x over runs).
- FT01-05 main test renamed test_ft01_05_401_is_an_auth_error_without_retry_and_force_open_opens_the_breaker.
- Run (spec size, no env override): tests/fault/connectors + BT01-02 file: 7 passed, 2 xfailed in 48.1 s; ruff and format clean; hooks passed.

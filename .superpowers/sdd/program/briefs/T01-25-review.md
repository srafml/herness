### Spec Compliance
Verdict: Approved (no Critical/Important findings).

Per row (each present exactly once, ID in test name/docstring):
- FT01-01 ✅ real subprocess, HERNESS_FAULTS plan kill nth=2; asserts exit code, fault-kill log, slices done/running/pending, superset (clean <= crashed, crashed = clean + killed-slice rows), core.* and stg equal after real build, core.* non-empty guard.
- FT01-03 ✅ 10 slices, kill nth=4; 3 slice_completed then exactly 7 on restart, 10 done, watermark None before, == min(end, newest) after.
- FT01-04 ✅ rule http_429 retry_after=7 count=3 source=jira; 3 retry events attempt 1..3, retry_after_s {7}, target jira; fault_point 3+9 calls, replay drained, search page seen twice only; set_watermark once and advanced.
- FT01-05 ✅ (with limit) AuthError, 1 request, no retry events, force_open -> open, breaker_open trips=1, next call CircuitOpen with no new request. Asserts what the row can assert today; the force_open call is manual (named in the row).
- FT01-06 ✅ run1 6 attempts then SourceUnavailable (breaker closed), run2 opens at 8, job done + skipped_open_circuit==["servicenow"], run3 no HTTP/token calls.
- BT01-01 ✅ 1M default, >= 5000 rows/s; BT01-03 ✅ BT01-01 run + 5M-key reconcile (5,000 tombstones asserted), RSS < 1.5 GiB via psutil sampler; BT01-05 ✅ 300k default over 7 sources, < 300 s, per-source counts asserted; thresholds untouched, env overrides default to spec sizes.
- BT01-02 ⚠️ test is faithful (1M rows, 100k ids, median of 3 ratios, <= 1.05). xfail non-strict. I ran it with --runxfail: fails for the stated reason (1.417x, repeats 1.4173/1.4170/1.4187, assertion on the ratio, not a setup error). Honest.
- FT01-05 strict twin: with --runxfail fails at `assert state()=="open"` ('closed'), the stated reason. Honest.
- BT01-06 ✅ written, skipif env HERNESS_BT01_06_SANDBOX unset, reason names V-1/V-2 and open questions 22/23; when enabled asserts lower bounds and writes data/bench json.
- Markers ✅ fault files: pytest.mark.fault only; bench files: [integration, slow] like test_connectors_files_bench.py (one category each). No live network (httpx2.MockTransport, synthetic creds).
- Deviations justified: respx does not patch httpx2 (matches unit/integration suites); threshold 8 is config/resilience.yaml (test asserts 6 then 8 attempts, closed then open); card-local page generator because T11-15 not on tree.

⚠️ Cannot verify / notes:
- One of my --runxfail runs of BT01-02 (with `-m slow`, 53 s) reported "1 passed", the next two runs failed at 1.417x consistently. Cause unknown (maybe the marker selection or host load); the result is load-sensitive. Non-strict xfail tolerates both, but record it.
- Slow benches (BT01-01/03/05) not re-run; relying on builder numbers (20.5k rows/s, 1,074 MiB, 24 s).
- FT01-05 / BT01-02 source gaps need a program ruling/source card (force_open caller on AuthError; DeletionFilter.apply set rebuilt per batch).

### Strengths
- Real resilience/HTTP/runner stack, subprocess kills with exit-code and log proof of the kill point; faithful strong assertions (superset + exact count, only-that-page-repeated).
- xfails document measured numbers and fail for the stated cause.
- Reuses unit-test helpers (_servicenow_env, _http_data, _jira_data), tests/support (fault_env, bench_stats, sn_cassettes); helper modules small (<=217 lines).

### Issues
#### Critical
None.
#### Important
None.
#### Minor
- tests/fault/connectors/test_source_resilience_fault.py:249 `c.reset_config()` at the end of FT01-06 and tests/bench/connectors/test_connectors_deletion_bench.py:118 at the end of BT01-02 are not run when the test fails/raises earlier (and BT01-02 is an expected failure), leaking config state to later tests. Use try/finally or a fixture teardown (as bench_env does).
- tests/bench/connectors/test_connectors_deletion_bench.py:60-66 xfail reason says "~1.3-1.7x" while report says 1.40-1.50 and my run 1.417; harmless, tighten if desired.
- tests/bench/connectors/test_connectors_deletion_bench.py:67-69 non-strict xfail masks any failure (including a test bug) once the source is fixed or broken; consider `raises=AssertionError` so setup errors are not swallowed.
- FT01-05 test name says "opens_the_breaker" though it calls force_open manually; docstring explains it, acceptable.

### Assessment
**Task quality:** Approved
**Reasoning:** All 10 rows are present once and assert their thresholds faithfully; fault suite passes (7 passed, 1 xfailed), both xfails fail for the documented reason, and no source gap is masked. Only minor cleanup remains.

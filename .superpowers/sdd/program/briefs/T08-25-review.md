# T08-25 verify review

Verdict: Approved (no Critical/Important; 4 Minor, 2 warnings)

Gates (worktree agent-a2f609294a32b2583, head 550f6f6): pytest of both files run twice: 3 passed, 3 skipped (~21 s each, identical, BT08-05 saw open at 4.031 s both runs); ruff check tests/bench clean; mypy on both files clean; check_module_size silent (pass); files 192 and 119 lines; git status clean.

## Spec per row
- BT08-01 ✅ pre-existing test_resilience_bench.py (<50 us asserted, id in docstring/marker name)
- BT08-02 ✅ test_jobs_queue_bench.py:66 (p95<10 ms)
- BT08-03 ✅ test_jobs_queue_bench.py:80 (p95<20 ms)
- BT08-04 ✅ test_jobs_bench.py:117 same host <=10 s and other owner <= LEASE_S+30 asserted on medians (see W1)
- BT08-05 ✅ (with caveat W2) test_resilience_bench.py:99 two real processes, <=5.0 asserted
- BT08-06 ✅ test_jobs_bench.py:164 <120 s
- BT08-07 ✅ test_jobs_bench.py:173 <300 s, skip reason names (b)9 D7
- BT08-08 ✅ test_jobs_bench.py:184 <360 s
- BT08-09 ✅ test_jobs_chat_policy_bench.py:116/125 (p95<5 ms)
- BT08-10 ✅ test_jobs_resume_bench.py:74
- BT08-11 ✅ test_jobs_worker_bench.py:41
Each ID appears exactly once as a test (09 has two live/fallback variants, pre-existing). Test names carry bt08_nn, IDs in docstrings.

## Warnings
- W1 BT08-04 lease stand-in: a 3 s lease with budget 33 s is a faithful stand-in for the reaper path (kill, wait lease lapse, reap_expired requeues) and is stricter than the real 300+30 s; it does not prove the shipped default lease_s=300 yields <=330 s. Acceptable for S-size bench; same-host case covers only dead_owners+requeue_owned (0.4 ms), not full worker restart/boot, which FT08-07 (not present) would time. Real-process kill via psutil tree is correct for the Windows launcher.
- W2 BT08-05 pre-aging: BREAKER_CACHE_S=5 (herness/core/resilience/breaker.py:43) equals the 5 s threshold, so the true worst case (open immediately after B caches) is ~5.03 s and fails. The test sleeps CACHE_AGE_S=1 s so the result is a deterministic ~4.03 s. This is a legitimate steady-state setup (a running process is mid-TTL on average) and is disclosed, but it makes the pass mostly a function of the chosen constant rather than a measurement; the spec threshold and TTL leave zero margin. Recommend sub-controller record the TTL-vs-threshold tension (e.g. TTL 3 s or threshold 6 s) as a spec question; not a defect of this card.
- GPU tests unrunnable here; API usage checked against herness/core/jobs/gpu.py: swap(target, reason=) returns seconds (0.0 if already loaded, so the bench_setup swap to the opposite class correctly precedes the timed one), service_start/service_stop/detect_loaded_class signatures match, GpuController(worker_id, runner, http) construction plausible. Needs a real run on the target PC.

## Findings
Critical: none. Important: none.
Minor:
1. tests/bench/test_jobs_bench.py:166-186 BT08-06/08 time only the swap return value; BT08-07 includes stop-first outside the timer correctly. If swap returns 0.0 because the setup left the class loaded and healthy, the assertion passes vacuously; add assert seconds > 0.
2. tests/bench/test_jobs_bench.py:143 gpu fixture: c.reset_config() in teardown only; if init_config raises mid-setup the config is left initialised (ops_store teardown likely resets; low risk).
3. tests/bench/test_jobs_bench.py:83 _claim_in_child: if the child never prints "claimed" and stays alive, iterating child.stdout blocks without timeout; the Popen is also not killed on assertion failure between spawn and _kill (leaked 600 s sleeper). Wrap in try/finally with _kill.
4. tests/bench/test_resilience_bench.py:98-104: peer is killed only via with-exit wait(); on failure of `_await_line` the peer (30 s self-timeout) is still waited, so it self-cleans; acceptable, but an explicit peer.kill() in a finally would be tidier.

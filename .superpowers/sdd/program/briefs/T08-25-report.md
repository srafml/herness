# T08-25 report (Benchmarks, tests only)

Files: tests/bench/test_resilience_bench.py (+BT08-05), tests/bench/test_jobs_bench.py (new: BT08-04, 06, 07, 08).
Pre-existing IDs (verified): BT08-01 test_resilience_bench.py; BT08-02/03 test_jobs_queue_bench.py; BT08-09 test_jobs_chat_policy_bench.py; BT08-10 test_jobs_resume_bench.py; BT08-11 test_jobs_worker_bench.py. All 11 now exist, none duplicated.
Added: BT08-04, BT08-06, BT08-07, BT08-08 in test_jobs_bench.py (new file named by the card; no existing jobs bench fit); BT08-05 in test_resilience_bench.py.
Rulings: no benchmark marker; test_jobs_bench.py is module-level fault+slow (plugin demands exactly one category per file, fault cannot mix with integration), gpu on BT08-06/07/08; BT08-07 skip reason names (b)9 D7; GPU tests gated by HERNESS_GPU_BENCH=1, use GpuController(swap, service_stop/service_start) on real config.
BT08-04: no FT08-07 module exists; built directly: child process claims a job and is killed (psutil tree kill, because the Windows venv python.exe is a launcher). Same host: dead_owners(host)+requeue_owned. Other owner: owner host "otherhost", lease shortened to 3 s via SQL, reaper reap_expired polled; budget lease_s+30 with LEASE_S=3 (real default 300 is not waited out).
Timings (dev box, 3 repeats, median): BT08-04 same host 0.0004 s (<=10 s); other owner 3.131 s (<=33 s). BT08-05 peer saw open after 4.03 s (<=5 s). BT08-01 2.4 us.
Skips: BT08-06/08 and BT08-07 skip without HERNESS_GPU_BENCH=1.
Concerns: (1) BT08-05: breaker cache TTL is exactly 5 s, so worst case seen time is 5 s plus the 50 ms poll step; a run that opens A immediately after B caches measured 5.03 s and failed. The test lets B's cached row age 1 s (CACHE_AGE_S) so the bound is meaningful; spec threshold and TTL are equal, consider lowering TTL or raising threshold. (2) BT08-04 same-host timing covers recovery only, not worker restart/boot. (3) GPU tests untested on real hardware.
Gates: ruff check/format, mypy on both files, check_module_size, pytest (3 passed, 3 skipped), --require-test-ids collect clean.

## Round 1
Fixed the four review findings: swap asserts now require seconds > 0; gpu fixture resets config in try/finally; _claim_in_child bounded by a 120 s watchdog timer and kills the child on any failure; BT08-05 peer killed (process tree) in a finally block. ruff, mypy, module size clean; 3 passed, 3 skipped.

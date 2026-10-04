# T08-21 fix round 2 scoped re-review (VERIFY, opus) - 727ff4f..e287f09
Recorded by sub-controller w20-s08 from the verifier's hand-back (worktree isolation refused its write). Head e287f09, 4 test files (+35/-20), no production code.
Verdict: Approved (0 Critical, 0 Important, 2 Minor).

1. OK IT08-13 clock-independent, assertions unchanged: fixed past CHAT_DAY 2026-06-17 noon (test_jobs_worker_gpu.py:45), jobs scheduled_for=noon, claimable_counts now=noon (:93), guard assert at < clock.now() (:54). Fake-clock plugin (herness.core.time.now = real + offset; children real clock): fix 10/10 tests pass at Fri 08:00, 13:00, 23:30, Sat 08:00, 13:00, 23:30 business time; control old IT08-13 (727ff4f) passes 08:00, FAILS 13:00/23:30 (bug reproduced). Extra past instants Thu 23:30, Sat 08:00/23:30, Sun 13:00 pass. 2026-06-17 is a Wednesday in EDT, chat window 08:00-19:00 every day; window_at assert fails loudly on config change. Unit DAY (test_jobs_supervisor_gpu.py:64) same properties.
2. OK no remaining wall-clock dependence in test_jobs_worker*.py, test_jobs_supervisor*.py, test_jobs_child.py, _supervisor_env.py, worker_env.py (stall tests relative to clock.now()).
3. OK tests/integration/jobs + tests/unit/core/jobs 620 passed (109 s) on clean tree; IT08-13 x5 5/5 (~4 s); ruff check, format (767), mypy 0 (297).
4. OK mutation min_priority=None at supervisor.py:287 -> red test_cv_t08_21_gpu_claim_uses_the_arbiter_filter; reverted.

Minor:
- M1 IT08-13 cannot see the claim-level min_priority filter (plan returns None once p75 done, claimable_counts=0); unit test pins it; pre-existing.
- M2 tests outside this diff mix real/faked clocks (test_jobs_queue_claim.py:60 datetime.now(UTC)+1h; IT08-08, IT08-01 compare times across processes) - fail only with test-process clock faked AHEAD of real time; fine on real clock at any time of day. Hygiene note.

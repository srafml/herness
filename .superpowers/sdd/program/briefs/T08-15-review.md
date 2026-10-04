# T08-15 review (Outcome application)
(Written by the sub-controller from the verify agent's reply; its own write was refused by worktree isolation.)
Spec: ✅ U08-49, ✅ U08-50 steps 1-6, ✅ UT08-58 (every taxonomy leaf, derived), ✅ UT08-59, ✅ TH08-08 guards; ⚠️ IT08-04/UT08-62/ST08-02 out of card.
Evidence: tests/unit/core/jobs 440 passed; card 110 passed; outcomes.py 100% line and branch; 3 mutation probes (SKIP_ON_OPEN_CIRCUIT reconcile, MESSAGE_MAX_CHARS 4096, metric on lease-lost path) all fail tests.
Guards: every completion SQL in _job_sql.py guarded on lease_owner = :owner AND status = 'running' (finalize: 'canceled'); re-finishing a done/failed row updates 0 rows -> lease_lost, no side effect; missing row -> lease_lost (spec silent, accepted).
1 MiB cap enforced by JobOutcome validator (herness/core/types/jobs.py:103-109); finish_job does not bypass it.
Critical: none. Important: none.
Minor: (1) test_jobs_outcomes.py:95-113 oracle mirrors the implementation, add literal rows; (2) outcomes.py:104-118 no test re-finishing a done/failed row; (3) outcomes.py:225-240 chain error caught and logged ERROR jobs.schedule.error (builder decision; record ruling); (4) spec line 641 vs U08-50 step 4 duration_s on job_yield (builder followed step 4); (5) outcomes.py:189 requeue with no scheduled_for falls through to failed (add an assert).
Verdict: Approved

# T08-22 review (verify agent, opus; recorded by sub-controller w24-s08 - verifier write refused by isolation)
Head 8f616da, base 9e88eeb. Verdict: Approved (0 Critical, 0 Important, 5 Minor).
Spec: U08-90 run_inline OK (steps 1-8, ExitStack unwind sigint->heartbeat->lock); U08-91 status_snapshot OK (15 keys = §3.17, acceptance test parses the spec table; bounded port reads only); U08-92 health OK (down/degraded incl. faults TH08-06/ok); U08-93 enqueue_resume OK (priority 80, idem resume:<run_id>).
Warnings: BT08-10 measures enqueue_resume -> in-process Supervisor -> fake handler (no CLI until T09-22; re-measure there); resume/checkpoint test = SIGINT after save_state, second run_inline does not repeat step 1 (jobs-layer guarantee only; no crash-between-effect-and-save case; not via enqueue_resume).
Evidence: 659 passed (unit jobs + integration jobs + IT01-08, -p pandas); cov 100% line/branch inline.py, status.py, _status_views.py; BT08-10 claim 0.052 s, first task done 1.374 s (< 30 s); status suite green at shifted T0 19:37 UTC, 2026-10-25 00:59 UTC (DST end), 2026-12-31 23:59:30; 10 mutations red (finish_job bypass, lock release skip, SIGINT restore skip, faults_enabled key drop, faults->degraded drop, idem_key, priority 79, swap-fail finish_job skip, held-lock finish_yield skip, heartbeat False ignored).
Deviations accepted: _status_views.py split with §2 row; §8.1 jobs.inline.heartbeat_failed; health applies liveness at given now; missing handler fails job then raises (matches child._run).
Minor:
M1 inline.py:157-162 no lease heartbeat / SIGINT handler during the swap (spec step order; start_timeout_s 900-1200 > lease_s 300): reaper may requeue; Ctrl+C during swap leaves job running until lease expiry. Suggest spec note.
M2 status.py:42,224 imports private breaker._family; expose breaker_settings(key).
M3 status.py:202-206 planned_rekey scans the 200 newest queued maintenance jobs (cap); dedicated port query -> ports/U08-98 follow-up.
M4 status.py:110-114 third copy of the U08-54 liveness rule (gpu.py:87-90, queue.py:354-359).
M5 inline.py:89 _beat catches only HernessError; other exceptions kill the heartbeat thread silently.

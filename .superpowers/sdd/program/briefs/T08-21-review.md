# T08-21 review (VERIFY, opus) - Supervisor, child entry and run_worker
Recorded by the sub-controller w20-s08 from the verify agent's hand-back (the verifier's worktree isolation refused writes outside the worktree). Worktree agent-a475763e8e03bde6c, base 9c8f34a, head 8061e24; verifier left the tree clean.
Verdict: Needs fixes (0 Critical, 3 Important, 8 Minor); runtime code judged sound.

## Gates (verifier)
- tests/unit/core/jobs + tests/integration/jobs: 615 passed; ID selection 30 passed; --require-test-ids clean.
- Coverage: supervisor.py 99% (100% line, partial 289->exit), child.py 99% (partial 97->exit), _supervisor_boot/_child/_gpu 100%.
- ruff, format, mypy strict 0 (297), lint-imports 13 kept, module_size 0, type_ownership 0.
- Flakiness: 3 integration worker files x3, 11/11 each.
- BT08-11 dry run 90 s: 0.000% (Windows tick-sampled accounting -> a bound; < 1% holds). Builder's 600 s run: 0.000%.
- Mutation probes red: stall no-terminate, child owner guard, bad frame not crashed, reaper counter, no redaction, failed-ticks 11, no shutdown stop, arbiter reject, SIGINT ignore. Survived: GPU claim min_priority=None; exempt kinds []; run_scheduler removed; run_due_probes removed; ctx.close() removed.

## Spec
U08-87 OK except queue-depth gauge (I2); U08-88 OK (child.py:95-128); U08-89 OK (supervisor.py:348-361); test IDs present, IT08-13 too weak (I1).

## Focus checks
1 OK spawn plain-string args, list argv, no shell=True, env only 2 non-secret test paths. 2 OK pipe cap/strict decode, bad frame -> child_crash (ST08-13). 3 OK stall -> terminate -> finish_job(ModelUnavailable("stalled")), IT08-06 real; no second advance_chain catch. 4 WARN step 7a correct, UT08-62 red on mutation, IT08-10 OK; scheduler/probe calls untested (I3). 5 WARN chat rule wired but IT08-13 does not guard it (I1, M2). 6 WARN IT08-09/IT08-08 recover; hard supervisor death leaves a non-daemon child that ignores stops running (M4). 7 OK exit 0/1 only. 8 OK request_rejected + requested_class cleared. 9 OK child_main U08-88. 10 OK BT08-11 method. 11 deviations accepted except missing queue-depth gauge (I2); IT08-03 re-check accepted as carry-over.

## Findings
Important:
- I1 IT08-13 does not test the chat-window claim filter: min_priority=None at supervisor.py:286 survives; test stops ticking at test_jobs_worker_gpu.py:76-78 so the assert at :80 cannot fail. Fix: tick more after both done, assert p40 still queued; unit test for claim filter args.
- I2 herness_jobs_queue_depth_count{gpu_class} gauge (spec :734, section 8.2 :2312) not emitted; no port query supports it. Controller decision.
- I3 deleting run_scheduler(now)/run_due_probes(now) at supervisor.py:234-235 survives; add a step-7a call test.
Minor:
- M1 --once with a spawn OSError loops forever (supervisor.py:293, :259, :184).
- M2 exempt kinds at _supervisor_gpu.py:60 unasserted.
- M3 ctx.close() ordering at child.py:111-112 unasserted.
- M4 orphan child after hard supervisor death possible; requeue may duplicate side effects (spec-conformant, untested).
- M5 Windows console break during child start-up -> child_crash with attempt charge.
- M6 lease heartbeats skipped while draining (supervisor.py:222-226); validation rule shutdown_grace_s < lease_s.
- M7 config_invalid logged without paths for bootstrap errors (supervisor.py:124-129, :358-359).
- M8 test_pt08_06 (not this card) flaked once under load.

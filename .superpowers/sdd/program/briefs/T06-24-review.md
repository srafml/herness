# T06-24 verify review: Escalation (impl 06 U06-134, U06-135)

Worktree agent-ad92a62ee1d3603a0, head 587fab8, base e41d62c. Files: `herness/harness/swarm/escalation.py` (209/220 lines), `tests/integration/harness/test_swarm_escalation.py` (17 cases).

### Spec Compliance

U06-134 escalate_to_review
- ✅ 1. `find_run_by_escalation` first; existing run with `meta.job_id` → `(run_id, job_id)` (escalation.py:105-107). Ids validated before use (:71-75, :104).
- ✅ 2. `RunRequest(kind, depth="fast", question, focus, budget_override=cfg.pipelines.pipelines.chat.escalation)` (:109-115); question redacted (fail-closed), then clipped to 2000.
- ✅ 3. `create_run_record(..., job_id=None, escalated_from={session_id, message_id}, current_build=read_current)` (:116-121).
- ✅ 4. `enqueue("review", {"run_id", "resume": True}, gpu_class="reasoning", priority=75, idem_key=f"escalate:{session_id}:{message_id}")` on the writer (:123-127).
- ✅ 5. `update_run_fields(meta_patch={"job_id"})` in `run_write` on the writer (:129-132).
- ✅ 6. `harness.chat.escalated` INFO with ids + kind only (:133-136).

U06-135 post_escalation_summary
- ✅ 1. Existing assistant row with `meta.escalation_run_id` → its id (:185-186).
- ✅ 2. First paragraph of `executive_summary` only for `mode == "full"`; no draft / findings_only → none (:149-156).
- ✅ 3. Valid markers whose id is in `p.numbers` replaced by `format_number`, others kept (:159-166); fallback text verbatim `"The review finished without an executive summary; see run " + run_id + "."` (:189).
- ✅ 4. `append_message(session_id, role="assistant", content, status="done", verified="verified" iff draft and passed else "partial", run_id, query_ids=sorted(...), meta={"escalation_run_id","mode":"escalation","numbers":[...]})` (:194-207). Content redacted (fail-closed) and capped at 20000.

- ⚠️ Cannot verify here: production binding of `append_message`/`find_message` (impl 09 has no assistant append and `find_assistant_message` looks up by `reply_to`, not `meta.escalation_run_id`) — accepted carry-over; IT06-14 (full flow) belongs to a later card.

### Verification run
- `pytest tests/integration/harness/test_swarm_escalation.py --cov=herness.harness.swarm.escalation --cov-branch`: 17 passed; escalation.py 86 stmts / 26 branches, 100% line, 100% branch.
- ruff check: clean; ruff format --check: clean; mypy (2 files): 0 issues; lint-imports: 13 kept, 0 broken; tools.check_module_size: exit 0.
- IT06-33 uses the real tmp migrated `ops_store`, real `queue` on `SqliteJobsBackend`; all test names carry `it06_33`, docstrings start with `IT06-33`, `pytestmark = pytest.mark.integration`.
- Mutation probes (each reverted with `git checkout -- herness/harness/swarm/escalation.py`; tree clean afterwards):
  1. Disable `find_message` early return (:185) → RED (`test_it06_33_call_twice_one_assistant_row`).
  2. Idem key `escalate:{s}:{m}:{run_id}` (:125) → RED (`test_it06_33_escalate_creates_run_and_one_job`).
  3. Disable step-1 early return (:106) → **survived** (17 passed). See Important #1.
  4. Constant `verified="verified"` (:199) → RED (`test_it06_33_fallback_content[none|failed]`).

### Focus checks
1. Idempotency. Retry after full success: step 1 returns the stored pair, nothing enqueued. Retry after crash between create and enqueue: run found without `job_id` → enqueue with the same key + patch, no second run (tested). Retry after enqueue but before patch: same path; `insert_job` returns the existing job id while that job is still `queued`/`running` (`job_idem_active` is a partial unique index over active statuses only). If that job already finished before the retry, a second `review` job for the same run is created (Minor #2). Concurrent first calls: `find_run_by_escalation` is a plain read on the caller thread, not inside the writer transaction that creates the run, so two concurrent calls for the same message can both create a run; the idem key then collapses them onto one job (payload names only the first run), both runs get `meta.job_id = J`, the second run stays `created` forever and is the one `find_run_by_escalation`/`find_run_by_job` return (newest first). No duplicate job, but an orphan run. The race is inherent in the spec's find-then-create algorithm and requires two concurrent escalations of one chat message (normally one chat job per message under a lease) → Minor #1.
2. Content is redacted then bounded (:192); the message references the run by id only (fallback text, `run_id`, `meta.escalation_run_id`). Job kind is the literal `"review"`; idem key and SQL params are built only from regex-validated ids; logs carry ids + kind only (tested: question absent from log events).
3. Deviations: (a) `find_message(session_id, run_id)` and (b) session from `run.meta.escalated_from` — both come from the run row written by `escalate_to_review` from validated, binding-supplied ids; neither function takes model text for ids; the session is re-validated (:140-146), satisfying TH06-17. (c) `current_build=read_current` matches U06-137 step 3. (d) Completing a run without `job_id` with the same key is a correct hardening of step 1 (spec would return `None` job id). (e) Clipping after redaction is the safe order (clipping first could split a secret and defeat the redactor); a clip may cut a redaction placeholder token in half, which only truncates the placeholder and cannot reveal redacted text — harmless. (f) The extra anchor user row "(escalation result)" exists only in the test-local adapter (carry-over); production needs an impl 09 assistant append / find-by-run.

### Strengths
- Tight, well-documented module within budget; fail-closed redaction with text-free errors; ids validated before they enter keys or queries; writes routed through the Blackboard writer; 100% line/branch coverage on a real migrated store.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
1. tests/integration/harness/test_swarm_escalation.py:267 — the retry assertion cannot distinguish step 1 (return stored run/job) from re-enqueue: removing the early return at escalation.py:106-107 survives all 17 tests, because the idem key dedupes against the still-queued job. Step 1 is the only guard once the first job has left `queued`/`running`. Fix: before the retry, mark the job `done` (or count `harness.chat.escalated` events / `job` rows across both calls) and assert one job, the same pair, and no second log event.

#### Minor (Nice to Have)
1. escalation.py:105-121 — find-then-create is not atomic; concurrent first calls for one message can create an orphan `created` run that also carries `meta.job_id` and becomes the newest match for `find_run_by_escalation`/`find_run_by_job`. Spec-inherent and unlikely; consider doing the lookup inside the create transaction on the writer (or a follow-up note for U06-137/U06-34).
2. escalation.py:106-127 — crash after enqueue and before the meta patch, followed by a retry only after the job has finished: the active-only idem index lets a second `review` job be enqueued for the same run (resume of a terminal run; likely a no-op). Edge case; document or check job status by idem key before enqueue.
3. Carry-over (accepted ruling, for tracking): production `append_message`/`find_message` adapters await an impl 09 assistant append and lookup by `meta.escalation_run_id`; the `find_message(session_id, run_id)` convention should be recorded for the T06-21/T06-25 caller.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Implementation conforms to U06-134/U06-135 and all gates pass, but the core idempotency guard (step 1 early return) is not pinned by any test (mutation survives); a small test addition closes it.

---

## Re-review r1 (head 7439b71, tests only; scope: Important #1)

- New case `test_it06_33_escalate_retry_after_finished_job` (tests/integration/harness/test_swarm_escalation.py:296-321): escalates, claims the job and finishes it through the real `finish_job` (asserts status `done`), retries and asserts the same `(run_id, job_id)`, exactly one `review` job row for the idem key, one run row, and no `harness.chat.escalated` event during the retry. Name, docstring (`IT06-33 ...`) and markers conform.
- Probe re-run: step-1 early return disabled (escalation.py:106) → RED (`test_it06_33_escalate_retry_after_finished_job`: retry returned a new job id). Restored with `git checkout -- herness/harness/swarm/escalation.py`; tree clean at 7439b71.
- Card file: 18 passed; escalation.py 100% line / 100% branch. ruff check, ruff format --check, mypy on the test file: clean.
- Important #1: resolved.
- Minor (new, non-blocking): the fixture now calls `bind_ops_backend(SqliteResilienceBackend())` (test file:62) without unbinding; at interpreter exit the registered atexit metric flush runs against the removed tmp store and logs `resilience.metrics.flush_failed error_type=SchemaViolation rows=4` after the pytest summary. Same pattern as existing tests (tests/integration/connectors/test_sync_jobs_flow.py:67, tests/integration/harness/test_loop_it.py), so project-wide noise rather than a defect of this card; worth a shared fixture that flushes/unbinds on teardown.
- Minors #1-#3 of the first review: parked by controller ruling.

**Task quality:** Approved

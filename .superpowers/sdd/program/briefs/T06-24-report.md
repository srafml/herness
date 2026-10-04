# T06-24 report — Escalation (impl 06 U06-134, U06-135)

Status: DONE_WITH_CONCERNS (carry-overs only). Worktree agent-ad92a62ee1d3603a0, base e41d62c.
Commit: 587fab8 feat(harness): T06-24 escalation to a mini swarm (all pre-commit hooks passed, incl. pytest-unit and detect-secrets)

## Implemented
- `herness/harness/swarm/escalation.py` (209/220 lines, L4): `JobsFacade` Protocol (spec note: no spec defines it; mirrors `herness.core.jobs.queue.enqueue`, the queue module satisfies it), `escalate_to_review` (U06-134), `post_escalation_summary` (U06-135).
- `tests/integration/harness/test_swarm_escalation.py`: 17 IT06-33 test cases (real tmp migrated `ops_store`, real job queue on `SqliteJobsBackend`, test redactor from `_blackboard_env`).
- `swarm/__init__.py`, impl 09 chat area, jobs/__init__ untouched.

## Behaviour
- escalate_to_review: session/message ids validated (`ses_<ULID>` / `msg_<ULID>`, SchemaViolation, before any key/query/write); `find_run_by_escalation` first → existing `(run_id, meta.job_id)`; question redacted with `redact_text` (None → PolicyViolation "escalation question failed redaction", nothing written), clipped to RunRequest's 2000-char max; `RunRequest(kind, depth="fast", question, focus, budget_override=cfg.pipelines.pipelines.chat.escalation)`; `create_run_record(..., job_id=None, escalated_from={session_id, message_id}, current_build=herness.store.warehouse.read_current)` run via `asyncio.to_thread` (it waits on the writer itself); `jobs.enqueue("review", {"run_id", "resume": True}, gpu_class="reasoning", priority=75, idem_key=f"escalate:{session_id}:{message_id}")` on the writer; `update_run_fields(meta_patch={"job_id"})` inside `run_write` on the writer; log `harness.chat.escalated` INFO with ids + kind only.
- post_escalation_summary: session from `run.meta.escalated_from.session_id` (validated; otherwise returns None, nothing written); `find_message(session_id, run_id)` → existing id; first paragraph of `executive_summary` for `mode == "full"` drafts; markers in `p.numbers` replaced with `format_number`, other/malformed markers kept; fallback text with the run id; content redacted (None → PolicyViolation, no row) and capped at 20000; `append_message(session_id, role="assistant", content=, status="done", verified=, run_id=, query_ids=sorted(...), meta={"escalation_run_id","mode":"escalation","numbers":[NumberRef JSON]})`; log `harness.chat.escalation_posted` with ids.

## Evidence
- RED: `PYTHONUTF8=1 uv run pytest tests/integration/harness/test_swarm_escalation.py` → `ImportError: cannot import name 'escalation' from 'herness.harness.swarm'` (1 error during collection).
- GREEN: same command → 17 passed; coverage escalation.py 86 stmts / 26 branches, 100% line and branch.
- tests/unit/harness/swarm + card test: 143 passed. (No separate wip checkpoint landed: the first commit attempt was refused by detect-secrets; the fix went into the single final commit.)
- ruff check/format clean; mypy (full `uv run mypy`) 0 issues in 334 files; lint-imports 13 kept 0 broken; check_module_size exit 0; check_type_ownership exit 0; detect-secrets clean (literal ULIDs in test params replaced by f-strings after the first commit attempt was refused by the hook).

## Deviations / concerns
1. Carry-over (controller ruling): impl 09 chat area has no assistant-row append (append_chat_message rejects role assistant) and no lookup by `meta.escalation_run_id`; IT06-33 binds test-local adapters composed of T09-03 public functions (list_chat_messages filter; append_chat_message anchor user row + upsert_assistant_placeholder + update_chat_message). The anchor adds a user row "(escalation result)" per summary — a production adapter needs an impl 09 assistant-append / find-by-run function.
2. `find_message` calling convention chosen as `find_message(session_id, run_id)` (positional, mirroring find_assistant_message's shape); document for callers.
3. `escalate_to_review` has no `current_build` parameter in U06-134; it passes `herness.store.warehouse.read_current` (per U06-137 step 3). Tests monkeypatch `escalation.read_current`.
4. Retry extension: a run found without `meta.job_id` (stopped between create and enqueue) is completed (enqueue with the same idem key + patch) instead of returning a None job id; no second run.
5. Redacted question clipped to 2000 chars (RunRequest max) rather than raising a pydantic ValidationError that would carry the text.
6. IT06-14 (full flow) belongs to a later card; not added.
7. Unit/integration broad suite not run (controller: no full suite); commit hooks ran pytest-unit.

## Fix round 1 (review Important #1)
- Commit: 7439b71 test(harness): T06-24 fix round 1 retry after finished job (tests only; escalation.py unchanged; all hooks passed, incl. pytest-unit and detect-secrets).
- Added `test_it06_33_escalate_retry_after_finished_job`: escalate, then `queue.claim(owner, ["reasoning"], job_id)` + `finish_job(..., JobOutcome(status="done"))` (real queue/backend API, no raw SQL writes), job status `done`; retry returns the same `(run_id, job_id)`, exactly one `review` job row for the idem key, one run, no second `harness.chat.escalated` event. The `store` fixture now also binds `SqliteResilienceBackend` (finish_job records resilience events).
- Probe: removing the step-1 early return (escalation.py:106-107) → `test_it06_33_escalate_retry_after_finished_job` FAILED (1 failed, 17 passed); restored with `git checkout -- herness/harness/swarm/escalation.py` → 18 passed.
- Gates: ruff check/format and mypy on the test file clean; card tests 18 passed. A `resilience.metrics.flush_failed` WARNING appears at process exit (metric buffer flushed after the tmp store is torn down); it does not affect results.
- Minors #1-#3 parked by controller ruling, not addressed.

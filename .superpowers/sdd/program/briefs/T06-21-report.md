# T06-21 report (build agent): Run lifecycle helpers

Status: DONE_WITH_CONCERNS (interim Scenario stand-in per sub-controller ruling; see carry-overs). Base 3f61b67.

## Files
| File | Lines | Budget |
|------|-------|--------|
| herness/harness/swarm/lifecycle.py (new) | 264 | 330 (ENG 400) |
| tests/unit/harness/swarm/test_swarm_lifecycle.py (new) | ~500 | n/a |
| tests/unit/harness/test_tools_recording.py (+1 allowlist line) | n/a | n/a |

herness/harness/swarm/__init__.py untouched (docstring-only; T06-13 adds re-exports).

## Implemented
- U06-76 RunRequest (pydantic, extra=forbid, strict=False; chat requires a non-empty question; field bounds per spec).
- U06-77 RunResult (pydantic, frozen, extra=forbid).
- U06-83 request_config_hash: cfg_ + first 16 hex of sha256 over canonical_json of config (config_hash of cfg) and request (kind, depth, profile, question, focus, scenarios, budget_override), request fields from req.model_dump in json mode.
- U06-137 create_run_record: review-kind check, profile, resolve_knobs, build (NotFound when none), hash, run_id = run_ + new_ulid, planner TaskSpec (scope run:RUN_ID, objective "Plan the KIND review.", default_tools and role_budget with writer_tokens=0, model_role planner, compute_dedup_key). Run and planner inserted in ONE run_write transaction, op swarm_create_run, via bb_writer.run_on_writer_sync. meta per postcondition incl. job_id (D06-26). No log here (harness.run.created belongs to U06-79).
- U06-84 handle_budget_exhausted: select pending analyst/skeptic tasks; per task on the writer claim_task then fail_task with BudgetExceeded run_budget; CAS run to verifying from running/challenging (op swarm_budget_exhausted); WARNING harness.budget.exhausted with run_id, phase=analysis, tasks_dead only. Running tasks untouched.
- U06-86 swarm_health: select_runs over non-terminal statuses and review kinds; HernessError gives down with the class name; open runs and worker_alive false gives degraded "no live worker for N open runs"; list_jobs queued and running review jobs (limit 50); an open run older than 24 h (injected now) not referenced by payload.run_id or meta.job_id gives degraded "stalled run RUN_ID"; else ok with empty reason.

## Tests
- UT06-54: request validation (chat without question, bounds, JSON input), RunResult, create record rows, meta and planner, profile and build precedence, bad override writes nothing, no promoted build writes nothing, chat kind rejected.
- UT06-55: equal twice; depth and profile change the hash; build_id and session_id excluded; question included.
- UT06-57: ok (two setups), stalled run degraded, referenced by payload or meta.job_id ok, no live worker degraded, unreadable store down (monkeypatched select_runs; real renamed run table; list_jobs HernessError).
- UT06-91: pending analyst and skeptic dead with class BudgetExceeded and message run_budget; running analyst and pending writer untouched; run verifying; log fields exact; nothing pending with status outside allowed_from; lost claim skipped.

Card tests: 29 passed. tests/unit/harness: 1678 passed, 1 skipped (Windows symlink privilege).
Coverage of lifecycle.py: 100% line, 100% branch (123 statements, 22 branches).
Gates: ruff format, ruff check, mypy (305 files plus the test file), lint-imports (13 kept), check_module_size exit 0, check_type_ownership exit 0.
RED: the card test file failed collection with ImportError, cannot import name lifecycle from herness.harness.swarm. GREEN: 29 passed.

## Spec notes and deviations
1. Scenario stand-in per sub-controller ruling: RunRequest.scenarios is list of str, max 5 items, each 1-64 chars, with a "# T04-20:" marker; U06-76 names list of Scenario or str. herness.metrics.portfolio.Scenario (U04-76, T04-20) does not exist yet.
2. request_config_hash hashes the resolved profile argument as request.profile (accepted ruling).
3. create_run_record.current_build is typed Callable returning str or None; None raises NotFound "no promoted warehouse build" before any write (accepted ruling). escalated_from is typed Mapping or None and stored as a dict.
4. create_run_record raises ConfigError for a non-review kind (U06-137 precondition) before any write.
5. swarm_health: a HernessError from the list_jobs reads also returns down with the class name. The spec names only select_runs; the job table lives in the same ops store, so unreadable-means-down is applied to both reads. ok carries an empty reason.
6. The RunRequest chat invariant rejects an empty question as well as None.
7. U06-84 names no op for the run CAS; used op swarm_budget_exhausted. select_tasks runs on the caller thread (reads use the thread connection, R-10); claims, fails and the CAS run on the writer.
8. The UT05-124 allowlist _NON_ROW_HASHES in tests/unit/harness/test_tools_recording.py gains swarm/lifecycle.py request_config_hash: the U06-83 sha256 is a config hash, not a row hash.

## Carry-overs
- T04-20: widen RunRequest.scenarios to list of Scenario or str when herness.metrics.portfolio.Scenario lands. request_config_hash must then canonicalise Scenario objects via model_dump in json mode (the path already used); add a UT06-55 case with a Scenario object and check its Decimal budget_usd serialises as a string.
- T06-13: re-export RunRequest and RunResult from herness/harness/swarm/__init__.py; bind swarm_health list_jobs and worker_alive to herness.core.jobs.queue (not exported from herness.core.jobs yet).
- IT06-03, IT06-32, ST06-13 belong to later cards.

## Checkpoints
- 373b587 wip(T06-21): lifecycle helpers and UT06-54/55/57/91 tests. All pre-commit hooks passed, pytest-unit included. A first attempt did not land because I edited a comment while its hooks ran; a clean retry succeeded.

## Fix round 1

Review minors M1-M3, tests only. Only tests/unit/harness/swarm/test_swarm_lifecycle.py changed (524 -> 574 lines). herness/harness/swarm/lifecycle.py is byte-identical to ab6e925 (`git diff --quiet ab6e925 -- herness/harness/swarm/lifecycle.py` returned 0).

- M1 (UT06-57 24 h boundary): `test_ut06_57_exactly_24h_is_not_stalled` adds runs aged exactly 24 h and 24 h minus 1 s, no jobs, and expects `ok`, because U06-86 step 4 says "started more than 24 h ago". `test_ut06_57_just_over_24h_is_stalled` adds a run aged 24 h plus 1 s and expects `degraded` / `stalled run <id>`.
- M2 (UT06-54 one transaction): `test_ut06_54_run_and_planner_in_one_transaction` monkeypatches `lifecycle.insert_tasks` with a wrapper that runs the real `insert_tasks`, records `count(*) FROM run` seen inside the transaction (asserted to be 1, so the failure happens after `insert_run`), and then raises. run_write maps the non-Herness error to `FatalError` ("_TaskInsertError" in the message). The test then asserts 0 run rows and 0 task rows.
- M3 (UT06-55 focus): the equal-twice case now has `focus=EntityScope(team, [t1, t2], period_start=2026-01-01)` and round-trips through `model_dump_json` / `model_validate_json`. A changed focus (`entity_ids=[t1]`) now gives a different hash.

Probes (each one undone with `git checkout -- herness/harness/swarm/lifecycle.py`):
- M2: `insert_run` and `insert_tasks` split into two run_write calls -> RED (`test_ut06_54_run_and_planner_in_one_transaction`: the run row was still there; 1 failed, 31 passed).
- M1: `now - run.started_at <= STALL_AFTER` changed to `<` -> RED (`test_ut06_57_exactly_24h_is_not_stalled`; 1 failed, 31 passed).
- Extra, M3: `"focus"` removed from `_HASHED` -> RED (`test_ut06_55_hash_stable_and_depth_sensitive`; 1 failed, 31 passed).
- After restoring: GREEN.

Gates (worktree .venv python, PYTHONUTF8=1): card file 32 passed. `tests/unit/harness/swarm` + `test_tools_recording.py`: 245 passed. ruff format --check: clean (811 files). ruff check: clean. mypy on the test file: no issues. tools.check_module_size: exit 0.
No real bug was found in lifecycle.py.

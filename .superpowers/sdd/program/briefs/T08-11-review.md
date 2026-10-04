# T08-11 review: Jobs store backend (impl 08 U08-95, U08-96)

Worktree agent-a8b97afa9a542a521, base 750ec27, head 455f5af. Reviewer: verify agent (read-only).

### Spec Compliance
- ❌ Issues found:
  - U08-95 `insert_job` sched check binds `:sched_key` to `job.idem_key` (jobs.py:132). For `idem_mode == "sync"` (U08-72 step 1d: `idem = f"sync:{source}"`, submitted with `sched_check`), the check matches any earlier `sync:<source>` job in any status, so every later fire is deduped to the first finished job forever. Design 08 §5.7 says the check is for a job with a `sched:` key. See I1.
  - IT08-02 "never two exclusive kinds running" is not proven: the test still passes with the exclusive rule removed from the claim SQL. See I2.

Per row:

| Row | Result | Note |
|-----|--------|------|
| U08-95 insert_job | ❌ | the INSERT/ON CONFLICT/RETURNING and the active-idem fallback match §5.7 exactly; the sched_key binding is wrong for sync-mode schedules (I1) |
| U08-95 claim_job | ✅ | matches §5.7 exactly, plus the min_priority/exempt clause and the slot rule `(j.kind IN gpu_slot_kinds) = (:slot = 'gpu')` inside the subquery; ORDER BY priority DESC, scheduled_for, created_at; outer `AND status='queued'`; RETURNING * |
| U08-95 claimable_counts | ✅ | same ELIGIBLE fragment with slot 'gpu'; every class of `classes` is a key (0 by default) |
| U08-95 heartbeat / finish_done / yield / requeue / failed / finalize_canceled / save_job_state | ✅ | every one guarded by `job_id AND lease_owner AND status` (TH08-08); columns as in U08-95 |
| U08-95 cancel_job / retry_job | ✅ | D5 (two status-guarded UPDATEs) gives the same results as U08-51. retry follows U08-52 (attempts 0, scheduled_for now, lease and finished_at cleared, last_error kept); IntegrityError is caught inside the callback and returns `conflict` |
| U08-95 reap_expired / requeue_owned | ✅ | the two §5.7 statements are verbatim, with a SELECT of the affected rows first, all in one run_write. queued/failed is decided from the same `attempts < max_attempts` test. OWNED is swapped in for the expiry test |
| U08-95 running_on_host | ✅ | `\ % _` are escaped, and `ESCAPE '\'` is used |
| U08-95 postpone_class / sched_fired / recent_scheduled / rekey_on / queue_stats / get / list | ✅ | each matches its U08-41 row |
| U08-96 WorkerSqlMixin | ✅ | upsert ON CONFLICT(worker_id) DO UPDATE; the allowlist is exactly the six columns; values are bound; set_requested_class and run_row match the SQL of U08-96 |
| UT08-53 (backend half) | ✅ | both halves pass, but there is no sync-idem case (I1) |
| UT08-62 (backend half) | ✅ | 1/3 → queued with LeaseExpired; 3/3 → failed; both rows reported |
| UT08-65 (backend half) | ✅ | 89 s / 91 s |
| IT08-02 | ❌ | no double claim ✅ (set check plus every row `done` with attempts 1); per-thread priority order ✅ (sound, because non-exclusive jobs are always eligible); exclusive rule ❌ (I2) |
| ST08-08 | ✅ | A's seven writes change 0 rows; B's result is kept |
| Acceptance: EXPLAIN QUERY PLAN | ✅ | I checked it myself: `SEARCH j USING INDEX job_claim (status=? AND gpu_class=? AND scheduled_for<?)` and `SEARCH r USING INDEX job_claim (status=?)`; no SCAN of job (temp B-tree for ORDER BY, as expected) |
| Transaction boundaries | ✅ | every write is in run_write; nothing in jobs.py or worker.py records metrics or events; rows are parsed outside the transaction |
| write_open (ruling 6) | ✅ | mirrors run_write's own nested guard (`conn.in_transaction` on the per-thread connection). Probe: False outside, True inside run_write, False on another thread |
| ops/__init__ block order, UT02-68 | ✅ | "08 jobs" sits after "08 metrics" and before "09 ui_reads" in both blocks; tests/unit/store is green |
| Budgets | ✅ | jobs 337/400, _job_sql 154/200 (§2 row added), worker 123/200, ports 300/300, core/resilience/ports 143/190, metrics 243/260, store resilience 234/320, __init__ 310/400; check_module_size exit 0 |
| Test IDs / docstrings / pytestmark | ✅ | every function name carries an ID, every docstring's first line starts with that ID; unit/unit/integration markers set |

- ⚠️ Cannot verify from diff / notes:
  - The reaper `failed` statement keeps `lease_expires_at` (D3, verbatim from design). Harmless, but a failed row keeps a stale lease timestamp.
  - `write_open()` calls `connection()`, which opens and logs a connection on a thread that has none. The flush it guards would open one anyway, so there is no new effect in practice. core.py is frozen, so a cheaper cached-entry check is not available.
  - SqliteJobsBackend does not yet satisfy the whole JobsBackend protocol, because the task methods come with U08-97 (D8). bind_core_backends has to wait for that.

### Strengths
- The SQL is close to verbatim design §5.7 and U08-95. Keeping it in one constant module (`_job_sql.py`) makes auditing easy, and there is no string splicing: lists go through `json_each` and the `update_worker` columns come from a frozen allowlist.
- The owner and status guard is on every completion and state write. ST08-08 exercises all seven stale-owner writes.
- The reaper reports and updates rows in one transaction and uses the same predicate, so its report cannot disagree with the rows it changed.
- The write_open deferral is small, correct and tested both ways (defer inside the write; a store error does not defer).
- Gates, run by me: 1245 passed and 1 skipped (symlink privilege) across tests/unit/store, tests/unit/core/jobs, tests/unit/core/resilience and tests/integration/jobs. Coverage: jobs.py, _job_sql.py, worker.py, ports.py and store resilience.py at 100 % line and branch; core/resilience/metrics.py at 99 % (line 182 missed). ruff check shows only the 7 known TID251 hits; ruff format is clean; mypy has 0 errors in 225 files; lint-imports keeps 13 contracts; check_module_size and check_type_ownership both exit 0.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
- **I1 herness/store/ops/jobs.py:130-135 (with _job_sql.py:18-22): the sched check blocks every later fire of a sync-mode schedule.** `{"sched_key": job.idem_key, ...}` is only correct when the idem key is `sched:<name>:<F>`. U08-72 step 1d gives sync-mode schedules `idem = "sync:<source>"` and still passes `sched_check=(name, ts(F))`. `SCHED_SEEN`'s `idem_key = :sched_key` term then matches the earliest `sync:<source>` job in any status (done, failed or canceled), and insert_job returns `(old_id, False)` for every future fire.
  - Probe: enqueue `sync:jira` for fire F1, claim it, finish it done, then enqueue `sync:jira` for fire F2 with `SchedCheck("sync.jira", F2)`. The result is `('job_1', False)`: no job is created, and scheduled syncs stop forever.
  - Design 08 §5.7 frames the check as "no job with a `sched:` key exists in any status". U08-46's postcondition is "a finished job with a `sched:` key ... blocks re-creation".
  - Fix: bind `sched_key = f"sched:{sched_check.schedule}:{sched_check.fire_at}"` (equal to the idem key for non-sync schedules; for sync mode it matches nothing, and the payload `schedule`/`fire_at` test still dedupes the same fire).
  - Add a UT08-53 or UT08-54 backend test: a done `sync:jira` job for fire F1, then submit fire F2 with the same idem key and sched_check → created.
- **I2 tests/integration/jobs/test_jobs_claim_concurrency.py:107-112, 144: the exclusive-rule check in IT08-02 proves nothing.** The in-memory counter is held only around `out.append(...)`, a window of microseconds, and it is released before `finish_done`, so two concurrent exclusive leases are almost never both inside it.
  - Mutation probe: I deleted the `NOT (j.kind IN exclusive AND EXISTS running exclusive)` clause from `_job_sql.CLAIM` and ran the test body. IT08-02 passed in 3 of 3 runs.
  - UT08-57 covers the rule single-threaded, but the IT08-02 row's third expectation ("never two exclusive kinds running") is not demonstrated.
  - Fix: after an exclusive claim returns, the claiming thread reads `SELECT COUNT(*) FROM job WHERE status='running' AND kind IN (<exclusive>)` and records a violation when it is not 1. While our job is running, no other thread can legitimately start one, so the check is exact.
  - Then re-run the mutation and confirm the test goes red.

#### Minor (Nice to Have)
- **M1 tests/integration/jobs/test_jobs_claim_concurrency.py:84-114: an exception that kills a worker thread does not fail the test.** An example is StoreBusy after the sqlite_write retries; the report mentions ~5 s busy waits. The exception only raises pytest's PytestUnhandledThreadExceptionWarning, and `filterwarnings = ["error:::herness"]` does not escalate it. A thread that dies inside `claim_job` leaves nothing running, so the `>= 2` check and the 9th-owner drain hide it. Fix: wrap the loop body in `try/except BaseException as exc: exclusive.errors.append(exc)`.
- **M2 herness/core/resilience/metrics.py:182: the changed line `_auto_flush()` in `record_gauge` is not covered.** The module is still at 99 %. One gauge record with an elapsed interval would cover it.
- **M3 herness/store/ops/_job_sql.py:145-149: `NEXT_JOB`'s "claim order" ignores class, slot and exclusive eligibility.** This is acceptable for a status display, but the QueueStats spec note could say "priority order" rather than "claim order" to avoid overclaiming.

### Assessment
The 9th-owner sweep does not hide a claim starvation defect. The leftover jobs are exclusive ones: exclusive jobs are serialised, and every `None` result from `claim_job` uses up one of a thread's 1 000 attempts, so late in the run seven of the eight threads burn their attempts while one exclusive job runs. The drain then proves every remaining job is still claimable, single-threaded. SQLite's busy handler is not fair (per the report, some threads waited ~5 s per BEGIN IMMEDIATE), which matters for BT08-03, not for correctness.

**Task quality:** Needs fixes
**Reasoning:** The SQL, guards, transactions, index use and write_open change are correct and well tested. Two fixes are needed before approval: I1 (the sched check permanently suppresses every sync-mode scheduled fire after the first) and I2 (IT08-02 cannot detect a broken exclusive rule, shown by mutation).


## Re-review round 1 (head cfe1f80, round diff 455f5af..cfe1f80)

Scope: findings I1, I2, M1, M2, M3 and the lines this round touched. Read-only; `git status` was clean before and after the probes.

| Finding | Result | Evidence |
|---------|--------|----------|
| I1 sched check blocks every later sync-mode fire | ✅ resolved | jobs.py:129-132 now binds `sched_key = f"sched:{schedule}:{fire_at}"` from the SchedCheck. Probe re-run: F1 (`sync:jira`) done, then F2 → `('job_2', True)` (was `('job_1', False)`). New tests `test_ut08_53_sync_mode_schedule_fires_again` (F2 created; the same F2 is deduped both while queued and after done) and `test_ut08_53_sched_key_alone_blocks_the_fire` cover both terms of SCHED_SEEN. Non-sync schedules behave as before (key equals the idem key). |
| I2 IT08-02 exclusive check proves nothing | ✅ resolved | Each thread holding an exclusive job reads the committed count of running exclusive jobs on its own connection, 3 times, and records any count other than 1. The check is exact because claims are serialised by BEGIN IMMEDIATE. Mutation probe re-run (exclusive clause replaced in memory, no file change): red 3/3, failing at `assert shared.violations == []` (line 158). Unmutated: green. |
| M1 thread exceptions lost | ✅ resolved | `_worker` wraps `_claim_loop` in `except BaseException` and appends to `shared.errors` under the lock. A lost lease now raises inside the thread. `errors == []` is asserted after the 8 threads and again after the drain. |
| M2 `record_gauge` `_auto_flush` uncovered | ✅ resolved | The deferral test now records counter, gauge and histogram inside run_write and flushes through `record_gauge` outside it. metrics.py is at 100 % line and branch. |
| M3 NEXT_JOB "claim order" wording | ✅ resolved | The wording is now "highest-priority due, else earliest not yet due; eligibility not applied", consistently in ports.NextJob, the _job_sql comment, the U08-41 spec note and the test docstring. The SQL is unchanged. |

Regression check on touched lines: none found.
- ports.py stays at 300/300 and _job_sql.py is 155/200. check_module_size exits 0.
- Tests: tests/unit/store, tests/unit/core/jobs, tests/unit/core/resilience and tests/integration/jobs → 1247 passed, 1 skipped (symlink privilege).
- Coverage: jobs.py, _job_sql.py, ports.py and metrics.py all at 100 % line and branch.
- ruff check shows only the 7 known TID251 hits; ruff format is clean; mypy reports 0 errors; lint-imports keeps 13 contracts.

Open findings: none.

**Task quality:** Approved
**Reasoning:** Both Important findings are fixed and confirmed by the probes: the sync-mode schedule fires again, and IT08-02 goes red when the exclusive rule is removed. All three Minor findings are closed, and nothing regressed.

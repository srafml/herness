### Spec Compliance
- ✅ Spec compliant. Each unit was checked against the brief text word for word:
  - ✅ U09-90 `submit_job`. Serialises with `json_default` and raises `UserInputError("job payload too large")` above 65,536 UTF-8 bytes, before any jobs call (wait.py:96; UT09-82 asserts `fake.calls == []`). Calls `jobs.enqueue(kind, body, gpu_class, priority, scheduled_for, idem_key=)`, then logs `cli.job.enqueued` (INFO job_id, kind). The priority default is `jobs.MANUAL_PRIORITY` (80) and `None` is passed through to the impl 08 default (asserted). Unless `inline`, when no worker is alive it writes the exact R-45 Warning/Fix pair to stderr, sets `opts.worker_warned` and logs `cli.worker.absent` (WARNING job_id). Naive `scheduled_for` is rejected downstream by `JobSpec._scheduled_for_aware` (herness/core/types/jobs.py:89), which is acceptable.
  - ✅ U09-91 `follow_job`. Makes one `jobs.get` per poll. run_id is taken from result, then payload.request, then payload, string values only (wait.py `_run_id`). A progress line is printed only on a change of (status, attempts, review task counts) and is suppressed by `--quiet`. The worker check repeats only while `worker_warned` is false. Terminal mapping: done 0; partial 6 with the exact warning and `partial=True`, from `result.partial is True` or `get_run(run_id).status == "partial"` (a None run is handled); `skipped_open_circuit` 4 with the exact warning; failed gives `exit_code_for_class_name(class, key=last_error.get("key"))`; canceled 1. Sleeps with `clock.sleep(poll_s)`. After `MAX_FOLLOW_S = 172_800` it detaches with exit 0 and the exact note. Ctrl+C gives exit 130 detached with "Detached; job `<id>` keeps running." and `cli.job.detached`. On StoreBusy it logs WARNING `cli.job.poll_busy` and retries; the 10th consecutive failure raises. It never cancels the job (probe P6). The FollowOutcome fields match.
  - ✅ U09-92 payloads. `STAGE_ORDER` is exact. `pipeline_payload` produces `{"stages": list, "build_id", **extra}` and an unknown stage raises UserInputError. `review_request` builds `RunRequest(kind, depth, scenarios=[Scenario(name=f"custom_{int(b)}", budget_usd=b)], question).model_dump(mode="json")` under the `request` key. `parse_budget_usd` accepts digits with `_`/`,` separators and an optional `.00`, and enforces 1 ≤ v ≤ 10^12, else UserInputError; the input is never echoed. DD-10 keys are the default (ruled).
  - ✅ U09-103 `run_job_inline`. `require_role(actor, "job_inline")` is the first statement (wait.py:239), and `job_inline` maps to admin in `ACTION_ROLES` (herness/reports/rules.py:71). Then `inline_started`, `run_inline`, `get`. `yield` prints the exact "Interrupted; job `<id>` was released and stays queued." message and returns 130 detached. Otherwise it applies the U09-91 step 5 mapping, then logs `inline_completed` (job_id, kind, status, exit_code). JobStateError and handler errors propagate.
- Builder deviations, judged:
  - (1) The 8-argument `submit_job(opts, *, ..., inline=False)` is **acceptable**: step 4 needs `opts` and the inline condition. `# noqa: PLR0913` carries its reason.
  - (2) Public `stages_from` is **acceptable**. It is the spec's "`--from-stage S` yields the suffix" behaviour with a UserInputError, but it is outside the §2 export list. The T09-22..25 callers should use it rather than re-slice.
  - (3) Failed/canceled messages go to stderr in human mode only, and JSON callers build the envelope from `outcome.job.last_error`. This is **acceptable**: FollowOutcome has no error field, and the message passes through `_clean`.
  - (4) The partial and circuit warnings are only in `FollowOutcome.warnings`. This is **spec-conformant**: step 5 says "add the warning", not print it. The caller's `emit` must print them, which is T09-22..25's obligation.
  - (5) Ctrl+C before the first `get` re-raises. This is **acceptable**, because FollowOutcome.job requires a row and U09-84 maps the exception to 130. The cost is that the detach line and `cli.job.detached` log are not emitted in that window (Minor 5).
- ⚠️ Cannot verify from the diff: the end-to-end JSON-mode envelope and the printing of `warnings` (deferred to T09-22..25 wiring); IT09-09/IT09-28/ST09-29 (not in this card); the real `jobs.run_inline` SIGINT/lease behaviour behind `yield` (faked here).

### Strengths
- The TH09-27 guard is tested in the strongest form. UT09-100 asserts `fake.calls == []` for a reviewer plus one audit call, so removing the role check or moving it after `run_inline` are both caught (P1, P2).
- Every spec message and log event is asserted verbatim, including full structlog dicts for `cli.worker.absent`, `cli.job.enqueued`, `cli.job.detached` and `inline_started`/`inline_completed`. The fake-clock assertions pin the poll count and elapsed time (4.0 s; 49 gets / 172,800 s).
- StoreBusy handling is real: WARNING logs, the counter resets on success (`[1, 2, 1]`), and exactly 10 gets before the raise.
- `herness.store.ops` is imported lazily (BT09-07), and the jobs module reference is resolved at call time, which gives clean seams without extra layers.
- Budgets: wait.py 254/300, payloads.py 84/180, lifecycle.py 264/330. `lint-imports` 15 kept / 0 broken; `check_module_size` exit 0; ruff, ruff format and mypy are clean on the changed files (re-run here). wait.py has no runtime third-party import, and payloads.py imports only pydantic.
- Card tests: 53 passed in 4.65 s; the UT06-55 tests pass (3).

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. **Payload-size boundary is only tested at 70 KB.** tests/unit/cli/test_cli_wait.py:389 tests a 70,008-byte payload and :414 tests exactly 65,536 accepted, but no case covers 65,537. Probe P3 (`> 70_000`) survived. JobSpec has no payload cap of its own (it caps only `result`), so `submit_job` is the only 64 KB guard on this path. Add a 65,537-byte case.
2. **run_id precedence is not tested.** tests/unit/cli/test_cli_wait.py:197-213 never uses a row where `result.run_id` and `payload.run_id` differ. Probe P8 (reversed order) survived. Add one row carrying both.
3. **Budget-parsing and scenario-name gaps.**
   - test_cli_payloads.py:74-92 has no two-digit non-zero fraction case such as `1.50`. Probe P25 (`\.[0-9]{2}`) survived.
   - P22 (`custom_{b}`) survived, because `parse_budget_usd` already normalises its output. `review_request` is public and accepts any Decimal, though: `Decimal("5.00")` gives a correct `custom_5` today, but this is not pinned by a test.
4. **Worker warning can print at the terminal poll** (wait.py:175). The worker check runs even when that poll returns a terminal row. A worker that exits right after finishing the job then yields "No worker is running; the job is queued." alongside a done job. This is literal to the spec's step order (step 4 before step 5), but could be gated on a non-terminal status.
5. **Unhandled edge cases.**
   - wait.py:231: Ctrl+C before the first row re-raises, so neither "Detached; job `<id>` keeps running." nor `cli.job.detached` is emitted in that window. This is deviation 5, accepted.
   - wait.py:147-148: with `result.partial is True` and no run_id, the warning reads "Run `None` finished partial".
   - wait.py:175-177: a StoreBusy from `worker_alive()` or `ui_task_status_counts()` discards an already-read row and counts as a failed poll.
6. **Wasted store reads under `--quiet`.** wait.py:191 computes `ui_task_status_counts` on every poll of a review job even with `--quiet`, where no line is ever printed. Skip the counts when quiet.
7. **Heavy module-level import workaround in the tests.** tests/unit/cli/test_cli_wait.py:37 warms 33 `herness.enrich` lazy names at collection, about 2.3 s, loading LanceDB and pandas (measured; torch is not loaded).
   - It is sound and documented in place, and the implementer recorded the carry-over for T11-03 (freezegun `extend_ignore_list`).
   - The risk is that if a future facade name imports torch, collecting this file fails on runners without CUDA. The fix belongs in tests/support/fake_clock.py (T11-03).
8. **Weak RED evidence.** The RED evidence for UT09-67/68/69/82/100 is a collection ImportError, which is valid but not behavioural; UT09-70 has a behavioural RED (2 failed before the lifecycle widening). The new UT06-55 Scenario case (test_swarm_lifecycle.py `test_ut06_55_scenario_entry_round_trips_and_hashes`) has no RED output of its own. It would have failed before 930e9ba, but this is not shown.
9. **Validation detail dropped.** payloads.py:66-72 turns every pydantic ValidationError into the single generic "invalid review request" (`from None`). That is safe, since no input is echoed, but which field failed (depth or budget count) is lost. A `details={"field": ...}` from `exc.errors()[0]["loc"]` would help users without leaking values.

#### Mutation probes
Probes were run on herness/_cli/wait.py and payloads.py, one at a time. P1–P5 ran against all of tests/unit/cli (about 72 s each); the rest ran against the two card test files, and the run was switched for time. Each probe was restored with `git checkout`.
- P1 remove `require_role` in run_job_inline → caught
- P2 move `require_role` after `run_inline` → caught
- P3 size check `> 70_000` → **survived** (Minor 1)
- P3b size check `>= 65_536` → caught
- P4 partial exit 6→4 → caught; P4b circuit exit 4→6 → caught
- P5 drop the `opts.worker_warned = True` set → caught
- P6 follow cancels the job on detach → caught
- P7 never raise after StoreBusy (limit 1000) → caught; P7b raise at 11 → caught
- P8 reversed run_id precedence → **survived** (Minor 2)
- P9 progress ignores `--quiet` → caught; P15 progress printed every poll → caught
- P10 max-follow detach exit 0→130 → caught; P10b `MAX_FOLLOW_S` 86,400 → caught
- P11 inline `yield` exit 130→0 → caught; P17 Ctrl+C exit 130→1 → caught
- P12 failed ignores `last_error.key` → caught
- P13 `inline` flag ignored in submit → caught
- P14 canceled exit 1→0 → caught
- P16 `inline_completed` at DEBUG → caught
- P18 run-status partial ignored → caught
- P19 budget upper bound `<` → caught; P20 lower bound 0 → caught
- P21 `stages_from` off by one → caught; P23 no stage check in pipeline_payload → caught
- P22 `custom_{b}` instead of `custom_{int(b)}` → survived (equivalent for parsed inputs; Minor 3)
- P24 `model_dump()` without `mode="json"` → caught
- P25 budget accepts `.NN` → **survived** (Minor 3)
- Tree restored: `git status --short` and `git diff --stat` are empty, and HEAD is still dddf964.

### Assessment
**Task quality:** Approved
**Reasoning:** All four units match the brief word for word, including messages, log events, exit codes, the 48 h detach, the StoreBusy limit and the TH09-27 role check. The tests catch 25 of 29 mutations, including every threat and exit-code probe. Of the four survivors, P3, P8 and P25 are untested boundary and precedence cases (Minor), and P22 is equivalent for parsed inputs; none is a code defect.

### Re-review r1
Scope: f819e18, one commit on dddf964, tests only. This pass was read-only: no edits and no probes. `tests/unit/cli` was run once: 123 passed in 71.65 s. `git diff --stat dddf964..HEAD` touches only tests/unit/cli/test_cli_wait.py and tests/unit/cli/test_cli_payloads.py, and `git status --short` is empty.

The three survived probes are each killed by a new test. This was checked by reading the tests against the mutations, not by re-probing, since this pass is read-only.
- **P3 (`> 70_000`): killed.** tests/unit/cli/test_cli_wait.py:442 `test_ut09_82_payload_size_boundary`.
  - 65,537 bytes raises `UserInputError("job payload too large")`, and `fake.calls == []` shows it happens before enqueue.
  - 65,536 bytes is enqueued unchanged.
  - The sizes are fixed by `_sized` (:434), which asserts the exact byte count using the same `json.dumps(default=json_default, ensure_ascii=False, separators=(",", ":"))` call as submit_job. With the `> 70_000` mutant, the 65,537 case is accepted and the test fails.
- **P8 (reversed run_id order): killed.** tests/unit/cli/test_cli_wait.py:216 `test_ut09_67_run_id_precedence`.
  - With three distinct run_ids, result wins over payload.request, which wins over payload.run_id; the payload-only fallback is also covered.
  - Canceled sync rows in JSON mode mean no store read and no output, and the first poll is terminal, so there is no sleep and no clock dependency.
  - Under the reversed order, the first assertion yields `run_payload`.
- **P25 (`\.[0-9]{2}`): killed.** tests/unit/cli/test_cli_payloads.py:83-85 reject `1.50`, `2.5` and `2.01`; :60-62 accept `2.00`, `2,000,000.00` and `1_500_000`. Under the mutant, `1.50` and `2.01` parse to in-range Decimals and are no longer rejected.

Conventions: the new functions carry the IDs (`test_ut09_67_…`, `test_ut09_82_…`) and their docstrings start with "UT09-67" / "UT09-82". The parametrize rows extend existing UT09-70 functions. Every assertion checks a concrete spec value; none is vacuous.

Findings: none. Minor items 4–9 of the first review stay as recorded and are not in this round's scope.

**Task quality:** Approved

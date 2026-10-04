# T09-21 build report: Job submission, following and payloads

Status: DONE_WITH_CONCERNS. The coordinator fixed the torch environment (torch
2.11.0+cu130), so the commit hooks now pass, including pytest-unit over the whole unit set.
Branch `claude/w32-c09-T09-21`, base 3e8c337.
Commits: wip 930e9ba `wip(T09-21): widen RunRequest.scenarios (T06-21 carry-over)` (every hook
passed, pytest-unit included; 35 min wall time); final dddf964 `feat(cli): T09-21 job submission, following and payloads` (every hook
passed, pytest-unit included; 35 min). Tree clean.

## What I implemented
- `herness/_cli/payloads.py` (U09-92): `STAGE_ORDER`, `pipeline_payload(stages, build_id,
  **extra)` (DD-10 keys `stages`, `build_id` + `enrich_stage`/`depth`/`score_steps`; an unknown
  stage raises UserInputError), `stages_from(stage)` (the `--from-stage` suffix),
  `review_request(kind, depth, budgets_usd, question=None)` (`{"request":
  RunRequest(...).model_dump(mode="json")}` with `Scenario(name=f"custom_{int(b)}",
  budget_usd=b)`; a pydantic ValidationError, such as an unknown depth or more than 5 budgets,
  becomes UserInputError("invalid review request")), `parse_budget_usd` (regex
  `[0-9]+(?:[_,][0-9]+)*(?:\.00)?`, 32-char cap, 1 ≤ v ≤ 10^12, returns an integral Decimal;
  never echoes the input).
- `herness/_cli/wait.py` (U09-90, U09-91, U09-103): `FollowOutcome` (frozen dataclass: job,
  exit_code, run_id, detached, partial, warnings), `submit_job`, `follow_job`,
  `run_job_inline`, `MAX_FOLLOW_S = 172_800`.
  - submit_job: json.dumps with `json_default`; > 65,536 bytes raises
    `UserInputError("job payload too large")` before enqueue. The JSON form (json.loads of the
    text) is what gets enqueued, so Decimal and Path values reach JobSpec as strings.
    `jobs.enqueue(kind, payload, gpu_class, priority, scheduled_for, idem_key=)` is followed by
    the `cli.job.enqueued` log (INFO job_id, kind). Unless `inline=True`, when
    `jobs.worker_alive()` is false it prints the R-45 Warning/Fix pair to stderr, sets
    `opts.worker_warned` and logs `cli.worker.absent` (WARNING job_id).
  - follow_job: one `jobs.get` per poll. run_id comes from result, then payload.request, then
    payload. Unless --quiet, a progress line goes to stderr when (status, attempts, review task
    counts from `ops.ui_task_status_counts`) change. The worker check repeats only while
    `worker_warned` is false. Terminal mapping per R-46/R-39: done 0, partial 6 (result
    partial True, or `ops.get_run(run_id).status == "partial"`) with its warning and
    partial=True, `skipped_open_circuit` 4 with its warning, failed by
    `exit_code_for_class_name(class, key=)`, canceled 1. The sleep is
    `clock.sleep(poll_s)`. After MAX_FOLLOW_S it detaches with exit 0 and the note "Still
    running; detached. Follow with `herness jobs list`." On Ctrl+C it detaches with exit 130,
    "Detached; job `<id>` keeps running.", and logs `cli.job.detached` (job_id, reason
    interrupt|max_follow). A StoreBusy poll logs `cli.job.poll_busy` (WARNING job_id,
    failures) and retries; the 10th in a row raises. Nothing ever cancels the job.
  - run_job_inline: `require_role(actor, "job_inline")` runs first, before any jobs call
    (TH09-27). Then the `cli.job.inline_started` log, `jobs.run_inline`, `jobs.get`. A
    `yield` outcome prints "Interrupted; job `<id>` was released and stays queued." and
    returns 130 detached; otherwise the U09-91 terminal mapping applies (a non-terminal row
    maps to 1), then the `cli.job.inline_completed` log (job_id, kind, status, exit_code).
    JobStateError and handler errors propagate.
  - All text goes to stderr through `output._clean` (marked `# T09-25:`); nothing writes to
    stdout; no print. The impl 08 functions are reached through the `from herness.core import
    jobs` module reference at call time (tests monkeypatch `wait.jobs`). `herness.store.ops`
    is imported lazily inside functions (BT09-07).
- R2: `RunRequest.scenarios: list[Scenario | _ScenarioName]` (max 5, strings 1–64) in
  herness/harness/swarm/lifecycle.py, with `Scenario` imported from herness.metrics.portfolio
  at module level and the `# T04-20:` marker removed. New UT06-55 case
  `test_ut06_55_scenario_entry_round_trips_and_hashes`: model_dump(mode="json") and JSON round
  trip, and the hash changes with a Scenario entry and with its budget.
- herness/cli.py untouched; no Typer command registered.

## Files changed
- A herness/_cli/wait.py (253 lines / budget 300)
- A herness/_cli/payloads.py (84 / 180)
- M herness/harness/swarm/lifecycle.py (264 / 330)
- A tests/unit/cli/test_cli_wait.py (UT09-67, UT09-68, UT09-69, UT09-82, UT09-100; 28 tests)
- A tests/unit/cli/test_cli_payloads.py (UT09-70; 25 tests)
- M tests/unit/harness/swarm/test_swarm_lifecycle.py (+1 UT06-55 test)

## RED evidence
`uv run pytest tests/unit/cli/test_cli_wait.py tests/unit/cli/test_cli_payloads.py -q -p no:logging`
```
E   ImportError: cannot import name 'wait' from 'herness._cli' (/home/user/herness/herness/_cli/__init__.py)
E   ImportError: cannot import name 'payloads' from 'herness._cli' (/home/user/herness/herness/_cli/__init__.py)
!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!!
```
After payloads.py, before the lifecycle widening:
`FAILED test_ut09_70_budgets_and_stage_suffix`, `FAILED test_ut09_70_review_request_round_trips_to_scenarios`
(`UserInputError: invalid review request`, because RunRequest.scenarios still accepted only str); 2 failed, 23 passed.

## GREEN evidence
`uv run pytest tests/unit/cli tests/unit/harness/swarm -q -p no:logging` gave 291 passed in 77.10s.
`uv run pytest tests/unit/cli/test_cli_wait.py -q -p no:logging` gave 28 passed in 4.53s.

## Gates (final working tree)
- `uv run ruff format --check .`: 1110 files already formatted
- `uv run ruff check .`: All checks passed!
- `uv run mypy`: Success: no issues found in 396 source files
- `uv run lint-imports`: Contracts: 15 kept, 0 broken
- `uv run python -m tools.check_module_size`: exit 0
- `uv run python -m tools.check_type_ownership`: exit 0
- `detect-secrets-hook --baseline .secrets.baseline` on the 4 new files: exit 0
- pre-commit on `wip(T09-21): widen RunRequest.scenarios`: every hook passed up to
  `pytest-unit`, which **Failed**:
  `ERROR tests/unit/enrich/test_cluster_stage_recovery.py - OSError: libcudart.so.13: cannot open shared object file`.
  `pytest -m unit --co` shows 6 such collection errors (test_cluster_stage_recovery,
  test_cluster_stage_run, test_embed, test_gpu, test_laya_decider, test_laya_trainer), out of
  10,340 unit tests collected.

## Coverage
`uv run pytest tests/unit/cli --cov=herness._cli --cov-branch --cov-report=term-missing -q -p no:logging`
(115 passed): payloads.py 100 % (45 stmts, 8 branches); wait.py 99 % (156 stmts, 0 missed;
36 branches, 1 partial `134->137`); the package total is 99 %.

## Deviations from the brief and why
1. `submit_job(opts, *, kind, payload, gpu_class, priority=jobs.MANUAL_PRIORITY,
   scheduled_for=None, idem_key=None, inline=False)`. U09-90 step 4 sets `opts.worker_warned`
   and skips the check "when the caller runs the job inline", so the function needs `opts`
   and an `inline` flag beyond the six spec keywords. That makes 8 arguments, under
   `# noqa: PLR0913` with its reason, the same pattern as `jobs.enqueue`.
2. Added the public helper `payloads.stages_from(stage)` for "`--from-stage S` yields the
   suffix", so that an unknown stage is a UserInputError and not a ValueError from
   `tuple.index`. It is not in the §2 export list.
3. Failed and canceled jobs: follow prints `Error: Job `<id>` failed (<class>): <message>` or
   `Error: Job `<id>` was canceled.` to stderr in human mode only. In JSON mode it prints
   nothing; the caller (T09-22..25) builds the error envelope (type = `last_error["class"]`,
   message) from `outcome.job.last_error`, because FollowOutcome has no error field.
4. The partial and circuit warnings are returned in `FollowOutcome.warnings`, not printed;
   the caller's `emit` prints them as `Warning:` lines. The worker warning, progress lines and
   detach notes are printed directly.
5. A Ctrl+C before the first job row is read re-raises KeyboardInterrupt (U09-84 maps it to
   130), because FollowOutcome.job needs a row; this is tested.
6. `test_cli_wait.py` resolves the `herness.enrich` lazy facade's names at import time. Under
   freezegun, `FakeClock.__enter__` reads every attribute of every loaded module.
   `herness.cli` loads `herness.enrich`, whose PEP 562 `__getattr__` then imports
   LanceDB/pandas while time is frozen, and the test stalled for more than 40 s. Warming the
   names first (about 3 s, no torch import) avoids that.
7. No `wip(T09-21): red tests` checkpoint: a red test file fails the mypy and pytest-unit
   hooks by construction. The RED output is recorded above instead.
8. Attribution: the harness attribution reminder of this session says `Co-Authored-By: Claude
   Opus 5.5 <noreply@anthropic.com>` and replaces earlier guidance; implementer-rules also say
   "use the Co-Authored-By line your harness attribution reminder gives you". The dispatch
   asked for `Claude Fable 5.1`. My (failed) commit message used the reminder's line together
   with the Claude-Session line. The coordinator then asked for `Claude Fable 5.1`, saying the
   harness reminder line wins. This session's harness reminder gives `Claude Opus 5.5`, so
   both commits (930e9ba, dddf964) end with `Co-Authored-By: Claude Opus 5.5
   <noreply@anthropic.com>` + the Claude-Session line. If the program needs Fable 5.1, the
   controller can reword at merge; I did not amend.

## Concerns / for the controller
- (Resolved by the coordinator: torch cu130 installed.) Before that, commits could not pass
  `pytest-unit` on this runner. These options are the
  controller's to choose, not mine: (a) make `import torch` work in .venv, either with the
  cu128 wheels of uv.lock (about 3 GB) or a torch build whose CUDA libs exist; (b) a ruling
  allowing e.g. `PYTEST_ADDOPTS="--ignore=tests/unit/enrich"` for commits on this runner,
  which is not literally `SKIP=` or `--no-verify` but does narrow a gate, so I did not use it
  unilaterally. Even with collection fixed, the hook runs all 10,340 unit tests on every
  commit, so expect a long wall time.
- (Done: committed as 930e9ba + dddf964.) Original note, to finish once unblocked: stage the 6 files and commit `wip(T09-21): widen
  RunRequest.scenarios ...` (message at /tmp/w32-c09/builder/msg1.txt; lifecycle files already
  staged), then payloads, wait, and the final `feat(cli): T09-21 job submission, following and
  payloads`. Or make one final commit.

## Carry-overs noticed
- tests/support/fake_clock.py: freezegun's module scan triggers PEP 562 lazy imports
  (`herness.enrich`), which stalls any test that imports herness.cli and uses `fake_clock`.
  Possible fix: `freezegun.configure(extend_ignore_list=["herness.enrich"])`, or give the
  facade a non-importing `__dir__`. Owner impl 11 (T11-03).
- `herness.cli` import pulls in `herness.enrich`, and `import herness.cli` takes about 1.5 s
  here (BT09-07 budget < 1 s for `--help`); worth a T09-25 look.
- T09-25: swap `output._clean` in wait.py for `term.safe_terminal_text` (marker in place).

## Fix round 1 (tests only)
Commit f819e18 `test(cli): T09-21 fix round 1 — boundary tests for payload size, run_id
precedence and budget grammar`. Every hook passed, pytest-unit included (35 min). No
production code changed.
- UT09-82 `test_ut09_82_payload_size_boundary`: a payload whose JSON is 65,537 bytes raises
  UserInputError("job payload too large") and enqueue is never called. A payload of exactly
  65,536 bytes is enqueued unchanged. Both sizes are asserted with the same serialisation
  submit_job uses: `json.dumps(..., default=json_default, ensure_ascii=False,
  separators=(",", ":"))`. The review's wording, `json.dumps(payload, default=json_default)`,
  adds spaces after the separators, so it would measure 1 byte more than the gate does.
- UT09-67 `test_ut09_67_run_id_precedence`: when result, payload.request and payload each
  carry a different run_id, the result's wins. Without one in the result, payload.request's
  wins over payload.run_id. When the request has no run_id, payload.run_id is used. The test
  uses canceled sync rows in JSON mode, so there is no store read and no output.
- UT09-70 parametrize rows: `1.50`, `2.5` and `2.01` are rejected with UserInputError; `2.00`,
  `2,000,000.00` and `1_500_000` are accepted.
- Mutation check, applied by hand and reverted with git checkout: changing the limit to
  70_000, reversing the run_id order, and changing the budget fraction to `\.[0-9]{2}` each
  make the new tests fail.
- GREEN: `uv run pytest tests/unit/cli -q -p no:logging` gives 123 passed in 78.41s. ruff
  format and ruff check are clean on both files; mypy reports Success (396 files).
- Attribution: as on 930e9ba and dddf964, the commit ends with this session's harness
  reminder line (`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`) and the
  Claude-Session line, not Fable 5.1.

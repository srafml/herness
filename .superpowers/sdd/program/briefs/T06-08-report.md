# T06-08 Blackboard — build report

Worktree: D:\herness\.claude\worktrees\agent-a63ee4e8be643241c (branch worktree-agent-a63ee4e8be643241c, base 8bb8194).
Commits: 14a7f72 wip(T06-08): blackboard module and unit tests; 5762876 wip(T06-08): IT06-16 and ST06-10 contention tests; d67b7ec feat(harness): T06-08 blackboard single-writer API (empty marker commit; code is in the two wip commits).

## Built
- `herness/harness/blackboard.py` (379 / 380 lines, L4): `FindingFilter` (U06-51, pydantic, extra=forbid, strict=False,
  bounds per spec, `effective_statuses()`), `Blackboard` (U06-52..U06-59):
  - writer: own `ThreadPoolExecutor(1, "herness-bb-writer")` unless one is passed (not shut down then); `run_on_writer`
    (run_in_executor), `run_on_writer_sync` (submit + `wait(timeout)`; timeout -> `StoreBusy("blackboard writer timeout")`,
    errors of `fn` incl. StoreBusy/TimeoutError propagate unchanged); latency -> `herness_harness_blackboard_write_seconds`
    through a private `_MetricSink` Protocol stand-in (`record_histogram(name, value, *, component)`), `# T08-05:` marker at the call site.
  - `post(ctx, **args)` (U06-53): role/run check; unknown argument names rejected; Finding built with new `fnd_` id, ctx
    run/task/role, `query_ids = sorted(query_ids ∪ numbers[].query_id)`; ValidationError -> `invalid post_finding arguments: <paths>`
    (paths only); `validate_markers` (+ spec hint); `find_uncited` -> `numerals outside markers: <tokens>`; query ids: ops
    `get_evidence` with `build_id == self.build_id`, the rest via `SELECT query_id FROM meta.evidence WHERE query_id IN (SELECT unnest(?))`
    on `ctx.warehouse.cursor()`; `catalog.missing`; `get_redactor().scan` -> `claim contains personal data (EMAIL, PERSON)` (types only).
    Every rejection logs INFO `harness.finding.post_rejected` (run_id, task_id, rule). Step 7 on the writer (30 s,
    `StoreBusy("blackboard writer timeout: task_id=<id>")`): task read, canonical-JSON duplicate check (claim, entity_type,
    entity_id, numbers) returns the existing id, revision tasks post once, `SwarmTaskState` from checkpoint (or phase = run status)
    with id appended, `save_checkpoint(task_id, "state", ..., writes=cb)` where cb = `insert_finding` or `tx_supersede`.
    Then `fault_point("swarm.after_finding_write", role="analyst")` and DEBUG `harness.finding.posted` (only when a row was written).
  - `list_findings` (asyncio.to_thread(query_findings) with effective statuses; run mismatch -> ToolInputError).
  - `tx_challenge`/`challenge`, `tx_mark_verified`/`mark_verified`, `tx_reject`/`reject` (reason set on v when given),
    `tx_supersede`/`supersede`, `tx_merge`/`merge` — all through `transition_finding` CAS, async forms via `run_write` on the
    writer (ops `bb_challenge`, `bb_verify`, `bb_reject`, `bb_supersede`, `bb_merge`); DEBUG `harness.finding.transitioned`
    on success, DEBUG `harness.finding.merge_skipped` for skipped dups.
- Tests: `tests/unit/harness/test_blackboard.py` (UT06-35..42, ST06-01, ST06-05), `tests/unit/harness/_blackboard_env.py`
  (shared fixture: migrated ops store + SqliteJobsBackend bound, running run/claimed analyst task, ops evidence row, fake DuckDB
  warehouse with core.team + meta.evidence, test redactor with a planted name), `tests/integration/harness/test_blackboard_contention.py`
  (IT06-16 shared writer + 5 Blackboards with own writers on threads; ST06-10 parallel reject/verify across writers).

## Evidence
- RED: `pytest tests/unit/harness/test_blackboard.py` -> ImportError: cannot import name 'blackboard' from 'herness.harness'.
- GREEN: card tests 37 passed (34 unit + 3 integration; integration repeated 3x, stable, ~0.5 s); `pytest tests/unit/harness -q` 1172 passed, 1 skipped (symlink privilege).
- Coverage blackboard.py: 100 % line, 100 % branch (237 stmts, 56 branches).
- Gates: ruff format --check / ruff check (repo) clean; mypy (project) clean, test modules also clean with `mypy -m`; lint-imports 13 kept;
  check_module_size 0; check_type_ownership 0; pre-commit hooks passed on every commit. Full suite not run (per dispatch).

## Threat assertions
- ST06-01: stray digit, marker to nothing, fabricated query_id (in query_ids and in a NumberRef) each -> exact ToolInputError message, zero finding rows, no checkpoint.
  Ops evidence of another build is rejected; a meta.evidence id is accepted.
- ST06-05: email + planted directory name -> `claim contains personal data (EMAIL, PERSON)`; message, hint, context and captured logs contain no name/email/claim text.
  Over-long claim -> `invalid post_finding arguments: claim` without the claim.
- UT06-36: failing writes callback (after insert) -> FatalError, no row, checkpoint still NULL; loop key preserved (R-21); fault point observed after commit with role=analyst.
- IT06-16 / ST06-10: exactly one True result, no exception (no "database is locked"), revision task inserted iff revise won.
- SQL: only fixed statements; agent values are bound parameters.

## Spec readings / notes
- Warehouse for step 4: the Blackboard signature has no warehouse, so step 4 uses `ctx.warehouse` (ToolContext, same build as the task).
- `tracer` is accepted and stored but not emitted to: spec 05 `TraceType` has no blackboard event type and §8.3 lists none for U06-53..58; the reject reason goes to the log event.
- `tx_supersede` reads the old challenge history with `conn` inside the transaction (no private findings helper needed); `tx_challenge` checks its ConfigError preconditions (also covers the async form).
- `SwarmTaskState(phase=...)` falls back to "running" if the run row is missing (defensive; not reachable in tests beyond coverage of the ternary).

## Concerns
- Spec-verbatim messages echo agent-supplied fragments: `numerals outside markers: <tokens>` (numeral tokens from the claim, e.g. a phone-number-like numeral would appear before the PII check runs, since step 3 precedes step 6) and `unknown <entity_type> <id>` (agent entity_id, <= 200 chars). Implemented as specified; the controller may want the numeral tokens dropped or the PII scan moved before step 3.
- Warehouse errors in the meta.evidence lookup (e.g. a build without `meta.evidence`) propagate as raw duckdb errors; spec gives no mapping.
- Line budget: 379 / 380 — no headroom for later additions.

## Fix round 1 (review T06-08-review.md: I-1, I-2)
- I-1: the UT06-36 rollback test now fails the checkpoint write itself, after `writes` (the finding insert) has run. A test trigger
  `BEFORE UPDATE OF checkpoint ON task ... RAISE(ABORT)` is installed after a `loop` checkpoint was saved. The test asserts a HernessError
  (SchemaViolation), 0 finding rows, and the checkpoint unchanged. Control test `test_ut06_36_rollback_probe_detects_split_transactions`
  monkeypatches `save_checkpoint` to commit the insert in its own `run_write` first. Under the same trigger that leaves 1 row, so the atomic
  test would go red for a two-transaction implementation.
- I-2 (controller ruling): `_validate` order is markers (2), PII scan (6), numerals (3), evidence (4), entity (5).
  New test `test_st06_05_pii_checked_before_numerals_and_ids`: the claim has an unmarked phone number and an email with digits, plus unmarked
  "17" and an unknown entity id. It is rejected with `claim contains personal data (EMAIL, PHONE)`. Message, hint, context and details contain
  none of 415/867/5309/ops42/17/t9.
- Spec note: U06-53 PII scan moved before steps 3–5 (ENG §3.4). The spec text still lists it as step 6, so the spec needs this note mirrored.
- Resolves the first concern above for numerals of PII-bearing claims. Non-PII numeral tokens and entity ids still appear in the spec-verbatim messages.
- Evidence: card tests 39 passed (36 unit + 3 integration). tests/unit/harness 1174 passed, 1 skipped. blackboard.py coverage is 100 % line and branch, 379/380 lines.
  ruff, mypy, lint-imports, check_module_size and check_type_ownership are clean.

## Fix round 2 (review T06-08-review.md "Re-review r1": I-3, M-5)
- I-3: `_validate` now runs the PII scan first, before step 2 (`validate_markers`). The order is PII (6), markers (2), numerals (3),
  evidence (4), entity (5). A malformed bracketed token that holds personal data (`[[+1 415-867-5309]]`, `[[ops42@example.com]]`) is
  rejected as PII before findings.py can echo it as `malformed marker [[...]]`. findings.py is unchanged.
  New test `test_st06_05_pii_checked_before_marker_validation`: the message is `claim contains personal data (EMAIL, PHONE)`. The message,
  hint, context, details and captured logs contain none of 415/867/5309/ops42/example.com/malformed.
- M-5: `test_st06_05_pii_checked_before_numerals_and_ids` now captures structlog logs and includes them in its leak check.
- Spec note: U06-53: PII scan runs first, before step 2 markers (ENG §3.4).
- Evidence: card tests 40 passed (37 unit + 3 integration). tests/unit/harness (-p no:logging): 1175 passed, 1 skipped.
  blackboard.py 379/380 lines. ruff format, ruff check, mypy (blackboard.py, test_blackboard.py) and check_module_size are clean.

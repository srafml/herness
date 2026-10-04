# T06-08 Blackboard — review (verify agent)

Worktree agent-a63ee4e8be643241c, head d67b7ec (base 8bb8194). Files: herness/harness/blackboard.py (379/380),
tests/unit/harness/test_blackboard.py, tests/unit/harness/_blackboard_env.py, tests/integration/harness/test_blackboard_contention.py.

## Evidence (re-run by reviewer)
- Card tests: 37 passed (6.6 s); integration file re-run 3x: 3 passed each (~0.47 s), no flakes, no "database is locked".
- Coverage blackboard.py: 100 % line, 100 % branch (237 stmts, 56 branches).
- ruff check: clean; ruff format --check: clean (641 files); mypy: 0 issues (253 files); lint-imports: 13 kept, 0 broken;
  check_module_size: exit 0; check_type_ownership: exit 0.
- Mutation probes (scratch pytest plugin in the reviewer scratchpad, monkeypatching herness.harness.blackboard at test time;
  worktree untouched):

| Mutation | Result |
|---|---|
| find_uncited -> () | RED: UT06-37[42], ST06-01 |
| _unknown_queries -> [] | RED: ST06-01 (both tests) |
| ops-evidence build_id check removed | RED: test_st06_01_evidence_of_another_build_is_unknown |
| redactor.scan -> [] | RED: ST06-05 |
| validate_markers -> [] | RED: 4x UT06-37, ST06-01 |
| fault_point also fired before the write | RED: test_ut06_36_fault_point_after_commit |
| transition_finding CAS widened to all statuses | RED: IT06-16 (both), ST06-10, UT06-38/39/40/41 |
| **finding insert in its own run_write, then save_checkpoint without writes (non-atomic)** | **GREEN: 37 passed** |

## Spec compliance

| Item | Status | Note |
|---|---|---|
| U06-51 FindingFilter | ✅ | fields, extra=forbid, strict=False, bounds (entity_ids<=50, task_ids<=200, limit 1-500, confidence 0-1), effective statuses |
| U06-52 Blackboard | ✅ | own ThreadPoolExecutor(1, "herness-bb-writer") shut down in close(); a passed writer is left running; no store handle |
| U06-53 post | ✅ (see I-2, plan-mandated) | steps 1-9 in order; query_ids union; paths-only schema errors; ops evidence with build check ∪ meta.evidence (bound param, unnest); catalog; PII types only; writer 30 s with task_id StoreBusy; dup via canonical_json; revision once; SwarmTaskState extended or phase=run status; save_checkpoint(task, "state", ..., writes=cb) with insert_finding / tx_supersede; fault_point after commit; posted DEBUG |
| U06-54 list_findings | ✅ | to_thread(query_findings); run mismatch -> ToolInputError |
| U06-55 challenge/tx_challenge | ✅ | ConfigError preconditions; revise inserts the task only when the CAS won |
| U06-56 mark_verified/reject | ✅ | reason set on a v copy; v None leaves the column; transitioned DEBUG (fields per §8.2) |
| U06-57 supersede/tx_supersede | ✅ | not found / not open -> ToolInputError; history copied |
| U06-58 merge/tx_merge | ✅ | sorted; skipped dups DEBUG; keep in dups -> ConfigError |
| U06-59 run_on_writer(_sync) | ✅ | timeout -> StoreBusy; fn errors unchanged; latency histogram (T08-05 stand-in + marker) |
| UT06-35 | ✅ | |
| UT06-36 | ⚠️ | passes, but the rollback test does not prove same-transaction (I-1) |
| UT06-37 | ✅ | every case incl. non-analyst role and run mismatch |
| UT06-38 | ✅ | |
| UT06-39 | ✅ | |
| UT06-40 | ✅ | |
| UT06-41 | ✅ | |
| UT06-42 | ✅ | |
| IT06-16 | ✅ | 50 calls via one shared writer and via 5 writers on threads; exactly one True; no exceptions; revision task iff revise won |
| ST06-01 | ✅ | digits, marker-to-nothing, fabricated id (query_ids and NumberRef) -> exact messages, 0 rows, no checkpoint; mutation-verified |
| ST06-05 | ✅ | EMAIL, PERSON only; message/hint/context/logs free of name, email, claim text; mutation-verified |
| ST06-10 | ✅ | reject vs verify across writers, one winner, reason consistent with the winner |

Other checks:
- Evidence shapes: execute_recorded (herness/harness/tools.py:97-131) records ops Evidence(query_id, build_id=ctx.build_id) before
  returning RecordedResult.query_id (cache hits re-record too); the pipelines (_review_common.read_rows) cite that query_id. These pass
  step 4 as long as the Blackboard's build_id equals ctx.build_id (the swarm builds both from the run). ✅
- SQL: only fixed statements (_META_SQL, _CHALLENGE_SQL); agent values are bound params; no identifiers from agent text. ✅
- Bounds: Finding type (claim <=1500, numbers 1-20, query_ids 1-50 with pattern q_[0-9a-f]{16}, entity_id <=200) + FindingFilter bounds. ✅
- Single writer: every post/transition write goes through _sync / run_on_writer; thread-name test present. ✅
- Uncited claims cannot reach the board through post; the async supersede(old, new) takes a pre-built Finding without re-validation
  (spec-mandated signature; callers are swarm-internal).

## ⚠️ Spec questions
- W-1 tracer is accepted but never emitted to; U06-56 says the reject reason (v None) is carried "by the log event and the trace",
  but spec 05 TraceType has no blackboard event and §8.3 lists none. The log event carries it. Controller: accept or record a carry-over.
- W-2 U06-56 text says transitioned carries "from set"; the §8.2 table lists finding_id, to, reason. Builder followed §8.2 (fine);
  spec-internal inconsistency to note.
- W-3 (builder C2) step 4 has no error mapping for warehouse failures; every build has meta.evidence (impl 02 §4.7, U02-88..92),
  so a missing table means a broken build and a raw duckdb error is acceptable.

## Findings

### Critical
none

### Important
- I-1 UT06-36 rollback test does not prove atomicity: tests/unit/harness/test_blackboard.py:128-142. The failing callback raises
  inside insert_finding, so a non-atomic implementation (insert in its own run_write, then save_checkpoint without writes) also leaves
  "neither": the probe above stays 37/37 GREEN. The same-transaction invariant (U06-53 postcondition, R-21) is therefore untested.
  Fix: add a case where the checkpoint UPDATE fails after the finding insert ran, e.g. create a trigger in the test store
  (CREATE TRIGGER t BEFORE UPDATE OF checkpoint ON task BEGIN SELECT RAISE(ABORT, 'x'); END) or wrap the jobs backend so the UPDATE
  fails after writes, then assert 0 finding rows. The implementation itself is correct (herness/harness/blackboard.py:247-253).
- I-2 (plan-mandated) numeral tokens echoed in the error before the PII scan: herness/harness/blackboard.py:216-218 (step 3 precedes
  step 6 at :223). A claim like "call 555-123-4567" or an account/employee number yields "numerals outside markers: 555, 123, 4567" in
  a ToolInputError that spec 05 returns as a tool result (traced/persisted), contradicting the global constraint "No ... personal data
  in any error message". Spec-verbatim, so it needs a controller ruling: run the PII scan before the numeral check (line-neutral) and/or
  drop the tokens (report count/offsets only). Same class, lower risk: "unknown <entity_type> <id>" (:222) echoes an agent entity_id
  (<=200 chars free text); consider naming only the entity type.

### Minor
- M-1 herness/harness/blackboard.py:245: the 201st pending finding of one task makes SwarmTaskState.model_validate raise a raw pydantic
  ValidationError on the writer (pending_findings max_length=200), not a ToolInputError. Unreachable with realistic max_steps; could map
  to ToolInputError("task finding limit reached").
- M-2 herness/harness/blackboard.py:207-208: warehouse lookup errors propagate raw (builder C2); acceptable per W-3.
- M-3 herness/harness/blackboard.py:174-176: on writer timeout a started future cannot be cancelled; the commit may land after StoreBusy
  was raised. A retried post returns the same id via the duplicate check, so benign; worth a comment.
- M-4 Module at 379/380 (builder C3): zero headroom; a fix for I-2 must be line-neutral or needs a ruling / private sibling module.

## Builder concerns: judgement
- C1: real conflict with the global rule -> I-2 (plan-mandated, controller ruling). Recommended: move step 6 before step 3 and drop
  the tokens from the numerals message.
- C2: Minor (M-2 / W-3).
- C3: noted (M-4).

## Verdict
**Needs fixes**: I-1 (strengthen the atomicity test; test-only, builder-fixable) and I-2 (plan-mandated; needs a controller ruling on
message content / check order). Implementation otherwise conforms to U06-51..59; all gates green; every security guard except
transaction atomicity is mutation-verified by its test.

## Re-review r1

Scope: I-1, I-2 (controller ruling) on d67b7ec..990e0a7. Probes ran from a pytest plugin in the scratchpad (`-p t0608probe`,
monkeypatch only); the worktree was not modified.

| Finding | Status | Evidence |
|---|---|---|
| I-1 atomicity test | ✅ | see probes 1-2 |
| I-2 PII before numerals/entity | ✅ (ruled scope) | see probe 3; residual gap -> I-3 below |

Probes:
1. I-1 split-transaction mutant: `bbmod.save_checkpoint` replaced by `run_write(writes)` + a separate `save_checkpoint` (no writes).
   `test_ut06_36_failing_checkpoint_write_leaves_neither` -> **RED** (`assert 1 == 0` at test_blackboard.py:145). The revised
   test detects a non-atomic implementation.
2. I-1 real code with `insert_finding` instrumented: the insert executed once, then the trigger `t06_08_block` aborted the
   checkpoint UPDATE; the test passes: 0 finding rows, checkpoint equal to the pre-post value (`{"loop": {"step": 1}}` envelope).
   Same transaction confirmed (herness/harness/blackboard.py:245-252, `save_checkpoint(..., writes=writes)`).
   The in-repo control test `test_ut06_36_rollback_probe_detects_split_transactions` encodes probe 1 permanently (asserts 1 row).
3. I-2 order mutant: `_validate` restored to the old order (numerals, evidence, entity, then PII). Test file -> 1 failed / 35 passed:
   `test_st06_05_pii_checked_before_numerals_and_ids` RED with message `numerals outside markers: +1, 415, 867, 5309, 17`.
   Real code: message `claim contains personal data (EMAIL, PHONE)`; no digits, `ops42`, or entity id `t9` in message, hint,
   context or details. Spec-verbatim messages for the other rules unchanged (ST06-01 exact messages still pass).

Gates (real code, head 990e0a7):
- blackboard.py 379 lines (<= 380) ✅
- tests/unit/harness/test_blackboard.py + tests/integration/harness/test_blackboard_contention.py: 39 passed ✅
- coverage herness/harness/blackboard.py 100% statements and branches ✅
- ruff check, ruff format --check, mypy on blackboard.py and test_blackboard.py: clean ✅
- Round diff: no regressions; removed imports (sqlite3, FatalError, Finding, insert_finding) unused elsewhere in the test file.

### New findings

#### Important
- I-3 PII still reaches the message through the marker step: herness/harness/blackboard.py:214-215 (step 2 `validate_markers`
  still precedes the PII scan at :216). herness/harness/findings.py:81 echoes the inner text of every malformed double-bracket token
  (herness/core/numbers.py:21 `ANY_MARKER_RE` accepts up to 40 arbitrary chars). Probe with real code:
  claim `"Team t1 had [[n1]] incidents, call [[+1 415-867-5309]]"` -> `malformed marker [[+1 415-867-5309]]`;
  `"... mail [[ops42@example.com]]"` -> `malformed marker [[ops42@example.com]]`. This breaks the I-2 ruling's invariant ("a claim
  with personal data is rejected naming only entity types and none of its digits/ids reach the message") and the global "no
  personal data in any error message" rule. Fix (line-neutral): move the PII scan to the top of `_validate`, before
  `validate_markers`; add a test with a bracketed phone/email. Probe: with the PII scan moved first, all 39 card tests stay green
  (ST06-01 exact messages unaffected).

#### Minor
- M-5 tests/unit/harness/test_blackboard.py:346-356: the new ST06-05 case does not include captured logs in `text` (the older ST06-05
  case does). `_reject` logs only `rule`, so no leak today; cheap to add for symmetry.

## Verdict (r1)
**Needs fixes**: I-1 resolved ✅; I-2 resolved for the ruled order ✅; new I-3 (PII scan must also precede the marker check;
one-line move + one test). M-1..M-4 from round 0 unchanged.

## Re-review r2

Scope: I-3, M-5 on 605a313..3a305b5 (head 3a305b5). Probe ran from a scratchpad pytest plugin (`-p t0608r2probe`, monkeypatch of
`Blackboard._validate` only); the worktree was not modified (git status clean).

| Finding | Status | Evidence |
|---|---|---|
| I-3 PII scan before marker validation | ✅ | herness/harness/blackboard.py:214-216 — `get_redactor().scan` is now the first check in `_validate`, ahead of `validate_markers`, numerals, evidence and entity; probe below |
| M-5 logs in the ST06-05 leak check | ✅ | tests/unit/harness/test_blackboard.py `test_st06_05_pii_checked_before_numerals_and_ids` now wraps in `structlog.testing.capture_logs()` and includes `repr(logs)` in the leak check |

Probe (mutation, run by the verifier because the builder could not run it):
1. Old-order mutant (`validate_markers` before the PII scan, rest unchanged) -> test file **1 failed / 36 passed**:
   `test_st06_05_pii_checked_before_marker_validation` RED with
   `malformed marker [[+1 415-867-5309]]; malformed marker [[ops42@example.com]]` != `claim contains personal data (EMAIL, PHONE)`.
   The new test detects the regression.
2. Real code: the same test is green; message is `claim contains personal data (EMAIL, PHONE)`; no `415`/`867`/`5309`/`ops42`/
   `example.com`/`malformed` in message, hint, context, details or captured logs; no finding row written.

Gates (real code, head 3a305b5):
- blackboard.py 379 lines (<= 380) ✅
- tests/unit/harness/test_blackboard.py + tests/integration/harness/test_blackboard_contention.py: 40 passed ✅
- coverage herness/harness/blackboard.py 100% statements and branches ✅
- ruff check, ruff format --check, mypy on blackboard.py and test_blackboard.py: clean ✅
- Round diff: only the reorder in `_validate` (docstring updated to "Steps 6, 2, 3, 4, 5"), the M-5 test edit and the new test; no
  regressions. ST06-01 exact messages for the other rules still pass.
- I-1 (atomic rollback tests incl. the split-transaction control) and I-2 (PII before numerals/entity) still green ✅

### New findings
None.

## Verdict (r2)
**Approved**: I-3 ✅, M-5 ✅; I-1/I-2 still hold. M-1..M-4 from round 0 unchanged (Minor, non-blocking).

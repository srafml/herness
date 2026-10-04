# T08-20 report: Pipe and job contexts (U08-84, U08-85, U08-86)

Worktree: D:\herness\.claude\worktrees\agent-ab0965800086f7879, branch worktree-agent-ab0965800086f7879, base 2451ea4.
Status: DONE_WITH_CONCERNS (one ruling needed: private sibling module for the line budget).

## What was built
- herness/core/jobs/pipe.py (138 lines, budget 150): U08-84. Six frozen pydantic models (extra="forbid", strict=True)
  in a discriminated union on `type` (`PipeMessage`); caps from the table (gpu_reply.message 500, heartbeat.note 200,
  outcome.error_message 2048); request_id must be a canonical ULID. `MAX_PIPE_MSG_BYTES = 8388608`.
  `encode_message` = canonical JSON of model_dump, over cap -> SchemaViolation. `decode_message` checks type/size BEFORE
  json.loads (NaN/Infinity rejected), then TypeAdapter.validate_python(strict=True); every failure ->
  SchemaViolation("bad pipe message"), raised outside the except block so no __cause__/__context__ carries content.
- herness/core/jobs/context.py (312 lines, budget 360): U08-85 ChildJobContext/ChildServiceControl and U08-86
  InlineJobContext/InlineServiceControl. A TYPE_CHECKING `_conforms` function makes mypy prove both classes (and their
  `services`) satisfy the JobContext/ServiceControl protocols structurally.
- herness/core/jobs/_context_base.py (163 lines, NEW private sibling, no section 2 row): ContextBase (row shortcuts,
  first-wins stop reason + threading.Event, load_state deep copy, require_gpu_class, gpu_scope), the gpu_scope restore
  rule incl. ERROR `jobs.gpu.restore_failed` {job_id, class, error_type}, note cleaning, the 4 MiB checkpoint cap, the
  U08-85 default GPU wait. Split out because context.py was 423 lines as one module (over the 360 budget and the
  400 hard limit).

### Child context behaviour (U08-85)
- Reader: daemon thread `herness-pipe-reader`, sole reader; loops `conn.poll(0.2)` then `recv_bytes(MAX_PIPE_MSG_BYTES)`
  (an oversized frame raises OSError inside recv, never buffered). `stop` -> first reason wins + Event; `gpu_reply` ->
  the Queue(maxsize=1) registered for its request_id; a reply for an unknown id (caller already timed out) is dropped
  with DEBUG `jobs.pipe.late_reply`.
- Pipe loss / hostile message decision: EOF, OSError, a message that fails decode (pickle, extra field, ...) or a
  wrong-direction message (a child-to-parent type sent by the parent) is FATAL for the pipe. Nothing is unpickled or
  executed; the reader logs ERROR `jobs.pipe.lost` {job_id, reason: closed|bad_message} and exits; every waiting GPU
  call is woken with a sentinel and raises ModelUnavailable("supervisor pipe closed"); new GPU calls fail at once;
  should_yield() becomes true with reason "shutdown" unless a stop already arrived (first wins). `close()` stops the
  reader quietly and also makes later GPU calls fail at once.
- Writes: one write lock; send_bytes OSError -> ModelUnavailable("supervisor pipe closed").
- heartbeat: at most one message per HEARTBEAT_MIN_INTERVAL_S (1 s, clock.monotonic); throttled calls overwrite a
  pending note (a None note does not erase it); the next allowed send carries it.
- require_gpu_class: CPU slot -> ConfigError("GPU control requires the GPU slot") before any config read or send;
  wait = timeout_s or the default stop+60+max start of cls+warm-up+60; no reply -> ModelUnavailable("gpu request timed
  out"); ok False -> the named class (ConfigError / ModelUnavailable) with the reply message ("gpu request failed"
  when none).
- services.start/stop/healthy: gpu_request ops service_*, same slot check and reply handling; healthy returns
  `reply.healthy is True`.
- save_state: canonical JSON <= 4 MiB else SchemaViolation, sends save_state; load_state: deep copy of
  row.result["state"], {} when absent or not a dict.

### Inline context behaviour (U08-86)
- Constructor: `InlineJobContext(row, *, owner, controller: GpuController | None, backend: JobsBackend | None = None)`
  (backend None -> require_jobs_backend() at save time). Extra API for run_inline (T08-22): `request_stop(reason)`
  (first wins, sets the Event) and `note` (last heartbeat note, redacted/cut, in memory).
- require_gpu_class -> controller.swap(cls, reason="in_job") (previous = controller.loaded for gpu_scope; timeout_s
  is not used inline, the controller applies its configured timeouts); services.* -> controller.service_*;
  controller None -> ConfigError on every GPU call; save_state -> backend.save_job_state(job_id, owner, bytes),
  False -> JobStateError("job state not saved", job_id=...).

## Tests (all pass; 3 consecutive runs stable; about 5 s)
- tests/unit/core/jobs/test_jobs_pipe.py: UT08-97 (round trip of every type, over a real Pipe, 9 MB encode/decode,
  size checked before json.loads, 16 invalid inputs incl. unknown type/extra field/NaN/deep nesting, strict
  no-coercion), ST08-13 (pickled bytes with a flag-setting __reduce__ -> SchemaViolation, flag unset; extra field, no
  content echoed), CV-T08-20 acceptance grep: no `.send(`/`.recv(`/pickle/eval/exec in herness/core/jobs/*.py.
- tests/unit/core/jobs/test_jobs_context.py: UT08-98 (stop first-wins via pipe ordering, reader thread name/daemon,
  5 heartbeats in 1 s -> one message + pending note carried, redaction + 200 cut, 5 MB state -> SchemaViolation,
  every GPU call on the CPU slot -> ConfigError with nothing sent), UT08-99 (fake parent replying
  previous_class=decider: restore request for decider on normal and raising exit; restore failure logged at ERROR
  and the original propagates; failure on normal exit raises; no restore when previous == cls or None), ST08-13 child
  side (pickle / extra field / wrong direction from the parent: nothing unpickled, waiting call fails fast, reason
  shutdown, ERROR log with ids only), CV tests (named error classes, service ops, timeout + late reply dropped,
  default waits, EOF from the parent, oversized frame, quiet close, row shortcuts, load_state copies).
- tests/unit/core/jobs/test_jobs_context_inline.py: test_cv_t08_20_inline_* (IT08-12 needs run_inline, T08-22).
- Results: card tests 77 passed; tests/unit/core/jobs 397 passed.
- Coverage (card tests, --cov-branch): pipe.py 100 %, context.py 100 %, _context_base.py 100 % line and branch.
- Gates: ruff format/check clean, mypy (256 files) clean, lint-imports 13 kept 0 broken, check_module_size exit 0,
  check_type_ownership exit 0. Pre-commit hooks passed on every checkpoint commit.
- TDD note: for each module the tests were written directly after the implementation in the same step, so there is
  no recorded RED run; the tests were then run green.

## Deviations / spec notes
1. RULING NEEDED: new private sibling herness/core/jobs/_context_base.py (163 lines) keeps context.py (312) within its
   360 budget; the section 2 module map needs a row (docs edit outside the card files, not made). Merging back is not
   possible under the 400-line hard limit.
2. Note cleaning redacts the whole note first and then cuts to 200 chars (spec wording: "cut to 200 chars and passed
   through redact_text"). Cutting first can split a secret so the redactor no longer recognises it (same order as the
   classify precedent "redacts before the 500-char cut"); the final cut keeps the 200 cap after placeholder
   expansion. A redaction failure (redact_text -> None) sends/stores no note (fail closed).
3. pipe.py bounds beyond the table: outcome.error_class <= 100 chars; gpu_request.timeout_s > 0 and finite;
   gpu_reply.request_id is also validated as a ULID.
4. Log events not in the section 8.1 table: `jobs.pipe.lost` (ERROR; job_id, reason) and `jobs.pipe.late_reply`
   (DEBUG; job_id).
5. Pipe loss sets stop_reason "shutdown" when no stop arrived yet (so the handler checkpoints and yields).
6. Default wait for services.* (the spec only says "same reply handling"): stop+60+(timeout_s or the service
   start_timeout_s)+warm-up+60. For require_class with an explicit timeout_s the wait is timeout_s and it is also
   sent in the request.
7. gpu_scope: a reply with previous_class None means no restore.
8. The throttle clock is clock.monotonic (not patched by fake_clock); tests patch clock.monotonic directly.

## Carry-overs
- ST08-13 second half ("supervisor treats the child as crashed" on a bad child message) -> supervisor card T08-21
  (U08-87 step b must catch SchemaViolation from decode_message and finish the job as child_crash).
- IT08-12 -> T08-22 (run_inline); it must build InlineJobContext(row, owner=..., controller=..., backend=...) and use
  request_stop("cancel"/"shutdown") from the heartbeat thread / SIGINT handler.
- herness.core.jobs.__init__ exports for the four context classes and the pipe names -> U08-98 export-map card (not
  edited).
- child_main (U08-88) should call ChildJobContext.close() before closing conn.

## Concerns
- Child writes (send_bytes) are not time-bounded: if the supervisor stops draining the pipe while a message larger
  than the OS pipe buffer is in flight, the handler thread blocks in send; the supervisor stall/terminate path
  (U08-87 step e) is the backstop. Reads and reply waits are all bounded.

## Commits
- d16d92a wip(T08-20): pipe messages and tests
- 8804e25 wip(T08-20): child job context over the pipe
- c50f7d1 wip(T08-20): inline job context tests
- 78848a4 feat(jobs): T08-20 pipe messages and job contexts (final)

## Fix round 1 (review T08-20-review.md: Approved with minors) -> 1558bea fix(jobs): T08-20 review round 1
1. Ruling w16-s08b applied: impl 08 section 2 row for `herness/core/jobs/_context_base.py` (budget 180; file is 163
   lines) added right after the context.py row; section 8.1 rows added for `jobs.pipe.lost` (ERROR; job_id, reason,
   error_type) and `jobs.pipe.late_reply` (DEBUG; job_id). Nothing else in the doc was touched. Deviations 1 and 4
   above are therefore resolved.
2. M1: `_check_timeout` rejects a handler-supplied timeout_s that is not None or a finite int/float > 0 (bool, str,
   0, negative, NaN, inf) with ConfigError("timeout_s must be None or a finite number > 0", value_type=<type name>)
   before anything is sent; services.* now use `timeout_s if timeout_s is not None else <service start>`.
   Tests: test_cv_t08_20_bad_timeout_config_error (7 cases x require/start), test_cv_t08_20_explicit_small_timeout_is_not_unset.
3. M4: `_read_loop` has a final `except Exception` (fail closed): `_pipe_lost("reader_failed", error_type)` logs one
   ERROR `jobs.pipe.lost` with reason + error_type only and wakes every waiter. Test:
   test_cv_t08_20_reader_failure_fails_closed (decode_message patched to raise RuntimeError -> waiting
   require_gpu_class raises ModelUnavailable("supervisor pipe closed") promptly, reason shutdown, no message text logged).
4. M5: the acceptance grep walks herness/core/jobs with rglob, blanks string literals and comments via tokenize (no
   docstring false positives), and flags `.send(`/`.recv(`, import/from/attribute use of pickle, cPickle, marshal and
   shelve, `eval(`, `exec(`, `multiprocessing.Queue` and `Manager(`; test_cv_t08_20_code_only_blanks_strings_and_comments
   checks the helper.
- Parked per controller: M2, M3 (carry-over to U08-88: child_main redacts/cuts error_message before OutcomeMsg), M6.
- The detect-secrets hook refreshed .secrets.baseline line numbers (doc rows shifted); the diff has only line_number and
  generated_at changes, no dropped entries, LF endings.
- Results: card tests 87 passed (3 consecutive runs, about 7 s), coverage 100 % line/branch on pipe.py, context.py
  (329 lines / 360) and _context_base.py; tests/unit/core/jobs 407 passed; ruff format/check, mypy (256 files),
  lint-imports (13 kept), check_module_size and check_type_ownership clean; hooks passed.

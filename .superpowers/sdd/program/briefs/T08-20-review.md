# T08-20 review: Pipe and job contexts (U08-84, U08-85, U08-86)

Reviewer: verify agent. Worktree agent-ab0965800086f7879, base 2451ea4, head 78848a4. Worktree left clean.

### Spec Compliance
- ✅ U08-84 `PipeMessage` / `encode_message` / `decode_message` (pipe.py:45-138): six frozen models, `extra="forbid"`, `strict=True`, discriminated on `type`; table caps (message 500, note 200, error_message 2048); `MAX_PIPE_MSG_BYTES = 8_388_608` on encode (pipe.py:114) and on decode BEFORE `json.loads` (pipe.py:131 vs :134); NaN/Infinity rejected; every failure is `SchemaViolation("bad pipe message")` raised outside the except, so no `__cause__`/`__context__` (probe confirmed both None). Duplicate JSON keys follow last-wins (json default), harmless under validation.
- ✅ U08-85 `ChildJobContext` / `ChildServiceControl` (context.py:73-230, _context_base.py): sole daemon reader `herness-pipe-reader`, polled reads with `recv_bytes(MAX_PIPE_MSG_BYTES)`; stop first-wins + Event (_context_base.py:143-147); replies per request_id in `Queue(maxsize=1)`; write lock; heartbeat 1/s throttle with pending-note overwrite (context.py:175-186); CPU slot -> `ConfigError` before any send; default wait formula (_context_base.py:51-65); timeout -> `ModelUnavailable("gpu request timed out")`; `ok=False` -> named class; gpu_scope restore on normal and exception exit, ERROR `jobs.gpu.restore_failed` and original re-raised (_context_base.py:85-107); save_state 4 MiB cap; load_state deep copy; services ops and `healthy` from reply.
- ✅ U08-86 `InlineJobContext` / `InlineServiceControl` (context.py:233-305): `swap(cls, reason="in_job")`, `service_*` direct, `controller None` -> `ConfigError`, `save_job_state` False -> `JobStateError`, heartbeat note in memory, `request_stop` sets the Event (first wins).
- ✅ Protocol conformance: TYPE_CHECKING `_conforms` (context.py:307-312) assigns both contexts to `JobContext` and both `services` to `ServiceControl`; `mypy herness/core/jobs` clean (16 files), so the structural check is real.
- ✅ UT08-97 (test_jobs_pipe.py:51-146): round trip, real Pipe, 9 MB encode and decode (size-before-parse), unknown type / extra field / non-JSON, strict no-coercion.
- ✅ UT08-98 (test_jobs_context.py:163-278): stop + reason, 5 heartbeats -> 1 message, 5 MB state -> SchemaViolation, CPU slot -> ConfigError.
- ✅ UT08-99 (test_jobs_context.py:281-346): restore to `decider` on normal and raising exit, original propagates, restore-failure log.
- ✅ ST08-13 (pipe side test_jobs_pipe.py:153-175; child side test_jobs_context.py:481-508): pickled bytes with a flag-setting `__reduce__` -> SchemaViolation, flag unset; extra field rejected, no content echoed.
- ✅ Acceptance grep (test_jobs_pipe.py:177-192). Mutation probe: added `self._conn.send(data)` in `_send` -> test failed with `['context.py:172']`; reverted with `git checkout`.
- ⚠️ ST08-13 second half ("supervisor treats the child as crashed") cannot exist until the supervisor (T08-21, U08-87 step b). Carry-over recorded in the report; T08-21 must catch `SchemaViolation` from `decode_message` -> `child_crash`.
- ⚠️ IT08-12 (U08-86) needs `run_inline` (T08-22); only CV unit tests exist now. Acceptable carry-over.
- ⚠️ `outcome.error_message` redaction/cap is the sender's job (child_main, U08-88); pipe.py only rejects > 2048. See Minor 3.
- ⚠️ `bind_ids`: U08-85/86 do not require it and neither context calls it; both contexts pass `job_id=` explicitly on every log line (context.py:147, :155; _context_base.py:99-104), so lines are attributable. Binding `job_id` for handler logs belongs to child_main (U08-88) / run_inline (U08-89); flag for those cards, not a finding here.

### Checks run
- Card tests 3x: 77 passed each (about 5 s); `PYTHONUTF8=1 uv run pytest tests/unit/core/jobs -q -p no:logging`: 397 passed.
- Coverage (card tests, branch): pipe.py, context.py, _context_base.py 100 % line and branch.
- ruff check / format: clean. mypy herness/core/jobs: clean. lint-imports: 13 kept 0 broken. tools/check_module_size.py exit 0.
- No edits to herness/core/jobs/__init__.py or ports.py (diff stat has only the 3 source + 3 test files).
- Test naming/pytestmark: `pytestmark = pytest.mark.unit` in all three files; every test names its ID (UT08-97/98/99, ST08-13, CV-T08-20) in the docstring.
- grep of herness/core/jobs/: no pickle/marshal/shelve/eval/exec, no `.send(`/`.recv(`; the only `multiprocessing` use is the TYPE_CHECKING `Connection` import; `queue.Queue` is thread-local (no pickling).

### Focus-item judgements
1. Size-before-parse, strict, forbid, caps, 8 MiB both ways: confirmed. SchemaViolation texts are constants ("bad pipe message", "pipe message exceeds 8 MiB", "job state exceeds 4 MiB").
2. No pickle/eval: confirmed; grep test catches a regression (probe above). The regex is line-based and non-recursive (`glob("*.py")`); the package is flat today.
3. No secrets in child context: heartbeat note redacted then cut (_context_base.py:68-73), fails closed on redactor None; logs carry job_id/reason/class/error_type only; ModelUnavailable/ConfigError texts are constants or the supervisor reply's bounded message.
4. Timeouts: reader polls 0.2 s; reply wait `box.get(timeout=wait_s)`; EOF, OSError, oversized frame, bad frame and wrong direction all wake waiters with a sentinel (context.py:152-166) and later calls fail at once. Tests prove waiting calls do not hang. Builder's concern (child `send_bytes` unbounded): accepted as a residual. The supervisor drains continuously and its stall/terminate path (U08-87 step e) kills a stuck child; `multiprocessing.Connection` offers no send timeout without a writer thread. Keep it on the T08-21 checklist.
5. bind_ids: not used; not required by these units (see ⚠️).
6. U08-85/U08-86 semantics: all confirmed (see Spec rows).
7. `_context_base.py` split: endorsed. context.py 312/360 and _context_base 163 lines; one module would be 423, over the 400 hard limit. Private, underscore-prefixed, imported only by context.py; the §2 row is still to be added (known, not a finding). Deviations: redact-then-cut (endorsed; cutting first can split a secret the redactor then misses, and the final cut keeps the 200 cap); extra bounds (error_class <= 100, timeout_s > 0 and finite, reply request_id ULID: endorsed, but see Minor 1); new log events `jobs.pipe.lost` / `jobs.pipe.late_reply` (endorsed, ids only; need §8.1 rows, same docs follow-up as the module row); pipe loss -> stop reason `shutdown` (endorsed: the handler checkpoints and exits instead of computing for a dead supervisor); services default wait (bounded; see Minor 2); previous_class None -> no restore (reasonable).
8. Gates: all green (above).

### Strengths
- Decode path is minimal and hardened: type/size gate, constant-rejecting json.loads, strict adapter, unchained constant error.
- Reader failure handling is complete: every failure mode fails waiters fast instead of hanging and is tested with real pipes (EOF, oversized 9 MB frame, pickle, extra field, wrong direction).
- Tests are deterministic: Events and joins with generous limits, `clock.monotonic` patched for the throttle, and no sleeps as synchronisation. The 3 runs were stable.
- mypy-enforced Protocol conformance without runtime cost.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. context.py:220-222, :229 (`GpuRequestMsg(... timeout_s=timeout_s)`): a handler passing `timeout_s=0` or a negative/inf value to `require_gpu_class` or `services.start` gets a raw pydantic `ValidationError` (probe: "Input should be greater than 0 [input_value=0]"), not a herness error class. For `services.start(timeout_s=0)` the `timeout_s or start_s(...)` in :228 also treats 0 as "unset" for the wait. Suggest validating `timeout_s` up front -> `ConfigError("timeout_s must be > 0")`. Integers are fine: a strict float field accepts int 30 -> 30.0 (probe).
2. context.py:228: `services.stop`/`healthy` wait `stop+60+start+warm-up+60` (about 20 min with defaults) for what the controller does as a 5 s health GET or a stop. It is bounded, so this is not a hang, but a lost reply blocks the handler far longer than needed. Consider a per-op wait (e.g. stop_timeout_s + 60 for stop, a short bound for healthy). Spec is silent, so this is not a violation.
3. pipe.py:93-100 / U08-88 carry-over: `OutcomeMsg` rejects rather than cuts an over-long `error_message`, and pydantic's `ValidationError` text embeds the input value. child_main must redact and cut BEFORE constructing the model (as `clean_note` does for heartbeats), or an unredacted error text can surface in a ValidationError traceback. Suggest adding this to the T08-21/U08-88 carry-overs explicitly.
4. context.py:128-135 (`_read_loop`): only EOFError/OSError/SchemaViolation are handled. Any other exception (e.g. MemoryError) ends the reader without `_fail_waiters`, so waiters block until their (long) default wait. Add a final `except Exception` -> `_pipe_lost("error")`.
5. tests/unit/core/jobs/test_jobs_pipe.py:177-184: the acceptance grep is non-recursive (`JOBS_DIR.glob("*.py")`) and does not cover other pickling channels (`multiprocessing.Queue`/`Manager`, `marshal`). Use `rglob` and add `multiprocessing.Queue|SimpleQueue|Manager|marshal` to the pattern to future-proof ST08-13.
6. context.py:293-296 (inline `_require`): `controller.loaded` is read outside the controller lock before `swap`. With one inline job per process this is benign; noted only for completeness.

### Assessment
**Task quality:** Approved
**Reasoning:** All three units and every card test row (UT08-97/98/99, ST08-13 child and pipe sides, the acceptance grep) are implemented and proven by deterministic tests at 100 % line and branch coverage with clean gates. The remaining items are minor hardening points and carry-overs to T08-21/T08-22, plus the known §2/§8.1 docs rows for `_context_base.py` and the two new log events.

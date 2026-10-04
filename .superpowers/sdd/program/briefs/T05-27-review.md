# T05-27 review (verify): Security and integration suites

Worktree agent-a608c4a4d35a2cc88, base a51221f, head fccd53f (wip b6faba9, 3a6cbad; fccd53f is an empty commit, noted only).
Verdict: **Needs fixes** (2 Important, both test-only and small).

### Spec Compliance
- ✅ ST05-01: real get_record, get_cluster and run_sql through run_agent and HarnessHooks. The planted payload (`</untrusted_data>`, "ignore previous instructions", fake tool-call JSON, a forged opening tag) is the whole body of exactly one escaped block for get_record and get_cluster. Each run_sql cut cell sits in its own block, and nothing of the payload is outside a block. The obeying model's propose_memory and escalate calls get ToolInputError, the spies never run, and the request tool list is the role set. Mutations V1-V5 turn it red.
- ✅ ST05-09: the cartesian join and the unbounded recursive CTE are each tested in an aggregate form (timeout) and a row form (timeout or size error), with elapsed time < timeout_s + 1 s (time.monotonic). V6 (timer at 3x) turns it red. ⚠️ See the timing note below.
- ✅ ST05-13: already complete, and the file is untouched. The diff a51221f..fccd53f only adds files and modifies none.
- ❌ ST05-14 (e2e half): the trace half is solid, but the "logs above DEBUG" half asserts nothing (Important I-2).
- ⚠️ ST05-15: request body, URL, other headers, trace file, raw log events and the OpenAI translated exception are covered. The Anthropic exception path is not (Important I-1).
- ✅ ST05-17: run_id with separators gives ConfigError and creates no file or directory. The control case writes only `<traces_dir>/<run_id>.jsonl`. V18 turns it red.
- ✅ ST05-18: every llm_call has prompt_hash (equal to the role's), model and client. Every tool_call has args_hash equal to call_signature. Every query has an evidence row and an evidence_use row for this run and task, with no stray uses. V12 and V13 turn it red.
- ✅ ST05-20: through a real loopback FakeLLMServer subclass, 5 MB plain and streamed text give OutputValidationError, a wrong model name gives exactly one model_mismatch WARNING, and non-object args give OutputValidationError. A control case is included. V14-V17 turn it red.
- ✅ IT05-11: real ModelChain retry and OpenAICompatClient streaming from the loopback server. A 500 mid-stream after two deltas leads to one reset, the full answer and an exact final text. A run_sql step runs on the tmp warehouse, with a no-fault control. V19 and V20 turn it red.
- ✅ IT05-12: a real child process with HERNESS_ENV=test and the real HERNESS_FAULTS loader (kill, nth 4 on llm.call). Checks: killed returncode, checkpoint at step 3 with 3 queries, resume with no duplicate evidence_use, state and scratchpad unchanged after the kill and at the end, envelope keys exact, status completed. V21 and V22 turn it red.
- ✅ No existing security test weakened. The diff adds 15 files and modifies none: test_llm_anthropic.py (ST05-13(a)), ST10-25, the TID251 config, ST10-54/55 and ST01-14 are untouched.

⚠️ Items
- ST05-09 timing: each case takes about 1.03 s against a 2.0 s bound (about 1 s of headroom). It passed in 4 isolated runs and in the combined run (tests/security plus the IT files, 783 passed). It is a wall-clock test by nature, so it is a flake risk under heavy CI load but acceptable. V7 (scan cap disabled on its own) survives because the row allows "timeout or size error". The scan_rows cap is therefore not pinned by this card, which is by design of the row and noted only.
- IT05-12 sets checkpoint_min_interval_s=0 in the child config (a deliberate, documented deviation for determinism). This is fine.

### Strengths
- Real components end to end (loop, hooks, ModelChain, adapters, egress loopback client, Tracer, ops store, fault loader, child-process kill). There is no network: respx, MockTransport and a loopback stub server.
- Wire faults are shaped by a subclass (tests/support/impostor_llm.py) rather than by editing T11-23's fake_llm.py.
- Controls are present for ST05-17, ST05-20 and IT05-11. Sentinels are built at runtime and the detect-secrets baseline is unchanged.

### Issues
#### Critical
None.

#### Important
- **I-1 tests/security/test_st05_secrets.py:203-227: the ST05-15 "exception strings" check covers only the OpenAI adapter.**
  - The Anthropic path translates in its own `_fail` (herness/harness/llm/anthropic_client.py:315-323).
  - Mutation V9 changes it to `raise type(err)(f"{err.message} {exc}") from exc`, which puts the provider body echoing the key into the message. All 7 ST05-15 tests stay green.
  - Fix: parametrize the echo test over Anthropic too. A respx or MockTransport 401/403/400/500 whose body echoes `x-api-key` should leave str, repr, args and context of the translated error, and the logs, free of the key.
- **I-2 tests/security/test_st05_trace_privacy_e2e.py:278, 316-318: the "logs above DEBUG" assertion is vacuous.**
  - `configure_logging` runs in the `run_env` fixture, so its SafeStreamHandler binds stderr before the test's `capsys` is set up. `capsys.readouterr()` is empty: a probe measured 0 chars with both capsys and capfd.
  - Mutation V11 logs every untrusted block (including the sentinel email, name and secret) at INFO in `wrap_untrusted`, and the test stays green.
  - Fix: configure logging inside the test after capture starts (or request capfd before run_env, or add a dedicated handler or the log_dir file sink). Then assert the captured log is non-empty, for example that it contains a known INFO event, before asserting the sentinels are absent.

#### Minor
- M-1: strict mypy on the new files (not part of the project gate, since `[tool.mypy] files = ["herness", "tools"]`; the project mypy run is clean) reports 6 errors:
  - tests/support/loop_kill.py:97-98: wrong ignore code (`assignment`; needs `attr-defined`), so it is also an unused ignore.
  - tests/security/test_st05_secrets.py:217: `egress_clients.httpx2` is not an explicit export.
  - tests/integration/harness/test_chat_stream_it.py:137: `_Gpu` does not satisfy GpuStateReader.
  - These are cosmetic; fix them in passing.
- M-2: ST05-01 run_sql gives "one block per cut cell" (two blocks for the two selected cells) rather than one block for the whole text. This is correct given run_sql's 80-char cell cut, and the docstring says so. Noted only.
- M-3: IT05-12 checks state and scratchpad after the kill and at the end, not after every intermediate save (the report says "after every save"). This is adequate, because V21 (scratchpad dropped on loop saves) turns it red. Wording only.
- M-4: the final commit fccd53f is empty (the content landed in the wip commits). Noted per dispatch.

### Builder concern 1 (SDK exception kept as `__cause__`)
- **Verified:** a 401 or 500 echo body puts the key in `str(err.__cause__)` and in `traceback.format_exception(err)` (probe: True for both).
- **Not a TH05-15 defect as specified:**
  - U05-25 step 4, U05-28 step 4 and ENG-STANDARDS line 121 mandate `raise translate_*_error(exc) from exc`.
  - TH05-15's mitigation is "error translation drops bodies". The translated error's message, repr, args and context are clean, and ST05-15 asserts this.
  - The only production renderer of exception chains is the logging pipeline. A probe ran the real `configure_logging(scrubber=scrub_secrets)` and `log.exception` on the translated error. The rendered chain includes the echoed body ("key rejected: ...") but the key is masked, because the resolved key is in `secrets.known_values()`.
  - herness/ has no excepthook, print_exc or format_exception.
- **Recommendation:**
  - Do NOT pin a strict xfail. It would assert the opposite of the mandated `from exc`, and there is no failing observable in a production sink.
  - Record a defense-in-depth carry-over for the U05-30 owner (T05-07): either a spec amendment (for example, chain a body-free stand-in) or explicit acceptance of the residual, which is in-memory only and covered by the log scrubber.
  - Optional cheap pin (can go with I-1): a ST05-15 test that `log.exception` of the translated error, through the configured pipeline, renders without the key.
  - Related existing gap: the ST10-14 xfail (T10-21) shows that job.last_error does not scrub known secrets. It stores only the clean translated message, so it does not apply here.

### Mutation table (own probes; each file restored by rewriting the original bytes; final status clean, no diff under herness/)
| # | Guard disabled | File | Target test | Result |
|---|----------------|------|-------------|--------|
| V1 | get_cluster sample not wrapped | herness/harness/_warehouse_tools_read.py:154 | ST05-01 | RED |
| V2 | get_record body not wrapped | _warehouse_tools_read.py:213 | ST05-01 | RED |
| V3 | run_sql untrusted cell not wrapped | herness/harness/tools.py:211 | ST05-01 | RED |
| V4 | escaping drops `>` | tools.py:170 | ST05-01 | RED |
| V5 | dispatch falls back to any registered tool | herness/harness/_tools_dispatch.py:99 | ST05-01 (obeying model) | RED |
| V6 | interrupt timer at 3x timeout_s | herness/harness/_tools_record.py:175 | ST05-09 | RED |
| V7 | scan_rows cap off (timer intact) | _tools_record.py:136 | ST05-09 | survived (by design: the row accepts timeout) |
| V8 | OpenAI translated message appends SDK text | herness/harness/llm/errors.py:55 | ST05-15 | RED |
| V9 | Anthropic `_fail` message appends SDK text | herness/harness/llm/anthropic_client.py:323 | ST05-15 | **survived (I-1)** |
| V10 | adapter logs the key at INFO | herness/harness/llm/openai_compat.py:229 | ST05-15 | RED |
| V11 | untrusted text (sentinels) logged at INFO | tools.py:169 | ST05-14 e2e | **survived (I-2)** |
| V12 | `client` dropped from llm_call | herness/harness/tracing.py:51 | ST05-18 | RED |
| V13 | evidence_use written without task_id | tools.py:158 | ST05-18 | RED |
| V14 | bound_response text cap off | herness/harness/llm/base.py:123 | ST05-20 (5 MB) | RED |
| V15 | stream text cap off | herness/harness/llm/_openai_stream.py:58 | ST05-20 (streamed) | RED |
| V16 | model_mismatch warning off | herness/harness/llm/_openai_map.py:172 | ST05-20 | RED |
| V17 | non-object args coerced to {} | _openai_map.py:127 | ST05-20 | RED |
| V18 | traces dir created before the run_id check | tracing.py:185 | ST05-17 | RED |
| V19 | reset sent at every stream start | herness/harness/hooks.py:132 | IT05-11 | RED |
| V20 | reset never sent | hooks.py:133 | IT05-11 | RED |
| V21 | loop save drops `scratchpad` (R-21) | herness/core/jobs/tasks.py:127 | IT05-12 | RED |
| V22 | evidence_use INSERT not idempotent | herness/store/ops/evidence.py:80 | IT05-12 | RED |

### Determinism, offline and gates
- **Repeat runs:** the new tests (34) passed in 4 runs (27.7 s, 19.0 s, 18.6 s, plus the run inside the combined suite).
- **Combined run:** tests/security plus both IT files in one process: 783 passed, 1 skipped (symlink privilege), 1 xfailed (pre-existing ST10-14).
- **Offline:** respx, MockTransport, a loopback stub server, FakeLLMClient and ListClient. No real network.
- **Timing:** wall-clock time is used only in ST05-09 (inherent). IT05-12 uses checkpoint interval 0, so it is not timing-dependent.
- **Naming and markers:** IDs are in the function names, docstring first lines start with the ID, and pytestmark is set (unit for security, integration for IT). `pytest --require-test-ids --co` collects 34.
- **Gates:** ruff check and ruff format are clean on the 11 new .py files. check_module_size rc 0 (largest new file 239 lines). Project mypy reports no issues (337 files).

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Twenty of the 22 probes turn red, and the rows are exercised with real components. However, ST05-14's log half asserts nothing (V11 survives), and ST05-15's exception-string check misses the Anthropic translation path (V9 survives). Both are small, test-only fixes.


## Re-review round 1 (head 25d9914, prev fccd53f)
Verdict: **Approved**

Scope: I-1, I-2, M-1 (diff briefs/T05-27-review-r1.diff; report section "Review round 1").

### Findings
- **I-1 fixed** (tests/security/test_st05_secrets.py).
  - New `test_st05_15_anthropic_error_echoing_key_is_dropped_from_exception[401,403,400,500]` goes through the real AnthropicClient and the real egress guard (hybrid profile), with the pool transport echoing `x-api-key`.
  - New logged-exception tests cover both adapters, using the real `configure_logging(scrubber=scrub_secrets)` set up after capsys starts. Each has positive controls: the event line and "key rejected" (the rendered SDK cause) must appear.
  - The OpenAI echo test was refactored into the shared helper `_assert_clean_error`. It keeps every original assertion: str, repr, args, context, logs, and the exact message.
- **I-2 fixed** (tests/security/test_st05_trace_privacy_e2e.py). `configure_logging` now runs in the test body after capsys. Positive controls (`core.logging.configured` and the test's own `st05_14.control` line) are asserted before the sentinel-absence loop. All original trace and log assertions are still there.
- **M-1 fixed.** `mypy --strict --explicit-package-bases` on the 11 new files reports "Success: no issues". loop_kill.py now uses setattr, there is no `egress_clients.httpx2` access, and `_Gpu` matches GpuStateReader. Ruff check and format are clean.
- **Minor (new, nit):** the control event names `st05_15.call_failed` and `st05_14.control` have only two segments. The pipeline flags them `event_name_invalid: true` (non-strict mode, so harmless). Three-segment names, for example `st05_15.call.failed`, would avoid the flag. Optional.

### Probes (each file restored by byte rewrite; final `git status` clean; `git diff --stat herness/` empty; HEAD 25d9914)
| # | Mutation | Target | Result |
|---|----------|--------|--------|
| V9 | Anthropic `_fail` raises `type(err)(f"{err.message} {exc}")` (anthropic_client.py:323) | ST05-15 | RED (it survived in round 0) |
| V11 | `wrap_untrusted` logs its text at INFO as field `text=` (tools.py:169) | ST05-14 e2e | green, as the builder said: `text` is in `_FREE_TEXT_NAMES` (herness/core/_log_pipeline.py:33-36). `_guard` (line 144) replaces it with "[omitted]" at INFO, so nothing leaks and this is not a test gap. Confirmed. |
| V11d | same, field `detail=` | ST05-14 e2e | RED (the sentinel reaches the captured log) |
| V23 | `scrub_secrets` returns the event unchanged (secrets.py:144) | ST05-15 logged-exception tests | RED (key in the rendered cause) |

### Runs
- The 9 card files ran twice: 40 passed (19.4 s, 27.8 s).
- tests/security plus both IT files in one process: 789 passed, 1 skipped (symlink privilege), 1 xfailed (pre-existing ST10-14).

### Carry-over (unchanged from round 0)
The `__cause__` chain holding the provider body is spec-mandated (`from exc`). It is masked by the log scrubber, and that is now pinned by the two logged-exception tests. The defense-in-depth decision stays with the U05-30 owner, T05-07; no xfail.

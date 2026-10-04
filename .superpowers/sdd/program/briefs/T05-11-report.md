# T05-11 Tracer: build report

## What was built
- `herness/harness/tracing.py` (358 lines; budget 360): `Tracer`, which implements `TraceEmitter`, plus `TraceType` (a StrEnum with the 10 values), `llm_call_fields` and `tool_call_fields` (U05-69, U05-70).
  - Tracer API: `__init__` with the U05-69 signature. `for_run` reads `get_config().paths.data / "traces"` (controller ruling) and `get_config().models.harness.trace`. Also `null()`, `bind(task_id, role)` for views, the `run_id`/`task_id` properties (R-66), `emit`, `is_sampled`, `close` and `health`.
  - Each root tracer owns one daemon writer thread. The writer puts every event through `herness.core.secrets.scrub_secrets(None, "trace", event)` (controller ruling). If the scrub fails closed and returns `{"event": "log.scrub.failed"}`, the event is dropped, counted and logged. The writer then appends one JSON line to the file (UTF-8, append mode) and flushes once `flush_interval_s` has passed. When the dropped counter is above 0, it writes a `budget` event with `kind="dropped_events"`, `used={}`, `limit={}`, `message="dropped <n> trace events"`.
  - Enqueue uses `put_nowait`. At `qsize >= ceil(0.8*queue_max)` the payload is dropped and `payload_dropped: true` is set. When the queue is full, the event is dropped and counted. `emit` calls after `close` are counted as dropped.
  - Payload handling: the payload is kept only when `is_sampled(effective task_id)`. The sample test is `sha256(task_id or run_id)[:8]/2^32 < rate`. The payload is made JSON-safe. Then each string passes `redact_text`, is cut to `max_payload_chars`, and is scrubbed. Every `opaque` key is removed at any depth. If `redact_text` returns None, that string becomes null. If redaction raises, the payload is dropped (fail closed) and WARNING `harness.trace.payload_failed` is logged.
  - Errors and logging:
    - An unknown type raises `ConfigError("unknown trace type <t>")`.
    - A bad `run_id` raises `ConfigError("invalid trace run_id")`, a bad queue or interval raises `ConfigError`, and so does a traces dir that cannot be created.
    - I/O errors log ERROR `harness.trace.write_failed` (error_type only, once per failure streak), are counted as dropped, and the writer keeps draining.
    - A close timeout logs WARNING `harness.trace.close_timeout`.
  - `health()` returns `{"status": "down" | "degraded" | "ok"}`.

## Deviations and choices (reviewer please check)
1. **JSON-safe conversion uses `pydantic_core.to_jsonable_python(..., fallback=str)`, and the writer uses `to_json(inf_nan_mode="strings")`.** This was done to fit the budget: a hand-written converter put the file at 487 lines. Effects:
   - A UTC datetime is written as `2026-09-26T01:02:03Z`. This is ISO with Z, but not the fixed-width 27-char form.
   - An aware non-UTC datetime keeps its offset instead of being converted to Z.
   - NaN is written as `"NaN"`.
   - The common `ts` field still uses `clock.format_utc` (fixed width).
2. **A bound view's `close()` is a no-op.** Only the root drains and closes, so a task cannot close the run's writer. The spec does not cover this case.
3. **Fields named like common fields are refused.** `ts`, `type`, `run_id`, `build_id`, `span_id`, `payload` and `payload_dropped` in `**fields` raise `ConfigError("reserved trace field <k>")`.
4. **Payload strings are scrubbed at emit time as well as at write time.** `scrub_secrets` stops recursing at depth 6, so a secret nested in tool-call arguments inside a message part would otherwise pass unscrubbed.
5. **`null()` behaviour.** It uses run_id `run_000…0` (26 zeros), still validates the type, and returns a fresh ULID without building the event.
6. **`TraceType` uses the functional StrEnum API**, again for the budget. Members are lower-case (`TraceType.budget`) and values equal the names.
7. **A `close()` join timeout does not close the file.** The writer thread owns the file handle and closes it when it exits; `close()` only logs the WARNING. This avoids a cross-thread race on the handle.
8. **No `run_kind` membership check.** `TraceSettings.payload_sample_rate` always holds all three kinds (`min_length=3`, Literal keys), and the type checker enforces `run_kind`.

## Tests (all pass)
- `tests/unit/harness/test_tracing.py` (unit), 23 tests:
  - UT05-43: every type, common fields, JSON-safe fields, bind views, the llm and tool field helpers, bad run_id, bad arguments, null, for_run.
  - UT05-44: determinism, and 10 % ± 1.5 % over 10,000 ids.
  - UT05-45: sentinel email redacted, `opaque` gone at every depth, 50,000 chars cut to 20,000, fail-closed paths.
  - UT05-46: with queue_max 10 and the writer held, payloads drop from emit #8 (0-based) and events from #10; the budget line reads "dropped 10 trace events". Also covers emit after close, I/O error, scrub failure, dead writer shown as down, close_timeout, periodic flush.
  - UT05-130 (tracer part only): run_id/task_id on the root and on views.
- `tests/security/test_st05_tracing.py` (unit), ST05-14 tracer part: full config through `init_config`. The secret is resolved through the keyring into known_values; the name comes from the redactor directory; the email is in the ticket text; an opaque block is in a reasoning part. Payload rate is 1.0 (eval) through `for_run`. None of the sentinels and no `"opaque"` key appear in the trace file or in the INFO logs.
- `tests/security/test_st05_trace_flood.py` ([integration, slow]), ST05-19: 1,000,000 emits. p99 < 1 ms, queue depth never above 10,000, `dropped_events` reported, and written + dropped == 1,000,000. Takes about 10 s without coverage.
- `tests/fault/test_harness_tracing_fault.py` (fault), FT05-01: a subprocess writing traces is killed after the file passes 1 MB. Every complete line is valid JSON; only the last line may be truncated. Takes about 5 s.
- `tests/bench/test_harness_tracing_bench.py` ([integration, slow]), BT05-11: 100,000 events without payload.
- **Measured on the dev box:** enqueue mean 0.0058 ms (target < 0.2 ms); writer 55,000 events/s end to end (target ≥ 1,000 events/s); all 100,000 lines written.
- **Coverage of tracing.py:** 99 % line; 1 partial branch out of 54. The only uncovered code is the flush-OSError lines and the `if late:` race drop.
- **Gates:** ruff format and ruff check clean, mypy clean, lint-imports 13 kept, check_module_size OK, check_type_ownership OK. The card `-k` selection, including the slow tests, gives 27 passed. `-m "(unit or integration) and not slow"` gives 4233 passed, 5 skipped, 1 xfailed.
- **detect-secrets:** the sentinels are built at runtime and the variables were renamed. No baseline change.

## Carry-overs
- ST05-14 end to end (scripted run with ticket and memory through `run_agent`, plus the logs check across the whole run) needs T05-22 `run_agent` and the impl 11 scripted-run fixtures.
- UT05-130 loop part (loop_signals and nudges) belongs to T05-22.
- ST05-18 (llm_call and tool_call provenance end to end) is not in this card's Tests row. `llm_call_fields` and `tool_call_fields` are covered by UT05-43.

## Concerns
- Deviation 1 (datetime text from pydantic instead of the fixed-width format) is the one most likely to be questioned. If a ruling requires fixed-width Z for field datetimes, it will cost about 8 lines against a budget that has 2 lines left.
- The file is at 358/360, so later cards have almost no room here.

## Fix round 1 (review T05-11-review.md)
- **New private module `herness/harness/_trace_clean.py` (105 lines, L4, no spec row, under the 150-line ruling).** It holds `clean_fields` and `clean_payload`, which share one walk. The walk:
  - turns models into dumps, dates into UTC Z, and Enum into its value;
  - makes Decimal, Path, bytes and any other object `str`, and NaN `"nan"`;
  - drops every `opaque` key at any depth;
  - cleans every string, dict keys included;
  - raises `ConfigError` below depth 64 (this catches circular values).
- `tracing.py` is now 341 lines (budget 360). check_module_size and lint-imports (13 kept) pass on the new module unchanged.
- **I-1:** payload keys pass `redact_text` and the scrub. A key that cannot be cleaned is dropped together with its value.
- **I-2:** each payload string is redacted, then scrubbed as a whole, and only then cut to `max_payload_chars`. `scrub_secrets` itself looks only at the first 64 KiB. When a string is longer than that, the cleaner drops the last N characters of the scrubbed text, where N is the length of the longest known value. A secret cut at 64 KiB can only sit in that tail. The test covers max_chars of 20,000 and 200,000.
- **I-3:** `opaque` is stripped from `**fields` at every depth (plain key, a `ReasoningPart` model, a nested list).
- **M-1:** datetime fields use `clock.format_utc` (fixed-width Z). Aware values are converted to UTC; naive values are taken as UTC. `ts` is unchanged.
- **M-2:** field strings and keys get the per-string secret scrub at emit, at any depth.
  - Field strings are not passed through `redact_text`; the spec redacts payloads only. A plain field holding an email is therefore still written (repro case D2).
  - Field keys that fail the scrub are dropped.
- **M-3:** fields that cannot be serialized raise `ConfigError("trace fields cannot be serialized")`. The same values given as a payload drop the payload (`payload_dropped`).
- **M-4:** after a write error with an open handle, the next line is prefixed with `\n`. The torn fragment stays on its own line and later lines are valid JSON. The side effect is a possible empty line when the failed write wrote nothing, so readers should skip empty lines.
- **M-6:** `payload_failed` now carries `run_id`.
- **Parked as instructed:** M-5 (emit/close race).
- **Tests:** 7 new regression tests (UT05-43, UT05-45, UT05-46 IDs) plus updates to the datetime, NaN and redaction-failure tests.
  - The card selection gives 35 passed, slow ones included.
  - Harness unit, security and fault tests (not slow) give 925 passed.
  - Coverage: `tracing.py` 99 % line; `_trace_clean.py` 100 % line and branch.
  - Both repro scripts are clean: no leaks, and ConfigError is raised or the payload dropped as expected.
- **Throughput now:** enqueue mean 0.011 ms (was 0.006; field scrub per string); writer 46,700 events/s. BT05-11 and ST05-19 pass.

## Fix round 2 (re-review N-1)
- **N-1:** `_trace_clean._scrub` now drops a longer tail from strings over 64 KiB: `max(_MIN_CUT_TAIL = 512, longest known value)` characters. The constant is named and commented. This also covers credentials the detectors find by pattern (`ghp_`, `xox...`) when one is cut at the 64 KiB limit.
- **Regression test:** UT05-45 `test_ut05_45_pattern_credential_across_scrub_limit_leaves_no_prefix`. It puts a `ghp_` token across the limit in a field string and checks that the note is all `x` with length 65536-512.
- **repro4:** no fragment is written any more.
- **N-2:** parked. The `clean_payload` docstring now notes that payload strings are capped near 64 KiB.
- **Line counts:** `_trace_clean.py` 110 (ruling ≤ 150); `tracing.py` 341/360.
- **Tests:** the card selection gives 36 passed, slow ones included. Coverage of `_trace_clean` is 100 %. mypy, ruff, detect-secrets and module-size are clean.

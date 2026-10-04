# T05-11 Tracer: review (verify agent)

**Verdict: Needs fixes** (3 Important findings: two TH05-14 leak paths and one broken invariant. No Critical findings.)

Reviewed commit 0551118 (base 6e45de8). Reviewed files: `herness/harness/tracing.py` (358/360 lines) and five test files.

Evidence I ran myself:
- The card test files: 27 passed.
- Coverage of `tracing.py`: 99 % line, 1 partial branch out of 54 (missing lines 139-140 and 169).
- `--require-test-ids`: passes.
- mypy: clean. ruff check: clean. lint-imports: 13 contracts kept.
- Leak repro scripts: `repro.py` and `repro2.py`, next to this file.

## Spec compliance
- ✅ **U05-69 Tracer**
  - Signature, `for_run` (paths.data/traces, per the ruling), `null`, `bind` and the `run_id`/`task_id` properties all match.
  - The unknown-type check raises `ConfigError`.
  - The `run_id` regex uses fullmatch and re.ASCII.
  - Every line carries the common fields, including the writer's budget line.
  - `is_sampled` matches the spec exactly.
  - Payloads drop at `qsize >= ceil(0.8*queue_max)` and events drop when the queue is full. The `dropped_events` budget line has the exact form.
  - Writer thread: I/O errors are logged at ERROR and the thread keeps draining. Health reports down, degraded or ok. Close semantics match, and emits after close are counted as dropped.
- ❌ **U05-69 invariant / TH05-14:** see I-1, I-2 and I-3.
- ❌ (minor) **"datetime → ISO Z":** see M-1.
- ✅ **U05-70:** `llm_call_fields` gives the 17 fields and the payload as specified. `tool_call_fields` gives the 8 fields and `{"args": ...}`.
- ✅ **Tests:**
  - IDs are in function names and docstrings.
  - `pytestmark`: unit; `[integration, slow]` for ST05-19 and BT05-11; `fault` for FT05-01.
- ⚠️ **Carry-overs, per ruling:** ST05-14 end to end and the UT05-130 loop part (T05-22 / impl 11). The dev-box throughput figure comes from the builder (55k/s); my own bench run also passed.

## Builder concerns
- **Datetime:** Minor (M-1). A ruling is needed.
- **Accepted as-is:** NaN written as `"NaN"`, a view's `close()` as a no-op, reserved fields raising `ConfigError`, lower-case `TraceType` members, and no `run_kind` check.
- **Emit-time scrub:** correct in intent, but the order is wrong (I-2) and it covers the payload only (M-2).

## Important
**I-1: dict keys in the payload are never redacted or scrubbed, so personal data leaks (TH05-14).**
- Where: `herness/harness/tracing.py:69-70`. `scrub_secrets` also scrubs values only (`herness/core/secrets.py:130`).
- Repro:
  ```python
  tr.emit("retry", payload={"sentinel.person@example.org": 1})
  ```
  The line contains `"payload": {"sentinel.person@example.org": 1}`.
- Why it matters: tool args and tool_calls are model-controlled.
- Fix: clean the keys too, and add a UT05-45 case.

**I-2: the cut to `max_payload_chars` happens before `scrub_secrets`, so a known secret that straddles the cut leaks its prefix.**
- Where: `herness/harness/tracing.py:61`.
- Repro: `_remember(SECRET)`, where SECRET is 34 characters. Then:
  ```python
  tr.emit("retry", payload="a " * 9995 + SECRET)
  ```
  The payload ends in `Zq9f8e7d6c`, the first 10 characters of the secret.
- Fix: scrub the whole string first, then cut. Add a boundary test.

**I-3: the U05-69 invariant (no line contains `ReasoningPart.opaque`) is not enforced on `**fields`.**
- Where: `herness/harness/tracing.py:272`.
- Repro: each of these writes the opaque content:
  ```python
  tr.emit("retry", opaque={"sig": "S"})
  tr.emit("retry", part=ReasoningPart(..., opaque={"sig": "S"}))
  tr.emit("retry", parts=[{"opaque": "S"}])
  ```
- Fix: strip `opaque` at every depth from the converted fields as well (or in `_write`), and add a test.

## Minor
- **M-1:** `tracing.py:272` and `:114`. A naive datetime is written as `2026-01-02T03:04:05` (no Z), and an aware non-UTC datetime keeps `+05:00`. The spec says "ISO Z", and the test covers only UTC.
- **M-2:** `tracing.py:272` and `:110`. Fields rely only on the write-time scrub, which stops at depth 6. `emit(..., deep=[[[[[[[[SECRET]]]]]]]])` writes the secret verbatim.
- **M-3:** `tracing.py:272`. A circular field, or a field whose `__str__` raises, makes `emit` raise `ValueError`/`RuntimeError`, not `ConfigError`. The payload path fails closed; the fields path does not.
- **M-4:** `tracing.py:119-122`. A partial write followed by the next append corrupts a line mid-file, but FT05-01 allows only the last line to be truncated.
- **M-5:** `tracing.py:274-283` vs `:163-169`. In the close race, a late `put_nowait` can leave an event that is neither written nor counted.
- **M-6:** `tracing.py:295`. The `payload_failed` WARNING has no `run_id`.
- **M-7:** the file is at 358/360 lines, so the fixes need line savings or a budget ruling.

## Strengths
- The shared `_Core` with thin views is clean, and the writer owns the file handle.
- Redaction and scrub failures fail closed and are tested.
- Deep payload nesting is scrubbed at emit time.
- bytes, sets and `str`-fallback objects are redacted.
- The UT05-46 test pins exact indices.
- ST05-19 checks written + dropped == 1e6.
- Coverage is 99 %. Gates are clean. Layering is OK.

## Assessment
**Task quality:** Needs fixes
**Reasoning:** Queue, sampling, writer and helpers are correct and well tested. But TH05-14 is broken in two reproducible ways (unredacted keys, a partial secret at the cut boundary), and the opaque invariant does not hold for fields. Each is a few-line fix plus a regression test.

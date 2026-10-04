# T05-16 review — Tool registry and dispatch (base 88a7f75, head 1f7ea17)

Verdict: **Needs fixes** (1 Critical, 2 Important, 5 Minor)

### Spec Compliance
- U05-33 ToolRegistry / tool_registry / TOOL_OWNERS: ✅ (14 names verbatim, read-only MappingProxy; register checks owner + Draft 2020-12 + R-26 strict at every level; idempotent for same object; resolve allow-list → task-tool owner 06 → registry, sorted order; tool_specs strict=True; threading.Lock).
- U05-34 dispatch: ❌ "nothing else escapes" is violated for calls rejected at steps 1-2 (Critical C1); otherwise steps 1-13 match (order, cap 16, size cap before signature/schema, repeat + remember, schema hint, run_sql cap/count, TaskGroup + Semaphore(get_config max_parallel), retry_call/aretry_call("tool_store") only, FatalError unwrapped after sibling cancel, results in order, trace + metrics).
- Constants (spec ~1464): MAX_TOOL_CALLS_PER_MESSAGE=16, MAX_TOOL_ARGUMENT_CHARS=32,000 importable from herness.harness.tools ✅ (defined in _tools_dispatch, re-exported; recorded in the §2 row).
- Adapter bound MAX_TOOL_CALLS=64 (herness/harness/llm/base.py:42, anthropic_client.py:54) untouched ✅.
- Tests: UT05-68 ✅, UT05-69 ✅, UT05-70 ✅, UT05-71 ✅, UT05-72 ✅, UT05-73 ✅, UT05-74 ✅, UT05-75 ✅ (attempts==3 asserted both ways), UT05-76 ✅, UT05-77 ✅, UT05-78 ✅, UT05-79 ✅, UT05-131 ✅, ST05-02 ✅, ST05-07 ✅ (static + spy cursor via stand-in run_sql; carry-over to real tools), ST05-10 ✅, ST05-16 ✅ (but misses the step 1-2 variants, see C1), ST05-22 ✅, BT05-02 ✅.
- ⚠️ Cannot verify: ST05-07 "spec 05 tool classes execute only SELECT" against the real tool classes (T05-17/18 not built; stand-in used — carry-over). RoleSpec / LoopHooks typed via local Protocols (ruling 1) until T05-19/22.

### Probes run
- Baseline: 56 passed / 1 skipped (symlink privilege) on the card test files; 1102 passed / 2 skipped for tests/unit/harness + ST05 tools/warehouse under coverage.
- BT05-02 re-run: **mean 0.282 ms per call** (< 5 ms).
- Mutation probes (scratch pytest plugin outside the tree, monkeypatching modules): cap→10^6 (UT05-79, ST05-10 fail); size cap→10^9 (ST05-16 fails); schema_hint→None (UT05-71, UT05-72 x2 fail); _strict_ok→True (UT05-69, UT05-131 x7 fail); all TOOL_OWNERS→"06" (19 fail incl. ST05-02/07/22); semaphore→1000 (UT05-78, ST05-10 fail); name-lookup bypass (ST05-02, UT05-73, ... fail); _first_fatal no-unwrap (UT05-76 x2 fail); warehouse schema() fix reverted (concurrent_selects fails 3/3). Every mutation caught.
- Hint probes: enum / maxLength / pattern / nested type errors show `<string value>`, JSON path present, ≤ 300 chars; additionalProperties echoes the offending KEY (M1).
- Gates: ruff check/format clean; mypy clean on the 4 touched herness modules; check_module_size exit 0 (tools.py 392/400, _tools_dispatch.py 192/250, _tools_schema.py 94/120, warehouse.py 215/220); coverage tools.py 100 %, _tools_dispatch 98 %, _tools_schema 98 %, warehouse 96 % (branch partials: 2, 1, 2). Test IDs / docstrings / pytestmark OK; `-k` over all 19 IDs collects 44 tests. No getattr/importlib/eval on model data; no local retry loop. git status clean.

### Strengths
- Clean split into _tools_dispatch/_tools_schema with §2 rows; tools.py public surface as specified.
- Size cap truly precedes signature and schema work on the execution path (ST05-16 spies schema_hint); rejected blobs traced with `{}`.
- Tests are real: every mutation probe flips at least one test red.
- Hint never echoes argument values (repr replaced by `<type value>`), bounded 300.
- Warehouse race fix is minimal and has a regression test that reliably catches the race.

### Issues
#### Critical (Must Fix)
- C1 `herness/harness/_tools_dispatch.py:99-106` + `:185` (via `herness/harness/tracing.py:336`): a call rejected at step 1 (index ≥ 16) or step 2 (unknown name) keeps `slot.traced = call` with its full arguments; `finish` then calls `tool_call_fields`, whose `LoopState.call_signature` → `canonical_json` raises `SchemaViolation` (a **FatalError**) for arguments nested > 64 deep. Probe: 16 valid calls + a 17th with `{"n": nested(75)}` → dispatch raises `SchemaViolation: canonical JSON too deep`; same for `ToolCall(name="shell", arguments={"n": nested(75)})`. ToolCall accepts depth 75 and the adapters do not bound depth, so model output (or injected ticket text steering it) aborts the task/run — violates "nothing else escapes" and TH05-16/TH05-10. Fix: apply the step-3 size/JSON guard to the traced copy of calls rejected at steps 1-2 too (blank `traced` arguments on SchemaViolation / oversize for every slot); add ST05-16/ST05-10 cases (deep + 1 MB args on call #17 and on an unknown name) asserting an error result and `{}` payload.

#### Important (Should Fix)
- I1 `herness/harness/_tools_dispatch.py:99-106`: same root as C1 for size — a 1 MB argument blob on call #17+ or on an unknown tool name is handed whole to the tracer (probe: RecordingTracer payload repr 1,000,019 chars) and fully canonicalised for `args_hash`. The real Tracer's `clean_payload` caps it, so no raw record, but the claim "1 MB payloads never enter the tracer" is false on these paths and ST05-10/ST05-16 do not cover them. Fixed by the same change as C1.
- I2 `herness/harness/warehouse.py:134-141` (`table_comment`): still executes on the shared `self._con` from tool threads; it will race like the old `schema()` once describe_table (T05-17) runs under concurrent dispatch. The concurrency exposing it is introduced here — fix now in the same style (`with self._con.cursor() as cur`, budget 215/220) or record a binding carry-over on T05-17.

#### Minor (Nice to Have)
- M1 `herness/harness/_tools_schema.py:88-93` `schema_hint`: `additionalProperties` / `unevaluatedProperties` / `patternProperties` messages echo model-supplied property NAMES (probe: `$: Additional properties are not allowed ('sk-SECRETKEY1234567890abcdef' was unexpected)`). Keys are model-controlled text (ENG §3.4); replace the list with a count or the first key cut short.
- M2 `herness/harness/_tools_schema.py:23-25,36-47` `_strict_ok` does not descend into `dependentSchemas`, `propertyNames`, `unevaluatedProperties`/`unevaluatedItems`, nor follow `$ref`; a non-strict object hidden there passes registration. Low risk (schemas are code-owned).
- M3 `herness/harness/_tools_dispatch.py:78`: rejected blobs are traced with `arguments={}`, so `args_hash` is the hash of `{}` for every rejected blob — weakens ST05-18 attribution; acceptable trade-off, document it.
- M4 `herness/harness/_tools_dispatch.py:122-123` `_is_async` misclassifies a sync callable returning an awaitable (e.g. partial of an async fn); fine under the Tool/AsyncTool protocols.
- M5 `tests/security/test_st05_tools.py:331` the warehouse cold-schema regression carries ID ST05-07; allowed by the shared-ID rule, but the docstring should say it is a T05-13 regression guard.

### Builder deviations
- Ruling 1 Protocols (_RoleLike, _LoopHooksLike): accepted (carry-over).
- resolve dedups, requires task-tool key == tool.name and strict schema: accepted (stricter, TH05-22).
- step 11 counts only executed run_sql QueryErrors: accepted (avoids double count).
- empty `calls` → []: accepted.
- functools.partial into retry_call/aretry_call: accepted (avoids kwarg collision with retry_call's own keywords; still policy "tool_store").
- warehouse.py edit outside Files: correct and thread-safe (lock + check under lock, dedicated short-lived cursor; per-call lock cost negligible), justified by a reproduced race this card's concurrency exposes; regression test mutation-proven. Accept the provisional ruling; see I2.
- test config via write_full_config: accepted.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Registry, caps, schema rules, retry policy and tests are solid and mutation-proven, but a model-controlled deeply nested argument on a 17th or unknown-name call makes dispatch raise a FatalError (C1), breaking the "nothing else escapes" invariant; the same path passes oversized blobs to the tracer (I1).

---

## Re-review round 1 (head b4d1054, previous 1f7ea17)

Scope: C1, I1, I2, M1, M5 (M2, M3, M4 parked by ruling). Diff: briefs/T05-16-review-r1.diff.

Verdict: **Approved**

### Findings status
- C1 ✅ fixed — `herness/harness/_tools_dispatch.py:68-84,99-116`: `_vet` now runs on every call before steps 1-2 and blanks the traced arguments whenever the size or JSON check fails. Probes (scratch script, real `dispatch`): 16 valid calls plus a 17th with depth-75 args → no exception, 16 executed, 17th gets "too many tool calls in one message (max 16)"; unknown name `shell` with depth-75 args → no exception, "tool shell not allowed; allowed: get_metric". A depth-75 call to a known tool → "tool arguments are not valid JSON values".
- I1 ✅ fixed — 1 MB args on call 17 (parsed-only and raw-text variants) and on unknown `shell`: no exception, the largest traced event repr is ≤ 210 chars (payload `{"args": {}}`). New test `test_st05_16_blobs_on_rejected_calls_never_raise_or_trace` [beyond_cap, unknown_name] covers raw, parsed and deep blobs on both paths.
- I2 ✅ fixed — `herness/harness/warehouse.py:136-143` `table_comment` now uses its own short-lived `self._con.cursor()`. New `test_ut05_49_concurrent_schema_and_table_comment` (8 threads × 20, cold handle).
- M1 ✅ fixed — `herness/harness/_tools_schema.py:27,93-94`: `additionalProperties` / `unevaluatedProperties` / `propertyNames` errors become `"<validator> failed; property names not shown"`. Probe: key `sk-SECRETKEY…` → hint `$: additionalProperties failed; property names not shown`. A `propertyNames` sub-error (validator `maxLength`) still masks via the `<string value>` path. New test `test_ut05_72_extra_property_names_not_echoed`.
- M5 ✅ fixed — the docstring at `tests/security/test_st05_tools.py` (concurrent_selects) now says it is a T05-13 warehouse regression guard.

### Mutation probes (scratch plugin outside the tree)
- Restoring the old tracing of full args for step 1-2 rejects → both new ST05-16 cases fail.
- Emptying `_NAME_ECHOING` → `test_ut05_72_extra_property_names_not_echoed` fails.
- Reverting `table_comment` to the shared connection → `test_ut05_49_concurrent_schema_and_table_comment` fails 3 of 3 runs.

### Regression check
- `tests/unit/harness` + ST05 tools/warehouse: 1106 passed, 2 skipped (symlink privilege). BT05-02 re-run: **mean 0.274 ms per call**.
- `ruff check .` clean, `ruff format --check .` clean, `mypy herness` clean (213 files), `check_module_size` exit 0: `_tools_dispatch.py` 193/250, `_tools_schema.py` 97/120, `warehouse.py` 216/220, `tools.py` 392/400.
- Coverage: `_tools_dispatch` 98 %, `_tools_schema` 97 %, `tools` 100 %, `warehouse` 96 %.
- Behaviour is unchanged for valid calls. Step 7 still records only calls that passed steps 1-3 (calls at index ≥ 16 compute a signature but do not record it).
- `_vet` now canonicalises the args of every call, including calls 17+. This is bounded by the adapter's `MAX_TOOL_CALLS=64` and the ~1 MB response cap, so there is no new DoS surface.
- Nit, no action: the `bad_args or "invalid arguments"` fallback at `_tools_dispatch.py:110` cannot be reached (`_vet` always returns an error when `sig` is None). It is harmless and kept for the type checker.

No new findings.

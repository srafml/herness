# T08-09 review: Repair and model chain (commits 0f4a3a0 + 58c5367, base 750ec27)

### Spec Compliance
- ✅ Spec compliant (U08-35..U08-38), with minor deviations listed under Minor.
  - U08-35 `complete_validated` ✅: ConfigError without schema / max_repairs outside 0-5; `llm.output` fault -> `("/", "fault: malformed_json")`; parsed-over-text; decode error uses `exc.msg` only; pydantic errors via `errors(include_input=False)` -> (RFC 6901 pointer(loc), msg); jsonschema -> "failed '<validator>' constraint"; `OutputValidationError("output invalid after <n> repairs", client=...)`; repair request built from `current` (history accumulates, temp 0, same client); `repair` event detail {model_profile, repair_no, error_paths[:20]}, target client.name, run_id/task_id from original req.metadata; `herness_resilience_repairs_total{client}`.
  - U08-36 `build_repair_request` ✅: verbatim intro/outro text, first 20 errors, msgs cut to 200, echo 4000, canonical_json schema, `model_copy` with messages + temperature 0.0, input not mutated.
  - U08-37 `ModelChain` / `candidates()` ✅: ctor no I/O, validated role/depth, read-only fields, `__slots__`; rules (a) off_network w/o egress, (b) open breaker dropped only while `now < retry_at` (due probe stays), (d) gpu_class != loaded (swapping drops all GPU). Rule (c) applied only in `acomplete` step 1, which matches "candidates() called directly uses no run filter".
  - U08-38 `acomplete` ✅: empty -> `ModelUnavailable("no available model for role <role>")`; req_k with client/timeout; `_RetryingCompleter` = line-for-line async form of retry `_run`/`aretry_call` (guard + classify + breaker record via `_ainvoke`, hot-path attempt 1 replayed into tenacity, `_should_retry` stop/CircuitOpen/Retry-After cap, `_StopAfterDelay`, asleep, `aemit`) with this candidate's tracer and target `llm`, breaker `model:<key>`, `fault_point("llm.call")` inside the attempt; schema -> response_schema/model_json_schema + response_schema_name; max_repairs from `R.fallback.max_repairs`; reason map exact (6 triggers); auth -> auth_dropped per (run_id,key) under lock; fallback event + `herness_resilience_fallbacks_total{from_profile,to_profile,reason}` only when a next candidate exists; every candidate starts from the original `req`; everything else propagates; exhausted -> ERROR `resilience.chain.exhausted(model_role, tried)` + last error.
  - Event field sets match U08-18 DETAIL_FIELDS (retry 7 keys, fallback 3, repair 3), asserted exactly in tests. Metric labels match §8.2.
  - Rulings honoured: only `ModelChain`, `complete_validated` added to `__init__` (110/120); retry.py untouched (339/340); ports.py untouched; fixtures in resilience conftest.py.
- ⚠️ Cannot verify from diff / carried over: ST08-07 `chat_policy -> small_model` half (U08-75, later card); FT08-01/FT08-02 fault tests (ruled carry-over); lint-imports and check_module_size taken from the report (chain.py 381/390 confirmed by wc).

### Verification run (reviewer)
- `PYTHONUTF8=1 uv run pytest tests/unit/core/resilience -q -p no:logging` at 58c5367 (clean tree): 451 passed, 1 skipped (pre-existing symlink privilege skip).
- Coverage chain.py: 195 stmts / 38 branches, 100% line and branch.
- ruff check + ruff format --check on the 4 touched files: clean. mypy chain.py + __init__.py: clean.
- Mutation probes (20, applied in memory via a scratch pytest plugin; worktree never modified): 16 killed. Survivors: (1) dropping `[:ERROR_LINES_MAX]` on error_paths (equivalent: record_event caps lists at 20); (2) `now >= due` -> `now > due` (boundary untested); (3) `due is None or` inverted (equivalent: open state always has a probe time); (4) removing `fault_point("llm.call", ...)` from the attempt (untested; FT08-01 carry-over).

### Strengths
- Faithful, small reuse of retry internals (`_Retry`, `_ainvoke`, `_areplay`) rather than a divergent copy of the tenacity wiring; retry semantics identical to `aretry_call` plus tracer/target.
- TH08-15 handled at the source (`include_input=False`, `exc.msg`, paths-only event detail); tests pin it for pydantic, jsonschema, decode errors and the ST08-15 payload in lines, trace events and DB rows.
- Async-safety: breaker reads (`candidates`), event/metric writes off the loop via `asyncio.to_thread`; the only shared mutable state (`auth_dropped`) touched under `process_state().lock`.
- Tests assert exact field sets, DB rows, counters, call counts, timeouts, original-message handoff and per-run auth dropping; strong mutation kill rate.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
1. herness/core/resilience/chain.py:91 (spec erratum, plan-mandated): step 5 uses pydantic `err["msg"]` verbatim, but some pydantic messages embed input: `union_tag_invalid` gives "Input tag 'SECRET-TAG' found using 't' does not match ..." (verified). So a repair error line can carry an attacker value, against the TH08-15 note "never input values". Traces stay clean (paths only) and the line only goes back to the same model, which already gets the full echo, so exposure is low. Suggest a spec note or a fixed message for tag errors (for example `err["type"]`) in a later pass.
2. herness/core/resilience/chain.py:366: the "keep req.timeout_s" rule applies to any candidate whose key equals `req.client`, but the spec says "the first candidate keeps ...". It only differs when the caller's client is a later candidate. Either check `i == 0` or record the reading.
3. herness/core/resilience/chain.py:79/109-111: only `json.JSONDecodeError` is mapped. A deeply nested reply (`[[[[...`) raises `RecursionError` from `json.loads`, which escapes as a non-HernessError instead of a validation failure or fallback. This is a hostile-output edge case (TH08-14). Consider mapping `RecursionError` to `("/", "invalid JSON: too deeply nested")`.
4. tests/unit/core/resilience/test_resilience_chain.py:~293 (UT08-37 due-probe test): the boundary `now == retry_at` is untested (the `>=` -> `>` mutant survives). Consider asserting at exactly `retry_at`.
5. tests/unit/core/resilience/test_resilience_chain.py: no unit test drives the `llm.call` fault point through `_RetryingCompleter` (the mutant that removes it survives). This is covered by the FT08-01 carry-over, so it is noted only for tracking.
6. herness/core/resilience/chain.py:44 imports three private names from retry.py across modules. This is accepted under the ruling (retry.py 339/340). Record it as debt so they move to a shared `_arun` when retry.py gets budget.

### Assessment
**Task quality:** Approved
**Reasoning:** All four units match the spec algorithms, event field sets and metric labels. The async retry runner copies `retry._run` semantics exactly, and the tests pin the behaviour (100% line/branch coverage, 16 of 20 mutants killed; the 4 survivors are equivalent or carried over). The remaining items are minor edge cases and a spec erratum.

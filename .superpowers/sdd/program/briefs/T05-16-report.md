# T05-16 report — Tool registry and dispatch

Status: DONE_WITH_CONCERNS (see Concerns). Worktree agent-ade13e717ba891ac3, base 88a7f75.
Commit: 1f7ea17 feat(harness): tool registry and dispatch (T05-16) — all pre-commit hooks passed (incl. pytest-unit). No wip checkpoint landed: the first wip attempt was refused by detect-secrets (test variable named `secret`, renamed) and folded into this commit.

## Implemented
- `herness/harness/tools.py` (392/400): `TOOL_OWNERS` (read-only MappingProxyType, 14 names verbatim), `ToolRegistry` (register / names / resolve / tool_specs, threading.Lock), `tool_registry()`, private `_reset_tool_registry()` (test hook), `dispatch(ctx, tools, calls, hooks, state, /)`; re-exports `MAX_TOOL_CALLS_PER_MESSAGE` (16), `MAX_TOOL_ARGUMENT_CHARS` (32,000).
- NEW `herness/harness/_tools_dispatch.py` (192/250, ruling 2): pre-checks steps 1-7, TaskGroup + Semaphore(max_parallel) execution with `retry_call`/`aretry_call("tool_store")`, result fill, run_sql failure counters, `tool_call` trace via `ctx.tracer` + `tool_call_fields`, metrics `herness_harness_tool_calls_total{tool,ok}` / `herness_harness_tool_latency_seconds{tool}` (tool label = resolved name or "unknown": bounded cardinality).
- NEW `herness/harness/_tools_schema.py` (94/120): `check_tool_schema` (Draft202012 check_schema + R-26 strict rule at every object level: properties, items, prefixItems, anyOf/oneOf/allOf, $defs/definitions, not/if/then/else) and `schema_hint` (step 5). Second private sibling needed: registry+dispatch in tools.py was 439 lines. Both rows added to spec §2 module map next to `_tools_record.py` (same commit). Budgets come from the doc module map (check_module_size exit 0); pyproject has no per-module entry for private modules.
- `herness/harness/warehouse.py` (215/220) — FIX outside card files, see Concerns.
- Tests support: `tests/support/dispatch_standin.py` (SyncTool/AsyncFnTool spies, FakeWarehouse, make_tool_ctx, make_state, use_test_config), `tests/support/harness_state.py` (autouse `reset_harness_state`, registered in tests/conftest.py pytest_plugins; resets only when herness.harness.tools is already imported, so non-harness tests pay nothing). Existing fixtures untouched.
- Spec docs: 2 module-map rows; `.secrets.baseline` line-number shift of the existing docs/impl/05 entry (2648 -> 2650), LF kept, no entries dropped.

## Security points (ruling 3)
- Names: only `tools.get(call.name)` on the resolved mapping; no getattr/import from model data (ST05-02 also calls `execute_recorded` as a tool name -> rejected).
- Size cap before any schema validation/signature (ST05-16 spies schema_hint: never called for oversized/too-deep calls). Arguments are already parsed by the adapter (ToolCall.arguments); size = len(raw_arguments or canonical_json(arguments)). canonical_json SchemaViolation (depth > 64) is caught -> ToolInputError "tool arguments are not valid JSON values" (otherwise a FatalError would escape).
- Rejected blobs (too large / not canonical) are traced with `arguments={}` (args_hash then hashes `{}`) so 1 MB payloads never enter the tracer.
- Hint: `<json_path>: <message>` <= 300 chars; the offending value's repr is replaced by `<type value>` so no argument text is echoed (UT05-72 checks a 5,000-char probe value is absent).
- 16-call cap; ST05-10: 200 calls -> 16 executed, peak in-flight <= 4, 200 results in order, content capped 12,000 / truncated. LLM adapter MAX_TOOL_CALLS=64 untouched.
- FatalError: TaskGroup cancels siblings, the first FatalError leaf is re-raised unwrapped (UT05-76 async sibling sees CancelledError; unclassified RuntimeError -> classify -> FatalError propagates). Other HernessError -> error result.
- Sync tools called via `functools.partial(tool, ctx, **args)` passed to retry_call (equivalent to the spec's `retry_call("tool_store", tool, ctx, **arguments)`, but a model argument named `breaker_key` cannot collide with retry_call's keyword).

## Tests (IDs, underscore form)
UT05_68 x4, UT05_69 x4, UT05_70 x2, UT05_71 x2, UT05_72 x2, UT05_73, UT05_74, UT05_75 x2, UT05_76 x2, UT05_77 x2, UT05_78, UT05_79 x2, UT05_131 x3 (7 cases), ST05_02, ST05_07 x3, ST05_10, ST05_16, ST05_22 x2 (4 cases), BT05_02.
Files: tests/unit/harness/test_tools_registry.py, test_tools_dispatch.py, tests/security/test_st05_tools.py (unit marker), tests/bench/test_harness_dispatch_bench.py ([integration, slow]).
RED: `pytest tests/unit/harness/test_tools_registry.py tests/unit/harness/test_tools_dispatch.py` -> ImportError: cannot import name 'MAX_TOOL_CALLS_PER_MESSAGE' from 'herness.harness.tools' (2 collection errors).
RED (race): ST05-07 SELECT test failed "SQL guard: table ... core.big does not exist" before the warehouse fix; a 6-concurrent-SELECT probe failed 3/3 runs cold, passed with a warmed schema.
GREEN: `pytest tests/unit/harness tests/security/test_st05_tools.py tests/security/test_st05_warehouse.py tests/bench/test_harness_dispatch_bench.py` -> 1103 passed, 2 skipped (symlink privilege).
BT05-02: dispatch with 1 no-op sync tool, 1,000 calls (one call per dispatch, one event loop, 50 warm-up): mean 0.31-0.40 ms per call (< 5 ms).
Gates: ruff format/check clean, mypy (243 files) clean, lint-imports 13 kept, check_module_size 0, check_type_ownership 0.

## Deviations / rulings applied
- Ruling 1: `resolve(role, ...)` typed against local `_RoleLike` Protocol (name, allowed_tools); `dispatch` hooks typed as empty `_LoopHooksLike` Protocol (| None) and ignored.
- resolve returns `sorted(set(names))` (duplicates once) and additionally requires each task-tool key == tool.name and a strict schema for task tools (TH05-22 hardening).
- step 11 counts only EXECUTED run_sql QueryErrors (a call rejected at the cap in step 6 is not counted again). Rebuilt stop-hint result keeps the tool's message.
- Empty `calls` returns [] (spec precondition non-empty; no raise).
- Config in tests: the repo config/ is not loadable in tests (metrics.yaml empty), so dispatch tests cache the full test config (tests/support/config_tree.write_full_config); UT05-78 uses override `models.harness.tools.max_parallel=2`.

## Concerns
1. warehouse.py race (T05-13 module, outside card files): `DuckWarehouse.schema()` loaded the cold schema on the SHARED connection without a lock; under concurrent dispatch (sync tools in threads) the load raced into a partial schema, so valid run_sql calls failed with "table does not exist". Fixed minimally: lock + a dedicated short-lived cursor (`with self._con.cursor() as cur`), regression test ST05_07 `test_st05_07_concurrent_selects_on_cold_schema`. Not fixed: `table_comment()` still executes on the shared `self._con` and can race the same way under concurrency — flag for the T05-17 (describe_table) owner.
2. tools.py 392/400: little headroom for later cards.

## Carry-overs
- T05-19 RoleSpec: replace `_RoleLike` if desired (structural match should already hold).
- T05-22/23 LoopHooks: dispatch `hooks` param typed `_LoopHooksLike`; T05-23 calls dispatch.
- T05-17/18 warehouse tools not built: ST05-07 "spec 05 tools execute only SELECT" tested via a run_sql stand-in over execute_recorded + spy cursor through registry+dispatch; re-point to the real `RunSql`/tool classes when they land (static check of tool classes too).
- T08-05 metric sink: counters/histograms use record_counter/record_histogram directly (signatures fit).

## Fix round 1 (review T05-16-review.md)
- C1/I1: `_tools_dispatch.precheck` now vets EVERY call first (`_vet`: raw-text size check first, then canonical size, then signature; SchemaViolation caught). Any call whose arguments are too large or not canonicalisable is traced with `arguments={}` — including calls 17+ and unknown names — so neither a depth>64 SchemaViolation nor a 1 MB blob reaches `tool_call_fields`/the tracer. Error order unchanged (cap, name, then size/JSON). RED: new test on HEAD 1f7ea17 -> 2 failed "SchemaViolation: canonical JSON too deep"; GREEN after fix.
  New test: ST05_16 `test_st05_16_blobs_on_rejected_calls_never_raise_or_trace[beyond_cap|unknown_name]` (1 MB raw, 1 MB parsed, depth-75 args on call 17+ and on `shell`: ToolInputError results, no exception, traced payload `{"args": {}}`, each traced event < 2,000 chars).
- I2: `DuckWarehouse.table_comment()` uses its own short-lived cursor (`with self._con.cursor() as cur`); new UT05_49 `test_ut05_49_concurrent_schema_and_table_comment` (8 threads x 20 calls on a cold handle). warehouse.py 216/220.
- M1: `schema_hint` masks name-echoing validators (additionalProperties, unevaluatedProperties, propertyNames): hint = "<path>: <validator> failed; property names not shown". New UT05_72 `test_ut05_72_extra_property_names_not_echoed`.
- M5: ST05-07 cold-schema test docstring states it is a T05-13 warehouse regression guard.
- Line counts: tools.py 392/400, _tools_dispatch.py 193/250, _tools_schema.py 97/120, warehouse.py 216/220.
- GREEN: tests/unit/harness + test_st05_tools + test_st05_warehouse + BT05-02 -> 1107 passed, 2 skipped; BT05-02 mean 0.39 ms. Gates: ruff format/check, mypy (243), lint-imports (13 kept), module size 0, type ownership 0.
- Commit: b4d1054 fix(harness): vet tool arguments on every rejected call (T05-16); all hooks passed.

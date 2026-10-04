# T06-13 report: Routing, hooks and tool context

Status: DONE_WITH_CONCERNS (spec notes below; no behaviour contradicting the brief)
Branch: worktree-agent-a91fdf51a82d3b12e (base 10f9733). Final commit: see bottom.

## Units done
- U06-139 `RunEnv` (dataclass, slots, kw_only) - `herness/harness/swarm/_routing_ctx.py`, re-exported by `routing`.
- U06-96 `map_agent_result` - `routing.py`.
- U06-98 `Route` (frozen) + `route_task` + `BASE_MODEL_ROLE` - `routing.py`.
- U06-99 `build_hooks`, U06-100 `build_tool_context` - `_routing_ctx.py`, re-exported by `routing`.
- U06-141 `build_task_input` - `herness/harness/swarm/_task_input.py`, re-exported by `routing`.

## Files
- herness/harness/swarm/routing.py (226 lines, budget 330)
- herness/harness/swarm/_routing_ctx.py (new, 179 lines; new §2 row, budget 200)
- herness/harness/swarm/_task_input.py (new, 109 lines; new §2 row, budget 130)
- herness/harness/swarm/__init__.py (20 lines, budget 20) - C2 lazy re-exports
- docs/impl/06-swarm-and-pipelines.impl.md - two §2 module-map rows for the private siblings
- tests/unit/harness/swarm/test_swarm_routing_ctx.py (new): UT06-63, UT06-65, UT06-66, UT06-67, UT06-92, RF re-export test
- tests/unit/harness/swarm/test_swarm_routing.py: UT06-68 stand-in parity test (C1 guard)
- tests/security/test_st06_task_input.py (new): ST06-19 (real run_agent + HarnessHooks, recording ListClient)

## Evidence
- RED: `pytest tests/unit/harness/swarm/test_swarm_routing_ctx.py` -> ImportError: cannot import name 'Route' from routing (before implementation).
- GREEN: `pytest tests/unit/harness/swarm tests/unit/harness/pipelines tests/security/test_st06_task_input.py` -> 298 passed.
- Coverage (card tests only, branch): _routing_ctx 100 %, _task_input 100 % after tuple test, __init__ 100 %; routing.py remainder covered by UT06-64/68 in test_swarm_routing.py.
- Gates: ruff format/check clean; mypy (384 files) 0 issues; lint-imports 15 kept / 0 broken; check_module_size exit 0; check_type_ownership exit 0; detect-secrets clean on touched files.

## Deviations / spec notes (with why)
1. RunEnv gains `warehouses: WarehousePool`, `ops: OpsHandle`, `vectors: VectorHandle`. U06-100 reads `env.warehouses` and needs `ops`/`vectors` for ToolContext, but U06-139's field list omits them. Spec 05 `WarehousePool` has `get(build_id)`, not `handle(...)` - used `get`.
2. `MetricSink` has no owner type in the tree: private Protocol `_MetricSink` in `_routing_ctx.py` with `# T06-13:` marker (same shape as blackboard's `_MetricSink`). Carry-over: replace when spec 08 publishes MetricSink.
3. `sql_limits` built from `hcfg.models.harness.sql` (return_rows, scan_rows, timeout_s[run.depth]); max_attempts_per_query keeps the SqlLimits default 3 (no config key exists).
4. Off-network detection = spec rule OR `config.off_network` flag (safe side only, matches spec 05 `_is_off_network`). Spec 05 config validation already rejects non-loopback hosts without the flag, so the host branch is defence in depth (tested with model_construct).
5. `route_task` ConfigError -> BASE_MODEL_ROLE fallback implemented literally, but spec 05 `LLMRegistry.model_for`/`chain_for` already apply `BASE_ROLE`, so with the real registry a missing `skeptic_final` routes to the skeptic's key while `model_role` stays `skeptic_final` (egress purpose stays `reasoning_final`). UT06-65 tests both: real registry (key falls back) and a strict registry (model_role becomes base).
6. `build_tool_context` also drops `task_tools` entries whose name is not in the final `tool_names` (least privilege; dispatch would reject them anyway). `run.build_id is None` -> ConfigError. Tracer passed with `cast("TraceEmitter", ...)` (Tracer.emit's extra typed kwargs make mypy reject the protocol structurally).
7. `build_task_input`: `claim` is wrapped wherever it occurs (not only under `finding`/`findings[*]`), safer and equivalent for current payloads. Record ids come from the nearest enclosing mapping (`task_id`; `finding_id` or `finding.finding_id`; `message_id`), inherited by nested values. Roles with rules: planner, analyst, skeptic, writer, chat; all field rules apply for every known role (wrapping more is harmless; role only gates unknown roles). judge/verifier -> ConfigError per spec. Tuples are kept as tuples. Return type `dict[str, object]` (callers cast for run_agent's dict[str, JsonValue]).
8. Split: routing.py would have been 394 lines (> 330), so RunEnv/tool context/hooks went to `_routing_ctx.py` and build_task_input to `_task_input.py`; §2 rows added in the same commit. `_routing_ctx` imports `routing` only under TYPE_CHECKING (no cycle).
9. Memory: `MemoryStore` imported from the `herness.harness.memory` facade under TYPE_CHECKING only (sub-controller ruling); lint-imports unchanged at 15/0.

## Carry-overs
- C1 NOT closed (illegal): spec 06 §2 import-direction table puts `herness.harness.pipelines.*` at rank 3 below `herness.harness.swarm` (rank 2); rank 3 may not import rank 2, so `_review_common` cannot import `swarm.routing.default_tools`. Stand-in left in place; UT06-51 unchanged. Added a UT06-68 parity test that fails if the stand-in drifts from `routing.default_tools`. Suggest the spec drop "until T06-13" from the `_review_common` row, or have the swarm inject the tool list (spec owner ruling).
- C2 closed: `swarm/__init__.py` lazily re-exports `RunRequest`, `RunResult` (from lifecycle) via module `__getattr__` (no heavy import, no cycle); `Swarm`, `review_job_handler` come with run.py/handler.py. No routing names added.
- New: `_MetricSink` stand-in (spec 08 MetricSink) - see note 2.
- T06-09 parked M4 (spawn broker sync store reads inside its lock): noted, unchanged.

## Concerns
- Commit hooks run the full unit + cpu suite (~10+ min per commit); one checkpoint attempt was rejected by detect-secrets (`client_key == "..."` literals flagged), fixed by asserting via `client.name`/tuples.

## Final commit
1d4b2eb feat(swarm): T06-13 routing, hooks and tool context. All pre-commit hooks passed (ruff, mypy, import-linter, detect-secrets, module-size, type-ownership, pytest-unit). Single commit: the earlier wip checkpoint was rejected by detect-secrets and never landed.

## Review round 1 (fix commit da65120)
- M1: duplicated asserts replaced: first UT06-65 test now locks `model_role == "skeptic_final"` (real registry resolves the base role itself); the hybrid fallback test asserts `role_spec.name == "skeptic"`.
- M5: new `test_ut06_65_hybrid_without_skeptic_final_cost_cap_reached` (brief setup in one test): models.yaml without skeptic_final, skeptic routed to claude-opus with chain [claude-opus, local-small-cpu], hybrid, cost cap reached -> local-small-cpu with fallback_local=True; a strict registry variant shows model_role falling back to `skeptic`.
- M3: §2 routing row deps now list `herness.harness.roles` and `herness.harness.pipelines.settings`; `_routing_ctx` row lists runtime deps and type-only deps; `_task_input` row already correct.
- M2: `_review_common.default_tools` marker reworded (stand-in stays under the §2 import-rank rule, guarded by UT06-68, needs an owner ruling); §2 `_review_common` row says the same (base_priority still "until T06-15").
- Parked untouched: M4, M6, M7.
- Tests: 299 passed (swarm + pipelines + ST06-19); ruff, mypy, lint-imports 15/0, module size, type ownership, detect-secrets clean; all commit hooks passed.

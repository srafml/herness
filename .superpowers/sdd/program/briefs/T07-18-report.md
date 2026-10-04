# T07-18 report: Outcome job (impl 07 U07-86, U07-87)

Worktree: D:\herness\.claude\worktrees\agent-a2d179ae8d924c195 (branch worktree-agent-a2d179ae8d924c195, base e8634a0).
Status: DONE. Checkpoint 6bc0fbb (wip, all code/tests/spec note; all pre-commit hooks passed incl. pytest-unit); final commit = empty feat(memory) commit naming the card on top of it.

## What was built
- `herness/harness/memory/outcome.py` (328/330 lines): `outcome_measure_handler(ctx)` (U07-86), `measure_recommendation(due, *, con, catalog, deps, now)` (U07-87), the frozen `OutcomeDeps` and the module seam `configure_outcome(deps | None)` / `outcome_deps()` (unset -> `ConfigError`), as T07-22's maintenance seam.
- `herness/harness/memory/outcome_stats.py` (179/240): T07-17 carry-over resolvers `metric_weeks(cfg, metric) -> MetricWeeks` and `due_weeks(cfg) -> (per_metric_pairs, default_pair)` (the `due_measurements` week pairs from U07-83 `Windows.due`).
- `docs/impl/07-memory.impl.md`: T07-18 spec note under U07-87 (OutcomeDeps, seam, readings 1-10) and the §2 row of outcome_stats.py lists the two new exports.

## Tests (all IDs in names + docstrings, module pytestmark)
- tests/unit/harness/memory/test_memory_outcome.py (unit): UT07-74 x14, UT07-87 x8 (+12 parametrized bad payloads) -> 35 tests.
- tests/integration/harness/test_memory_outcome_it.py (integration): IT07-05 x2 (planted 20 % MTTR improvement vs flat peers -> paid_off; same in all peers -> no_effect; reruns write no row).
- tests/security/test_st07_outcome.py (integration, existing ST convention): ST07-18 x3.
- tests/fault/harness/test_memory_outcome_fault.py (fault): FT07-04 x2 via `fault_env` rule `{"point": "sql.query", "action": "error:QueryError", "kind": "outcome_measure"}` (count 1, and nth 2 for a failure at the second pair).
- tests/unit/harness/memory/_outcome_env.py: helpers (planted warehouse on the metrics_tiny DDL + real materialize_facts, real compute_metric/peer_group, real MemoryWriter, closed-loop seeds, FakeCtx).
- Result: card tests 42 passed; `pytest tests/unit/harness/memory tests/fault/harness + card IT/ST + ST07 episodic` 751 passed.
- Coverage (card tests + outcome_stats tests): outcome.py 100 % line / 100 % branch (200 stmts, 40 branches); outcome_stats.py 98 % (line 179 pre-existing).

## TH07-18 under mutation (RED evidence for the guards)
Script C:\Users\santh\AppData\Local\Temp\w29-s07\t0718_mutate.py applied each mutation to outcome.py, ran the 4 card test files, restored:
- exclusion removed (`- excluded` dropped): 3 failed (UT07-74 treated peer, ST07-18 x2).
- min_peers check removed: 3 failed (UT07-74 x2, ST07-18 fallback).
- outcome_exists check removed: 1 failed (UT07-74 existing outcome -> no queries).
- spec 04 prior_year fallback ignored: 1 failed (UT07-74 fallback honoured).
Scope: rejected / deferred / metric-less recs get no outcome or summary (ST07-18, UT07-87 empty sweep); every summary passes `find_uncited_numerals` and cites an evidence query.

## Gates
ruff format/check clean; `uv run mypy` 0 issues (374 files); mypy --explicit-package-bases on the 5 test files clean; lint-imports 13 kept; check_type_ownership 0; check_module_size 0.

## Rulings / spec readings (recorded in the T07-18 spec note)
1. OutcomeDeps seam (R-42 pattern of T07-22).
2. Catalog = `catalog_from_config()` (same catalog compute_metric/peer_group read) instead of `load_catalog(path)`.
3. Week resolvers live in outcome_stats (budget room).
4. Warehouse opened only when a pair is due (empty sweep / not-due single never opens it); closed in finally.
5. peer_group evidence persisted via `on_evidence` -> `details.peer_query_id` always resolves. Work item with NULL `core.work_item.service_id`: outcome with method `none`, inconclusive, query_id = peer group query id.
6. TH07-18 exclusion by target_id regardless of target_type (conservative). `details.peer_ids` = peers used (empty for prior_year).
7. ToolInputError from peer_group/compute_metric (unknown target etc.) -> `memory.outcome.skipped` reason=invalid_request, counted as skipped (does not fail the job forever); QueryError / RetryableError / StoreBusy propagate.
8. New fault call site `fault_point("sql.query", kind="outcome_measure")` before each series query (FT07-04 injection point; no new shim).
9. Summary names the target generically (`a <type> target`) when target_id holds an uncited numeral (T07-16 pattern); lost insert race (row appears after outcome_exists) writes no summary and returns None.
10. tiny_build absent: tests plant their own warehouse.

## Carry-overs for T07-23 (composition root)
- Build `OutcomeDeps(writer=<MemoryWriter>, outcome=cfg.memory.outcome, allowed=<writer's allowed numeral patterns>)`; the other fields have production defaults: `config_hash` (config_hash(get_config())), `open_current` (warehouse.open_readonly), `load_catalog` (catalog_from_config), `record_evidence` (ops.record_evidence).
- Call `herness.harness.memory.outcome.configure_outcome(deps)`.
- Register `herness.harness.memory.outcome.outcome_measure_handler` for job kind `outcome_measure` (gpu_class none) with herness.core.jobs.handlers.register_handler.
- episodic.py keeps its private `_weeks`; it can switch to `outcome_stats.metric_weeks` (identical semantics) when next touched.

## Concerns
- outcome.py is at 328/330 lines (budget tight, no ruling needed).
- Spec 11 `tiny_build` absent (as for other cards); acceptance shown on a planted warehouse.

## Fix round 1 (review T07-18-review.md, Approved with Minor; M1-M3 fixed, M4-M6 left to spec notes as instructed)
- M1 (done; outcome.py now 327/330, no split, no budget change): the U07-33 `treated_targets` read moved out of `_measure` into `measure_recommendation`, before the `try`; the excluded ids travel in `_Pair.excluded` (frozenset). Only `_measure` (spec 04 `peer_group` / `compute_metric`, `_owner` DuckDB read, evidence persistence) stays inside the `except ToolInputError` skip. None of these besides spec 04 raises ToolInputError. A ToolInputError from ops now propagates. New test `test_ut07_74_ops_tool_input_error_propagates`. Probe: moving the read back inside the try -> 1 failed (that test).
- M2: new `test_ut07_74_control_is_the_weekly_peer_median` with heterogeneous peers. Pre values are 9/10/11 and post values 10/14/9, so the weekly median is 10 in both windows and the pinned result is did = -2, rel 0.2. Using any single peer or the mean would give a different did. Probes: first peer only -> 1 failed; mean instead of median -> 1 failed.
- M3: `_outcome_env.open_current` records each handed-out connection, and `all_closed(env)` asserts every one is closed (a closed DuckDB cursor raises ConnectionException). This is asserted on the done path (UT07-87 sweep) and the error path (FT07-04 QueryError). Probe: removing `con.close()` -> 2 failed.
- Tests: card files 44 passed; memory unit + harness fault + card IT/ST 752 passed; outcome.py coverage 100 % line / 100 % branch. ruff, mypy (repo + test files), lint-imports (13 kept), check_module_size all clean.

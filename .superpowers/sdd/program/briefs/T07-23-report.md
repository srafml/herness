# T07-23 report: MemoryStore facade and composition root (U07-97, U07-98)

Worktree: D:\herness\.claude\worktrees\agent-a2d179ae8d924c195 (branch worktree-agent-a2d179ae8d924c195), base 869a2d9.
Commits: 474d8a3 wip(T07-23): move NUMBER_REF_SCHEMA to spec 05 and forbid memory -> swarm/pipelines/eval; final b22bddc feat(memory): add MemoryStore facade and composition root (T07-23) (all pre-commit hooks passed, tree clean).

## What was built
- `herness/harness/memory/__init__.py` (87/330): cheap, cycle-free package: lazy PEP 562 re-export of `MemoryStore`, `HealthResult` (the `herness.core.jobs` pattern); `get_memory_store()` (module `threading.Lock`, `MemoryStore.from_config(get_config())`, `_State.store` the only module state); `register_memory_components(tool_registry, store)`; `_reset_memory_store()` (also clears the outcome/maintenance seams of loaded modules).
- `herness/harness/memory/_facade.py` (272/300, NEW private sibling, §2 row): `MemoryStore` (from_config, the U07-97 constructor, all design 07 §3.3 methods + recall_with_status + health), `HealthResult`, `PENDING_EMBEDDING_MAX`.
- `herness/harness/memory/_compose.py` (175/200, NEW private sibling, §2 row): `CurrentGuard` (SqlGuard over the CURRENT build schema, empty schemas dropped, rebuilt when CURRENT moves, allows nothing without a readable build), `ScratchpadOps` (CompactorOps: U07-29 + save_checkpoint "scratchpad"), `NoModels`, `drafts` (dict -> RecommendationDraft; ReportContractError "recommendation rank <n> invalid: <field>"), `run_bound` (bind_ids run_id), `related` (U07-61 on CURRENT), `current_config_hash`.
- C4 move: `NUMBER_REF_SCHEMA` -> `herness/harness/_tools_schema.py` (118/120), re-exported by `herness/harness/tools.py` (384/400); swarm/tools.py (310/330) and memory/_tools_args.py import it from `herness.harness.tools`. JSON text byte-identical (md5 of old vs new json.dumps; POST_FINDING_SCHEMA identical).
- pyproject.toml: import-linter contract "memory-no-callers (T07-23)": source herness.harness.memory; forbidden herness.harness.swarm, .blackboard, .pipelines, herness.eval; indirect imports checked, only the config-root settings exception ignored. KEPT. Probe: re-adding the old swarm import -> BROKEN (reverted).
- tests/support/harness_state.py: autouse `reset_harness_state` also calls `herness.harness.memory._reset_memory_store()` when the package is loaded (the spec 11 fixture of U07-98). tests/conftest.py untouched.
- tests/support/memory_kill.py (NEW): FT07-01 child process (T05-27 loop_kill pattern).

## Carry-over checklist
| # | Status | Evidence |
|---|--------|----------|
| C1 MemoryStore satisfies MemoryToolStore | closed | __init__.py:74 typed call `register_memory_tools(tool_registry, store)` with `store: MemoryStore` passes mypy --strict; test_ut07_85_satisfies_tool_store_protocol (signature parity + typed assignment) |
| C2 tools + outcome_measure + memory_maintenance, idempotent | closed | __init__.py:74-78 (register_handler from herness.core.jobs.handlers); test_ut07_48_register_components_twice (two tools once, both handlers resolve to the unit handlers, seams set), test_ut07_48_seams_follow_latest_store, test_ut07_48_foreign_handler_conflict |
| C3 slug redactor agrees with store redactor | closed | _facade.py:86 from_config uses get_redactor() (the process redactor the tool slugs with); test_ut07_85_from_config_builds_per_u07_98 |
| C4 NUMBER_REF_SCHEMA move + lint contract | closed | _tools_schema.py:48, swarm/tools.py:32, _tools_args.py import; pyproject.toml:551; lint-imports 14 kept 0 broken; spec notes impl 05 (§2 rows + module note), 06 (§2 row), 07 (§2 row + T07-11 note (3)) |
| C5 RecommendDeps | closed | _facade.py:193-200: dict drafts converted (_compose.drafts), priors read once per call, adjust = U07-80 bound to priors / Embedder.embed / relatedness / cfg.feedback / now, content_max = cfg.write.max_content_chars, run_id bound (bind_ids) so memory.feedback.degraded carries it; tests test_ut07_85_write_recommendations_* |
| C6 EpisodicDeps, prior_context/decide | closed | _facade.py:121; enqueue already via herness.core.jobs.queue.enqueue (episodic.py:21, :249); delegation tests |
| C7 ProceduralDeps | closed | _facade.py:123-124 (CurrentGuard, the writer's scanner); CurrentGuard tests (current build, rebuild on move, no / unreadable build, blocked columns = harness.sql.blocked_columns) |
| C8 LoraDeps | closed | _facade.py:240 per call: export_root <paths.data>/models/lora_data, config_hash(get_config()), memory Embedder, write-path InjectionScanner, cfg.procedural.lora; open_warehouse / metric_names keep defaults (warehouse.open_readonly, load_catalog().names()); test_ut07_85_export_lora |
| C9 ChatDeps | closed | _facade.py:126 (built in ctor: missing prompt -> ConfigError there, U07-99); NoModels stand-in when llms is None; delegation tests |
| C10 MaintenanceDeps, register, IT07-10, FT07-01 | closed | _facade.py:127, __init__.py:76/78; IT07-10 tests/integration/harness/test_memory_maintenance_it.py (missing vector, stale hash, orphan vector, TH07-13 status desync; repaired through the registered handler; rejected item never recalled); FT07-01 real kill: test_ft07_01_process_killed_after_commit_then_maintenance_backfills (child killed by fault plan `kill` on sqlite.write kind memory_embedding_flag, after the commit and a failed upsert; maintenance flags + backfills). loop_kill.py itself runs only a loop task and cannot reach the memory write path, so the same pattern got its own child (spec note (13)) |
| C11 MemoryLifecycle wired | closed | _facade.py:116; approve/reject/expire/expire_item/record_use delegation tests + real end-to-end test; purge NOT built (ruling: T07-26) |
| C12/C15 OutcomeDeps + configure_outcome | closed | _facade.py:129 OutcomeDeps(writer, cfg.outcome, allowed) with T07-18 defaults; __init__.py:75; outcome.py untouched (327/330) |
| C13 singleton, cheap and cycle-free import, budget | closed | __init__.py:52; tests: singleton from get_config, 8 concurrent first calls -> 1 build, ConfigError not cached, reset clears seams; subprocess: package import loads none of lancedb/torch/sentence_transformers/duckdb/anthropic/openai/pyarrow/sqlglot; tools/outcome/maintenance/core.config/settings/_facade imported first then the package (and package then core.config) all work |
| C14 owning submodules for register_handler / enqueue | closed | __init__.py imports herness.core.jobs.handlers.register_handler; _compose imports herness.core.jobs.tasks.save_checkpoint; episodic uses herness.core.jobs.queue |

## Files and sizes (lines / budget)
__init__.py 87/330; _facade.py 272/300 (new §2 row); _compose.py 175/200 (new §2 row); _tools_schema.py 118/120; tools.py 384/400; swarm/tools.py 310/330. `uv run python -m tools.check_module_size` exit 0.

## Tests
- New: tests/unit/harness/memory/test_memory_facade.py (UT07-85, UT07-48; incl. subprocess import tests), tests/integration/harness/test_memory_maintenance_it.py (IT07-10), FT07-01 kill case in tests/fault/harness/test_memory_maintenance_fault.py.
- Card files: 59 passed. Touched packages (tests/unit/harness, tests/fault/harness, tests/security/test_st07_*, memory ITs, tests/unit/repo): 2509 passed, 1 skipped.
- Coverage (card tests, branch): __init__.py 98% (0 missed lines; 1 partial branch = "seam module not loaded"), _compose.py 100%, _facade.py 100%.
- Gates: ruff format / check clean; mypy --strict 0 errors (376 files); lint-imports 14 kept, 0 broken; check_module_size 0; check_type_ownership 0.

## Rulings
- R1 `MemoryStore` lives in the private sibling `_facade.py`, lazily re-exported by `__init__.py`; adapters in `_compose.py` (§2 rows + spec note in the same commit). Why: herness.core.config imports herness.harness.memory.settings, which runs the package __init__ inside the config import, so __init__ can import nothing from herness at module level, and the class needs lancedb/duckdb-backed collaborators.
- R2 purge (U07-100) omitted (card T07-26), per the group ruling.
- R3 The handler seams are configured by register_memory_components (not the constructor), so constructing a store has no process-wide side effect; the frozen deps are exposed as `MemoryStore.outcome_deps` / `.maintenance_deps`.

## Spec notes
docs/impl/07-memory.impl.md "T07-23 spec note" after U07-98 (items 1-13), §2 rows (__init__, _facade, _compose, _tools_args); docs/impl/05-harness-core.impl.md §2 rows (tools.py, _tools_schema.py) + import-rule note; docs/impl/06-swarm-and-pipelines.impl.md §2 row (swarm/tools.py). Deviations recorded: from_config passes egress_enabled=cfg.security.egress.enabled to LLMRegistry and data_root=cfg.paths.data (actual field name); write_recommendations accepts dict drafts; HealthResult is a frozen dataclass; ensure_table failure logs memory.recall.degraded.

## Cross-card edits
- herness/harness/_tools_schema.py, herness/harness/tools.py (impl 05): NUMBER_REF_SCHEMA defined / re-exported.
- herness/harness/swarm/tools.py (impl 06): imports NUMBER_REF_SCHEMA from herness.harness.tools (definition removed).
- herness/harness/memory/_tools_args.py, tests/unit/harness/memory/test_memory_tools.py (T07-11): import line.
- tests/fault/harness/test_memory_maintenance_fault.py (T07-22): new FT07-01 kill case + docstring.
- tests/support/harness_state.py (T05-16 fixture): memory reset.
- .secrets.baseline: line-number shift of an existing docs/impl/05 entry (hook regen).

## Concerns
- The shipped `local` profile with egress disabled makes `LLMRegistry` raise ConfigError (off-network client claude-opus in the routing), so `get_memory_store()` raises there, as U07-98 Errors prescribes. Owner: impl 05/10 config, or a ruling to fall back to llms=None.
- The pre-commit unit hook takes ~7.5 min per commit. test_st06_05_pii_checked_before_marker_validation (tests/unit/harness/test_blackboard.py) flaked once (a random ULID contained "415"); unrelated, passed on retry; determinism follow-up for impl 06.

## Fix round 1 (review briefs/T07-23-review.md: Approved with Minor; R1, R2 accepted)
- m1 (test_ut07_85_write_recommendations_converts_and_binds): the store is now built with a non-default `write.max_content_chars = 1500` and the test asserts `deps.content_max == 1500`. It also asserts that both adjustments get the exact list returned by the priors read (`kwargs["priors"] is measured`, and the `(draft, base)` arguments too). This kills the "drop content_max" and "drop priors" mutants.
- m2: the recall delegation cases now pass a non-None `filters` ("flt") and `run_ctx` ("run-ctx"), for both `recall_with_status` (parametrized table) and `recall`, and assert that both reach `MemoryRecaller.recall`. The other run_ctx-bearing delegations, `propose` ("ctx") and `prior_context` ("ctx"), already passed non-None values.
- m3 (__init__.py `register_memory_components`): registration is now atomic. Both kinds are checked first with `resolve_handler`, and a different handler raises `ConfigError("handler already registered for <kind>")` before anything changes. Only then are the handlers, the tools and the seams registered. New test `test_ut07_48_conflict_registers_nothing[outcome_measure|memory_maintenance]` checks that after a conflict there are no tools, the other kind has no handler, the foreign handler is kept, and both seams are unset.
- m4: the tests/conftest.py docstring now says that `reset_harness_state` also resets the process `MemoryStore` and its outcome/maintenance seams. This is a comment-only edit in tests/conftest.py.
- m5: the spec note (5) now says the priors read runs outside `run_bound`, so a failure log of that read has no `run_id`. Spec note (3) also records the new check-then-register order.
- Sizes: __init__.py 103/330 (was 87), _facade.py 272/300, _compose.py 175/200, _tools_schema.py 118/120, tools.py 384/400, swarm/tools.py 310/330.
- Tests: the card files give 61 passed (was 59). Memory unit tests, tests/fault/harness, test_st07_* and the memory ITs give 983 passed. Coverage: __init__.py 98% (no missed lines; 1 partial branch, the "seam module not loaded" arm), _compose.py 100%, _facade.py 100%.
- Gates: ruff clean, mypy --strict 0 errors, lint-imports 14 kept 0 broken, check_module_size 0, check_type_ownership 0.

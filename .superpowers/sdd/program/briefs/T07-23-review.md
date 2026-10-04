# T07-23 review: MemoryStore facade and composition root (U07-97, U07-98)

Verifier (opus). Worktree D:\herness\.claude\worktrees\agent-a2d179ae8d924c195, base 869a2d9, head b22bddc (474d8a3 wip + b22bddc). `git status` clean at report time (all 32 probes reverted with `git checkout -- <file>`).

**Verdict: Approved** (0 Critical, 0 Important, 6 Minor).

## Gates (TMP/TEMP = C:\Users\santh\AppData\Local\Temp\w29-s07, PYTHONUTF8=1)
| Gate | Result |
|---|---|
| Card tests (test_memory_facade.py, IT07-10, FT07-01 file) | 59 passed (+ test_memory_tools: 109 total) |
| Coverage, card tests, branch | `__init__.py` 98% (0 missed lines; partial 86->85 = seam module not loaded), `_facade.py` 100%, `_compose.py` 100% (>= 90/85) |
| tests/unit/harness + tests/fault/harness + tests/security/test_st07_* + IT07-10 | 2496 passed, 1 skipped (208 s) |
| ruff check / ruff format --check | clean / 1048 files formatted |
| mypy (strict) | 0 issues, 376 files |
| lint-imports | 14 kept, 0 broken (incl. "memory-no-callers (T07-23)") |
| check_type_ownership / check_module_size | 0 / 0 |

## Carry-over checklist
| # | Status | Evidence / how verified |
|---|---|---|
| C1 MemoryStore satisfies MemoryToolStore | ✅ | `__init__.py:74` typed `register_memory_tools(tool_registry, store)` with `store: MemoryStore`; mypy strict 0 errors; signatures `_facade.py:145-156,175` match the Protocol (test_ut07_85_satisfies_tool_store_protocol compares parameter names and defaults). |
| C2 register_memory_components: tools + outcome_measure + memory_maintenance, idempotent | ✅ | `__init__.py:65-78`; test_ut07_48_register_components_twice (two tools once, same objects, both handlers resolve, seams set). Probes R2a/R2b/R2c (drop handler / seam / tools) all KILLED. |
| C3 slug redactor = store redactor | ✅ | `_facade.py:86` `redactor=get_redactor()`, the same process redactor `_tools_args.slug` uses (T07-11 note (7)); probe C3 KILLED. |
| C4 NUMBER_REF_SCHEMA moved; contract | ✅ | Defined `_tools_schema.py:139-151`, re-exported `tools.py:46,56`; `swarm/tools.py:32` and `memory/_tools_args.py:23` import from `herness.harness.tools`; no memory -> swarm import remains. Contract `pyproject.toml:550-566` KEPT. Probes: re-adding the old swarm import (C4e), a direct swarm.spawn import (C4a), herness.eval (C4b), pipelines (C4c), blackboard (C4d) and a function-local swarm import in `__init__` (C4f) all BROKEN the contract. The only ignored edges are the config-root `-> **.settings` exceptions. |
| C5 RecommendDeps | ✅ (test gap m1) | `_facade.py:189-200`: `cp.drafts` dict conversion (`_compose.py:134-153`), priors read once, `partial(self._adjust, priors)` binds U07-80 to embed/related/cfg.feedback/now, `content_max = cfg.write.max_content_chars`, `run_bound(run_id)`. Probes C5b (no binding) and D1 (rank fallback) KILLED. C5 (content_max dropped) and C5c (priors not forwarded) SURVIVED, see m1. The code is correct by reading. |
| C6 EpisodicDeps; enqueue from herness.core.jobs.queue | ✅ | `_facade.py:121-122` field order matches `episodic.EpisodicDeps` (conn_factory, writer, redactor, allowed = the writer's compiled patterns `_facade.py:106`, episodic, outcome); `episodic.py:21,249` `job_queue.enqueue`. Delegation tests for prior_context/decide pass deps. |
| C7 ProceduralDeps | ✅ | `_facade.py:123-125` (CurrentGuard, the writer's scanner); `_compose.py:62-100` CurrentGuard; probes C7 (fresh scanner) and C7b (never rebuild on CURRENT move) KILLED. |
| C8 LoraDeps | ✅ | `_facade.py:115,240-243`: export_root `<data_root>/models/lora_data`, `current_config_hash()`, memory Embedder, the writer's scanner, `cfg.procedural.lora`; the defaults open_readonly / catalog names are kept. Probe C8 KILLED. |
| C9 ChatDeps | ✅ | `_facade.py:126` (`llms or cp.NoModels()`), built in the ctor so a missing prompt is a ConfigError at construction; probe C9 KILLED. |
| C10 MaintenanceDeps, register, IT07-10, FT07-01 | ✅ | `_facade.py:127-128`, `__init__.py:76,78`; IT07-10 `tests/integration/harness/test_memory_maintenance_it.py:38` passes (probe R2d: seam not configured makes IT07-10 fail). FT07-01: `tests/fault/harness/test_memory_maintenance_fault.py:136` real process kill via the new `tests/support/memory_kill.py`. **Ruling: acceptable.** `loop_kill.py` only drives a loop task and cannot reach the memory write path. memory_kill.py follows the same child pattern (fault plan `kill`, survival marker, exit-code assertion), and this case is stronger than the row asks: the kill lands before the pending flag and maintenance still flags and backfills. The "item has embedding_pending" half stays covered by the existing non-kill FT07-01 cases. |
| C11 MemoryLifecycle wired; purge omitted | ✅ | `_facade.py:116-117,158-177` (approve/reject/expire/expire_item/record_use, one call each; parametrized delegation test plus a real end-to-end test). purge is absent per the group ruling (T07-26); spec note (12). |
| C12/C15 OutcomeDeps + configure_outcome + outcome_measure | ✅ | `_facade.py:129` `OutcomeDeps(writer, cfg.outcome, allowed)` (T07-18 defaults); `__init__.py:75,77`; outcome.py untouched. |
| C13 singleton, reset, cheap and cycle-free import | ✅ | `__init__.py:52-62` lock + `_State.store`; `_reset_memory_store` `__init__.py:81-87`; `tests/support/harness_state.py:28-42` calls it before and after each test (plugin registered at `tests/conftest.py:35`). Probes G1 (no cache), G2 (reset keeps store), G3 (seams not cleared) and G4 (lock removed, 8 concurrent builds) all KILLED. Own subprocess check: `import herness.harness.memory` takes 0.04 s and loads 3 herness modules and none of lancedb/torch/sentence_transformers/duckdb/anthropic/openai/httpx/pyarrow/sqlglot/jsonschema. These import orders all import and resolve `MemoryStore`: config->pkg, pkg->config, tools->pkg, outcome->pkg, maintenance->pkg, settings->pkg, _facade alone, harness.tools->_facade, swarm.tools->_facade. An unknown attribute raises AttributeError. lint-imports 0 broken. `__init__.py` 87/330. |
| C14 owning submodules | ✅ | `__init__.py:70` `herness.core.jobs.handlers.register_handler`; `_compose.py:23` `herness.core.jobs.tasks.save_checkpoint`; episodic uses `herness.core.jobs.queue`. |

## Spec compliance
| Item | Status | Notes |
|---|---|---|
| U07-97 signatures (design 07 §3.3 + additions) | ✅ | recall, recall_with_status, propose, approve, reject, expire, expire_item, record_use, render (`.text`), prior_context, write_recommendations (also dicts, spec note 5), decide, outcome_adjustment, compactor(profile, *, ctx), promote_procedural, export_lora(..., *, golden_questions), session_load, session_save_turn (-> str \| None, R-32), health. on_review_decided removed (R-33). purge deferred (T07-26 ruling). |
| U07-97 every method delegates to exactly one unit | ✅ | write_recommendations and outcome_adjustment also read the priors (the U07-80 precondition says priors = outcomes_for_similarity()). That is composition, not a second unit. |
| U07-97 constructor builds scanner/writer/lifecycle/recaller + ensure_table | ✅ | `_facade.py:106-133`; a ModelUnavailable is logged as `memory.recall.degraded` (probes E1, E2 KILLED). |
| U07-97 compactor | ✅ | `_facade.py:223-229`; client is None without a registry; ScratchpadOps (U07-29 + save_checkpoint "scratchpad"). |
| U07-97 health ok/degraded/down | ✅ | `_facade.py:257-272`; probes H1 (> vs >=), H2 (vector -> down), H3 (list_ids args) and H4 (drop SELECT 1) all KILLED. |
| U07-98 get_memory_store / register / _reset | ✅ | As C2 and C13. from_config follows U07-98 (`_facade.py:81-91`), incl. `egress_enabled=cfg.security.egress.enabled` and `data_root=cfg.paths.data` (spec note 4). |
| UT07-85 | ✅ | construct, delegation, health (ok / degraded x2 / down), from_config, singleton, imports. |
| UT07-48 (U07-98 half) | ✅ | register twice, seams follow the latest store, foreign handler ConfigError. |
| IT07-10 | ✅ | Passes. The desync cases (missing, stale, orphan, TH07-13 status) are repaired via the registered handler; the rejected item is never recalled. |
| §2 rows (__init__, _facade 300, _compose 200, _tools_args, impl 05 tools/_tools_schema, impl 06 swarm/tools) | ✅ | sizes 87/330, 272/300, 175/200, 118/120, 384/400, 310/330; check_module_size 0. |
| Acceptance: lint-imports forbids memory -> swarm/pipelines/eval | ✅ | probed (C4a-f). |

⚠️ Not verifiable here: the real `herness.cli.worker_bootstrap` (T09-27) wiring, which is the production caller of `register_memory_components` and does not exist yet.

## Rulings
- **R1 (private siblings + lazy re-export): ACCEPTED.** Claim verified: `herness/core/config.py:38` imports `herness.harness.memory.settings` (and `_config_sections.py:17` does too), which runs the package `__init__` first. Probe R1 (eager `from ._facade import MemoryStore` in `__init__`) makes `python -c "import herness.core.config"` fail with `ImportError: cannot import name 'HernessConfig' from partially initialized module 'herness.core.config'`. So the facade cannot live in `__init__` or be eagerly imported by it.
- **R2 (seams configured in register_memory_components, not the constructor): ACCEPTED.**
  - Handler registration and seam configuration happen in the same function. Any process in which `outcome_measure` and `memory_maintenance` resolve therefore also has the seams set. Only `register_memory_components` registers these kinds (grep: no other `register_handler` for them).
  - The job worker's composition root is U09-104 `herness.cli.worker_bootstrap`. Impl 09 step 6 calls `register_memory_components`, and the same function is also the root for `run_inline` callers, so a worker never runs outcome_measure without a seam.
  - The dashboard (`app/common/bootstrap.get_services`, U09-53 step 7) only calls `get_memory_store()` and does not run jobs.
  - Setting seams in the constructor would make every constructed store (tests, `from_config` callers in spec 06) silently re-point process-wide state.
  - **Carry-over for T09-27:** worker_bootstrap must call `register_memory_components(tool_registry(), get_memory_store())`, since U09-104 names the function but not its arguments. The CLI `--inline` path (U09-103) must also run through worker_bootstrap.
- **LLMRegistry concern: NOT memory-specific.**
  - Under the shipped config (`profile local`, `security.egress.enabled = False`), two calls fail the same way, verified in a subprocess: `LLMRegistry(cfg.models, profile="local")` and the spec 05 accessor `client_for("analyst", profile="local", depth="standard")`. Both raise `ConfigError: client claude-opus is off-network but egress is disabled in profile local`.
  - Cause: the `config/models.yaml` fallback chains (planner/analyst/skeptic/skeptic_final/writer/chat) end in `claude-opus` (off_network: true), and `registry.py:_used_client_names` includes every fallback entry.
  - Today the only callers of `LLMRegistry` are `_facade.from_config` and `client_for`. Every future composition root (worker_bootstrap llm_factory, get_services, swarm) will hit the same error.
  - **Recommendation: keep** the propagation. U07-98 Errors is ConfigError, and falling back to `llms=None` would hide a config error and silently turn off LLM use for chat and compaction.
  - **Carry over to the impl 05/10 config owner.** Two options: the `local` profile overlay drops off-network fallbacks, or the registry check skips or filters off-network fallback entries when egress is disabled. That is a design decision for 05/10.

## Cross-card edits
- `_tools_schema.py` / `tools.py` (impl 05): the schema is built from `NumberRef.model_fields` like the old one. The builder's md5 parity claim agrees with the diff (same keys, order and constraints). POST_FINDING_SCHEMA untouched. Budgets kept. OK.
- `swarm/tools.py` (impl 06): definition removed, now imported from `herness.harness.tools`; the unused `NumberRef` import is removed. OK.
- `_tools_args.py` + `test_memory_tools.py` (T07-11): import line only. OK.
- FT07-01 (T07-22): only a kill case was added. OK.
- `tests/support/harness_state.py`: memory reset before and after each test. The full harness suites pass with it, so no test relied on seams leaking across tests. OK.
- `.secrets.baseline`: only a line_number shift (2662 -> 2663) of the existing docs/impl/05 entry, plus generated_at. There are 80 hashed_secret entries before and after, so no audited entry was dropped.
- Spec notes: impl 07 T07-23 note (1)-(13), §2 rows and T07-11 note (3) updated; impl 05 §2 rows and import note; impl 06 §2 row. All match the code.

## Findings
### Critical
none
### Important
none
### Minor
- m1 `tests/unit/harness/memory/test_memory_facade.py:285-313`: two mutants survive because the test cannot tell them apart.
  - Dropping `content_max` from `RecommendDeps` (`_facade.py:197`) passes, because the `MemoryConfig()` default `write.max_content_chars` equals the dataclass default 2000.
  - Replacing `partial(self._adjust, priors)` with `partial(self._adjust, [])` passes, because the test never asserts the `priors` kwarg of the patched `outcome_adjustment` Rec.
  - Fix: build the store with a non-default `max_content_chars`, and assert that `outcome_adjustment` saw the `priors` from the single read.
- m2 `test_memory_facade.py:244-250`: the `recall` delegation test passes `run_ctx=None`, so a facade that drops `run_ctx` (`_facade.py:143`) survives (probe). Pass a sentinel run_ctx.
- m3 `__init__.py:74-78`: on a foreign-handler `ConfigError`, the tools are already registered and both seams already re-pointed before `register_handler` raises (partial side effects). Harmless at start-up because the process exits. Registering the handlers first (or validating first) would make the failure atomic.
- m4 `tests/conftest.py:8`: the docstring still says `reset_harness_state` resets "the process tool registry" only; it now also resets the memory store and seams.
- m5 `_facade.py:194,211`: the priors read (`outcomes_for_similarity`) runs outside `run_bound`, so a store error there is logged without `run_id`. U07-80's "never raises" covers the unit, not this read. Acceptable (store errors are store errors); note only.
- m6: the LLMRegistry/local-profile issue (see the ruling) is a carry-over to impl 05/10, not a T07-23 defect.

## Mutation probes (32; each reverted with `git checkout -- <file>`)
| # | Mutation | Check | Result |
|---|---|---|---|
| C4a | memory/recall.py imports herness.harness.swarm.spawn | lint-imports | KILLED (BROKEN) |
| C4b | memory/lora.py imports herness.eval | lint-imports | KILLED |
| C4c | memory/chat.py imports herness.harness.pipelines | lint-imports | KILLED |
| C4d | memory/episodic.py imports herness.harness.blackboard | lint-imports | KILLED |
| C4e | _tools_args.py back to `from herness.harness.swarm.tools import NUMBER_REF_SCHEMA` | lint-imports | KILLED |
| C4f | function-local swarm.tools import in memory/__init__ | lint-imports | KILLED |
| R1 | eager `_facade` import in `__init__` | `import herness.core.config` | KILLED (circular ImportError) |
| H1 | `pending >` -> `>=` | health tests | KILLED |
| H2 | vector failure -> `down` | health tests | KILLED |
| H3 | `list_ids("", 2)` | health tests | KILLED |
| H4 | drop `SELECT 1 FROM memory_item` | health tests | KILLED |
| E1 | ensure_table not called | construct tests | KILLED |
| E2 | ModelUnavailable not caught | construct tests | KILLED |
| G1 | get_memory_store never caches | singleton tests | KILLED |
| G2 | reset keeps store | singleton test | KILLED |
| G3 | reset keeps seams | reset test | KILLED |
| G4 | lock removed | concurrent test | KILLED |
| R2a | outcome_measure not registered | UT07-48 | KILLED |
| R2b | configure_maintenance dropped | UT07-48 | KILLED |
| R2c | tools not registered | UT07-48 | KILLED |
| R2d | configure_maintenance dropped | IT07-10 | KILLED |
| C5 | content_max not passed | write_recommendations tests | SURVIVED (m1, equivalent under default config) |
| C5b | run_id not bound | write_recommendations tests | KILLED |
| C5c | priors not forwarded to adjust | write_recommendations / outcome_adjustment tests | SURVIVED (m1) |
| C8 | export_root `<data>/lora_data` | export_lora test | KILLED |
| C3 | from_config redactor not get_redactor | from_config test | KILLED |
| U97a | compactor client always None | compactor test | KILLED |
| U97b | recall drops run_ctx | recall tests | SURVIVED (m2) |
| C7 | fresh scanner for procedural | construct test | KILLED |
| C7b | CurrentGuard never rebuilds | current_guard tests | KILLED |
| D1 | draft rank fallback always index | invalid-dict test | KILLED |
| C9 | NoModels stand-in removed | construct test | KILLED |

29 killed, 3 survived. All three are Minor test-strength gaps; the code is correct by reading.

## Assessment
**Task quality:** Approved
**Reasoning:** All of C1–C15 are closed with evidence. U07-97/U07-98 behave as specified: health, singleton, registration, the lint contract and the R1 cycle claim are all confirmed by probes. Gates and the 2496 touched-package tests pass. What remains is test-strength polish and a config carry-over to impl 05/10.

`git status`: clean.

## Re-review round 1 (fix b189e8b on b22bddc; scope m1-m5)

**Verdict: Approved.** All five minors are closed. The three probes that survived round 0 are now killed, and the new m3 probes are killed. `git status` is clean (each probe was reverted with `git checkout -- <file>`).

| Item | Status | Evidence |
|---|---|---|
| m1 content_max and priors | ✅ | The test builds the store with `max_content_chars = 1500` and asserts that the priors list is the exact object from the single read. Probes C5 and C5c are now KILLED (`test_memory_facade.py:316`, `:317`). |
| m2 run_ctx and filters reach the recaller | ✅ | Test-only change: `_facade.py` already forwarded both arguments. Probes U97b (recall drops run_ctx), U97c (recall drops filters) and U97d (recall_with_status drops run_ctx) are all KILLED. |
| m3 atomic registration | ✅ | `__init__.py:78-94`: both kinds are checked with `resolve_handler` before anything is registered or configured. The new test `test_ut07_48_conflict_registers_nothing[outcome_measure, memory_maintenance]` checks: no tools, the other kind not registered, the foreign handler kept, both seams unset. Three probes are KILLED: m3a (pre-check disabled), m3b (tools and seams moved before the check), m3c (the round-0 order restored: register first, check after). A same-object re-registration still passes (UT07-48 twice). The check-then-register window is not locked, which is fine: start-up runs on one thread (U09-104 Concurrency). |
| m4 conftest docstring | ✅ | `tests/conftest.py:8-9` |
| m5 spec note | ✅ | Note (5) now says the priors read is outside `run_bound`; note (3) records the check-then-register order. |

### Probes, round 1

| # | Mutation | Result |
|---|---|---|
| C5 | content_max not passed | KILLED |
| C5c | priors not forwarded | KILLED |
| U97b | recall drops run_ctx | KILLED |
| U97c | recall drops filters | KILLED |
| U97d | recall_with_status drops run_ctx | KILLED |
| m3a | pre-check disabled | KILLED |
| m3b | tools and seams before the check | KILLED |
| m3c | check moved after registration | KILLED |

### Gates

- **Card tests** (facade, IT07-10, FT07-01, memory tools): 111 passed.
- **Coverage:** `__init__` 98% with 0 missed lines (the one partial branch, 102->101, is the "seam module not loaded" case), `_facade` 100%, `_compose` 100%.
- **Static checks:** ruff check clean; ruff format clean (1048 files); mypy 0 issues in 376 files; lint-imports 14 kept, 0 broken; check_module_size 0; check_type_ownership 0.
- `__init__.py` is 103/330 lines.

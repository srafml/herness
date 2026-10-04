# T07-01 report: shared and module-local memory types

Status: DONE_WITH_CONCERNS
Commit: d69bd05 feat(core): add shared and module-local memory types (T07-01)
Worktree: D:\herness\.claude\worktrees\agent-ad60debef6d692056 (parent 311ed52)

## Resume
The previous agent left two untracked test files. I kept both after checking them against the brief and impl 07 §3.1/§3.2:
- tests/unit/core/test_types_memory.py (UT07-01, UT07-02, UT07-03)
- tests/unit/harness/memory/test_memory_types_local.py (type-level checks for U07-11..U07-17, under the IDs those units list: UT07-24/32/44/52/68/78/80/81). The only change: `import pickle` got the same `# noqa: TID251` reason used by UT00-08.

## Implemented
- herness/core/types/memory.py (U07-01..U07-10). Contents: Layer, Kind, Status (plain `Literal` aliases, so `get_args` works) and KIND_LAYER (a MappingProxyType). Provenance is strict, frozen and extra=forbid, with the human→author_ref and agent→author_role+run_id invariants. MemoryItem is lax, uses AwareDatetime, checks KIND_LAYER, and allows 2,000 chars or 8,000 for sql_template/qa_pair. MemoryProposal is strict with unique NumberRef ids. RecallHit requires the six component keys, final == score and unconfirmed == pending_approval. MemoryRunContext has `from_tool_ctx(ctx, *, run_meta)`, which raises ToolInputError("run kind unknown for <run_id>"). RecommendationDraft is strict: summary 1–400 chars (R-30), finding_ids 1–50, marker refs `^n[0-9]+$`, target_type is a closed set. PriorRecommendation requires the eight outcome keys. PriorContext fills tally with 0 and rejects unknown keys. ConfidenceAdjustment and SimilarOutcome also added. It imports only pydantic, stdlib, herness.core.errors and herness.core.types.harness.
- herness/core/types/__init__.py: adds the memory import and names. I compacted `__all__` to several names per line (fmt: off, `# noqa: RUF022`), because ruff's RUF022 re-sorts it isort-style and conflicts with OWN032's code-point order. It also would have gone over its 150-line budget (152). It is now 99 lines.
- herness/harness/memory/types.py (U07-11..U07-17): RecallFilters, ProposeResult (merged_into ⇒ memory_id == merged_into; closed flag set), ChatTurn, SessionContext, PromotionReport, ExportReport (non-negative counts), ContextStats (0 < target < soft < hard < budget), MemoryNotFound(NotFound) (message "<kind> not found: <ident>", details {kind, ident}, survives pickle through _extra_attrs). It also holds MEMORY_ID_RE, REC_ID_RE, QUERY_ID_RE, FINDING_ID_RE and re-exports the 14 shared names (impl 07 §3.1).
- herness/harness/__init__.py and herness/harness/memory/__init__.py: docstring only.
- pyproject.toml: `herness.harness` is now the top layer of "herness layers" and is added to the "core base is closed" forbidden_modules. **Merge note:** the parallel group that also creates herness.harness makes the same two edits and its own __init__.py. Keep one copy of each.
- tests/integration/repo/test_import_contracts_enforced.py (ST00-10): it now creates herness/harness only if missing and adds the pyproject entries only if C1 does not already list herness.harness. Before this change it would fail with FileExistsError or duplicate layers once the package exists.

## RED / GREEN
- RED: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/unit/core/test_types_memory.py tests/unit/harness -q -p no:logging` → `ImportError: cannot import name 'MemoryItem' from 'herness.core.types'`, 2 collection errors.
- GREEN: the same command → 19 passed. Coverage (branch) of both new modules is 100 %.

## Gates
- mypy (project config, strict): Success, no issues found in 37 source files.
- lint-imports: 8 kept, 0 broken.
- ruff check: all checks passed. ruff format --check: 82 files already formatted.
- tools.check_type_ownership: exit 0. The only pending lines are for 06 and 09; nothing is pending for 07. test_ut00_48 passes.
- Fast suite `-m "(unit or integration) and not slow"`: 568 passed, 1 failed. The failure is IT00-02 (test_check_scripts), and its only cause is the MS001 below.
- tools.check_module_size: **fails with one finding**: `herness/core/types/memory.py: 263 lines > budget 150`. herness/harness/memory/types.py is 150/150 and core/types/__init__.py is 99/150.

## Concerns
1. **Module budget (needs a ruling).** herness/core/types/memory.py is 263 lines against a 150 budget; the hard limit is 400. It holds 10 pydantic models plus 3 literals and a map, with about 80 field lines and the required validators. With ruff formatting, two blank lines between classes and docstrings, it cannot reach 150 without hurting readability. I propose raising the impl 07 §2 budget for this row to 270. That also turns IT00-02 green.
2. **Duplicated ID patterns.** Impl 07 §3 says the ID patterns are "defined once in herness/harness/memory/types.py". The L0 module cannot import from L4, and OWN041 forbids importing private names through the submodule. So core/types/memory.py keeps private copies of the pattern strings, with a comment. Test UT07-68 asserts that MemoryItem's memory_id pattern equals MEMORY_ID_RE.pattern.
3. **Choices where the spec is open.** All models are extra="forbid", in line with TH07-02. Datetimes use AwareDatetime (impl 07 §3 Time rule), except ChatTurn.created_at, which stays plain `datetime` as the spec says. PriorRecommendation.confidence is bounded to 0–1. kind, target_type and rec_id stay `str`, because the spec gives no closed set.
4. The spec's §2 "memory must not import swarm/blackboard/pipelines" forbidden contract is not added: those modules do not exist yet, and UT00-58 rejects contracts that name missing modules.

## Fix round 1
- 94dee19 docs: raise core/types/memory.py budget to 270 (sub-controller ruling w02-s07). Only impl 07 §2 row changed (150 → 270). pyproject `[tool.herness.module_budgets].overrides` has no entry for this file, so nothing else to change.
- 7c39b69 fix(core): review fixes for memory types (T07-01)
  - Renamed the 8 type-level tests in tests/unit/harness/memory/test_memory_types_local.py to `test_rf_...`. Their docstrings now start with `RF`, following the global-constraints convention for review-focus tests. They no longer carry the UT07-24/32/44/52/68/78/80/81 IDs.
  - RecallHit now compares `set(components)` with a named `_COMPONENT_KEYS` frozenset. The `len(...) != 6` check and its `noqa: PLR2004` are removed. The file is now 264 lines, within the 270 budget.
- Gates:
  - ruff check: clean. ruff format --check: clean.
  - mypy: 0 issues. lint-imports: 8 kept, 0 broken.
  - check_type_ownership: exit 0 (pending 06 and 09 only). check_module_size: exit 0.
  - Fast suite: 568 passed, 1 xfailed, 5 deselected. The xfail is IT00-02, which carries a pre-existing xfail marker: check_traceability still reports doc defects in specs 01-11. The module-size part of IT00-02 now passes.

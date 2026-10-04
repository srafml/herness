# T07-11 report: memory tools (recall_memory, propose_memory)

Status: DONE_WITH_CONCERNS (final commit 28ae0fe feat(memory): T07-11 memory tools)
Worktree: D:\herness\.claude\worktrees\agent-a3db8db23c2dcf888 (branch worktree-agent-a3db8db23c2dcf888, base 7dff609)
Checkpoint: a5fcc81 wip(T07-11): memory tools and unit tests green

## Files
- herness/harness/memory/tools.py: 239 lines (budget 280). RecallMemoryTool, ProposeMemoryTool, register_memory_tools, MemoryToolStore Protocol, constants.
- herness/harness/memory/_tools_args.py: 153 lines (new private sibling, §2 row added, budget 170). Strict schemas, kind data, NumberRef conversion, data.items entry, field-only validation errors.
- tests/unit/harness/memory/test_memory_tools.py: UT07-46, UT07-47, UT07-48, UT07-90 (43 tests).
- tests/unit/harness/memory/_tools_env.py: helpers (seed_run, ctx_for, make_hit, FakeStore, RealStore adapter over MemoryRecaller + MemoryWriter + MemoryLifecycle).
- tests/security/test_st07_tools.py: test_st07_02_*, test_st07_06_*, test_st07_23_* (19 tests) on the real composition.
- docs/impl/07-memory.impl.md: §2 row for _tools_args.py; T07-11 spec note under U07-63.

## Red-then-green
- RED (unit): `uv run pytest tests/unit/harness/memory/test_memory_tools.py` before tools.py existed -> ImportError: cannot import name 'tools' from 'herness.harness.memory' (collection error).
- GREEN (unit): 41 passed (then 43 after branch-coverage tests).
- Security tests were written after the implementation; proven red by a mutation probe (dropped the extra-argument check and made include_pending_for unconditional): 8 failed (all six test_st07_02_forged_provenance_arguments_refused params, test_st07_06_pending_items_only_for_own_chat_user, test_st07_06_no_argument_widens_visibility); reverted with git checkout; GREEN 19 passed.
- Final: tests/unit/harness/memory + tests/security/test_st07_*.py + test_tools_registry.py: 614 passed.

## Coverage (card tests)
tools.py 100% line / 100% branch; _tools_args.py 100% / 100%.

## Gates
ruff format --check clean, ruff check clean, mypy (344 files) clean, lint-imports 13 kept, check_type_ownership 0, check_module_size 0. Commit hooks (incl. pytest-unit) passed on the checkpoint commit.

## Acceptance
- Snapshot test test_ut07_90_schemas_match_spec_tables: both schemas equal the §3.12 tables; $defs.NumberRef == herness.harness.swarm.tools.NUMBER_REF_SCHEMA (imported).
- test_ut07_90_strict_compatible: check_tool_schema passes and _strict_ok true for both.
- test_ut07_47_writer_role_cannot_resolve_propose_memory: ToolRegistry.resolve(WRITER, ["propose_memory"]) -> ConfigError; analyst_ops resolves it.

## Deviations / spec notes (recorded under U07-63)
1. MemoryToolStore Protocol (3 methods, §3.21 signatures); T07-23's MemoryStore must satisfy it.
2. is_strict_compatible = _tools_schema.check_tool_schema / _strict_ok.
3. NumberRef schema imported from herness.harness.swarm.tools (spec 05 publishes no schema object of its own; that one implements the U05-10 row_key pair fallback). Module map "imports" column of tools.py says herness.harness.tools only; the swarm import sits in _tools_args.py and is recorded in its §2 row.
4. Tools refuse arguments outside their schema even when dispatch is bypassed; ValidationErrors become ToolInputError naming fields only.
5. run_meta = {**run.meta, "kind": run.kind} (spec wrote {"kind": run.kind, **run.meta}; row kind now wins).
6. kinds deduplicated before RecallFilters; glossary term/definition stripped; empty ASCII slug -> "rule"; null rationale omitted from data.
7. Private sibling _tools_args.py to fit 280 lines (tools.py was 371 lines in one module).

## Carry-overs
- T07-23: UT07-48's U07-98 half ("handlers registered") and wiring register_memory_components(tool_registry, MemoryStore) -> register_memory_tools.
- Potential future import cycle: memory._tools_args imports herness.harness.swarm.tools (blackboard, spawn, store.ops). If swarm later imports herness.harness.memory at module level and memory/__init__ imports tools, a cycle appears; moving NUMBER_REF_SCHEMA to a spec 05 module (e.g. herness.harness.tools) would remove it.

## Fix round 1 (review T07-11-review.md) — commit b4b50d1 fix(memory): T07-11 redact business_rule rule_id slug
- I-1: herness/harness/memory/_tools_args.py kind_data: business_rule rule_id is now slug(herness.core.redact.get_redactor().redact(content).text); RedactionFailed propagates (not caught). _write_steps.py untouched. Spec note sentence added under U07-63 (TH07-05).
  Tests: test_ut07_47_rule_id_slug_is_redacted (planted email + name absent from the slug), test_ut07_47_rule_id_redaction_failure_propagates, test_st07_05_rule_id_carries_no_planted_personal_data (real write path, stored data.rule_id and content hold no planted value).
  Test helpers make `get_redactor()` return the write path's redactor (monkeypatch of herness.core.redact.get_redactor; setting _State.redactor did not survive the config reset that the ops_store fixture runs).
  RED: with the slug reverted to slug(content), both rule_id tests failed (2 failed); GREEN after restore.
- M-1: test_ut07_47_unproposable_kinds_rejected_before_store (mapping, sql_template, qa_pair, run_summary with dispatch bypassed -> ToolInputError before the run lookup and with no store call).
- M-3: test_ut07_46_run_row_kind_wins_over_meta (meta {"kind": "chat"} on an org_review run -> run_ctx.run_kind == "org_review" for both tools).
- Sizes: tools.py 239/280, _tools_args.py 155/170. Coverage 100% line/branch on both.
- Tests: card files 70 passed; tests/unit/harness/memory + tests/security/test_st07_*.py 606 passed. Gates clean (ruff format/check, mypy, lint-imports, type ownership, module size).
- Parked as ruled: M-2, M-4, M-5, NUMBER_REF_SCHEMA import (carry-over).

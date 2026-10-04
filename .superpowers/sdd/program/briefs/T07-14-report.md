# T07-14 report: ContextCompactor and compaction notes summarizer

Worktree agent-a557a8b5dbd0d1f19, base 9c8f34a. Status: DONE (final commit SHA appended below).

## Files (lines / budget)
- herness/harness/memory/compactor.py 312/330 (new): ContextCompactor (U07-76), summarize_notes (U07-77), CompactorOps, CompactionReport
- herness/harness/memory/_compactor_ledger.py 155/200 (new private sibling, new section 2 row): LedgerPass, task_message, text_of, query_ids_in, kept_refs, invariants_hold, enforce_cap
- herness/harness/memory/_compactor_llm.py 146/200 (new private sibling, new section 2 row): compaction_prompt, NOTES_SCHEMA, Completer, chunks, request
- herness/harness/memory/prompts/compaction_notes.md 34/60 (new)
- herness/harness/memory/compact_build.py 383/390 (transcript wrap ruling; _transcript renamed to public transcript)
- working.py 296/300 and hooks.py 340/340 untouched
- Tests: tests/unit/harness/memory/test_memory_compactor.py, _compactor_support.py (InMemoryOps fake CompactorOps, FakeBackend, RefusingLLM/SlowLLM subclasses of FakeLLMClient), test_memory_compact_build_props.py (PT07-01 loop added), tests/fault/harness/test_memory_compactor_fault.py, tests/security/test_st07_compactor.py
- Minimal edits: test_memory_compact_build.py (UT07-59 expects wrapped transcript); tests/unit/harness/test_tools_recording.py (UT05-124 reviewed hash allow-list gains memory/_compactor_llm.py compaction_prompt = prompt content hash)

## Test IDs
- UT07-60: test_ut07_60_k_shrinks_to_reach_target, _ledger_compacted_then_fits, _over_hard_raises_output_validation_error, _no_budget_exceeded_raise_in_memory (grep), _ledger_budget_error_propagates_unchanged, _claude_shrink_and_summary_preamble
- UT07-61: test_ut07_61_llm_notes_charged_and_traced, _refusal_gives_deterministic_notes, _bad_json_twice_gives_deterministic_notes, _bad_json_once_is_repaired, _timeout_gives_deterministic_notes, _server_fault_and_no_client, _notes_failing_validation, _chunks_at_half_budget_carry_prior_notes, _summarize_notes_direct_without_tool_groups
- UT07-62: test_ut07_62_saved_then_restored_by_new_compactor, _busy_store_logs_and_continues, _completed_event_and_report, _short_history_is_copied, _pressure_and_compactor_like (typed CompactorLike return checked by mypy --explicit-package-bases + runtime signature check), _second_compaction_does_not_nest_scratchpads, _claude_profile_ledgers_kept_groups, _orphans_state_ids_and_summary_rescue, _invariant_failure_retries_then_raises, _invariants_detect_losses, _size_cap_after_compact_drops_oldest_unmatched, _prompt_file_contract, _missing_prompt_is_config_error, _task_message_of_plain_and_merged_heads
- PT07-01: test_pt07_01_compactor_loop_keeps_ids_numbers_and_groups (new, hypothesis, output fed back as state.messages, both profiles) alongside the T07-13 PT07-01 tests
- FT07-05: test_ft07_05_hung_summarizer_then_restart_from_checkpoint[timeout|hang]
- ST07-15: test_st07_15_adversarial_tool_outputs_keep_invariants[local|anthropic], test_st07_15_injection_stays_inside_transcript_wrapper
- ST07-16: test_st07_16_invented_numbers_and_ids_repaired[local|anthropic]

## Rulings applied
- Carry-overs: M0 = original task message (merged head cut before its first scratchpad part); Claude profile ledgers kept groups (once per call); orphan results under placeholder call "unknown".
- CompactorLike compatibility tested; hooks.py untouched.
- Open question 1: Claude transcript = wrap_untrusted("tool_results", None, escape_content(transcript)) in build_compacted. ST07-15/16 assert closing-tag injection, fake scratchpad tags and role-marker lines stay inside the escaped wrapper / flattened escaped scratchpad lines.
- Summarizer: complete_validated(Completer, max_repairs=1); Completer = wait_for(profile.timeout_s) + ctx.ledger.charge per response incl. repair + llm_call trace (llm_call_fields with prompt_hash) + ModelRefused on refusal. U07-77 step 6 list -> deterministic with memory.compaction.notes_fallback WARNING reason=class name. validate_notes on parsed result. Ledger BudgetExceeded propagates (tested). No BudgetExceeded raise in herness/harness/memory.
- Size cap enforce_cap before each render/save (compact, then oldest unmatched; ids and cited refs never dropped).
- Prompt: exact U07-99 sentence, 34 lines, no secrets, importlib.resources on herness.harness.memory + prompts/compaction_notes.md (wheel ships whole package; no build change), cached once per process, 16-hex sha256 prompt_hash, missing -> ConfigError at ContextCompactor construction.

## Spec notes (docs/impl/07-memory.impl.md)
Section 2 rows for both siblings; notes on U07-75 (escaped+wrapped, not unchanged), U07-76 (M0 recovery, Claude ledgering + covers_steps end, orphan placeholder, generalized id rescue, cap placement, numeral-mention reading, CompactorLike, metric backend label = profile.kind, siblings), U07-77 (via complete_validated(max_repairs=1), spec 08 repair text, escaped prior notes, step lines on success, oversize group cut), U07-99 (resource loading, hash, ConfigError location).

## Deviations / readings
- Resources resolved on package herness.harness.memory with relative path (no prompts/__init__.py, which would be an unmapped module).
- Id rescue generalized (every id of state.messages / state.query_ids missing from ledger, M0 and kept groups) - superset of spec rules.
- Step 9 numeral-mention check = every recorded unmatched value present in new list.
- Claude covers_steps end = last ledgered step.
- Repair text is spec 08 U08-36 (ruling).
- Repair path records a resilience repair event, needing a bound resilience backend (tests bind a fake).
- On OutputValidationError the in-memory scratchpad keeps that call's ledger updates; nothing saved.

## Gates
Card tests + tests/unit/harness/memory: 247 passed. Coverage: compactor.py 99%, _compactor_ledger.py 98%, _compactor_llm.py 100%, compact_build.py 100% (line+branch combined). ruff format/check clean, mypy clean, lint-imports 13 kept, check_module_size 0, check_type_ownership 0.

## Concerns
- Pre-commit unit hook flaked twice on unrelated tests (test_synth_catalog timing under load; test_blackboard st06_05 random ULID containing "17"); both pass alone; commit retried. WIP checkpoint commits did not land because of these flakes, so the card lands as one commit.

## Final commit
bf24f0c feat(T07-14): ContextCompactor and compaction notes summarizer (all pre-commit hooks passed, incl. full unit suite)

## Fix round 1 (review T07-14-review.md)
- I1: new ST07-16 test test_st07_16_summarizer_input_is_escaped_and_wrapped reads the request text from the llm_call trace payload: exactly one tool_results wrapper, one closing tag, no raw injection, no raw "<scratchpad"; prior notes containing a fake scratchpad tag and the closing-tag injection are escaped (line 111), tool-result injection escaped inside the wrapper (line 125).
- I2: new UT07-62 test test_ut07_62_size_cap_wired_before_render_and_save patches compactor.enforce_cap to a small limit: saved checkpoint <= limit, fewer unmatched kept, last_report.ledger_compacted True, head scratchpad == render of the saved (capped) pad, no cols=[.
- m1: Completer now takes the profile; when profile.supports.sampling_params is false it sends temperature=None on every request incl. the repair (build_repair_request sets 0.0). Test test_ut07_61_no_sampling_params_means_no_temperature.
- m2: code comment at the complete_validated call and U07-77 spec note: repair needs the bound resilience ops backend (else ConfigError, not a fallback).
- m3: orphan test uses a table result; asserts row_count 3 and step 1 (not producible by the rescue).
- m4: U07-76 spec note: rescued ids appear in the LEDGER despite its "verbatim from tool results" header.
- m5: U07-77 spec note records max_output_tokens = min(cfg.summary_max_tokens, profile.max_output_tokens).
- Manual mutation check: both escapes removed, enforce_cap call removed, cap moved after render, orphan results skipped, temperature fix removed: all 6 killed.
- Sizes: compactor.py 314/330, _compactor_llm.py 150/200. Tests: 250 passed (card + tests/unit/harness/memory). ruff, mypy, lint-imports, check_module_size clean.
- Fix round 1 commit: b2a6cdb (all pre-commit hooks passed on the first try).

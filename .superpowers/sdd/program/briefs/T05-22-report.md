# T05-22 report (Hooks) — build agent

Worktree: D:\herness\.claude\worktrees\agent-a7e455eb794045665 (branch worktree-agent-a7e455eb794045665, base 8bb8194)
Status: DONE_WITH_CONCERNS (minor; see Concerns). Final commit: 2553fc6 feat(harness): T05-22 GatedClient, HarnessHooks and truncate_context (all pre-commit hooks passed; no wip commit landed: the first wip attempt was refused by the module-size hook at 365 lines, then trimmed to 340).

## Files
- herness/harness/hooks.py — 340 lines (budget 340, at budget). New.
- tests/unit/harness/test_hooks.py — 747 lines, 25 tests. New.
- No other file changed (tools.py untouched; no loop.py; no spec edits).

## Units
- U05-61 GatedClient: gate entered around exactly one inner call (`async with`), `last_gate_wait_ms` = round((monotonic after enter - t0) * 1000); gate released before the refusal check (ModelRefused(category=refusal_category, client=name)) and before any inner error propagates. Streaming only with sink + StreamCapable inner + no response_schema; `on_text_delta(None)` reset when the shared StreamState says text was already emitted. `complete` = U05-25 rule. No-gate case uses `contextlib.nullcontext()`.
- U05-62 HarnessHooks: before_call identity; call: chain path `chain.acomplete(req, schema=, client_for=make, tracer=)` where make(key) wraps `registry.client(key)` with `gates.get(key)`; no-chain path GatedClient(client, gates.get(client.name)) with `resilience.complete_validated(g, req, tracer=)` then `schema.model_validate(resp.parsed)` (None / ValidationError -> OutputValidationError, `from None` so model output is not chained); gate waits summed in creation order into `state.note_gate_wait`. needs_compaction (compactor pressure or est >= soft), on_context_pressure (compactor; OutputValidationError -> WARNING `harness.loop.truncation_fallback` task_id/phase/reason="compactor_failed" -> truncate_context; no compactor -> truncate_context), on_loop_signal delegates to `resilience.loop_signal_policy`, after_step saves via `asyncio.to_thread(jobs_tasks.save_checkpoint, task_id, "loop", state.to_checkpoint())` when stopping / stop() / first save / interval elapsed (interval read from get_config() only when needed), then CancelledError on stop. Precondition task_id/phase both-or-neither enforced with ConfigError.
- U05-76 truncate_context: pure; groups = assistant message + following messages; oldest whole groups dropped while `fixed + estimate_tokens([first, note] + kept) >= soft` and more than one group left; note = user/compaction_summary TextPart `wrap_untrusted(body, source="truncation")` with head line, "Steps removed: <first>–<last>" (or "none"), query_ids and finding_ids joined by ", ".
- spec 08 functions are called through their packages at call time (`resilience.complete_validated`, `resilience.loop_signal_policy`, `jobs_tasks.save_checkpoint`) so tests monkeypatch fakes.

## Tests (RED -> GREEN)
- RED: `pytest tests/unit/harness/test_hooks.py` -> ImportError: cannot import name 'hooks' from 'herness.harness' (collection error).
- GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/harness/test_hooks.py -q -p no:logging --require-test-ids --cov=herness.harness.hooks --cov-branch` -> 25 passed; hooks.py 100 % line, 100 % branch.
- `pytest tests/unit/harness -q -p no:logging` -> 1163 passed, 1 skipped (Windows symlink privilege).
- IDs covered: UT05-95 (2), UT05-96 (2), UT05-97 (4), UT05-98 (2), UT05-99 (4), UT05-100 (3), UT05-101 (1), UT05-102 (3), UT05-127 (4). UT05-95 uses a real CallGate(size 1) and a real 1 s tool sleep (~1 s test).
- Gates: ruff format/check clean, mypy (253 files) clean, lint-imports 13 kept, check_module_size exit 0, check_type_ownership exit 0.

## Deviations / spec notes (no spec edit made)
1. save_checkpoint is not exported from `herness.core.jobs` (lazy _EXPORTS lacks it); hooks import `herness.core.jobs.tasks` and call `tasks.save_checkpoint`. `state.to_checkpoint()` already returns a JSON-mode dict (cost as str), so it is passed as-is (no extra model_dump); UT05-102 asserts equality with `to_checkpoint()` and that it validates as LoopCheckpoint.
2. Tracer vs impl 08 TracerLike: `Tracer.emit(type, /, ...)` is positional-only while `TracerLike.emit(type, **fields)` is not, so mypy rejects passing Tracer. hooks pass `cast("TracerLike", tracer)` (same object; identity asserted in UT05-98). Worth a cross-spec ruling (make TracerLike.emit positional-only).
3. "state is stopping" is only the private `LoopState._stopping` (no public accessor in U05-14); hooks read `state._stopping` (SLF001 is not enabled). T05-23 `_finish` must set it.
4. truncate_context step numbering: groups carry no step numbers, so assistant groups are numbered backwards from `state.step` (newest = state.step); a leading non-assistant group (no step) is its own group. Earlier `compaction_summary` messages are dropped so the result has exactly one summary (the new note carries every id); this also drops an earlier compactor summary text when truncation runs after a successful compaction.
5. Streaming that ends without a `Done` raises OutputValidationError("stream ended without a final response") (defensive; StreamCapable promises a final Done).
6. `StreamState` is public (named in the U05-61 signature) and in `__all__` next to the module-map exports; `_PressureLike` is a private protocol for the pressure object.
7. Gate wait is noted only when `call` succeeds (spec step 4 after step 2/3); a raising chain leaves gate wait unrecorded.

## Concerns
- hooks.py is exactly at its 340-line budget (no headroom for T05-23 fixes).
- TracerLike/Tracer signature mismatch (item 2) needs a ruling eventually.
- UT05-95 costs ~1 s wall time (real 1 s tool sleep per the test row).

## Fix round 1 (review T05-22-review.md, Approved; polish)
- M1: `GatedClient._stream` iterates `inner.astream(req)` in try/finally and, when the stream is an `AsyncGenerator`, awaits `aclose()` inside the gate (type-safe for a plain AsyncIterator without aclose). Tests: UT05-97 half-read stream closed before gate release (log enter, closed, exit; verified failing with the aclose removed) and UT05-97 plain AsyncIterator stream.
- M2 mutation-killing tests: M12 schema alone disables the sink (UT05-98), M16 gate chosen by chain key not client name (UT05-98), M13 earlier summary in a kept group dropped (UT05-127), M17 steps counted back from state.step > groups (UT05-127), M7 estimate exactly at soft drops (UT05-127), M19 save runs in a worker thread via threading.get_ident (UT05-102).
- Room for M1 made by trimming the module docstring and one protocol docstring; no logic removed. hooks.py = 340 lines (budget 340).
- Spec notes "Note (T05-22)" added to docs/impl/05-harness-core.impl.md in the Complexity-and-limits rows of U05-61 (stream closing, no-Done error, last_gate_wait_ms overwritten by repairs), U05-62 (TracerLike cast, save_checkpoint via jobs.tasks, _stopping PrivateAttr, gate wait only on success) and U05-76 (step numbering, dropped earlier summaries).
- Parked per controller: M3, M4, M6 (unchanged).
- Tests: test_hooks.py 33 passed (--require-test-ids), hooks.py 100 % line / 100 % branch; tests/unit/harness 1171 passed, 1 skipped. ruff, mypy (253), lint-imports (13 kept), check_module_size 0, check_type_ownership 0.
- Fix round 1 commit: b45da6c fix(harness): T05-22 review round 1 (stream aclosing, mutation tests, spec notes); all pre-commit hooks passed.

# T05-22 review (Hooks: GatedClient, HarnessHooks, truncate_context)

Reviewer: verify agent. Worktree D:\herness\.claude\worktrees\agent-a7e455eb794045665, base 8bb8194, head 2553fc6. Read-only; probe edits reverted (`git checkout -- herness/harness/hooks.py`, tree clean at finish).

**Verdict: Approved** (no Critical, no Important; Minor items and spec notes below).

### Spec Compliance
- ✅ U05-61 GatedClient: gate entered via `async with` around exactly one inner call (hooks.py:107-110), so it is released on success, inner error, stream error, sink error and cancellation; refusal check after the gate (hooks.py:111-113); streaming only with sink + `StreamCapable` + `response_schema is None` (hooks.py:127); `on_text_delta(None)` reset through the shared `StreamState` (hooks.py:133-135); `complete` = U05-25 rule (hooks.py:116-123); `nullcontext` when no gate.
- ✅ U05-62 HarnessHooks: `call` chain path `chain.acomplete(req, schema=, client_for=make, tracer=)` with `make(key)` -> `GatedClient(registry.client(key), gates.get(key))`, sink disabled when `schema` is set (hooks.py:193-208); no-chain path `resilience.complete_validated(g, req, tracer=)` + `_parse` (None / ValidationError -> `OutputValidationError ... from None`, hooks.py:265-274); gate waits summed in creation order (hooks.py:216); `needs_compaction` both branches (hooks.py:219-224); `on_context_pressure` fallback with WARNING `harness.loop.truncation_fallback` carrying only task_id/phase/reason (hooks.py:226-239); `on_loop_signal` is pure delegation to `resilience.loop_signal_policy` with the tracer (hooks.py:245); `after_step` saves via `asyncio.to_thread(jobs_tasks.save_checkpoint, task_id, "loop", state.to_checkpoint())` (keyed form, no `now`) when stopping / stop() / first / interval elapsed from `get_config().resilience.resilience.loop.checkpoint_min_interval_s`, then `CancelledError` after the save (hooks.py:247-262). Precondition task_id/phase enforced (hooks.py:165-167).
- ✅ U05-76 truncate_context: pure; first message identity kept; exactly one `user`/`compaction_summary` note wrapped with `wrap_untrusted(..., source="truncation")` (TH05-01, hooks.py:335-339); whole groups dropped oldest-first while `len > 1` and `fixed + estimate >= soft` (hooks.py:285-290); every query/finding id in the note in state order; edge cases (no groups -> [first, note]; single oversized group kept; earlier summaries superseded) behave as intended.
- ⚠️ Cannot verify here: (a) that T05-23 `_finish` sets `LoopState._stopping` (only a private attr exists in U05-14, herness/core/types/harness/agent.py:183); (b) that spec 06 keeps `task.status='running'` until the loop's stop-save runs (otherwise `save_checkpoint` raises `JobStateError`, see Minor 3); (c) IT05-11 (integration, not in this card).

### Checks run (all on head)
- ruff check / ruff format --check: clean (608 files). mypy: clean (253 files). lint-imports: 13 kept. check_module_size: exit 0 (hooks.py = 340, exactly at budget). check_type_ownership: exit 0.
- `pytest tests/unit/harness/test_hooks.py --require-test-ids --cov-branch`: 25 passed; hooks.py 100 % line, 100 % branch (178 stmts, 42 branches). `pytest tests/unit/harness`: 1163 passed, 1 skipped (Windows symlink privilege). Test names carry UT05-95..102/127 IDs.
- No HTTP/LLM SDK imports in hooks.py (grep httpx/requests/anthropic/openai: none). Model calls go only through `GatedClient` / `complete_validated` / `ModelChain`.
- Log redaction: the only log call is hooks.py:233-238 (task_id, phase, reason). `ModelRefused` / `OutputValidationError` messages are constants; `_parse` uses `from None` so pydantic's echo of model output is not chained.
- Determinism: gate-wait sum over `made` in creation order; groups in message order; ids joined in state order.

Mutation probes (19; file restored after each run and via git checkout at the end):

| # | Mutation | Result |
|---|----------|--------|
| M1 | gate replaced by nullcontext | killed |
| M2 | no `sink(None)` reset | killed |
| M3 | `emitted` never set | killed |
| M4 | stop does not force a save | killed |
| M5 | `_stopping` ignored | killed |
| M6 | interval `>=` -> `>` | killed |
| M7 | truncation `>= soft` -> `> soft` | **survived** |
| M8 | may drop the last group | killed |
| M9 | wrap source changed | killed |
| M10 | gate wait sum -> max | killed |
| M11 | tracer not passed to loop_signal_policy | killed |
| M12 | sink passed even when `schema` set | **survived** |
| M13 | earlier `compaction_summary` kept | **survived** |
| M14 | refusal check removed | killed |
| M15 | compactor fallback removed | killed |
| M16 | `gates.get(inner.name)` instead of `gates.get(key)` | **survived** |
| M17 | step numbering offset changed | **survived** |
| M18 | CancelledError skipped after a save | killed |
| M19 | save called synchronously (no `to_thread`) | **survived** |

### Strengths
- Gate handling is structurally correct: a single `async with` covers every exit path, including `CancelledError`; the refusal is raised after release as specified; nothing runs under the gate except the one model call (plus the spec-mandated UI sink).
- UT05-95 uses the real `CallGate(size 1)` with a real 1 s tool and proves the second call waits less than the tool time.
- Redaction is deliberate (`from None`, constant messages, three log fields).
- 100 % line/branch coverage; most behavioural mutations are killed.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. hooks.py:137-142: `inner.astream(req)` is iterated without `contextlib.aclosing(...)`. If the sink raises, the task is cancelled, or the stream raises mid-iteration, the async generator (and its HTTP stream) is closed only by the GC finalizer, possibly after the gate is released, so the backend can briefly see more open streams than the gate allows (TH05-08). Wrap with `async with contextlib.aclosing(inner.astream(req)) as events:` (about one line; budget is at 340).
2. Test gaps in tests/unit/harness/test_hooks.py, each proven by a surviving mutation:
   - M12 (:403-417): the schema test also sets `req.response_schema`, so the rule "sink = None when `schema` is set" is not pinned; use a request without `response_schema` and `schema=Answer`.
   - M16 (:364-398): registry client names equal the keys, so `gates.get(key)` vs `gates.get(inner.name)` is not distinguished; give a client a name different from its key.
   - M13/M17 (:723-738): the old summary sits in the oldest group, which is dropped anyway, and `state.step` equals the number of assistant groups, so neither the summary filter nor backward step numbering (step > groups, e.g. after an earlier compaction) is exercised.
   - M7 (:679-708): no case with the estimate exactly equal to soft.
   - M19 (:615-639): nothing asserts the save runs off the event loop (e.g. record `threading.get_ident()` in the fake save and compare with the loop thread).
3. hooks.py:249-256: if `save_checkpoint` raises on a stop (e.g. `JobStateError("task not running")` when spec 06 has already moved the task out of `running`, or a store error), that error replaces the `CancelledError`. The spec is silent; this is defensible (a failed save is not hidden) but should be confirmed against spec 06's cancel/preempt flow (spec note).
4. hooks.py:109 and :216: `last_gate_wait_ms` is overwritten per attempt, so waits of `complete_validated` repair attempts on the same GatedClient are not summed; and gate wait is not noted when `call` raises. Both are spec-literal (step 4 follows 2/3; the attribute is "last"); acceptable, spec note only.
5. hooks.py (whole file): exactly at its 340-line budget; T05-23 fixes (and Minor 1) have no headroom. Consider a budget note now.
6. Unbounded awaits, spec-mandated: gate entry (hooks.py:108) has no timeout, and the UI sink is awaited inside the gate (hooks.py:139). Boundedness relies on spec 06 cancellation and adapter HTTP timeouts; no change requested, noted for T05-23/spec 06 wiring.

### Builder deviations (judgement)
1. `cast("TracerLike", tracer)` (hooks.py:178): acceptable; same object (identity asserted in UT05-98). Needs a cross-spec note: make impl 08 `TracerLike.emit`'s `type` positional-only so the cast can go.
2. `save_checkpoint` via `herness.core.jobs.tasks` (hooks.py:25, 252): acceptable; `tasks.py` owns it per the impl 08 module map, and the lazy `herness.core.jobs._EXPORTS` simply lacks it. Spec/08 note: add `save_checkpoint` to `_EXPORTS`, then switch the import.
3. Reading `LoopState._stopping` (hooks.py:259): acceptable (U05-14 defines only the private attr; `_finish` "marks the state stopping"). Spec note: add a public `stopping` property / `mark_stopping()` to U05-14 in a later card; T05-23 must set it.
4. Truncation step numbering backwards from `state.step` (hooks.py:307-315): acceptable (the spec gives no source for step numbers); needs a spec note stating the rule, plus the test in Minor 2. Dropping earlier `compaction_summary` messages (hooks.py:298) follows from the "one note" postcondition but discards a prior compactor summary's prose; spec note.
5. Stream without `Done` -> `OutputValidationError` (hooks.py:143-145): acceptable defensive choice (lets the chain fall back); spec note.
6. Gate wait recorded only on success (hooks.py:216): acceptable, spec-literal (see Minor 4).
7. `StreamState` public in `__all__`: acceptable (named in the U05-61 signature).

### Assessment
**Task quality:** Approved
**Reasoning:** All three units match the binding spec, the gate is released on every path, redaction and spec 08 delegation are correct, and every gate passes with 100 % coverage. Remaining items are small test gaps, an `aclosing` hardening and spec notes.


---

## Round 1 re-review (head b45da6c, base 2553fc6)

**Verdict: Approved.** No new findings of Critical or Important severity.

Scope: fix commit b45da6c (hooks.py +/-32, test_hooks.py +138, spec notes in docs/impl/05-harness-core.impl.md U05-61/62/76 rows). Previous Minors 3, 4 and 6 are parked as agreed.

### Minor 1 (stream close): ✅ fixed
- hooks.py `_stream`: `events = inner.astream(req)` is iterated in `try`/`finally`, and the `finally` calls `await events.aclose()` when `events` is an `AsyncGenerator`. This runs inside `async with self._gate`, so the stream is closed before the gate is released.
- Proven by a probe script (scratch `cancel_probe_t0522.py`, recording gate plus an async-generator client):
  - cancel mid-stream: `['enter', 'closed', 'exit']`
  - normal completion: `['enter', 'closed', 'exit']`, response returned unchanged
- Error path: new test `test_ut05_97_half_read_stream_closed_before_gate_release` asserts `['enter', 'closed', 'exit']`.
- Behaviour is otherwise unchanged: a plain `AsyncIterator` without `aclose` still works (`test_ut05_97_plain_async_iterator_stream_without_aclose`), and a stream that ends without `Done` still raises `OutputValidationError`. Both adapters' `astream` are `async def` generators (anthropic_client.py:329, openai_compat.py:239), so production streams take the close path.
- Nit (no action): if the generator's own `finally` raised during `aclose()` while another exception was propagating, it would replace that exception. This is standard `aclosing` semantics.

### Minor 2 (test gaps): ✅ fixed
I re-ran all 19 original mutants plus a new M20 (`aclose` removed) with the uniquely named script `mut_t0522_r1.py`. The script restores the bytes after each mutant, and the tree was clean afterwards.
- **All 20 killed.** This includes the previous survivors M7, M12, M13, M16, M17 and M19.
- Each is killed by its targeted new test: estimate equal to soft; schema set without `response_schema`; summary inside a kept group; gate chosen by chain key vs client name; steps counted back from `state.step`; save thread ≠ loop thread.

### Docstring trims: ✅ nothing binding lost
- The module docstring was shortened. The spec 08 block names and the release wording still appear in `HarnessHooks`/`GatedClient`/`acomplete` docstrings and code comments.
- `_PressureLike`'s docstring became an inline comment (private protocol).
- ruff (pydocstyle) is clean.

### Spec notes: ✅ accurate
- U05-61: `try`/`finally` + `aclose` for async generators; no-`Done` → `OutputValidationError`; `last_gate_wait_ms` is the last attempt only.
- U05-62: (a) the `TracerLike` cast, (b) the `jobs.tasks.save_checkpoint` path and JSON-mode `to_checkpoint()`, (c) the `_stopping` PrivateAttr and the T05-23 obligation, (d) gate wait recorded only on success.
- U05-76: backward step numbering from `state.step`, the leading non-assistant group, and earlier summaries dropped with their text discarded.

All match the code. The U05-61 note's "never outlives the gate" is correctly qualified by "when it is an async generator".

### Gates (head b45da6c)
- ruff check clean; ruff format --check clean (608 files); mypy clean (253 files); lint-imports 13 kept, 0 broken.
- check_module_size exit 0: hooks.py = 340, still exactly at budget (earlier Minor 5 stands).
- check_type_ownership exit 0.
- test_hooks.py `--require-test-ids`: 33 passed. hooks.py coverage 100 % line (182 stmts), 100 % branch (44).
- tests/unit/harness: 1171 passed, 1 skipped (Windows symlink privilege).

**Task quality (round 1):** Approved.

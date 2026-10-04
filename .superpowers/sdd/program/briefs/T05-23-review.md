# T05-23 review — Agent loop (verify agent)

Worktree agent-a1f8d286923a37ae9, base 9c8f34a, head a770b37. Reviewed the brief, the spec (§3.6 U05-57..60, U05-74, §8 events, §11 rows), the report and the diff. Card tests were run with coverage, then tests/unit/harness, the static gates and 30 mutation probes. Every probed file was restored; `git status` is clean after the temp dir was deleted.

## Verdict: **Approved** (no Critical or Important findings; Minor items below)

## Spec compliance
- U05-57 LoopHooks ✅ The protocol matches the signature exactly (six async methods plus the `on_text_delta` attribute). tools.py `dispatch` is retyped to `LoopHooks | None` under TYPE_CHECKING (net -3 lines, per ruling w20-s05b).
- U05-58 run_agent ✅ Each numbered spec step is covered:
  - Preconditions: the profile/client name check and the egress-purpose check both raise ConfigError.
  - Setup (1)-(6): in `_loop_steps.fresh_state`.
  - (7) Compaction: `n_messages_removed` is counted by `id()` against the captured `old` list. This is safe because `replace_messages` rebinds the list. `fresh_conversation` is `kind == "anthropic"`, and a result still over the hard limit ends `task_tokens` with cause `context`.
  - (8) Hard limits run in the spec order: steps, then tokens, then wall clock through `herness.core.time.monotonic` with the deadline through `clock.now()`.
  - (9) Wrap-up and warn are each gated once by `_wrap_up_sent` / `_budget_warned`.
  - (10)-(12) Requests go through `hooks.before_call` and `hooks.call`; the loop never calls the client directly. Then the state is charged, the ledger is charged (`BudgetExceeded` propagates and is logged), and `llm_call` is emitted through `llm_call_fields` with `prompt_hash` and `gate_wait_ms`.
  - (13) The refusal check comes after the charge, as the spec orders.
  - (14)-(17) Append, cost cap (`>`, cause `task_cost`), tool branch through `dispatch` with the `resolve`d registry, then `CONTINUE_NUDGE` with `counts=False`, then `_finalize`, then `step += 1`.
  - (18) A loop signal goes to `hooks.on_loop_signal`. `stop` finishes with `STOP_REASON_BY_CAUSE` and `guard_emitted=True`; `nudge` calls `add_nudge` (counted).
  - (19) `after_step` runs, and a CancelledError propagates.
- U05-59 _build_request ✅ `role_params` come from the role, falling back to the base role. The downgrade WARNING is logged. For wrap-up or final: Anthropic with specs gets `tool_choice="none"` with the specs kept; otherwise `[]` with `"auto"`. The schema and schema name, `messages=list(...)`, and `request_key` `<task>:<step>:final|step` all match.
- U05-60 _finalize / _finish ✅ `_finish` is bound with `functools.partial`. In order it:
  1. emits `guard_stop` when `guard_emitted` is not set;
  2. sets `LoopState._stopping` before `hooks.after_step`, which is the attribute `HarnessHooks._save_due` reads (the carry-over is closed and tested with the real HarnessHooks);
  3. logs INFO `harness.loop.stopped`;
  4. returns `partial`.
  `_finalize` handles the no-output-model case (last assistant text), appends `FINAL_JSON_INSTRUCTION` with `counts=False`, charges and traces, maps a refusal to ModelRefused, and raises "final output missing".
- U05-74 constants ✅ `BUDGET_WARN_RATIO` 0.75, `WRAP_UP_NUDGE`, `CONTINUE_NUDGE`, `FINAL_JSON_INSTRUCTION` and `STOP_REASON_BY_CAUSE` are verbatim; I checked each string against the spec table.
- TH05-07/08 ✅ All three stop paths use the same `_finish`: every limit, the loop signals and the ledger. Tools come only from `tool_registry().resolve` and `dispatch`. ST05-08 (7 tests) covers:
  - a model that repeats forever: stops `repeat_call`, and `run_sql` executes once;
  - a model that never finishes: stops `max_steps` or `no_progress`;
  - a runaway tool caller: bounded to `max_steps-1` executions, and the wrap-up calls are never dispatched;
  - a token runaway: overshoot at most one call;
  - a cost runaway: tools never run;
  - off-registry calls: an unknown name, a registered tool not given to the task, extra, mistyped and missing arguments, and a tool not in the task list. All get `is_error` results, nothing executes, and the request's tool list holds only `run_sql`.
  The threat is genuinely asserted.
- ⚠️ Cannot verify here: the BT05-01 absolute number on CI hardware. Locally it ran 30 tool steps + final, 200 runs, 5 warm-up, with model and tool time subtracted. The report says 0.763 ms and my rerun gave 1.156 ms, far under 20 ms. The gate wait is included (not excluded), so the measurement is conservative.

## Item 5 — the missing `cause` in the ST05-08 guard_stop (root cause)
This is not a loop bug, and not an impl 08 bug. The loop forwards correctly: `HarnessHooks.on_loop_signal` passes the tracer to `loop_signal_policy`, which calls `record_event(detail={"cause", "step"}, tracer=...)`.

`record_event._filter_detail` runs every string through `_clean_str`, i.e. `scrub_secrets` then `redact_text`. In the ST fixture `redact_text("repeat")` raises `ConfigError: secret not found: redact.hmac_key`. `_clean_str` fails closed and returns `_DROPPED`; the debug log `resilience.event.detail_dropped dropped=1 kind=guard_stop` shows this. The `step` field is an int, so it is not redacted and survives.

IT05-04 sees `cause` because its `env` fixture installs a test `Redactor` (test_loop_it.py:80-82). The security `env` fixture does not (tests/security/test_st05_loop.py:37-63). I reproduced this with a temporary probe (restored).

So the weakened assertion hides a missing fixture, not a loop defect. The report wrongly points it at the impl 08 owner. See Minor m1.

## Tests, coverage, gates (re-run by reviewer)
- Card tests: 47 passed. Coverage: loop.py 100% line and branch (106 stmts, 28 branches); _loop_steps.py 100% line and branch (76 stmts, 16 branches).
- tests/unit/harness: 1470 passed, 1 skipped (Windows symlink privilege, pre-existing).
- ruff check: clean. ruff format --check: clean. mypy herness: 0 issues. lint-imports: 13 kept / 0 broken. check_module_size: exit 0.
- Sizes: loop.py 217/220, _loop_steps.py 196/200 (the §2 row was added per ruling), tools.py 383 (≤ 386).
- Test hygiene: every card ID has at least one function, names and docstrings carry the IDs, and `pytestmark` is set everywhere (unit / integration / [integration, slow] for the bench).
- IT05-01 runs the real OpenAICompatClient over `respx_router`, so there is no network. IT05-03..06 use FakeLLMClient / ScriptBook.
- Suppressions acceptable:
  - `# noqa: PLR0913` on `_build_request` (8 params) and `_finalize` (9 params), and `PLR0913, PLR0917` on `run_agent` (7 positional-or-keyword params, fixed by spec 00 §12.3). All three are forced by the spec signatures, carry reasons, and are recorded in the card spec note (global-constraints allow `noqa` with reason).
- tests/support/loop_standin.py (288 lines) is appropriate. It is a shared stand-in (ListClient, recording FakeHooks, the stop_on policy fake, demo_role, loop_ctx) used by the four card test files, and it avoids duplication.
- .secrets.baseline: only `line_number` 2653 moves to 2656 (the docs diff adds 3 lines before it) plus `generated_at`. No audited entries were dropped.

## Mutation probes (card unit + IT + ST tests, -x)
- **Killed (21):**
  - M01 step `>=`→`>`
  - M04 drop deadline
  - M06 drop `_stopping = True`
  - M07 skip ledger charge
  - M08 wrap-up at `max_steps-2`
  - M11 `guard_emitted` False on signal stop
  - M12 CONTINUE_NUDGE `counts=True`
  - M14 dispatch during wrap-up
  - M15 `fresh_conversation` flip
  - M16 identity count flip
  - M18 warn ratio 0.8
  - M19 `_finalize` skips account
  - M20 drop refusal check
  - M21 final `tool_choice` `none`→`auto`
  - M22 `request_key` final→step
  - M24 `_finish` skips `after_step`
  - M25 signal nudge not counted
  - M26 `_finish` skips `guard_stop`
  - M27 wrap ratio `>=`→`>`
  - M30 loop skips `after_step`
- **Survived (10):**
  - M02 tokens `>=`→`>`, M03 wall clock `>=`→`>`, M05 deadline `>=`→`>`, M13 cost `>`→`>=`, M17 compaction hard `>=`→`>`: equality boundaries are untested.
  - M09 wrap-up nudge/event not once, and M10 warn not once: no test has two consecutive wrap-up (or post-warn) steps.
  - M23 hard limits checked before compaction, and M29 token check before the step check: the order is not pinned by a test.
  - M28 `messages` not copied: an equivalent mutant, because pydantic `LLMRequest` validation copies the list anyway.

## Findings
### Critical
None.
### Important
None.
### Minor
- m1 tests/security/test_st05_loop.py:37-63, 99. The ST05-08 fixture lacks the test Redactor that IT05-04 installs (test_loop_it.py:80-82). The spec 08 `record_event` therefore fails closed and drops `cause` from `guard_stop`, and the test asserts only `step`. Fix: install the test Redactor, as IT05-04 does, and assert `[{"cause": "repeat", "step": 3}]`. The report's line blaming impl 08 is inaccurate.
- m2 herness/harness/loop.py:146,150. The "emitted once" behaviour for the wrap-up nudge, the `budget wrap_up` event and the `warn` event is correct, but no test guards it (M09 and M10 survive). Add a case with two steps past the ratio: for example, a wrap-up reply that stops on `max_tokens` followed by another step, asserting one nudge and one event of each kind.
- m3 herness/harness/_loop_steps.py:91-97 and loop.py:115. The spec-mandated check order is not pinned (M23 and M29 survive):
  - compaction before the hard limits;
  - steps before tokens before wall clock.
  Add a test where steps and tokens are exhausted together and expect `max_steps`.
- m4 _loop_steps.py:91-96, 85 and loop.py:124. The limit comparisons have no equality-boundary tests (M02, M03, M05, M13, M17). This is low risk.
- m5 (spec-level, for the controller, not a builder defect) loop.py:126-134. Following step 16 literally, a wrap-up reply that still carries tool calls goes to `_finalize` with those calls left unanswered in the history. Real OpenAI-compatible and Anthropic endpoints may reject an assistant `tool_calls`/`tool_use` with no matching tool result. ST05-08 exercises this path only against fakes. Consider a spec note or ruling, for example appending synthetic error results or dropping the calls.

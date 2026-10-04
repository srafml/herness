# T05-23 report — Agent loop (run_agent, LoopHooks, _build_request, _finalize, _finish)

Worktree D:\herness\.claude\worktrees\agent-a1f8d286923a37ae9, branch worktree-agent-a1f8d286923a37ae9, base 9c8f34a.
Commits: 28e9bf1 wip(T05-23) (loop + unit + IT), final a770b37 feat(harness): T05-23 agent loop run_agent, LoopHooks, finalize/finish.

## Files (lines)
- herness/harness/loop.py 217/220 — LoopHooks, run_agent, _wrap_up, _budget_event, _on_signal, _finalize, _finish, U05-74 loop constants (BUDGET_WARN_RATIO, WRAP_UP_NUDGE, CONTINUE_NUDGE, FINAL_JSON_INSTRUCTION, STOP_REASON_BY_CAUSE); re-exports HarnessHooks, GatedClient; `_build_request` importable as loop._build_request.
- herness/harness/_loop_steps.py 196/200 (NEW private sibling, per ruling) — fresh_state, compact, limit_hit, account (charge + ledger + llm_call + refusal), build_request (U05-59), result.
- herness/harness/tools.py 386 -> 383 — `_LoopHooksLike` deleted; dispatch `hooks: LoopHooks | None` imported under TYPE_CHECKING. _tools_dispatch.py untouched (no stand-in type there). MERGE AGENT: tools.py is net -3 lines.
- docs/impl/05-harness-core.impl.md — §2 row for _loop_steps.py (200) + T05-23 spec note under the card.
- .secrets.baseline — line-number shift of an existing docs/impl/05 entry only (LF kept).
- tests/support/loop_standin.py (ListClient, FakeHooks, demo_role, loop_ctx, resp/final builders)
- tests/unit/harness/test_loop.py (33 tests), tests/integration/harness/test_loop_it.py (6), tests/security/test_st05_loop.py (7), tests/bench/test_harness_loop_bench.py (1)
- tests/fixtures/llm_scripts/analyst_5_steps.yaml

## Tests (47 card tests, all green)
UT05-103..113, UT05-128, UT05-130 (unit); IT05-01..06 (integration; IT05-04 also carries ST08-14); ST05-08 x7 (unit, tests/security); BT05-01 (integration, slow).
- tests/unit/harness: 1470 passed, 1 skipped (Windows symlink privilege, pre-existing).
- Coverage (card tests): loop.py 100% line / 100% branch; _loop_steps.py 100% / 100%.
- BT05-01: mean loop overhead 0.763 ms per step (30-step script, 200 runs, model and tool time subtracted; threshold 20 ms).
- Gates: ruff check pass, ruff format --check pass, mypy 0 issues (294 files), lint-imports 13 kept / 0 broken, check_module_size 0, check_type_ownership 0, --require-test-ids collect OK.
- No full-suite run (per dispatch); pre-commit hooks (incl. pytest-unit) passed on the wip commit.

## Deviations / spec notes (recorded in the spec card note)
- Private sibling _loop_steps.py (ruling allowed); loop.py stays 217.
- `_build_request` (8 params) and `_finalize` (9 params) carry `# noqa: PLR0913` — their U05-59/U05-60 signatures exceed max-args 6; run_agent carries `# noqa: PLR0913, PLR0917` (ruff also flags 7 positional params). These go beyond the spec's "only listed suppression".
- Invalid LLMRequest -> ConfigError("invalid model request") without the pydantic message (may echo prompt text).
- budget event: message = WRAP_UP_NUDGE for wrap_up, null for warn; used/limit cost_usd as strings.
- Loop also logs harness.loop.budget_exceeded (INFO), harness.llm.call_completed (DEBUG), harness.llm.refused (WARNING, defensive path) per §8.1. harness.llm.output_invalid not emitted by the loop (OutputValidationError propagates from hooks / "final output missing").
- A wrap-up reply that still carries tool calls is not dispatched and goes to _finalize (spec step 16 literal); a model that ignores wrap-up then fails the final call with OutputValidationError after complete_validated's repairs (ST05-08 asserts the bound).

## Fixture substitutions
- `tiny_build` does not exist: IT tests use tests/support/warehouse_tools_build (stand-in build) with the real warehouse tools; "fake swarm tools" = a SyncTool post_finding passed as a spec 06 task tool.
- IT05-01 runs the real OpenAICompatClient over respx_router (ScriptRouter, no network). The script matches `dedup_key: "*"` because the adapters send no DD11-02 headers (role would be `*`).
- IT05-05 uses a real ModelChain with a minimal local ChainRegistry/GPU reader; IT05-04/ST05-08 use the real spec 08 loop_signal_policy with a migrated ops store bound as resilience backend.
- UT05-109 wall clock uses FakeClock (freezegun moves clock.monotonic).

## Carry-overs
- CLOSED: _finish sets LoopState._stopping before hooks.after_step (UT05-109 test drives real HarnessHooks: forced save at stop despite the 5 s interval).
- CLOSED: dispatch hooks retype to LoopHooks (w15-s05/w16-s05).
- CLOSED: ST08-14 HarnessHooks integration half — test_it05_04_st08_14_* (real HarnessHooks + spec 08 policy: stops on 2nd signal with guard_stop trace + resilience_event row, one model call per step).
- OPEN (unchanged): public accessor for LoopState._stopping / _wrap_up_sent / _budget_warned (loop writes the private attrs); TracerLike positional-only emit; herness.core.jobs export of save_checkpoint; spec 06 cancel/stop-save ordering (T05-22 M3).
- Not depended on: T05-18, T07-14, T05-20/21 (tests monkeypatch roles.base._prompts_root and _catalog_describe).

## Concerns
- Pre-commit pytest-unit hook takes ~10+ min under host load.
- (Corrected in polish round 1) The missing `cause` in the ST05-08 `guard_stop` event was a test-fixture gap, not an impl 08 defect: the ST05-08 fixture lacked the test Redactor, so `redact_text` failed ("secret not found: redact.hmac_key") and `record_event` failed closed by dropping the string field. With the Redactor installed as in IT05-04, the test asserts `{"cause": "repeat", "step": 3}` exactly.

## Polish round 1 (review m1-m5; test-only plus one spec note) — commit af118cc
- m1: ST05-08 fixture installs the test Redactor; guard_stop asserted exactly as `{"cause": "repeat", "step": 3}`.
- m2: UT05-105 two steps past both ratios (nudge once, `wrap_up` and `warn` once) and two steps past only the warn ratio (`warn` once).
- m3: UT05-109 check order: compaction staying over hard beats `max_steps` (cause `context`); steps+tokens together -> `max_steps`; tokens+wall clock together -> `task_tokens`.
- m4: UT05-109 equality boundaries: step == max_steps, tokens_used == max_tokens, elapsed == wall_clock_s all stop; cost == max_cost_usd does not stop.
- m5: spec note sentence under T05-23 (open question for the spec owner: a wrap-up reply that still carries tool calls goes to `_finalize` with unanswered calls; real endpoints may reject that history; behaviour unchanged).
- Mutation probes (files restored, loop.py/_loop_steps.py unchanged): M09, M10, M23, M29 KILLED; boundary mutants steps/tokens/wall `>=`->`>` and cost `>`->`>=` KILLED.
- Card tests now 53 (unit 39, integration 6, security 7, bench 1). Module sizes unchanged (loop.py 217, _loop_steps.py 196).

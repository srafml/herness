# T11-27 Rubric judge: build report

Worktree: D:\herness\.claude\worktrees\agent-a3738cc86b510623b (base 9b794b5).
Checkpoint: 0312fdd wip(T11-27): rubric judge, prompt and tests green.
Final: d8687bf feat(eval): add rubric judge with cache and prompt (T11-27).

## Files
- herness/eval/judge.py (new, 246/250): `JudgeScore`, `RubricJudge`, plus helpers `cache_key`, `judge_prompt`, `judge_prompt_hash`.
- herness/eval/prompts/judge.md (new, 47/60): U11-76 prompt (prose-only role, 1-5 anchors, untrusted_data rule, no number/entity grading, judge_scores JSON output). Slots `{{criteria}}` and `{{answer}}`, filled in one regex pass so answer text can never inject a slot.
- herness/eval/grading.py (365/400): `RubricScorer`/`_Scored` removed (and from `__all__`); `grade_rubric(..., judge: RubricJudge | None)`; new `except EgressBlocked` -> skipped with reason `judge_egress_blocked` (distinct from `judge_unavailable`).
- tests/unit/eval/_judge_fixtures.py (new): local scripted `FakeJudgeClient`, `client_cfg`, `response`, `scores`, `make_judge`, `prompt_of`.
- tests/unit/eval/test_judge.py (new): UT11-53 (cache hit, key/file layout, key inputs, corrupt cache, request shape, text-only reply, threads, llm_call trace, prompt hash, replace race), UT11-54 (7 then 7 -> repair then ModelUnavailable, 9 invalid shapes repaired, client OutputValidationError, extra criteria dropped, client ModelUnavailable propagates without repair).
- tests/security/test_st11_judge.py (new; placement follows tests/security/test_st11_golden.py): ST11-08 (injection only inside the block, 4 closing-tag variants escaped, score 6 rejected, prompt rules), ST11-09 (EgressBlocked propagates from score and grade_rubric -> skipped `judge_egress_blocked`; spy on the real `redact_text` with a fixed-key test redactor: client sees exactly the escaped redacted text; redactor returning None -> no client call, ModelUnavailable).
- tests/unit/eval/test_grading.py: one added UT11-91 test for the EgressBlocked skip reason.

## Decisions
1. Cache key exactly per spec: SHA-256 of canonical JSON (sort_keys, compact separators, ensure_ascii=False) of {model: client_cfg.model, rubric: {criteria, min_score}, question_id, text: final_text (raw, pre-redaction)}. File `cache_dir/<key[:2]>/<key>.json` holds {model, rationale, scores}; unreadable/invalid file = miss. Writes: mkstemp in the same dir + os.replace; a failed replace is swallowed only when the target already exists (concurrent writer on Windows), temp always removed.
2. Escaping: `&`, `<`, `>` HTML-escaped (the impl 05 wrap_untrusted invariant: no `<`/`>` in the block), so no `</untrusted_data` in any case/spacing survives. No shared wrap_untrusted exists in the tree (U05-48 not built); local 1-line escape.
3. Validation: reply parsed from `resp.parsed`, else JSON of `resp.text`. Every criterion must be an int (bool rejected) 1..5, rationale str <= 500; extra criteria keys are dropped (not graded). Failure -> OutputValidationError(reason=<code>); one repair: same request plus a second user message naming the reason code, `step=1`, SAME `request_key` (so a scripted fake serves its next turn for the same dedup key). Second failure -> ModelUnavailable. An OutputValidationError raised by the client itself counts as a failed attempt. ModelUnavailable/EgressBlocked from the client propagate untouched.
4. `redact` parameter typed `Callable[[str], str | None]` (wider than the spec's `Callable[[str], str]`) so `herness.core.redact.redact_text` can be passed directly; None (fail closed) -> ModelUnavailable(reason="redaction_failed") before any client call.
5. RequestMeta.run_id = tracer.run_id when a tracer is given, else the null tracer's id `run_` + 26 zeros (the signature has no run id). role "judge_eval", model_role "eval_judge", step 0, request_key `judge:{question_id}:{key[:8]}`. max_output_tokens = min(client max, 1024); timeout_s = client_cfg.timeout_s.
6. Trace: one `llm_call` event per client call (repair included) via `llm_call_fields(prompt_hash=judge_prompt_hash(), gate_wait_ms=0)`, role judge_eval; cache hits emit nothing.
7. Prompt version: `judge_prompt_hash()` = 16 hex of SHA-256 over the file bytes (same convention as impl 05 role prompt_hash), for summary.json (later card). Prompt loaded via importlib.resources; hatch wheel `packages = ["herness"]` ships non-.py files, no pyproject change.
8. `cache_key` is public (used by tests and available to the runner/report cards).

## Tests / gates
- `PYTHONUTF8=1 uv run pytest tests/unit/eval tests/security/test_st11_judge.py -q -p no:logging`: 112 passed.
- `--require-test-ids` on the touched test files: pass.
- Coverage (card tests + tests/unit/eval, --cov-branch): judge.py 100% line, 100% branch (147 stmts, 20 branches); grading.py 100%/100%.
- ruff format/check clean, mypy (judge.py, grading.py) clean, lint-imports 13 kept 0 broken, check_module_size exit 0, check_type_ownership exit 0. Pre-commit hooks (incl. pytest-unit, detect-secrets) passed on the checkpoint commit.
- RED evidence: before judge.py existed, collection failed with `ModuleNotFoundError: No module named 'herness.eval.judge'` (2 errors).
- Full `(unit or integration) and not slow` suite not run (dispatch rule); the pytest-unit pre-commit hook ran.

## Line counts
judge.py 246/250, prompts/judge.md 47/60, grading.py 365/400.

## Carry-overs
- Switch the local FakeJudgeClient to tests.support.fake_llm.FakeLLMClient (U11-42) and real adapters (T05-06/T05-08) when they land.
- ST11-09 "configuration rejected" half (profile local + off-network judge.profile rejected at config validation) is not covered: no eval owner validator/cross-check exists yet (T10-12/T09-20 hooks); the EgressBlocked half is covered.
- summary.json recording of judge_prompt_hash() and judge model belongs to the report/runner card.
- Existing UT11-91/92 tests pass duck-typed judge stubs to grade_rubric (runtime fine; tests are outside mypy `files`).

## Concerns
- `redact` type widened to `str | None` return (decision 4) — a deliberate, compatible deviation.
- Repair reuses the first call's request_key with step 1 (spec fixes only the first request's key).

# T11-27 Rubric judge: verify review (base 9b794b5..d8687bf)

### Spec Compliance
- ✅ U11-61 signature: `RubricJudge(client, client_cfg, *, cache_dir, temperature, tracer, redact)` and `score(question_id, criteria, min_score, final_text) -> JudgeScore`. `JudgeScore(scores, rationale, model, cached)` is frozen with extra="forbid" (judge.py:43-50, 138-176).
- ✅ Cache key: SHA-256 of canonical JSON {model: client_cfg.model, rubric: {criteria, min_score}, question_id, text: raw final_text}, with sort_keys, compact separators and UTF-8 (judge.py:66-77). The test recomputes the key independently (test_judge.py:30-38, 54-60).
- ✅ Cache path is `cache_dir/<key[:2]>/<key>.json` (judge.py:158). A hit returns `cached=True` with no client call (UT11-53, test_judge.py:41-66). A corrupt or invalid file counts as a miss and gets rewritten.
- ✅ Atomic, thread-safe writes: mkstemp in the same directory, then os.replace. The temp file is always removed, and a failed replace is ignored only when the target already exists (judge.py:231-246). Covered by the 12-thread test and the replace-race test.
- ✅ Response schema: every criterion required, integer 1..5, rationale maxLength 500, additionalProperties false (judge.py:90-99). Range and type validation rejects bool, str, missing and out-of-range values (judge.py:110-122).
- ✅ Exactly one repair, then `ModelUnavailable` (judge.py:167-174). UT11-54 (7 then 7) makes 2 calls and writes no cache file. A client-raised `OutputValidationError` uses up the repair. Client `ModelUnavailable` propagates without a repair.
- ✅ `EgressBlocked` propagates from `score`. grading.py:286-288 maps it to `skipped` with the distinct reason `judge_egress_blocked` (controller ruling implemented). `RubricScorer`/`_Scored` are replaced by `RubricJudge` (controller ruling implemented).
- ✅ The untrusted block opens with exactly `<untrusted_data source="eval_answer" record_id="">` and closes with `</untrusted_data>` (judge.py:36-37, 85). The injection text appears only inside the block. `</untrusted_data` in any case or spacing cannot survive the escaping (ST11-08).
- ✅ The client receives the redacted text: `redact` runs before rendering, and the ST11-09 spy on the real `redact_text` checks that the block equals the escaped redacted text (test_st11_judge.py:112-133).
- ✅ Request: client name, a single system prompt, user "Score the answer.", `response_schema_name="judge_scores"`, temperature, and RequestMeta(task_id=None, role judge_eval, model_role eval_judge, step 0, request_key `judge:{qid}:{key[:8]}`) (judge.py:178-202).
- ✅ Trace: one `llm_call` event per client call through `llm_call_fields` with prompt_hash, and none on a cache hit (judge.py:213-217, UT11-53 trace test with a real Tracer).
- ✅ U11-76 prompt: grades prose quality only; 1-5 anchors; the listed criteria; the untrusted_data block is data and instructions inside it are ignored; says not to grade numbers or entities; requires `judge_scores` JSON output. `judge_prompt_hash()` is available for summary.json.
- ✅ Budgets: judge.py 246/250, judge.md 47/60, grading.py 365/400.
- ✅ Tests: UT11-53, UT11-54, ST11-08 and ST11-09 are present. Names and docstrings carry the IDs, `pytestmark = pytest.mark.unit` is set (matching the other tests/security files), and `--require-test-ids` passes.
- ✅ Error messages carry only ids and reason codes, with no answer text (judge.py:129-130, 164-165, 173-174).
- ⚠️ ST11-09 "configuration rejected" half is not covered. The expectation reads "configuration rejected **or** `EgressBlocked` → skipped", and the EgressBlocked branch is covered, so the row is met. The config cross-check is a carry-over to the owner validator card.
- ⚠️ The `eval.judge.skipped` log event in the error table (spec ~1346-1347) is emitted nowhere in the tree. Its site is `grade_rubric` (U11-59, card T11-26), so it is outside this card, but the controller should track it.
- ⚠️ The spec's real FakeLLMClient (U11-42) and real adapters are not in the tree. A local `FakeJudgeClient` stands in (controller ruling; carry-over).

### Declared deviations (assessed)
- `redact: Callable[[str], str | None]`, where None raises ModelUnavailable(reason="redaction_failed") before any client call. Accepted: it is compatible with `redact_text(str|None) -> str|None` (core/redact.py:306) and fails closed. Tested.
- `RequestMeta.run_id` comes from tracer.run_id, or from the null tracer's zero id `run_`+26 zeros when there is no tracer. Accepted: the signature has no run id, and in the eval run it matches the run's tracer.
- Escaping `&`, `<`, `>` rather than only `</untrusted_data`. Accepted: it is stricter and matches the impl 05 invariant. ST11-09's "equals the redacted text" becomes "equals the escaped redacted text", which is unavoidable because escaping is spec-mandated.
- The repair uses step=1 with the same request_key. Accepted: the spec fixes only the first request's key. The only request_key use in herness is the type (no dedup cache in the clients yet). Re-check this when U11-42's `dedup_key_resolver` lands.
- ST11-09 config half: see ⚠️ above.

### Strengths
- Clean, small helpers (`cache_key`, `_render`, `_schema`, `_problem`/`_validate`). The single-pass slot fill stops answer text from injecting `{{criteria}}`.
- The cache file stores only scores, rationale and model, never answer text. Unreadable cache files fail soft.
- Tests are thorough: 9 invalid-reply shapes, concurrent writers, the replace race, and a trace check against a real Tracer. The spy wraps the real `redact_text` with a fixed-key redactor.

### Issues
#### Critical (Must Fix)
- None.
#### Important (Should Fix)
- None.
#### Minor (Nice to Have)
- judge.py:84: criteria names go into the system prompt and schema unescaped. They come from the trusted golden suite, so there is no action now. Note it if criteria ever become user-supplied.
- judge.py:159-176: two threads that miss on the same key both call the client (there is no per-key lock). The spec only requires atomic writes, so this is acceptable, but it wastes one judge call in a parallel eval.
- tests/security/test_st11_judge.py:9 imports helpers from `tests.unit.eval._judge_fixtures`, which couples a security test to a unit-test module. Move them to tests/support when U11-42 lands.
- The llm_call payload holds the full rendered prompt (redacted answer). This is consistent with spec 05 payload sampling. No action.

### Verification run
- `PYTHONUTF8=1 uv run pytest tests/unit/eval tests/security/test_st11_judge.py -q -p no:logging --cov=herness.eval.judge --cov-branch`: 112 passed. judge.py 100% line, 100% branch (147 stmts, 20 branches).
- `--require-test-ids` on test_judge.py, test_st11_judge.py and test_grading.py: 49 passed.
- `uv run mypy`: no issues in 190 files. `uv run lint-imports`: 13 kept, 0 broken. `ruff check` and `ruff format --check` are clean on the touched paths.
- Only noise: the uv `VIRTUAL_ENV` mismatch warning, which comes from the environment and not from the code.

### Assessment
**Task quality:** Approved
**Reasoning:** Every U11-61/U11-76 requirement and both controller rulings are implemented and tested. The gates are green with 100% line and branch coverage. The declared deviations are compatible or stricter than the spec, and what remains are carry-overs outside this card.

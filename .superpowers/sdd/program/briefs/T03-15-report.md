# T03-15 report — LLM decider and prompts (build)

Status: DONE_WITH_CONCERNS (spec-literal deviations below, all small)
Worktree/branch: D:\herness\.claude\worktrees\agent-a1ec505ecf7351f42 / worktree-agent-a1ec505ecf7351f42, base 55f2c48
Commits: 06a1c35 wip(T03-15): LLM decider, prompt files and card tests (all content); final card-subject commit: see bottom.

## Implemented
- `herness/enrich/deciders/llm.py` (344/350): `CompletionClient` (runtime-checkable Protocol: `name: str`, `complete`, `acomplete`; no herness.harness import, R-05), `vote_schema` (U03-61, exact postcondition shape), `vote_distribution` (U03-62, Laplace, label order, vote outside labels -> OutputValidationError("vote outside labels")), `load_prompt(path) -> (header, 5 paraphrases)` (ConfigError "prompt file <name> missing|malformed", file name only), `LlmDecider` (U03-63/64).
  - decide: `asyncio.run`; `asyncio.Semaphore(max_concurrency)` over items; the votes of one item run in turn inside its slot (so <= max_concurrency model calls in flight = the client's max_concurrency); TaskGroup, first backend failure re-raised (openjev pattern).
  - Each vote: LLMRequest per step 2 (client=client.name, system=[SystemBlock(header)], one user message, schema `enrich_votes`, temperature, seed=i, thinking off, metadata role/model_role enrich_decider, step=i, request_key `<content_hash>:<i>`), sent via `aretry_call("llm_local", complete_validated, client, req, max_repairs=2, breaker_key="decider:llm")`. Only `OutputValidationError` drops the vote; every other error (ModelUnavailable, CircuitOpen, AuthError, EgressBlocked, also ModelRefused) propagates. Vote read from `response.parsed`, else its (already validated) decoded text (same rule as complete_validated's `_json_of`).
  - >= 1 valid vote: `vote_distribution`, argmax with ties -> first label (`max` keeps the first), probability = dist[answer], backend_confidence None. 0 valid: `DecisionOutput(answers={}, error="OutputValidationError")` + WARNING `enrich.decide.item_failed` (decider, error_class). Dropped vote: INFO `enrich.decide.vote_dropped` (decider, record_id, vote index). No ticket text in logs or errors.
  - Metrics as §8 lists for U03-63: `herness_enrich_decider_latency_seconds{decider}` per vote (timed), `herness_enrich_decider_errors_total{decider,error_class}` per failed vote (constants imported from openjev.py; the `_asked` shared rule imported from openjev.py as jev_hosted does).
  - "outputs record samples = votes": DecisionOutput has no samples field (the cache writer takes `samples=` separately), so `LlmDecider.samples` (read-only property = votes) is what the caller passes to `add(outputs, samples=decider.samples)`.
  - Constructor: votes in {1,3,5} (bool refused) else ConfigError; max_concurrency >= 1 else ConfigError; version validated against the DecisionOutput.decider_version field (TypeAdapter, jev_hosted precedent) else ConfigError; prompt file loaded once (fail fast).
  - health: `call_with_timeout(lambda: client.complete(req), 30.0)` with a 1-token request (max_output_tokens=1, temperature=0, thinking off, no schema); any Exception -> ModelUnavailable("llm health").
- Wrapping (R-20, ST03-01): `<untrusted_data source="enrich.text_redacted" record_id="<rid>">text</untrusted_data>`; every `</untrusted_data` in text -> `&lt;/untrusted_data` BEFORE wrapping, matched case-insensitively (original case kept, e.g. `&lt;/UNTRUSTED_DATA`) — hardening beyond the literal spec. Record id: `html.escape(record_id, quote=True)`, so `"`, `<`, `>`, `&`, `'` cannot close the attribute or the tag (DecisionInput.record_id is any 1-256 char string); covered by a UT03-61 test.
- User message = paraphrase (i mod 5) + blank line + "Questions:" list (`- id: <qid>; type: <type>; instructions: <text>`, then `  - <label>: <description>` for choice options, `  - <0..3>: <level>` for score, `  - true` / `  - false` for bool) + "Ticket:" + the block (last).
- `herness/enrich/prompts/enrich_decider.md` (61/80): a leading HTML comment documents the format (never sent), `## System` header (task, standing untrusted-data instruction naming the exact delimiter, rules, output), `## Paraphrase 1..5`. The loader requires exactly these headings in order with non-empty bodies.
- `herness/enrich/prompts/cluster_namer.md` (32/50): prompt file only (call site is a later card, U03-102); same format comment, `## System`, same standing untrusted-data instruction (record_id=""), 60-char label rule, optional root_cause_category, JSON-only output. No loader here.
- Packaging: hatch `include = ["herness", ...]` ships the .md files; default path `Path(__file__).parents[1]/"prompts"/"enrich_decider.md"` (spec: `prompt_path: Path`). No __init__.py in prompts/ (data dir).
- Tests: `tests/unit/enrich/_fake_llm.py` (test-local FakeLLMClient + `votes_by_seed`, carry-over note to U11-42 per ruling), `tests/unit/enrich/test_llm_decider.py` (UT03-58..UT03-62, PT03-06 hypothesis `@given` like PT01-01/PT03-04), `tests/unit/enrich/security/test_llm_decider_security.py` (ST03-01, marker unit), UT03-45 table in test_decider_protocol.py extended with `_llm`. Resilience env: the `jev_env` fixture (ops-store resilience backend + full test config), so votes run through the real aretry_call / complete_validated / repair loop.

## Deviations / rulings needed
1. RequestMeta.run_id is `str` (not Optional): spec step 2 `run_id=None` fails validation. Used `"run_" + "0"*26` (the null-tracer run id the eval judge uses). Spec note to U03-63.
2. LLMRequest requires `max_output_tokens` and `timeout_s` (spec silent): 2,048 tokens per vote, 120 s per call (constants `_MAX_OUTPUT_TOKENS`, `_CALL_TIMEOUT_S`); health uses 1 token / 30 s. Could later come from the client's ClientConfig via the composition root.
3. `vote_schema` returns `dict[str, JsonValue]` (spec: `dict[str, object]`) because LLMRequest.response_schema is `dict[str, JsonValue]`.
4. `samples = votes` exposed as `LlmDecider.samples` (DecisionOutput has no samples field).
5. `ModelRefused` (not named by the spec) propagates like the other backend errors, not an item error.
6. Small fixed strings in code ("Questions:", "Ticket:", health ping "Reply with OK.") are message scaffolding, not prompts; all instructions live in the prompt files.
7. `load_prompt` is public (in __all__) so tests and the later cluster-naming card can reuse the format; the spec lists only 4 exports.

## Concerns / carry-overs
- FakeLLMClient (impl 11 U11-42) absent -> test-local fake (ruled); re-point later.
- Dynamic choice options (options_source core.team/core.service) are rendered outside the untrusted block like static option descriptions (the spec puts them in the question list); team/service names come from source systems — consider wrapping or sanitising in a later card.
- Environment: C: is full (~0.9 GB free); pre-commit pytest-unit fails UT01-50 (sparse 1 GiB file, ENOSPC) with the default TEMP. Commits were made with TMP/TEMP=D:\tmp-herness-T03-15 (outside the repo; a TEMP inside the worktree breaks UT11-37's pytester rootdir). All hooks passed, no SKIP, no --no-verify. Temp dir removed afterwards.

## RED evidence
`PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_llm_decider.py tests/unit/enrich/security/test_llm_decider_security.py -q -p no:logging`
-> `ModuleNotFoundError: No module named 'herness.enrich.deciders.llm'` (2 collection errors).

## GREEN evidence
- Card tests + UT03-45 table: 43 passed; coverage llm.py 100 % line, 100 % branch (167 stmts, 26 branches).
- `PYTHONUTF8=1 uv run pytest tests/unit/enrich -q -p no:logging`: 675 passed, 1 skipped (symlink privilege).
- pre-commit on 06a1c35: all hooks passed incl. ruff-check, ruff-format, mypy, import-linter, detect-secrets (no baseline change needed), module-size, type-ownership, pytest-unit (full unit suite).
- ruff format --check (621 files) / ruff check: clean; mypy (243 files): 0 issues; mypy --explicit-package-bases on the 4 test files: 0 issues; lint-imports: 13 kept, 0 broken; check_type_ownership 0; check_module_size 0.

## Line counts vs budgets
llm.py 344/350; enrich_decider.md 61/80; cluster_namer.md 32/50.

## Final commit
615b7d2 feat(enrich): LLM decider and prompt files (T03-15) — empty card-subject commit on top of wip 06a1c35 (all content); hooks green, pytest-unit passed, no SKIP. Worktree clean.

## Fix round 1 (test-only)
Review minors m1-m4 closed with tests in tests/unit/enrich/test_llm_decider.py; no change to herness/.
- m1 `test_ut03_61_calls_in_flight_bounded_by_max_concurrency[1|2|3]`: an async fake (`_SlowClient`, awaits inside `acomplete`) records peak in-flight calls over 7 items x 3 votes; asserts peak == max_concurrency (so <= bound, and >1 reached for 2 and 3).
- m2 `test_ut03_61_backend_errors_propagate[auth|model_unavailable|circuit_open|egress_blocked]`: each propagates out of decide as its own class, no vote_dropped event. Runs with max_concurrency=1: with 2 items in flight, retried ModelUnavailable calls of both items can open the `decider:llm` breaker first, so decide then raises CircuitOpen (observed ~1 in 3 runs) - intended resilience behaviour, not a bug, but callers should expect CircuitOpen after a burst of ModelUnavailable.
- m3 `test_ut03_62_dropped_vote_and_item_error_logs_carry_no_text`: structlog capture_logs over one dropped vote and one all-invalid item; no event contains either ticket text.
- m4 `test_ut03_61_votes_run_under_the_decider_breaker`: spy wrapping the real aretry_call records ("llm_local", breaker_key="decider:llm") for every vote.
Mutants (in place, restored with git checkout, herness/ clean after): semaphore removed, semaphore +1, votes of an item gathered concurrently, all errors swallowed, ModelUnavailable swallowed, text= on vote_dropped, text= on item_failed, breaker_key=None - all 8 killed.
Checks: test_llm_decider.py 40 passed (6 repeat runs stable); ruff format/check clean; mypy clean on the test file.

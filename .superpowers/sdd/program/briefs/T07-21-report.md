# T07-21 report — Chat session memory (build)

Worktree: D:\herness\.claude\worktrees\agent-a69071b7c76db2d7e (branch worktree-agent-a69071b7c76db2d7e, base e41d62c)
Checkpoint: wip(T07-21) commit (prompts, loader, chat.py). Final commit: see below / git log.

## Built
- `herness/harness/memory/chat.py` (329/330): `ChatDeps` (frozen dataclass: cfg ChatMemoryConfig, llms ChatModels Protocol = LLMRegistry subset model_for/client/config, writer Proposer Protocol = MemoryWriter.propose, redactor Redactor, allowed numeral patterns, optional tracer; __post_init__ reads both prompts -> ConfigError), `session_load` (U07-93), `session_save_turn` (U07-94, sync wrapper: asyncio.run, or with a running loop a 1-worker ThreadPoolExecutor + asyncio.run waited 120 s -> on expiry WARNING memory.session.save_timeout, None), `capture_correction` (U07-95). Both model calls: chat client `model_for("chat","fast")` via `complete_validated(max_repairs=0)` over private `_Caller` (asyncio.wait_for profile.timeout_s, llm_call trace with prompt_hash, refusal -> ModelRefused). Chat text (and prior summary) escaped + wrapped once in `<untrusted_data source="chat">`. Summary post-processing: every double-bracket marker and every uncited numeral -> `[number]`, redact, cut at last whitespace to summary_max_chars; blank -> not written. Classification -> MemoryProposal via propose only (chat -> pending). PolicyViolation -> INFO memory.correction.rejected {session_id, rule}. SQLite calls in async code via asyncio.to_thread. No SQL in chat.py; logs carry ids/counts/status/rule only.
- `_compactor_llm.py` (174/200): cached `memory_prompt(file_name)` (bare name only; missing/unsafe -> ConfigError; (text, 16-hex sha256)); `compaction_prompt()` delegates (hash unchanged, asserted in UT07-86); docstring updated. `PROMPT_FILE` is now the bare name.
- Prompts: `prompts/chat_summary.md` (35 lines), `prompts/correction_classify.md` (36 lines); required sentence verbatim + U07-99 requirements; no dotted tokens/hosts/secrets.
- Tests: `tests/unit/harness/memory/test_memory_chat.py` (UT07-81, UT07-82, UT07-83, UT07-86; 28 functions, parametrized), helper `tests/unit/harness/memory/_chat_env.py`, `tests/security/test_st07_chat.py` (ST07-22 x4: other session -> None/no call/nothing stored + writer rule provenance.session; other user -> rejected provenance.session; injection in message -> escaped in one wrapper, echoing classification -> PENDING + review item only; summary path wraps injected messages and post-processes echoed reply, logs free of chat text).
- Existing tests touched (needed by the loader rename): `tests/security/test_st05_prompts.py` _EXPECTED_COUNT 15 -> 17 (comment names U07-99/T07-21) and checks the two new names; `tests/unit/harness/memory/test_memory_compactor.py` UT07-62 missing-file test now patches `PROMPT_FILE="missing.md"` and clears `memory_prompt` cache; `tests/unit/harness/test_tools_recording.py` UT05-124 hash allow-list entry `compaction_prompt` -> `memory_prompt` (the sha256 call moved function).
- pyproject: hatch artifacts already ship `herness/harness/memory/prompts/*.md` (confirmed, not edited). UT05-94 reader list unchanged (chat.py has no `prompts` literal).

## Spec notes
- 07-memory.impl.md: after U07-94 (summary schema {summary 1..summary_max_chars}, max_repairs 0, window/cut rules, empty summary, no-assistant-row skip, timeout behaviour, to_thread); after U07-95 (ChatDeps definition, call wrapper, schema bounds, empty statement fallback, extra `now` kw on capture_correction, ownership checked by propose 6d); after U07-99 (memory_prompt loader, UT05-94/ST05-21 effects).
- 05-harness-core.impl.md: one-line addendum under the T07-14 merge-fix note (memory_prompt, ST05-21 = 17, UT05-124 entry).

## Evidence
- Card + neighbour run: `pytest tests/unit/harness/memory tests/security/test_st05_prompts.py tests/unit/harness/roles/test_roles_prompts.py tests/unit/harness/test_tools_recording.py tests/security/test_st07_*.py` -> 715 passed (148 s).
- Coverage: chat.py 100 % line / 95 % branch (1 partial: tracer None), _compactor_llm.py 100 %/100 %.
- RED: first run of the new tests had 10 failures (log key `component`, sampling-param fixture, cut expectation, ST assertion on prompt text) — all test-side, fixed; ST05-21 red at 17 vs 15 before the count update.
- Gates: ruff check/format clean, mypy (project) clean + new tests clean with --explicit-package-bases, lint-imports 13 kept, check_module_size 0, check_type_ownership 0.

## Deviations / concerns
- `capture_correction` has an extra keyword `now` (passed to propose for the rate window) — spec note added.
- `max_repairs=0` for both calls (spec has no repair; avoids needing the resilience ops backend for repair events).
- No role_params applied (temperature 0 where sampling supported for both calls; thinking/effort defaults).
- On the 120 s sync-wait expiry the worker thread is not cancelled (it may still finish the write later).
- Messages in the summary window are cut to 4,000 chars each (LLM10) — not in the spec, noted.
- Three existing tests edited outside the listed files (UT07-62 one-liner, UT05-124 allow-list entry) because the loader generalisation moves the hash call into `memory_prompt`.

## Final
- Commits: 9deda78 wip(T07-21) checkpoint; 366363b feat(memory): T07-21 chat session memory and correction capture (all pre-commit hooks passed, no SKIP; first attempt refreshed .secrets.baseline line numbers for the docs shift only, normalised to LF and committed with the card).
- Temp folder C:\Users\santh\AppData\Local\Temp\w24-s07d-build deleted.

## Fix round 1 (review T07-21-review.md, head 366363b)
- I1: new UT07-82 test `test_ut07_82_turn_message_is_latest_user_row_before_the_answer` (an answered turn, an older unanswered user row, the run's user row + answer, a later user row): asserts stored provenance.source_message_id is the latest user row before the run's assistant row and the classification request carries only that text. Mutant `users[-1] -> users[0]` in `_turn_message`: KILLED (1 failed, 50 passed), source restored.
- m1: `_post_process` cuts at `min(cfg.summary_max_chars, SUMMARY_MAX_CHARS=6000)`; test `test_ut07_82_summary_clamped_to_store_limit` (summary_max_chars=8000, 7,800-char output -> stored 5,990..6,000 chars, no SchemaViolation).
- m2: `_session` returns MemoryNotFound for session_id > 64 chars without calling get_chat_session (covers session_load and session_save_turn); test `test_ut07_81_overlong_session_id_without_store_call` (get_chat_session patched to fail).
- m3 parked. Spec note on U07-94 extended (clamp, length guard).
- chat.py 330/330 (one line saved by folding the redact result into a walrus). Card run: 599 passed; chat.py coverage 100 % line, 1 partial branch (tracer None, parked). mypy, lint-imports, check_module_size, ruff check/format clean.

## Fix round 2 (gate failure of test_ut07_82_turn_message_is_latest_user_row_before_the_answer)
Cause: the gate imported a mutant `chat.py`, not a defect in the code or the test. The re-review re-ran mutation M18 (`users[-1]` -> `users[0]` in `_turn_message`) on head 0f99493 in this same worktree, and the gate suite was collecting in that worktree at the same moment.
- Gate timing: the suite took 1435 s and its log ends 23:36:47, so it started about 23:12:52.
- `herness/harness/memory/chat.py` was rewritten at 23:13:46 (mtime) with content identical to HEAD; `git diff HEAD` is empty. That is the re-reviewer's byte-for-byte restore. T07-21-fix1.diff was written at 23:12:11 and T07-21-review.md, which reports "M18 now KILLED", at 23:16:45.
- The gate process imported `chat.py` while the mutant was in place. `__pycache__/chat.cpython-312.pyc` was rewritten at 23:14:05, after the restore.
- The observed pick matches M18 exactly. msg_...VVNP is the session's first user row: the session was VVNM, then run1 VVNN, then the first user message VVNP in the same millisecond, sequential ULIDs. That is `users[0]`, not an ordering tie.
Evidence that the committed code and test are deterministic:
- (a) The suite in gate order with IT00-01 deselected: 9264 passed, 13 skipped, 2 xfailed (21:36). IT00-01's own run in a first attempt timed out and killed the pytest process, so it was excluded.
- (b) tests/security plus test_memory_chat.py: 799 passed.
- (c) tests/unit/harness: 1948 passed.
- (d) The test repeated 300 times (temporary parametrize, reverted): 300/300 passed.
- The pick depends only on created_at (distinct per Clock tick), message_id and run_id, and nothing in it depends on time or ordering ties.
No code or test change was made and nothing was committed; head stays 0f99493. Process note for the controller: mutation runs on a worktree must not overlap a gate or suite run on the same worktree. Mutating a copy of the tree avoids the race.

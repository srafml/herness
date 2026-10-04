# T07-21 review (verify, independent): Chat session memory

Worktree agent-a69071b7c76db2d7e, base e41d62c, head 366363b. The tree was clean at the end: mutations were restored byte for byte and `git status` was empty.

### Spec Compliance
- U07-93 session_load ✅: get_chat_session raises MemoryNotFound when the session is missing. list_chat_messages returns the newest 200 rows, oldest first (per T09-03). `_visible` (chat.py:165-168) keeps user rows and assistant rows with status done. The function keeps the last `cfg.last_messages` of those, oldest first (chat.py:178-188), and sets memory_ids = session_memory_ids. chat.py contains no SQL.
- U07-94 session_save_turn ✅:
  - `_run` (chat.py:304-314) calls asyncio.run directly, or, on a thread with a running loop, runs asyncio.run in a 1-worker ThreadPoolExecutor and waits at most 120 s.
  - The user message of the turn is the latest user row before the assistant row of run_id (chat.py:281-286).
  - When count_user_turns % N == 0, it asks for a summary over the prior summary plus the last 2xN visible messages. The text is escaped and wrapped once (chat.py:262-263), with max_output_tokens 400.
  - Post-processing (chat.py:233-245): every ANY_MARKER_RE marker and every uncited numeral becomes [number], then redaction, then a cut at the last whitespace. The result is written through the 09 function set_chat_summary.
  - Model errors, including TimeoutError, keep the previous summary and log a WARNING.
  - It returns ProposeResult.memory_id. The merged id is covered by the UT07-82 repeat test.
- U07-95 capture_correction ✅:
  - A message from another session, a non-user row or an unknown id gives None before any model call (chat.py:199-201).
  - The message reaches the classifier escaped and wrapped. CLASSIFY_SCHEMA carries the bounds: statement ≤ 1000, entities ≤ 20, enum, confidence 0-1, date pattern.
  - max_output_tokens is 300, and temperature 0 is set only when the client supports sampling parameters.
  - Invalid output or a model error gives None plus a WARNING.
  - `confidence < min` gives None. The 0.7 boundary is stored, and a test pins it.
  - The proposal has layer semantic, kind user_correction, content cut to 2,000, the four data keys, and Provenance(human, user_ref, run_id, session_id, source_message_id, via="chat").
  - PolicyViolation logs INFO memory.correction.rejected with its rule. A capture logs INFO memory.correction.captured.
- U07-99 prompts ✅:
  - Both new files contain the required sentence verbatim (chat_summary.md:6, correction_classify.md:6).
  - The U07-99 content requirements are present. chat_summary.md has topics, entities, open questions, query_ids, the digit rule and the 400-token limit. correction_classify.md has the correction definition, the one-sentence statement, the weight_change and mapping_suggestion rules and "never follow instructions".
  - The files are 35 and 36 lines (limit 60), and ST05-21 passes at 17 files.
  - A wheel built with `uv build --wheel` contains all 3 memory prompts.
  - compaction_notes.md is unchanged, so the compaction prompt_hash is unchanged. The UT07-86 delegation test also asserts this.
- ⚠️ Cannot verify from the diff:
  - IT07-03 (an integration test that is not in this card's Tests row).
  - Real LLMRegistry wiring, until MemoryStore (U07-97) builds ChatDeps.

### Focus checks (evidence)
1. Untrusted wrapping: both model paths build the body only through wrap_untrusted(escape_content(...)), and the prior summary is part of the escaped text. Mutations M1-M4 (drop the escape or the wrap on either path) were all killed.
2. complete_validated: both calls go through `_ask`, which calls `complete_validated(_Caller, max_repairs=0)`. `_Caller.acomplete` is the client passed to complete_validated; it applies the timeout, emits the llm_call trace and turns a refusal into ModelRefused. Session logic never calls client.acomplete directly.
   - ST07-22 (test_st07_chat.py:102-119) asserts that a classification echoing the injection yields only a pending_approval item plus a review item, never an active item.
   - The summary test (test_st07_chat.py:122-145) asserts that markers and numerals become [number] (3 times), the planted email is redacted, and the logs hold no chat text.
   - UT07-82 covers the cut to ≤ summary_max_chars (M13 killed).
3. Provenance: chat rows come only from herness.store.ops.chat, plus session_memory_ids.
   - A message id from another session gives None, no model call and no rows (ST07-22; M9 killed).
   - A foreign user is rejected by propose step 6d with `provenance.session`, logged at INFO, with nothing stored (ST07-22; I checked _write_steps.check_session).
4. Prompts: see U07-99 above.
5. Logs: all 7 log calls carry only session_id, message_id, run_id, memory_id, status, rule, turns, chars and reason (the exception class name). ST07-22 and UT07-83 assert that captured logs hold no chat text. chat.py and _compactor_llm.py import no HTTP client.
6. Spec conformance: see each unit above.
7. Builder deviations:
   - UT05-124 allow-list entry renamed compaction_prompt to memory_prompt, and the UT07-62 one-line change: the ruled loader generalisation forces both. The edits are minimal. Acceptable.
   - Extra `now` keyword on capture_correction: propose needs it for the rate-limit window. It defaults to None and a spec note was added. Acceptable.
   - max_repairs=0: the spec defines no repair step, and the ruling requires complete_validated. Recorded in the spec. Acceptable.
   - No role_params: the spec asks for temperature 0 where supported, and that is done. Acceptable.
   - 4,000-character cut per message: an LLM10 bound, with a spec note. Acceptable.
   - The worker keeps running after the 120 s timeout: the spec says only "waiting at most 120 s" and says nothing about cancellation. The late work is idempotent (exact-hash merge; set_chat_summary never moves back). Acceptable, with a spec note.
   - .secrets.baseline: only one line_number (2660 to 2662) and generated_at changed. The set of file names is identical to the base (checked by diff), and line endings are LF (0 CR).
8. Tests: function names carry their IDs, docstrings start with the ID, and both new test files set pytestmark = unit. _chat_env.py is a helper with no tests. Every ID in the Tests row (UT07-81/82/83/86, ST07-22) has at least one function.

### Gates (run by the verifier)
- pytest on tests/unit/harness/memory, test_st05_prompts.py, roles/test_roles_prompts.py, test_tools_recording.py and tests/security/test_st07_*.py: 715 passed in 147 s.
- Coverage: chat.py 100 % line, 1 partial branch out of 20 (129->133, tracer None; 99 % combined). _compactor_llm.py 100 % line and 100 % branch.
- ruff check: clean.
- ruff format --check: 912 files already formatted.
- mypy: no issues in 334 source files.
- lint-imports: 13 contracts kept, 0 broken.
- check_module_size: exit 0 (chat.py 329/330).
- check_type_ownership: exit 0.
- Wheel: herness-0.1.0-py3-none-any.whl contains memory/prompts/chat_summary.md, compaction_notes.md and correction_classify.md.

### Mutation results (chat.py; run against test_memory_chat.py and test_st07_chat.py)
23 of 24 mutations were killed:

| Area | Mutations killed |
|---|---|
| Wrapping and escaping | M1 classify without escape, M2 classify without wrap, M3 summary without wrap, M4 summary without escape |
| Summary post-processing | M5 marker substitution dropped, M6 uncited-numeral substitution dropped, M7 redaction dropped, M13 cut dropped |
| Correction capture | M8 `<` changed to `<=`, M9 session check dropped, M10 role check dropped, M14 2,000-char content cut dropped, M15 PolicyViolation not caught, M20 via other than "chat" |
| Session load and summary window | M11 assistant-done filter dropped, M12 window of N instead of 2N, M19 newest-first load, M21 summary on every turn, M22 prior summary dropped |
| Model call and errors | M16 summary error raises, M17 always returns None, M23 temperature always 0, M24 TimeoutError not treated as a model error |

SURVIVED: M18. `_turn_message` returns the first user row of the session instead of the latest one before the run's assistant row (chat.py:286, `users[-1]` changed to `users[0]`).

### Strengths
- Clear separation: store access goes only through the 09 and 07 area functions, model I/O is isolated in `_ask` and `_Caller`, and prompts load through the single reader the ruling allows.
- Strong security tests: the real MemoryWriter on a migrated store, an assertion on the wrapper's shape (exactly one element, injected closing tag escaped) and scans of the logs for chat text.
- Every deviation is recorded as a spec note in the same commit.

### Issues
#### Critical (Must Fix)
- none

#### Important (Should Fix)
- tests/unit/harness/memory/test_memory_chat.py:148-182 (UT07-82), code at chat.py:281-286: no test checks which user message U07-94 step 3 picks as "the user message of this turn". Mutation M18 (pick the session's first user row instead of the latest one before the run's assistant row) passes the whole card suite.
  - Why it matters: the scripted fake returns a correction whatever the input. A wrong pick would silently classify an old message and attribute the correction (`source_message_id` and the content fallback) to the wrong message. Correct attribution is the property TH07-22 protects.
  - Fix: in the 6-turn test, or a new UT07-82 case, assert that the classified body contains the last turn's text, or that `rows[0]["provenance"]["source_message_id"]` equals the latest user message id. Ideally also add a case where an older run_id selects its own user row.

#### Minor (Nice to Have)
- chat.py:240, against herness/harness/memory/settings.py:298 and herness/store/ops/chat.py:33,316: the config allows `summary_max_chars` up to 20,000, but set_chat_summary rejects summaries over 6,000 characters with SchemaViolation. That error would escape session_save_turn after the answer was already sent, and U07-94's invariant is "≤ 6,000". Fix: clamp the cut limit to min(cfg.summary_max_chars, 6000), or record a spec note, or tighten the config bound with the owner.
- chat.py:178: the U07-93 precondition `session_id ≤ 64 chars` is not checked. Harmless: the lookup returns None and the function raises MemoryNotFound.
- chat.py:129->133: the tracer-None branch is only partially covered (cosmetic).

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation matches U07-93/94/95/99, and every security focus check holds, with the matching mutations killed and all gates green. One Important test gap remains: the step that picks the turn's user message (and so the correction's attribution) is not tested, and M18 survives. It needs an assertion before merge.

## Re-review round 1 (head 0f99493, fix commit on top of 366363b)

Scope: my findings only. The diff 366363b..0f99493 touches chat.py (+4/-3), test_memory_chat.py (+55) and the 07 spec note on U07-94. The tree was clean at the end, and every mutant was restored byte for byte.

- I1 (pick of the turn's user message) ✅. New test `test_ut07_82_turn_message_is_latest_user_row_before_the_answer` places an older unanswered user row, the turn's user row, the run's answer, and a later user row. It asserts `source_message_id == latest` and that the classified body holds only that message.
  - M18 (`users[-1]` → `users[0]`) is now KILLED by the new test.
  - Extra mutant M18b (drop the `rows[:found]` slice) is also KILLED, by the new test and by `unknown_run_captures_nothing`.
- m1 (6,000 clamp) ✅. chat.py:241 sets `limit = min(cfg.summary_max_chars, SUMMARY_MAX_CHARS=6000)`. `test_ut07_82_summary_clamped_to_store_limit` (cfg 8,000) stores 5,990-6,000 characters, cut at a word boundary. Mutant M25 (no clamp) is KILLED. The response_schema maxLength still follows cfg (8,000), which is fine because post-processing clamps.
- m2 (session_id over 64) ✅. `_session` returns MemoryNotFound without calling get_chat_session when len > 64. `session_save_turn` reaches it through `_session` too. `test_ut07_81_overlong_session_id_without_store_call` covers both entry points, with get_chat_session patched to fail if called. Mutant M26 (no length check) is KILLED.
  - The spec note wording is accurate: "a `session_id` over 64 characters is `MemoryNotFound` without a store read (U07-93 precondition, also in `session_save_turn`)".
  - Boundary mutant M27 (`<=` → `<`) SURVIVES because no test uses a 64-character id. This is a Minor, non-blocking finding.
- m3 (tracer-None partial branch, now 131->135): parked, as agreed.

Gates (verifier run):
- pytest on tests/unit/harness/memory, test_st05_prompts.py, roles/test_roles_prompts.py, test_tools_recording.py and tests/security/test_st07_*.py: 718 passed (147 s).
- Coverage of chat.py: 100 % line, 1 partial branch out of 20 (131->135, m3).
- ruff check: clean.
- ruff format --check: 912 files already formatted.
- mypy: 0 issues.
- check_module_size: exit 0 (chat.py 330/330).

New findings:
- Minor (optional): tests/unit/harness/memory/test_memory_chat.py:145-158. The `len == 64` boundary is not pinned, so M27 survives. A 64-character id that still loads (or reaches the store) would kill it.

**Task quality:** Approved
**Reasoning:** I1, m1 and m2 are fixed with tests that kill the targeted mutants, and all gates are green. The only thing left is an optional test for the exact 64-character boundary.

# T07-08 report: Propose pipeline (U07-50)

Worktree: D:\herness\.claude\worktrees\agent-a5a7e90506ce9216f (branch worktree-agent-a5a7e90506ce9216f, base 984de12)
Commits: f4e2909 wip(T07-08): propose pipeline and unit tests; final commit: see git log (feat(memory): T07-08 propose pipeline)

## Files (lines)
- herness/harness/memory/write.py 345 / 380 (MemoryWriter: propose, insert_system_item, embed_after_commit; steps 1-14)
- herness/harness/memory/_write_steps.py 222 / 260 (private sibling, pre-authorised; §2 row + spec note added)
- docs/impl/07-memory.impl.md: §2 row for _write_steps.py; "T07-08 spec note" after the U07-50 block (§3.9)
- tests/unit/harness/memory/_write_env.py 261 (helper module, not a test file: writer factory on the real Redactor/InjectionScanner/Embedder/VectorIndex, seeds, proposal builders)
- tests/unit/harness/memory/test_memory_write.py 650 (marker unit)
- tests/security/test_st07_write.py 433 (marker integration, per impl 07 §11.5)
- tests/fault/harness/test_memory_write_fault.py 66 (marker fault)
- tests/security/test_st07_render.py: only the ST07-07 config-pattern case re-pointed at MemoryWriter.propose (carry-over closed)

## Tests per ID (function counts)
UT07-24 6, UT07-25 6, UT07-26 2, UT07-27 5, UT07-28 6, UT07-29 6, UT07-30 5, UT07-31 7,
ST07-01 8, ST07-02 5, ST07-03 4, ST07-05 1, ST07-10 1, ST07-11 7, ST07-22 4, ST07-23 1, FT07-02 3 (+ ST07-07 carry-over).
Acceptance: `memory.proposal.stored` asserted exactly (test_ut07_31_stored_log_and_metrics_carry_no_text) with structlog capture_logs.

## Threat proofs (removing the guard fails the test)
- TH07-05 redaction before numerals/scan/hash/embedding/writes: ST07-05 checks memory_item + review_item text, memory_fts content and MATCH, and FakeEmbed.calls; ID_KEYS and entity type/id kept verbatim, other entity fields redacted.
- TH07-01 config patterns (MemoryConfig -> InjectionScanner) incl. zero-width (U+200B/C/D, U+2060) and full-width forms -> instruction_like -> pending + review item; a custom config pattern proves the configured list drives the scan; injection in a data string skips dedupe (no merge into an active twin); `memory.injection.flagged` has only task_id + pattern_indices.
- TH07-23: every kind x author x role x chat/tool combination is rejected or pending (never active), each pending with a review item.
- TH07-22: another session's message, another user's session (chat and dashboard), system message, unknown/missing ids -> provenance.session.
- TH07-02: extra author_type/author_ref/via fields -> ValidationError; run/task mismatch with run_ctx -> provenance.mismatch; human claim via tool -> policy.via.
- TH07-10: 60 proposals, 51st..60th rate.per_run; per session and per user-day; in-transaction re-check proven by blinding the pre-check (UT07-28) and the same for idempotency (UT07-27).
- TH07-11: 1 MB content (schema), >2000 chars, depth 50, >16 KB data, 30 and 1000 entities, 1000 NumberRefs (schema), > max_numbers.
- FT07-02: real fault plan (sqlite.write error:StoreBusy kind=memory_propose count=2) -> succeeds, plan counter 3, one row, one review item; count=7 -> StoreBusy, nothing stored.
- No memory text in logs or errors: rejection message is "memory write policy: <rule>", log events asserted field-by-field.
- Metrics: herness.core.resilience.metrics.record_counter exists, used (component memory): herness_memory_proposals_total{layer,kind,status}, herness_memory_policy_violations_total{rule}, herness_memory_injection_flags_total{kind}; label values sanitised to the metric label charset.

## Coverage
write.py 100 % line / 67 of 68 branches (99 %); _write_steps.py 100 % / 100 %.

## Gates
ruff format --check, ruff check, mypy (299 files), lint-imports (13 kept), check_module_size exit 0,
pytest tests/unit/harness/memory tests/security tests/fault/harness: 1020 passed, 1 skipped (Windows symlink privilege, ST05-17, unrelated).
`--require-test-ids` on the card files: 79 passed.

## Spec readings / deviations (recorded in the spec note)
1. insert_system_item skips step 11 dedupe/merge (identity = keyed hash, §4.4); otherwise near-identical run summaries of different runs would merge and break keyed idempotency. U07-50 says "same steps"; this is the one deliberate departure.
2. With conn, the system row stores embedding_pending=true; embed_after_commit clears it on success (keeps it on ModelUnavailable) so maintenance repairs a crash before the caller embeds.
3. Rate counts run only for scopes present in provenance (count_proposals requires a scope).
4. ANN search failure after a successful embed: skip 11(c)-(d) without embedding_pending; vector still written in step 13.
5. Merges log memory.proposal.stored (merged_into set); vanished merge target falls through to insert.
6. Security tests live in tests/security/ (repo convention, dispatch) rather than tests/integration/memory/security/ (spec §11.5); marker integration as in §11.5.
7. ST07-11 "k 1000" is a recall parameter (T07-10); not covered here.

## Concerns
- ENVIRONMENT: at ~05:08 the D: drive was full (864 KB free) and my worktree was removed from disk and unregistered while it had only untracked work (git in the directory then resolved to D:\herness; one commit attempt ran the pre-commit hooks against the main checkout index; it failed at pytest-unit and committed nothing; no files were staged there because .claude/ is ignored). I re-attached my own worktree with `git worktree add <my path> worktree-agent-a5a7e90506ce9216f` (same branch, base 984de12) and rewrote the files from context. Please confirm D:\herness is clean.
- The venv for this worktree is on C: (UV_PROJECT_ENVIRONMENT=C:\tmp-w21s07a\venv, TEMP/TMP=C:\tmp-w21s07a\build) because D: had no space for a new .venv; a partial 1.2 GB .venv I created on D: was deleted immediately.
- Commit message file lives in C:\tmp-w21s07a (per-agent, outside the repo) instead of .agent-tmp.
- Pre-commit pytest-unit hit the known Hypothesis deadline flake test_st05_03 (sql_guard) once; retried, passed.

## Sub-controller addendum
Final commit made by the w21-s07a sub-controller: bdf18c7 feat(memory): T07-08 propose pipeline (builder's staged security/fault tests, ST07-07 re-point, §2 row + spec note), on top of f4e2909 wip. Hooks all passed, no SKIP. Builder venv/temp under C:\tmp-w21s07a are stale (to delete).

## Correction
The final commit was made by the sub-controller: bdf18c7 feat(memory): T07-08 propose pipeline.

## Fix round 1 (review briefs/T07-08-review.md)
- I1 (_write_steps.redact_payload): exemptions narrowed to U07-50 step 3. An ID_KEYS value is kept only when it is a str or list of str (other types are walked); the type/id exemption covers only the str `type`/`id` of the top-level data["entities"] entries (nested `entities` keys are redacted normally); dict keys are redacted too (ID_KEYS names and the entity type/id names excepted). ST07-05 gains the verifier's three probes ({"x":{"entities":[{"id":EMAIL}]}}, {"query_ids":{"n":[EMAIL]}}, {EMAIL:"x"}) plus {"template_id":{"owner":EMAIL}}. Each checks memory_item/review_item text, FTS content and MATCH, and the embedder input.
- M1: test_st07_05_only_spec_exemptions_skip_the_redactor spies on Redactor.redact. It asserts the redactor never sees the ID_KEYS str values or the top-level entity type/id, and does see every other string and key. Verified by hand: removing `rule_id` from ID_KEYS fails it, and so does emptying the entity keep set.
- M2: test_ut07_30_dedupe_embedding_failure_is_final checks one embed call and embedding_pending in the stored flags, data and review payload. Verified by hand: removing the step-11(b) flag fails it.
- M4: test_ft07_02_busy_inside_the_transaction_is_retried (parametrised over create_review_item and insert_memory_item) raises sqlite3.OperationalError("database is locked") once inside the insert tx. It checks the run_write retry leaves exactly one memory_item and one review_item, linked both ways.
- M3: spec note item (8). Step 6(a) applies only when run_ctx is given, so the propose_memory wrapper must always pass run_ctx for agent proposals. The spec note also has item (7), which documents the redaction exemptions.
- Commit: e0e44e0 fix(memory): T07-08 review round 1 (real hooks, first attempt).
- Gates: ruff format/check, mypy (299 files), lint-imports 13 kept, check_module_size 0 (_write_steps.py 245/260, write.py 345/380). Card tests 87 passed with --require-test-ids. tests/unit/harness/memory + tests/security + tests/fault/harness: 1028 passed, 1 skipped (ST05-17 symlink). Coverage: _write_steps 100/100, write.py 100 % line / 67 of 68 branches.

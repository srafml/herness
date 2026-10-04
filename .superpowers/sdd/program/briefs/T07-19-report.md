# T07-19 report: Procedural promotion (U07-88 … U07-91)

Worktree: D:\herness\.claude\worktrees\agent-a1b3795376e5298aa (branch worktree-agent-a1b3795376e5298aa, base e41d62c)
Commits: b03a2f1 wip(T07-19) (all code, tests, spec note; message file reuse gave it the wip subject) + final empty-tree commit `feat(memory): T07-19 procedural promotion` carrying the card subject (no amend, per rules).
Note: the worktree had no .venv at dispatch; created with `uv sync --frozen`.

## Files (lines / budget)
- herness/harness/memory/procedural.py 383 / 390 — ProceduralDeps, wilson_lower_bound, promote_procedural, validate_templates (re-exports parameterize_sql, bind_template, ParamSpec, ParameterizedSql)
- herness/harness/memory/_procedural_sql.py 252 / 260 — NEW private sibling (size-forced): parameterize_sql, bind_template, metrics_used, unsafe_reason (pre-create guard check), explain_ok (U07-91 steps 1-3), ints/strs data readers. §2 row added.
- docs/impl/07-memory.impl.md — §2 row for _procedural_sql.py; "T07-19 spec note" after the U07-90 block (items 1-7).
- tests/unit/harness/memory/_procedural_env.py 141 (helper, not a test file: deps factory on the real writer env + real SqlGuard, run/task/evidence/finding seeds)
- tests/unit/harness/memory/test_memory_procedural.py 491 (pytestmark unit; hypothesis for PT07-05)
- tests/security/test_st07_procedural.py 144 (pytestmark unit, like the other test_st07_* files)

## ProceduralDeps (frozen dataclass, procedural.py)
conn_factory: Callable[[], sqlite3.Connection]; writer: MemoryWriter; vectors: VectorIndex; redactor: Redactor; config: ProceduralConfig; guard: SqlGuard. Keyed hash = policy.keyed_hash (no extra hook needed).

## Spec readings / deviations (all in the spec note)
1. Ruling 2 (defense in depth): before writing a fingerprint with passes: template must contain no sqlglot Literal; concrete query rebuilt via bind_template (literal nodes from params[*].example) must pass deps.guard.check. Failure -> nothing created/updated, observations added to skipped_unparsable, log memory.procedural.skipped {run_id, fingerprint, reason in template_literal|unbound_placeholder|guard_rejected}. Note: it is applied also when the template already exists (stricter than "before CREATING"; a template whose SQL no longer passes the guard is not updated by passes either). PolicyViolation from the write path -> reason policy:<rule>, not counted as unparsable.
2. Placeholders render as `$name` (sqlglot duckdb spelling of `:name`). Source order = token start offset. Later dates and numbers/strings share the p<n> counter; ids: entity_id, entity_id_2, ...
3. Fingerprint = policy.keyed_hash(template)[:16] (identical SHA-256 value) — a direct hashlib call in herness/harness fails UT05-124 (reviewed hash call-site list in tests/unit/harness/test_tools_recording.py, outside Files). UT07-75 asserts it equals sha256(template)[:16].
4. The write path (T07-08) redacts every non-ID data string: it turned build id "20260901-120000-ABCDEF" into "[PHONE_…]-ABCDEF". build_id_last_ok is therefore set after insert in the same transaction. sql_template text and params examples stay as redacted by the writer (privacy); a redacted example could make EXPLAIN fail -> carry-over below.
5. Run without build_id -> evidence build_id of last passing query. Task without objective -> no question/qa_pair, content "SQL template <fp>".
6. qa_pairs only for passing (finding, query) pairs (TH07-20). New qa_pair of an active template set active in the same tx.
7. Template keeps data.qa_ids (last 200): ops has no lookup by data.template_id (carry-over).
8. Template keyed hash = keyed_hash("sql_template\n"+fp+"\n"+run_id): rerun of the creating run after expiry creates nothing (already_processed); later runs may create a fresh template.
9. validate_templates returns (passed EXPLAIN, expired); unbound placeholder / bad example / guard rejection = validation failure.
10. Step 9: embed_after_commit for new items, then VectorIndex.set_status per status; ModelUnavailable -> log memory.embedding.failed op=set_status (maintenance repairs).

## Tests
UT07-75 1, UT07-76 4 (+5 parametrized invalid cases), PT07-05 1 (hypothesis, 60 examples), UT07-77 1, UT07-78 9, UT07-79 5, ST07-20 5 functions (9 cases): rejected-only runs (x3 + reruns) create nothing; failures only lower an existing template; guard-rejected: table outside allowlist, column outside schema, denied schema (stg), non-SELECT (DELETE), unbound named placeholder; template with a literal (monkeypatched parameterize_sql); guard spy sees the rebuilt concrete query. Logs asserted free of SQL text/planted values.
Mutation: removing the guard.check call in unsafe_reason -> 5 ST07-20 cases red (restored).
UT07-78 idempotence: rerun of run 3 -> memory_rows() identical, already_processed True, created/updated/qa 0.

## Coverage (card tests)
procedural.py 98% (262 stmts, 3 miss; 70 branches, 3 partial); _procedural_sql.py 99% (2 miss: float-parse fallback). Line >= 90, branch >= 85.

## Gates
ruff check: pass | ruff format --check: 911 formatted | mypy: 0 issues (335 files) | lint-imports: 13 kept 0 broken | check_module_size rc 0 | check_type_ownership rc 0 | detect-secrets hook on new files rc 0
PYTHONUTF8=1 uv run pytest tests/unit/harness/memory tests/security -q -p no:logging: 1167 passed, 1 skipped, 1 xfailed (pre-existing ST10-14) | test_tools_recording.py (UT05-124) green after fix.

## Carry-overs
- T07-09 / MemoryStore facade (store.py, __init__.py) wiring of promote_procedural/validate_templates and construction of ProceduralDeps (guard from CURRENT schema, empty schemas dropped) — not in Files, not done.
- ops area memory: a lookup of qa_pairs by data.template_id would replace data.qa_ids.
- Write path (T07-08) redacts data strings of sql_template (template, params examples, build_id_last_ok). build id restored here; if real redactor masks date-like/ID-like examples, nightly EXPLAIN may fail and expire templates. Ruling wanted: add build_id_last_ok / params examples handling to ID_KEYS or an exemption for procedural kinds.
- BT07-09 / IT07-06 belong to other cards.

## Fix round 1 (review briefs/T07-19-review.md: I1-I3; tests only, no code change)
- I1: test_ut07_78_fail_in_recent_blocks_promotion rewritten: min_passes 1, min_runs 2, min_pass_lb 0.1; run 1 = 5 passes (candidate by min_runs), run 2 = 1 fail -> passes 5, 2 runs, pass_lb >= 0.1 asserted, recent [pass, pass, fail] -> stays candidate (only the recent-fail gate blocks); run 3 = 3 passes -> recent all pass -> promoted.
- I2: test_ut07_78_single_run_cannot_promote_alone (defaults: 9 passes in one run, pass_lb 0.7012 >= 0.7 asserted -> candidate, min_runs 2, TH07-20); test_ut07_78_min_passes_gate (min_passes 5, min_runs 1, min_pass_lb 0.1: 4 passes candidate with pass_lb >= 0.1 asserted, 5th pass promotes).
- I3: test_ut07_79_guard_rejection_alone_fails_validation: same template EXPLAINs fine (1, 0) with the deps guard; with a guard blocking metrics.daily_incidents.incidents (dataclasses.replace of deps) -> (0, 0) failures 1, then (0, 1) expired.
- Mutations (script C:\tmp-w24s07c\build\mutate.ps1, file restored after each; git diff on herness empty):
  M1 drop `"fail" not in recent` -> fail_in_recent red; M2 drop min_runs -> single_run + fail_in_recent red; M3 drop min_passes -> min_passes_gate red; M4 drop guard.check in explain_ok -> guard_rejection_alone red.
- Tests: card files 37 passed (28 unit + 9 ST07-20); tests/unit/harness/memory + tests/security: 1170 passed, 1 skipped, 1 xfailed (pre-existing). ruff check/format, mypy clean.
- I1-I3 commit: a476025 test(memory): T07-19 fix round 1 gate tests (hooks green).

### Fix round 1, controller items (second commit: fix(memory): T07-19 fix round 1 build id, pending lookup, update scan)
- (a) Cross-card scoped edit: `build_id_last_ok` added to ID_KEYS in herness/harness/memory/_write_steps.py (T07-08 file; that entry only, 246/260 lines). The restore-after-insert workaround is removed from procedural.py: create now passes the real build id, and update sets it in _merge when the run has passes. Regression test test_ut07_78_build_id_passes_write_path_unredacted: insert_system_item keeps "20260901-120000-ABCDEF" verbatim. Recorded in the T07-19 spec note item (4).
- (b) Template lookup statuses are now `_OPEN` = candidate, pending_approval, active. A pending_approval (injection-flagged) template takes the later run's counts and is never promoted or expired by this code (invariant reading in spec note (4); leaving pending is approve/reject). Test test_ut07_78_pending_template_is_not_duplicated: second run -> templates_created 0, updated 1, same memory_id, still pending_approval, passes 2.
- (c) New ProceduralDeps field `scanner: InjectionScanner` (the write pipeline's scanner class over the same configured injection_patterns; no duplicated patterns). In _merge, a new question example is added only when scanner.scan(q) is empty. Test test_st07_20_injected_question_not_added_on_update: a planted "Ignore previous instructions …" objective on the update path leaves question_examples == [OBJECTIVE]; its qa_pair goes through the write path and is pending_approval. Spec note (1) lists the field.
- (d) Spec note under U07-92 (for the T07-20 LoRA owner): qa_pair.data.sql is redacted concrete SQL and may no longer parse or keep its meaning; the export should parse it and drop/count such pairs, or use the template. No code change here.
- (e) Fingerprint via policy.keyed_hash: unchanged.
- Sizes: procedural.py 390/390, _procedural_sql.py 252/260, _write_steps.py 246/260; check_module_size rc 0.
- Mutations (C:\tmp-w24s07c\build\mutate2.ps1, each file restored after; sources re-checked by grep):
  M1 drop "fail" not in recent -> fail_in_recent red; M2 drop min_runs -> single_run_cannot_promote_alone + fail_in_recent red; M3 drop min_passes -> min_passes_gate red; M4 drop guard.check in explain_ok -> guard_rejection_alone_fails_validation red; Ma drop build_id_last_ok from ID_KEYS -> build_id_passes_write_path_unredacted + unknown_run_and_unparsable red; Mb lookup with _LIVE (no pending_approval) -> pending_template_is_not_duplicated red; Mc drop update-path scan -> st07_20_injected_question_not_added_on_update red.
- Tests: card files 40 passed (30 unit + 10 ST07-20); tests/unit/harness/memory + tests/security: 1173 passed, 1 skipped, 1 xfailed (pre-existing ST10-14). Coverage procedural.py 98% / _procedural_sql.py 99% (branch included). ruff, format, mypy (0 in 335 files), lint-imports (13 kept) clean.

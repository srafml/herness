# T03-19 review (verify agent): Resolve SQL and frame

Reviewed: worktree agent-a3fc6e3fcb8ed8255, base bdb61b7, head 344f8d5 (code in e77783f). Read-only.

**Task quality: Needs fixes** (one Important: the PT03-10 acceptance property leaves the SQL's fingerprint and version filters unverified. The SQL itself matches `resolve_pair`.)

### Spec Compliance
- ✅ U03-78 `resolve_decisions.sql`. It covers every item below.
  - Pairs: text rows x `qmeta` where the entity is in `applies_to`.
  - `in_scope` = primary `laya` OR `opened_at >= $bootstrap_since`. A NULL `opened_at` counts as not in scope, the same as the reference.
  - Candidates: current fingerprint (joined through `qmeta`) and `versions`, deduplicated per (hash, question, decider) by latest `decided_at` with `QUALIFY`.
  - Calibration formulas for bool (clamp, sigmoid, `1-v` for `false`) and choice/score (softmax with `+1e-9`), with `t` from `calib` defaulting to 1.0. These are verbatim from the spec. T is bounded [0.05, 10] by the calibration loader, so there is no exp overflow.
  - Rank: ensemble 1, primary passing the gate 2, chain `3+list_position`. A primary below threshold gets a NULL rank and is dropped.
  - Human join on the current fingerprint; `has_primary` from current-version primary rows; `pending_items` DISTINCT.
  - `resolve_pair` steps 2-4 as CASE expressions. Checked each branch by hand against decide.py:208-256: status, use_human, the corrected/confirmed/pending/none review status, escalated, decided_at, agreement.
  - Ensemble `escalated` uses a LEFT JOIN to the Laya candidate row, which is equivalent to the spec's "lateral lookup".
  - Output has the 15 columns. 132/160 lines.
- ✅ U03-79 `resolve_frame`.
  - Registers `cache_rows`, `human_latest`, `calib`, `qmeta` (chain_after, pair questions excluded), `versions` and `pending_items`, all as Arrow tables. The ops DB is never attached.
  - Pending items are filtered by qsv.
  - SQL is loaded through importlib.resources and run with bound `$bootstrap_since = now - bootstrap_window_days`.
  - Views are unregistered in `finally`. The ops read happens before anything is registered.
- ✅ U03-80 `escalation_queue`.
  - Groups queue rows per record, with `question_ids` sorted and `bool_or(scoring_use)`.
  - Order `max_scoring DESC, opened_at DESC NULLS LAST, record_id`; bound `LIMIT $max_records`; joins text; the final ORDER BY is repeated, so the result is deterministic (with Minor 1).
- ✅ UT03-76: cap 2 gives scoring_use first, then newest first, 2 records with all their queued questions (test_resolve.py:337). Extra cases cover full order, NULLs last, cap 0, exclude and errors.
- ❌ PT03-10: runs 200 examples and passes, but does not cover stale-only candidates. See Important 1.
- ✅ TH03-09: the cap is bound as a parameter. ✅ ENG §3.5: no value is formatted into SQL. The `$exclude` list and `$max_records` are parameters; the only f-strings are in tests.
- ✅ Errors: DuckDB errors become `SchemaViolation("resolve_decisions: ...")` / `("escalation_queue: ...")`. The message is the first line for Catalog/Binder errors, else the class name (the no-leak rule, same as the link_changes precedent).

⚠️ Cannot verify from diff / needs controller ruling:
- `exclude_deciders` semantics (U03-80 has no algorithm text). The builder leaves out a queued (hash, question) when an excluded decider has a current row. A queued pair has no ensemble, passing primary or chain row, so in practice this excludes pairs whose excluded decider is the below-gate primary (or a non-chain decider such as laya). The reading is plausible and no caller in the spec passes the argument (run_decide_teacher, impl line 1833, calls `escalation_queue(max_records=...)` only). The controller should confirm it or amend the spec.
- The extra kw-only `deciders: DecidersSettings` in `resolve_frame` follows the T03-17 precedent (R-76 made `chain_after` need it). Acceptable; record it as a spec delta.
- IT03-04 proper (the stage run twice) belongs to a later card.

### Gates (run by reviewer)
- `pytest tests/unit/enrich/test_resolve.py -q -p no:logging`: 9 passed in 17 s. Coverage of resolve.py is 99% (91 stmts, 0 missed; 20 branches, 1 partial at 125->121), above the 90/85 bar.
- ruff check and format are clean. Project `mypy` (files = herness, tools) passes on 208 files. `tools/check_module_size.py` exits 0. lint-imports: 13 kept, 0 broken.
- Mutation probe. The reviewer ran scratch copies of the SQL, via a patched `_SQL_FILE`, through the PT03-10 test body; the worktree was untouched.

| Mutation | Result |
|---|---|
| laya in_scope | killed |
| NULL opened_at coalesce | killed |
| gate removed | killed |
| ensemble laya-missing escalated | killed |
| chain order | killed |
| has_primary from best | killed |
| calib t ignored | killed |
| **cache fingerprint filter removed** (sql:16) | **SURVIVED** |
| **versions join removed** (sql:17) | **SURVIVED** |
| `>=` changed to `>` in the gate | survived (expected: ties are excluded by `assume`) |
| agreement not restricted to ensemble | survived (generator gives non-ensemble rows NULL confidence; the SQL CASE is defensive, harmless) |

### Strengths
- The SQL is a faithful, readable transcription of `resolve_pair`. Every CASE branch maps one-to-one to decide.py.
- The PT03-10 reference p_cal comes from an independent `apply_temperature`, not a copy of the SQL formula. Worlds share content hashes across records and include human rows plus a stale-fingerprint human row (which kills a missing human fingerprint join), pending items, NULL/in/out-of-window `opened_at`, ensemble and random temperatures. The hard branches are covered, except the one in Important 1.
- Clean failure handling: nothing is registered when the ops read fails, and views are unregistered on SQL failure (tested).
- Parameter binding throughout. The ops DB reaches DuckDB only as Arrow.

### Issues

#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. **PT03-10 never generates stale-only candidates, so the SQL's current-fingerprint and current-version filters are untested** (tests/unit/enrich/test_resolve.py:168-170; the filters are at herness/enrich/sql/resolve_decisions.sql:16-17).
   - Every distractor row sits next to a current row for the same decider. The stale-fingerprint row is always older, so dedup hides it. The `v0` row is identical in answer, distribution and `decided_at`, so dedup ties.
   - Removing either filter still passes all 200 examples (reviewer mutation probe).
   - These filters are exactly what the SQL must add over `resolve_pair` (whose precondition assumes rows are already filtered). A regression would let stale teacher answers become final, flip `has_primary` (out_of_scope vs queue) and change the rank winner.
   - Fix: sometimes draw a decider that has only a stale-version or stale-fingerprint row (absent from the reference `rows`), and make distractors newer than the current row with a different answer and distribution. Re-check that both mutations are then killed.

#### Minor (Nice to Have)
1. resolve_decisions.sql:85 and :95 partition and join `best` by (record_id, question) without `entity`; resolve.py:50-57 groups the queue by record_id/entity/content_hash but orders ties by record_id only. This is correct only if record ids are globally unique across incident/change/problem. Add `entity` to the partition, join and ORDER BY, or document the assumption.
2. `escalation_queue` depends on the unspec'd temp table `enrich_cand` even when `exclude_deciders` is empty (resolve.py:53-56). Any `enrich_resolved` built another way (e.g. a later stage test fixture) must also create `enrich_cand`. `enrich_cand` also stays alive on the build connection after the frame; at full scale that is roughly hashes x questions x deciders rows. Consider skipping the NOT EXISTS when the set is empty and/or dropping `enrich_cand` once no longer needed, and add the extra table to the spec delta. `enrich_laya_cal` is spec-mandated (U03-88), fine.
3. The IT03-04 ID is reused on three unit tests (test_resolve.py:504, 532, 542). IT03-04 is an integration flow test for a later card; this marks it "implemented" in check_traceability and will collide (TR007) when the real IT03-04 lands. Note that UT03-76 (x4) and PT03-10 (x2) also trigger TR007, a repo-wide pre-existing pattern. Consider asking the controller for a UT id for resolve_frame, or renaming when IT03-04 is written.
4. The test file fails mypy strict (test_resolve.py:102 "Cannot infer type of lambda"; :127 `str` passed where a `QuestionType` Literal is expected). It is not in the gate (`files = herness, tools`), so this is polish only.
5. The unregister assertion omits `pending_items` (test_resolve.py:529).
6. `qmeta.labels` is filled but unused by the SQL (resolve.py:85-91, 113). This is allowed: the spec lists the column, and using it would diverge from `resolve_pair`.
7. Extra ConfigErrors (missing primary resolve.py:106-108; negative cap resolve.py:198-200) are reasonable guards. List them in the spec delta.

### Builder concerns, judged
- `deciders` param: accept (T03-17 precedent, R-76).
- `exclude_deciders` reading: plausible; controller to confirm (⚠️ above).
- `enrich_cand` / `enrich_laya_cal`: `enrich_laya_cal` is required by U03-88. `enrich_cand` is acceptable (it is used in three joins and by the queue), with Minor 2.
- Unused `qmeta.labels`: fine (Minor 6).
- Extra ConfigErrors: fine (Minor 7).
- IT03-04 ID reuse: Minor 3.
- Payload_match instead of a Python filter: same result, fine.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The SQL, frame and queue are correct and secure, and the gates pass. But the card's acceptance property PT03-10 does not exercise stale-version or stale-fingerprint candidates, so two core filters could regress unnoticed. The fix is small and test-only.


---

## Re-review round 1 (head 70a2dd1, code in c94ef4b)

**Task quality: Approved**

### Findings verified
- ✅ **I-1 (PT03-10 stale candidates): fixed.** In test_resolve.py `_draw_pair`, every decider now gets a `v0` row and a stale-fingerprint row. Both are 1 day newer than the current row and carry a different answer and distribution. Half the time the decider has only those stale rows and is absent from the reference `rows`. There is also an older same-version duplicate with a different answer.
  - Reviewer mutation probe on a scratch copy of the new SQL: 11 of 13 mutants killed, including the removed cache fingerprint filter, the removed `versions` join, and both new entity mutants (partition and join).
  - The two survivors are expected, as in round 1: the `>=`/`>` tie, which `assume` excludes, and the defensive agreement CASE.
- ✅ **M1 (entity): fixed.**
  - resolve_decisions.sql: `entity` is carried in `ranked`, used in the `best` partition, and used in the `best` join.
  - resolve.py queue: `entity` is added to both ORDER BYs as the final tie-break.
  - PT03-10 draws (record_id, entity) collisions across incident and change; `applies_to` now covers both entities.
  - The new test `test_ut03_76_record_id_shared_across_entities` covers the queue side.
- ✅ **M2 (enrich_cand coupling): fixed.** `enrich_cand` is read only when `exclude_deciders` is non-empty, and a test drops `enrich_cand` and still gets a queue.
  - The fragment is spliced with `str.format`, but it is a constant (`_EXCLUDE_SQL` or ""); values stay bound as `$exclude` and `$max_records`, so ENG §3.5 holds.
- ✅ **M3 (IT03-04 ID reuse): fixed.** The tests are renamed `test_rf_*`, and check_traceability no longer reports IT03-04 against this file. The remaining TR007 hits (PT03-10 x2, UT03-76 x5) are the repo-wide pre-existing pattern.
- ✅ **M4 (strict mypy on tests): fixed.** `mypy --strict tests/unit/enrich/test_resolve.py` is clean.
- ✅ **M5 (unregister assertion): fixed.** `_assert_unregistered` checks all six views, on both the success path and the SQL-error path.
- M6 and M7 needed no change.

### Controller rulings noted
- `exclude_deciders` semantics accepted and recorded as a spec note.
- The `deciders` param is accepted (T03-17 precedent).
- `enrich_laya_cal` is accepted.

### Gates (run by reviewer)
- Card tests: 10 passed in 20 s.
- resolve.py coverage: 99% (95 stmts, 0 missed; 22 branches, 1 partial at 126->122).
- ruff check and format: clean. Project mypy: 208 files clean.
- check_module_size: exit 0. resolve.py is 207/380 lines; the SQL is 133/160.
- lint-imports: 13 kept.
- The worktree is clean after the review; the mutants were run from the scratchpad.

### Open findings
None.

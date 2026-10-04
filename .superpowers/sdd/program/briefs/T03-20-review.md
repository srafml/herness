# T03-20 review — Resolve stage (U03-81..U03-83)

Reviewer: verify agent · Worktree agent-a3c6bc561c79e5bcc · HEAD fc919b7 (base 750ec27)

### Spec Compliance
- ✅ U03-81 `select_spot_checks` (herness/enrich/_spot_checks.py, re-exported by resolve): n = least(n_cap, floor(rate x N_new)), where n_cap = min(nightly_max, max(0, open_cap - open_counts[q])). N_new counts final, non-human rows with decided_at >= since. Picks: n//2 uniform, then n - n//2 band picks in [threshold, threshold+0.1] on the calibrated `enrich_resolved.probability`, then a uniform fill when the band is short. No row is picked twice. Order is `sha256(build_id|content_hash|question)`, computed in DuckDB SQL. The payload has exactly the 11 keys of design §4.6, with purpose=spot_check, text_ref=enrich.text_redacted and no text. DuckDB errors raise SchemaViolation. I checked the `_pick` bound by hand: the SQL returns the first n rows by hash plus the first n band rows, which is enough for the uniform, band and fill branches.
- ✅ U03-82 `decision_wide_sql`: the exact DDL shape, covering non-pair questions in set order. Each id is re-checked with `re.fullmatch("[a-z][a-z0-9_]{1,40}")`, so a trailing newline is also rejected. Ids go in as double-quoted identifiers and single-quoted literals. A bad id raises ConfigError without echoing the id.
- ✅ U03-83 `run_resolve` order: the pure DDL is built first (so nothing is written on a bad id), then sync_label_checks, resolve_frame, one INSERT…SELECT WHERE status='final' with $qsv, the decision_wide DDL, the stats query, the spot-checks (open_label_counts -> select_spot_checks -> create_if_absent with blocking statuses pending/approved/rejected, scope {qsv}, now=clock.now()), and finally the report counters, the two gauges and the completion log. The metric names match, and both pass the real `record_gauge` name/label validation (I checked by calling it directly).
- ✅ UT03-77: 10,000 new rows with open count 290 give 10 picks, 5 uniform + 5 band. The test recomputes the picks with hashlib, independently of DuckDB. It checks determinism across runs and across connections, and that a different build_id gives different picks. The band-shortfall fill, the caps (50 / open cap / odd n), the per-question order and the missing-frame SchemaViolation are also covered.
- ✅ UT03-78: exact DDL string; it runs twice on DuckDB; the wide row values and column names are checked; the pair question is excluded.
- ✅ ST03-19: `a"; DROP` plus 5 other bad ids raise ConfigError without echoing the id; marker unit, in tests/unit/enrich/security.
- ✅ IT03-06: real ops store, cache and label store. escalated/review_status match design §4.1 (openjev escalation → escalated=true; pending label_check → pending; approved correction folded in by sync → human/corrected/p=1.0/decided_at=label time). Also checked: decided_at = cache row time, agreement NULL, decision_wide, report 5/2, gauges, spot-check items, TH03-03 (no "secret body" in logs or payloads), no duplicates on a rerun, and that a bad id writes nothing.
- ✅ Test IDs are in every test name and docstring; markers are unit/integration.
- ✅ Module budgets: resolve.py is 348/380 lines, _spot_checks.py 137/150, and `tools/check_module_size.py` exits 0.

Builder concerns, judged:
1. **match_keys (U03-83 literal):** confirmed as a real defect in U03-148. `create_if_absent` (herness/enrich/review_items.py:109) passes `match_keys + tuple(scope)` to impl 02. `create_review_item_if_absent` (herness/store/ops/shared.py:335) calls `_review_common.keys_ok`, which requires `len(set(keys)) == len(keys)` (herness/store/ops/_review_common.py:45). So the literal call always raises ConfigError. The workaround keeps the semantics:
   - The store match set becomes (purpose, question, content_hash, question_set_version), which is identical to the spec's.
   - The in-call dedupe key drops qsv, but `_check_create_if_absent` forces every payload to equal the scope, so qsv is constant.
   Accepted. Carry-over: have U03-148 dedupe scope keys against match_keys, then restore the literal tuple.
2. **Candidate dedupe per (question, content_hash):** sound. Because content_hash is a match key, a duplicate would otherwise become a suppressed pick and n would silently shrink. N_new still counts rows, per spec. This goes beyond the literal postconditions, is harmless, and is recorded in the report.
3. **`since` = run_started_at:** U03-83 leaves `since` unbound. Cache rows get `decided_at = clock.now()` at write time (cache.py:246), so "decided in this run" means "newly decided". This is acceptable (see the ⚠️ item below).
4. **Non-idempotent INSERT:** spec-mandated. The impl 03 §4.1 table says "one INSERT … SELECT", and design §6 says build_pipeline restarts the build (new wh-<build_id> file) on a crash. Not a defect.

- ⚠️ Cannot verify from diff:
  - After a mid-run crash and build restart, rows decided in the crashed run have decided_at < the new run_started_at, so they are never spot-checked. This only matters when a crash happens after decide and before resolve; it is a design-level question for the pipeline card.
  - Coverage uses final / (status <> 'out_of_scope'). `enrich_resolved` does not expose `in_scope`, so a final row for an out-of-scope pair (one with a primary cache row outside the bootstrap window) counts in both numerator and denominator. This is a reasonable reading of "in-scope pairs" given T03-19's output schema.

### Strengths
- The SQL keeps the candidate set small, and `_pick` is correct for every branch.
- The tests use an independent hashlib reference implementation.
- The DDL is built before any write; the error message never echoes the id.
- IT03-06 exercises the real ops and label stores, including sync ordering and rerun idempotency.
- Tests pass: card tests 18/18; tests/unit/enrich + tests/integration/enrich 557 passed, 2 skipped (platform/laya), with no pytest warnings.
- Static checks pass: ruff check/format and mypy are clean on all 5 touched files.
- Coverage (full enrich suites): _spot_checks.py 100%; resolve.py 146/148 lines (98.6%) and 32/34 branches (94%).

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/enrich/_spot_checks.py:45: `BETWEEN w.threshold AND w.threshold + 0.1` is computed in floating point (0.7 + 0.1 = 0.7999999999999999), so a calibrated probability of exactly threshold+0.1 falls outside the band. This is an edge case; a small epsilon or rounding would make the closed interval exact.
2. herness/enrich/resolve.py:87-92: the INSERT has no column list and depends on the column order of the `enrich.decision` DDL in herness/model/sql/000_settings.sql:52-65. The order matches today and IT03-06 checks it. Adding an explicit column list would guard against DDL reordering. (The spec text also omits one, so this is optional.)
3. herness/enrich/resolve.py:343-346: the DuckDB error path of `run_resolve` (`_duck_error(..., where="run_resolve")`) is untested (lines 345-346). The `decided == 0` branch (305->308) is also uncovered. Both are well within the coverage gates.
4. herness/enrich/resolve.py:293: `enrich.spot_check.created` is logged only for questions that got payloads. Questions with n=0 emit nothing, so an operator cannot tell "capped by open cap" from "no new rows". Optional.
5. herness/enrich/_spot_checks.py:48 (dedupe per (question, content_hash)) and resolve.py:86 (match_keys workaround) are recorded in the build report and code comments but not in the impl 03 spec text. Carry both to the spec-notes / U03-148 owner so the literal U03-83 tuple can be restored.

### Assessment
**Task quality:** Approved
**Reasoning:** All four card test rows and all three units meet the spec, and the gates pass. The one deviation (match_keys) works around a confirmed U03-148 defect with no change in store-level dedupe behaviour. The remaining items are Minor robustness and traceability points plus one carry-over.

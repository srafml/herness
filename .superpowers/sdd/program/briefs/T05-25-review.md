# T05-25 Verifier: review (verify agent)

Worktree agent-ab8d2158ef5032393, commit 3799879 (base 6b70189). Read-only review; no code changed.

### Spec Compliance
- ✅ U05-63 (construction, re-run cache): override patterns compiled (ConfigError on bad pattern), `sql=None` reads `get_config().models.harness.sql`; cache keyed `(query_id, build_id)` under `threading.Lock` (_verifier_rerun.py:243-283); `build unavailable` (:288-289); guard only when `stored.guard` (ops evidence) with `allow_catalog=True` and `guard: <rule>` (:290-296); `threading.Timer(rerun_timeout_s, cur.interrupt)` cancelled in finally (:303-319); streaming via `iter_batch_rows` into `result_hash` (R-15, not reimplemented); `too large` above `scan_rows`; rows kept only while count ≤ 10,000 else per-row_key matches from the streaming pass (:193-214); DuckDB/interrupt text cut to 200 chars. Implemented as `RerunCache.get` in a private sibling (controller ruling).
- ✅ U05-64 steps 1-8, 10: unknown markers incl. marker ids without a NumberRef and malformed tokens; `duplicate:<id>`, `<name>:<id>`, `<name>:not_usd` (_verifier_items.py:112-144); `number_unused` WARNING with `where`,`ids`; uncited via `find_uncited` with configured/override patterns; `unverified_findings` for non-`verified`/missing ids; grouping by query_id; ops evidence then `meta.evidence` with query_id recompute (tamper → missing_query) (_verifier_rerun.py:124-145); `wrong_build` for ops evidence; re-run error → query_failed; hash equal / equivalent (stored row_count ≤ 50 and rows kept → `rows_equivalent` on JSON-safe rows) / values_only (row_count > 50), `result drift` → query_failed; steps 6-7 always run for non-failed groups (missing_column → row_not_found / row_ambiguous → compare_value), `actual` JSON-safe (Decimal → str); `passed` consistent with the U05-12 validator; `claim_support=None`; `fault_point("verifier.mid_batch")` between items via `_shims`; `verifier_verdict` with all 9 fields; `# T08-05:` metric markers; BUILD_ID_RE → ConfigError.
- ✅ U05-67: verify_findings (one result per finding, input order, shared cache, `finding:<id>`); verify_draft order title, sections[i].title, sections[i].paragraphs[j], recommendations[k] (headline\nsummary, the 4 named refs + `action_levers[m].delta_usd_ref`, nulls dropped), caveats[c], prior_outcomes_commentary; single `_verify_items` call; verify_answer `where="answer"`.
- ✅ Security: stripping empty schemas before SqlGuard does not weaken it (a table in a dropped schema still fails `_check_table_name` — `self._schema.get(db, {})` → "does not exist"; ALLOWED_SCHEMAS is static). `python_enable_replacements: False` (warehouse.py) disables DuckDB Python replacement scans — a hardening, asserted in UT05-49. TH05-11/12/23 covered by ST05-11/12/23; ops tamper check is U05-71 (`get_evidence` recompute, confirmed in herness/store/ops/evidence.py:89-106).
- ⚠️ Cannot fully verify / accepted stand-ins: spec 11 `tiny_build`/`lake_small`/`full` and the real IT05-07 corpus/BT05-09 dataset (stand-ins, ruled); U05-35 JSON-safe conversion (private `json_safe`, ruled); FT05-03 kill simulated by BaseException + test-local plan (ruled); module-map note for `_verifier_rerun.py` (320) / `_verifier_items.py` (152) still to be recorded. When `meta.evidence` cannot be read because the build is gone, the result is `missing_query` (not `query_failed`) — consistent with "neither → missing_query" and asserted in FT05-05, but worth noting to spec 06. Ops `result_sample` cells redacted by U05-35 (`redact_output_columns`) can never be `rows_equivalent` to raw re-run cells on a hash mismatch — spec-level edge, not this card's.

### Evidence (run by reviewer)
- `pytest tests/unit/harness + IT05-07 + ST05 + FT05 + BT05-09 --cov=herness.harness --cov-branch`: 461 passed, 1 skipped (symlink privilege, pre-existing); verifier.py 100 %, _verifier_items.py 100 %, _verifier_rerun.py 99 % (138-139), warehouse.py 96 % (branch on).
- ruff check / format --check clean; mypy --strict 0 issues on the 3 modules; lint-imports 13 kept 0 broken; check_module_size exit 0 (verifier.py 383 ≤ 400).
- Test IDs present and asserting outcomes: UT05-117 (every check result planted incl. meta fallback/tamper, item lists, trace fields), UT05-118 (30 citations → 1 execute; >10k rows → rows None + re-run for new key; too large; interrupt ≤200 chars; guard error), UT05-119 (order + `not_usd`), UT05-120, UT05-122 (3 runs + fresh instance identical), UT05-129 (equivalent / drift / values_only with match+mismatch), IT05-07 (13/13, 50/50, mixed 63), ST05-11/12/23, FT05-03, FT05-05, BT05-09.

### Strengths
- Faithful step-by-step implementation; clean split into two private siblings keeps verifier.py within budget.
- Strong tests: real DuckDB builds, real ops store for ST05-12, determinism checked against a fresh instance.

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
1. herness/harness/_verifier_rerun.py:292 and :302 — `wh.schema()` and `wh.cursor()` sit outside any try; a `duckdb.Error` (e.g. the handle closed by `WarehousePool` LRU eviction or `close_all` from another thread) escapes `_rerun`, against U05-63 "None escape". Same for :143-145 (`int(row_count)` on a NULL/odd `meta.evidence` row → TypeError). Wrap and map to `failed(...)` / `None`.
2. herness/harness/verifier.py:330-332 — the failure reason (`result drift`, `too large`, `guard: <rule>`, `build unavailable`, DuckDB text) is never logged or traced; `NumberCheck` has no error field, so the spec's "error text `result drift`" is unobservable (UT05-129 can only infer drift from `cached.error is None`). Add e.g. an INFO `harness.verifier.query_failed` (`query_id`, `error`) log.
3. herness/harness/_verifier_rerun.py:127-128 — the `meta.evidence` lookup runs without the rerun timer (low risk: point lookup).
4. herness/harness/_verifier_rerun.py:44-46, 217-219 — `_is_numeric` duplicates `herness.metrics.evidence._is_numeric`; `_EXACT_NUMERIC` includes FLOAT/REAL/DOUBLE (misnamed).
5. herness/harness/_verifier_rerun.py:270-272 — re-scanning kept rows for new keys copies up to 10,000 rows into `scan.rows` needlessly (pass a flag to skip keeping).

### Assessment
**Task quality:** Approved
**Reasoning:** All U05-63/64/67 steps are implemented and tested with real builds, gates and coverage pass, and the security-relevant changes (empty-schema filtering, replacement scans off) do not weaken the guard; remaining items are Minor robustness/diagnosability polish plus ruled carry-overs.

---

## Re-review round 1 (commit e9b0bc2; scope M1, M2, M4 only)

### M1 — no exception escapes `_rerun`: ✅ resolved
- `_verifier_rerun.py` `_compute`: `pool.get` and `wh.schema()` now share one try that catches `FileNotFoundError, ConfigError, QueryError, duckdb.Error` → `build unavailable`. `open_warehouse` raises only ConfigError/QueryError, or duckdb.Error from its `SET` statements, so all of these are covered. `_execute` wraps `wh.cursor()` (`duckdb.Error` → `build unavailable`). The execute/stream path already mapped `duckdb.Error` (incl. InterruptException), `SchemaViolation` and `_TooLargeError`. The guard raises only `QueryError`.
- `load_meta`: non-str `sql` or `result_hash`, or non-int/bool `row_count` → `None` (missing_query) before any conversion. The `str()`/`int()` coercions are gone. JSON and query_id errors were already caught.
- Tests: `test_ut05_118_handle_closed_by_other_thread_is_query_failed` [schema, cursor] asserts query_failed with cached `error == "build unavailable"`. `test_ut05_117_meta_evidence_null_fields_missing_query` [row_count, sql, result_hash] asserts missing_query. Both assert the right outcome. (The builder notes the NULL-sql case already passed through the tamper check.)

### M2 — `harness.verifier.query_failed` INFO log without raw text: ✅ resolved
- `verifier.py` `_check_group` logs `query_id` and `reason` only. `reason` is `result drift`, or `Rerun.reason`, which is a fixed category: `too large` / `build unavailable` / `guard: <rule>` / `duckdb: <ExceptionClass>`. The guard rule is a fixed rule name (`QueryError("SQL guard: <rule>")`) with no SQL or hint text. The DuckDB message stays only in the cached `Rerun.error` and is never logged; no other log or trace in the module carries it.
- Test `test_ut05_118_query_failed_logs_category_only` makes a ConversionException that quotes `'Jane Doe INC0099999'`, a guard rejection and a drift. It asserts the three reasons and the info level, that "Jane" is absent from the log events, and that it is present in the cached error. The outcomes are correct.

### M4 — `_EXACT_NUMERIC` naming: ✅ resolved
- Renamed `_NUMERIC_TYPES`. Keeping a local `_is_numeric` rather than importing the private `herness.metrics.evidence._is_numeric` is acceptable (no private cross-module import).

### Gates (run by reviewer)
- Card test set + coverage: 467 passed, 1 skipped (symlink privilege). verifier.py 100 %, _verifier_items.py 100 %, _verifier_rerun.py 99 % (141-142: JSON/query_id error branch in `load_meta`).
- ruff check / format --check clean; mypy --strict 0 issues; lint-imports 13 kept, 0 broken; check_module_size exit 0 (verifier.py 386, _verifier_rerun.py 327).

### New findings
- Minor (nit): `verifier.py` `_check_group` — the `rerun.reason or "error"` fallback can never be reached, because `failed()` always sets `reason`. It is harmless.

### Assessment
**Task quality:** Approved
**Reasoning:** M1, M2 and M4 are fixed as intended, the new tests assert the right outcomes, and all gates pass. M3 and M5 stay parked as Minor.

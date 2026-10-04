# T04-05 review: Recorded execution (verify agent)

Head 758f31d, base a59bb45. Reviewed the diff and the cited spec sections (U04-10..U04-13, §6 failure table, §8, §13.3 VI04-01, DECISIONS R-12), plus these checks:
- `PYTHONUTF8=1 uv run pytest tests/unit/metrics -q -p no:logging`: 476 passed, 1 xfailed (the T04-08 xfail that was already there).
- ruff check and ruff format --check: clean. mypy on evidence.py, _recorded.py and both test files: no issues.
- `uv run lint-imports`: 13 kept, 0 broken. `uv run python -m tools.check_module_size`: exit 0. evidence.py is 381 lines (budget 390); _recorded.py is 252 lines (default budget 400).
- BT04-09 bench (cheap enough to run): 1 passed in 15.5 s, under the 25 s limit.
- Focused check: calling `con.interrupt()` on an idle DuckDB connection does nothing, and the next statement runs normally.

### Spec Compliance
- ✅ U04-10 RecordedQuery: it is a frozen, slotted dataclass with every field the spec lists. `to_evidence(run_id)` copies the fields 1:1 and returns `herness.core.types.Evidence`, imported through the re-export (R-01). The `rows is None or len(rows) == row_count` invariant is not checked at runtime (Minor 1).
- ✅ U04-11 IntoSpec: `__post_init__` checks the table against WRITABLE_TABLES and raises "table <t> is not writable by metrics". It raises "bad upstream query id" for a malformed ID and for upstream IDs given with `id_column == "query_id"`. The extra mode and id_column checks are safe additions.
- ✅ U04-12 run_recorded, step by step:
  - Signature: con, sql, params and producer are positional; build_id, into, timeout_s and max_rows are keyword-only.
  - Preconditions: `into` without a producer raises ConfigError. The params-size check measures `canonical_json(p)` against 65,536 bytes and raises "params too large".
  - Step 2: `normalize_sql`, then "--", "/*" or ";" in the result raises "recorded SQL must not contain comments or semicolons".
  - Step 3: reads `meta.build` and raises "meta.build must hold one row" if it does not hold exactly one row.
  - Step 4: the ID comes only from `ids.query_id`; the package never computes one itself.
  - Step 5: a `threading.Timer` interrupts the connection and is cancelled in `finally`.
  - Step 6a: streams Arrow batches of 10,000 rows, keeps the rows, feeds the accumulator per batch, and raises "result too large: more than <n> rows" with query_id.
  - Step 6b: CREATE OR REPLACE TABLE ... AS / INSERT INTO ... with `$__query_id` and `$__upstream`, the exact list_concat ID expression, a read-back filtered for append and unfiltered for replace, and the worker pool used when the count is at least HASH_PARALLEL_MIN_ROWS. `rows` is None.
  - Step 8: INSERT ... ON CONFLICT (query_id) DO NOTHING, read back the stored hash, and raise "nondeterministic result for <qid>" plus the `metrics.evidence.hash_conflict` log.
  - Steps 9 and 10: the `metrics.query.recorded` log (DEBUG, all six §8.1 fields, no SQL), the two §8.2 metrics with `producer` / `none` labels, `clock.now()` and `perf_counter`.
  - Errors: a timeout gives QueryError "timeout after <t>s" with query_id. A duckdb.Error gives QueryError(<message>) with query_id. `metrics.query.failed` is logged with query_id, template and error_class, as §6 and §8.1 require.
- ✅ VI04-01: DuckDB accepts named parameters in CTAS and INSERT ... SELECT, so the main path is used. The UT04-09 tests exercise it, which settles the item.
- ✅ Card tests:
  - UT04-07: two runs leave one evidence row, and the stored columns are checked.
  - UT04-08: with producer=None there is no evidence row, rows are returned, `to_evidence` maps fields 1:1, and the max_rows and sample-cap cases are covered.
  - UT04-09: replace, append and query_ids targets; the stored hash equals the re-run hash; parallel hash equals in-process hash; a spy confirms the pool is used exactly at the threshold; IntoSpec refusals are covered.
  - UT04-10: Decimal, date and datetime params give JSON types and sorted keys, the params text equals `ids.canonical_json`, and the ID equals `ids.query_id`. build_id handling and rejected inputs are covered.
  - ST04-06 (unit part): a tampered evidence hash, an edited score.funding value, and changed inputs on the same build are all detected.
  - ST04-09 (IntoSpec half): `IntoSpec("core.incident", ...)` is refused.
  - BT04-09: a slow, integration-marked bench on a 5M-row stand-in.
  - UT04-66 (shared with T04-08): timeout and DuckDB-error cases are covered.
- ✅ Acceptance check: `test_ut04_07_logs_query_recorded` captures the `metrics.query.recorded` event and asserts its level, fields and that it carries no SQL.
- ✅ R-12 metric path (question 2): compliant. R-12 fixes only the table and the single writer name. Impl 08 U08-19/U08-20 `record_counter` / `record_histogram` are the component-facing recorders; their buffered flush goes through `record_metric_samples`. herness.core.audit and herness.core.redact already record metrics this way. herness.metrics (L3) importing herness.store directly would also be a layering problem. Recommend a one-line spec note on U04-12 "Side effects": "through the impl 08 recorders (U08-19/U08-20), whose flush is record_metric_samples" (controller).
- ✅ Private split (question 3): acceptable. Public names stay in evidence.py. `_recorded` takes limits and the allowlist as arguments and imports evidence only inside functions (the same pattern `_encode.hash_arrow_batch` already uses). The C6 forbidden-list entry sits with the other metrics private modules, and lint-imports is green. Needs the module-map row the builder proposed (controller spec note).

⚠️ Cannot verify from the diff and unit tests:
- ⚠️ BT04-09 runs on a stand-in SELECT, not on `metrics.incident_fact` from lake_small, because stage 400 and the synthetic lake do not exist yet. Re-run it on the real table once T04-06 and impl 11 land.
- ⚠️ The other half of ST04-09 (`persist=True` refused on a read-only connection) belongs to T04-20. The `score_evidence_coverage` part of ST04-06, and IT04-01 / IT04-11, belong to later cards.
- ⚠️ Evidence.sql max_length 20,000 (question 5): this is a cross-spec limit, not a T04-05 defect. Impl 05 U05 caps Evidence.sql at 20,000 characters, and impl 04 U04-15 caps a MetricDef's `sql` template at 20,000 characters. The rendered wrapper can still exceed that, and so can the scoring templates, which have no cap. `to_evidence` would then raise a pydantic ValidationError, which is not a taxonomy error. Recommend a spec note, owned by T04-08 / impl 05: bound the rendered SQL, or state that `to_evidence` maps a ValidationError to SchemaViolation.

### Strengths
- Parallel hashing is correct:
  - `hash_arrow_batch` is a top-level, picklable, pure function.
  - Batches are serialized to Arrow IPC.
  - The window of futures is bounded at 2 per worker and drained in submission order, so digests merge in batch order.
  - Parallel and in-process hash and sample are shown equal on Windows spawn (2 workers, 25k rows).
- Only allowlisted table names reach SQL text (TH04-09). All values are bound.
- Error mapping is tight: the timeout message is chosen from a flag the timer sets before interrupting, not by parsing DuckDB text.
- Logs carry no SQL, filter values or secrets.
- The tests check behaviour, not only that calls happen: stored-table hashes, evidence columns, no table left after a refused call, the connection still usable after a timeout.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/metrics/evidence.py:233-261: the U04-10 invariant `rows is None or len(rows) == row_count` is documented but not checked, for example in `__post_init__`. Today it holds by construction; a check would protect direct construction such as MetricResult.recorded() in a later card.
2. herness/metrics/_recorded.py:98-117 with evidence.py:333-335: the timer only calls `con.interrupt()`. With `into`, `_execute` runs several statements (materialize, count, select). If the timer fires between two statements, the interrupt does nothing (verified: an idle interrupt is ignored), so the rest runs with no time limit. This is harmless today, because callers that use `into` pass no timeout (spec lines 1068 and 1342). Consider checking `fired` after each statement, or documenting the limitation.
3. herness/metrics/_recorded.py:193-204 and evidence.py:327-340: errors that are neither DuckDB errors nor taxonomy errors escape unmapped and without the `metrics.query.failed` log. Examples: BrokenProcessPool or a pickling error from the worker pool, pa.ArrowInvalid, or a ValueError from json.dumps(allow_nan=False) in record_evidence. Consider mapping pool and Arrow failures to QueryError with query_id.
4. herness/metrics/evidence.py:251-254: `to_evidence` can raise pydantic ValidationError, which is not a taxonomy error (the SQL-length case above).
5. The spec notes the controller needs to add: the module-map row for `herness/metrics/_recorded.py`, the R-12 recorder route, and the extra IntoSpec and precondition messages the builder added (report deviations 3 and 4).

### Assessment
**Task quality:** Approved
**Reasoning:** Every U04-10..U04-12 step, error message and invariant (except the unchecked, documented RecordedQuery invariant) is implemented and backed by behavioural tests, including the `metrics.query.recorded` log-capture acceptance check. All gates are green. The metric path through the impl 08 recorders complies with R-12, and the remaining items are minor hardening or spec notes.

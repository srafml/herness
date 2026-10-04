# T04-05 report: Recorded execution (RecordedQuery, IntoSpec, run_recorded)

Status: DONE_WITH_CONCERNS (minor, see Concerns). Branch worktree-agent-a439f00a110f69782, base a59bb45.
Checkpoint: fa06e5b wip(T04-05): recorded execution green (unit tests). Final commit: 758f31d feat(metrics): recorded execution — RecordedQuery, IntoSpec, run_recorded (T04-05)

## Built
- `herness/metrics/evidence.py` (381 lines, budget 390): `RecordedQuery` (U04-10, frozen/slots dataclass; `to_evidence(run_id)` returns `herness.core.types.Evidence` via model_validate, 1:1 field copy), `IntoSpec` (U04-11; `__post_init__` checks WRITABLE_TABLES, mode, id_column, upstream IDs `^q_[0-9a-f]{16}$` and `()` with `query_id`), `run_recorded` (U04-12 orchestration of steps 1-10, error mapping, `metrics.query.failed` / `metrics.query.recorded` logs, metric samples), `Producer` type alias. `HASH_PARALLEL_MIN_ROWS` / `HASH_WORKERS` are read at call time (monkeypatchable).
- `herness/metrics/_recorded.py` (252 lines, NEW private sibling; default budget 400): `prepare` (preconditions, steps 1-2: canonical params, 64 KiB check on `canonical_json(p)`, `normalize_sql`, comment/semicolon refusal), `query_error`, `read_build_id` (step 3), `interrupt_after` (step 5: `threading.Timer(timeout_s, ...)` sets a flag then `con.interrupt()`, cancelled in finally), `collect_rows` (6a: `to_arrow_reader(10_000)`, per batch `iter_batch_rows` + `HashAccumulator.add_rows`, max_rows check), `materialize` + `read_back` (6b: CTAS / INSERT...SELECT with `$__query_id` / `$__upstream`, filtered read-back, count >= HASH_PARALLEL_MIN_ROWS goes to `hash_in_workers`), `hash_in_workers` (ProcessPoolExecutor(HASH_WORKERS); each batch serialized to Arrow IPC bytes; `_encode.hash_arrow_batch` (top-level, picklable); merged with `add_digests` in batch order; bounded in-flight window of 2 x workers), `record_evidence` (step 8: INSERT ... ON CONFLICT DO NOTHING, read back the stored hash, `metrics.evidence.hash_conflict` + `SchemaViolation("nondeterministic result for <qid>")`). `canonical_params` / `iter_batch_rows` are imported function-level from evidence (cycle; same pattern as `_encode.hash_arrow_batch`).
- `pyproject.toml`: `herness.metrics._recorded` added to the C6 "settings modules are leaves" forbidden list next to the other metrics private modules.
- `tests/unit/metrics/test_metrics_catalog.py`: mis-attributed `# T04-05:` marker (ST04-04 schema scan) relabelled `# T04-06:` per controller ruling. This card ships no .sql/.sql.j2 file.
- Tests: `tests/unit/metrics/test_metrics_recorded.py` (430 lines, pytestmark unit, 31 tests), `tests/bench/test_metrics_hash_bench.py` (56 lines, pytestmark [integration, slow]).

Module-map row needed: `herness/metrics/_recorded.py` | Private DuckDB mechanics of recorded execution (prepare, timer, collect, materialize/read-back, parallel hash, evidence write), re-exported through `evidence.run_recorded` | L3 | duckdb, pyarrow | 260 (suggested).

## VI04-01 outcome
DuckDB 1.5.5 ACCEPTS named parameters in `CREATE OR REPLACE TABLE ... AS SELECT` and `INSERT INTO ... SELECT` (verified with `$__query_id`, `$__upstream` VARCHAR[] via `list_concat`, and user binds). The main path of U04-12 step 6b is used; the fallback (add column + parameterized UPDATE) was not needed. Note: DuckDB rejects unused named parameters ("Parameter argument/count mismatch"), so `bind` must hold exactly the names the SQL uses (render.py already makes `bind` = used names).

## Tests covered (IDs)
- UT04-07: two runs with producer="metrics" give one meta.evidence row; stored columns checked (normalized sql, params = canonical_json(p), sample JSON, producer); metric samples; log-capture test asserts `metrics.query.recorded` (DEBUG; query_id, producer, template, row_count, duration_ms, build_id; no SQL). This is the card's acceptance check.
- UT04-08: producer=None gives no evidence row; rows/columns returned; hash = result_hash(rows); to_evidence maps all fields (run_id set / None); max_rows gives QueryError "result too large: more than 2 rows" with query_id; sample capped at 50 over 25,000 rows (3 Arrow batches).
- UT04-09: into replace (hash = re-run of the SELECT = hash of stored rows without id column; rows None; query_id column filled), append (query_id and query_ids, filtered read-back), query_ids = [qid, *upstream]; parallel path: HASH_PARALLEL_MIN_ROWS monkeypatched to 10 and HASH_WORKERS to 2, parallel hash and sample equal the in-process ones and the non-materialized re-run; spy test that the pool is used exactly when count >= threshold; IntoSpec refusals (core.incident, meta.evidence, injection-shaped name, bad or misplaced upstream, bad mode / id column); into without producer gives ConfigError and writes no table.
- UT04-10: Decimal/date/datetime params give JSON types, sorted keys, params text = ids.canonical_json, query_id = ids.query_id(normalize_sql(sql), p, build); build_id argument vs meta.build (0 rows, 2 rows, missing table give SchemaViolation "meta.build must hold one row"); rejected inputs (`--`, `/*`, `;`, params > 64 KiB, wrong params keys, bad bind name) give ConfigError and no evidence row; trailing semicolon normalized.
- UT04-66 (shared with T04-08): timeout_s=0.1 on a slow query gives QueryError "timeout after 0.1s" with query_id, `metrics.query.failed` logged, connection usable afterwards; DuckDB errors give QueryError with the message and query_id.
- ST04-06 (unit part): a tampered meta.evidence hash makes the re-run raise SchemaViolation "nondeterministic result for <qid>" and log `metrics.evidence.hash_conflict` (query_id, build_id); an edited score.funding value no longer matches its evidence hash; changed inputs on the same build give a hash conflict with still one evidence row.
- ST04-09 (IntoSpec half): IntoSpec("core.incident", ...) refused; WRITABLE_TABLES holds only metrics.* / score.*.
- BT04-09: 5,000,000-row fact-shaped stand-in (lake_small / stage 400 not built yet) through run_recorded(into=replace, producer="facts") with HASH_WORKERS=8 on this machine: 15.9 s (< 25 s). Marked [integration, slow]; not in the fast suite.

## Evidence
- RED: `PYTHONUTF8=1 uv run pytest tests/unit/metrics/test_metrics_recorded.py -q -p no:logging` gave `ImportError: cannot import name 'IntoSpec' from 'herness.metrics.evidence'` (1 error during collection).
- GREEN: same command gives 31 passed in 2.6 s. `PYTHONUTF8=1 uv run pytest tests/unit/metrics -q -p no:logging` gives 476 passed, 1 xfailed (pre-existing T04-08 xfail).
- Coverage (recorded + evidence tests): evidence.py 100 % line/branch; _recorded.py 99 % (one defensive branch).
- Gates: ruff format/check clean; mypy on touched files clean; lint-imports 13 kept, 0 broken; `uv run python -m tools.check_module_size` exit 0; pre-commit hooks (incl. pytest-unit, detect-secrets, module-size, type-ownership) passed on commit.

## Deviations / spec notes
1. Metric samples (§8.2 `herness_metrics_query_duration_seconds` histogram, `herness_metrics_query_rows_total` counter, label `producer` in facts/metrics/score/none) go through the impl 08 buffered recorders `herness.core.resilience.metrics.record_histogram` / `record_counter` (U08-19/U08-20), whose flush is the single writer `herness.store.ops.metrics.record_metric_samples` (R-12), the same pattern herness.core.audit and herness.core.redact use. A direct per-query `record_metric_samples` call would open a SQLite write per recorded query and fail the query whenever the ops store is unavailable, while impl 08 makes metrics best effort. If the reviewer wants the direct call, it is a 3-line change.
2. File split: run_recorded pushed evidence.py over 390, so the DuckDB mechanics live in the private sibling `_recorded.py` (module-map row needed, above). Public names stay in evidence.py.
3. IntoSpec also rejects a bad `mode` / `id_column` at runtime ("bad into mode", "bad into id column"); the spec lists messages only for table and upstream IDs. Upstream IDs given with `id_column="query_id"` raise "bad upstream query id".
4. Precondition messages the spec does not give verbatim: "into requires a producer: stored results are always recorded", "params must be exactly the mappings bind and template".
5. An unreadable `meta.build` (missing table / DuckDB error) is also reported as SchemaViolation("meta.build must hold one row").
6. `metrics.query.failed` (ERROR; query_id, template, error_class) is logged for every taxonomy error once the query_id is known (including the hash conflict); the `template` log field is `params["template"]["name"]` (None when absent).
7. result_sample is stored in meta.evidence as compact JSON preserving column order (not key-sorted canonical_json); params use canonical_json(p).

## Concerns / carry-overs
- ST04-09 other half (`persist=True` on a read-only connection refused) belongs to the portfolio API (T04-20); the ST04-06 `score_evidence_coverage` part belongs to the scoring cards.
- IT04-01 / IT04-11 are integration tests of later cards; nothing here defines canonical_json / normalize_sql / query_id (`query_id` appears only as a dataclass field and log key).
- `Evidence.sql` has max_length 20,000 (owner 05): `to_evidence` raises a pydantic ValidationError for a recorded SQL longer than that (large rendered scoring templates may approach it). Flag for T04-08 / impl 05 owners.
- DuckDB error text is passed through as the QueryError message (spec: `QueryError(<DuckDB message>)`); DuckDB conversion errors can echo a literal value from the query.

## Fix round 1
- Commit: 62ffd86 fix(metrics): check RecordedQuery rows invariant (T04-05).
- Review Minor 1: `RecordedQuery.__post_init__` now enforces the U04-10 invariant `rows is None or len(rows) == row_count`. Spec note: the wording is SchemaViolation("recorded rows do not match row_count"). U04-10 says "Errors none", but this case breaks the data contract, so SchemaViolation matches the rest of the module.
- Test: `test_ut04_08_rows_invariant`. A mismatched `rows` raises; `rows=None` is accepted.
- evidence.py is 386/390 lines; check_module_size exits 0. tests/unit/metrics: 477 passed, 1 xfailed. ruff and mypy on the touched files are clean, and the pre-commit hooks passed.

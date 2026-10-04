# T03-05 Text stage - build report

Status: DONE_WITH_CONCERNS (spec deviations below; no blocking issue)
Worktree: D:\herness\.claude\worktrees\agent-ab2f686106ea93b0a (branch worktree-agent-ab2f686106ea93b0a, base a59bb45)
Commits: 200fa3d wip(T03-05): text helpers and stage with unit tests; c96598f feat(enrich): T03-05 text stage (hooks passed on both)

## Implemented
- herness/enrich/text.py (258 lines / budget 260): normalize_text (U03-24), compose_text (U03-25),
  content_hash (U03-26), pair_text (U03-27), build_text_redacted (U03-28).
  `report` typed against module-private Protocol `_Report` (rows, cache_hits, failed) with
  comment "T03-xx pipeline: retype to StageReport (U03-142, herness.enrich.pipeline)".
  Redaction called as `redact.redact_table(tbl, ["text"], "record_id")` via `from herness.core import redact`.
- tests/support/text_warehouse.py: hand-built warehouses (real 000_settings.sql + minimal core.incident/
  core.change (record_id, source_updated_at, short_description, description) and core.problem
  (record_id, source_updated_at, root_cause_text); core DDL 100-299 is not in the tree).
- tests/unit/enrich/test_enrich_text.py (unit): UT03-22, UT03-23, UT03-24, PT03-03 (hypothesis), UT03-25 (stub redactor).
- tests/integration/enrich/conftest.py (`redaction_on`: full config + fake keyring key -> real redact_table),
  tests/integration/enrich/test_enrich_text_incremental.py (IT03-02, integration, real redact_table + spy wrapper),
  tests/integration/enrich/security/test_enrich_text_security.py (ST03-02 text-stage half, integration).

## Algorithm notes / deviations
1. ATTACH parameter: DuckDB 1.5.5 rejects `ATTACH ? AS prev (READ_ONLY)` (ParserException at "?").
   The path is quoted as a SQL string literal ('' doubling); a path with a control character is refused
   (logged enrich.text.prev_unavailable error_type=InvalidPath). Any ATTACH failure -> WARNING and redact all.
2. "record_id not in the inserted set": computed with an anti-join
   `NOT EXISTS (SELECT 1 FROM enrich.text_redacted t WHERE t.entity = ? AND t.record_id = c.record_id)`
   (NULL-safe, unlike NOT IN); rows with NULL record_id are skipped. Selection ordered by record_id.
   Copy step joins prev.enrich.text_redacted with core.<t> and prev.core.<t> on record_id with
   `cur.source_updated_at = old.source_updated_at` (NULL timestamps are not copied -> re-redacted).
3. Chunks: a separate `wh.cursor()` streams the selection with `to_arrow_reader(20_000)` so inserts on `wh`
   do not end the stream (`fetch_record_batch` is deprecated and fails under filterwarnings=error).
   Inserts via registered Arrow view `_enrich_text_chunk` with `INSERT ... BY NAME SELECT *`, unregistered in finally.
4. DETACH: done in `finally`, so also on failure paths; a DETACH error is logged (enrich.text.detach_failed,
   WARNING) and never raised, so it cannot mask the stage's own error.
5. SchemaViolation message: "text stage <entity>: <reason>". For CatalogException/BinderException the reason
   is the first line of the DuckDB message (names only); for any other DuckDB error (e.g. ConversionException,
   constraint errors, which can quote row values) the reason is the error class name only and the cause is
   not chained (`from None`) so no record text reaches the message or a traceback (ENG §3.4). Spec text says
   "<duckdb message>" - this is the deviation.
6. report counters are added to (+=), not assigned: rows = copied + redacted, cache_hits = copied, failed = NULL
   redactions. Log `enrich.text.redacted` once per stage with rows, copied, redacted, failed, empty,
   prev_attached, duration_ms (no text).
7. StoreBusy (pool crash) / ConfigError from redact_table propagate unchanged.
8. No transaction wrapping: a failure mid-stage leaves already-inserted rows (spec silent; precondition is an empty table).

## Carry-overs
- ST03-02 spy decider / spy encoder half (model inputs equal text_redacted or pair texts) needs encode/decide
  stages and spec 11 tiny_build: not built; stated in the test module docstring.
- Acceptance "on tiny_build": tiny_build does not exist; tests use hand-built DuckDB warehouses (controller ruling).
- StageReport (U03-142) retype marker in text.py.
- IT03-02 uses the real redact_table (in-process: every chunk is below the pool's 20,000-row chunk size).

## Evidence
- RED: the implementation was drafted together with the tests (not strictly test-first); first run failed
  6 UT03-25 tests (SystemError from deprecated fetch_record_batch under warnings-as-errors) and one regex
  mismatch, fixed before the checkpoint commit.
- GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/enrich tests/integration/enrich -q -p no:logging`
  -> 339 passed, 1 skipped (pre-existing symlink skip).
  Card selection `-k "UT03_22 or UT03_23 or UT03_24 or UT03_25 or PT03_03 or IT03_02 or ST03_02"` -> 30 passed;
  coverage herness/enrich/text.py 100 % line, 100 % branch. `--require-test-ids` on the new files: pass.
- Gates: ruff format/check clean; mypy (whole configured tree) 0 errors in 183 files; lint-imports 13 kept 0 broken;
  check_module_size exit 0; check_type_ownership exit 0.
- detect-secrets: sha256("abc") test vector marked with inline `# pragma: allowlist secret`; baseline unchanged.
  Test emails use the reserved corp.test domain and are built at runtime.

## Fix round 1 (review: Approved with Minors)
- Minor 1: build_text_redacted now refuses a connection inside an explicit transaction before any write:
  it runs `SELECT txid_current()` twice. With auto-commit on, each statement gets a new transaction id, so the two
  values differ. Inside an open transaction they are equal, and the stage raises
  SchemaViolation("text stage: connection must be in auto-commit mode").
  I checked this on DuckDB 1.5.5 (auto: 4 then 5; inside BEGIN: 6 then 6). A BEGIN probe is unusable: its
  failure aborts the caller's transaction. New test test_ut03_25_open_transaction_is_refused (nothing redacted, no rows).
  To stay within budget, the _duck_reason docstring was cut to one line; text.py is 258/260.
- Minor 4: test_ut03_25_detach_failure_is_logged closes its in-memory connection (try/finally).
- Evidence: `PYTHONUTF8=1 uv run pytest tests/unit/enrich tests/integration/enrich -q -p no:logging` -> 340 passed,
  1 skipped. Card -k selection -> 31 passed. text.py coverage is 100 % line and branch. ruff/mypy clean on touched files;
  check_module_size exit 0.

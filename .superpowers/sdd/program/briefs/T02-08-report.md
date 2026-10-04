# T02-08 report: Vector store

Status: DONE_WITH_CONCERNS (OI-12 resolved to the spec's fallback; two small spec-API substitutions)
Commit: 4a6e296 feat(store): add LanceDB vector store (T02-08)
Worktree/branch: D:\herness\.claude\worktrees\agent-a1af0ed3d76167e88 / worktree-agent-a1af0ed3d76167e88

## Built
- herness/store/vectors.py (232 lines, budget 260): EMBEDDING_DIM=1024, TICKET_EMBEDDING_SCHEMA,
  MEMORY_EMBEDDING_SCHEMA (names/types/nullability verbatim from U02-63), `type TableName`,
  `type HealthResult`, VectorStore(path=None -> data_layout().vectors) with ensure_tables, table,
  delete_ids, purge_history, count, health.
  - __init__: mkdir + lancedb.connect; OSError/RuntimeError/ValueError -> SchemaViolation("cannot open vector store").
  - ensure_tables: create missing with declared schema (log store.vectors.table_created); verify
    names+types of existing tables in both directions (missing, extra, retyped field) ->
    SchemaViolation("vector table <name> schema mismatch: <field>"); "already exists" from a
    racing creator tolerated by re-listing.
  - table/count: name allowlist -> ConfigError; missing -> NotFoundError(kind="vector_table", key=name).
  - delete_ids: column allowlist per table; ids must be a non-str collection of 1..100000 items each
    fullmatching [A-Za-z0-9_:.|-]{1,256} (ConfigError before any deletion; the bad id is never echoed,
    only its index); deduped; count_rows per 500-id IN-filter chunk, delete only if rows>0 (no empty
    version); log store.vectors.deleted (table, rows); "conflict" errors -> StoreBusy, others -> SchemaViolation.
  - purge_history: table.optimize(cleanup_older_than=timedelta(0), delete_unverified=True);
    conflict -> StoreBusy, other -> SchemaViolation; log store.vectors.history_purged.
  - health: listing fails -> ("down", class name); missing table -> ("degraded", "missing vector tables: ...");
    else ("ok", "vector tables present"). Never raises.

## OI-12 finding (checked inside UT02-51)
Pinned lancedb 0.39.0: `LanceTable.compact_files()` is deprecated (DeprecatedWarning, "use Table.optimize")
and raises ImportError because it needs the `pylance` package, which is not in uv.lock.
`cleanup_old_versions` exists but is also a lance-era path. `optimize(cleanup_older_than=, delete_unverified=)`
exists and works; per U02-68's own fallback clause it replaces both steps. Verified: after delete+purge only
one version remains; checkout of an older version raises "Version N no longer exists". Recommend closing OI-12
with "optimize(cleanup_older_than=timedelta(0), delete_unverified=True)".

## Deviations
1. U02-65/U02-70 say `table_names()`; in 0.39.0 it emits DeprecationWarning (and has a default limit=10),
   which the repo's `filterwarnings = error:::herness` would turn into failures. Used paginated
   `list_tables()` (following page_token) instead; semantics identical.
2. Commit-conflict detection: LanceDB has no typed conflict exception in 0.39.0; conflicts are detected by
   "conflict" in the error message (case-insensitive). Tested with monkeypatched failures only; no real
   concurrent-writer test (would need multiprocess/fault_plan of impl 08).
3. Non-"not found" ValueError on open_table -> SchemaViolation("vector open failed on <name>") (spec lists only
   ConfigError/NotFoundError for table(); this avoids mislabelling corruption as missing).
4. delete_ids also rejects a bare `str` for ids (a str is a Collection[str]) and non-str items -> ConfigError.

## Tests
- tests/unit/store/test_store_vectors.py (unit): UT02-49 (constants, ensure twice/idempotent, schemas,
  count, unknown name ConfigError on all entry points, NotFoundError, missing/extra/retyped field, concurrent
  create, create failure, connect failure, default path via data_layout, list_tables pagination, open failure),
  UT02-50 (3 rows delete 2 -> 2 & 1 left; 1203 ids chunked by content_hash; no-match -> 0 and no new version;
  9 bad-input cases; conflict -> StoreBusy), ST02-08 (injection -> ConfigError, count unchanged, id not echoed),
  UT02-51 (OI-12 API check; purge -> single version, old checkout fails; error mapping), UT02-52 (degraded,
  ok, down with class name).
- tests/integration/store/test_store_vectors_history.py (integration): ST02-09 delete, purge, open every
  remaining version -> deleted vector absent in all.
RED: `pytest tests/unit/store/test_store_vectors.py tests/integration/store/test_store_vectors_history.py`
  -> "2 errors during collection" (herness.store.vectors missing).
GREEN: same -> 33 passed; coverage herness/store/vectors.py 100% line, 100% branch.
Full: `pytest -m "(unit or integration) and not slow" -q -p no:logging` -> 361 passed, 1 skipped (symlinks), 4 deselected.

## Gates
ruff format --check: clean; ruff check: all passed (one test noqa S106 on a page-token literal);
mypy strict: no issues (24 files); lint-imports: 8 kept, 0 broken; tools.check_type_ownership: exit 0.
No pyproject change needed (herness.store already in the layers contract; lancedb ships py.typed).

## Concerns
- OI-12 should be closed in the spec with the optimize() form (and U02-65/U02-70 wording updated to list_tables()).
- Conflict mapping is message-based (see deviation 2).

## Fix round 1
Commit: see `git log` subject "fix(store): address T02-08 review round 1 (T02-08)" (on top of dd05904).
1. mypy arg-type in tests (lines 92/95): the loop cases are typed `tuple[tuple[TableName, pa.Schema], ...]`
   (TableName imported); no type-ignore needed. `mypy tests/unit/store/test_store_vectors.py
   tests/integration/store/test_store_vectors_history.py` -> no issues.
2. `_open` now catches OSError/RuntimeError/ValueError: ValueError "not found" -> NotFoundError; everything
   else -> `_store_error`. `count` wraps `count_rows()` the same way. `_store_error` treats messages containing
   "conflict", "busy" or "lock" (`_BUSY_MARKERS`) as StoreBusy, otherwise SchemaViolation (the module's
   fatal store error; U02-69 lists only NotFoundError, so SchemaViolation follows U02-68's "other errors"
   convention). New tests: test_ut02_49_open_os_and_runtime_errors_mapped (4 cases, table and count),
   test_ut02_49_count_errors_mapped (3 cases).
3. ST02-09 uses a distinctive deleted id/content_hash; asserts their bytes exist in files under
   memory_embedding.lance before purge_history and in no file after it (plus the per-version checkout loop).
Gates: ruff check clean; ruff format --check clean; mypy strict (25 files) clean; lint-imports 8 kept;
check_type_ownership exit 0. Card tests 40 passed, vectors.py 100% line/branch; full unit+integration
428 passed, 1 skipped, 4 deselected. vectors.py 239 lines (budget 260).

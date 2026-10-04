# T01-13 report — Phase 1 fault and orphan tests (tests only)

Worktree: D:\herness\.claude\worktrees\agent-a7d231cb4d0815a5d (base fb2d932). No herness/ source changed.

## Files
- tests/support/sync_kill.py (59) — NEW subprocess kill harness: `python -m tests.support.sync_kill <config_dir> <ops_db>`; loads the parent's config tree (MemoryKeyring), binds the parent's migrated ops store (`reset_connections(path=)`), runs `handle_sync` for `files` once, prints one `SYNC_RESULT {json}` line. Fault plan only from env (HERNESS_ENV=test + HERNESS_FAULTS), like a real worker. `KILLED_RETURNCODE` = -9 on POSIX, 15 on Windows (os.kill(SIGTERM) = TerminateProcess(15)).
- tests/fault/connectors/_files_env.py (92) — shared set-up: config tree = full test tree + lake_small mappings + `build: threads: 2` + files source (teams delta); committed lake file listing, raw lake table, real `run_build_pipeline` stage build, read-only build open, per-core-table duplicate count (rows − distinct rows), `stg.files_teams` rows.
- tests/fault/connectors/test_files_crash_fault.py (144) — FT01-02.
- tests/fault/connectors/test_orphan_temp_fault.py (120) — FT01-07.
- tests/unit/connectors/test_deletion_lake_security.py (132) — ST01-11.
- tests/unit/connectors/test_connectors_http_lint.py (159) — ST01-14 (+ self-tests).
No __init__.py (sibling test dirs have none; namespace imports as elsewhere).

## Per-test evidence
- FT01-02 (fault): real child with plan `[{"point":"connector.before_watermark","action":"kill","nth":1,"source":"files"}]`. Asserts: returncode == KILLED_RETURNCODE (15 on this Windows host), no result line, child output contains `resilience.faults.enabled` and `resilience.faults.kill`; lake commit happened (committed parquet with T1..T3), `file_ingest` empty, no `watermark` row for files, no temp file left. Rerun child (no plan): rows 3, `file_ingest` = exactly one row (fingerprint = sha256 of the inbox file, rows 3, `files` = the rerun's lake files only). Third child: rows 0, files [], still one row. Raw lake holds each record_id exactly twice (at-least-once: killed commit + rerun). Real core build over lake_small + files lake: row_counts == golden, core_snapshot == golden, every core.* table has 0 duplicate rows, `stg.files_teams` = exactly T1..T3 once each.
  - Note: the kill fires at the FIRST `connector.before_watermark` call, i.e. in `WriteLoop._commit` right after `LakeWriter.commit()` (files_ingest fires the point a second time in step e); both are after commit and before `record_file_ingest`.
  - RED evidence: rule label changed to `source: jira` → `assert 0 == <Signals.SIGTERM: 15>` (child ran to completion), 1 failed.
  - "core.* has no duplicates": core.* does not read files data in this version (140_stg_files.sql: OI-05), so the test proves it at every layer available: core.* tables of the full build (0 dup rows, golden-identical) AND `stg.files_teams` (the deepest layer fed by files; one row per record_id although the raw lake holds each twice).
- FT01-07 (fault, in-process): after a real sync, two valid-Parquet temp files named `.part-<ulid>.parquet.tmp-<ulid>` (checked against lakefiles `_TEMP_RE`) holding rows ORPHAN_OLD / ORPHAN_YOUNG, mtimes now−2 h and now−10 min, beside the committed file. Build #1: golden core counts, `stg.files_teams` = T1..T3 only (neither orphan read). Sync #2 (runner cleanup at start): 2 h file deleted, 10 min file kept, committed file untouched. Build #2: still no orphan row.
  - RED evidence: young age set to 3 h → `young.is_file()` fails.
- ST01-11 (unit): real LakeWriter, files source with delta `teams` and snapshot `roster` (+ a second snapshot file → reconcile tombstone file); one `running` deletion request per entity with distinctive key/name values. Every file under data/raw is scanned as raw bytes and as decoded cells (all columns incl. `_payload`): the deleted record_id, its key and its name value appear nowhere. Positive controls: kept record_ids appear in both raw bytes and decoded cells. RED evidence: without the deletion requests both scans find all six needles (raw hits list printed, decoded assertion fails).
- ST01-14 (unit): scanner = text regex (httpx.Client( / AsyncClient( / HTTPTransport( / AsyncHTTPTransport( anywhere incl. comments; `verify=False`) + AST (from-httpx imports of those names, `verify=False` keyword, `.put/.patch/.delete/.head/.options(` calls, `request/stream/build_request` whose method is not a literal GET/POST (a non-literal method is flagged), any `method=` literal PUT/PATCH/DELETE/HEAD/OPTIONS/TRACE/CONNECT, `import requests` / `from requests… import`). Scans all 18 modules of herness/connectors (asserts ≥ 15). Self-tests: 22 planted snippets each flagged; an allowed-forms snippet (get/post, request("GET"), method="post", verify=bundle, httpx.Timeout, auth method strings, the word "requests.") passes. First run flagged `.connect(` (duckdb.connect) → CONNECT removed from the attribute-call list (kept for `method=` literals).

## Markers and runtime
- FT tests: module `pytestmark = pytest.mark.fault` only (same as every tests/fault module); they are outside `-m "(unit or integration) and not slow"`, so the subprocess test does not bloat the default suite; runtime is far below the 30 s `slow` threshold, so no `slow`.
- `pytest -m fault -k "FT01_02 or FT01_07"`: 2 passed (FT01-02 5.6 s incl. 3 child processes + build, FT01-07 1.1 s).
- `pytest -k "ST01_11 or ST01_14"`: 25 passed (<1 s each).
- tests/unit/connectors: 574 passed, 4 skipped (host symlink skips).
- `--require-test-ids` on the new files: 27 passed.
- Gates: ruff format/check clean, mypy 0 issues (274 files), lint-imports 13 kept, check_type_ownership 0, check_module_size 0.

## Deviations
- Acceptance `-k` uses the underscore IDs (`FT01_02`…) per controller addendum; the card text has hyphens.
- `core.*` duplicate proof extended to `stg.files_teams` because no core table reads files (see above) — not faked: core.* is really built and checked, it simply cannot contain files rows.
- Full not-slow suite not run by the builder (dispatch: do not run the full suite); the pre-commit `pytest-unit` hook runs on each commit.

## Concerns
- None blocking. FYI: the X5 rule fires at the write-loop point (first of two `connector.before_watermark` calls per file); a plan with nth=2 would fire at the files_ingest step-e point instead — both land between commit and record.

## Final commit
- 8494a59 test(connectors): phase 1 fault and orphan tests (T01-13) — all pre-commit hooks passed (incl. pytest-unit). Checkpoint: 5fabee4 wip(T01-13).

## Review round 1 (fixes, test-only)
- I1: `.query(` added to the flagged client verbs and QUERY to the banned `method=` literals; `httpx.Request(...)` / `httpx2.Request(...)` / an imported (aliased) `Request(...)` construction is flagged unless its method is a literal GET or POST (non-literal flagged). Self-tests added.
- I2: `httpx2` treated like `httpx` in the text regex, the constructor check and the from-import check; module aliases tracked (`import httpx as h; h.Client()`, `import httpx2 as x; x.AsyncHTTPTransport()`, `import httpx.foo as h`). Self-tests added (13 new planted snippets; allowed-forms snippet extended with Request GET/POST). The real herness/connectors scan stays green (no product-code hits).
- m1: tests/support/sync_kill.py takes an optional third argument `source` (default `files`).
- m2: comments on both "no watermark row" asserts: files never writes a watermark, so they are guards, not the proof.
- Evidence: `pytest -k "ST01_11 or ST01_14"` 38 passed; `pytest -m fault -k "FT01_02 or FT01_07"` 2 passed; ruff check/format clean, mypy 0 issues.
- Round 1 commit: 102a8a2 test(connectors): T01-13 review round 1 (all hooks passed).

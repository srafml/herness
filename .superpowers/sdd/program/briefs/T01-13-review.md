# T01-13 review (verify agent) — Phase 1 fault and orphan tests

Worktree agent-a7d231cb4d0815a5d, head 8494a59, base fb2d932. Six new test files, no herness/ changes. Read-only review: two temporary test-file mutants were created and then deleted. A product-code mutation (moving the fault point before `LakeWriter.commit()`) was blocked by the permission classifier and not run. The product file is unchanged (checked).

## Runs
- `uv run pytest -m fault -k "FT01_02 or FT01_07" -q -p no:logging` -> 2 passed
- `uv run pytest -k "ST01_11 or ST01_14" -q -p no:logging` (whole tree) -> 25 passed; slowest 0.20 s setup / 0.16 s call
- `--require-test-ids` on the 4 test modules -> 27 passed
- `-m "(unit or integration) and not slow" --collect-only` over the new files -> 25/27 collected; the 2 FT tests are deselected
- ruff check: clean. ruff format --check: 6 files already formatted. `uv run mypy` (project config): 0 issues in 274 files. mypy `--explicit-package-bases` on the 6 test files: 0 issues.

## Spec
| Row | Verdict | Evidence |
|---|---|---|
| FT01-02 | ✅ | See 1 |
| FT01-07 | ✅ | See 2 |
| ST01-11 | ✅ | See 3 |
| ST01-14 | ✅ (meets the literal row), with Important gaps | See 4 |

### 1. FT01-02
- Plan source: a JSON file in tmp_path, passed only through `HERNESS_FAULTS`, with `HERNESS_ENV=test` in the child env. An inherited `HERNESS_FAULTS` is popped (test_files_crash_fault.py:202-207). No hook is monkeypatched. The real `handle_sync` -> `fault_point` -> `_ensure_loaded` -> `load_fault_plan` (faults.py:188-209). The asserted `resilience.faults.enabled` log comes only from that path.
- Kill location: the asserted `resilience.faults.kill` log comes only from `_kill_process` (faults.py:249-251). The return code must equal `KILLED_RETURNCODE` and no result line may appear, so a timeout or exception exit fails. The first matching call is `_write_loop._commit` line 146, after `writer.commit()` (141) and before `record_file_ingest` (files_ingest.py:183-184).
- Assertions: after the kill, the committed parquet holds T1..T3, `file_ingest` is empty, there is no watermark row and no temp file. The rerun writes exactly one `file_ingest` row (sha256 of the inbox file, rows 3, files = the rerun's files only). A third run adds 0 rows and still leaves one row. The raw lake Counter is exactly 2 per record_id. The full build gives golden counts and a golden core_snapshot, 0 duplicate rows per core.* table, and `stg.files_teams` = T1..T3 once each.
- Builder's core.* argument: TRUE. The 140_stg_files.sql header says no core table reads files (OI-05; 02-data-model.impl.md:2659, 4065). `grep files_ herness/model/sql` matches only 140_stg_files.sql. `stg.files_teams` is therefore the deepest observable layer (see W1).
- Mutations (temporary test copies, deleted afterwards):
  - Point `http.page` gives RED (`assert 0 == SIGTERM(15)`).
  - Child `HERNESS_ENV=prod` gives RED (same failure).
  - The builder reported RED for `source: jira`.
- Exit code: Windows `os.kill(SIGTERM)` is TerminateProcess(15), so rc 15. POSIX SIGKILL gives rc -9. sync_kill.py:25 is correct on both.

### 2. FT01-07
- The name `.part-<ulid>.parquet.tmp-<ulid>` matches lake.py:296-297, and `_TEMP_RE.fullmatch` is asserted (test_orphan_temp_fault.py:84).
- mtimes are now-2 h and now-10 min. The cleanup `max_age` is 1 h (lakefiles.py:77).
- The old file is deleted, the young file kept and the committed file untouched (113-117).
- "Build ignores both" is proven. The orphans are valid Parquet in the same partition, with live distinct rows (asserted at line 107). Exact equality of `stg.files_teams` to T1..T3 is asserted before and after cleanup (95, 109, 119).

### 3. ST01-11
- Real LakeWriter. Delta and snapshot entities are set up, plus a reconcile tombstone file.
- Every file under data/raw is scanned as raw bytes, per file, and as decoded cells including `_payload`. The needles are the record_id, the key and the name.
- Positive controls: the kept ids appear in both scans (129-132), so the test is not vacuous. Builder RED: without the deletion requests the needles are found.

### 4. ST01-14
- Covers the literal row (text regex plus AST). It scans all of herness/connectors (at least 15 modules asserted), and has 22 planted self-tests plus an allowed-forms test.
- Probed false negatives via `scan_source`:
  - NOT flagged: `import httpx as h; h.Client()`
  - NOT flagged: `httpx.Request("PUT")` / `client.send(httpx.Request("DELETE"))`
  - NOT flagged: `client.query(...)` (httpx2 QUERY method)
  - NOT flagged: `httpx2.Client(` / `httpx2.HTTPTransport(` / `from httpx2 import Client`. egress.py:281-302 builds **httpx2** clients.
  - NOT flagged: getattr/importlib/`**{"verify": False}`/`from httpx import *`.
  - Flagged correctly: `httpx.put`, `httpx.request("PATCH")`, `from httpx._client import Client`, urllib3 `.request("PUT")`, a variable method.
- Mitigation: ST10-25 (tests/security/test_st10_lint.py) and ruff TID251 already cover constructors, aliases, httpx2, star imports and `requests` for herness/connectors. Nothing covers the method gaps (`.query(`, `Request(non-GET/POST)` + `send`), and TH01-14 depends on them.

## Markers / harness
- FT modules use `pytestmark = pytest.mark.fault`, like every tests/fault module. They are excluded from `(unit or integration) and not slow`. ST modules are unit and take under 0.25 s.
- Test IDs are present and `--require-test-ids` passes.
- The harness writes only under tmp_path and the ops db. It is reusable apart from m1.

## ⚠️
- W1. core.* cannot observe files data until OI-05 lands. `m.latest` (`_macros.jinja:26-29`, QUALIFY row_number by `_record_id`) makes `stg.files_teams` unique per record_id by construction, so that check cannot fail from lake duplicates. The load-bearing evidence is the lake Counter == 2 and the single `file_ingest` row. Re-examine when OI-05 lands.
- W2. The watermark "no row" asserts (test_files_crash_fault.py:264, 281) are vacuous for files, which never writes a watermark. FT11-05 (impl 11) says "one watermark advance" and will need reconciling when T11 lands.
- W3. The card says tests go in tests/fault/connectors/, but the ST tests are in tests/unit/connectors/ (matches §11 locations for unit tests). Acceptable.

## Findings
### Critical
- none

### Important
- I1. tests/unit/connectors/test_connectors_http_lint.py:30-33,69-74: gaps in the method check with no other gate.
  - (a) `query` is missing from `_VERB_CALLS` and `"QUERY"` from `_OTHER_VERBS`. The httpx2 clients handed out by egress expose `.query`.
  - (b) `httpx.Request(...)` / `httpx2.Request(...)` with a non-GET/POST method (sent via `client.send`) is not flagged.
  - Add both checks plus self-test snippets.
- I2. test_connectors_http_lint.py:27,54: the constructor and import checks know only `httpx`. They miss `httpx2.*` (the project's actual client package) and `import httpx as h; h.Client()`, although the docstring claims "aliased imports". ST10-25 and ruff cover the product, but ST01-14 itself is weaker than it claims. Treat httpx2 like httpx, track module aliases, and add self-tests.

### Minor
- m1. tests/support/sync_kill.py:43: the source is hard-coded to `files`. Make it an argument for reuse.
- m2. tests/fault/connectors/test_files_crash_fault.py:264,281: comment that files never writes a watermark (W2).

## Verdict
**Needs fixes** (I1, I2: test-only hardening of ST01-14; FT01-02, FT01-07 and ST01-11 are approved as-is).
